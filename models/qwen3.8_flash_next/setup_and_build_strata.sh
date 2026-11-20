#!/usr/bin/env bash
# ==============================================================================
# Предполётная проверка хоста и сборка Docker-образа Strata (sm_70 + AVX-пол)
# для Qwen3.8-Flash-Next на Tesla V100 32GB. Раскладка как у остальных моделей:
# скрипт в корне модели, compose и Dockerfile движка - в jmnargi-strata-engine/.
# Скачивание GGUF намеренно отделено: это ~76 GB, он запускает его отдельно
# через ./download_model_strata.sh.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="${1:-${SCRIPT_DIR}/jmnargi-strata-engine}"
[ -d "$ENGINE_DIR" ] || { echo "Каталог движка не найден: $ENGINE_DIR"; exit 1; }
cd "$ENGINE_DIR"

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || ENV_FILE=".env.strata-v100.example"
set -a; . "$ENV_FILE"; set +a

echo "======================================================================"
echo " 0. Предполётная проверка хоста"
echo "======================================================================"

if grep -q avx2 /proc/cpuinfo; then
    echo "[OK] CPU с AVX2 - готовый движок форка (strata-linux-x64-sm70-cuda12) тоже подойдёт."
else
    echo "[!] CPU без AVX2: $(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs)"
    echo "    Готовая сборка Strata собрана под AVX2 и на этом CPU падает с SIGILL (exit 132)."
    echo "    Образ собирается с -DSTRATA_ISA_FLOOR=avx (по замерам форка декодирование"
    echo "    проседает примерно с 26 до 11-17 ток/с - это цена такого CPU)."
fi

echo "[*] GPU:"
nvidia-smi --query-gpu=index,name,memory.total,compute_cap --format=csv,noheader
echo "[*] Драйвер: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
echo "    Сборка на CUDA 12.8: CUDA 13 не компилирует sm_70 (Volta)."

echo "[*] RAM:"
free -g | awk "NR==1||NR==2"
echo "    Арены экспертов: IQ2_XS 35.5 GiB, IQ3_S 50.3 GiB. ninfer-профиль держит"
echo "    58.6 GiB - движки живут по одному, перед запуском его нужно остановить."

echo "[*] Диск:"
df -h / | tail -1
echo "    Стартуем с пакетом IQ2_XS: нужно ~76 GB (GGUF 68.0 + ~8 GB на пакет и MTP)."
echo "    IQ3_S: 83.6 + 8 = ~92 GB, на этом разделе не помещается."

echo "[*] Уже скачанные файлы в $(dirname "${STRATA_MODELS_DIR:-/opt/models/flash_next}"):"
echo "    Файлы .ninfer - это пакеты движка NInfer (магия NINFER), а не GGUF:"
echo "    Strata их прочитать не может, GGUF нужно качать отдельно."

echo "======================================================================"
echo " 1. Контекст по рекомендации самого движка"
echo "======================================================================"
echo "    setup.py считает рекомендацию так: rec_ctx по VRAM (32 GB карты -> 131072),";
echo "    и минимум из этого с правилом RAM (арена + контекст * 13 * 1056 байт + 24 GB)."
echo "    Для IQ3_S на 62.7 GB RAM правило RAM тоже даёт 131072 (на 262144 нужно 77.9 GB).";
echo "    ИТОГ: CONTEXT=${CONTEXT:-131072}. KV int8 занимает 138 MiB VRAM, стриминг KV"
echo "    ~1.8 GiB RAM; в VRAM на кэш экспертов остаётся ~25.6 GB."

echo "======================================================================"
echo " 2. Сборка образа strata-v100:cuda12.8-sm70-avx"
echo "======================================================================"
docker compose --env-file "$ENV_FILE" -f docker-compose.strata-v100.yml build

echo "======================================================================"
echo " 3. GGUF-файлы модели"
echo "======================================================================"
MODELS_DIR="${STRATA_MODELS_DIR:-/opt/models/flash_next/gguf}"
DATA_DIR="${STRATA_DATA_DIR:-/opt/models/flash_next/strata}"
mkdir -p "$MODELS_DIR" "$DATA_DIR"
if ls "${MODELS_DIR}"/Qwen3.8-Flash-Next-GSQ-RCO-*.gguf >/dev/null 2>&1; then
    echo "[OK] GGUF на месте: $(ls "${MODELS_DIR}"/*.gguf | wc -l) файл(ов) в $MODELS_DIR"
else
    echo "GGUF в $MODELS_DIR нет. Скачать (~76 GB для IQ2_XS):"
    echo "    ./download_model_strata.sh $MODELS_DIR ${MODEL:-IQ2_XS}"
fi

echo "======================================================================"
echo " Готово. Запуск: ./switch-to-strata.sh"
echo "======================================================================"
