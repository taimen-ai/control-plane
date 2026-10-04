# ADR-0051: Execution trace автономного прогона — транскрипт как артефакт и action на вызов инструмента

Статус: Accepted (TASK-000093, 2026-09-10)

Контекст: ADR-0019 «Execution audit: run actions» — действия без payload'ов;
ADR-0013/0020 — модель артефактов и их ревизии; ADR-0016 — адаптеры
автономного рантайма; `harness-protocol.md` §8, §10.

## Контекст

До этого решения адаптеры Claude Code и Codex публиковали о прогоне ровно
две вещи: один артефакт `report` с финальным summary и один run action
(`claude-code.turn` / `codex.turn`) на весь ход. Всё остальное — сообщения
ассистента, каждый `tool_use` и `tool_result`, команды, MCP-вызовы —
оставалось в локальном логе runner-хоста (`0600`, потолок 32 МБ) и никуда
не уходило. Правило было сознательным: «ничего сказанное в разговоре не
становится артефактом».

Правило сделало консоль слепой. Оператор видит, что прогон был, сколько он
стоил и чем кончился, но не видит, что агент делал: какие инструменты
вызывал, что они вернули, где он ошибся и почему принял решение. Для
пилота автономного исполнителя это ровно то, что нужно проверять, а
единственный способ сегодня — `ssh` на runner и чтение JSONL.

ADR-0019 при этом остаётся верным: `run_actions` — аудит с ссылками, не
хранилище payload'ов, и не журнал. Скрытые рассуждения модели (`thinking`,
reasoning summaries) не хранятся нигде, и это не пересматривается.

## Решение

1. **Транскрипт публикуется как артефакт типа `transcript`.** Один документ
   на прогон, схема `agent-transcript/1`:

   ```json
   {
     "schema": "agent-transcript/1",
     "harnessType": "claude-code",
     "sessionId": "…", "model": "…", "tools": ["Bash", "…"],
     "entries": [
       {"seq": 1, "at": "…", "kind": "tool_call", "call": 1, "callId": "…", "tool": "Bash", "input": "…"},
       {"seq": 2, "at": "…", "kind": "tool_result", "call": 1, "callId": "…", "isError": false, "output": "…"},
       {"seq": 3, "at": "…", "kind": "assistant", "text": "…"}
     ],
     "final": {"text": "…", "truncated": false},
     "usage": {"inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "durationMs": 0},
     "stats": {"assistantMessages": 0, "toolCalls": 0, "toolErrors": 0,
               "hiddenReasoningBlocks": 0, "truncatedEntries": 0, "droppedEntries": 0},
     "truncated": false
   }
   ```

   Виды записей: `assistant`, `user`, `tool_call`, `tool_result`. Итоговый
   ответ — отдельное поле `final`, у него собственный лимит: он не
   вытесняется записями. `metadata` артефакта дублирует счётчики (`entries`,
   `toolCalls`, `toolErrors`, `model`, токены, `costUsd`, `truncated`), чтобы
   список артефактов читался без открытия документа.

2. **Документ ограничен и отредактирован до публикации.** Потолок 512 КиБ
   на документ; текстовый блок — 20 000 символов, вход и выход инструмента —
   по 6 000, итог — 60 000. Сверх потолка записи не хранятся, а считаются
   (`droppedEntries`). Каждая строка проходит `redact_local_paths` и
   `redact_credentials` (префиксы токенов, JWT, `key=value`-формы,
   приватные ключи); символ NUL (`U+0000`) в ней заменяется на `U+FFFD` —
   API отвергает NUL в теле запроса (CP-ADR-0083, А2). Документ, который после этого всё равно не проходит
   `assert_portable`, публикуется **withheld**: счётчики и usage без текста.
   Прогон из-за транскрипта не падает никогда — урок TASK-000040.

3. **Скрытые рассуждения не хранятся.** `thinking` и `redacted_thinking`
   блоки Claude Code, `reasoning` элементы Codex только увеличивают
   `hiddenReasoningBlocks`. Промпт адаптера в документ не попадает.

4. **Один run action на вызов инструмента, вживую.** При `tool_use`
   адаптер пишет `POST /runs/{id}/actions` со `status=started`,
   `action=tool.<имя>`, `externalReference=<harness>:session/<id>#call/<n>`
   и `metadata={tool, call, summary}` (summary — одна строка входа,
   ≤160 символов, отредактированная); при `tool_result` — `:finish` со
   статусом `completed` или `failed`. Payload'ы в action не пишутся — они в
   артефакте, action ссылается на них ординалом `call`. Незакрытые на момент
   конца хода действия закрываются как `failed`. Это соблюдает ADR-0019
   буквально и даёт консоли живой ход прогона до его окончания.

5. **Бюджет действий действует на нарратив, не на работу.** `409
   budget_exceeded` при записи action выключает дальнейшую запись actions
   в этом прогоне; транскрипт продолжает копиться и публикуется целиком.

6. **Настраивается на деплойменте, не в коде.** `CONTROL_PLANE_TRACE_TRANSCRIPT`,
   `CONTROL_PLANE_TRACE_ACTIONS`, `CONTROL_PLANE_TRACE_TOOL_RESULTS` (все по
   умолчанию `1`). Третья переменная оставляет в документе только размер и
   флаг ошибки результата инструмента — для площадок, где вывод инструментов
   не должен покидать хост, при этом ход разговора и аудит сохраняются.

7. **Ядро не меняется.** Ни таблиц, ни эндпоинтов, ни событий: артефакт и
   actions — существующие контракты. Ограничение тела запроса 1 МиБ
   покрывает документ с запасом. Консоль читает `GET /artifacts?runId=`,
   `GET /runs/{id}/actions`, `GET /runs/{id}/checkpoints`.

Общий код — `control_plane_agent/trace.py` (`TranscriptBuilder`,
`TraceRecorder`, `TraceSettings`, редакция); вендорные отображения событий —
`consume_stream_event` в `control_plane_claude/adapter.py` (stream-json:
`assistant`/`user`/`system.init`/`result`) и `CodexEventMapper` в
`control_plane_codex/adapter.py` (`item.started`/`item.completed`:
`command_execution`, `mcp_tool_call`, `file_change`, `web_search`,
`agent_message`, `reasoning`).

## Не принято

- **Отдельная таблица сообщений прогона в ядре.** Control Plane — не
  файловое хранилище (§8); транскрипт — work product прогона, и модель
  артефактов уже даёт ему provenance, ревизии и права. Если документы
  вырастут за потолок, следующий шаг — `uri` на внешнее хранилище, а не
  новая сущность.
- **Полные payload'ы в `run_actions`.** ADR-0019 остаётся в силе.
- **Хранение chain-of-thought.** Не хранится ни в каком виде.
- **Транскрипт в журнале событий.** События по-прежнему несут только ссылки.
- **OpenCode-раннер в этом срезе.** У него нет локального потока событий;
  транскрипт можно снять через `GET /session/{id}/message` сервера OpenCode
  тем же билдером — отдельной задачей.

## Последствия

- Консоль получает экран прогона: ход, вызовы инструментов с входом и
  выходом, итог, checkpoints, actions — и обновление вживую по actions,
  пока прогон идёт.
- Артефактов на прогон становится два (`report` и `transcript`); клиенты,
  считавшие «первый артефакт = summary», продолжают работать: порядок
  сохранён, summary первый.
- Метаданные `report` дополнены `inputTokens`/`outputTokens` у Claude Code —
  первый шаг к учёту стоимости из ADR-0026 суперпроекта.
- Runner-хост по-прежнему хранит сырой поток; после подтверждения работы
  экрана его можно сократить (`CONTROL_PLANE_CLAUDE_LOGS=0`), но это
  отдельное решение эксплуатации.
- Проверка: `tests/unit/test_trace.py` (редакция, бюджет, withheld,
  actions без payload'ов, отключение), `test_claude_adapter.py` и
  `test_codex_adapter.py` (форма документа, промпт и thinking не уходят,
  порядок actions).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane_agent/trace.py, pattern: 'TRANSCRIPT_SCHEMA = "agent-transcript/1"'}
  repo: control-plane
- grep: {path: src/control_plane_agent/trace.py, pattern: 'TRANSCRIPT_ARTIFACT_TYPE = "transcript"'}
  repo: control-plane
- grep: {path: src/control_plane_agent/trace.py, pattern: 'MAX_TRANSCRIPT_BYTES = 512 \* 1024'}
  repo: control-plane
- grep: {path: src/control_plane_agent/trace.py, pattern: 'flag\("CONTROL_PLANE_TRACE_TOOL_RESULTS"\)'}
  repo: control-plane
- grep: {path: src/control_plane_agent/trace.py, pattern: 'return f"tool\.\{cleaned\}"'}
  repo: control-plane
- grep: {path: tests/unit/test_trace.py, pattern: 'def test_hidden_reasoning_is_counted_not_stored'}
  repo: control-plane
```
