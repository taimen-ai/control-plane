*Русская версия. English: [README.md](README.md)*

# control-plane

Control Plane — product-neutral координационная платформа, в которой люди,
AI-агенты и
автоматизированные процессы являются **равноправными участниками организации**:
задачи, principals, рабочие сессии, **атомарные захваты задач с lease и fencing
token**, runs (попытки исполнения), artifacts, approvals, организационная
модель (workspaces / roles / capabilities / skills), неизменяемый журнал
доменных событий, transactional outbox и realtime-обновления через WebSocket.

**v0.2 Organization Model**: человек и агент — оба `Principal` и работают через
единый транзакционный протокол координации. Различия выражаются через
`principal.kind`, роли, capabilities, skills и permissions — не через отдельные
системы задач.

**v0.3 Harness Protocol & Execution Runtime**: к Control Plane подключаются
реальные рабочие среды через единый версионируемый протокол
[`control-harness`](docs/harness-protocol.md): регистрация harness при
открытии сессии, bootstrap-контекст (`GET /harness/context`), discovery
доступной работы (`GET /work/available`, subtree-фильтры по workspaces),
Run Context, checkpoints, approval gates c suspension, ревизии артефактов,
skill-версии (`name@version`), кооперативная отмена, execution audit
(`run_actions`) и бюджеты runs. В репозитории: официальный SDK
`control_plane_client`, CLI `control-plane`, product-neutral MCP-сервер
`control-plane-mcp` для human harness
([Codex](docs/codex.md), [Claude Code](docs/claude-code.md)) и reference автономный демон
`control-plane-agent`. Principal может выйти из одного
процесса, вернуться через другой harness и продолжить работу — continuity
живёт на сервере, а не в разговоре.

**v0.5 Project Model**: портфели, программы, проекты, подпроекты и рабочие
потоки описываются **одним** деревом Workspace. Project — это Project Profile,
привязанный к Workspace один-к-одному; родитель проекта вычисляется как
ближайший предок с профилем, отдельного `parentProjectId` в модели нет
([ADR-0031](docs/adr/0031-project-profile-and-lifecycle.md)). К этому:
типизированные Workspace Types, неизменяемые версионированные Project
Templates, настраиваемый lifecycle с пятью системными категориями,
append-only ревизии конфигурации с детерминированным effective config и
provenance, типизированная решётка governance (потомок может только ужесточить
рамку предка), generic external references и project-scoped discovery/context.
В той же версии закрыт эксплуатационный долг v0.4: per-tenant изоляция
доставки, операторский redrive, retention/archive/rebuild журнала, сквозной
`X-Run-Id` и удаление окна совместимости продуктового имени.

## Архитектурное резюме

- **Модульный монолит**, два процесса из одной кодовой базы: HTTP API (FastAPI)
  и background worker.
- **PostgreSQL — единственный источник истины.** Текущее состояние живёт в
  нормализованных таблицах; доменные события — append-only журнал для аудита,
  realtime и downstream-потребителей (не event sourcing).
- **Одна команда — одна транзакция.** Успешная мутация атомарно пишет: новое
  состояние + запись в `events` + запись в `outbox` (+ `pg_notify`, который
  PostgreSQL доставляет подписчикам только при commit).
- **Конкурентность обеспечивает БД**: `SELECT ... FOR UPDATE`, частичный
  уникальный индекс «один active claim на задачу», уникальные ограничения,
  optimistic versioning (`If-Match`/ETag), fencing tokens, idempotency keys,
  `FOR UPDATE SKIP LOCKED` в worker.
- **Lease + fencing.** Сессии и claims — аренды, продлеваемые heartbeat'ами.
  Просроченный claim реквизируется атомарно самой командой claim — корректность
  не зависит от работы worker. Проснувшаяся старая сессия не может записать
  результат: её fencing token уже не равен `claim_epoch` задачи.
- **Организационная модель (v0.2).** Иерархические workspaces; роли
  (scope = поддерево workspace), capabilities и skill-реестр; задачи описывают
  требуемого исполнителя декларативно (`requirements`), claim проверяет
  eligibility; граф зависимостей задач с защитой от циклов и readiness;
  `Run` — попытка исполнения с зафиксированным fencing token (зомби-run не
  может записать результат после takeover); append-only artifacts; approvals
  «одна запись — одно решение». Claim = `tasks.claim` ∧ eligibility ∧
  readiness ∧ concurrency-правила.

Подробности: [docs/architecture.md](docs/architecture.md),
[docs/api.md](docs/api.md), [docs/harness-protocol.md](docs/harness-protocol.md),
эксплуатация — [docs/operations.md](docs/operations.md), обновление —
[docs/migration-v0.6.md](docs/migration-v0.6.md), решения —
[docs/adr/](docs/adr/README.md).

## Системные требования

- Python ≥ 3.12, [uv](https://docs.astral.sh/uv/)
- Docker + Docker Compose (для PostgreSQL и контейнерного запуска)
- PostgreSQL 16 (единственная поддерживаемая БД)

## Быстрый старт через Docker Compose

```bash
cp .env.example .env            # задайте CP_BOOTSTRAP_TOKEN
docker compose up -d --build db api worker
curl -f http://localhost:8000/health/ready
```

`api` сам применяет миграции (`alembic upgrade head`) при старте. OpenAPI:
<http://localhost:8000/docs>.

Контекст сборки — корень раскладки umbrella, на два уровня **выше** репозитория
(`services/control-plane`, TAI-ADR-0064): enforcement SDK лежит в репозитории
`platform-auth-sdk` по пути `sdk/platform-auth-sdk` и подключается путём, потому что
общего внутреннего индекса пакетов у платформы пока нет. Compose это учитывает
сам, а вручную образ собирается так:

```bash
docker build -f services/control-plane/Dockerfile -t control-plane ../..
```

## Запуск локально (без контейнера для приложения)

```bash
uv sync
docker compose up -d db                  # PostgreSQL на localhost:5433
uv run alembic upgrade head              # миграции
CP_BOOTSTRAP_TOKEN=dev-token uv run uvicorn control_plane.main:app --port 8000
# в соседнем терминале — worker:
uv run python -m control_plane.worker
```

## Bootstrap (одноразовая инициализация)

Пока в системе нет ни одного tenant, доступен guarded-endpoint:

```bash
curl -s -X POST http://localhost:8000/api/v1/bootstrap \
  -H "Authorization: Bearer $CP_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenantSlug":"acme","tenantName":"Acme Corp","adminDisplayName":"Alice"}'
```

Ответ содержит tenant, административный principal и **полный admin API key —
он показывается ровно один раз**. Повторный вызов → `409 already_bootstrapped`.

## Основной сценарий (пример)

```bash
ADMIN_KEY="cp_..."   # из ответа bootstrap
B="http://localhost:8000/api/v1"
H="Authorization: Bearer $ADMIN_KEY"

# 1. Агент + его ключ
AGENT=$(curl -s -X POST $B/principals -H "$H" -H "Content-Type: application/json" \
  -d '{"kind":"agent","displayName":"Build Agent"}')
AGENT_ID=$(echo $AGENT | jq -r .id)
AGENT_KEY=$(curl -s -X POST $B/principals/$AGENT_ID/api-keys -H "$H" \
  -H "Content-Type: application/json" \
  -d '{"permissions":["sessions.open","tasks.read","tasks.write","tasks.claim","events.read"]}' \
  | jq -r .key)
AH="Authorization: Bearer $AGENT_KEY"

# 2. Сессия агента
SESSION_ID=$(curl -s -X POST $B/sessions -H "$AH" -H "Content-Type: application/json" \
  -d '{"clientName":"builder","clientVersion":"1.0.0"}' | jq -r .id)

# 3. Задача
TASK=$(curl -s -X POST $B/tasks -H "$H" -H "Content-Type: application/json" \
  -d '{"title":"Ship the release","priority":"high"}')
TASK_ID=$(echo $TASK | jq -r .id)

# 4. Атомарный захват (lease + fencing token)
CLAIM=$(curl -s -X POST "$B/tasks/${TASK_ID}:claim" -H "$AH" \
  -H "Content-Type: application/json" -d "{\"sessionId\":\"$SESSION_ID\"}")
CLAIM_ID=$(echo $CLAIM | jq -r .id); TOKEN=$(echo $CLAIM | jq -r .fencingToken)

# 5. Heartbeat'ы аренды
curl -s -X POST "$B/sessions/${SESSION_ID}:heartbeat" -H "$AH" > /dev/null
curl -s -X POST "$B/claims/${CLAIM_ID}:heartbeat"     -H "$AH" > /dev/null

# 6. Завершение с optimistic concurrency + fencing
VERSION=$(curl -s "$B/tasks/$TASK_ID" -H "$AH" | jq -r .version)
curl -s -X POST "$B/tasks/${TASK_ID}:complete" -H "$AH" \
  -H "If-Match: \"task-$VERSION\"" -H "Content-Type: application/json" \
  -d "{\"claimId\":\"$CLAIM_ID\",\"fencingToken\":$TOKEN}"
```

Realtime: `GET /api/v1/events` (пагинация по sequence) и WebSocket
`/api/v1/events/ws?after=<sequence>` — клиент хранит последний обработанный
sequence и дочитывает пропущенное после reconnect.

## Операторский фронт

Веб-консоль Control Plane (11 экранов: обзор, фокус, задачи, агенты, approvals,
журнал событий, организация, типы задач, principals, память) живёт не здесь, а
модулем панели управления платформы — `platform-core/apps/web/src/products/control-plane`.
Вход в панель — Keycloak platform-core; к этому API она обращается через шлюз
platform-api `/api/v1/services/control-plane/…`, который федерирует identity
пользователя в IAM (`federation:exchange`) и получает токен audience
`control-plane`. Каждому человеку нужна строка `iam_principal_bindings`.

## Тесты

```bash
docker compose --profile test up -d db-test   # эфемерный PostgreSQL :5434
uv run pytest                                 # unit + integration + concurrency + e2e
# или: make test                              # то же, один прогон на рабочую копию
```

- Каждый процесс pytest создаёт свою базу рядом с базой из
  `CP_TEST_DATABASE_URL` (`<имя>_<epoch>_<hex>`) и удаляет её в конце; базы,
  оставшиеся от убитого прогона, удаляет следующий прогон через 6 часов.
  Параллельные прогоны на одном сервере друг друга не блокируют.
- Каждый тест ограничен 120 с (`pytest-timeout`, `timeout` в `pyproject.toml`);
  долгому тесту предел поднимают маркером `@pytest.mark.timeout(<с>)`.
  Зависшее ожидание блокировки падает со стеком, а не вешает прогон.
- `make test` берёт `flock` на `.pytest.lock`: второй `make test` в той же
  рабочей копии сразу выходит с сообщением «тесты уже идут» (код 75). При
  заданном `CP_TEST_DATABASE_URL` compose-база не поднимается; аргументы
  pytest — `make test PYTEST_ARGS="tests/unit -x"`. Автоматическим
  исполнителям тесты запускать через `make test`.

Линт и типы: `uv run ruff check . && uv run ruff format --check . && uv run mypy`.

## Миграции

```bash
uv run alembic upgrade head          # применить
uv run alembic revision --autogenerate -m "..."   # новая миграция (нужна запущенная БД)
uv run alembic downgrade -1         # откат
```

`/health/ready` возвращает 503, если ревизия БД отстаёт от head в коде.

## Пример v0.2: организация и единый протокол

```bash
# Организация: workspace, роль, capability, skill (админ)
WS=$(curl -s -X POST $B/workspaces -H "$H" -H "Content-Type: application/json" \
  -d '{"slug":"platform","name":"Platform"}' | jq -r .id)
curl -s -X POST $B/roles -H "$H" -H "Content-Type: application/json" \
  -d '{"slug":"software-engineer","name":"Software Engineer"}' > /dev/null
# Профиль агента: /principals/{id}/roles|capabilities|skills

# Задача с требованиями вместо конкретного исполнителя
curl -s -X POST $B/tasks -H "$H" -H "Content-Type: application/json" -d '{
  "title": "Fix regression #483", "workspaceId": "'$WS'",
  "requirements": {"roles": ["software-engineer"]}
}'

# Агент: claim -> start-run -> artifact -> succeed (задача завершается атомарно)
curl -s -X POST "$B/tasks/${TASK_ID}:start-run" -H "$AH" \
  -H "Content-Type: application/json" \
  -d "{\"claimId\":\"$CLAIM_ID\",\"fencingToken\":$TOKEN}"
curl -s -X POST "$B/runs/${RUN_ID}:succeed" -H "$AH" \
  -H "Content-Type: application/json" -d '{"output":{"pr":484}}'
```

## Известные ограничения MVP

- Delivery outbox-записей — структурированный лог (точка расширения для
  webhooks/брокера); после исчерпания retry запись dead-letter'ится
  (`available_at` в далёком будущем) с `last_error` для диагностики,
  автоматического re-drive нет.
- Rate limiting (429) не реализован.
- Heartbeat'ы сессий/claims не журналируются в `events` (это продление аренды,
  а не доменное изменение) — см. ADR-0003.
- Permissions делегирования хранятся, но не сужают права сессии on-behalf-of;
  права всегда определяются API-ключом действующего principal.
- PATCH задачи с живым чужим claim запрещён даже админу (снимите claim через
  `:release`); claim с мёртвой сессией задачу не блокирует.
- Идемпотентный replay создания API-ключа возвращает `key: null` — одноразовый
  секрет никогда не сохраняется на диске; потеряли ответ — выпустите новый ключ.
- Bootstrap не поддерживает `Idempotency-Key` (одноразовая операция, защищена
  advisory lock + `409 already_bootstrapped`).
- Один процесс WS-gateway = один LISTEN-коннект; горизонтальное масштабирование
  API-инстансов возможно, но каждый держит свой LISTEN.
- Выдача событий читателям задерживается до коммита самой долгой открытой
  пишущей транзакции (см. architecture.md).
- **Дефект replay v0.3 исправлен в v0.4:** доставка и курсор идут по паре
  `(tx_id, sequence)` под стабильным горизонтом — закоммиченное событие не
  может быть пропущено навсегда из-за порядка коммитов конкурентных
  транзакций. Бывший строгий xfail
  `test_event_prefix_is_complete_under_xid_inversion` — обычный passing
  regression. Публичный курсор — непрозрачный `ec1_...`; legacy
  `after=<sequence>` принимается в окне совместимости (см.
  docs/architecture.md).
- v0.2: workspace membership — организационные метаданные, не участвуют в
  eligibility; требования задач — строгий AND, без expression language; только
  `done` удовлетворяет зависимость (`cancelled` продолжает блокировать до
  удаления ребра).
- v0.3: `/work/available` пост-фильтрует eligibility по-задачно (страница
  может быть короче limit; на очень больших backlog'ах discovery — O(страниц));
  событийный WS не фильтрует по типам (клиент фильтрует сам); диапазоны версий
  skills (`^2`) не поддерживаются — только `name` и `name@version`; server-side
  enforcement бюджета run ограничен записью actions/checkpoints (стеночные
  часы исполнения — на совести harness); `control-plane login` хранит ключ в macOS
  Keychain или файле 0600 — иные OS secure-storage не интегрированы;
  `pendingApprovals` в контексте ограничен 50 записями (полный список —
  `GET /approvals`); discovery/claimability отвечают на вопрос
  «организационно доступна ли задача» и не проверяют наличие у ключа
  `tasks.claim` — claim остаётся авторитетным гейтом.
- v0.5: `GET /workspaces/tree` без `depth` на дереве в 10 000 узлов отдаёт
  документ ~2.6 МБ (серверная часть ~100 мс, остальное — передача и разбор на
  клиенте) — используйте `depth`; ужесточение `allowedChildTypes` или
  `fieldSchema` типа не переваливает существующее дерево в невалидное
  ретроактивно (правило проверяется только при мутациях, ADR-0029); provenance
  effective config даётся по верхнеуровневым ключам разделов, а не по каждому
  листу; удалить унаследованный вложенный ключ можно только переопределив
  объект-контейнер целиком (sentinel-удаления нет, ADR-0032); external
  reference нельзя удалить через API (только создать/обновить metadata,
  ADR-0034); `governance` задаётся только через config revisions, а не через
  `settings` профиля; Context Adapter остаётся одним процессом — per-tenant
  изоляция изолирует ОТКАЗЫ, а не даёт параллелизм (ADR-0036); архив журнала
  живёт в той же базе, внешнее объектное хранилище — следующий этап
  (ADR-0038); retention — операторская команда, планировщика нет; OpenCode
  adapter в CI проверяется контрактным стендом, живой `opencode serve` в образ
  не входит (ADR-0041).

## v0.3: Claude Code и harness-клиенты

```bash
control-plane init --server http://localhost:8000   # .control-plane/config.json
control-plane login --server http://localhost:8000  # ключ -> Keychain / файл 0600
claude mcp add control-plane -- control-plane-mcp   # MCP-сервер для Claude Code
control-plane work list                             # discovery из терминала
control-plane-agent                                 # reference-демон (CONTROL_PLANE_*)
```

Алиасы и legacy-локации прежнего кодового имени **удалены в v0.5**
(ADR-0040); при обнаружении устаревшей переменной окружения или
codename-каталога привязки проекта инструменты печатают явную ошибку
миграции — см. [docs/migration-v0.5.md](docs/migration-v0.5.md).

Протокол: [docs/harness-protocol.md](docs/harness-protocol.md). Интеграции:
[Codex](docs/codex.md), [Claude Code](docs/claude-code.md). Рабочая копия на
задачу и коммит как evidence:
[docs/execution-workspace.md](docs/execution-workspace.md). Задача с
`customFields.baseBranch` (ветка фичи, TAI-ADR-0047) получает копию от
`origin/<baseBranch>`, и в неё же указывает `targetBranch` артефакта `commit`;
ветки нет в forge — run падает с причиной, а не уходит от `main`.

## v0.4: Reliable Event Cursor, Context Memory, White-Label

Главная идея версии: **Control Plane хранит истину о работе; Context Memory
Engine хранит то, что было узнано в процессе; надёжный event stream
связывает настоящее с долговременной памятью.**

- **Надёжный replay** (ADR-0023/0024): доставка по `(tx_id, sequence)` под
  стабильным горизонтом — закоммиченное событие не может быть потеряно
  из-за порядка коммитов; публичный курсор — непрозрачный `ec1_...`
  (harness protocol v2), legacy integer принимается в окне совместимости.
- **Внешняя память** (ADR-0025/0026): опциональный Context Memory Engine за
  жёсткой HTTP-границей (`CP_CONTEXT_PROVIDER=none|http`); Context Adapter
  (`python -m control_plane.worker.context_adapter`) реплеит журнал в
  Observations с at-least-once доставкой и durable-курсором; выключенная
  память не влияет ни на координацию, ни на readiness.
- **Явный remember** (ADR-0027): `POST /observations` — replayable-событие
  `observation.recorded` с серверной provenance; в Claude Code —
  `cp_remember`.
- **Working context** (ADR-0028): `POST /context` = авторитетное текущее
  состояние + ContextPack памяти с явным `memoryStatus`, freshness-курсорами
  и trace id; в Claude Code — `cp_get_context`. Новая сессия без прошлого
  разговора продолжает работу: E2E `scripts/e2e_v04.py` доказывает
  continuity, переживание outage памяти и дедупликацию при crash адаптера.
- **White-label** (ADR-0022): ядро и официальные клиенты нейтральны;
  кодовое имя осталось только в deprecated-алиасах и legacy-fallback'ах.

## v0.5: Project Model, Reliability Backlog

Главная идея версии: **проектная и организационная иерархии — это одно и то же
дерево**, а всё «проектное» вычисляется из него, а не дублируется полем.

- **Workspace Types** (ADR-0029): tenant-scoped справочник типов узлов с
  `fieldSchema` (JSON Schema для custom fields) и `allowedChildTypes`; правило
  проверяется при create/move/retype под тем же per-tenant advisory lock.
  Системный тип `generic` заводится автоматически, поэтому дерево v0.4
  мигрирует без ручного ремонта.
- **Project Templates** (ADR-0030): версия шаблона неизменяема с момента
  записи (триггер БД); изменение — новая версия; проект всегда ссылается на
  точную версию.
- **Project Profile и lifecycle** (ADR-0031): `UNIQUE (workspace_id)` — весь
  сюжет конкурентного создания; пользовательские статусы отображаются в пять
  системных категорий (`planned`, `active`, `paused`, `terminal_success`,
  `terminal_cancelled`), и core принимает решения только по категории.
- **Versioned configuration** (ADR-0032): append-only ревизии; создание не
  активирует; активация — отдельная транзакционная команда с `If-Match`;
  effective config складывается из слоёв template → ancestors → revision →
  profile и возвращается вместе с provenance по каждому ключу.
- **Governance** (ADR-0033): фиксированный типизированный словарь с частичным
  порядком «строже»; потомок может только ужесточить, попытка ослабления
  отклоняется до commit; `:move` перепроверяет всё поддерево и отклоняется
  целиком.
- **External references** (ADR-0034): product-neutral mapping внешних
  идентификаторов, immutable identity, никакого dual-write.
- **Project scope** (ADR-0035): `projectId` в `/tasks` и `/work/available` с
  двумя семантиками (точный scope и `includeSubprojects`), фильтрация в
  PostgreSQL до пагинации; архивный проект перестаёт выдавать новую работу, не
  трогая живые claim и run.
- **Per-tenant доставка** (ADR-0036): poison-событие одного Tenant больше не
  останавливает остальных; курсоры, parked-состояние и backoff — на строку
  Tenant, round-robin по давности обслуживания.
- **Операторский redrive** (ADR-0037): `GET /operations/context-adapter`,
  `:redrive`, `:rebuild` — ни одна операция не может продвинуть курсор вперёд,
  то есть пропустить событие; каждая пишет audit-событие.
- **Retention журнала** (ADR-0038): `event_archive` + floor; `:archive`
  сохраняет replay прозрачным, `:prune` делает старый курсор
  `cursor_below_journal_floor` вместо тихого пропуска.
- **Сквозной `X-Run-Id`** (ADR-0039): валидируется, эхом возвращается,
  пишется в событие и outbox, уходит в Memory и в логи как `run_id`.
- **OpenCode adapter** (ADR-0041): `control-plane-opencode` — harness поверх
  подтверждённого HTTP-контракта `opencode serve`, continuity через Run
  Checkpoints.
- **Claude Code adapter** (ADR-0016 §1): `control_plane_claude` — адаптер
  reference-демона (`CONTROL_PLANE_AGENT_ADAPTER=claude-code`), запускает
  `claude -p` в рабочей копии задачи, continuity через Run Checkpoint
  `claude-code.session`, MCP пробрасывается внутрь с отобранными авторитетными
  командами. Промпт идёт stdin, transcript остаётся на runner, наружу — только
  summary. Подробности: [docs/claude-code-adapter.md](docs/claude-code-adapter.md).
- **Инструкции исполнителю** (CP-ADR-0066): версия типа задачи несёт
  `instructions` (Markdown ≤ 16 KiB, иммутабельно, проверка размера и
  вставленных секретов), проект — `settings.agentInstructions`. Ядро собирает их
  после своего контракта платформы в блок `instructions: {layers, hash}` контекста
  run и рабочего контекста и фиксирует хэш и версии слоёв в run и в `run.started`.
  Все три адаптера собирают prompt одним рендером
  (`control_plane_agent/instructions.py`): заметка харнесса, слои, соглашения
  агента — слой «Agent conventions»: `executor.instructions` ревизии агента или
  файл (`CONTROL_PLANE_CLAUDE_PROMPT_FILE`,
  `CONTROL_PLANE_CODEX_PROMPT_FILE`, `CONTROL_PLANE_OPENCODE_PROMPT_FILE`), задача,
  пакет памяти как данные. Сырой JSON `effectiveConfig` в prompt больше не
  вставляется. Подробности: [docs/api.md](docs/api.md#инструкции-исполнителю-cp-adr-0066).

E2E: `scripts/e2e_v05.py` (реальный Docker-стенд + реальный Memory Service),
бенчмарки: `scripts/bench_v05.py`, миграция:
[docs/migration-v0.5.md](docs/migration-v0.5.md), измеренные гарантии и
ограничения — [baseline v0.5](docs/reference/control-plane-v0.5-baseline.md).

## v0.6: Human Operator Harness Pilot

- Session возвращает server-derived `controlLevel`; human получает
  `human_operated`, agent/service — `connected`. Это observability, не право.
- Общий MCP adapter конфигурирует harness type/version/client name через
  environment и берёт client version из installed package metadata.
- SDK/MCP поддерживают create/list/optimistic update Tasks, relations и
  атомарное создание child + parent relation.
- `POST /runs/{runId}:handoff` под `Idempotency-Key` одной транзакцией пишет
  handoff checkpoint, suspend'ит Run, освобождает Claim, возвращает Task в
  `todo` и публикует события/outbox. Следующий harness создаёт новый Claim и
  Run из server context.
- Operator workflow и границы подтверждений зафиксированы в
  [ADR-0042](docs/adr/0042-human-operator-harness-handoff.md); обновление — в
  [migration-v0.6](docs/migration-v0.6.md).

## v0.7–v0.9: карта версий

Краткая карта версий после v0.6; полный реестр решений по версиям —
[docs/adr/](docs/adr/README.md).

| Версия | Что вошло | Решения | Обновление |
|---|---|---|---|
| **v0.7 — Agent Runtime Contracts** | Effective Harness Manifest как immutable evidence Run; Durable Active Turn Control как subresource Run (`run_control_messages`, capability `active_turn_control.v1`); Scoped Tool Discovery (`GET /tools`, `GET /tools/{ref}`, `cp_search_tools`/`cp_describe_tool`) и re-authorization при исполнении; Durable Child Run Handle (launch/list/resolve/revoke, capability `child_run_handle.v1`) | [ADR-0043](docs/adr/0043-effective-harness-manifest.md), [ADR-0044](docs/adr/0044-durable-active-turn-control.md), [ADR-0045](docs/adr/0045-scoped-tool-discovery.md), [ADR-0046](docs/adr/0046-durable-child-run-handle.md) | [docs/migration-v0.7.md](docs/migration-v0.7.md): ревизии `c91f3c7ad8e2`, `9c41ee0d7b52`, merge `d4e6f8a1b2c3`, `e7c2a95d41b8`, head `a7f2c4d19b60` |
| **v0.8 — модель work item** | реестр `task_types`, статус как пара «ключ + категория» и настраиваемый lifecycle; custom fields, плановые даты и расширенная выборка (`/tasks` с фильтрами и `sort=dueDate\|startDate`); комментарии к work item с append-only историей правок; реестр entity bindings для external references | [ADR-0047](docs/adr/0047-generic-external-references.md), [ADR-0048](docs/adr/0048-work-item-type-and-lifecycle.md), [ADR-0049](docs/adr/0049-work-item-fields-dates-filters.md), [ADR-0050](docs/adr/0050-work-item-comments.md) | [docs/migration-v0.8.md](docs/migration-v0.8.md): `c8a51d70b394` → `a1c7e94b2f60` → `b8d3f1a45c72` |
| **v0.9 — наблюдаемость исполнения** | execution trace: транскрипт прогона как bounded артефакт `transcript` и run action `tool.<имя>` на каждый вызов инструмента; автоматическое ревью кода — задачу ревьюеру заводит демон runner'а, вердикт из summary попадает в поля задачи | [ADR-0051](docs/adr/0051-execution-trace-transcript-artifact.md), [ADR-0052](docs/adr/0052-auto-review-by-runner-daemon.md) | схема не менялась, миграции нет |

Между v0.7 и v0.8 лежит ревизия `f5b91c3e7a24` (`iam_principal_bindings`,
IAM enforcement — раздел ниже). Дальше: v0.10 Identity —
[ADR-0053](docs/adr/0053-iam-identity-source-and-binding-api.md), ревизия
`c2d8e4f6a1b3` (статус binding `active|disabled|revoked`, раздел «Управление
bindings» ниже); v0.11 память и авторизация —
[ADR-0054](docs/adr/0054-governed-graph-memory.md) и
[ADR-0055](docs/adr/0055-policy-authorize-and-shadow-mode.md), ревизия
`a9c4e2d7f1b3` (`iam_actor_id` в журнале событий).

Автоматическое ревью (v0.9) переходит от демона к типу задачи: версия типа может
объявить работу после завершения — `completionSchema` (ревизия `a4c7e2f9b1d3`,
[ADR-0061, амендмент 2026-09-25](docs/adr/0061-approval-outcomes-declared-by-task-type.md)).
Ядро заводит её при завершении задачи кем угодно — раннером, человеком, исходом
approval — с полномочиями завершившего: например, задачу `code-review` с
`customFields` (ветка, коммит) и gate-approval ревьюеру, если у задачи есть
опубликованный артефакт `commit`. Если тип так объявляет, демон runner'а ревью не
заводит; для типов без раздела — прежнее поведение ADR-0052.

## IAM enforcement (IAM-7)

Control Plane остаётся resource server: он проверяет токен, выпущенный
`iam-service`, но сам credentials не выпускает и лицензий не хранит. Порядок
решения один и тот же для HTTP, WebSocket и фоновых вызовов:

```
IAM identity → local revocation → entitlement → domain policy → transactional gates
```

Общий Policy Enforcement Point живёт в отдельном product-neutral пакете
`platform-auth-sdk`; здесь описано только то, что специфично для продукта.

### Откуда берутся права

В токене IAM доменных permissions нет и быть не должно: право «создать Task»
принадлежит продукту, а не identity provider. Внешняя identity сопоставляется с
локальным Principal через таблицу `iam_principal_bindings`, и права берутся
оттуда. Пара `(issuer, iam_principal_id)` уникальна — одна upstream identity не
получает двух локальных Principal, — а `iam_tenant_id` хранится рядом, чтобы
токен чужого tenant не сработал по совпадению subject.

Scope предъявленного токена работает потолком: `control-plane:read`,
`control-plane:write`, `control-plane:admin`. Binding может разрешать запись, но
токен, выданный только на чтение, писать не даст. Пересечение сужает, никогда не
расширяет; admin-scope ничего не добавляет к binding — он лишь не сужает его.

Четвёртый scope, `control-plane:decide`, — токен одного решения из канала
(Telegram и т. п., CP-ADR-0070). `purpose_ref=approval:<id>` называет approval.
Такой токен может только `POST /approvals/{id}:approve|:reject` этого approval и
только с `Idempotency-Key`. На любой другой запрос он получает `403 outside_purpose`,
а токен без `purpose_ref` — `403 purpose_ref_required`. Из прав binding у него
остаётся только `approvals.decide`. `acr=channel:<имя>` попадает в поле `channel`
события `approval.approved|rejected`.

Строка `status`/`revoked_at` в binding — локальная revocation policy: она
закрывает вход немедленно, не дожидаясь истечения уже выданного access token.
Статусы: `active`, `disabled` (операторский переключатель) и `revoked`
(отозван через API); любой не-`active` закрывает вход.

### Управление bindings (ADR-0053)

IAM — источник identity; API-ключ `cp_` остаётся только credential bootstrap
и аварийным входом при недоступности IAM. Bindings заводятся и отзываются
API, а не SQL.

**Первый binding — при bootstrap.** Необязательное поле `iamBinding` создаёт
строку для admin principal в той же транзакции, что tenant и ключ, — на
IAM-only стенде администратор входит без единой строки SQL. Права — все
permissions по имени (scope первого токена работает потолком, и голый `admin`
под `read+write` сузился бы до пустого множества):

```json
POST /api/v1/bootstrap            Authorization: Bearer <CP_BOOTSTRAP_TOKEN>
{
  "tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin",
  "iamBinding": {
    "issuer": "https://iam.example/iam",
    "iamTenantId": "00000000-…", "iamPrincipalId": "11111111-…"
  }
}
→ 201 { "tenant": …, "adminPrincipal": …, "apiKey": …,
        "iamBinding": { "id": "…", "issuer": "…", "iamPrincipalId": "…",
                        "permissions": [ "admin", "approvals.decide", … ],
                        "status": "active", … } }
```

**Дальше — API principal'ов** (права те же, что у API-ключей):

| Маршрут | Право | Семантика |
|---|---|---|
| `GET /api/v1/principals/{id}/iam-bindings` | `principals.read` | все identity principal'а, включая отозванные |
| `POST /api/v1/principals/{id}/iam-bindings` | `principals.write` | upsert по `(issuer, iamPrincipalId)`: `201` создан, `200` обновлён (перенаправлен, права заменены, статус снова `active`) |
| `POST /api/v1/iam-bindings/{id}:revoke` | `principals.write` | `status=revoked`; идемпотентно |

```json
POST /api/v1/principals/{id}/iam-bindings
{
  "issuer": "https://iam.example/iam",
  "iamTenantId": "00000000-…", "iamPrincipalId": "22222222-…",
  "permissions": ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]
}
```

Правила upsert повторяют выпуск ключа: неизвестное право и `admin` от
не-admin — `422 invalid_permissions`; права сверх собственных — `403
permission_escalation` (`details.missing`); не-active principal — `422
principal_not_active`. Сверх этого principal вида `agent`/`service` не
получает `admin` и `approvals.decide` — `422 permissions_not_allowed_for_kind`.
Identity, привязанная в другом tenant, — `409 iam_identity_bound_elsewhere`.
Issuer, отличный от `CP_IAM_ISSUER` (если он задан), — `422 iam_issuer_untrusted`.
Перенос identity с другого principal — только `admin` (`403
permission_escalation`, `details.previousOwnerKind`). Principal действующего
агента реестра через этот маршрут identity не получает — `409
agent_identity_conflict` с `details.route` `/agents/{key}/identity`; повторно
привязать identity, уже записанную в реестре, может только `admin` (CP-ADR-0073,
амендмент 2026-09-30, И4).

События журнала: `iam_binding.created` / `updated` / `revoked` с
`principalId`, `issuer`, `iamPrincipalId` и правами. После коммита роутер
сбрасывает кэш enforcement для этой identity (`BindingDirectory.invalidate`),
так что новый или отозванный binding действует со следующего запроса — в том
числе поверх отрицательного ответа, закэшированного до появления строки.

SDK: `list_iam_bindings`, `upsert_iam_binding`, `revoke_iam_binding`.
Конформанс — `tests/integration/test_iam_bindings.py`.

### Compatibility window

Пока `CP_LEGACY_API_KEYS_ENABLED=true`, прежний ключ `cp_<prefix>_<secret>`
остаётся рабочим credential. Вид предъявленного значения определяется по форме,
а не перебором способов: перебор сообщал бы кодом ответа, какой из них подошёл.
Выключение переводит сервис в режим IAM-only.

### Аварийный ключ (ADR-0065)

Пока IAM недоступен, владелец хоста выпускает из shell на хосте короткоживущий
admin-ключ для активного человека; API для этого нет:

```bash
docker compose exec control-plane-api python -m control_plane.break_glass \
  issue --principal <uuid> --ttl 3600 --reason "IAM недоступен"
docker compose exec control-plane-api python -m control_plane.break_glass revoke
```

Ключ (`cp_bg…`) принимается и при закрытом окне совместимости, живёт не дольше
`CP_BREAK_GLASS_MAX_TTL_SECONDS` (4 ч), выпуск пишется в журнал событием
`api_key.break_glass_issued` с причиной. `revoke` закрывает все живые аварийные ключи,
когда IAM поднят. `CP_BREAK_GLASS_ENABLED=false` выключает этот путь.

### Отказы

| Ситуация | Ответ |
|---|---|
| Любой дефект токена, неизвестный или отозванный binding | `401 invalid_credentials` |
| Токен без scope Control Plane | `403 insufficient_scope` |
| Identity без лицензии | `403 not_entitled` |
| Лицензия без доменного права | `403 permission_denied` |
| JWKS или entitlement недоступны дольше окна | `503` |

Все дефекты токена схлопываются в один ответ, а неизвестный binding отвечает
так же, как отозванный: разные коды превратили бы endpoint в справочник по
чужим credential. Точная причина уходит в journal решения (`control_plane.authz`)
с correlation id и без секретов. Для WebSocket те же исходы отдаются close-кодами
`4401`/`4403`/`4503`.

Недоступность внешнего решения — это `503`, а не allow: право не проверено, а не
подтверждено.

### Конфигурация

```bash
CP_IAM_ENABLED=true
CP_IAM_ISSUER=https://iam.example
CP_IAM_JWKS_URL=https://iam.example/.well-known/jwks.json
CP_IAM_AUDIENCE=control-plane
CP_LEGACY_API_KEYS_ENABLED=true          # окно совместимости
CP_BREAK_GLASS_ENABLED=true              # ADR-0065: аварийный ключ из shell хоста

CP_ENTITLEMENT_ENABLED=true              # требует собственной service identity
CP_ENTITLEMENT_BASE_URL=https://entitlement.example
CP_IAM_BASE_URL=https://iam.example
CP_IAM_CLIENT_ID=control-plane
CP_IAM_CLIENT_SECRET=...                 # только окружением
```

Включённый `CP_IAM_ENABLED` без issuer или JWKS роняет запуск: сервис, который
«почти» перешёл на IAM, хуже обоих состояний. Выключенный entitlement виден в
журнале решений источником `disabled`, а не отсутствием записи.

Конформанс-матрица — `tests/integration/test_iam_enforcement.py`.

### Credential локального harness

Codex, Claude Code и CLI берут credential через `resolve_credential`: если
объявлен `CONTROL_PLANE_IAM_URL`, используется identity IAM, иначе — прежний
ключ `cp_`. Настроенный наполовину IAM (URL без tenant) закрывает вход ошибкой,
а не откатывается на старый ключ незаметно.

Platform Access Token предъявляется **только** IAM и только телом запроса;
Control Plane получает короткоживущий access token своего audience. Токен живёт
минуты, а сессия harness — часы, поэтому credential не разрешается один раз при
старте: он обменивается заново перед истечением и ещё раз, если сервер ответил
`401`. Повтор идёт с тем же `Idempotency-Key` — переотправленный запрос обязан
остаться той же бизнес-командой, — а второй `401` считается отказом, а не
поводом продолжать попытки.

```bash
iam auth login                              # PAT попадает в credential store
CONTROL_PLANE_IAM_URL=https://iam.example
CONTROL_PLANE_IAM_TENANT=<tenant-uuid>
CONTROL_PLANE_IAM_AUDIENCE=control-plane    # по умолчанию
CONTROL_PLANE_IAM_SCOPES="control-plane:read control-plane:write"
```

Сам PAT в окружение не попадает: клиент читает его из того же хранилища, что и
`iam auth` (объявленный CI-режим, Keychain, файл `0600`). Файл с расширенными
правами не читается вовсе — это инцидент, а не неточность конфигурации.
