# Задача: движок Strata-V100 для Qwen3.8-Flash-Next + контекст по рекомендации

Запрос: добавить движок Strata-V100 в `models/qwen3.8_flash_next/` с раскладкой как у
`coder390`, проанализировать параметры запуска под железо (максимум контекста), пуш в git.
Уточнение 2: уменьшить контекст так, чтобы KV-кэш помещался по рекомендации движка,
использовать уже скачанную модель из /opt/models, файлы запуска разложить как у других
моделей, снова пуш.

## План

- [x] 1. Раскладка как у coder390: папка движка (compose + Dockerfile + .env.example +
  .dockerignore), скрипты в корне модели
- [x] 2. Сборка образа под хост: CUDA 12.8 + sm_70 + STRATA_ISA_FLOOR=avx + SM60
- [x] 3. Замеры хоста и расчёт памяти/контекста: tasks/strata-v100-flash-next-analysis.md
- [x] 4. Три яруса памяти движка (VRAM кэш экспертов / RAM эксперты / NVMe PLE) - флаги и
  числа, поправка: --resident-budget-gib к нашим пакетам не применяется
- [x] 5. Контекст снижен 262144 -> 131072 по формуле рекомендации из setup.py (7786-7788)
- [x] 6. Каталоги приведены к соглашению моделей: GGUF /opt/models/flash_next/gguf,
  данные движка /opt/models/flash_next/strata
- [x] 7. Скрипты переложены в корень модели (git mv), пути и аргументы поправлены
- [x] 8. llama-swap: context 131072 у flash-next-strata
- [x] 9. Проверка конфигов и скриптов, коммит и пуш
- [ ] 10. Первый прогон: ~76 GB GGUF + сборка образа + остановка flash-next-llama-server

## Проверка (что реально выполнено)

| Шаг | Команда | Результат |
|---|---|---|
| Compose | `docker compose --env-file .env.strata-v100.example -f docker-compose.strata-v100.yml config` | exit 0; CONTEXT 131072, каталоги /opt/models/flash_next/{gguf,strata} |
| Dockerfile | `docker build --check` | «Check complete, no warnings found» |
| Скрипты | `bash -n` трёх скриптов | ОК |
| llama-swap | `python3 -c yaml.safe_load` | 8 моделей, flash-next-strata -> 8089, context 131072 |
| Формат файлов в /opt/models | `od -c` первых 16 байт | магия `NINFER` - не GGUF, переиспользовать нельзя |
| Формула рекомендации | `sed -n 7786,7788p setup.py` | rec_ctx 131072 по VRAM, RAM-правило даёт тот же 131072 |

## Что осталось незакрытым

- GGUF для Strata не скачаны: в /opt/models/flash_next лежат .ninfer-пакеты NInfer.
  IQ2_XS требует ~76 GB (свободно 80 GB - впритык), IQ3_S ~92 GB - не помещается.
- Первый прогон (скачивание, сборка образа, остановка рабочего flash-next-llama-server)
  не выполнялся - нужно решение по диску и окну простоя.
- В llama-swap/config.yaml остаётся дубль алиаса `flash-next` (у coder390 и у flash-next) -
  чужая незакоммиченная правка, требовала бы отдельного решения.

