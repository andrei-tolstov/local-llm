#!/usr/bin/env python3
"""
Tesla V100 (пассивная) — автоматический контроллер охлаждения (v2).

Вентилятор запитан от мультиконтроллера Nuvoton NCT6779 (каналы pwm2 = CPU_FAN,
pwm3 = SFAN3). На Huananzhi X79 с серверным вентилятором логика ШИМ ИНВЕРТИРОВАНА:
255 = минимальные обороты (тишина), 0 = 100 % (максимальный обдув).

Что изменено относительно v1 (замеры scripts/gpu_fan_sweep.py, 18.11.2026):

  1. Кривая построена от РЕАЛЬНОГО порога троттлинга. У этой карты
     `SW Thermal Slowdown` включается при 82 °C (не 87, как в `GPU Slowdown Temp`):
     частоты падают со 1380 до 700-900 МГц, потребление с 170 до 75 Вт.
     v1 уходила в 80 % оборотов уже на 58 °C и в 100 % на 74 °C — это и был шум.
     Замеренная характеристика (стационар, нагрузка 125-142 Вт):

       duty 64.7 % (PWM 90)  -> 53.3 °C
       duty 52.9 % (PWM 120) -> 58.9 °C
       duty 45.1 % (PWM 140) -> 60.0 °C
       duty 43.1 % (PWM 145) -> 63.0 °C
       duty 35.3 % (PWM 165) -> 67.9 °C
       duty 33.3 % (PWM 170) -> 73.9 °C
       duty 25.5 % (PWM 190) -> 82.1 °C + троттлинг  <- нижняя граница

     Рабочий потолок выбран 74-78 °C: запас 4-8 °C до троттлинга и 7-11 °C до
     предела HBM2 (85 °C).

  2. Гистерезис + выдержка: убраны «качели» 90 <-> 125 на простое (слышимое
     раскручивание/останов каждые полминуты).

  3. Учитывается HBM2 (temperature.memory) с поправкой, а не только ядро.

  4. Потолок по CPU: этот же вентилятор охлаждает зону CPU/VRM.

  5. Отказоустойчивость: 3 неудачных опроса nvidia-smi -> 100 % обдува; старт демона
     сразу со ступени, соответствующей текущей температуре (демон может подняться на
     уже горячей карте); при остановке сервиса ШИМ фиксируется на FAILSAFE_PWM (53 %),
     в BIOS SmartFan каналы возвращаются только явно (--release).

Запуск: systemd (v100-fancontrol.service). Отладка: --once, --set <PWM>.
"""

import argparse
import glob
import os
import signal
import subprocess
import sys
import time

# --------------------------------------------------------------- кривая
# (upper_temp, pwm): первая же строка, где control_temp <= upper_temp, задаёт режим.
# Инвертированный ШИМ: чем больше число, тем ТИШЕ. duty % = (255 - pwm) / 255.
CURVE = [
    (48.0, 205),   # ~19.6 % — простой, почти бесшумно
    (54.0, 185),   # ~27.5 %
    (60.0, 170),   # ~33.3 %
    (65.0, 150),   # ~41.2 % — типовой инференс
    (70.0, 140),   # ~45.1 % — плотный инференс / prefill
    (74.0, 120),   # ~52.9 % — тяжёлая нагрузка
    (78.0, 100),   # ~60.8 % — предельная нагрузка
    (80.0, 60),    # ~76.5 % — близко к троттлингу
    (1e9, 0),      # 100 % — аварийный режим
]

HYSTERESIS_C = 4.0        # на сколько должно остыть, чтобы понизить ступень
DOWN_DELAY_S = 150        # как минимум столько держим повышенную ступень
UP_DEBOUNCE_S = 12        # столько держим рост температуры, прежде чем разгонять
FAILSAFE_PWM = 120        # ~53 % обдува — положение, в котором карта остаётся без демона
HBM_OFFSET_C = -3.0       # HBM2 холоднее ядра на ~3 °C, а предел у неё выше (85 vs 82)
CPU_SAFE_C = 70.0         # выше этой температуры CPU не даём вентилятору опуститься ниже
CPU_SAFE_PWM = 150        # инвертированный ШИМ, не ниже которого уходим при перегреве CPU
MAX_PWM = 255
MIN_PWM = 0               # 100 % обдува в инвертированной логике
READ_FAIL_LIMIT = 3
PERIOD_S = 3.0

GPU_INDEX = os.environ.get("GPU_INDEX", "0")
CHANNELS = [2, 3]


# --------------------------------------------------------------- hwmon

def find_hwmon(pattern="nct"):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(d, "name")) as f:
                if pattern in f.read().lower():
                    return d
        except Exception:
            pass
    return None


NCT_DIR = find_hwmon()
if not NCT_DIR:
    sys.stderr.write("ERROR: NCT6779 hwmon device not found!\n")
    sys.exit(1)


def set_pwm(value):
    value = max(MIN_PWM, min(MAX_PWM, int(value)))
    for ch in CHANNELS:
        try:
            with open(os.path.join(NCT_DIR, f"pwm{ch}_enable"), "w") as f:
                f.write("1")            # 1 = manual mode
            with open(os.path.join(NCT_DIR, f"pwm{ch}"), "w") as f:
                f.write(str(value))
        except Exception as e:
            sys.stderr.write(f"Failed to set pwm{ch}: {e}\n")


def revert_to_bios():
    """Возврат каналов в автоматический режим BIOS (SmartFan)."""
    for ch in CHANNELS:
        try:
            with open(os.path.join(NCT_DIR, f"pwm{ch}_enable"), "w") as f:
                f.write("5")
        except Exception:
            set_pwm(MIN_PWM)


def duty_pct(pwm):
    return int((MAX_PWM - pwm) * 100 / MAX_PWM)


# --------------------------------------------------------------- телеметрия

def read_temps():
    """(gpu_temp, mem_temp) либо None, если nvidia-smi недоступен."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "-i", GPU_INDEX,
             "--query-gpu=temperature.gpu,temperature.memory",
             "--format=csv,noheader,nounits"], timeout=3).decode().strip()
        parts = [p.strip() for p in out.splitlines()[0].split(",")]
        gpu = float(parts[0])
        mem = float(parts[1]) if parts[1] not in ("N/A", "") else gpu
        return gpu, mem
    except Exception:
        return None


def read_cpu_package():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if "coretemp" not in open(os.path.join(d, "name")).read():
                continue
        except Exception:
            continue
        best = None
        for lbl in glob.glob(os.path.join(d, "temp*_label")):
            try:
                if "package" not in open(lbl).read().lower():
                    continue
                v = float(open(lbl.replace("_label", "_input")).read()) / 1000.0
                best = v if best is None else max(best, v)
            except Exception:
                pass
        if best is not None:
            return best
    return None


# --------------------------------------------------------------- регулятор

def band_index(temp):
    for i, (upper, _) in enumerate(CURVE):
        if temp <= upper:
            return i
    return len(CURVE) - 1


def step(current_idx, control_t, now, held_since, rising_since):
    """Повышение оборотов — почти сразу, понижение — только после гистерезиса
    и выдержки. Возвращает (idx, held_since, rising_since)."""
    want = band_index(control_t)
    cur_pwm, want_pwm = CURVE[current_idx][1], CURVE[want][1]

    if want_pwm < cur_pwm:                       # нужно больше обдува
        if rising_since is None:
            rising_since = now
        if now - rising_since >= UP_DEBOUNCE_S:
            return want, now, None
        return current_idx, held_since, rising_since

    if want_pwm > cur_pwm:                       # можно стать тише
        rising_since = None
        if control_t > CURVE[current_idx][0] - HYSTERESIS_C:
            return current_idx, held_since, rising_since
        if now - held_since < DOWN_DELAY_S:
            return current_idx, held_since, rising_since
        return want, now, rising_since

    return current_idx, held_since, rising_since


# --------------------------------------------------------------- запуск

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один замер и расчёт ступени")
    ap.add_argument("--set", type=int, metavar="PWM", help="рукописный инвертированный ШИМ")
    ap.add_argument("--release", action="store_true",
                    help="явно вернуть каналы в режим BIOS SmartFan (enable=5)")
    args = ap.parse_args()

    if args.release:
        revert_to_bios()
        print("каналы переданы обратно в BIOS SmartFan (enable=5)")
        return

    if args.set is not None:
        set_pwm(args.set)
        print(f"manual pwm={args.set} (~{duty_pct(args.set)} % оборотов)")
        return

    if args.once:
        temps = read_temps()
        if temps is None:
            print("nvidia-smi недоступен")
            return
        control_t = max(temps[0], temps[1] + HBM_OFFSET_C)
        idx = band_index(control_t)
        print(f"gpu={temps[0]}C mem={temps[1]}C cpu={read_cpu_package()}C control={control_t}C "
              f"-> pwm={CURVE[idx][1]} (~{duty_pct(CURVE[idx][1])} %)")
        return

    print(f"Starting inverted fan controller on {NCT_DIR} (channels {CHANNELS})", flush=True)
    # SIGTERM/SIGINT должны выполняться через finally, а не «молча» убивать процесс,
    # иначе канал останется в ручном режиме на тестовой ступени.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Стартовая ступень — по фактической температуре, а не «самая тихая»: демон может
    # стартовать на уже горячей карте (рестарт сервиса, прогрев после теста).
    temps = read_temps()
    if temps is None:
        idx = len(CURVE) - 1                     # не можем измерить -> максимальный обдув
    else:
        idx = band_index(max(temps[0], temps[1] + HBM_OFFSET_C))
    set_pwm(CURVE[idx][1])
    held_since = time.time()
    rising_since = None
    fails = 0
    last_log = 0.0

    try:
        while True:
            temps = read_temps()
            if temps is None:
                fails += 1
                if fails >= READ_FAIL_LIMIT:
                    set_pwm(MIN_PWM)             # 100 % обдува при отказе опроса
                    print("WARNING: nvidia-smi недоступен, включён 100 % обдув", flush=True)
                time.sleep(PERIOD_S)
                continue
            fails = 0

            gpu_t, mem_t = temps
            control_t = max(gpu_t, mem_t + HBM_OFFSET_C)
            idx, held_since, rising_since = step(idx, control_t, time.time(),
                                                 held_since, rising_since)
            pwm = CURVE[idx][1]

            cpu_t = read_cpu_package()
            if cpu_t is not None and cpu_t >= CPU_SAFE_C and pwm > CPU_SAFE_PWM:
                pwm = CPU_SAFE_PWM
            set_pwm(pwm)

            if time.time() - last_log > 30:
                last_log = time.time()
                print(f"gpu={gpu_t:.0f}C mem={mem_t:.0f}C cpu={cpu_t}C "
                      f"control={control_t:.0f}C -> pwm={pwm} (~{duty_pct(pwm)} %) band={idx}",
                      flush=True)
            time.sleep(PERIOD_S)
    finally:
        # Передавать каналы обратно BIOS SmartFan нельзя: у платы нет датчика GPU,
        # и вентилятор уйдёт в минимум по холодному CPU. Оставляем известную безопасную
        # ступень (~53 %). В BIOS осознанно можно вернуться флагом --release.
        set_pwm(FAILSAFE_PWM)
        print(f"Exiting: ШИМ зафиксирован на {FAILSAFE_PWM} (~{duty_pct(FAILSAFE_PWM)} %)",
              flush=True)


if __name__ == "__main__":
    main()
