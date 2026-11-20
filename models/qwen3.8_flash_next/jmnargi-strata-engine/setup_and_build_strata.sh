#!/usr/bin/env bash
# ==============================================================================
# Проверка хоста и сборка Docker-образа Strata (sm_70 + AVX-пол) для
# Qwen3.8-Flash-Next на Tesla V100 32GB. Скачивание GGUF отделено намеренно:
# это ~70-85 GB, оно запускается отдельно через download_model_strata.sh.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || { echo "Нет $ENV_FILE. Скопируйте .env.strata-v100.example в .env"; exit 1; }
set -a; . "$ENV_FILE"; set +a

echo "======================================================================"
echo " 0. Предполётная проверка хоста"
echo "======================================================================"

if grep -q avx2 /proc/cpuinfo; then
    ISA_FLOOR=""
    echo "[OK] CPU с AVX2 — готовый движок форка (strata-linux-x64-sm70-cuda12) тоже подойдёт."
else
    ISA_FLOOR="avx"
    echo "[!] CPU без AVX2: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)"
    echo "    Готовая сборка Strata собрана под AVX2 и на этом CPU упадёт с SIGILL (exit 132)."
    echo "    Образ собирается с -DSTRATA_ISA_FLOOR=${ISA_FLOOR}."
fi

echo "[*] GPU:"
nvidia-smi --query-gpu=index,name,memory.total,compute_cap --format=csv,noheader
echo "[*] Драйвер: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
echo "    Сборка на CUDA 12.8: CUDA 13 не компилирует sm_70 (Volta)."

echo "[*] RAM:"
free -g | awk 'NR==1||NR==2'
echo "    Арены пакетов Strata: IQ2_XS 35.5 GiB, IQ3_S 50.3 GiB (+ ~10-24 GiB на движок,"
echo "    KV и PLE). ninfer-профиль держит 58.6 GiB — перед запуском его нужно остановить."

echo "[*] Диск:"
df -h / | tail -1
echo "    Скачивание: IQ2_XS 68.0 GB, IQ3_S 83.6 GB, Q2_0 66.4 GB (+ распаковка пакета)."

echo "======================================================================"
echo " 1. Сборка образа strata-v100:cuda12.8-sm70-avx"
echo "======================================================================"
docker compose --env-file "$ENV_FILE" -f docker-compose.strata-v100.yml build

echo "======================================================================"
echo " 2. GGUF-файлы модели"
echo "======================================================================"
MODELS_DIR="${STRATA_MODELS_DIR:-/opt/models/flash-next-strata}"
DATA_DIR="${STRATA_DATA_DIR:-${MODELS_DIR}/data}"
mkdir -p "$MODELS_DIR" "$DATA_DIR"
if ls "${MODELS_DIR}"/Qwen3.8-Flash-Next-GSQ-RCO-*.gguf >/dev/null 2>&1; then
    echo "[OK] GGUF на месте: $(ls "${MODELS_DIR}"/*.gguf | wc -l) файл(ов)"
else
    echo "GGUF в $MODELS_DIR нет. Скачайте отдельно (~70-85 GB):"
    echo "    ./download_model_strata.sh $MODELS_DIR ${MODEL:-IQ2_XS}"
fi

echo "======================================================================"
echo " Готово. Запуск: ./switch-to-strata.sh"
echo "======================================================================"