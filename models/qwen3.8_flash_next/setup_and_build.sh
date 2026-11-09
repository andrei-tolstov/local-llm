#!/usr/bin/env bash
# ==============================================================================
# Скрипт подготовки и сборки NInfer V100 для Qwen3.8-Flash-Next MoE (IQ3_S)
# Платформа: NVIDIA Tesla V100 32GB (CUDA 12.8, sm_70)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODELS_DIR="${FLASH_NEXT_MODELS_DIR:-/opt/models/flash_next}"
mkdir -p "$MODELS_DIR"

echo "======================================================================"
echo " 1. Загрузка весов Qwen3.8-Flash-Next MoE и companion n-gram таблицы..."
echo "======================================================================"
bash ./download_model.sh "$MODELS_DIR" all

MODEL_FILE="$MODELS_DIR/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-ninfer-v3.ninfer"
TABLE_FILE="$MODELS_DIR/Qwen3.8-Flash-Next-ngram-table-IQ4_NL-ninfer-v3.ninfer"

if [ ! -f "$MODEL_FILE" ]; then
    echo "ОШИБКА: Файл модели $MODEL_FILE не найден после скачивания!"
    exit 1
fi

if [ ! -f "$TABLE_FILE" ]; then
    echo "ОШИБКА: Файл таблицы $TABLE_FILE не найден после скачивания!"
    exit 1
fi

echo "======================================================================"
echo " 2. Проверка / сборка Docker-образа ninfer-v100:1.7.2..."
echo "======================================================================"
if docker image inspect ninfer-v100:1.7.2 >/dev/null 2>&1; then
    echo "[OK] Базовый образ ninfer-v100:1.7.2 уже присутствует в локальном Docker registry."
else
    echo "[*] Сборка специализированного образа ninfer-v100:1.7.2..."
    docker build -f Dockerfile.ninfer-v100 -t ninfer-v100:1.7.2 .
fi

echo "======================================================================"
echo " Готово! Среда и артефакты Qwen3.8-Flash-Next MoE готовы."
echo " Для запуска используйте: ./switch-to-flash-next.sh или docker compose up -d"
echo "======================================================================"
