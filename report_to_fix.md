# Отчёт о проблеме с утечкой вызовов функций в Hermes Agent и инструкция по исправлению

> **Дата анализа:** 2026-11-06 / 2026-11-08  
> **Хост:** `192.168.50.38`  
> **Компоненты:** Hermes Agent (`/root/.hermes`), движок инференса `coder390-llama-server` (`ninfer-serve`, порт `8084` за `llama-swap:8090`).

---

## 1. Описание бага и симптомы

В сессиях Hermes Agent (в частности, в сессии `20261105_215558_96c171` за 2026-11-05 22:29–23:02 МСК и 2026-11-06 09:17–10:05 МСК) агент вошёл в бесконечный цикл.
Модель `coder390` пыталась отредактировать код (`sources.py`) и генерировала нативный вызов инструмента:
```xml
<tool_call>
<function=patch>
<parameter=path>/root/ru-marketplace-mcp/sources.py</parameter>
...
</function>
</tool_call>
```

Однако в логах `docker logs coder390-llama-server` было зафиксировано более **30 предупреждений подряд**:
```
WARN req#N tool markup returned as text | undeclared tool
```
Движок `ninfer-serve` вернул блок `<tool_call>...</tool_call>` не как структурированный `delta.tool_calls`, а как обычный текст (`content`) с `finish_reason: "stop"`.

В коде Hermes функция `strip_think_blocks` (`/root/.hermes/hermes-agent/agent/agent_runtime_helpers.py:213`) содержит регулярное выражение:
```python
_TOOL_CALL_TAG_NAMES = ("tool_call", "tool_calls", "tool_result", "function_call", "function_calls")
_TOOL_CALL_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{_NS_PREFIX}{name}\b[^>]*>.*?</{_NS_PREFIX}{name}>", re.DOTALL | re.IGNORECASE)
    for name in _TOOL_CALL_TAG_NAMES
)
```
Она **молча удаляет** сырой блок `<tool_call>...</tool_call>` из текста!
В итоге от ответа модели оставалась либо пустая строка (`Thinking-only response` → 3 ретрая), либо одна фраза `"Вставляю drom SourceSpec в реестр."` без единого вызова инструмента. Агент `goal_judge` считал, что задача не завершена, и повторно отправлял `[Continuing toward your standing goal] Goal: продолжай` — 15 раз подряд в бесконечном цикле.

---

## 2. Корневые причины (Root Causes) в `hermes-laya-tools`

Почему `patch`, `read_file`, `write_file` оказались `undeclared tool` (в `tools` запроса отправлялось только 3 или 13 инструментов вместо всех 18 базовых)?

### Причина 1: Инверсия приоритетов конфигурации в `config.py`
- В веб-интерфейсе (`/root/.hermes/config.yaml`) был выбран безопасный режим `mode: Добавление топ 3` (`enrich_top3`) и `hierarchical: false`. В этом режиме все 18 базовых инструментов Hermes **всегда остаются в запросе**, а Laya лишь добавляет к ним топ-3 отложенных инструмента.
- Однако в файле `/root/.hermes/.env` были прописаны старые значения:
  ```bash
  LAYA_MODE=active
  LAYA_HIERARCHICAL=true
  ```
- В `/root/.hermes/plugins/hermes-laya-tools/config.py` функция `_resolve_val` проверяла `env_vars` (`os.environ`) **раньше**, чем `yaml_settings` (`config.yaml`), хотя документация гласит: `1. ctx -> 2. config.yaml -> 3. .env -> 4. default`.
- В результате настройки из `config.yaml` полностью игнорировались, и плагин принудительно работал в агрессивном режиме `active`, урезая список инструментов.

### Причина 2: Слово `"продолжай"` в списке приветствий `_ACK_WORDS`
- В `/root/.hermes/plugins/hermes-laya-tools/hygiene.py` (строки 39–49):
  ```python
  _ACK_WORDS = frozenset({
      "ok", "ок", "хорошо", "ладно", "понял", "ясно", "принято",
      "продолжай", "давай", "вперед", "go", "continue", "proceed",
  })
  ```
- Когда пользователь написал `"продолжай"` в 09:17:03, функция `looks_trivial()` сочла это за пустое приветствие (`trivial_bypass -> direct_chat`) и **удалила все инструменты**, оставив только 3 мостовых (`tool_search`, `tool_describe`, `tool_call`)!

### Причина 3: Необратимое затирание `agent.tools` между ходами
- В `/root/.hermes/plugins/hermes-laya-tools/__init__.py` (строки 222–238):
  На 1-м ходе `_on_pre_llm_call` сохраняет полный список в `_saved_agent_tools[session_id]` и перезаписывает `agent.tools = effective_tools` (3 инструмента).
  На 2-м ходе строка 236 восстанавливала `all_tools` из `_saved_agent_tools` **только при `if _config.is_enrich_top3:`**! В режиме `active` на 2-м и последующих ходах `all_tools` брался из `agent.tools` (уже урезанного до 3 инструментов). Поэтому даже при таймауте Laya (`fallback_mode: pass-all`) отправлялись только те же 3 инструмента.

### Причина 4: Падение `NoneType` в `_on_pre_api_request`
- В `__init__.py` (строки 333–335):
  ```python
  body = req.get("body") if isinstance(req, dict) else {}
  ```
  Если `req = {"body": None}`, переменная `body` становится `None`, и последующий вызов `body.get("tools")` падает с `AttributeError: 'NoneType' object has no attribute 'get'`. Это было зафиксировано в `agent.log` в `09:47:20`.

### Причина 5: Отсутствие fallback-восстановления `<tool_call>` из текста
- Если `ninfer-serve` по любой причине вернул `<tool_call><function=NAME>...</tool_call>` в текст (`content`), Hermes удаляет его через `strip_think_blocks` вместо того, чтобы восстановить вызов.

---

## 3. Инструкция по исправлению Hermes Agent (когда потребуется применить)

### Шаг 1. Исправить приоритет в `/root/.hermes/plugins/hermes-laya-tools/config.py`
В функции `_resolve_val`:
```python
# Было:
for source in (ctx_vars, env_vars, yaml_settings):

# Должно быть:
for source in (ctx_vars, yaml_settings, env_vars):
```
И в `/root/.hermes/.env` обновить:
```bash
LAYA_MODE=enrich_top3
LAYA_HIERARCHICAL=false
```

### Шаг 2. Исключить команды продолжения из `_ACK_WORDS` в `hygiene.py`
В `/root/.hermes/plugins/hermes-laya-tools/hygiene.py`:
Удалить из `_ACK_WORDS` слова: `"продолжай"`, `"давай"`, `"вперед"`, `"proceed"`, `"continue"`. Эти слова выражают команду продолжать работу над задачей, а не тривиальное пустое приветствие.

### Шаг 3. Исправить восстановление инструментов и `NoneType` в `__init__.py`
В `/root/.hermes/plugins/hermes-laya-tools/__init__.py`:
1. В `_on_pre_llm_call`:
   ```python
   # Всегда восстанавливать all_tools из сохранённой копии сессии:
   if session_id in _saved_agent_tools:
       all_tools = list(_saved_agent_tools[session_id])
   else:
       all_tools = list(getattr(agent, "tools", []) or [])
       _saved_agent_tools[session_id] = list(all_tools)
   ```
2. В `_on_pre_api_request`:
   ```python
   body = (req.get("body") if isinstance(req, dict) else None) or {}
   ```

### Шаг 4. Добавить fallback-восстановление `<tool_call>` в `chat_completion_helpers.py`
В `/root/.hermes/hermes-agent/agent/chat_completion_helpers.py` в методе `_finish_chat_stream`:
Если `not mock_tool_calls and full_content and "<tool_call>" in full_content`:
- Распарсить `<tool_call><function=NAME>(<parameter=KEY>VALUE</parameter>)*</function></tool_call>`.
- Если `NAME` есть в `valid_tool_names`, добавить в `mock_tool_calls`.
- Если `NAME` был отложен через `tool_search`, завернуть в `tool_call(tool_name=NAME, tool_arguments=parsed_args)`.
- Очистить `full_content` от разобранного XML-блока.
- Установить `effective_finish_reason = "tool_calls"`.
