#!/usr/bin/env bash
# ==============================================================================
# Скрипт переключения на Qwen3.8-Flash-Next MoE (NInfer V100) на Tesla V100
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${SCRIPT_DIR}}"

echo "======================================================================"
echo " Переключение на Qwen3.8-Flash-Next MoE (JimmyMax/ninfer-v100) на V100..."
echo "======================================================================"

echo "[1/3] Остановка других LLM и Diffusion серверов на V100..."
docker stop \
    coder390-llama-server \
    efficientthink-k3-llama-server \
    fable-llama-server \
    qwen38-llama-server \
    bonsai2-llama-server \
    fluxed-up-sd-server \
    juggernaut-sd-server \
    qwen-image-sd-server \
    flash-next-llama-server 2>/dev/null || true
sleep 2

echo "[2/3] Запуск сервиса flash-next-llama-server (ninfer-serve)..."
cd "$TARGET_DIR"
COMPOSE_FILE="docker-compose.ninfer-v100.yml"
if [ ! -f "$TARGET_DIR/$COMPOSE_FILE" ]; then
    COMPOSE_FILE="docker-compose.yml"
fi

ENV_FILE=".env"
if [ ! -f "$TARGET_DIR/$ENV_FILE" ]; then
    ENV_FILE=".env.ninfer-v100.example"
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d

echo "[3/3] Ожидание инициализации модели на порту 8087..."
i=0
while [ $i -lt 120 ]; do
    if curl -s -f http://127.0.0.1:8087/health >/dev/null 2>&1 || curl -s -f http://127.0.0.1:8087/v1/models >/dev/null 2>&1; then
        echo ""
        echo "[OK] Qwen3.8-Flash-Next MoE (NInfer V100) успешно запущена и готова к работе!"
        echo "Прямой эндпоинт: http://192.168.50.38:8087/v1"
        echo "Через OmniRoute: http://192.168.50.38:20128/v1 (модель: qwen3.8-flash-next или flash-next)"
        docker ps --filter name=flash-next-llama-server
        exit 0
    fi
    echo -n "."
    sleep 3
    i=$((i + 1))
done

echo ""
echo "[!] Сервер еще загружается. Проверьте логи: docker logs -f flash-next-llama-server"
