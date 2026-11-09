#!/usr/bin/env bash
# ==============================================================================
# Скрипт загрузки Qwen3.8-Flash-Next MoE (IQ3_S) и n-gram таблицы для Tesla V100
# Движок: https://github.com/JimmyMax/ninfer-v100
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${FLASH_NEXT_MODELS_DIR:-/opt/models/flash_next}}"
MODE="${2:-all}" # all, model-only, table-only

mkdir -p "$TARGET_DIR"
cd "$TARGET_DIR"

HF_MODEL_REPO="WaveCut/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-NInfer-v3"
MODEL_FILENAME="Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-ninfer-v3.ninfer"
HF_MODEL_URL="https://huggingface.co/${HF_MODEL_REPO}/resolve/main/${MODEL_FILENAME}"
MODEL_FALLBACK_SIZE="58242390528"

HF_TABLE_REPO="WaveCut/Qwen3.8-Flash-Next-ngram-table-NInfer-v3"
TABLE_FILENAME="Qwen3.8-Flash-Next-ngram-table-IQ4_NL-ninfer-v3.ninfer"
HF_TABLE_URL="https://huggingface.co/${HF_TABLE_REPO}/resolve/main/${TABLE_FILENAME}"
TABLE_FALLBACK_SIZE="28905035512"

echo "======================================================================"
echo " Загрузка Qwen3.8-Flash-Next MoE (IQ3_S NInfer V3)"
echo " Каталог назначения : $TARGET_DIR"
echo " Режим              : $MODE"
echo "======================================================================"

get_remote_size() {
    local url="$1"
    local fallback="$2"
    local remote_sz
    remote_sz=$(curl -sIL "$url" | grep -i '^content-length:' | tail -n 1 | awk '{print $2}' | tr -d '\r')
    if [ -n "$remote_sz" ] && [ "$remote_sz" -gt 1000000000 ] 2>/dev/null; then
        echo "$remote_sz"
    else
        echo "$fallback"
    fi
}

check_file_validity() {
    local file="$1"
    local expected_size="$2"
    if [ ! -f "$file" ]; then
        return 1
    fi
    local size
    size="$(stat -c %s "$file" 2>/dev/null || stat -f %z "$file" 2>/dev/null || echo 0)"
    if [ "$size" = "$expected_size" ]; then
        return 0
    else
        return 1
    fi
}

download_artifact() {
    local name="$1"
    local filename="$2"
    local repo="$3"
    local url="$4"
    local fallback_size="$5"

    echo "----------------------------------------------------------------------"
    echo " [*] Артефакт: $name ($filename)"
    echo "----------------------------------------------------------------------"

    local expected_size
    expected_size=$(get_remote_size "$url" "$fallback_size")
    local size_gib
    size_gib=$(awk "BEGIN {printf \"%.2f\", $expected_size/1024/1024/1024}")
    echo "[*] Ожидаемый размер: $size_gib GiB ($expected_size байт)"

    if check_file_validity "$filename" "$expected_size"; then
        echo "[OK] Файл $filename уже существует и полностью совпадает по байтам."
        return 0
    fi

    echo "[*] Файл отсутствует или не докачан. Запуск загрузки..."

    # 1. Загрузка через aria2c в 16 потоков
    if command -v aria2c >/dev/null 2>&1; then
        echo "[1/3] Попытка через aria2c (16 параллельных соединений)..."
        aria2c \
            --file-allocation=none \
            --continue=true \
            --max-connection-per-server=16 \
            --split=16 \
            --min-split-size=10M \
            --summary-interval=15 \
            --out="$filename" \
            "$url" || true
    fi

    # 2. Fallback через huggingface-cli / hf
    if ! check_file_validity "$filename" "$expected_size"; then
        if command -v hf >/dev/null 2>&1 || command -v huggingface-cli >/dev/null 2>&1; then
            echo "[2/3] Загрузка через huggingface-cli..."
            local hf_cmd
            hf_cmd="$(command -v hf 2>/dev/null || command -v huggingface-cli 2>/dev/null)"
            $hf_cmd download "$repo" "$filename" --local-dir ./tmp_dl
            if [ -f "./tmp_dl/$filename" ]; then
                mv "./tmp_dl/$filename" "./$filename"
                rm -rf ./tmp_dl
            fi
        fi
    fi

    # 3. Fallback через curl с флагом докачки -C -
    if ! check_file_validity "$filename" "$expected_size"; then
        echo "[3/3] Докачка через curl (-C -)..."
        curl -L -C - --progress-bar -o "$filename" "$url"
    fi

    # Финальная проверка размера
    local current_size
    current_size="$(stat -c %s "$filename" 2>/dev/null || stat -f %z "$filename" 2>/dev/null || echo 0)"
    if [ "$current_size" != "$expected_size" ]; then
        echo "ОШИБКА: Размер файла ($current_size байт) не равен ожидаемому ($expected_size байт)!"
        exit 1
    fi
    echo "[OK] Размер $filename полностью подтвержден: $current_size байт."
}

if [ "$MODE" = "all" ] || [ "$MODE" = "model-only" ]; then
    download_artifact "Модель Qwen3.8-Flash-Next IQ3_S" "$MODEL_FILENAME" "$HF_MODEL_REPO" "$HF_MODEL_URL" "$MODEL_FALLBACK_SIZE"
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "table-only" ]; then
    download_artifact "PLE N-Gram Table" "$TABLE_FILENAME" "$HF_TABLE_REPO" "$HF_TABLE_URL" "$TABLE_FALLBACK_SIZE"
fi

echo "======================================================================"
echo " Загрузка всех необходимых компонентов успешно завершена!"
echo "======================================================================"
