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

---

## Второй движок: Strata-V100 (jmnargi/Strata-V100)

Тот же чекпоинт, другой движок: llama.cpp-потомок Strata с i-квантами GSQ-RCO в GGUF.
Раскладка каталога повторяет `models/qwen3.8_coder390`: на каждый движок своя
подпапка с compose-файлом, Dockerfile и примером окружения.

```
models/qwen3.8_flash_next/
  download_model_strata.sh              # GGUF: revision, размеры и SHA-256 зафиксированы
  setup_and_build_strata.sh             # проверка хоста + сборка образа движка
  switch-to-strata.sh                   # остановка прочих серверов и запуск Strata
  jmnargi-strata-engine/
    docker-compose.strata-v100.yml      # сервис flash-next-strata, порт 8089
    Dockerfile.strata-v100              # сборка: CUDA 12.8 + sm_70 + AVX-пол
    .env.strata-v100.example            # параметры запуска с замерами хоста
    .dockerignore
```

Скрипты — в корне модели, как `switch-to-flash-next.sh` и как у `coder390`; внутри
движка лежат только compose, Dockerfile и пример окружения.

### Почему образ собирается, а не берётся готовый
- **CUDA только 12.x**: CUDA 13 не компилирует sm_70 (Volta). Апстримный Dockerfile
 начинается с nvidia/cuda:13.0.0 — для V100 не годится.
- **CPU без AVX2** (Xeon E5-2650 v2): готовые сборки Strata собраны под AVX2 и падают
  с SIGILL (exit 132). Обязателен флаг `-DSTRATA_ISA_FLOOR=avx`.
- **Volta-пути**: `-DSTRATA_EXPERIMENTAL_SM60=ON` включает Volta prompt-attention и
  чанковую GDN-рекурренцию; `-DCMAKE_CUDA_ARCHITECTURES=70`.
- Драйвер 550.163.01: для CUDA 12.x хватает 525+, cuda-compat не подключается
  (на GTX 1060 forward-compat из образов 12.8 ломается с ошибкой 804).

### Контекст и память (замерено на хосте 2026-11-20)
| Параметр | Значение |
|---|---|
| Контекст по умолчанию | **131072** — рекомендация самого движка: `rec_ctx` по VRAM (32 GB карта => 131072) и минимум с правилом RAM |
| Почему не 262144 | Правилу RAM (`арена + контекст·13·1056 Б + 24 GB`) на 262144 нужно 77.9 GB, а RAM 62.7 GB. 262144 открывается только с `LOW_RAM=resident` (KV тогда в VRAM) |
| Больше 262144 | Только rope scaling (yarn), в доках помечено EXPERIMENTAL; docker-entrypoint.sh таких флагов не передаёт |
| KV int8 | 1056 Б на токен (12 слоёв QSA + MTP) => 138 MiB VRAM на 131072 |
| KV streaming | ~13.7 КБ на токен в RAM => ~1.8 GiB на 131072, освобождённую VRAM берёт кэш экспертов |
| Арена экспертов | IQ2_XS 35.5 GiB, IQ3_S 50.3 GiB |
| Скачивание пакета | IQ2_XS 68.0 GB, IQ3_S 83.6 GB, Q2_0 66.4 GB |
| RAM хоста | 62.74 GiB всего; ninfer-профиль держит 58.56 GiB => движки живут по одному |
| Диск | 80 GB свободно. По формуле setup.py: IQ2_XS ~76 GB (GGUF + ~8 GB пакета) проходит впритык; IQ3_S ~92 GB — нет, с `resident` ~143 GB |
| Уже скачанные файлы | В `/opt/models/flash_next` лежат `*.ninfer` (магия `NINFER`) — пакеты движка NInfer, не GGUF. Strata их не читает, GGUF качается отдельно |

### Три яруса памяти в Strata (аналог ninfer: expert cache / expert-residency / ngram-residency)

| Ярус | Чем задаётся | Значение на этом хосте |
|---|---|---|
| VRAM: кэш экспертов | `--expert-cache auto` стоит в конфиге всегда | ~25.6 GB экспертов при 131072 (VRAM - 5 GB на dense, буферы и KV на 32K - KV сверх 32K); при 262144 оставалось бы ~23.9 GB |
| RAM: остальные эксперты | `LOW_RAM=off` (вся арена) / `resident` (`--resident-experts`) / `on` или `mmap` (`--mmap-experts`) | IQ2_XS: арена 35.5 GB целиком в RAM. IQ3_S: нужен `resident`, тогда ~26.5 GB в RAM |
| NVMe: n-gram (PLE) таблица | `--ple-io`, по умолчанию `direct` - небуферизованные чтения мимо кэша ОС, строки IQ4_NL по 90 байт | 28.8 GB на NVMe 970 EVO Plus, rotational=0; `--ple-io ram` потребовал бы ~95 GB RAM, setup его не включает |

Правило контекста по RAM из setup.py (#406): для IQ3_S и IQ3_XXS нужно арена +
контекст умножить на 13 умножить на 1056 байт + 24 GB. При 62.74 GB RAM и `LOW_RAM=off`
это максимум 131072 токенов (на 262144 требуется 77.9 GB). С `LOW_RAM=resident` правило
не действует - KV живёт в VRAM - и 262144 становится доступен. Для IQ2_XS правило RAM
не ограничивает вовсе, там остаётся `LOW_RAM=off`.

### Запуск
```bash
cd models/qwen3.8_flash_next          # на хосте - каталог развёртывания модели
cp jmnargi-strata-engine/.env.strata-v100.example jmnargi-strata-engine/.env
./download_model_strata.sh /opt/models/flash_next/gguf IQ2_XS   # ~76 GB
./setup_and_build_strata.sh                                    # проверка хоста + сборка
./switch-to-strata.sh                                          # останавливает прочие серверы
```

Скрипты принимают каталог первым аргументом (`./switch-to-strata.sh
/opt/models/flash_next`), как `switch-to-flash-next.sh`. Контекст меняется через
`CONTEXT` в `.env` вместе с `REINSTALL=1`, чтобы движок пересобрал установку.

- **Прямой эндпоинт**: `http://192.168.50.38:8089/v1` (8087 — ninfer, 8088 — qwen-image)
- **Llama-Swap**: модель `qwen3.8-flash-next-strata`, алиасы `flash-next-strata`, `flash-next-iq2xs`
- **Логи**: `docker logs -f flash-next-strata-llama-server`
- GPU закрепляется UUID V100 (`GPU-733ae7cc-c1bf-3352-42d5-ba2762470325`); GTX 1060 (sm_61)
  контейнеру не отдаётся — движок собран под sm_70.
