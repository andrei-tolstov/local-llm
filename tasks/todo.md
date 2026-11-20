# Задача: движок Strata-V100 для Qwen3.8-Flash-Next (максимум контекста)

Запрос: добавить новый двигатель в `models/qwen3.8_flash_next/`, структуру файлов как в
`coder390`,.analysis параметров запуска под железо хоста (нужен максимальный контекст),
пуш в git. https://github.com/jmnargi/Strata-V100#use-strata

## План

- [x] 1. Изучить раскладку `models/qwen3.8_coder390/` (один каталог на движок:
  compose + Dockerfile + .env.example, общие скрипты в корне модели)
- [x] 2. Замерить железо: GPU/UUID/UUID sm, драйвер, CPU-флаги, RAM, диск
- [x] 3. Разобрать апстрим: релизы, Dockerfile, docker-entrypoint.sh, setup.py
  (пины GGUF, контексты, флаги сборки sm_70 / ISA floor, systemd-профиль V100)
- [x] 4. Написать `jmnargi-strata-engine/`: Dockerfile.strata-v100,
  docker-compose.strata-v100.yml, .env.strata-v100.example,
  download_model_strata.sh, setup_and_build_strata.sh, switch-to-strata.sh, .dockerignore
- [x] 5. Рассчитать профиль максимального контекста и задокументировать его в .env и отчёте
- [x] 6. Зарегистрировать движок в `llama-swap/config.yaml` (8089, context 262144)
- [x] 7. Дополнить `models/qwen3.8_flash_next/AGENTS.md` разделом про второй движок
- [x] 8. Машинная проверка конфигов (compose config, docker build --check, bash -n, yaml parse)
- [x] 9. Коммит и пуш в origin/master
- [ ] 10. Первый прогон (скачивание ~68 GB, остановка `flash-next-llama-server`, сборка
  движка) — отложен: требует места на диске и решения по рабочему контейнеру

## Проверка (что реально выполнено)

| Шаг | Команда | Результат |
|---|---|---|
| Compose | `docker compose --env-file .env -f docker-compose.strata-v100.yml config` | exit 0, device_ids и переменные разворачиваются |
| Dockerfile | `docker build --check` | «Check complete, no warnings found»; базовые образы cuda:12.8.0 резолвятся |
| Скрипты | `bash -n` по трём .sh | ОК |
| llama-swap | `python3 -c yaml.safe_load` | 8 моделей, `flash-next-strata` → 8089, context 262144 |
| Железо | nvidia-smi / lscpu / free / df | V100 32GB (занята ninfer на 31.2 GiB), GTX 1060 (sm_61), CPU без AVX2, RAM 62.74 GiB (58.56 занято), диск 80G свободно |

## Итог по максимальному контексту

262144 токенов — нативная длина обучения модели, это +31% к текущим 200 000 у ninfer-профиля
и потолок без экспериментального rope scaling. KV int8 стоит 276 MiB VRAM на 262144,
стриминг KV ~3.6 GiB RAM: узкое место не контекст, а арена экспертов и RAM хоста.
Профиль по умолчанию — IQ2_XS (арена 35.5 GiB). IQ3_S требует 83.6 GB на диск, а свободно 80 GB.

## Что осталось незакрытым (осознанно)

- В `llama-swap/config.yaml` (незакоммиченная правка до начала задачи) алиас `flash-next`
  объявлен и у модели coder390, и у flash-next — дубль. Не трогал, требуется решение.
- Первый прогон движка не выполнялся: ~68 GB трафика, распаковка пакета и остановка
  рабочего `flash-next-llama-server`.
