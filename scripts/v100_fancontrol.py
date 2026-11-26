#!/usr/bin/env python3
"""
Supermicro X9DAX-HFT — автоматический контроллер охлаждения через IPMI BMC (v3).

Раздельное управление двумя ШИМ-зонами платы Supermicro X9 (Nuvoton WPCM450 BMC)
через `ipmitool raw 0x30 0x91 0x5A 0x03 <zone> <pwm>`:

  - Zone 0x10 (FAN1–FAN6, вентилятор Tesla V100 подключён к FAN3):
      ИНВЕРТИРОВАННЫЙ ШИМ серверного вентилятора:
      255 (0xFF) = стоп / минимум, 0 (0x00) = 100 % (максимальный обдув).
      Калибровка кривой v2 от реального порога троттлинга Tesla V100 (82 °C)
      с учётом температуры HBM2, гистерезисом и выдержкой.

  - Zone 0x11 (FAN7 = CPU1, FAN8 = CPU2):
      ПРЯМОЙ ШИМ башенных кулеров 2× Xeon E5-2696 v2:
      0 (0x00) = минимум, 255 (0xFF) = 100 % (~1725 RPM).
      Нижняя ступень (120 / 0x78, ~1050 RPM) держит обороты с запасом выше
      порога Supermicro BMC `lnc` (600 RPM), исключая ложные срабатывания.

Постоянное обновление ШИМ каждые 3 с также автоматически гасит кратковременные
всплески BMC (например, при помехе тахометра 3-pin вентилятора на FAN1).

Запуск: systemd (v100-fancontrol.service).
Отладка: --once, --set <GPU_PWM>, --set-cpu <CPU_PWM>, --release.
"""

import argparse
import glob
import os
import shutil
import signal
import subprocess
import sys
import time

# --------------------------------------------------------------- IPMI зоны
ZONE_GPU = 0x10           # FAN1–FAN6 (GPU на FAN3, инвертированный ШИМ)
ZONE_CPU = 0x11           # FAN7 (CPU1), FAN8 (CPU2) — прямой ШИМ

BMC_MODE_STANDARD = 0x00  # автоматический режим BMC
BMC_MODE_FULL = 0x01      # ручной режим (обязателен для 0x30 0x91)

# --------------------------------------------------------------- кривая GPU (инвертированная)
# (upper_temp, pwm): первая же строка, где control_temp <= upper_temp, задаёт режим.
# Инвертированный ШИМ: чем больше число, тем ТИШЕ. duty % = (255 - pwm) / 255.
# Калибровка на Supermicro X9DAX-HFT (FAN3, Zone 0x10) при полном TDP 250 Вт
# (cublasHgemm FP16, ~247 Вт, порог SW Thermal Slowdown = 82 °C):
#   PWM 60  (76.5 %) -> 73.0 °C (HBM2 68.0 °C, запас 9 °C до троттлинга)
#   PWM 90  (64.7 %) -> 76.2 °C (HBM2 72.2 °C, запас ~6 °C до троттлинга)
#   PWM 120 (52.9 %) -> >80 °C при 245 Вт (недостаточно для 250 Вт, но хватает для ~140 Вт)
GPU_CURVE = [
    (38.0, 170),   # ~33.3 % — тихий простой (как в v1, даёт прогреться до ~40 °C)
    (42.0, 150),   # ~41.2 % — базовый idle ~40 °C (как в v1)
    (52.0, 140),   # ~45.1 % — лёгкая нагрузка
    (64.0, 125),   # ~51.0 % — умеренный / плотный инференс
    (72.0, 110),   # ~56.9 % — высокая нагрузка
    (76.0, 90),    # ~64.7 % — полная нагрузка 250 Вт (~76 °C, тише чем 76.5 %)
    (78.5, 60),    # ~76.5 % — запас при 250 Вт (осаживает до ~73 °C, 9 °C до троттлинга)
    (1e9, 0),      # 100 % — аварийный режим
]

# --------------------------------------------------------------- кривая CPU (прямая)
# Прямой ШИМ: чем больше число, тем БЫСТРЕЕ. Минимум 120 (~1050 RPM > lnc 600 RPM).
CPU_CURVE = [
    (45.0, 120),   # ~47 % (~1050 RPM) — тихий простой, безопасно выше порога lnc
    (55.0, 145),   # ~57 % (~1200 RPM)
    (65.0, 180),   # ~71 % (~1400 RPM)
    (72.0, 215),   # ~84 % (~1550 RPM)
    (1e9, 255),    # 100 % (~1725 RPM) — максимальное охлаждение CPU
]

GPU_HYSTERESIS_C = 4.0     # на сколько должно остыть GPU, чтобы понизить ступень
GPU_DOWN_DELAY_S = 150     # минимальная выдержка повышенной ступени GPU
GPU_UP_DEBOUNCE_S = 12     # задержка перед разгоном GPU (фильтр коротких пиков)
HBM_OFFSET_C = -3.0        # HBM2 холоднее ядра на ~3 °C, предел выше (85 vs 82)

CPU_HYSTERESIS_C = 3.0     # гистерезис для CPU
CPU_DOWN_DELAY_S = 30      # выдержка ступени CPU
CPU_UP_DEBOUNCE_S = 6      # задержка перед разгоном CPU

FAILSAFE_GPU_PWM = 90      # ~65 % обдува GPU при остановке демона (выдерживает даже 250 Вт)
FAILSAFE_CPU_PWM = 180     # ~71 % обдува CPU при остановке/ошибке чтения

MAX_PWM = 255
MIN_PWM = 0
READ_FAIL_LIMIT = 3
PERIOD_S = 3.0
MODE_CHECK_INTERVAL_S = 60.0

GPU_INDEX = os.environ.get("GPU_INDEX", "").strip()
IPMITOOL = shutil.which("ipmitool") or "/usr/bin/ipmitool"


# --------------------------------------------------------------- IPMI BMC

def ipmi_raw(*hex_bytes):
    cmd = [IPMITOOL, "raw"] + [f"0x{b:02x}" for b in hex_bytes]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"ipmitool exited {res.returncode}")
    return res.stdout.strip()


def get_bmc_fan_mode():
    try:
        out = ipmi_raw(0x30, 0x45, 0x00)
        return int(out.split()[0], 16)
    except Exception:
        return None


def set_bmc_fan_mode(mode):
    ipmi_raw(0x30, 0x45, 0x01, mode)


def ensure_manual_mode():
    mode = get_bmc_fan_mode()
    if mode != BMC_MODE_FULL:
        set_bmc_fan_mode(BMC_MODE_FULL)
        time.sleep(0.3)


def set_zone_pwm(zone, value):
    val = max(MIN_PWM, min(MAX_PWM, int(value)))
    try:
        ipmi_raw(0x30, 0x91, 0x5A, 0x03, zone, val)
    except Exception as e:
        sys.stderr.write(f"Failed to set zone 0x{zone:02x} pwm={val}: {e}\n")


def set_gpu_pwm(value):
    set_zone_pwm(ZONE_GPU, value)


def set_cpu_pwm(value):
    set_zone_pwm(ZONE_CPU, value)


def revert_to_bmc_standard():
    """Возврат управления в автоматический режим BMC Standard (0x00)."""
    set_bmc_fan_mode(BMC_MODE_STANDARD)


def gpu_duty_pct(pwm):
    return int((MAX_PWM - pwm) * 100 / MAX_PWM)


def cpu_duty_pct(pwm):
    return int(pwm * 100 / MAX_PWM)


# --------------------------------------------------------------- телеметрия

def read_gpu_temps():
    """(gpu_temp, mem_temp) для Tesla V100 либо None, если nvidia-smi недоступен."""
    try:
        cmd = ["nvidia-smi"]
        if GPU_INDEX:
            cmd += ["-i", GPU_INDEX]
        cmd += ["--query-gpu=name,temperature.gpu,temperature.memory",
                "--format=csv,noheader,nounits"]
        out = subprocess.check_output(cmd, timeout=3).decode().strip()
        v100_rows = []
        all_rows = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            name, gpu_s, mem_s = parts[0], parts[1], parts[2]
            try:
                gpu = float(gpu_s)
            except ValueError:
                continue
            try:
                mem = float(mem_s) if mem_s not in ("N/A", "[N/A]", "") else gpu
            except ValueError:
                mem = gpu
            all_rows.append((gpu, mem))
            if "v100" in name.lower():
                v100_rows.append((gpu, mem))
        rows = v100_rows or all_rows
        if not rows:
            return None
        return max(r[0] for r in rows), max(r[1] for r in rows)
    except Exception:
        return None


def read_cpu_packages():
    """Возвращает максимальную температуру среди всех сокетов CPU (Package id 0/1)."""
    best = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(d, "name")) as f:
                if "coretemp" not in f.read().lower():
                    continue
        except Exception:
            continue
        for lbl in glob.glob(os.path.join(d, "temp*_label")):
            try:
                with open(lbl) as f:
                    if "package" not in f.read().lower():
                        continue
                with open(lbl.replace("_label", "_input")) as f:
                    v = float(f.read().strip()) / 1000.0
                best = v if best is None else max(best, v)
            except Exception:
                pass
    return best


# --------------------------------------------------------------- регулятор

def band_index(curve, temp):
    for i, (upper, _) in enumerate(curve):
        if temp <= upper:
            return i
    return len(curve) - 1


def step_band(curve, current_idx, control_t, now, held_since, rising_since,
              hysteresis_c, down_delay_s, up_debounce_s):
    """Универсальный ступенчатый регулятор с гистерезисом и выдержкой.
    Индекс 0 = самая тихая ступень, чем больше индекс — тем сильнее обдув."""
    want = band_index(curve, control_t)

    if want > current_idx:                       # нужно больше обдува
        if rising_since is None:
            rising_since = now
        if now - rising_since >= up_debounce_s:
            return want, now, None
        return current_idx, held_since, rising_since

    if want < current_idx:                       # можно стать тише
        rising_since = None
        if control_t > curve[current_idx - 1][0] - hysteresis_c:
            return current_idx, held_since, rising_since
        if now - held_since < down_delay_s:
            return current_idx, held_since, rising_since
        return want, now, rising_since

    return current_idx, held_since, rising_since


# --------------------------------------------------------------- запуск

def check_ipmi_available():
    if not os.path.exists("/dev/ipmi0"):
        sys.stderr.write("ERROR: /dev/ipmi0 not found! Load ipmi_devintf/ipmi_si modules.\n")
        sys.exit(1)
    if not os.path.exists(IPMITOOL):
        sys.stderr.write("ERROR: ipmitool not found! Install ipmitool package.\n")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description="Supermicro X9DAX-HFT IPMI fan controller (GPU Zone 0x10 + CPU Zone 0x11)"
    )
    ap.add_argument("--once", action="store_true", help="один замер и расчёт ступеней")
    ap.add_argument("--set", type=int, metavar="PWM",
                    help="ручной инвертированный ШИМ для зоны GPU (0x10): 255=стоп, 0=100%%")
    ap.add_argument("--set-cpu", type=int, metavar="PWM",
                    help="ручной прямой ШИМ для зоны CPU (0x11): 0=мин, 255=100%%")
    ap.add_argument("--release", action="store_true",
                    help="вернуть управление в автоматический режим BMC Standard (0x00)")
    args = ap.parse_args()

    check_ipmi_available()

    if args.release:
        revert_to_bmc_standard()
        print("BMC возвращён в автоматический режим Standard (0x00)")
        return

    if args.set is not None or args.set_cpu is not None:
        ensure_manual_mode()
        if args.set is not None:
            set_gpu_pwm(args.set)
            print(f"manual GPU zone 0x{ZONE_GPU:02x} pwm={args.set} (~{gpu_duty_pct(args.set)} % обдува)")
        if args.set_cpu is not None:
            set_cpu_pwm(args.set_cpu)
            print(f"manual CPU zone 0x{ZONE_CPU:02x} pwm={args.set_cpu} (~{cpu_duty_pct(args.set_cpu)} % обдува)")
        return

    if args.once:
        temps = read_gpu_temps()
        cpu_t = read_cpu_packages()
        if temps is None:
            print("GPU: nvidia-smi недоступен")
        else:
            control_t = max(temps[0], temps[1] + HBM_OFFSET_C)
            g_idx = band_index(GPU_CURVE, control_t)
            g_pwm = GPU_CURVE[g_idx][1]
            print(f"GPU: core={temps[0]:.0f}C mem={temps[1]:.0f}C control={control_t:.0f}C "
                  f"-> zone 0x{ZONE_GPU:02x} pwm={g_pwm} (~{gpu_duty_pct(g_pwm)} % обдува, band={g_idx})")
        if cpu_t is None:
            print("CPU: coretemp недоступен")
        else:
            c_idx = band_index(CPU_CURVE, cpu_t)
            c_pwm = CPU_CURVE[c_idx][1]
            print(f"CPU: max_pkg={cpu_t:.0f}C "
                  f"-> zone 0x{ZONE_CPU:02x} pwm={c_pwm} (~{cpu_duty_pct(c_pwm)} % обдува, band={c_idx})")
        return

    print(f"Starting Supermicro X9 IPMI fan controller: "
          f"GPU zone=0x{ZONE_GPU:02x} (inverted), CPU zone=0x{ZONE_CPU:02x} (direct)",
          flush=True)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    ensure_manual_mode()

    now = time.time()
    temps = read_gpu_temps()
    gpu_idx = (len(GPU_CURVE) - 1 if temps is None
               else band_index(GPU_CURVE, max(temps[0], temps[1] + HBM_OFFSET_C)))
    cpu_t = read_cpu_packages()
    cpu_idx = (band_index(CPU_CURVE, 65.0) if cpu_t is None
               else band_index(CPU_CURVE, cpu_t))

    set_gpu_pwm(GPU_CURVE[gpu_idx][1])
    set_cpu_pwm(CPU_CURVE[cpu_idx][1])

    gpu_held_since = now
    gpu_rising_since = None
    cpu_held_since = now
    cpu_rising_since = None
    gpu_fails = 0
    last_log = 0.0
    last_mode_check = now

    try:
        while True:
            now = time.time()

            # Периодически проверяем, не сбросился ли режим BMC (например, после MC reset)
            if now - last_mode_check >= MODE_CHECK_INTERVAL_S:
                last_mode_check = now
                try:
                    ensure_manual_mode()
                except Exception as e:
                    sys.stderr.write(f"WARNING: failed to check/set BMC mode: {e}\n")

            # --- GPU Zone (0x10, инвертированная) ---
            temps = read_gpu_temps()
            if temps is None:
                gpu_fails += 1
                gpu_pwm = MIN_PWM if gpu_fails >= READ_FAIL_LIMIT else GPU_CURVE[gpu_idx][1]
                if gpu_fails == READ_FAIL_LIMIT:
                    print("WARNING: nvidia-smi недоступен, включён 100 % обдув GPU", flush=True)
                gpu_t = mem_t = control_t = None
            else:
                gpu_fails = 0
                gpu_t, mem_t = temps
                control_t = max(gpu_t, mem_t + HBM_OFFSET_C)
                gpu_idx, gpu_held_since, gpu_rising_since = step_band(
                    GPU_CURVE, gpu_idx, control_t, now,
                    gpu_held_since, gpu_rising_since,
                    GPU_HYSTERESIS_C, GPU_DOWN_DELAY_S, GPU_UP_DEBOUNCE_S,
                )
                gpu_pwm = GPU_CURVE[gpu_idx][1]

            # --- CPU Zone (0x11, прямая) ---
            cpu_t = read_cpu_packages()
            if cpu_t is None:
                cpu_pwm = FAILSAFE_CPU_PWM
            else:
                cpu_idx, cpu_held_since, cpu_rising_since = step_band(
                    CPU_CURVE, cpu_idx, cpu_t, now,
                    cpu_held_since, cpu_rising_since,
                    CPU_HYSTERESIS_C, CPU_DOWN_DELAY_S, CPU_UP_DEBOUNCE_S,
                )
                cpu_pwm = CPU_CURVE[cpu_idx][1]

            # Применяем ШИМ на каждом цикле — гасит возможные сбросы BMC от помех тахометра
            set_gpu_pwm(gpu_pwm)
            set_cpu_pwm(cpu_pwm)

            if now - last_log >= 30.0:
                last_log = now
                g_str = (f"gpu={gpu_t:.0f}C mem={mem_t:.0f}C ctrl={control_t:.0f}C"
                         if gpu_t is not None else "gpu=ERR")
                c_str = f"cpu={cpu_t:.0f}C" if cpu_t is not None else "cpu=ERR"
                print(f"{g_str} -> z10={gpu_pwm} (~{gpu_duty_pct(gpu_pwm)}% b{gpu_idx}) | "
                      f"{c_str} -> z11={cpu_pwm} (~{cpu_duty_pct(cpu_pwm)}% b{cpu_idx})",
                      flush=True)

            time.sleep(PERIOD_S)
    finally:
        # Оставляем обе зоны на безопасных оборотах (в Standard инвертированный GPU
        # уходит на максимум, в Full — останавливается).
        set_gpu_pwm(FAILSAFE_GPU_PWM)
        set_cpu_pwm(FAILSAFE_CPU_PWM)
        print(f"Exiting: GPU z10={FAILSAFE_GPU_PWM} (~{gpu_duty_pct(FAILSAFE_GPU_PWM)}%), "
              f"CPU z11={FAILSAFE_CPU_PWM} (~{cpu_duty_pct(FAILSAFE_CPU_PWM)}%)",
              flush=True)


if __name__ == "__main__":
    main()
