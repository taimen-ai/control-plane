# Harness Protocol (`control-harness/2`)

Harness Protocol — семантический контракт между Control Plane и любой
исполняющей средой (harness): Codex, Claude Code, CLI, IDE-расширение, autonomous
agent daemon, CI-worker, сторонние клиенты. Это **не** новый wire-формат:
транспорт — существующий REST API + событийный журнал (поллинг `GET
/api/v1/events` и/или WebSocket `/api/v1/events/ws`). Документ самодостаточен:
по нему можно написать новый harness, не читая исходники сервера.

Человеческий и автономный harness используют **один и тот же протокол**;
различие — в политике клиента (человек явно выбирает задачу, агент выбирает
сам), а не в серверных путях.

## 1. Роли и доверие

- Control Plane — авторитетное ядро координации. Каждая команда перепроверяет
  authorization, организационную eligibility, readiness, аренды и fencing
  в своей транзакции.
- Harness — **недоверенный** распределённый клиент. Никакие его утверждения
  («я всё ещё владею задачей», «пользователь подтвердил») не принимаются на
  веру. `harness.type` никогда не является security boundary.
- Идентичность даёт только API-ключ (`Authorization: Bearer cp_...`). Ключ
  привязан к principal и несёт permissions.

## 2. Регистрация harness = открытие сессии

Harness — это метаданные живой сессии (ADR-0015), отдельной сущности нет.

```http
POST /api/v1/sessions
{
  "clientName": "claude-code",
  "clientVersion": "2.1.0",
  "harness": {
    "type": "claude-code",
    "version": "2.1.0",
    "protocolVersion": "1",
    "capabilities": ["tasks.interactive", "artifacts.publish",
                     "resume", "checkpoints", "skills.protocol.mcp"],
    "environment": {"repository": "github:acme/x"}
  }
}
```

Ответ Session содержит серверное `controlLevel`: `human_operated` для human
Principal и `connected` для agent/service. Клиент не передаёт и не выбирает
это поле; unknown input отклоняется. `controlLevel` — только observability, не
permission и не trust signal. Legacy rows после миграции v0.6 имеют
`connected`.

Negotiation: сервер поддерживает версии из `GET /harness/context →
protocol.supportedVersions` (сейчас `["1", "2"]`). Неподдерживаемая версия →
`422 unsupported_protocol_version` (в `details.supported` — список). Блок
`harness` необязателен: до-v0.3 клиенты работают без изменений.

**Внимание (v0.4, breaking для v1):** сервер принимает открытие сессий с
`protocolVersion: "1"`, но ответы всегда в формате v2 — `eventCursor` стал
непрозрачной СТРОКОЙ. Клиенты v0.3, выполнявшие арифметику по
`eventCursor` (например, `events tail` старого CLI), падают на первом
обращении — им требуется обновление. Новые harness'ы обязаны отправлять
`"2"`; `"1"` принимается только на окно совместимости (ADR-0024).

Protocol capabilities (не путать с организационными `PrincipalCapability`) —
что умеет клиент:

| capability | значение |
|---|---|
| `events.realtime` | потребляет WebSocket-поток событий |
| `tasks.interactive` | задачи выбирает человек (no auto-claim) |
| `artifacts.publish` | умеет регистрировать артефакты |
| `approvals.interactive` | показывает approvals человеку |
| `resume` | сохраняет курсоры/state, умеет resume после рестарта |
| `checkpoints` | пишет run checkpoints |
| `active_turn_control.v1` | читает и подтверждает durable Run control messages |
| `child_run_handle.v1` | запускает дочерние Run и восстанавливает их после restart |
| `skills.protocol.<p>` | исполняет skills протокола `<p>` (mcp/http/local/opencode/custom) |

Неизвестные capabilities сервер молча отбрасывает (forward compatibility);
заявленные `skills.protocol.*` влияют на разметку `executable` в skill
resolution (§9).

## 3. Bootstrap: `GET /harness/context`

Один запрос отвечает на «кто я / где я / что я делаю / что мне доступно /
что произошло». Требует только аутентификации (данные — о самом principal).

```jsonc
{
  "protocol": {"name": "control-harness", "supportedVersions": ["1", "2"]},
  "tenant": {...}, "principal": {...},
  "session": {...} | null,          // при ?sessionId=; содержит controlLevel
  "activeSessions": [...],
  "activeClaims":  [{"id", "taskId", "taskPublicId", "fencingToken", "expiresAt", ...}],
  "activeRuns":    [...], "suspendedRuns": [...],
  "roles": [...], "capabilities": [...], "skills": [...],
  "pendingApprovals": [...],        // адресованные мне (напрямую или через роль)
  "eventCursor": "ec1_...",         // opaque-курсор: стабильная точка подписки
  "permissions": [...]
}
```

`eventCursor` — непрозрачная строка (`ec1_...`), вычисленная тем же
правилом, что у читателей журнала (наибольшая стабильная позиция
`(tx_id, sequence)` под горизонтом `pg_snapshot_xmin`). Подписка с него
гарантированно получает всё, что закоммитится после: незавершённые
транзакции сортируются строго позже любой выданной позиции, поэтому
закоммиченное событие не может быть пропущено навсегда (v0.4; в v0.3
целочисленный sequence-курсор такой гарантии не давал). Курсор нельзя
разбирать, сравнивать или конструировать на клиенте — только сохранять и
возвращать серверу (`GET /events?cursor=...`, `WS ?after=...`). Protocol v2
отличается от v1 ровно этим: курсоры непрозрачны, страница `/events` всегда
несёт `nextCursor` + `hasMore`, каждое событие — своё поле `cursor`; v1
(integer `after`) принимается в окне совместимости и адаптируется сервером
(возможна повторная выдача уже виденного — at-least-once). Harness'ам,
для которых актуальность критична, следует дополнительно
сверяться с авторитетным состоянием (`/harness/context`, `GET /runs/{id}`)
в ключевых точках, а не полагаться только на поток событий.

`pendingApprovals` ограничен 50 записями и содержит только approvals,
которые вызывающий действительно может решить: адресованные лично либо через
роль, удерживаемую в подходящем scope (tenant-wide или предок-или-сам
workspace approval'а).

## 4. Discovery работы

```http
GET /api/v1/work/available?workspaceId=...&includeDescendants=true&limit=20
```

Возвращает задачи, которые вызывающий principal **мог бы** захватить сейчас:
статус не терминальный, нет живого claim, зависимости выполнены, нет pending
gate-approval, организационные требования удовлетворены. Сортировка
стабильная: (priority: critical→low, created_at, id). Страница может быть
**короче** limit при непустом `nextCursor` (пост-фильтрация eligibility) —
листайте до `nextCursor: null`.

**Discovery — advisory.** Единственный авторитетный gate — claim: между
показом и захватом мир меняется, claim перепроверяет всё в своей транзакции.
Диагностика по конкретной задаче: `GET /tasks/{ref}/claimability` →
`{claimable, reasons: [{code: task_already_claimed|task_not_ready|
approval_required|not_eligible|task_not_claimable, ...}]}`.

Фильтр по поддереву workspace работает и на `GET /tasks`
(`workspaceId` + `includeDescendants=true`).

**Project scope (v0.5).** К обоим endpoint'ам добавлены `projectId` и
`includeSubprojects`:

| Параметр | Что попадает в выборку |
|---|---|
| `projectId=P` | Workspace проекта P и его обычные потомки; поддеревья вложенных проектов исключены |
| `projectId=P&includeSubprojects=true` | всё поддерево Workspace проекта P |

Принадлежность задачи проекту не хранится колонкой — сервер выводит её из
дерева Workspace, и `TaskOut` несёт вычисленный `projectId`. Фильтр
применяется в PostgreSQL до пагинации, поэтому страницы не пропускают и не
дублируют записи. Архивированный проект перестаёт выдавать **новую** работу;
уже взятый claim и запущенный run это не затрагивает.

## 5. Цикл исполнения

```
claim -> start-run -> (checkpoints / actions / artifacts)* -> succeed | fail | suspend | cancel
```

1. `POST /tasks/{ref}:claim {sessionId}` → claim c `fencingToken`
   (= новый `claim_epoch` задачи) и арендой `expiresAt`.
2. `POST /tasks/{ref}:start-run {claimId, fencingToken, maxDurationSeconds?,
   maxActions?}` → run, пришпиленный к эпохе claim.
3. `GET /runs/{id}/context` → операционный контекст: task, workspace, claim,
   requirements, relations, последние артефакты, pending approvals,
   **checkpoints всех прошлых runs задачи**, исполнимые skills, eventCursor.
   Это operational context, не память и не chat history.
4. Работа: `POST /runs/{id}/checkpoints {kind, data}` (durable operational
   state), `POST /runs/{id}/actions {action, skill?, externalReference?}`
   (audit trail, см. §10), `POST /artifacts {...}` (см. §8).
5. Финал: `POST /runs/{id}:succeed {output?, completeTask=true}` — атомарно
   run→succeeded + claim released + task done. `:fail` — честная запись
   неудачи (задачу не трогает, работает даже после потери аренды).
   `:suspend` — см. §7. `:cancel` — терминальная отмена.

Перед каждой авторитетной записью сервер проверяет: живость claim (аренда +
живая сессия), совпадение `fencingToken == claim_epoch`, владельца. Любое
несоответствие → `409 stale_claim`. **Получив `stale_claim`, harness обязан
прекратить авторитетные записи** и пересобрать контекст.

### Конфигурация исполнителя

Манифесты харнесса (ADR-0043, v0.7) удалены: конфигурация исполнителя — ревизия
агента в реестре ([ADR-0073](adr/0073-agent-registry.md)), прогон называет её
в `agentRevisionId` при `start-run`. Маршрутов `/runs/{id}/harness-manifest*`
больше нет.

## 6. Heartbeats

Session и claim — аренды. Рекомендованный cadence: `ttl / 3` (дефолтные TTL —
300 s ⇒ heartbeat каждые ~100 s; SDK `HeartbeatRunner` использует 60 s).

- `POST /sessions/{id}:heartbeat`, `POST /claims/{id}:heartbeat`.
- Heartbeat не является доменным событием и не журналируется.
- Ошибка heartbeat **не должна скрываться**: `409 session_expired /
  claim_expired / session_not_active` означает потерю аренды — прекратить
  авторитетные записи, уведомить человека (в human harness), пересобрать
  контекст. Корректность системы не зависит от heartbeat'ов: просроченная
  аренда будет пожата следующим claim или worker'ом.
- Отличайте **транспортный** сбой heartbeat от **доменного**: сетевой сбой и
  ответ прокси 502/503/504 (или 5xx без конверта ошибки, код `http_error`) —
  так выглядит перезапуск ядра — не доказывают потерю владения. Их повторяют
  через короткую паузу, а не через полный интервал: SDK `HeartbeatRunner`
  повторяет через `retry_seconds` (10 s). Терпение — не число попыток, а
  время: аренда считается потерянной, только когда с последнего успешного
  heartbeat (или со старта) прошло `outage_budget_seconds` — по умолчанию
  `interval × max_transport_failures`, 180 s при интервале 60 s, с запасом
  внутри TTL 300 s. Частота повторов на этот срок не влияет. Доменную ошибку
  (`409`, `404` claim, `403`) трактуют как потерю немедленно.

## 7. Ожидание approval: gate + suspend

Минимальный approval-gate без workflow-движка (ADR-0018):

- `POST /approvals {task, gate: true, requiredRoleId|assignedPrincipalId,
  artifactId?}` — пока такой approval `pending`, задачу нельзя ни claim'ить,
  ни завершать: `409 approval_required`. Любое решение (approve/reject/cancel)
  открывает gate. Gate на терминальной задаче (`done`/`cancelled`) отклоняется
  (`422 invalid_approval`) — он был бы инертен.
- **Отмена gate требует тех же полномочий, что и решение**: `:cancel` для
  gate-approval разрешён его автору либо принципалу, который eligible решить
  его (assigned principal / держатель требуемой роли в scope). Иначе
  `403 not_eligible` — иначе принципал, которого gate удерживает, мог бы
  снять его сам, имея лишь `approvals.manage`. Для не-gate approvals
  семантика v0.2 не изменилась.
- `POST /runs/{id}:suspend {reason, waitingForApprovalId?}` — атомарно:
  run → `suspended` (терминально для этого run), claim released, task → todo
  (но удерживается gate'ом). Эксклюзивная аренда **не** паркуется на часы
  ожидания.
- Продолжение — новый claim (новый fencing token) + новый run, который читает
  checkpoints прошлых попыток из Run Context. Suspended run остаётся
  audit-записью.
- Semantics reject: gate открывается, задача остаётся actionable; дальнейшее
  решение — за исполнителем/оркестрацией, сервер ничего не додумывает.

Рекомендованный паттерн: `checkpoint → request approval(gate) → suspend` —
затем следить за `approval.approved|rejected` в событиях.

### Human harness handoff (v0.6)

После явного решения человека текущий harness вызывает:

```http
POST /api/v1/runs/{runId}:handoff
Idempotency-Key: <stable-key-for-this-decision>
{
  "reason": "human_harness_handoff",
  "checkpoint": {
    "kind": "handoff",
    "data": {
      "summary": "Проверяемый результат без transcript",
      "nextSteps": ["Создать новый Claim и Run"],
      "evidenceRefs": ["commit:abc123"]
    }
  }
}
```

Одна транзакция проверяет permission, tenant, owner, живой Claim и fencing;
создаёт следующий checkpoint; переводит Run в `suspended`; освобождает Claim;
возвращает Task в `todo`; пишет `run.checkpointed`, `run.suspended`,
`claim.released`, `run.handoff_prepared` и outbox. Ответ содержит `run`, `task`,
`checkpoint`, `eventCursor` и resume hint. Повтор после ambiguous response с
тем же idempotency key возвращает сохранённый ответ без нового checkpoint и
без изменения fencing epoch.

Второй harness обязан создать новый Session/Claim/Run и прочитать Run Context.
Старый Run не возобновляется, process-local state и transcript не переносятся.
Summary/evidence отклоняются, если содержат credential-like значения или
полные machine-local filesystem paths.

## 8. Артефакты

Артефакт — лёгкая append-only ссылка на результат: `{type, name, uri?,
content?, metadata, task?, runId?, supersedesArtifactId?}`. Крупные данные
живут снаружи за `uri`; Control Plane — не файловое хранилище. `file://`-URI
имеет смысл только в породившем его окружении и не является переносимым
хранилищем. Ревизии: новая запись с `supersedesArtifactId` — цепочка
`draft v1 → draft v2 → final` (ADR-0020); старые записи неизменны.

Транскрипт автономного прогона — тоже артефакт (ADR-0051): `type =
transcript`, `content` по схеме `agent-transcript/1`, не больше 512 КиБ,
отредактированный от путей хоста и credential'ов, без скрытых рассуждений
модели и без промпта. Адаптер, который не может опубликовать документ
безопасно, публикует его withheld (только счётчики) — прогон из-за
транскрипта не падает.

## 9. Skills

Registry знает name/version/protocol/schemas/status. Требования задач: `name`
(любая не-disabled версия, дефолт-резолюция: свежайшая active) или
`name@version` (точная версия). Disabled-версия никогда не участвует в
resolution и не удовлетворяет требования. Исполняет skills harness;
Control Plane координирует доступность и аудит: `availableSkills` в
context/Run Context размечает `executable` по заявленным
`skills.protocol.*` capabilities сессии (сессия без заявленных протоколов
видит всё и фильтрует сама).

## 9.0.1. Scoped Tool Discovery (v0.7)

`availableSkills` в context отдаёт всё назначенное целиком и не масштабируется
на большой MCP/plugin catalog. Для него есть bounded projection:

```
GET /api/v1/tools?query=deploy&runId=<run>&limit=25&cursor=<opaque>
GET /api/v1/tools/{uuid|name|name@version}?runId=<run>
```

Три слоя различаются намеренно: **Capability Catalog** («что runtime
технически умеет»), **Effective Tool Policy** («что разрешено этому Principal,
Run и workspace сейчас») и **Tool Discovery View** — bounded проекция их
пересечения. Поиск отдаёт summary, describe — полную санитизированную
`inputSchema` (без `config`, `default`, `examples` и vendor extensions;
удалённые пути перечислены в `schemaRedactions`).

Правила, на которые можно рассчитывать:

- инструмент вне policy нельзя ни найти, ни описать: describe отвечает
  `404 tool_not_found` так же, как на несуществующее имя;
- назначенный инструмент, чей протокол сессия не объявила, виден с
  `visible: false` и `reason: protocol_not_supported_by_harness` — это
  диагностика конфигурации harness, а не запрет;
- `view.catalogRevision`, `view.policyRevision` и `view.viewHash` описывают,
  из чего собрана страница; `viewHash` отдаётся как `ETag`, а
  `If-None-Match` даёт `304`, пока обе ревизии неизменны;
- пустой `query` — eager-режим: та же выборка, первая страница;
- **discovery не даёт прав**. Перед записью action сервер пересчитывает
  policy заново, поэтому отозванное между `describe` и вызовом назначение
  даёт `403 tool_not_authorized` (см. §10).

MCP: `cp_search_tools`, `cp_describe_tool`.

## 9.1. Durable Active Turn Control

Простой Stop не выражает, какое именно исполнение нужно прервать. Harness с
capability `active_turn_control.v1` использует durable Run subresource:

```http
POST /api/v1/runs/{runId}/control-messages
Idempotency-Key: <stable-key>
{
  "operation": "steer",
  "causalPosition": "turn:17/tool-batch:2",
  "directive": "Сначала проверь миграционный roundtrip",
  "expectedRunVersion": 4
}
```

Операции различаются:

- `queue` — новое намерение после terminal boundary текущего turn;
- `steer` — correction на ближайшей safe boundary, без отмены action;
- `redirect` — отменить только model inference; во время tool execution
  harness применяет его как steer;
- `request_cancel` — кооперативная остановка; новые actions запрещены только
  после `applied` acknowledgement;
- `force_cancel` — permission `claims.manage`; сервер сразу переводит Run в
  `cancelled`, освобождает Claim и отменяет активных `spawned_by` descendants.

Harness читает сообщения по `GET /runs/{id}/control-messages` с непрозрачным
Run-bound курсором `rc1_...`. `GET /runs/{id}/context` дополнительно несёт
`pendingControlMessages`, поэтому restart между accepted/applied не теряет
intent.

Применение подтверждается `POST
/runs/{id}/control-messages/{messageId}:acknowledge` с `claimId`,
`fencingToken`, `expectedRunVersion`, `expectedMessageVersion` и
`safeBoundary`. Только holder живого Claim может подтверждать; сообщения
разрешаются по seq. Получив `stale_claim`, harness прекращает authoritative
writes и перечитывает context.

## 9.2. Durable Child Run Handle

Harness с capability `child_run_handle.v1` делегирует работу дочернему Run и
переживает собственный restart:

```http
POST /api/v1/runs/{runId}/child-handles
Idempotency-Key: <request-key>
{
  "correlationId": "review:migration-roundtrip",
  "title": "Проверить migration roundtrip",
  "grant": {"permissions": ["tasks.read", "tasks.claim"]},
  "cancellationPolicy": "cascade_cooperative"
}
```

Правила, на которые harness может опираться:

- **идемпотентность launch** — повтор с тем же `correlationId` возвращает `200`
  и тот же child, даже если `Idempotency-Key` другой. Именно это делает
  безопасным retry после ambiguous response и после restart;
- **handleToken отдаётся один раз** и является locator'ом, а не credential:
  каждое обращение всё равно проверяет ключ, tenant и permission. Потеря
  token'а ничего не стоит — всё работает и по `handleId`;
- **grant сужает, но не расширяет.** Запрос сверх потолка родителя — `422
  child_grant_exceeds_parent`. Дочерний Run обязан иметь `tasks.claim` в
  grant, иначе `:start-run` отклоняется;
- **reconnect** — `GET /runs/{id}/context` несёт `childHandles`; ни token, ни
  transcript для восстановления не нужны;
- **status выводится** из дочерних Task/Run. Отозванный handle с живым
  ребёнком показывает `running`, а не `revoked`: `revokedAt` и `expiresAt` —
  отдельные поля;
- **результат bounded и immutable.** `output` дочернего `:succeed` может нести
  `summary`, `data` и `artifactRefs`; превышение границ — `422
  child_result_too_large`, объём выносится в Artifact. Перезаписать
  записанный результат нельзя, в том числе повторной попыткой;
- **cancellation policy** — `cascade_cooperative` (по умолчанию) передаёт
  applied `request_cancel` родителя активным детям; `detach` — нет.
  `force_cancel` каскадирует всегда.

Directive — bounded intentional operational input, не transcript. Credential-
like payload и абсолютные локальные paths отклоняются; directive/reason не
копируются в events/outbox. Legacy `:request-cancel` остаётся wrapper'ом и
материализует typed `request_cancel` message.

## 10. Run actions и бюджеты

`POST /runs/{id}/actions` — запись «внешнего действия» (tool invocation,
skill use): `{action, status: started|completed|failed, skill?,
externalReference?, metadata}` + двухфазный `:finish` для длинных операций.
Обе записи (и старт, и `:finish`) проходят полный claim-gate: потеряв аренду,
процесс не может дописывать исход в аудит чужой уже работы — незавершённое
`started`-действие честно остаётся незавершённым.
Это execution audit, отдельный от доменного журнала (ADR-0019): без секретов,
без chain-of-thought, без полных payload'ов. Автономный адаптер пишет одно
действие на вызов инструмента — `action = tool.<имя>`, `externalReference =
<harness>:session/<id>#call/<n>`, `metadata = {tool, call, summary}` — и
закрывает его `:finish` по результату; вход и выход живут в артефакте
`transcript`, на который action ссылается ординалом `call` (ADR-0051).

`skill` (v0.7) проходит **повторную авторизацию**: сервер заново вычисляет
effective tool policy и отклоняет назначение, отозванное или запрещённое
governance, с `403 tool_not_authorized`. Отказ ничего не коммитит — `seq` не
растёт, бюджет цел. Если протокол skill не объявлен сессией, действие всё
равно записывается: заявленные capabilities — это утверждение клиента о себе,
и оно не может ни расширять, ни сужать его полномочия; несовпадение
фиксируется в `metadata.capabilityMismatch`.

Бюджеты (`maxActions`, `maxDurationSeconds` на run): harness обязан уважать
их; сервер enforce'ит то, что может — запись действия/checkpoint сверх
бюджета → `409 budget_exceeded`. Финализация (`:succeed`/`:fail`) бюджетом
не блокируется.

## 11. События

```
persist last processed sequence
GET /events?after=N (или WS /events/ws?after=N)
catch up -> consume -> reconnect -> catch up again
```

Журнал в PostgreSQL — source of truth; WebSocket — лишь сигнал «проснись».
Выдаваемый префикс всегда полон (см. architecture.md, tx_id horizon), поэтому
единственный курсор — последний обработанный `sequence`. SDK
`follow_events(after=...)` реализует паттерн поверх поллинга.

События, которые human harness должен показывать пользователю:
`approval.requested|approved|rejected`, `task.claimed` (кем-то другим),
`claim.released|expired` (потеря владения), `run.cancel_requested`,
`run.suspended`, `artifact.created`, `task.completed`.

Манифест добавляет `run.manifest_compiled` и `run.manifest_ephemeral_recorded`: в payload только ссылки и хеши, никогда
содержимое манифеста.

## 12. Resume / reconnect protocol

Continuity не зависит от памяти процесса. После рестарта:

1. Определить identity: API-ключ из credential store (§14).
2. `GET /harness/context`.
3. Ветвление:
   - **claim жив** (`activeClaims` содержит задачу): продолжить — heartbeat,
     существующий `activeRuns[*]` можно вести дальше (fencing тот же);
   - **claim истёк, задача свободна**: заново claim (новый fencing token) +
     новый run; старый run будет superseded;
   - **takeover произошёл**: у задачи чужой claim — старый процесс не
     предпринимает авторитетных записей; попытка `:succeed` честно даёт
     `409 stale_claim`;
   - **run suspended**: дождаться/проверить gate, затем новый claim + run.
4. Дочитать события с сохранённого курсора (или `eventCursor` контекста).
5. Открыть новую сессию, если старая умерла (сессии дёшевы; чужие claims
   новая сессия не наследует — их надо переclaim'ить).

Control Plane восстанавливает work state, не скрытое состояние LLM.

## 13. Cancellation

- `POST /runs/{id}:request-cancel` (permission `tasks.write` или
  `claims.manage`) — кооперативный сигнал: `cancel_requested_at` + событие
  `run.cancel_requested`. Идемпотентен.
- «Запрошена отмена» ≠ «исполнение остановлено»: harness замечает сигнал
  (события или `GET /runs/{id}`), останавливается и финализирует `:cancel`
  (или `:fail`).
- Авторитетная остановка чужого run: holder или `claims.manage` через
  `:cancel`. После commit отмены `:succeed` невозможен (`409 run_not_active`);
  гонка cancel/succeed сериализуется локами task+run — победитель ровно один.

## 14. Аутентификация локального harness (ADR-0017)

- Ключи не хранятся в репозитории, CLAUDE.md, MCP-конфиге, истории shell.
- Резолюция: `CONTROL_PLANE_API_KEY` env → macOS Keychain →
  `~/.config/control-plane/credentials.json`. `control-plane login/logout`
  управляет хранилищем; отзыв — server-side revoke API-ключа. Legacy-локации
  кодового имени (env-переменные, сервис Keychain и каталог конфигурации)
  удалены в v0.5 (ADR-0040, [migration-v0.5](migration-v0.5.md)): читаются и
  пишутся только нейтральные локации.
- `CONTROL_PLANE_API_KEY` — явный override и применяется к любому серверу;
  остальные хранилища привязаны к URL сервера. В CI задавайте пару
  `CONTROL_PLANE_SERVER` + `CONTROL_PLANE_API_KEY` одного окружения.
- Файл credentials создаётся сразу с правами 0600 (не «записать, потом
  chmod»); в Keychain ключ передаётся через stdin, а не аргументом (argv виден
  в `ps`).
- Привязка проекта: `.control-plane/config.json` (legacy-каталог больше не
  читается — SDK бросает `LegacyProjectConfigError`; server/tenant/workspace/
  project/repository — только несекретные метаданные, можно коммитить). Поиск идёт вверх по
  дереву, но останавливается на корне репозитория (каталог с `.git`) и не
  выходит за пределы home: посторонний конфиг предка не должен решать, какому
  серверу клиент отправит ключ.

## 15. Идемпотентность

Все повторяемые команды принимают `Idempotency-Key`. SDK генерирует ключ на
логическую операцию и повторяет транспортные сбои **с тем же ключом**: retry
HTTP-запроса ≠ вторая бизнес-команда. Реплей возвращает сохранённый ответ с
заголовком `Idempotency-Replayed: true`.

Повторяет SDK только то, что повтор не превратит во вторую команду: `GET` и
запрос с `Idempotency-Key`. Повторяются сбои, которые ничего не говорят о
команде (`is_transient`): сетевой сбой и 502/503/504 прокси перед
перезапускающимся ядром. Так же считается обмен токена в IAM перед командой:
его 502/503/504 и отказ соединения (`iam_unreachable`) — перезапуск IAM, а 401,
403, 400 и прочие ответы IAM — вердикт. `HeartbeatRunner` переживает такую
недоступность IAM в пределах бюджета простоя, как и недоступность ядра. Паузы растут от 0,5 до 5 s, пока их сумма укладывается
в `retry_window` клиента: по умолчанию 1,5 s (три попытки). Ответ
`idempotency_in_flight` после такого сбоя тоже повторяется — первая попытка
ещё исполняется. Демон `control-plane-agent` берёт окно 300 s — TTL claim по
умолчанию (`CONTROL_PLANE_AGENT_RETRY_WINDOW`): перезапуск ядра на несколько
секунд не проваливает прогон, а недоступность дольше TTL проваливает.

## 16. Failure semantics (сводка)

| Сбой | Авторитетное состояние | Действие клиента | Retry безопасен? |
|---|---|---|---|
| network failure | неизвестно | повторить с тем же Idempotency-Key | да (с ключом) |
| harness crash | аренды тикают до TTL | рестарт → §12 | — |
| Control Plane restart | всё в PostgreSQL; прокси отвечает 502/503/504 | повторить GET и команды с ключом (§15), reconnect, дочитать события | да |
| session expiry | сессия stale, claims освобождены | новая сессия, переclaim | да |
| claim expiry | claim reap'ается следующим захватом | переclaim (новый token) | да |
| takeover | новый claim у другого | прекратить записи, сообщить человеку | нет (для старого) |
| run stale (superseded) | run failed(superseded) | остаётся audit-записью | — |
| approval rejected | gate открыт, задача actionable | решает исполнитель | — |
| skill unavailable | 409 skill_unavailable | выбрать другую версию/skill | да |
| invalid artifact | 404/422 | исправить запрос | да |
| event stream disconnect | журнал полон | reconnect с last sequence | да |

## 17. Machine-readable errors

Единый конверт `{"error": {code, message, details, requestId}}`. Ключевые
коды для harness UX: `not_eligible`, `task_not_ready`, `task_already_claimed`,
`stale_claim`, `session_expired`, `approval_required`, `skill_unavailable`,
`version_conflict`, `idempotency_key_reused`, `idempotency_in_flight`,
`budget_exceeded`, `run_not_active`, `unsupported_protocol_version`,
`task_cancelled`, `task_not_claimable`.

## 18. Working context и долговременная память (v0.4)

Два разных вида continuity, и протокол их не смешивает (ADR-0028):

- **Operational** — авторитетное ТЕКУЩЕЕ состояние: `/harness/context`,
  `/runs/{id}/context`, а с v0.4 — `POST /context`, который добавляет фокус
  задачи (task + artifacts) к bootstrap-набору.
- **Durable memory** — накопленное знание из внешнего Context Memory Engine
  (опционален): findings/decisions прошлых сессий, связанные факты, с
  provenance и `trace_id`.

`POST /context` `{query?, task?, runId?, workspaceId?, maxTokens?,
includeMemory?}` возвращает `{operational, memory | null, memoryStatus,
memoryTraceId, freshness{currentCursor, memoryCursor, memoryLagEvents},
warnings}`. Правила:

- operational — истина; воспоминание «задачей владел A» никогда не
  перекрывает текущий claim в operational;
- память недоступна → `memoryStatus: disabled|unavailable|timeout`, ответ
  остаётся 200 с полным operational;
- scopes авторизуются сервером внутри tenant'а ДО обращения к памяти;
  Memory-credentials никогда не выдаются harness'у.

`POST /observations` `{kind, content, data?, task?, runId?, workspaceId?,
sessionId?}` (permission `observations.write`) — явный «remember»:
записывается событием `observation.recorded` (replayable), provenance
проставляет сервер. Baseline kinds: `finding, decision, constraint, note,
summary, result, preference, external_fact`. Сохранять можно только
намеренно externalized знание — никаких скрытых рассуждений, сырых промптов
и истории терминала.

Ошибки контекстного слоя: `context_provider_unavailable` (в
`memoryStatus`/warnings, не HTTP-ошибка), `invalid_context_request`,
`observation_invalid`, `invalid_cursor`, `unsupported_cursor_version`.

## 19. Project-контекст (v0.5)

Harness, работающий внутри проекта, может сфокусировать контекст:

```http
POST /api/v1/context
{"projectId": "...", "includeSubprojects": false, "task": "TASK-000123"}
```

`operational.project` содержит идентичность проекта (`id`, `workspaceId`,
вычисленный `parentProjectId`, `templateKey`/`templateVersion`), lifecycle
(`statusKey` + `systemStatusCategory`), `workspaceScope` и — главное —
`effectiveConfig` вместе с `configProvenance`: по каждому верхнеуровневому
ключу видно, каким слоем он задан (`template` / `ancestor` / `revision` /
`profile`) и каким проектом-предком.

Что harness обязан понимать про эти данные:

- решения принимайте по `systemStatusCategory`, а не по пользовательскому
  `statusKey` — набор ключей задаёт tenant, набор категорий фиксирован;
- `effectiveConfig.governance` — рамка, а не пожелание: она уже свёрнута с
  предками и не может быть слабее их;
- конфигурация **не** уходит в долговременную память: в Memory передаётся
  только идентичность и статус проекта;
- `projectId` требует права `projects.read`; чужой проект — `404`.

Права v0.5: `projects.read` достаточно для контекста и чтения; изменение
проекта — `projects.manage`; шаблоны — `project_templates.read|manage`;
операторские действия — `operations.read|manage`.

## 20. Checklist нового harness

1. Получить API-ключ principal'а; сохранить в credential store.
2. `GET /harness/context` — identity + recovery (§12).
3. Открыть сессию с блоком `harness` (§2); heartbeat-цикл (§6).
4. Discovery (§4) → выбор задачи (человеком или политикой агента);
   при работе внутри проекта — `projectId` (§19).
5. claim → start-run → `POST /context` (операционное состояние + память,
   §18; `projectId` для проектной рамки, §19) → работа с
   checkpoints/actions/artifacts.
6. Важные findings/decisions — `POST /observations` (§18).
7. Финализация; при ожидании — gate + suspend (§7).
8. Подписка на события с курсора; уведомления человеку (§11).
9. На любой `stale_claim` — стоп авторитетных записей.
