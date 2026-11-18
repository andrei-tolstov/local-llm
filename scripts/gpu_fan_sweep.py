#!/usr/bin/env python3
"""
Tesla V100 fan / power sweep bench.

Измеряет тепловую характеристику пассивной Tesla V100 (вентилятор на каналах
NCT6779 pwm2 = CPU_FAN, pwm3 = SFAN3, инвертированная логика ШИМ: 255 = минимум
оборотом, 0 = 100 %) под максимальной нагрузкой (cublas HGEMM).

Логика прогона:
  1. Останавливается демон v100-fancontrol (иначе он перетирает ручной ШИМ).
  2. Выставляется режим GPU (power limit + lock clock) — воспроизводимый источник тепла.
  3. Запускается непрерывная нагрузка.
  4. ШИМ проходит ступени ОТ БОЛЕЕ ВЫСОКИХ ОБОРОТОВ К БОЛЕЕ НИЗКИМ
     (по возрастанию числа PWM), на каждой ступени ждём стационарного режима.
  5. Защита: GPU >= abort_temp или CPU package >= cpu_abort -> ШИМ = 0 (100 %),
     точка помечается как aborted.
  6. В finally: безопасный ШИМ, перезапуск демона, восстановление режима GPU.

Пример:
  python3 gpu_fan_sweep.py --pwm 40,70,100,130,150,170 --pl 250 --lgc 1380 \
      --min-duration 120 --max-duration 420 --out /tmp/sweep1.json
"""

import argparse
import csv
import glob
import json
import os
import signal
import subprocess
import sys
import time

GPU_INDEX = os.environ.get("GPU_INDEX", "0")
NCT_DIR = None


def find_hwmon(pattern="nct"):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(d, "name")) as f:
                if pattern in f.read().lower():
                    return d
        except Exception:
            pass
    return None


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


# ---------------- GPU telemetry ----------------

FIELDS = ("temperature.gpu,temperature.memory,power.draw,clocks.current.sm,"
          "utilization.gpu,utilization.memory,clocks_throttle_reasons.active")


def gpu_telemetry():
    out = sh(f"nvidia-smi -i {GPU_INDEX} --query-gpu={FIELDS} --format=csv,noheader,nounits")
    if not out:
        return None
    p = [x.strip() for x in out.split(",")]
    try:
        return {
            "gpu_temp": float(p[0]),
            "mem_temp": float(p[1]),
            "power_w": float(p[2]),
            "sm_mhz": float(p[3]),
            "gpu_util": float(p[4]),
            "mem_util": float(p[5]),
            "throttle_bits": int(p[6], 16) if p[6].startswith("0x") else int(p[6]),
        }
    except (ValueError, IndexError):
        return None


# throttle reason bit mask (NVML)
THROTTLE_NAMES = {
    0x1: "gpu_idle", 0x2: "clocks_request", 0x4: "sw_power_cap", 0x8: "hw_slowdown",
    0x10: "sync_boost", 0x20: "sw_thermal_slowdown", 0x40: "hw_thermal_slowdown",
    0x80: "hw_power_brake",
}


def throttle_str(bits):
    names = [n for b, n in THROTTLE_NAMES.items() if bits & b]
    return ",".join(n for n in names if n not in ("gpu_idle", "clocks_request")) or "none"


def reduce_or_bits(rows):
    bits = 0
    for h in rows:
        bits |= h["throttle_bits"]
    return bits & ~0x3  # игнорируем idle/clocks_request — это не троттлинг


def cpu_package_temp():
    """Package temp по coretemp (sysfs-обход, без зависимости от sensors)."""
    best = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(d, "name")) as f:
                if "coretemp" not in f.read():
                    continue
        except Exception:
            continue
        for lbl in glob.glob(os.path.join(d, "temp*_label")):
            try:
                if "package" not in open(lbl).read().lower():
                    continue
                inp = lbl.replace("_label", "_input")
                v = float(open(inp).read()) / 1000.0
                best = v if best is None else max(best, v)
            except Exception:
                pass
    return best


# ---------------- fan control ----------------

def set_pwm(value, channels=(2, 3)):
    v = max(0, min(255, int(value)))
    for ch in channels:
        try:
            with open(os.path.join(NCT_DIR, f"pwm{ch}_enable"), "w") as f:
                f.write("1")
            with open(os.path.join(NCT_DIR, f"pwm{ch}"), "w") as f:
                f.write(str(v))
        except Exception as e:
            print(f"!! pwm{ch} write failed: {e}", flush=True)


def duty_pct(pwm):
    return round((255 - pwm) * 100.0 / 255.0, 1)


def read_pwm(channel=3):
    try:
        return int(open(os.path.join(NCT_DIR, f"pwm{channel}")).read())
    except Exception:
        return 0


# ---------------- GPU power/clock mode ----------------

def gpu_mode(pl=None, lgc=None):
    if lgc:
        sh(f"nvidia-smi -i {GPU_INDEX} -lgc {lgc}")
    if pl:
        sh(f"nvidia-smi -i {GPU_INDEX} -pl {pl}")


def gpu_read_mode():
    out = sh(f"nvidia-smi -i {GPU_INDEX} --query-gpu=power.limit,clocks.max.sm "
             "--format=csv,noheader,nounits").split(",")
    try:
        return {"pl": float(out[0]), "max_sm": float(out[1])}
    except Exception:
        return {}


# ---------------- load ----------------

class Load:
    def __init__(self, cmd, safety_sec=900, log_path=None):
        self.cmd = cmd
        self.safety_sec = safety_sec
        self.log_path = log_path
        self.proc = None
        self.started = 0.0

    def ensure(self):
        if self.proc and self.proc.poll() is None and \
                time.time() - self.started < self.safety_sec:
            return
        self.stop()
        out = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(self.cmd, shell=True, preexec_fn=os.setsid,
                                     stdout=out, stderr=subprocess.STDOUT)
        self.started = time.time()
        print(f"  [load] restarted: {self.cmd}", flush=True)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                killed = False
                for _ in range(20):
                    if self.proc.poll() is not None:
                        killed = True
                        break
                    time.sleep(0.5)
                if not killed:
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                self.proc.terminate()
            time.sleep(2)
        self.proc = None


# ---------------- one PWM step ----------------

def run_step(load, pwm, args, log_rows):
    if pwm is not None:
        set_pwm(pwm)
        label = f"PWM {pwm} (duty ~{duty_pct(pwm)} %)"
    else:
        label = "MONITOR (ШИМ отдаёт демон)"
    print(f"\n=== {label} — прогрев ===", flush=True)
    t0 = time.time()
    history = []
    steady = False
    abort_reason = None
    last_print = 0.0

    while True:
        load.ensure()
        t = gpu_telemetry()
        if not t:
            time.sleep(1)
            continue
        t["t"] = round(time.time() - t0, 1)
        t["pwm"] = pwm if pwm is not None else read_pwm()
        t["cpu_temp"] = cpu_package_temp()
        history.append(t)

        if t["gpu_temp"] >= args.abort_temp or (t["cpu_temp"] or 0) >= args.cpu_abort:
            abort_reason = f"guard: gpu={t['gpu_temp']:.0f}C cpu={t['cpu_temp']}"
            set_pwm(0)  # 100 % обдув
            break

        now = time.time()
        if now - last_print > 20:
            last_print = now
            print(f"  t={t['t']:6.0f}s temp={t['gpu_temp']:5.1f}C mem={t['mem_temp']:5.1f}C "
                  f"{t['power_w']:6.1f}W {t['sm_mhz']:6.0f}MHz cpu={t['cpu_temp']}C "
                  f"throttle={throttle_str(t['throttle_bits'])}", flush=True)

        el = time.time() - t0
        win = max(20, int(args.steady_window))
        if not steady and el > args.min_duration and len(history) > win + 5:
            w1 = [h["gpu_temp"] for h in history[-win:]]
            dt = history[-1]["t"] - history[-win - 1]["t"]
            slope_per_min = (w1[-1] - w1[0]) * 60.0 / dt if dt > 0 else 99.0
            spread = max(w1) - min(w1)
            if abs(slope_per_min) < args.steady_slope and spread <= args.steady_spread:
                steady = True
                print(f"  [steady] slope {slope_per_min:+.2f} °C/мин, разброс {spread:.1f} °C",
                      flush=True)
        if steady or el > args.max_duration:
            break
        time.sleep(1.0)

    tail = history[-int(args.tail_avg_sec):] if history else []
    if not tail:
        return None
    avg = lambda k: round(sum(h[k] for h in tail if h.get(k) is not None) /
                          max(1, len([h for h in tail if h.get(k) is not None])), 1)
    eff_pwm = max(h["pwm"] for h in tail)  # инверсия: max PWM = минимальные обороты
    res = {
        "pwm": eff_pwm,
        "duty_pct": duty_pct(eff_pwm),
        "duration_s": round(time.time() - t0, 1),
        "steady": steady,
        "aborted": abort_reason,
        "gpu_temp_avg": avg("gpu_temp"),
        "gpu_temp_max": max(h["gpu_temp"] for h in tail),
        "mem_temp_avg": avg("mem_temp"),
        "mem_temp_max": max(h["mem_temp"] for h in tail),
        "power_avg": avg("power_w"),
        "sm_mhz_avg": avg("sm_mhz"),
        "cpu_temp_max": max((h["cpu_temp"] or 0) for h in tail),
        "throttle_last": throttle_str(history[-1]["throttle_bits"]),
        "throttle_any": throttle_str(reduce_or_bits(tail)),
    }
    log_rows.extend(history)
    print(f"  --> ИТОГ ступени: {res['gpu_temp_avg']}°C (max {res['gpu_temp_max']}), "
          f"mem {res['mem_temp_avg']}°C, {res['power_avg']}W, {res['sm_mhz_avg']}MHz, "
          f"throttle={res['throttle_last']}", flush=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pwm", default="40,70,100,130,150,170")
    ap.add_argument("--pl", type=float, default=None, help="power limit, W")
    ap.add_argument("--lgc", type=float, default=None, help="lock SM clock, MHz")
    ap.add_argument("--load-cmd", default="/tmp/gemm_bench 600")
    ap.add_argument("--min-duration", type=float, default=120)
    ap.add_argument("--max-duration", type=float, default=420)
    ap.add_argument("--steady-window", type=int, default=60, help="окно стационарности, с")
    ap.add_argument("--steady-slope", type=float, default=0.25, help="°C/мин")
    ap.add_argument("--steady-spread", type=float, default=1.5, help="°C в окне")
    ap.add_argument("--load-restart-sec", type=float, default=1800)
    ap.add_argument("--load-log", default=None, help="писать вывод нагрузки в файл")
    ap.add_argument("--tail-avg-sec", type=int, default=60)
    ap.add_argument("--abort-temp", type=float, default=85.0)
    ap.add_argument("--cpu-abort", type=float, default=78.0)
    ap.add_argument("--safe-pwm", type=int, default=120, help="ШИМ при выходе (инверт.)")
    ap.add_argument("--skip-daemon-stop", action="store_true")
    ap.add_argument("--monitor-only", action="store_true",
                    help="не трогать ШИМ и демона: только нагрузка + замеры (валидация кривой)")
    ap.add_argument("--out", default="/tmp/fan_sweep.json")
    args = ap.parse_args()

    global NCT_DIR
    NCT_DIR = find_hwmon()
    if not NCT_DIR:
        print("NCT hwmon not found", file=sys.stderr)
        sys.exit(1)

    if args.monitor_only:
        args.skip_daemon_stop = True
        steps = [None]
    else:
        steps = [int(x) for x in args.pwm.split(",") if x.strip()]
        steps.sort()  # инверсия: сначала низкие числа (высокие обороты) -> безопаснее всего
    prev_mode = gpu_read_mode()
    print(f"hwmon={NCT_DIR} GPU={GPU_INDEX} режим до теста={prev_mode}", flush=True)
    print(f"Ступени ШИМ (по возрастанию = по убыванию оборотов): {steps}", flush=True)

    load = Load(args.load_cmd, safety_sec=args.load_restart_sec)
    results = []
    raw_rows = []
    daemon_stopped = False
    try:
        if not args.skip_daemon_stop:
            subprocess.run("systemctl stop v100-fancontrol.service", shell=True)
            daemon_stopped = True
        gpu_mode(args.pl, args.lgc)
        if not args.monitor_only:
            set_pwm(steps[0])
        load.ensure()
        time.sleep(10)
        for pwm in steps:
            r = run_step(load, pwm, args, raw_rows)
            if r:
                results.append(r)
                if r["aborted"]:
                    print(f"!! ПРОГОН ПРЕРВАН: {r['aborted']}", flush=True)
                    break
    finally:
        load.stop()
        if not args.monitor_only:
            set_pwm(args.safe_pwm)
        if prev_mode and not args.monitor_only:
            gpu_mode(prev_mode.get("pl"), None)
        if daemon_stopped:
            subprocess.run("systemctl start v100-fancontrol.service", shell=True)
        print("\n[restore] ШИМ в безопасное положение, демон перезапущен", flush=True)

    with open(args.out, "w") as f:
        json.dump({"config": vars(args), "prev_mode": prev_mode, "results": results}, f,
                  indent=2, ensure_ascii=False)
    csv_path = os.path.splitext(args.out)[0] + "_raw.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(raw_rows[0].keys()) if raw_rows else ["pwm"])
        w.writeheader()
        w.writerows(raw_rows)

    print(f"\nСохранено: {args.out} + {csv_path}", flush=True)
    print("\n| PWM | duty % | T gpu ср/макс | T mem ср/макс | Вт | МГц | троттлинг | стационар |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['pwm']} | {r['duty_pct']} | {r['gpu_temp_avg']}/{r['gpu_temp_max']} | "
              f"{r['mem_temp_avg']}/{r['mem_temp_max']} | {r['power_avg']} | {r['sm_mhz_avg']} | "
              f"{r['throttle_last']} | {'да' if r['steady'] else 'нет'} |")


if __name__ == "__main__":
    main()
