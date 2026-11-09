# Qwen3.8-Flash-Next MoE (GSQ-RCO IQ3_S) на Tesla V100 32GB

## Обзор архитектуры

- **Модель**: `Qwen3.8-Flash-Next` (архитектура `Qwen4ExpForConditionalGeneration`).
- **Параметры**: 125B параметров всего, 48 слоев, 512 маршрутизируемых экспертов.
- **Активных параметров на токен**: ~6B (высочайшая выразительность при низкой вычислительной нагрузке на шаг).
- **Квантование**: GSQ-RCO IQ3_S (3.5 бит/токен) со встроенной мультимодальностью (Vision tower) и MTP-головой (Unsloth shared-Q8_0).
- **Таблица N-Gram**: Companion-файл `Qwen3.8-Flash-Next-ngram-table-IQ4_NL-ninfer-v3.ninfer` (~26.9 GiB) для послойных эмбеддингов (PLE).

---

## Конфигурация памяти и запуск (Tesla V100 32GB)

### Разделение памяти (VRAM & Disk/Host):
1. **Tesla V100 32GB (VRAM)**:
   - Dense Base & PLE Router: ~2.8 GiB
   - KV Cache & GDN Recurrent State (`rk4v4`): ~1.8 GiB при окне 200,000 токенов
   - Vision Tower: ~0.9 GiB
   - Динамический кэш экспертов: ~18.8 – 24.1 GiB
2. **Дисковый / хостовый уровень**:
   - На сервере с 31 GiB RAM используется режим `--expert-residency disk`, при котором 53.7 GiB экспертов читаются на лету через кэш страниц NVMe, предотвращая OOM в системной памяти.
   - N-gram строки считываются по 16 строк на токен с диска (`--ngram-residency disk`).

### Рекомендованные параметры инференса (JimmyMax/ninfer-v100):
- **Спекулятивное декодирование**: MTP с 3 драфт-токенами (`--spec mtp --draft-tokens 3 --lm-head-draft`).
- **Буферизация префилла**: `--expert-prefill-buffers 2` (двойной буфер перекрывает передачу по шине PCIe вычислениями текущего слоя).
- **Контекст**: До 200,000 токенов (рабочий потолок для обслуживания длинных цепочек рассуждений).
- **DMA Share**: `--expert-dma-share 1` (вычисления экспертов выполняются на GPU).

---

## Сетевые интерфейсы и интеграция

- **Прямой эндпоинт**: `http://192.168.50.38:8087/v1`
  - Health check: `http://192.168.50.38:8087/health`
  - Модели: `http://192.168.50.38:8087/v1/models`
- **Через Llama-Swap**: `http://192.168.50.38:8090/v1`
  - Имя модели: `qwen3.8-flash-next`
  - Алиасы: `flash-next`, `flash-next-iq3s`, `qwen-flash`
- **Через OmniRoute**: `http://192.168.50.38:20128/v1`

---

## Управление контейнером

- **Запуск / переключение**:
  ```bash
  /opt/models/flash_next/switch-to-flash-next.sh
  ```
- **Логи контейнера**:
  ```bash
  docker logs -f flash-next-llama-server
  ```
- **Остановка**:
  ```bash
  docker stop -t 5 flash-next-llama-server
  ```
