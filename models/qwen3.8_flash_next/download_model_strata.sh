#!/usr/bin/env bash
# ==============================================================================
# Загрузка GGUF-пакета Qwen3.8-Flash-Next для движка Strata (jmnargi/Strata-V100).
# Репозиторий, ревизия, размеры и SHA-256 зафиксированы так же, как в setup.py
# форка (HF_REVISIONS / ISTA_FILES), чтобы копия совпадала байт в байт.
#   ./download_model_strata.sh [каталог] [IQ2_XS|IQ3_S|Q2_0] [vision]
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Каталог по умолчанию - gguf-подкаталог рядом с .ninfer-пакетами NInfer. Файлы .ninfer
# Strata не читает: это пакет движка NInfer (магия NINFER), а не GGUF.
TARGET_DIR="${1:-${STRATA_MODELS_DIR:-/opt/models/flash_next/gguf}}"
QUANT="${2:-IQ2_XS}"
WANT_VISION="${3:-no}"

HF_REPO="ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF"
HF_REV="ed59f92082b1e93c0e96d60a8b11aab089b52f09"
HF_BASE_URL="${HF_ENDPOINT:-https://huggingface.co}"

declare -A FILES=()
declare -A SIZES=()
declare -A HASHES=()

# shard 2 (n-gram таблица) одинаков для всех размеров пакета.
add() { FILES["${1}"]="${HF_BASE_URL}/${HF_REPO}/resolve/${HF_REV}/${1}"; SIZES["${1}"]="${2}"; HASHES["${1}"]="${3}"; }

case "$QUANT" in
  IQ2_XS)
    add "Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00001-of-00002.gguf" 39225954592 92cee27ae5bbadcd732416a0f7a7f0acc092399dbbe8f5a5efa707c2ec0a49d7
    add "Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00002-of-00002.gguf" 28800138432 316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113
    ;;
  IQ3_S)
    add "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf" 54817524224 4c1eb2ceb4915e1192f4f386021897bde56a97f40a0bb78bb86465e0f7d2aca3
    add "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf" 28800138432 316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113
    ;;
  Q2_0)
    add "Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00001-of-00002.gguf" 37623740192 69820c02ec7d0b45ef2ebb19d6620299db749fe2aded7f39f93c6b88b199b720
    add "Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-00002-of-00002.gguf" 28800138432 316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113
    ;;
  *)
    echo "Неизвестный квантование: $QUANT (ожидается IQ2_XS, IQ3_S или Q2_0)"; exit 1 ;;
esac

if [ "$WANT_VISION" = "vision" ] || [ "$WANT_VISION" = "yes" ]; then
    add "mmproj-Qwen3.8-Flash-Next-BF16.gguf" 907543008 b1a82259702816a5330d7bd7607cd9676b11780e79ff7348c21103ff3ce49bd0
fi

TOTAL=0
for f in "${!SIZES[@]}"; do TOTAL=$((TOTAL + SIZES[$f])); done
FREE=$(df -B1 --output=avail "$TARGET_DIR" | tail -1 | tr -d ' ')

echo "======================================================================"
echo " Пакет Strata: $QUANT, файлов: ${#FILES[@]}, всего $((TOTAL / 1024 / 1024 / 1024)) GiB"
echo " Каталог: $TARGET_DIR"
echo " Свободно на разделе: $((FREE / 1024 / 1024 / 1024)) GiB"
echo "======================================================================"
# GGUF распаковываются в пакет Strata (pack/experts.bin), поэтому нужно место и
# под GGUF, и под пакет. ориентир форка: +~20-40 GiB на подготовленный пакет.
NEED=$((TOTAL + 30 * 1024 * 1024 * 1024))
if [ "$FREE" -lt "$NEED" ]; then
    echo "ВНИМАНИЕ: свободно $((FREE / 1024 / 1024 / 1024)) GiB, а нужно"
    echo "$((NEED / 1024 / 1024 / 1024)) GiB (GGUF + ~30 GiB на распаковку пакета)."
    echo "Распаковка остановится в середине и оставит незавершённый пакет."
fi

mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"

download() {
    local name="${1}" url="${2}"
    echo "[*] $name"
    if command -v aria2c >/dev/null 2>&1; then
        aria2c --allow-overwrite=false --auto-file-renaming=false --continue=true \
            --max-connection-per-server=16 --split=16 --max-concurrent-downloads=1 \
            --file-allocation=none --summary-interval=5 "$url" --dir . -o "$name"
    elif command -v hf >/dev/null 2>&1; then
        hf download "$HF_REPO" "$name" --revision "$HF_REV" --local-dir .
    else
        curl -L -C - --fail --retry 5 --retry-delay 5 -o "$name" "$url"
    fi
}

for f in "${!FILES[@]}"; do
    if [ -f "$f" ] && [ "$(stat -c %s "$f")" = "${SIZES[$f]}" ]; then
        echo "[OK] Уже на месте: $f"
        continue
    fi
    n=0
    while [ $n -lt 3 ]; do
        n=$((n + 1))
        if download "$f" "${FILES[$f]}"; then break; fi
        echo "[!] Прервано ($n/3), продолжение через 5 c..."; sleep 5
    done
done

echo "======================================================================"
echo " Проверка целостности (размер + SHA-256)..."
echo "======================================================================"
fail=0
for f in "${!FILES[@]}"; do
    size=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if [ "$size" != "${SIZES[$f]}" ]; then
        echo "ОШИБКА: $f — размер $size, ожидалось ${SIZES[$f]}"; fail=1; continue
    fi
    echo "[OK] размер $f = $size"
    if ! echo "${HASHES[$f]}  $f" | sha256sum -c -; then
        echo "ОШИБКА: SHA-256 не совпал у $f"; fail=1
    fi
done
[ "$fail" -eq 0 ] || { echo "Пакет повреждён — перезапустите скрипт."; exit 1; }

echo "======================================================================"
echo " GGUF готовы. Сборка образа: ./setup_and_build_strata.sh"
echo "======================================================================"