# TODO: Добавление модели Qwen3.8-Flash-Next IQ3_S (JimmyMax/ninfer-v100)

- [x] Исследование и согласование архитектуры и параметров запуска <!-- id: 0 -->
- [x] Разработка артефакта Implementation Plan с детальной спецификацией <!-- id: 1 -->
- [x] Создание директории `models/qwen3.8_flash_next` и конфигурации: <!-- id: 2 -->
  - [x] `Dockerfile.ninfer-v100` (база Volta sm_70, JimmyMax/ninfer-v100 v1.7.2) <!-- id: 3 -->
  - [x] `.env.ninfer-v100.example` и `.env` со всеми рекомендуемыми параметрами <!-- id: 4 -->
  - [x] `docker-compose.ninfer-v100.yml` и `docker-compose.yml` <!-- id: 5 -->
  - [x] `download_model.sh` (загрузка IQ3_S 54.5GB + ngram table 26.9GB) <!-- id: 6 -->
  - [x] `setup_and_build.sh` (автоматизация подготовки, symlink, docker build) <!-- id: 7 -->
  - [x] `switch-to-flash-next.sh` (оркестрация переключения и healthcheck) <!-- id: 8 -->
  - [x] `AGENTS.md` (руководство для агентов и системные характеристики) <!-- id: 9 -->
- [x] Интеграция в роутер `llama-swap/config.yaml` (порт 8087, context 200k, алиасы) <!-- id: 10 -->
- [x] Загрузка весов модели и companion n-gram таблицы на сервере 192.168.50.38 (100% завершено) <!-- id: 11 -->
- [x] Исправление ошибки интеграции с llama-swap ("No such container: flash-next-llama-server"): <!-- id: 12 -->
  - [x] Обновить `.env` и `.env.ninfer-v100.example` (`FLASH_NEXT_EXPERT_CACHE_MIB=18432`) <!-- id: 13 -->
  - [x] Обновить `docker-compose.ninfer-v100.yml` и `docker-compose.yml` (добавить `--expert-cache-mib 18432`, ограничить `--expert-prefill-buffers` только для `host` режима) <!-- id: 14 -->
  - [x] Обновить `llama-swap/config.yaml` (безопасные команды stop, пересоздание контейнера на хосте) <!-- id: 15 -->
  - [x] Удалить старый контейнер и пересоздать `flash-next-llama-server` на хосте через compose up --no-start <!-- id: 16 -->
  - [x] Перезапустить `deepseek-llama-swap` <!-- id: 17 -->
  - [x] Протестировать переключение и инференс через llama-swap:8090 (запрос к `qwen3.8-flash-next`) <!-- id: 18 -->
  - [x] Протестировать возврат на `coder390` <!-- id: 19 -->
  - [x] Закоммитить изменения и запушить в GitHub (`origin/master`) <!-- id: 20 -->
  - [x] Синхронизировать проект на удаленном сервере в `/opt/local-llm` <!-- id: 21 -->

- [x] Оптимизация использования оперативной памяти (~25-28 Гб RAM под N-gram companion table, Вариант 3): <!-- id: 22 -->
  - [x] Разработка плана расчетов RAM (`qwen38_flash_next_ram_optimization_plan.md`) <!-- id: 23 -->
  - [x] Обновление конфигурации: `FLASH_NEXT_NGRAM_RESIDENCY=ram`, `FLASH_NEXT_NGRAM_LOCK=true`, `FLASH_NEXT_HOST_CACHE_MIB=0` <!-- id: 24 -->
  - [x] Обновление `docker-compose.ninfer-v100.yml` и `docker-compose.yml` (проброс `--ngram-table`, `--ngram-residency ram`, `--ngram-lock`, `--host-cache-mib 0`) <!-- id: 25 -->
  - [x] Запуск и верификация: контейнер заблокировал 27465 MiB (~26.8 GiB) физической RAM под n-gram таблицу (`MEM USAGE: 28GiB / 31.29GiB`, 89.5%) <!-- id: 26 -->
  - [x] Успешное тестирование инференса через llama-swap:8090 и обратного переключения на coder390 <!-- id: 27 -->
  - [x] Фиксация в git master и синхронизация на 192.168.50.38 (`80f0155`) <!-- id: 28 -->

## Результаты тестирования и верификации

1. **Динамическое переключение через Llama-Swap**:
   - Запрос на роутер `http://192.168.50.38:8090/v1/chat/completions` с моделью `flash-next`:
     - `deepseek-llama-swap` корректно остановил `coder390-llama-server`.
     - Запустил `flash-next-llama-server`.
     - Движок инициализировался: базовые веса 4.57 GiB, эксперты на NVMe со VRAM-кэшем 18432 MiB, n-gram companion table в RAM (27465 MiB, locked).
     - Использование памяти контейнером: 28 GiB / 31.29 GiB (89.5% RAM), хост: available 1.9 GiB (без OOM).
     - Healthcheck на порту 8087 пройден успешно.
     - Ответ сгенерирован со спекулятивным декодированием MTP + n-gram (`draft_n_accepted: 14` из 15, 93.3% acceptance).
2. **Возврат на primary модель (`coder390`)**:
   - При запросе к `coder390` контейнер `flash-next-llama-server` был остановлен и освободил 28GB RAM и 32GB VRAM.
   - `coder390-llama-server` запустился и вернулся в состояние `ready` (13.88 GiB RAM).
3. **Состояние репозитория и окружения**:
   - Все изменения запушены в GitHub `origin/master` (`80f0155`).
   - Удаленный хост `192.168.50.38` полностью синхронизирован в `/opt/local-llm` и `/opt/models/flash_next`.
