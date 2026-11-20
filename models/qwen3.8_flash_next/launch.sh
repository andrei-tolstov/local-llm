#!/bin/sh
# ==============================================================================
# Точка входа контейнера Qwen3.8-Flash-Next (NInfer V100): сервер + мониторинг.
#
# Сервер остаётся PID 1 — сигналы `docker stop` уходят ему, а не обёртке.
# Монитор (tools/monitor/monitor.py из движка) стартует фоном и читает
# stats-listener сервера; он переживает падение и перезапуск сервера.
#
# FLASH_NEXT_DRY_RUN=1 — напечатать командную строку и выйти (прогон аргументов
# без загрузки модели: docker run --rm -e FLASH_NEXT_DRY_RUN=1 ...).
# ==============================================================================
set -eu

SERVER_BIN="${SERVER_BIN:-/usr/local/bin/ninfer-serve}"
MONITOR_BIN="${MONITOR_BIN:-/opt/ninfer-monitor/monitor.py}"

MODEL_FILE="${FLASH_NEXT_MODEL_FILE:-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-ninfer-v3.ninfer}"
NGRAM_FILE="${FLASH_NEXT_NGRAM_FILE:-Qwen3.8-Flash-Next-ngram-table-IQ4_NL-ninfer-v3.ninfer}"
MODEL_PATH="/workspace/models/$MODEL_FILE"
NGRAM_PATH="/workspace/models/$NGRAM_FILE"

LOG_DIR="${FLASH_NEXT_LOG_DIR:-/workspace/logs}"
REQUEST_LOG="${FLASH_NEXT_REQUEST_LOG:-}"
STATS_PORT="${FLASH_NEXT_STATS_PORT:-}"
MONITOR_PORT="${MONITOR_PORT:-8090}"

if [ ! -f "$MODEL_PATH" ]; then
    echo "ОШИБКА: Файл модели $MODEL_PATH не найден!"
    exit 1
fi

ARGS="$MODEL_PATH --host 0.0.0.0 --port 8080"

if [ -f "$NGRAM_PATH" ]; then
    ARGS="$ARGS --ngram-table $NGRAM_PATH"
    if [ -n "${FLASH_NEXT_NGRAM_RESIDENCY:-}" ]; then
        ARGS="$ARGS --ngram-residency $FLASH_NEXT_NGRAM_RESIDENCY"
    fi
    if [ -n "${FLASH_NEXT_NGRAM_RAM_MIB:-}" ]; then
        ARGS="$ARGS --ngram-ram-mib $FLASH_NEXT_NGRAM_RAM_MIB"
    fi
    if [ "${FLASH_NEXT_NGRAM_LOCK:-}" = "true" ]; then
        ARGS="$ARGS --ngram-lock"
    fi
else
    echo "ПРЕДУПРЕЖДЕНИЕ: Файл n-gram таблицы $NGRAM_PATH не найден. Попытка запуска без --ngram-table..."
fi

if [ -n "${FLASH_NEXT_ALIAS:-}" ]; then
    ARGS="$ARGS --model-id $FLASH_NEXT_ALIAS"
fi
if [ -n "${FLASH_NEXT_API_KEY:-}" ]; then
    ARGS="$ARGS --api-key $FLASH_NEXT_API_KEY"
fi

ARGS="$ARGS --max-context ${FLASH_NEXT_MAX_CONTEXT:-200000}"
ARGS="$ARGS --kv-capacity ${FLASH_NEXT_KV_CAPACITY:-auto}"
ARGS="$ARGS --kv-dtype ${FLASH_NEXT_KV_DTYPE:-rk4v4}"
ARGS="$ARGS --expert-residency ${FLASH_NEXT_EXPERT_RESIDENCY:-disk}"
if [ -n "${FLASH_NEXT_EXPERT_CACHE_MIB:-}" ]; then
    ARGS="$ARGS --expert-cache-mib $FLASH_NEXT_EXPERT_CACHE_MIB"
fi
if [ -n "${FLASH_NEXT_HOST_CACHE_MIB:-}" ]; then
    ARGS="$ARGS --host-cache-mib $FLASH_NEXT_HOST_CACHE_MIB"
fi
ARGS="$ARGS --prefill-chunk ${FLASH_NEXT_PREFILL_CHUNK:-2048}"
ARGS="$ARGS --max-concurrency ${FLASH_NEXT_MAX_CONCURRENCY:-1}"
ARGS="$ARGS --pending-timeout-ms ${FLASH_NEXT_PENDING_TIMEOUT_MS:-1200000}"

if [ "${FLASH_NEXT_EXPERT_RESIDENCY:-disk}" = "host" ] && [ -n "${FLASH_NEXT_EXPERT_PREFILL_BUFFERS:-}" ]; then
    ARGS="$ARGS --expert-prefill-buffers $FLASH_NEXT_EXPERT_PREFILL_BUFFERS"
fi
if [ -n "${FLASH_NEXT_EXPERT_DMA_SHARE:-}" ]; then
    ARGS="$ARGS --expert-dma-share $FLASH_NEXT_EXPERT_DMA_SHARE"
fi
if [ "${FLASH_NEXT_SPEC_BACKEND:-mtp}" != "none" ]; then
    ARGS="$ARGS --spec ${FLASH_NEXT_SPEC_BACKEND:-mtp} --draft-tokens ${FLASH_NEXT_SPEC_DRAFT_TOKENS:-3}"
    ARGS="$ARGS --ngram-draft-tokens ${FLASH_NEXT_NGRAM_DRAFT_TOKENS:-0}"
fi
if [ "${FLASH_NEXT_PRESERVE_THINKING:-true}" = "true" ]; then
    ARGS="$ARGS --preserve-thinking"
fi
if [ "${FLASH_NEXT_ENABLE_VISION:-true}" = "true" ]; then
    ARGS="$ARGS --vision"
fi

# Отдельный listener статистики: /health, /stats, /v1/load, /metrics.
# Наружу не публикуется — читает его только монитор внутри контейнера.
# Релизные сборки движка (например v1.7.2) этих флагов не знают, поэтому
# флаги включаются только если бинарник их понимает.
HELP="$("$SERVER_BIN" --help 2>&1 || true)"
ACTIVE_STATS_PORT=""
if [ -n "$STATS_PORT" ] && printf '%s' "$HELP" | grep -q -- '--stats-port'; then
    ARGS="$ARGS --stats-port $STATS_PORT"
    ACTIVE_STATS_PORT="$STATS_PORT"
elif [ -n "$STATS_PORT" ]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: $SERVER_BIN не поддерживает --stats-port — монитор читает основной порт."
fi

# JSONL-журнал запросов: его хвост монитор показывает в панели запросов.
if [ -n "$REQUEST_LOG" ] && ! printf '%s' "$HELP" | grep -q -- '--request-log-jsonl'; then
    echo "ПРЕДУПРЕЖДЕНИЕ: $SERVER_BIN не поддерживает --request-log-jsonl — журнал отключён."
    REQUEST_LOG=""
fi
if [ -n "$REQUEST_LOG" ]; then
    mkdir -p "$(dirname "$REQUEST_LOG")"
    ARGS="$ARGS --request-log-jsonl $REQUEST_LOG"
    if [ -n "${FLASH_NEXT_REQUEST_LOG_MIB:-}" ]; then
        ARGS="$ARGS --request-log-max-mib $FLASH_NEXT_REQUEST_LOG_MIB"
    fi
    if [ -n "${FLASH_NEXT_REQUEST_LOG_KEEP:-}" ]; then
        ARGS="$ARGS --request-log-keep $FLASH_NEXT_REQUEST_LOG_KEEP"
    fi
fi

if [ -n "${FLASH_NEXT_EXTRA_ARGS:-}" ]; then
    ARGS="$ARGS $FLASH_NEXT_EXTRA_ARGS"
fi

echo "Запуск: $SERVER_BIN $ARGS"

if [ -n "${FLASH_NEXT_DRY_RUN:-}" ]; then
    exit 0
fi

# Монитор: дашборд на MONITOR_PORT внутри контейнера, источник — stats-listener.
if [ "${MONITOR_ENABLED:-true}" = "true" ] && [ -f "$MONITOR_BIN" ]; then
    mkdir -p "$LOG_DIR"
    if [ -n "${FLASH_NEXT_API_KEY:-}" ]; then
        NINFER_API_KEY="$FLASH_NEXT_API_KEY"
        export NINFER_API_KEY
    fi
    MON_ARGS="--stats-url http://127.0.0.1:${ACTIVE_STATS_PORT:-8080} --bind ${MONITOR_BIND:-0.0.0.0} --port $MONITOR_PORT"
    MON_ARGS="$MON_ARGS --interval ${MONITOR_INTERVAL:-5} --samples ${MONITOR_SAMPLES:-4320}"
    if [ -n "$REQUEST_LOG" ]; then
        MON_ARGS="$MON_ARGS --jsonl $REQUEST_LOG"
    fi
    # Надзиратель: монитор переживает собственную смерть, сервера — тем более.
    (
        while :; do
            python3 "$MONITOR_BIN" $MON_ARGS || true
            echo "Монитор завершился, рестарт через 5 с..."
            sleep 5
        done
    ) &
    echo "Монитор: python3 $MONITOR_BIN $MON_ARGS"
else
    echo "ПРЕДУПРЕЖДЕНИЕ: монитор ($MONITOR_BIN) недоступен — панель не поднимается."
fi

exec "$SERVER_BIN" $ARGS
