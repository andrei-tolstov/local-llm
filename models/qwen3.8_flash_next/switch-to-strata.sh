#!/usr/bin/env bash
# ==============================================================================
# Переключение V100 на Qwen3.8-Flash-Next в движке Strata (jmnargi/Strata-V100).
# Раскладка как у switch-to-flash-next.sh: скрипт в корне модели, compose движка
# в jmnargi-strata-engine/ (первым аргументом можно задать каталог развёртывания).
# Останавливает остальные LLM и Diffusion серверы: Strata забирает RAM и VRAM целиком.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${SCRIPT_DIR}/jmnargi-strata-engine}"

echo "======================================================================"
echo " Переключение на Qwen3.8-Flash-Next MoE (Strata V100) на V100..."
echo "======================================================================"

cd "$TARGET_DIR"
COMPOSE_FILE="docker-compose.strata-v100.yml"
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "Не найден $TARGET_DIR/$COMPOSE_FILE"
    exit 1
fi

ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
    ENV_FILE=".env.strata-v100.example"
fi
set -a; . "$ENV_FILE"; set +a

PORT="${STRATA_PORT:-8089}"
CONTAINER="${CONTAINER_NAME:-flash-next-strata-llama-server}"

echo "[1/3] Остановка других LLM и Diffusion серверов на V100..."
docker stop \
    flash-next-llama-server \
    coder390-llama-server \
    efficientthink-k3-llama-server \
    fable-llama-server \
    qwen38-llama-server \
    bonsai2-llama-server \
    fluxed-up-sd-server \
    juggernaut-sd-server \
    qwen-image-sd-server 2>/dev/null || true
sleep 2

echo "[2/3] Запуск сервиса $CONTAINER (Strata, контекст ${CONTEXT:-131072})..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d

echo "[3/3] Ожидание инициализации модели на порту $PORT..."
# Первый старт включает подготовку пакета движка из GGUF: десятки минут.
i=0
while [ $i -lt 400 ]; do
    if curl -s -f "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 \
       || curl -s -f "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
        echo ""
        echo "[OK] Qwen3.8-Flash-Next (Strata V100) успешно запущена и готова к работе!"
        echo "Прямой эндпоинт:   http://192.168.50.38:$PORT/v1"
        echo "Через Llama-Swap:  http://192.168.50.38:8090/v1 (qwen3.8-flash-next-strata)"
        echo "Через OmniRoute:   http://192.168.50.38:20128/v1"
        docker ps --filter name="$CONTAINER"
        exit 0
    fi
    echo -n "."
    sleep 6
    i=$((i + 1))
done

echo ""
echo "[!] Сервер ещё загружается. Проверьте логи: docker logs -f $CONTAINER"
