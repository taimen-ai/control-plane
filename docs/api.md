# HTTP API

Все endpoint'ы — под `/api/v1`. Полная схема: `GET /openapi.json` (Swagger UI —
`/docs`). Поля — camelCase.

## Аутентификация и авторизация

```
Authorization: Bearer cp_<prefix>_<secret>
```

Actor всегда определяется из ключа; `actorId` из тела запроса не принимается.
Permissions ключа: `principals.read|write`, `delegations.manage`,
`sessions.open|manage`, `tasks.read|write|claim`, `claims.manage`,
`events.read|export`, `workspaces.read|manage`, `org.read|manage`,
`artifacts.read|write`, `approvals.read|manage|decide`,
`observations.write`, `projects.read|manage`,
`project_templates.read|manage`, `operations.read|manage`,
`artifact_types.read|manage` (CP-ADR-0072),
`agents.read|manage|status.write` (CP-ADR-0073),
`processes.read|write|operate`, `packages.test|plan`, `calendars.write`
(CP-ADR-0074), `packages.settings.read|manage` (CP-ADR-0081),
`knowledge.packs.manage` (CP-ADR-0060), `admin`
(подразумевает все). Права v0.5 не выдаются старым ключам неявно —
их нужно назначить явно (см. [migration-v0.5](migration-v0.5.md)). Проверка выполняется и в route-слое, и в командах
application-слоя.

Организационные примитивы (roles/capabilities/skills) **не** дают API-прав:
они определяют *eligibility* (кто может claim'ить задачу с требованиями, кто
может решить approval). Claim требует одновременно: API permission
`tasks.claim` ∧ eligibility ∧ readiness ∧ concurrency-правила.

**Могу ли я X на Y** ([CP-ADR-0055](adr/0055-policy-authorize-and-shadow-mode.md),
амендмент 2026-09-29). Интерфейс не выводит доступность действия из плоских
permissions: право бывает выдано на один воркспейс, а решение approval
зависит ещё от eligibility и separation of duties. Он спрашивает ядро:

```
POST /api/v1/authz:check      (любой аутентифицированный; о себе; без Idempotency-Key)
{"checks": [{"action": "approve", "resourceType": "approval", "resourceId": "<uuid>"},
            {"action": "update-state", "resourceType": "agent", "resourceId": "<key>"}]}
→ 200 {"results": [{"action": "approve", "resourceType": "approval", "resourceId": "<uuid>",
                    "allowed": false,
                    "reason": {"code": "separation_of_duties_violation", "message": "…",
                               "details": {"approvalId": "<uuid>"}}},
                   {"action": "update-state", …, "allowed": true, "reason": null}]}
```

Действия: `approval` — `approve|reject`; `process_instance` —
`suspend|resume|cancel`; `run` — `request-cancel|cancel`; `rule` —
`enable|disable`; `agent` — `update-state` (`PATCH /agents/{key}/state`);
`principal` — `enable|disable` (`POST /principals/{id}:enable|:disable`).
Ответ считает тот же код, что и эндпоинт: `allowed: true` — эндпоинт не
ответит 403/404; отказ несёт код и details отказа эндпоинта. У `approve`
это включает чтение того, на что ссылаются preconditions типа задачи
(`tasks.read` на задачу и `spawnedBy`, `artifacts.read`), — но не их
выполнение: невыполненное условие — `409 approval_precondition_failed`. Состояние
ресурса (решённый approval, завершённый прогон) проверкой не покрывается —
это 409 эндпоинта. Исключение — principal: его ворота отвечают и за то, чем
цель является и останется (`422 principal_kind_not_enableable|_disableable`,
`409 cannot_disable_self`, `409 use_agent_publish|use_agent_retire`), — по
карточке principal'а интерфейс этого не прочтёт; повтор (включить активного,
отключить отключённого) эндпоинт отвечает `200`, и проверка его разрешает. 1–100 элементов (`400`), незнакомый глагол или
тип — `400 invalid_request` на весь пакет, известные глагол и тип вне реестра —
`422 unknown_action`, id не UUID — `422 invalid_check`. Токен решения
(`control-plane:decide`) проверку не вызывает: `403 outside_purpose`.

## Заголовки протокола

| Заголовок | Где | Семантика |
|---|---|---|
| `Idempotency-Key` | создающие и action POST | Повтор идентичного запроса возвращает сохранённый ответ (`Idempotency-Replayed: true`); тот же ключ с другим телом **или другим principal** → `409 idempotency_key_reused`; параллельный дубль ждёт результат первого. Одноразовые секреты не сохраняются: replay создания API-ключа возвращает `key: null`. Незавершённая (pending) запись живёт ≤60 с — упавший исполнитель не блокирует ключ на весь TTL |
| `If-Match: "<entity>-<version>"` | PATCH task/workspace/workspace_type/project/role/skill, task `:complete`, project `:transition` и `config-revisions/{n}:activate` | Optimistic concurrency; несовпадение → `409 version_conflict` (+ `currentVersion` в details); отсутствие → `428 if_match_required`; мусор → `400 invalid_if_match` |
| `ETag` | GET task/workspace/workspace_type/project/role/skill | `"<entity>-<version>"` (skill использует `row_version`) |
| `X-Request-ID` | везде | принимается или генерируется; echo в ответе и в ошибках |
| `X-Correlation-ID` | везде | попадает в `events.correlation_id` |
| `X-Run-Id` | везде | v0.5 trace-корреляция (ADR-0039): принимается по `^[A-Za-z0-9._:-]{1,128}$`, иначе генерируется; echo в ответе, пишется в `events.trace_run_id`, outbox, логи (`run_id`) и заголовок к Memory. **Не** доменная сущность Run |

## Пагинация

`?limit=50&cursor=<opaque>` (default 50, max 200; больше → `422 invalid_limit`).
Ответ: `{"items": [...], "nextCursor": "..." | null}`. Сортировка стабильная:
списки сущностей — по `(created_at, id)` (новые первыми), список run'ов — по
`(started_at, id)` (новые первыми, CP-ADR-0073 Г2). Журналы run'а
(`/runs/{id}/checkpoints`, `/runs/{id}/actions`) идут по `seq` (старые первыми);
их курсор — последний выданный `seq`, привязан к run'у (чужой → `422 invalid_cursor`),
поэтому записи, дописанные между запросами, опрос по `nextCursor` не пропускает.
Без `limit` и `cursor` журнал run'а отдаётся **целиком** одной страницей
(`nextCursor: null`) — как до появления у них пагинации; страница (default 50)
выдаётся только при явном `limit` и/или `cursor`.

`GET /tasks` дополнительно принимает `?sort=startDate|dueDate` (v0.8, ADR-0049):
ближайшие первыми, задачи без этой даты — в хвосте, тай-брейк по `id`, поэтому
равные даты не ломают постраничный обход. Курсор привязан к порядку, под которым
выдан: применённый к другому `sort` он даёт `422 invalid_cursor`. Неизвестное
значение `sort` → `422 invalid_sort`. Границы `startFrom|startTo|dueFrom|dueTo`
включающие и применяются до пагинации; задачи без соответствующей даты под такой
фильтр не попадают.

`GET /tasks?q=<текст>` — текстовый поиск (ADR-0049, амендмент 2026-09-29): каждый
терм `q` (через пробел) должен встретиться подстрокой без учёта регистра в
`title`, `description` или `publicId`; `%`, `_`, `\` — буквальный текст. Пустой
`q` — без фильтра; больше 200 символов или 10 термов → `422 invalid_search`.
Сочетается со всеми фильтрами, `sort` и курсором (фильтр до пагинации); права —
как у списка. Индексы — `pg_trgm` GIN на трёх полях.

Комментарии к задаче (v0.8, ADR-0050) — единственная выборка, идущая **от
старых к новым**: тред читают вперёд, и реплика, написанная во время листания,
приезжает на следующей странице, а не теряется перед первой. Курсор ленты имеет
собственный формат, поэтому курсор любой другой выборки здесь даёт
`422 invalid_cursor` (и наоборот).

События (`GET /events`) — отдельный контракт: доставка в порядке
`(tx_id, sequence)` под стабильным горизонтом, курсор непрозрачный
(`ec1_...`), ответ всегда содержит `nextCursor` (echo при пустой странице) и
`hasMore`; каждое событие несёт своё поле `cursor`, `workspaceId` (workspace
сущности; `null` у событий уровня tenant) и `schemaVersion` — версию схемы
`payload` по каталогу `docs/events/catalog.md` (CP-ADR-0068). Фильтры `types`
(префиксы типа) и `workspaceId` (поддерево; `includeDescendants=false` —
только сам workspace), а также `actorId` (автор события) и период
`occurredFrom` (включительно) / `occurredTo` (исключительно; ISO 8601 с зоной)
сужают выборку, не меняя порядок и смысл курсора (CP-ADR-0068, амендмент
2026-10-03, Б1–Б4). Те же фильтры за период принимает выгрузка
`GET /events:export?format=jsonl|csv` — всё тело одним потоком, с пределом
периода и объёма (CP-ADR-0068, амендмент 2026-10-04, В1–В5). Legacy
`?after=<sequence>` (v0.3) принимается и адаптируется; `?tail=N` возвращает
последние N стабильных событий. Назад журнал читается через
`?before=<cursor>` — события строго до курсора; `prevCursor` ответа — `before`
для предыдущей страницы (`null` у `tail`/`before` — начало журнала),
`order=desc` отдаёт события страницы от новых к старым; `before` вместе с
`cursor`/`after`/`tail` — `422 conflicting_cursors` (CP-ADR-0024, амендмент
2026-09-29). Малформированный курсор → `422
invalid_cursor`; будущая версия курсора → `422 unsupported_cursor_version`.
Потребителю с сохранением курсора, дедупом и пробуждением по WebSocket —
SDK `control_plane_client.events` ([docs/events/consumer.md](events/consumer.md),
CP-ADR-0069).

## Неизвестные query-параметры

Сервер не игнорирует query-параметры молча (ADR-0058). Параметр, которого
endpoint не объявляет, — опечатка или фильтр, до которого сервер ещё не
обновлён, — даёт `400 invalid_request`, и выборка не выполняется:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Request does not match the API contract",
    "details": {"errors": [{"loc": "query.assignedToMee", "message": "Unknown query parameter: ..."}]},
    "requestId": "req_..."
  }
}
```

Правило действует на всех endpoint'ах `/api/v1`, а не только на ограничивающих:
`2xx` означает, что сервер понял **все** переданные параметры. Непонятый
параметр назван в `error.details.errors[].loc` как `query.<имя>` — клиенту
следует опираться на `code` и `loc`, а не на текст `message`. Не добавляйте
служебные параметры (например, cache-buster `_=`): они тоже будут отклонены.
Исключение — WebSocket `/events/ws`, который отвечает кодами закрытия.

Защита есть только у сервера, где она развёрнута: сервер старее ADR-0058
неизвестное по-прежнему игнорирует. Клиент, полагающийся на новый фильтр,
должен выкатываться после сервера.

## Endpoint'ы

Пометка `[план AXXX]` — контракт принят
([CP-ADR-0072](adr/0072-artifact-content-types-task-io.md)), а реализует его
названная задача фичи `artifact-handoff`; до её выкатки сервер маршрута или
поля не знает.

```
POST /api/v1/bootstrap                    (Bearer <CP_BOOTSTRAP_TOKEN>; одноразово)

POST /api/v1/authz:check                  аутентифицированный (о себе; 1–100 проверок, CP-ADR-0055
                                                              амендмент 2026-09-29; см. «Могу ли я X на Y»)

POST /api/v1/principals                   principals.write
GET  /api/v1/principals                   principals.read
GET  /api/v1/principals/{id}              principals.read    (ETag: "principal-<version>")
PATCH /api/v1/principals/{id}             principals.write   (CP-ADR-0082 п.1: {displayName?, profile?},
                                                              profile — полная замена {jobTitle, email,
                                                              phone, note}; If-Match: "principal-<version>"
                                                              обязателен (428/400/409 version_conflict);
                                                              агент реестра — 409 principal_managed_by_registry;
                                                              секрет в строке — 422 secret_material_rejected,
                                                              форма — 422 validation_error, обе с
                                                              details.errors[].path без значения;
                                                              principal.updated с именами полей)
POST /api/v1/principals/{id}/api-keys     principals.write   (полный ключ — один раз)
POST /api/v1/api-keys/{id}:revoke         principals.write
GET  /api/v1/principals/{id}/iam-bindings principals.read    (связки IAM-identity, отозванные тоже)
POST /api/v1/principals/{id}/iam-bindings principals.write   (upsert по issuer + iamPrincipalId, ADR-0053;
                                                              visibility?: tenant|members — нет поля —
                                                              без изменения, у новой связки tenant;
                                                              неверное — 422 validation_error,
                                                              details.errors[{path:/visibility, code}];
                                                              members не у human — 422
                                                              visibility_requires_human (CP-ADR-0082 §2);
                                                              вызывающий в members не оставляет связку
                                                              tenant — 403 visibility_escalation
                                                              (CP-ADR-0082 Б5; так же identity:replace
                                                              и PUT /agents/{key}/identity, В3))
POST /api/v1/iam-bindings/{id}:revoke     principals.write
POST /api/v1/principals/{id}:disable      principals.write   (CP-ADR-0077: human/agent, не себя, не агента
                                                              реестра; администратора — только admin;
                                                              связки и делегирования → revoked, сессии
                                                              его и от его имени → closed, claims
                                                              освобождены, прогоны на них → failed,
                                                              вызовы навыков от его имени отменены,
                                                              principal.disabled; повтор — 200 без изменений)
POST /api/v1/principals/{id}:enable       principals.write   (CP-ADR-0077, амендмент «Включение»: disabled|paused
                                                              → active; связки, отозванные :disable, НЕ
                                                              восстанавливаются — вход через IAM новой связкой
                                                              POST …/iam-bindings; неотозванные неистёкшие
                                                              API-ключи снова работают, поэтому их права
                                                              (и права неотозванных связок) вызывающий
                                                              обязан иметь сам — иначе 403 permission_escalation
                                                              с details.missing, как при выдаче ключа;
                                                              выведенный агент реестра → 409
                                                              use_agent_publish; живой агент реестра
                                                              (agent|service) включается, связку
                                                              открывает PUT /agents/{key}/identity
                                                              (CP-ADR-0073 И5);
                                                              ответ — PrincipalOut + liveApiKeys (число живых
                                                              ключей); principal.enabled; повтор — 200 без
                                                              изменений)

POST /api/v1/delegations                  delegations.manage
GET  /api/v1/delegations                  delegations.manage
POST /api/v1/delegations/{id}:revoke      delegations.manage

POST /api/v1/sessions                     sessions.open      (onBehalfOf требует делегирования;
                                                              controlLevel назначает сервер:
                                                              human_operated для human,
                                                              connected для agent/service;
                                          v0.3: optional harness{type, version, protocolVersion,
                                          capabilities, hostname, environment} — регистрация
                                          harness, negotiation control-harness/1|2)
GET  /api/v1/sessions                     sessions.manage
GET  /api/v1/sessions/{id}                владелец или sessions.manage
POST /api/v1/sessions/{id}:heartbeat      владелец или sessions.manage
POST /api/v1/sessions/{id}:close          владелец или sessions.manage (снимает claims сессии)

GET  /api/v1/harness/context              только аутентификация (self-контекст: identity,
                                          активные sessions/claims/runs, roles/skills,
                                          pending approvals, eventCursor)
GET  /api/v1/work/available               tasks.read   (advisory discovery: eligible+ready+
                                          unclaimed+ungated; ?workspaceId=&includeDescendants=
                                          &projectId=&includeSubprojects=&assigneeId=
                                          &assignedToMe=&typeKey=&limit=&cursor=; typeKey —
                                          повторяемый ключ типа (любая версия), только сужает,
                                          пустой или >50 — 422 invalid_type_key (CP-ADR-0056
                                          Ж1); assignedToMe=true —
                                          только адресованное вызывающему, перекрывает assigneeId;
                                          страница может быть короче limit при непустом
                                          nextCursor; архивный проект новую работу не выдаёт;
                                          CP-ADR-0072: задача без обязательного входа типа
                                          не выдаётся)

POST  /api/v1/task-types                  task_types.manage (следующая версия ключа; версию
                                          выдаёт сервер; lifecycleSchema валидируется локально;
                                          approvalSchema — исходы gate-approval, ADR-0061:
                                          закрытый словарь действий и выражений, только
                                          гейт default, иначе 422 invalid_approval_schema;
                                          preconditions.approved — предусловия approve
                                          по наблюдениям (ADR-0061, амендмент 2 от 2026-09-25);
                                          execution {skill, version, inputs?} — задачи типа
                                          исполняет Skill, см. «Skill runtime»;
                                          contextSchema — профиль контекста, ADR-0064:
                                          anchors[{from, kind|kinds, via?}], traverse[
                                          {relation, direction, depth, limit, from}],
                                          asOf taskCreated|now|origin, budgetTokens;
                                          иначе 422 invalid_context_schema;
                                          instructions — Markdown ≤ 16 KiB, как
                                          исполнять задачу типа, CP-ADR-0066: больше
                                          16 KiB — 422 instructions_too_large, материал
                                          секрета — 422 secret_material_rejected;
                                          completionSchema — работа после завершения,
                                          CP-ADR-0061 амендмент 2026-09-25: {onComplete:
                                          {when?, actions}}, действия ensureWork|comment,
                                          корни $.task/$.spawnedBy, иначе 422
                                          invalid_completion_schema;
                                          artifactSchema — входы и выходы, CP-ADR-0072:
                                          inputs[{key, type, from
                                          depends_on|spawned_by|parent, required?}],
                                          outputs[{key, type, required?, mediaTypes?,
                                          content? required|optional}]; тип артефакта не
                                          зарегистрирован — 422 unknown_artifact_type,
                                          иначе 422 invalid_artifact_schema;
                                          executorRoles[] — slug ролей, нужных человеку,
                                          чтобы брать работу версии, CP-ADR-0048
                                          амендмент 2026-10-03 А1: ≤ 20 уникальных slug,
                                          иначе 400 invalid_request; роли нет у tenant'а —
                                          422 unknown_role (details.field =
                                          executorRoles[i]); нет/[] — людей не ограничивает;
                                          изменение — только новой версией)
GET   /api/v1/task-types                  task_types.read   (?key=&status=&workspaceId=;
                                          workspaceId — только типы, разрешённые в workspace
                                          по его effectiveTaskTypes, CP-ADR-0008 амендмент
                                          2026-10-03 А2; чужой или несуществующий
                                          workspace — 404)
GET   /api/v1/task-types/{id}             task_types.read
GET   /api/v1/task-types/{id}/executors   workspaces.read и (org.read | principals.read)
                                          (?workspaceId= обязателен; CP-ADR-0048 амендмент
                                          2026-10-03 А2–А3: кто может брать эту версию типа
                                          в workspace; items[]: principalId, kind
                                          human|agent|service, displayName, roles[],
                                          reason role|any|agent_task_types|agent_any;
                                          люди — участники workspace с одной из
                                          executorRoles в его области, пусто — все;
                                          агенты и сервисы — реестр /agents, running и
                                          active, spec.work.workspace — сам workspace или
                                          предок, spec.work.taskTypes содержит тип или не
                                          задан; чужой тип или workspace — 404; назначение
                                          без роли ядро не запрещает)
POST  /api/v1/task-types/{id}:deprecate   task_types.manage (идемпотентно; задачи, ссылающиеся
                                          на версию, продолжают работать)
POST  /api/v1/task-types/{id}:migrate-tasks task_types.manage + tasks.write (ADR-0048,
                                          амендмент 2026-09-30: открытые задачи версии
                                          {id} → toVersion? (по умолчанию новейшая active
                                          того же ключа), statusMap?, limit? 1..500
                                          (100), cursor? — страница по id; ответ
                                          {typeKey, fromTypeVersion, typeVersion, typeId,
                                          migrated[{taskId, publicId, fromStatus, status,
                                          version}], skipped[{taskId, code, message,
                                          details}], nextCursor}; цель = источник или
                                          по умолчанию старше источника —
                                          422 invalid_migration_target; задача без
                                          tasks.write у вызывающего в страницу не входит)

POST  /api/v1/tasks                       tasks.write  (optional parentTask создаёт parent relation
                                                        атомарно; requirements.skills: "name" |
                                          "name@version" — точная версия;
                                          v0.8: typeKey|typeId|typeVersion — по умолчанию
                                          системный тип; status по умолчанию initialStatus типа;
                                          customFields проверяются по fieldSchema выбранной
                                          версии типа; startDate/dueDate — ISO-8601;
                                          M1.1, ADR-0062: goalId, origin {kind, ref?, ruleId?,
                                          evidence[]} — неизменяем, по умолчанию выводится
                                          ядром (parentTask → parent, иначе human|harness по
                                          виду principal), acceptance[], evidence[];
                                          acceptance[].spec — по грамматике вида (ADR-0067),
                                          иначе 422 invalid_acceptance_spec; скилл критерия
                                          deterministic — зарегистрированный и не
                                          external_write, тип артефакта критерия
                                          spec.artifact — зарегистрированный, иначе
                                          тоже 422; ключ с префиксом output. —
                                          422 invalid_acceptance; тип не разрешён в
                                          workspaceId (effectiveTaskTypes, CP-ADR-0008
                                          амендмент 2026-10-03 А3) — 422
                                          task_type_not_allowed {workspaceId, typeKey})
GET   /api/v1/tasks                       tasks.read   (?status=&systemStatusCategory=&typeKey=
                                          &priority=&ownerId=&assigneeId=&workspaceId=
                                          &includeDescendants=&startFrom=&startTo=&dueFrom=&dueTo=
                                          &sort=createdAt|startDate|dueDate&goalId=&q=)
GET   /api/v1/tasks/{id|publicId}         tasks.read   (+ETag)
GET   /api/v1/tasks/{ref}/claimability    tasks.read   (диагностика: claimable + reasons; ADR-0067:
                                          verification_pending {verificationId, status};
                                          CP-ADR-0072: input_missing
                                          {missing[{key, type, from}]};
                                          CP-ADR-0082 В4: невидимые вызывающему
                                          предусловия и шлюзы не называются —
                                          hiddenBlockers / hiddenApprovals (число),
                                          так же в 409 task_not_ready /
                                          approval_required у :claim)
GET   /api/v1/tasks/{ref}/verifications   tasks.read   (ADR-0067: попытки стадии проверки, новые
                                          сверху; ?limit=&cursor=)
GET   /api/v1/tasks/{ref}/transitions     tasks.read   (объявленные цели перехода из текущего
                                          статуса: status, displayName,
                                          systemStatusCategory, route = update|complete;
                                          route=complete означает ребро в terminal_success,
                                          которое проходит :complete, а не PATCH.
                                          Права на реестр типов не требует)
PATCH /api/v1/tasks/{id|publicId}         tasks.write  (If-Match; claimId+fencingToken при живом claim;
                                          customFields заменяют документ целиком, null запрещён
                                          (очистка — {}); startDate/dueDate: null очищает дату;
                                          goalId: null отвязывает; goalId — цель, читаемая
                                          (goals.read) и того же воркспейса или уровня tenant,
                                          иначе 404; перенос задачи от цели её воркспейса —
                                          422 goal_workspace_mismatch; acceptance/evidence
                                          заменяют список целиком, null запрещён; origin — 400;
                                          перенос в workspace, где тип задачи не разрешён, —
                                          422 task_type_not_allowed {workspaceId, typeKey})

POST  /api/v1/goals                       goals.write  (ADR-0062: title, desiredState, criteria[],
                                          ownerId, workspaceId, parentGoalId, createdFrom —
                                          форма origin, по умолчанию human|harness)
GET   /api/v1/goals                       goals.read   (?status=&workspaceId=&ownerId=&parentGoalId=)
GET   /api/v1/goals/{id}                  goals.read   (+ETag "goal-<v>")
PATCH /api/v1/goals/{id}                  goals.write  (If-Match; title, desiredState, criteria,
                                          ownerId|null, status active|achieved|abandoned,
                                          parentGoalId|null — цикл → 422 goal_cycle; родитель
                                          из другого воркспейса → 422 goal_workspace_mismatch,
                                          без goals.read на родителя → 404 как несуществующий)
GET   /api/v1/goals/{id}/work             goals.read + tasks.read (задачи цели, новые сверху;
                                          ?includeSubgoals=&systemStatusCategory=&limit=&cursor=)

POST  /api/v1/rules                       rules.write  (ADR-0063: key, description, workspaceId,
                                          goalId, trigger, condition, interpretation, action,
                                          status enabled|disabled; включённое правило действует
                                          полномочиями пишущего; 422 invalid_rule_condition|
                                          invalid_rule_trigger (в т.ч. триггер на rule.*|work.*|
                                          skill.invocation_*)|invalid_rule_action|
                                          invalid_rule_interpretation|unknown_task_type|
                                          unknown_skill|rule_skill_side_effects; 409 rule_key_taken;
                                          action.kind ensure_work|update_work|cancel_work|
                                          complete_work|request_decision; acceptance — у
                                          ensure_work|request_decision (грамматика ADR-0067,
                                          ошибка — invalid_rule_action с details.cause), check —
                                          у complete_work (ключ критерия, по умолчанию
                                          rule-evidence); амендмент ADR-0063 2026-09-25;
                                          fields.customFields — у ensure_work|
                                          request_decision: {поле: шаблон}, форма — при
                                          записи (invalid_rule_action), значения — fieldSchema
                                          типа при заведении работы (оценка failed,
                                          custom_fields_invalid); амендмент ADR-0063 oss-sync;
                                          амендмент ADR-0063 2026-09-27 (C005): identity
                                          {agent} — правило оценивается и действует
                                          полномочиями principal'а агента реестра, 422
                                          unknown_agent (нет или выведен), 403
                                          permission_escalation (у пишущего нет права
                                          ревизии агента); у ensure_work — taskTypes и
                                          шаблонный taskType, fields.relations {spawnedBy,
                                          dependsOn}; отказ элемента forEach —
                                          work[].refused, все отказаны — failed
                                          work_items_refused; в task — typeKey, typeVersion);
                                          амендмент ADR-0063 P012: fields.workspaceId —
                                          шаблон workspace заводимой работы (ensure_work|
                                          request_decision; пусто — workspace правила;
                                          tasks.write проверяется на целевом workspace; не id —
                                          failed invalid_rule_field), fields.assignee
                                          role:<slug> — работа роли целевого workspace без
                                          исполнителя (нет роли — failed unknown_role);
                                          амендмент ADR-0063 I001/TASK-001205: action.target
                                          dedup|task — у cancel_work|complete_work; task
                                          закрывает задачу, к которой привязано наблюдение
                                          триггера (payload.taskId); trigger.agent (ключ
                                          агента реестра) и trigger.actorId (id principal'а)
                                          — фильтр автора у триггера observation: правило
                                          берёт только наблюдения, записанные этим автором
                                          (events.actor_id; оба поля — должны совпасть оба);
                                          у event|schedule этих полей нет; неверная форма —
                                          422 invalid_rule_trigger; агент неизвестен,
                                          выведен — 422 unknown_agent, details {field:
                                          trigger.agent, agent}; target: task без
                                          trigger.agent и без trigger.actorId — 422
                                          invalid_rule_trigger, details.field =
                                          trigger.agent (POST, PATCH, пакет); правило,
                                          сохранённое раньше без фильтра, ничего не
                                          закрывает — оценка, в т.ч. ждавшая снятия claim,
                                          skipped trigger_author_unfiltered
GET   /api/v1/rules                       rules.read   (?status=&workspaceId=&key=&triggerKind=;
                                          archived — только по status=archived)
GET   /api/v1/rules/{id}                  rules.read   (+ETag "rule-<v>")
PATCH /api/v1/rules/{id}                  rules.write  (If-Match; description, trigger, condition|null
                                          (null — всегда), interpretation|null, action, goalId|null,
                                          identity|null (null — снять личность; у правила с
                                          личностью любая правка проверяет права пишущего);
                                          архивное — 409 rule_archived)
POST  /api/v1/rules/{id}:enable           rules.write  (идемпотентно; полномочия — вызывающего)
POST  /api/v1/rules/{id}:disable          rules.write  (идемпотентно)
DELETE /api/v1/rules/{id}                 rules.write  (204; архив: история остаётся, ключ свободен)
GET   /api/v1/rules/{id}/evaluations      rules.read   (история оценок, новые сверху; ?status=
                                          waiting|matched|not_matched|failed|skipped; waiting —
                                          ждёт скилл интерпретации или освобождения claim
                                          (result.waitingFor = claim, work[].runId,
                                          cancelRequested); после — already_done|
                                          already_closed|verification_pending|
                                          claim_not_released)
GET   /api/v1/rule-evaluations/{id}       rules.read   (одна оценка, та же форма, что в
                                          /rules/{id}/evaluations; цель origin.ref =
                                          rule_evaluation:<id> задачи правила; право — на
                                          workspace правила; чужой tenant — 404; CP-ADR-0063 Е1)
POST  /api/v1/tasks/{id|publicId}:claim   tasks.claim  (body: sessionId, ttlSeconds?, intent?;
                                          409 verification_pending, пока идёт проверка;
                                          CP-ADR-0072: нет обязательного входа
                                          типа — 409 input_missing, details.missing[{key,
                                          type, from}])
POST  /api/v1/tasks/{id|publicId}:migrate-type task_types.manage + tasks.write на задачу
                                          (If-Match; ADR-0048, амендмент 2026-09-30:
                                          перевод открытой задачи на другую версию
                                          своего ключа; body {typeVersion?, statusMap?};
                                          по умолчанию новейшая active, но не старше
                                          текущей — иначе 422 invalid_migration_target;
                                          уже на этой версии — 200 без изменений;
                                          409 task_terminal | task_claimed (и для
                                          держателя) | run_in_progress |
                                          verification_pending | approval_pending |
                                          incompatible_status; 422 invalid_status_map
                                          (в т.ч. ключ, которого нет в текущей версии)
                                          | custom_fields_invalid | invalid_acceptance;
                                          событие task.type_migrated, task.updated не
                                          шлётся)
POST  /api/v1/tasks/{id|publicId}:complete tasks.write (If-Match; claimId+fencingToken при живом claim;
                                          задача с acceptance или с обязательными
                                          выходами типа не выполняется сразу —
                                          открывается попытка проверки, см. ниже)

Стадия проверки (CP-ADR-0067). У задачи с непустым `acceptance` каждый из
трёх путей завершения — `:complete`, `:succeed` run, исход approval
`completeTask` — снимает claim, но **не** ставит `completionStatus`: задача
остаётся в текущем статусе, получает открытую попытку проверки
(`TaskOut.verification = {id, status, attempt, updatedAt}`, у задачи без
попыток — `null`) и не берётся исполнителями (`verification_pending`).
Повторное завершение, пока попытка открыта, возвращает задачу как есть и
второй попытки не создаёт. Worker исполняет критерии по порядку объявления
полномочиями завершившего:

- `deterministic` со `spec.skill` — вызов скилла (`requestedBy.kind =
  verification`, `requestedBy.ref` — id попытки, привязан к задаче); пройден,
  если вызов `succeeded` и выход равен `expect`. Без результата за
  `CP_VERIFICATION_SKILL_TIMEOUT_SECONDS` (900) вызов отменяется, критерий —
  `no_result`;
- `external_state` и `deterministic` без `spec` — evidence задачи с
  `check: <key>` (при `spec.event` — наблюдение этого вида); нет его за
  `CP_VERIFICATION_EXTERNAL_TIMEOUT_SECONDS` (86400) — `no_result`; попытка
  ждёт в `waiting_external`;
- `human`, `llm_judge` — решение gate-approval задачи. Approval, чей исход
  `completeTask` открыл попытку (`trigger = approval`), засчитывает все такие
  критерии сразу; иначе засчитывается gate задачи, одобренный после начала
  попытки (каждому критерию — своё решение). Если такого нет, попытка ждёт
  открытый gate задачи, а без него запрашивает gate-approval от имени ядра у
  `spec.approver` / `spec.approverRole` (id роли или `role:<slug>`), иначе у owner, затем assignee задачи
  (никого — провал `no_approver`); текст запроса — ключ и описание критерия,
  у `llm_judge` — и `rubric`. Попытка ждёт в `waiting_human` без таймера
  (`approvalId` — ожидаемый approval); решение или отмена approval будит её.
  `approved` — критерий пройден, evidence `{kind: approval, ref}`;
  `rejected` / `cancelled` — провал `approval_rejected` /
  `approval_cancelled` с комментарием решения в `message`. Пока ожидаемый
  gate открыт, повторное завершение задачи — `409 approval_required`, как
  всегда при открытом gate. Незакрытый запрос ядра отменяется, когда попытка
  проваливается или отменяется.

Все пройдены — задача переходит в `completionStatus` (`task.completed` и
`task.verified` в одной транзакции), на ней артефакт `verification`, затем
исполняется `completionSchema` её типа. Первый проваленный критерий проваливает
попытку: `task.verification_failed`, комментарий ядра с причинами, задача —
в `releaseStatus` и снова берётся исполнителем; третий провал подряд — в первый
достижимый статус категории `blocked`. Отмена задачи закрывает попытку
`cancelled` и отзывает живой вызов скилла. Попытка (`TaskVerificationOut`):
`attempt`, `status` (`running|waiting_human|waiting_external|passed|failed|
cancelled`), `trigger` (`complete|run|approval`), `triggerRef`,
`authorityPrincipalId`, `checks` (критерии на момент открытия, у каждого
`source`: `output | type | task | rule`), `results` (`[{key, kind, source,
status, evidence, reason, message?, details?}]`, `status` — `passed | skipped |
failed | cancelled`), `cursor`,
`skillInvocationId`, `approvalId`, `nextCheckAt`, `startedAt`, `finishedAt`.

Выходы типа как критерии (CP-ADR-0067, амендмент 2026-09-26; [план A006]).
`deterministic` принимает `spec: {artifact: {type, mediaTypes?, content?
required|optional}}` (взаимоисключает `skill`): пройден, если у задачи есть
head-ревизия артефакта этого типа с подходящим media type и, при `content:
required`, с содержимым; причины провала — `artifact_missing`,
`artifact_media_type`, `artifact_content_missing`. Обязательные выходы
`artifactSchema` типа задачи при каждом завершении становятся неявными
критериями `output.<key>` **перед** acceptance задачи — задача с обязательным
выходом проходит стадию, даже если её acceptance пуст. Ключ с префиксом
`output.` в acceptance — `422 invalid_acceptance`.

Декларативный цикл (фича `declarative-cycle`, контракт — C002; амендменты
CP-ADR-0067 2026-09-27, CP-ADR-0063 2026-09-27, CP-ADR-0073 А1).

Реализовано в C004:

- `acceptance[]` у `POST /task-types` и в `TaskTypeOut` — критерии по умолчанию
  версии типа (та же форма и грамматика, что у задачи; проверяются при
  публикации, иначе `422 invalid_acceptance_spec` / `invalid_acceptance` с
  `details.field`). Попытка исполняет выходы типа (`source: output`), затем
  критерии типа (`type`), затем критерии задачи (`task`); неявный
  `rule-evidence` (`rule`) — только если ни тип, ни задача критериев не
  объявляют. Ключ задачи, совпадающий с ключом её версии типа, —
  `422 invalid_acceptance` (`details.field = acceptance[i].key`).
  `TaskOut.acceptance` — по-прежнему только собственный документ задачи.
  Evidence задачи может ссылаться на критерий её типа (`check`).
- `executorRoles[]` у `POST /task-types` и в `TaskTypeOut` (CP-ADR-0048,
  амендмент 2026-10-03 А1) — slug ролей tenant'а, нужных человеку, чтобы брать
  работу версии; `[]` — людей не ограничивает. Неизвестная роль —
  `422 unknown_role` (`details.field = executorRoles[i]`). Кто подходит —
  `GET /task-types/{id}/executors?workspaceId=`; событие `task_type.created`
  несёт поле с версии 3.
- `when[]` у критерия (задача, тип, `acceptance` правил и `ensureWork`): 1…8
  выражений `$.task…` без `|truncate`; иначе `422 invalid_acceptance_spec`,
  `details.field = acceptance[i].when[j]`; у критериев цели —
  `422 invalid_acceptance`. Условие читается, когда попытка доходит до
  критерия, полномочиями завершившего. Невыполненное — результат `skipped`,
  `reason: condition_unmet`, `details.when` — первое невыполненное выражение;
  попытка идёт дальше, все пропущенные — попытка пройдена.
- `deterministic` со скиллом `external_write` допустим, если раньше него (для
  задачи — среди критериев её типа и своих) стоит `human`/`llm_judge` без
  `when` или с тем же `when`; иначе `422 invalid_acceptance_spec`,
  `details.cause = external_write_without_decision`. Исполняется, только если
  ближайший такой критерий прошёл в этой же попытке (иначе провал
  `no_decision`): вызов идёт полномочиями решившего gate
  (`decision_authority` approval) с `authorizationBasis = {kind: approval,
  approvalId}`. Решение gate задачи поэтому всегда сохраняет снимок полномочий
  решившего.

Реализовано в C006:

- `agent:<key>` в полях назначения: `assigneeId` у `POST`/`PATCH /tasks`,
  `assignee` у `ensureWork` (исходы approval и `completionSchema`),
  `fields.assignee` у `ensure_work` и `request_decision` правил. Ядро при
  записи заменяет ссылку на principal агента; в задаче и в `TaskOut.assigneeId`
  — UUID, ссылка не хранится. Агента с таким ключом нет, он выведен или ещё
  не связан с личностью — `422 unknown_agent`, `details: {field, agent}`
  (`field` — `assigneeId`, `ensureWork.assignee`, `action.fields.assignee`);
  в исходе и правиле это ошибка действия с тем же кодом, ничего не пишется.
  Ссылка разрешается после проверки права на запись: без `tasks.write` —
  `403`, а не ответ о существовании агента.

Ещё не реализовано — запрос с полем получает `501 not_implemented` с
`details.field` и ничего не пишет:

- `assigneeId` у `POST`/`PATCH /tasks` принимает `agent:<key>`. Неизвестный,
  выведенный или несвязанный агент — `422 unknown_agent` (C006).
- `identity {agent}` у `POST`/`PATCH /rules` и в `RuleOut`: правило действует
  полномочиями principal'а агента (C005).

GET  /api/v1/claims                       tasks.read   (?taskId=&sessionId=&status=)
GET  /api/v1/claims/{id}                  tasks.read
POST /api/v1/claims/{id}:heartbeat        держатель или claims.manage
POST /api/v1/claims/{id}:release          держатель или claims.manage (задача → releaseStatus
                                          её типа, если ребро объявлено)
POST /api/v1/claims/{id}:reclaim          tasks.claim  (только для истёкшего claim; новый token)

POST  /api/v1/workspaces                  workspaces.manage
GET   /api/v1/workspaces                  workspaces.read (?parentId=&rootsOnly=&status=)
GET   /api/v1/workspaces/{id}             workspaces.read (+ETag; ответы об одном workspace
                                          несут taskTypes и effectiveTaskTypes — null = все
                                          типы, CP-ADR-0008 амендмент 2026-10-03)
PATCH /api/v1/workspaces/{id}             workspaces.manage (If-Match "workspace-<v>"; taskTypes:
                                          [ключ типа] — разрешённые типы работ, null —
                                          наследовать от предка, [] — ни одного; неизвестный
                                          ключ — 422 unknown_task_type {field, taskType})
POST  /api/v1/workspaces/{id}:archive     workspaces.manage (нужно отсутствие активных детей)
POST  /api/v1/workspaces/{id}:move        workspaces.manage (body: newParentId|null; циклы -> 422)
POST  /api/v1/workspaces/{id}/members     workspaces.manage
GET   /api/v1/workspaces/{id}/members     workspaces.read   (только явное членство, CP-ADR-0010)
GET   /api/v1/workspaces/{id}/participants  workspaces.read и (org.read | principals.read)
                                          (амендмент CP-ADR-0010: явные участники и держатели
                                          ролей workspace; items[]: principalId, kind,
                                          displayName, status, member, roles[{roleId, slug,
                                          name, roleWorkspaceId, assignmentWorkspaceId}])
POST  /api/v1/workspaces/{id}/members/{principalId}:remove  workspaces.manage

POST  /api/v1/roles                       org.manage   (slug уникален в scope)
GET   /api/v1/roles[?workspaceId=]        org.read
GET   /api/v1/roles/{id}                  org.read (+ETag)
GET   /api/v1/roles/{id}/principals[?workspaceId=]  org.read | principals.read  (CP-ADR-0068:
                                          держатели роли — назначения уровня tenant и на
                                          workspace/его предке, как у права решать approval;
                                          без workspaceId — только уровня tenant; items[]:
                                          id, kind, displayName, status)
PATCH /api/v1/roles/{id}                  org.manage (If-Match "role-<v>")
POST  /api/v1/capabilities                org.manage
GET   /api/v1/capabilities[/{id}]         org.read
POST  /api/v1/skills                      org.manage   (name+version уникальны; contract v1 —
                                          см. «Skill runtime» ниже)
GET   /api/v1/skills                      org.read
GET   /api/v1/skills/{id|name@version|name}  org.read | skills.invoke | skills.execute
                                          (+ETag "skill-<rowVersion>"; тело — с полным contract;
                                          без org.read legacy config — {})
PATCH /api/v1/skills/{id}                 org.manage (If-Match; только description и status
                                          вперёд active→deprecated→disabled, иначе
                                          409 skill_version_immutable / invalid_status_transition)

POST /api/v1/principals/{id}/roles        org.manage   (body: roleId, workspaceId?)
GET  /api/v1/principals/{id}/roles        org.read | principals.read
POST /api/v1/principals/{id}/roles/{roleId}:revoke              org.manage
POST /api/v1/principals/{id}/capabilities                       org.manage
GET  /api/v1/principals/{id}/capabilities                       org.read | principals.read
POST /api/v1/principals/{id}/capabilities/{capId}:revoke        org.manage
POST /api/v1/principals/{id}/skills                             org.manage
GET  /api/v1/principals/{id}/skills                             org.read | principals.read
POST /api/v1/principals/{id}/skills/{skillId}:revoke            org.manage

POST   /api/v1/tasks/{ref}/relations      tasks.write  (body: toTask, type; циклы -> 422)
GET    /api/v1/tasks/{ref}/relations      tasks.read   (обе стороны)
DELETE /api/v1/tasks/{ref}/relations/{relationId}   tasks.write
GET    /api/v1/tasks/{ref}/requirements   tasks.read

POST  /api/v1/tasks/{ref}/comments        tasks.write  (body: body, runId?, artifactId?;
                                          автор — Principal вызывающего, в теле его нет)
GET   /api/v1/tasks/{ref}/comments        tasks.read   (лента от старых к новым)
GET   /api/v1/tasks/{ref}/comments/{id}   tasks.read   (+ETag "comment-<version>")
PATCH /api/v1/tasks/{ref}/comments/{id}   tasks.write  (If-Match; только автор; прежний текст
                                          сохраняется как ревизия)
GET   /api/v1/tasks/{ref}/comments/{id}/revisions   tasks.read   (append-only история правок)

POST /api/v1/tasks/{ref}:start-run        tasks.claim  (body: claimId, fencingToken, input?,
                                          maxDurationSeconds?, maxActions? — бюджет run;
                                          agentRevisionId? — ревизия своего
                                          агента, обязательна principal'у агента, иначе
                                          422 agent_revision_required|mismatch; у run —
                                          agentRevisionId, null не у агента, CP-ADR-0073 §7)
GET  /api/v1/runs                         tasks.read   (?taskId=&claimId=&status=&principalId=
                                          &agentKey=; новые первыми по (startedAt, id);
                                          agentKey — principal агента реестра, неизвестный
                                          или без principal — пустая страница; CP-ADR-0073 Г1)
GET  /api/v1/runs/{id}                    tasks.read
GET  /api/v1/runs/{id}/context            tasks.read   (Run Context: task/claim/requirements/
                                          artifacts/checkpoints всех прошлых runs/skills/cursor;
                                          CP-ADR-0066: instructions {layers[{source, ref,
                                          version, text}], hash} — слои platform →
                                          project → taskType, как они стоят сейчас;
                                          run.instructionsHash/instructionsRefs — с
                                          чем run был запущен; CP-ADR-0072:
                                          inputs[{key, type, artifactId, name, mediaType,
                                          sizeBytes, sha256, contentState, uri,
                                          sourceTask{id, publicId, relation}}] —
                                          разрешённые входы типа задачи, head-ревизии)
POST /api/v1/runs/{id}:succeed            tasks.claim  (владелец run; body: output?,
                                          completeTask=true -> атомарно завершает задачу)
POST /api/v1/runs/{id}:fail               владелец или claims.manage (задачу не трогает)
POST /api/v1/runs/{id}:cancel             владелец или claims.manage
POST /api/v1/runs/{id}:suspend            tasks.claim  (владелец с живым claim; run->suspended,
                                          claim released — waiting semantics, ADR-0018)
POST /api/v1/runs/{id}:handoff            tasks.claim  (Idempotency-Key; reason=
                                          human_harness_handoff; атомарно handoff checkpoint +
                                          run->suspended + claim released + task->todo;
                                          ответ: run/task/checkpoint/eventCursor/resume)
POST /api/v1/runs/{id}:request-cancel     tasks.write | claims.manage (кооперативный сигнал,
                                          идемпотентен; событие run.cancel_requested; демон
                                          раннера замечает его за ≤ 30 с, останавливает
                                          исполнителя, подтверждает control message и
                                          вызывает :cancel — runbook-agent-runner.md)
POST /api/v1/runs/{id}/control-messages   tasks.write; force_cancel: claims.manage
                                          (обязательны Idempotency-Key и expectedRunVersion;
                                          queue|steer|redirect|request_cancel|force_cancel)
GET  /api/v1/runs/{id}/control-messages   tasks.read   (Run-local rc1_ cursor,
                                          default 50/max 200)
POST /api/v1/runs/{id}/control-messages/{mid}:acknowledge
                                          tasks.claim  (holder живого Claim;
                                          claimId+fencingToken, expected Run/message versions;
                                          applied требует safeBoundary)
POST /api/v1/runs/{id}/child-handles      tasks.claim + tasks.write (владелец живого Claim
                                          родительского Run; Idempotency-Key обязателен;
                                          201 — новый child, 200 — повтор того же
                                          correlationId; handleToken отдаётся один раз)
GET  /api/v1/runs/{id}/child-handles      tasks.read   (cd1_ cursor, ?active=true)
GET  /api/v1/child-handles/{idOrToken}    tasks.read   (derived status + bounded result)
POST /api/v1/child-handles/{id}:revoke    держатель родительского Run или claims.manage
                                          (Idempotency-Key; body: reason, cancelChild)
POST /api/v1/runs/{id}/checkpoints        tasks.claim  (владелец с живым claim; body: kind, data)
GET  /api/v1/runs/{id}/checkpoints        tasks.read   (?limit=&cursor=; по seq, старые первыми;
                                          без limit и cursor — весь журнал)
POST /api/v1/runs/{id}/actions            tasks.claim  (владелец с живым claim; audit trail;
                                          enforce бюджета -> 409 budget_exceeded;
                                          skill: uuid|name|name@version; effective tool policy
                                          пересчитывается -> 403 tool_not_authorized)
POST /api/v1/runs/{id}/actions/{aid}:finish  tasks.claim (двухфазное завершение started-действия)
GET  /api/v1/runs/{id}/actions            tasks.read   (?limit=&cursor=; по seq, старые первыми;
                                          без limit и cursor — весь журнал)

GET  /api/v1/tools                        tasks.read   (Scoped Tool Discovery View, HRS-3:
                                          ?query=&runId=&limit=&cursor=; bounded projection
                                          пересечения catalog и effective policy; пустой query —
                                          eager-режим; ответ несёт view.catalogRevision,
                                          view.policyRevision и view.viewHash; viewHash
                                          отдаётся как ETag, If-None-Match даёт 304)
GET  /api/v1/tools/{ref}                  tasks.read   (ref: uuid|name|name@version; полная
                                          санитизированная inputSchema + schemaRedactions;
                                          вне policy — 404 tool_not_found, как и несуществующий)

POST /api/v1/artifacts                    artifacts.write (append-only; task|runId|workspaceId?;
                                          supersedesArtifactId? — ревизия, ADR-0020;
                                          CP-ADR-0072: contentRef — содержимое
                                          из PUT /artifact-contents, исключает uri и content
                                          (422 invalid_artifact_content); чужой, неизвестный
                                          или истёкший — 422 content_ref_not_found;
                                          type, зарегистрированный в tenant, проверяется по
                                          последней версии: metadata (422
                                          invalid_artifact_metadata, details.errors), media
                                          type и размер содержимого из contentRef (422
                                          media_type_not_allowed, 422 artifact_too_large);
                                          ответ + sizeBytes, mediaType, sha256, contentState
                                          none|stored|purged, typeVersion (null у
                                          незарегистрированного type))
GET  /api/v1/artifacts                    artifacts.read  (?taskId=&runId=&workspaceId=&type=;
                                          с taskId — право на этой задаче, с workspaceId — на
                                          воркспейсе, без них — уровня tenant)
GET  /api/v1/artifacts/{id}               artifacts.read  (CP-ADR-0072: на задаче артефакта,
                                          без задачи — на его воркспейсе, без обоих — tenant;
                                          ?forTask=<ref> — как вход задачи-получателя:
                                          tasks.read на ней и артефакт — её разрешённый
                                          вход, иначе обычная проверка; исполнитель
                                          скилла — skills.execute и artifacts.read на
                                          workspace задачи артефакта, K012)

Содержимое артефактов (CP-ADR-0072):
PUT  /api/v1/artifact-contents            artifacts.write (тело — байты файла, Content-Type —
                                          media type, обязателен; поток во временный файл,
                                          лимит CP_ARTIFACT_MAX_BYTES = 100 МБ, сверх — 413
                                          request_too_large; ответ 201 {contentRef,
                                          sizeBytes, mediaType, sha256, expiresAt}; ссылаться
                                          на contentRef может только загрузивший, 24 ч;
                                          Idempotency-Key не поддерживается; хранилище
                                          выключено или недоступно — 503
                                          content_store_unavailable)
GET  /api/v1/artifacts/{id}/content       artifacts.read на задаче артефакта |
                                          ?forTask=<ref> с tasks.read на задаче-получателе |
                                          skills.execute и artifacts.read на workspace
                                          задачи артефакта (исполнитель скилла, K012)
                                          (поток байтов;
                                          Content-Type = mediaType, ETag "sha256:<hex>",
                                          X-Content-Type-Options: nosniff, Cache-Control:
                                          private, no-store; Content-Disposition:
                                          attachment для активного содержимого — HTML, SVG,
                                          XML, JavaScript; без Range; 404 content_not_found
                                          у артефакта без содержимого, 410 content_purged,
                                          503 content_store_unavailable; каждая выдача —
                                          событие artifact.content_read)
POST /api/v1/artifacts/{id}:purge-content admin (body: reason 1..2000; запись остаётся с
                                          contentState=purged, объект удаляется, если на
                                          него не ссылаются другие артефакты tenant со
                                          stored и неистёкшие загрузки; повтор —
                                          200 без нового события; без содержимого — 409
                                          content_not_stored; событие artifact.content_purged)

Типы артефактов (CP-ADR-0072):
POST /api/v1/artifact-types               artifact_types.manage (следующая версия ключа, версию
                                          выдаёт сервер; key, displayName, description?,
                                          metadataSchema (JSON Schema ≤ 16 KiB, по умолчанию
                                          {}), mediaTypes ["type/sub" | "type/*" | "*/*"]
                                          (1…50), maxBytes ≤ CP_ARTIFACT_MAX_BYTES (по
                                          умолчанию — он); иначе 422 invalid_artifact_type
                                          с details.field; версия неизменяема; событие
                                          artifact_type.created)
GET  /api/v1/artifact-types               artifact_types.read (?key=&status=)
GET  /api/v1/artifact-types/{key}[@version]  artifact_types.read (без версии — последняя;
                                          нет такой — 404)

Типы подключений (CP-ADR-0079 п.2):
POST  /api/v1/connection-types            connections.manage ({key, version, spec} — spec
                                          объекта каталога вида ConnectionType: displayName,
                                          description?, auth [oauth2|token], oauth2
                                          {authorizeUrl, tokenUrlTemplate, accountParam?,
                                          authStyle, scopes}, accountField {title,
                                          description?, pattern}, settingsSchema, defaultKey;
                                          пара (key, version) неизменяема: тот же spec по
                                          sha256 канонического JSON — 200 без события, другой —
                                          409 connection_type_version_exists; новая — 201 и
                                          событие connection_type.published; нарушение формы —
                                          422 invalid_connection_type с details.field,
                                          неизвестное поле — 400 invalid_request; имя свойства
                                          settingsSchema вроде password или материал, похожий
                                          на секрет, в любой строке spec — 422
                                          secret_material_rejected с details {field: "spec",
                                          errors: [{path, match}]}: path — JSON Pointer внутри
                                          spec, для имени члена — путь объекта, значения в
                                          ответе нет (амендмент 2026-10-03); хост tokenUrlTemplate —
                                          внешнее DNS-имя, {account} — целые метки)
GET   /api/v1/connection-types            connections.read (?key=&status=&package=; новые
                                          первыми)
GET   /api/v1/connection-types/{key}[@version]
                                          connections.read (без версии — свежайшая active;
                                          нет такой — 404; ETag "connection-type-<rowVersion>";
                                          незаданное необязательное поле spec — null)
PATCH /api/v1/connection-types/{key}@{version}
                                          connections.manage (If-Match, body {status}; только
                                          вперёд active → deprecated → disabled, иначе 409
                                          invalid_status_transition; тот же статус — 200 без
                                          изменений; без версии в пути — 400)
PUT   /api/v1/connection-types/{key}/oauth-app
                                          connections.manage ({clientId, clientSecret} —
                                          секрет транзитом в kv/data/platform/oauth-apps/<key>
                                          хранилища с cas прочитанной версии; ответ {type,
                                          configured, clientId, updatedAt} без секрета; нет
                                          oauth2 ни в одной версии типа — 422
                                          auth_not_supported; документ другого tenant'а — 409
                                          oauth_app_owned_by_other_tenant; проигранный cas —
                                          409 oauth_app_write_conflict (retryable); событие
                                          connection_type.oauth_app_set {type, created})
GET   /api/v1/connection-types/{key}/oauth-app
                                          connections.read ({type, configured, clientId?,
                                          updatedAt?}; приложение другого tenant'а — configured
                                          false)

Подключения (CP-ADR-0079 п.3, п.4; материала доступа эти маршруты не касаются):
POST  /api/v1/connections                 connections.manage ({type, key?, displayName?,
                                          settings?}; тип — свежайшая active версия, нет её —
                                          422 unknown_connection_type; key по умолчанию —
                                          defaultKey типа, displayName — имя типа, settings —
                                          {}; settings по settingsSchema версии — иначе 422
                                          invalid_connection_settings с details {field:
                                          "settings", errors: [{path, code, message}]} — все
                                          нарушения (до 50), path — JSON Pointer внутри
                                          settings, code — ключевое слово JSON Schema, message
                                          без значения; имя ключа вроде password или материал
                                          секрета в строке или имени члена — 422
                                          secret_material_rejected с details {field:
                                          "settings", errors: [{path, match}]}, все находки,
                                          для имени — путь объекта (/ — корень), значения нет
                                          (CP-ADR-0079 амендмент 2026-10-03); занятый ключ — 409
                                          connection_key_taken: вторая учётка того же типа —
                                          другой key; 201 со status pending и событие
                                          connection.created)
GET   /api/v1/connections                 connections.read (?type=&status=; новые первыми;
                                          без agents)
GET   /api/v1/connections/{key}           connections.read (+ agents — ключи агентов, чья
                                          текущая ревизия называет подключение; ETag
                                          "connection-<version>"; чужое и несуществующее — 404)
PATCH /api/v1/connections/{key}           connections.manage (If-Match; {displayName?,
                                          settings?, typeVersion?}, хотя бы одно, null — 400;
                                          settings заменяются целиком и проверяются схемой
                                          версии после правки — ошибки в той же форме, что у
                                          POST; typeVersion — не disabled версия
                                          того же типа, иначе 422 unknown_connection_type;
                                          неверный If-Match — 409 version_conflict; те же
                                          значения — 200 без версии и события; иначе событие
                                          connection.updated с именами полей)
PUT   /api/v1/connections/{key}/status    connections.status.write ({status: active|expired,
                                          reason?, message?, checkedAt}; вызывающий — агент
                                          реестра, в spec.connections текущей ревизии которого
                                          есть key, иначе 403 connection_not_assigned —
                                          и для несуществующего key; active на active и
                                          expired на expired сдвигают только lastCheckedAt;
                                          expired на active — переход, reason обязателен (422
                                          status_reason_required), message без материала,
                                          событие connection.status_changed; остальное — 409
                                          connection_status_conflict; checkedAt старше
                                          lastCheckedAt — 409 stale_status_report)

Доступ подключения и секреты агентов (CP-ADR-0079 п.6, п.7, п.11; материал идёт транзитом
в хранилище секретов, ни в ответ, ни в базу, ни в журнал не попадает; хранилище не
настроено, недоступно или запечатано — 503 secret_store_unavailable, ничего не
изменилось):
POST  /api/v1/connections/{key}:authorize connections.manage ({}; тип без oauth2 — 422
                                          auth_not_supported; приложение типа не записано
                                          tenant'ом — 409 oauth_app_not_configured; пустые
                                          CP_OAUTH_REDIRECT_URI/CP_CONNECTIONS_RETURN_URL — 409
                                          oauth_not_configured с details.missing, не https —
                                          то же с details.insecure; 200
                                          {authorizeUrl, expiresAt}, Cache-Control: no-store;
                                          state 256 бит в базе только SHA-256, живые state
                                          подключения гасятся; повтор Idempotency-Key —
                                          authorizeUrl null; статус не меняется)
GET   /api/v1/connections:callback        публичный — одноразовый state (параметры задаёт
                                          провайдер, неизвестные не отвергаются); state
                                          гасится первым; неизвестный, погашенный, истёкший —
                                          303 ?result=invalid_state без записи; иначе
                                          полномочиями credential'а, выпустившего state:
                                          учётка из accountParam (≤ 253 символов, затем шаблон
                                          типа), сервер попытки и обмен кода в oauth2/
                                          хранилища под блокировкой подключения, прежний
                                          сервер удаляется после успеха, сервер неудачной
                                          попытки — сразу; active и событие
                                          connection.authorized; отказ
                                          согласия, ошибка провайдера или обмена, учётка,
                                          отозванный credential — статус прежний, statusReason
                                          и событие connection.authorization_failed; ответ 303
                                          на CP_CONNECTIONS_RETURN_URL ?connection=&result=
                                          &reason= (пустой или не https — 200 text/plain),
                                          no-store,
                                          no-referrer)
PUT   /api/v1/connections/{key}/token     connections.manage ({account, token, expiresAt?};
                                          тип без token — 422 auth_not_supported; учётка не по
                                          шаблону — 422 invalid_account; expiresAt в прошлом —
                                          422 invalid_expiry; token транзитом в
                                          kv/data/tenants/<t>/connections/<key>; поверх oauth2
                                          сначала удаляются creds и сервер; 200 ConnectionOut,
                                          событие connection.authorized с auth token; отпечаток
                                          Idempotency-Key — без token)
POST  /api/v1/connections/{key}:revoke    connections.manage ({reason?} ≤ 500 или без тела;
                                          сначала хранилище: creds и OAuth-сервер или документ
                                          kv со всеми версиями (DELETE kv/metadata), затем
                                          синхронно политики агентов, называющих подключение;
                                          потом учёт: status revoked, auth/secretRef/expiresAt
                                          null, statusMessage — reason после редакции, живые
                                          state гасятся, событие connection.revoked
                                          {key, type, previousStatus}; сбой хранилища — 503
                                          secret_store_unavailable, статус прежний, повтор
                                          доводит; повтор отозванного — 200 без события)
GET   /api/v1/agents/me/connections       аутентификация (подключения из spec.connections
                                          текущей ревизии агента вызывающего:
                                          {items: [AgentConnectionOut]} — key, type,
                                          typeVersion, account, auth, status, settings,
                                          secretRef, expiresAt; материала нет; не агент — 404)
GET   /api/v1/agents/me/connections/{key} аутентификация (AgentConnectionOut; ключ вне
                                          spec.connections и несуществующий — одинаково 404)
PUT   /api/v1/agents/{key}/secrets/{name} agents.secrets.manage ({value} — строка 1…65536;
                                          имя ^[a-z0-9][a-z0-9-]{0,62}$, иначе 422
                                          invalid_secret_name; агента нет или он чужого
                                          tenant'а — 404, выведен из оборота — 409
                                          agent_retired; value транзитом в
                                          kv/data/tenants/<t>/agents/<key>/<name> ({value},
                                          одна версия); в базе только имя, кто и когда; 201
                                          новое имя, 200 замена — {name, updatedAt,
                                          updatedBy}; событие agent.secret_set {agentKey,
                                          name, created}; отпечаток Idempotency-Key — без
                                          value; путь агента входит в его политику после
                                          сведения воркером)
GET   /api/v1/agents/{key}/secrets        agents.read ({items: [{name, updatedAt,
                                          updatedBy}]} по имени; значений нет, хранилище не
                                          нужно)
DELETE /api/v1/agents/{key}/secrets/{name} agents.secrets.manage (документ со всеми
                                          версиями — DELETE kv/metadata, затем имя; 204,
                                          событие agent.secret_deleted {agentKey, name}; имени
                                          нет — 404; выведен из оборота — 409 agent_retired:
                                          секреты такого агента удаляет воркер по списку
                                          kv/metadata хранилища, не по таблице имён;
                                          значения активного агента без имени — полный
                                          проход, CP-ADR-0079 п.11)

Реестр агентов (CP-ADR-0073):
POST /api/v1/agents                       agents.manage (body {key, spec, package?} — spec объекта
                                          каталога вида Agent; ревизия только при отличии
                                          sha256 канонического JSON spec без state и
                                          placement.replicas: 201 новая, 200 без изменений;
                                          state/replicas из spec — желаемое состояние;
                                          права ревизии ⊆ права применяющего — 403
                                          permission_escalation, роли/capabilities/
                                          skills.invoke — org.manage, непустой
                                          spec.connections (0…20 уникальных ключей
                                          подключений) — connections.manage
                                          (details.path = "spec.connections");
                                          422 invalid_permissions|permissions_not_allowed_for_kind|
                                          unknown_reference|secret_material_rejected|
                                          skill_disabled|skills_invoke_not_permitted|
                                          execution_skill_not_invoked; 409
                                          agent_retired; событие agent.revision_published;
                                          skills.invoke связанного агента приводится в
                                          principal_skills: skill.assigned|revoked;
                                          spec.executor.image — ссылка на образ OCI с
                                          тегом или дайджестом, ядро проверяет только
                                          форму: неверная — 400 invalid_request,
                                          CP-ADR-0073 З1–З3)
POST /api/v1/agents:validate              agents.manage (те же проверки без записи; ответ
                                          {key, specHash, currentRevision,
                                          wouldCreateRevision, wouldChangeState}; отказ — та же
                                          ошибка, что у POST /agents)
GET  /api/v1/agents                       agents.read (?status=active|retired&state=
                                          running|stopped&workspaceId=&include=status;
                                          include=status — у каждого элемента observedStatus,
                                          тело GET …/{key}/status, одним запросом на страницу)
GET  /api/v1/agents/me                    аутентификация (агент вызывающего с текущей
                                          ревизией; не агент — 404; ретайрнутый — status
                                          retired; AgentMeOut: и packageSettings
                                          {package, version, schemaRevision, values} —
                                          настройки пакета агента, null — агент не из
                                          пакета или пакет без настроек, CP-ADR-0081 §8)
GET  /api/v1/agents/{key}[@revision]      agents.read (без ревизии — текущая; AgentOut:
                                          state, replicas, currentRevision, revision{spec,
                                          specHash, createdBy}, principalId)
PATCH /api/v1/agents/{key}/state          agents.manage ({state?, replicas?}; ревизию не
                                          создаёт; событие agent.state_changed)
POST /api/v1/agents/{key}:retire          agents.manage ({reason}; state stopped, связки
                                          отозваны, principal disabled, claim'ы отпущены;
                                          событие agent.retired)
PUT  /api/v1/agents/{key}/identity        agents.status.write ({issuer, iamTenantId,
                                          iamPrincipalId}; ядро заводит principal, роли и
                                          связку по ревизии идемпотентно: повтор — 200 без
                                          изменений; другая идентичность — 409
                                          agent_identity_conflict; issuer не CP_IAM_ISSUER —
                                          422 iam_issuer_untrusted)
POST /api/v1/agents/{key}/identity:replace agents.manage ({issuer, iamTenantId,
                                          iamPrincipalId, reason}; только identity.kind
                                          service: principal тот же, прежняя связка
                                          и все прочие связки principal отозваны, новая —
                                          с правами текущей ревизии; issuer не
                                          CP_IAM_ISSUER (без него — не issuer агента) —
                                          422 iam_issuer_untrusted;
                                          права ревизии ⊆ права вызывающего; та же
                                          идентичность — 200 без изменений; не service —
                                          409 agent_identity_conflict; не привязан — 409
                                          agent_identity_not_linked; событие
                                          agent.identity_replaced)
GET  /api/v1/agents/{key}/revisions       agents.read (?limit=&cursor=; от новых к старым,
                                          без spec: revision, specHash, createdBy, createdAt,
                                          source{kind package|manual|unknown, package{key,
                                          version}}, active, changedFields — поля spec,
                                          изменённые против N−1, у ревизии 1 null)
GET  /api/v1/agents/{key}/status          agents.read (phase unknown до первого отчёта)
PUT  /api/v1/agents/{key}/status          agents.status.write ({phase, reason?,
                                          observedRevision?, node?, instances{desired, ready},
                                          observedAt}; старше сохранённого — 409
                                          stale_status_report; событие agent.status_changed
                                          только при изменении phase/reason/node/ревизии)

Процессы, календари, пакеты (CP-ADR-0074; контракт опубликован, до шага фичи
process-packages маршрут отвечает 501 not_implemented с details.implementedBy):
POST /api/v1/process-definitions          processes.write (на workspace процесса, если есть
                                          spec.workspaceId; body {key, spec} — spec объекта
                                          каталога вида Process; версия spec.version
                                          неизменяема: тот же хэш — 200, другое содержимое
                                          или версия не больше последней — 409
                                          process_version_conflict (details.latestVersion);
                                          422 invalid_process с details.problems[code,
                                          severity, path, file, line, message, hint]; права
                                          агента личности шире публикующего — 403
                                          permission_escalation; предупреждения — warnings
                                          версии; событие process.definition_published)
                                          — реализован
GET  /api/v1/process-definitions          processes.read (последняя версия каждого ключа, по
                                          key; ?key=&workspaceId=&governedBy=<ключ
                                          документа>&limit=&cursor=; governedBy — процессы,
                                          чья последняя версия ссылается на документ, с
                                          workspaceId и owner версии, CP-ADR-0076 п.7;
                                          ?status=active|retired — только в обороте или
                                          только выведенные, без него — все)
                                          — реализован
GET  /api/v1/process-definitions/{key}[@version]  processes.read (ProcessDefinitionOut с
                                          owner, warnings, status active | retired и
                                          retired {at, by, reason} | null — у каждой
                                          версии ключа; 404, если нет) — реализован
GET  /api/v1/process-definitions/{key}/versions   processes.read (ProcessVersionOut —
                                          без spec, новые первыми; ?limit=&cursor=)
                                          — реализован
POST /api/v1/process-definitions/{key}:replay     packages.test + processes.read ({spec,
                                          instanceIds?, limit ≤ 200, по умолчанию 50};
                                          журналы instanceIds или последних limit
                                          экземпляров текущей версии из читаемых workspace
                                          → кандидат под номером версии экземпляра; ответ
                                          200 {key, candidateHash, replayed, diverged,
                                          problems, instances[instanceId, instanceKey,
                                          version, events, divergences[journalSeq, kind
                                          decision | intent | input | settings | data |
                                          timer | state, element, recorded, replayed]]};
                                          settings — версии или ревизии схемы настроек
                                          записи нет в базе (recorded {version,
                                          schemaRevision}, CP-ADR-0081 §6) — у экземпляра не
                                          больше одного, первое; кандидат с ошибкой проверки —
                                          problems и пустой instances; нет процесса или
                                          экземпляра этого процесса — 404, экземпляр вне
                                          читаемых workspace — 403; транзакция только
                                          на чтение, память не зовётся) — реализован
POST /api/v1/process-definitions/{key}:retire     processes.write (на workspace последней
                                          версии; {reason 1…500}; ?dryRun=true — те же
                                          проверки и ответ без записи; все версии ключа
                                          выводятся: новые экземпляры не заводятся ни
                                          событием старта, ни POST /process-instances
                                          (409 process_retired), ни call (intent_failed
                                          process_retired у родителя), открытые доживают;
                                          200 {key, status retired, retired {at, by,
                                          reason}, openInstances, byVersion[version,
                                          openInstances]} — живые всех workspace; повтор —
                                          200 с первым выводом без события; 404, если нет;
                                          событие process.definition_retired; новая версия
                                          ключа возвращает его в оборот; CP-ADR-0074 Ж2)
                                          — реализован
POST /api/v1/process-instances            processes.operate (на workspace экземпляра;
                                          {process, key, data?, workspaceId?} — старт без
                                          события: ключ и данные заданы, data по схеме
                                          данных процесса, иначе 422 invalid_process_data;
                                          повтор ключа — 409 process_instance_exists с
                                          details.instanceId; выведенный процесс (:retire
                                          или переименование пакетом) — 409
                                          process_retired; ждёт вывод или публикацию
                                          ключа в полёте и стартует на версии после
                                          неё; первый шаг делает call, а ключ дочернего
                                          процесса занят (применение пакета, публикация
                                          или вывод) — 503
                                          catalog_key_busy (details {kind, key}),
                                          ничего не записано, повторить;
                                          201 ProcessInstanceOut; process.started с
                                          triggerType command) — реализован
GET  /api/v1/process-instances            processes.read (новые первыми; ?definitionKey=
                                          &instanceKey=&status=&workspaceId=&limit=&cursor=
                                          &slaState=breached|warning — до проекции сроков
                                          SLA пусто, иное значение — 400) — реализован
GET  /api/v1/process-instances/{id}       processes.read (данные, стадии, открытые элементы
                                          с задачей и approvals ожидания, ожидающие и
                                          замороженные таймеры; поля сроков SLA по
                                          CP-ADR-0078 §6: у шага attempt, due, slaState,
                                          overdueSeconds, у экземпляра sla и slaState —
                                          пока null) — реализован
GET  /api/v1/process-instances/{id}/journal  processes.read (журнал решений по шагам: вход,
                                          решения, намерения; seq, at, kind, element, reason,
                                          actorId, eventId, data; ?kind=&limit=&cursor=;
                                          data входа шага процесса, читающего settings, —
                                          ещё settingsVersion и settingsSchemaRevision,
                                          CP-ADR-0081 §6) — реализован
POST /api/v1/process-instances/{id}:suspend  processes.operate ({reason}; таймеры
                                          замораживаются; не running — 409
                                          invalid_process_instance_state) — реализован
POST /api/v1/process-instances/{id}:resume   processes.operate ({reason?}; не suspended —
                                          409; шаг делает call, а ключ дочернего
                                          процесса занят (применение пакета, публикация
                                          или вывод) — 503
                                          catalog_key_busy, ничего не записано) — реализован
POST /api/v1/process-instances/{id}:cancel   processes.operate ({reason, compensate=true};
                                          закрытый или уже отменяемый — 409; шаг
                                          компенсации делает call, а ключ дочернего
                                          процесса занят — 503 catalog_key_busy)
                                          — реализован
POST /api/v1/calendars                    calendars.write ({key, spec} вида Calendar; версия
                                          по хэшу spec с годами и датами по порядку: 201
                                          новая, 200 без изменений; событие
                                          calendar.published; 422 invalid_calendar с
                                          details.code unknown_timezone |
                                          duplicate_calendar_year |
                                          calendar_date_outside_year |
                                          calendar_day_conflict и details.path) — реализован
GET  /api/v1/calendars                    аутентификация (последняя версия каждого ключа,
                                          по key; ?limit=&cursor=&status=active|retired)
                                          — реализован
GET  /api/v1/calendars/{key}[@version]    аутентификация (CalendarOut со status и retired,
                                          как у процесса; 404, если нет) — реализован
POST /api/v1/calendars/{key}:retire       calendars.write ({reason 1…500}; ?dryRun=true;
                                          409 calendar_in_use, пока календарь нужен
                                          последней версии невыведенного процесса или
                                          версии живого экземпляра любого процесса:
                                          details {calendar, processes[key, version,
                                          openInstances] ≤ 50 — без процессов, которые
                                          вызывающему не читать, total}; 200 {key, status
                                          retired, retired}; повтор — 200 с первым
                                          выводом; 404, если нет; событие
                                          calendar.retired; новая версия процесса со
                                          ссылкой на выведенный календарь — 422
                                          invalid_process calendar_retired; новая версия
                                          календаря возвращает его; CP-ADR-0074 Ж3)
                                          — реализован
POST /api/v1/packages:test                packages.test ({package: {files[{path,
                                          content}]}, tests?, workspaceId?}; ?checkOnly=true —
                                          только проверка; песочница, транзакция только на
                                          чтение; ответ 200 status passed | failed |
                                          invalid, problems с file и line, tests с провалами
                                          по шагам, coverage — elements, transitions,
                                          decisionRows, handlers с missing; workspaceId —
                                          ещё processes.read на него, нет — 404;
                                          given.fromInstance теста — пробный прогон с копии
                                          живого экземпляра, читается как GET
                                          /process-instances/{id}: без processes.read на
                                          его workspace — 403, неизвестный — 404;
                                          тест subject: rule | taskType — код ядра
                                          в откатываемой транзакции (CP-ADR-0074
                                          З1–З6): tests[].subject, object, process
                                          (null у rule и taskType); ruleCoverage
                                          (branches, outcomes) и taskTypeCoverage
                                          (outcomes, preconditions, completion,
                                          acceptance); проверка формы WorkRule и
                                          TaskType — invalid_rule, invalid_task_type;
                                          без права вида — предупреждение
                                          permission_required и status error у
                                          тестов rule и taskType; test_field_ignored
                                          — предупреждение; каждый тест rule и
                                          taskType — своя транзакция, не больше
                                          100 на запрос — иначе too_many_tests;
                                          ошибки теста statement_timeout,
                                          test_timeout; предупреждения
                                          input_not_delivered,
                                          unmocked_skill_call — З7)
                                          — реализован
POST /api/v1/packages:plan                packages.plan ({package, workspaceId?,
                                          replayLimit=50, overwriteConsole=false}; виды
                                          TaskType, Agent, Calendar, Process, WorkRule
                                          (CP-ADR-0074 п.11, амендмент 2026-09-29), View
                                          (CP-ADR-0080: проверка вида и словарей с путём,
                                          Component встраивается в вид, retire — вид,
                                          который пакет больше не приносит), прочие
                                          виды пакета — в outside; ничего не пишет; changes —
                                          create | update | rename | restore | unchanged
                                          (restore — выведенный процесс или календарь
                                          без изменений возвращается в оборот) с полями
                                          before/after и владельцем package | console
                                          (поле консоли не перетирается без
                                          overwriteConsole); processes — replay на
                                          replayLimit экземплярах и судьба открытых по
                                          версиям pin | migrate | unaffected с
                                          migrationRequired; regulationCoverage — разделы
                                          регламента из памяти (узлы section_of) с
                                          элементами и непокрытые; problems с file и line;
                                          planHash, catalogEtag; workspaceId — подстановка
                                          ${…} в spec.workspaceId и processes.read на него;
                                          processes[].deadlines — сроки, которые миграция
                                          ставит, сдвигает, снимает или находит уже
                                          прошедшими, — шаг migrated на копии
                                          состояния, в planHash не входит; не больше
                                          200 на процесс, все — deadlinesTotal;
                                          settings — раздел настроек пакета
                                          (CP-ADR-0081 §7): schemaRevision {before,
                                          after}, added [{path, default?}], removed
                                          [{path, saved}], incompatible [{path, code}] —
                                          каждое ещё и ошибка settings_incompatible,
                                          uischemaChanged; null — пакет настроек не
                                          объявлял и не объявляет; входит в planHash,
                                          ревизия схемы и версия значений — в catalogEtag)
                                          — реализован
POST /api/v1/packages:apply               packages.plan + право вида каждого изменения
                                          (task_types.manage, agents.manage,
                                          calendars.write, processes.write, rules.write)
                                          ({package,
                                          planHash, workspaceId?, overwriteConsole?}; план
                                          строится заново под блокировкой: другой хэш —
                                          409 plan_stale с details.currentPlanHash и
                                          catalogEtag; 422 migration_required; иные ошибки
                                          плана — 422 invalid_package; одна транзакция:
                                          календари, процессы, перенос экземпляров
                                          migrate с process.migrated, вывод
                                          переименованного ключа, restore выведенного;
                                          календарь, нужный возвращаемому процессу,
                                          выведен после плана — 409 calendar_retired с
                                          details {process, calendars}, ничего не
                                          применено; ответ applied[kind, key,
                                          action, version] и catalogEtag после; новая
                                          ревизия схемы настроек пакета — активной,
                                          прежняя — нет; версия значений не меняется,
                                          CP-ADR-0081 §7) — реализован
POST /api/v1/packages:record              packages.plan + право записи каждого вида
                                          (CP-ADR-0074 Е2; {package: {key, version},
                                          installHash?, objects: [{kind, key}]} — объекты
                                          видов ArtifactType, TaskType, ProjectTemplate,
                                          WorkspaceType, Role (tenant), Capability,
                                          ConnectionType (право connections.manage), Skill,
                                          WorkRule, Agent, которые установщик применил, и
                                          неизменившиеся тоже; связь объекта переходит на эту
                                          версию пакета; Process/Calendar связывает
                                          packages:apply — 400; TaskType, Agent, WorkRule
                                          — переходный путь установщика (Е6), запись
                                          очищает spec прошлого apply; объекта нет в каталоге —
                                          422 unknown_object, ничего не пишется; ответ
                                          {package, installHash, recorded})

GET  /api/v1/views                       роль из audience вида (если есть) и право читать
                                          его источник (processes.read | tasks.read |
                                          events.read), CP-ADR-0080; ?locale=&package=
                                          &limit=&cursor=; строки на языке запроса, его
                                          базовом языке или defaultLocale пакета; форма,
                                          согласованная с консолью (CP-ADR-0080 §9):
                                          {items: [ViewSummary], nextCursor}, ViewSummary
                                          {key, title, description?, revision, hash,
                                          blocks: 1, locale, package, nav? {group: work |
                                          knowledge | packages, icon?, order?}, source
                                          {kind, process?, instance}} — без layout
GET  /api/v1/views/{view_key}             ViewOut = ViewSummary + layout: блоки с
                                          дискриминатором block — ключи, подписи и форматы
                                          колонок, полей и показателей, фильтры с типом из
                                          схемы данных; без путей, CEL и сырого source;
                                          невидимый, выведенный и несуществующий — 404
                                          not_found
POST /api/v1/views/{view_key}:query       данные одного блока вида источника process
                                          (CP-ADR-0080, амендмент А1–А8); видимость — как у
                                          GET /views/{view_key}, экземпляры — в workspace с
                                          processes.read; ?locale=; тело {block, params?,
                                          filter? [{field, op: eq|in|gte|lte|prefix,
                                          value}], sort? [{field, dir}], limit? ≤200,
                                          cursor?}; field — только объявленные блоком
                                          фильтры и сортировки (иначе 422
                                          undeclared_filter | undeclared_sort); ответ по
                                          блоку: table/list {items: [{id, title, values}],
                                          nextCursor}, board {columns}, metrics {values},
                                          chart {points}, header {title, status}, fields
                                          {values}, steps|timeline|artifacts|related
                                          {items}; поля data, которых вид не показывает, не
                                          отдаются; экземпляр чужого workspace или другого
                                          процесса — 404; вид tasks/knowledge — 501; больше
                                          20 000 экземпляров, вычисленных запись за записью
                                          (непереводимый CEL), — 409 view_too_costly
GET  /api/v1/package-settings            packages.settings.read (CP-ADR-0081 §4);
                                          ?locale=; {items: [{package, title,
                                          packageVersion, version, updatedBy, updatedAt}]}
                                          — пакеты с активной ревизией схемы настроек, по
                                          ключу; version 0 — ничего не сохраняли
GET  /api/v1/packages/{key}/settings      packages.settings.read; ?locale=; {package,
                                          title, packageVersion, schema, uischema|null,
                                          values, effective, version, schemaHash,
                                          updatedBy, updatedAt, canManage}; строки словаря
                                          пакета — прямо в schema (title, description) и
                                          uischema (label, text); ETag
                                          "package-settings-<version>"; пакета нет — 404
                                          package_not_installed, настроек не объявляет —
                                          404 settings_not_declared
PUT  /api/v1/packages/{key}/settings      packages.settings.manage; If-Match
                                          "package-settings-<version>" обязателен (428, 400);
                                          тело {values} целиком; ?locale= для строк ответа;
                                          порядок отказов: 404, 422
                                          secret_material_rejected {details.errors[{path,
                                          match}]} без значения, 422 settings_invalid
                                          {details.errors[{path, code, message}]}, 422
                                          unknown_ref {details.errors[{path, ref}]}, 409
                                          version_conflict {details.currentVersion}; те же
                                          значения — 200 без новой версии; иначе версия,
                                          строка истории и package.settings_changed (без
                                          значений); ответ — как у GET, новый ETag
GET  /api/v1/packages/{key}/settings/versions
                                          packages.settings.read; ?limit= (1…100, 50)
                                          &cursor=; {items: [{version, values,
                                          changedPaths, updatedBy, updatedAt}],
                                          nextCursor}, новые первыми

Привязка к пакету (CP-ADR-0074 Е3): списки и карточки task-types, artifact-types,
project-templates, workspace-types, roles, capabilities, connection-types, skills,
rules, agents, process-definitions, calendars и views отдают package {key, version, installHash,
installedAt} — пакет, который поставил объект (ключ, все его версии), или null у
созданного вручную; списки этих видов принимают ?package=<key>.

POST /api/v1/approvals                    approvals.manage (ровно одно из requiredRoleId |
                                          assignedPrincipalId; gate=true требует task и
                                          блокирует claim/complete до решения;
                                          excludedPrincipals — кому решать нельзя, CP-ADR-0074
                                          п.7: до 100 principal tenant'а, повторы схлопываются,
                                          неизвестный — 404, исключён assignedPrincipalId —
                                          422 invalid_approval; ответ и approval.requested v3
                                          несут список, [] — никто не исключён)
GET  /api/v1/approvals[/{id}]             approvals.read   (?status=&taskId=)
POST /api/v1/approvals/{id}:approve       approvals.decide + eligibility (assigned или роль;
                                          principal из excludedPrincipals — 403
                                          separation_of_duties_violation при любой роли);
                                          gate, тип которого объявляет preconditions.approved:
                                          пока хоть одно не выполнено — 409
                                          approval_precondition_failed (details.failed[index,
                                          kind, cause, reason, observationId?]), решение не
                                          записывается (ADR-0061, амендмент 2 от 2026-09-25)
POST /api/v1/approvals/{id}:reject        approvals.decide + eligibility (как у :approve)
POST /api/v1/approvals/{id}:cancel        approvals.manage; для gate=true и для approval
                                          с excludedPrincipals дополнительно автор
                                          запроса ИЛИ approvals.decide + eligibility
                                          решателя (иначе 403 not_eligible или
                                          separation_of_duties_violation); исключённый
                                          (и агент с его делегированием) — 403
                                          separation_of_duties_violation всегда
                                          (CP-ADR-0074 §7)
GET  /api/v1/approvals/{id}/outcome       approvals.read   (ADR-0061: объявленные типом задачи
                                          действия исхода решения и что с каждым стало:
                                          outcome, outcomeStatus pending|deferred|executed|
                                          failed, attempts, lastError, nextAttemptAt,
                                          actions[index, action, status executed|failed|
                                          not_executed, attempts, result, error])
POST /api/v1/approvals/{id}:replay-outcome approvals.decide; решивший (его ТЕКУЩИЙ credential
                                          становится полномочием) или admin (полномочие
                                          решения, если credential ещё активен). Только при
                                          outcomeStatus=failed или зависшем pending (были
                                          неудачные попытки либо не тронут ≥10 мин), иначе
                                          409 outcome_not_replayable; продолжает с первого
                                          невыполненного действия, ответ — как у /outcome

Исходы и работа после завершения (CP-ADR-0061, амендмент 2026-09-25):
`ensureWork` принимает `customFields {поле: строка-выражение}` — значения
рендерятся, пустые опускаются, результат проверяется по `fieldSchema` версии
целевого типа (отказ — действие `custom_fields_invalid`, остальные действия
исхода остаются для replay), найденная по `key` задача полей не получает; и
`requestApproval {assignee, comment?}` — gate-approval на созданной задаче
(нужно `approvals.manage`); `assignee` — id principal'а или `role:<slug>`:
решает держатель роли, найденной из workspace задачи (литеральная ссылка без
роли у tenant'а — `422 unknown_role` при публикации, CP-ADR-0061, амендмент 4). `completionSchema` версии типа исполняется при
переходе задачи в `terminal_success` (`:complete`, `:succeed` run,
`completeTask` исхода) синхронно, с правами завершившего, один раз на задачу;
отказ действия не отменяет завершения — он записан событием
`task.completion_work_failed` и комментарием ядра на задаче.

GET  /api/v1/me/attention                 tasks.read ИЛИ approvals.read (CP-ADR-0071: «Важное»
                                          вызывающего — элементы правил ruleKey@version:
                                          approval.review, approval.decide,
                                          approval.undecidable (CP-ADR-0074 §7),
                                          task.due_not_started, task.blocked,
                                          task.delegated_failing; ?workspaceId=
                                          &includeDescendants=, нет workspace -> 404; ответ
                                          {items[{itemKey, kind, reasonCode, rule, score,
                                          entity, title, workspaceId, taskId, taskPublicId,
                                          dueDate, since, details, actions[{action, method,
                                          href}], feedback}], degraded[{rule, reasonCode,
                                          message}], generatedAt}; score убывает; правило
                                          без права чтения — degraded permission_missing,
                                          упавшее — rule_failed, > 100 элементов — truncated)
POST /api/v1/me/attention/{itemKey}:feedback  право чтения правила элемента ({verdict:
                                          useful|not_needed, comment? ≤ 1000}; элемент
                                          пересчитывается его правилом: нет в списке
                                          вызывающего, чужой или неизвестный ключ -> 404,
                                          правило не вычислилось -> 503
                                          attention_rule_unavailable; 201 — первый вердикт,
                                          200 — замена; список не меняет; событие
                                          attention.feedback_recorded)

GET  /api/v1/events                       events.read  (?cursor=<opaque>|after=<seq legacy>
                                          |before=<opaque>|tail=N, &order=asc|desc
                                          &entityType=&entityId=; ответ: items[]
                                          c cursor у каждого события, nextCursor,
                                          prevCursor, hasMore; before — назад,
                                          с cursor/after/tail -> 422 conflicting_cursors;
                                          CP-ADR-0068: types=<префикс>[,…] (повторяемый,
                                          до 20; иначе 422 invalid_event_type_filter),
                                          workspaceId= — поддерево workspace, events.read
                                          спрашивается на нём, нет workspace -> 404; у
                                          события workspaceId и schemaVersion; каталог
                                          типов — docs/events/catalog.md;
                                          CP-ADR-0068 Б1–Б4: &includeDescendants=true|false
                                          (без параметра — поддерево; false — только
                                          workspace; без workspaceId не влияет)
                                          &actorId=<principal> &occurredFrom= (>=)
                                          &occurredTo= (<) — ISO 8601 с зоной, без зоны
                                          или from > to -> 422 invalid_event_period;
                                          фильтры — пересечение, право прежнее);
                                          CP-ADR-0082 В7: вызывающему в members —
                                          события видимых пространств и tenant, событие
                                          без пространства — если taskId/runId/
                                          workspaceId/entityId его payload видимы; так
                                          же WS; фильтры Б1–Б4 сужают только внутри
                                          видимого (пересечение)
GET  /api/v1/events:export               events.export + events.read  (CP-ADR-0068
                                          В1–В5: ?format=jsonl|csv (обязателен) и
                                          фильтры GET /events — types, entityType,
                                          entityId, actorId, workspaceId,
                                          includeDescendants, occurredFrom/occurredTo
                                          (оба обязательны; нет -> 422
                                          export_period_required; длиннее 92 дней ->
                                          422 export_period_too_long); cursor/limit/
                                          tail/order нет (-> 400); больше 100 000
                                          событий -> 422 export_too_large
                                          {maxEvents} — до тела; пределы —
                                          events_export_max_period_days|max_events;
                                          200 — поток в порядке журнала до снимка на
                                          момент запроса: JSONL
                                          (application/x-ndjson) — тело элемента
                                          GET /events на строку; CSV (text/csv, CRLF) —
                                          заголовок и id, occurredAt, type,
                                          schemaVersion, actorId, entityType, entityId,
                                          workspaceId, payload (JSON-строка); заголовки
                                          X-Event-Count, Content-Disposition
                                          attachment, Cache-Control private, no-store;
                                          видимость как у GET /events (members — только
                                          видимое, невидимый workspaceId -> 404);
                                          событие event_journal.exported — фильтры и
                                          число событий, без данных, до тела; отказ не
                                          журналируется)
GET  /api/v1/event-types                  events.read  (CP-ADR-0068, амендмент А: каталог
                                          событий целиком, по имени, без страниц;
                                          ?locale= — цепочка видов (CP-ADR-0080 §5),
                                          строки ядра только en; ответ {locale,
                                          items[]}: запись типа из docs/events/
                                          catalog.json (entityType, description,
                                          currentVersion, versions{"<n>": {changes?,
                                          schema}}) + type, group (префикс до точки,
                                          как в types=), supportedVersions, labelKey
                                          event.<type> для словаря консоли; ETag
                                          "event-types-<sha256>", Cache-Control
                                          private, max-age=300; If-None-Match ->
                                          304 после проверки права)
WS   /api/v1/events/ws?after=<cursor>     events.read  (opaque-курсор или legacy int;
                                          те же types= и workspaceId=;
                                          4401 — нет credentials, 4403 — нет права,
                                          4404 — нет workspace фильтра,
                                          4400 — malformed cursor или фильтр)

POST /api/v1/observations                 observations.write  (v0.4 explicit remember:
                                          kind/content/data + task|runId|workspaceId|
                                          sessionId; provenance — из аутентификации;
                                          записывается событием observation.recorded;
                                          ADR-0057: source, dedupKey, observedAt,
                                          supersedes, externalRef{system,id,url} —
                                          source обязателен с dedupKey/externalRef;
                                          повтор (source, dedupKey) тем же автором в
                                          tenant → 200 с тем же id и deduplicated=true,
                                          первый — 201; автор — часть ключа (амендмент
                                          ADR-0057 2026-10-01): та же пара от другого
                                          автора — его собственное наблюдение, 201 и
                                          новое событие, ничего о чужом в ответе;
                                          supersedes на неизвестное наблюдение → 404;
                                          амендмент ADR-0057 TASK-001205: task|runId
                                          требует tasks.read на этой задаче — иначе 403
                                          permission_denied, событие не пишется и ключ
                                          не занимается; неизвестная или чужая задача
                                          → 404; CP-ADR-0082 В6: runId невидимой работы
                                          и supersedes невидимого наблюдения — тот же
                                          404, что у несуществующих)
POST /api/v1/context                      только аутентификация  (v0.4: authoritative
                                          operational context + durable memory ContextPack;
                                          query/task/runId/workspaceId/maxTokens/
                                          includeMemory; v0.5: projectId/includeSubprojects —
                                          operational.project с effectiveConfig,
                                          configProvenance и workspaceScope, projectId
                                          требует projects.read; при недоступной памяти —
                                          200 с memoryStatus, не 5xx; CP-ADR-0059:
                                          asOf — момент для point-in-time recall,
                                          передаётся в Memory только если задан;
                                          CP-ADR-0064: если тип задачи фокуса объявляет
                                          contextSchema — taskContext {status ok|empty|
                                          unavailable|timeout|disabled|forbidden,
                                          contextPackId, claimId, asOf, asOfMode,
                                          replayed, recorded, budgetTokens, anchors[],
                                          warnings[], pack}; pack урезан до budgetTokens
                                          (по умолчанию 3000, omitted{entities, facts});
                                          держатель активного claim записывает пакет
                                          (task_context_packs, событие
                                          task.context_pack_recorded; задача, её
                                          evidence и version не меняются; claim
                                          перепроверяется под блокировкой задачи),
                                          повтор в том же claim повторяет записанный
                                          запрос — после редакции якорей, как в GET
                                          /context-packs (redactedAnchors); тип без
                                          профиля — ключа taskContext нет;
                                          CP-ADR-0066: при фокусе на задаче или run —
                                          instructions {layers, hash}, тот же блок, что
                                          в GET /runs/{id}/context; CP-ADR-0072:
                                          operational.focus.inputs — входы задачи
                                          фокуса, как inputs в GET /runs/{id}/context)
POST /api/v1/context/recall               events.read (+ tasks.read при task)  (CP-ADR-0064,
                                          MCP cp_recall: ровно одно из anchor (≤300) /
                                          query (≤2000), иначе 422 invalid_recall_request;
                                          kind, kinds[≤20], relations[≤10], direction
                                          in|out|both (both), depth 1..5 (1), limit 1..200
                                          (20), asOf, task | workspaceId — namespaces и
                                          видимость выводит ядро, budgetTokens 1..32000
                                          (3000); where[≤20] — {attr, op
                                          eq|in|prefix|lte|gte|exists, value} как
                                          $defs/memoryWhere, значения — литералы (CEL
                                          не вычисляется), уходят в typed памяти как
                                          есть и отбирают якоря; неверное условие —
                                          400 invalid_request;
                                          query без идентификаторов —
                                          семантический добор (semantic: true); ответ
                                          {anchors, semantic, asOf, namespaces, warnings,
                                          pack (урезан до budgetTokens, omitted)}; 503
                                          memory_disabled / memory_timeout, 502
                                          memory_unavailable)
GET  /api/v1/context-packs/{id}           tasks.read на задачу пакета + events.read  (CP-ADR-0064:
                                          запись пакета — request, asOf, asOfMode,
                                          namespaces, anchors, used{entities, facts,
                                          snapshots}, unresolved, traceId, claimId,
                                          compiledBy, redactedAnchors: якоря из
                                          метаданных артефактов без artifacts.read и из
                                          задачи spawnedBy без tasks.read на неё
                                          вырезаны вместе с их значениями в request,
                                          unresolved и used.entities; пакет задачи
                                          шага процесса — якоря source=step, в
                                          request.semantic — чтение по сходству,
                                          найденное им — в used.entities, CP-ADR-0076 п.6)
POST /api/v1/context-packs/{id}:replay    tasks.read на задачу + events.read  (записанный
                                          запрос — после той же редакции — снова,
                                          видимостью вызывающего:
                                          {contextPack, reproduced, drift{missingEntities,
                                          extraEntities, missingFacts, extraFacts}, pack})
POST /api/v1/knowledge/snapshots         observations.write на workspace:<workspaceId>
                                          (ADR-0060: снимок коннектора как есть — pack (необяз.),
                                          source (≤200), scope (строка ≤200), snapshotId
                                          (≤200), observedAt, entities[] + relations[]
                                          (вместе ≤20000) + workspaceId; namespace
                                          tenant:<t>:ws:<корень дерева> и scope
                                          workspace:<workspaceId> выводит ядро, namespace/
                                          scopes в теле → 400; тело до 8 МиБ; в память —
                                          плоский документ снимка + namespace, scopes
                                          (ReconcileIn); ответ памяти как есть (200);
                                          422 snapshot_invalid — память отвергла снимок;
                                          409 snapshot_stale — память держит более новый
                                          снимок; 502 memory_unavailable; 503
                                          memory_disabled; событие
                                          knowledge.snapshot_reconciled без содержимого;
                                          expectedState (≤128, stateToken предпросмотра) —
                                          в память рядом со снимком: применить, только
                                          если состояние источника не изменилось, иначе
                                          409 snapshot_stale)
POST /api/v1/knowledge/snapshots:preview observations.write на workspace:<workspaceId>
                                          (ADR-0060 амендмент 2026-09-28: тело снимка без
                                          expectedState → память reconcile с dryRun: true;
                                          ответ памяти как есть: changes, счётчики,
                                          conflicts, stateToken; ничего не пишет, событий
                                          нет; 422 snapshot_invalid, 409 snapshot_stale —
                                          снимок старше принятого; ответ без плана
                                          (память без предпросмотра) — 502
                                          memory_unavailable, retryable false)
POST /api/v1/knowledge/documents         observations.write на workspace:<workspaceId>
                                          (ADR-0060 амендмент 2026-09-28: {workspaceId,
                                          naturalKey, title, type, chunks[1..500]{text,
                                          heading, order}, links[≤200]{kind, key, rel},
                                          meta}; файлы ядро не разбирает; namespace корня
                                          дерева, scope workspace:<id>; тело до 8 МиБ →
                                          память POST /api/brain/documents; ответ памяти
                                          как есть; 422 document_invalid; событие
                                          knowledge.document_stored без текста)
POST /api/v1/knowledge/entities:query    events.read на workspace:<workspaceId>
                                          (ADR-0060 амендмент K031: {workspaceId,
                                          kinds[1..20], where[≤20]{attr, op, value} —
                                          как у /context/recall, asOf, limit 1..500
                                          (100), cursor}; namespace корня дерева и
                                          видимость вызывающего выводит ядро
                                          (namespaces/scope в теле → 400); память POST
                                          /api/memory/entities:query; ответ {items
                                          ({kind, key, title, attributes, validFrom,
                                          validTo, sources[{source, sourcePath,
                                          snapshotId}]} + переходные snake_case-поля
                                          памяти, амендмент 2026-10-03 С2), nextCursor
                                          (null — конец перечня), asOf}; include:
                                          {relations[≤20] | "*", direction out|in|both
                                          (both), limit 1..200 (20) на запись} →
                                          relations[{relation, direction, kind, key,
                                          title}] у записи через /api/memory/context/
                                          typed, конец вне видимости не выдаётся,
                                          limit страницы ≤100 (С1); namespace
                                          корня вне видимости policy → 403; 422
                                          entities_query_invalid — чужой курсор; 502
                                          memory_unavailable; 503 memory_disabled /
                                          memory_timeout; событий нет)
POST /api/v1/knowledge/packs             администратор платформы: principal из
                                          CP_KNOWLEDGE_PACK_ADMINS, пусто → 403 (ADR-0060:
                                          манифест доменного пакета как есть → память POST
                                          /api/memory/packages; ответ памяти как есть;
                                          без name/version → 422 pack_invalid (ядро);
                                          422 pack_invalid, 409 pack_version_conflict;
                                          событие knowledge.pack_registered);
                                          scope: tenant — пакет арендатора по
                                          knowledge.packs.manage без списка администраторов
                                          (амендмент 2026-09-28, K010): ядро добавляет
                                          namespace-владельца tenant:<tenantId>; namespace
                                          в манифесте → 400; имя пакета, вида или связи
                                          занято общим пакетом → 422 pack_invalid с
                                          details.conflict; scope: common — то же, что без
                                          scope (общий пакет); иное значение scope → 400
PUT  /api/v1/workspaces/{id}/knowledge-packs
                                          workspaces.manage на workspace:<id>  (ADR-0060:
                                          {packs[], strict} → память PUT
                                          /api/memory/namespaces/{ns}/kinds; только ссылки
                                          name@version или tenant:name@version (пакет
                                          арендатора), иначе 422 pack_version_required;
                                          неизвестный пакет — 422 pack_not_found; только
                                          корень дерева, иначе 422 workspace_not_root;
                                          событие knowledge.packs_configured)
GET  /api/v1/workspaces/{id}/knowledge-packs
                                          workspaces.read или workspaces.manage на
                                          workspace:<id>  (ADR-0060 амендмент 2026-09-30:
                                          набор namespace корня дерева — для любого
                                          workspace дерева; память GET
                                          /api/memory/namespaces/{ns}/kinds; ответ
                                          {workspaceId, rootWorkspaceId, configured,
                                          packs, strict, effective, updatedAt}: packs и
                                          strict — как их принимает PUT, effective —
                                          пакеты, которые применяет память; configured
                                          false — набор ещё не задавали, действует пакет
                                          памяти по умолчанию; namespace не отдаётся;
                                          502 memory_unavailable; 503 memory_disabled;
                                          событий нет)
GET  /api/v1/knowledge/packs/{ref}       events.read или knowledge.packs.manage;
                                          общий пакет — ещё администратор платформы из
                                          CP_KNOWLEDGE_PACK_ADMINS  (ADR-0060 амендмент
                                          2026-09-30: ref — name@version, name (последняя
                                          версия) или tenant:name[@version] — пакет
                                          своего арендатора; память GET
                                          /api/memory/packages/{name}?version=&namespace=;
                                          ответ памяти как есть: name, version, kinds,
                                          relations, description, scope, ref — без
                                          namespace-владельца; нет пакета или пакет
                                          чужого арендатора → 404 not_found с
                                          details.pack; иная ссылка → 422
                                          invalid_pack_ref; событий нет)

# --- v0.5 Project Model -----------------------------------------------------

POST /api/v1/workspace-types              workspaces.manage
GET  /api/v1/workspace-types              workspaces.read   (?status=&limit=&cursor=)
GET  /api/v1/workspace-types/{id}         workspaces.read   (+ETag "workspace_type-<v>")
PATCH /api/v1/workspace-types/{id}        workspaces.manage (If-Match)
POST /api/v1/workspace-types/{id}:archive workspaces.manage (422 workspace_type_in_use)

GET  /api/v1/workspaces/tree              workspaces.read   (?rootId=&depth=&includeArchived=
                                          &includeProjects=; один рекурсивный CTE, порядок
                                          братьев стабилен по (slug, id); ответ {roots: [...]})
POST /api/v1/workspaces                   workspaces.manage (v0.5: typeId|typeKey, customFields)
PATCH /api/v1/workspaces/{id}             workspaces.manage (If-Match; смена типа
                                          перепроверяет правило родителя и детей)
POST /api/v1/workspaces/{id}:move         workspaces.manage (перепроверяет governance всего
                                          поддерева; 422 governance_weakened — move отклонён
                                          целиком)

POST /api/v1/project-templates            project_templates.manage (создаёт СЛЕДУЮЩУЮ версию
                                          key; версия неизменяема после записи)
GET  /api/v1/project-templates            project_templates.read (?key=&status=)
GET  /api/v1/project-templates/{id}       project_templates.read
POST /api/v1/project-templates/{id}:deprecate  project_templates.manage (идемпотентно)

POST /api/v1/projects                     projects.manage  (workspaceId ИЛИ workspaceSlug —
                                          workspace и профиль создаются атомарно;
                                          409 project_exists при втором профиле)
GET  /api/v1/projects                     projects.read    (?workspaceId=&status=&statusKey=
                                          &systemStatusCategory=&templateKey=
                                          &externalSystem=&externalType=&externalId=)
GET  /api/v1/projects/{id}                projects.read    (+ETag "project-<v>"; ответ несёт
                                          parentProjectId, templateKey/Version,
                                          activeConfigRevision)
PATCH /api/v1/projects/{id}               projects.manage  (If-Match)
POST /api/v1/projects/{id}:archive        projects.manage  (идемпотентно; workspace, задачи,
                                          события и история конфигурации сохраняются)
POST /api/v1/projects/{id}:transition     projects.manage  (If-Match; только объявленные
                                          переходы; событие project.status_changed)
GET  /api/v1/projects/{id}/effective-config      projects.read (config + provenance по слоям)
GET  /api/v1/projects/{id}/config-revisions      projects.read
POST /api/v1/projects/{id}/config-revisions      projects.manage (append-only; НЕ активирует)
POST /api/v1/projects/{id}/config-revisions/{revision}:activate
                                          projects.manage  (If-Match; единственный
                                          авторитетный указатель)
GET  /api/v1/projects/{id}/external-references   projects.read (частный случай generic API)
POST /api/v1/projects/{id}/external-references   projects.manage (201 при создании, 200 при
                                          обновлении metadata того же ключа,
                                          409 external_reference_conflict — чужая сущность)

POST /api/v1/external-references          право по entityType: project → projects.manage,
                                          task → tasks.write (201 создание, 200 тот же ключ,
                                          409 external_reference_conflict, 422
                                          invalid_entity_type)
GET  /api/v1/external-references          прямой поиск ?entityType=&entityId= (право чтения
                                          типа) ЛИБО обратный ?externalSystem=&externalType=
                                          &externalId= (недоступные по чтению типы просто
                                          отсутствуют в выдаче, не 403)

GET  /api/v1/operations/context-adapter                    operations.read  (read-only)
POST /api/v1/operations/context-adapter/{tenantId}:redrive operations.manage (курсор НЕ
                                          двигается; чужой tenantId — 404)
POST /api/v1/operations/context-adapter/{tenantId}:rebuild operations.manage (только назад;
                                          вперёд — 422 cursor_must_not_advance)
POST /api/v1/operations/journal:archive                    operations.manage
POST /api/v1/operations/journal:prune                      operations.manage (единственная
                                          операция, после которой данные теряются)

GET /health/live | /health/ready | /metrics    (без аутентификации)
```

<a id="executor-instructions"></a>

## Инструкции исполнителю (CP-ADR-0066)

Исполнитель (адаптер раннера или харнесс человека) получает блок
`instructions: {layers: [{source, ref, version, text}], hash}` в
`GET /runs/{id}/context`, MCP `cp_get_run_context` и в `POST /context` при фокусе
на задаче или run. Слои — от общего к частному, дописываются, а не сливаются:

| source | ref | version | откуда текст |
|---|---|---|---|
| `platform` | `control-plane:platform-contract` | версия текста ядра | контракт платформы: протокол claim/run, отчёт, границы полномочий; всегда первый |
| `project` | `project:<id>` | номер активной ревизии конфигурации (или `null`) | `settings.agentInstructions` эффективной конфигурации проекта задачи (с наследованием), строка ≤ 16 KiB |
| `taskType` | `taskType:<key>` | версия типа задачи | `instructions` версии типа, ≤ 16 KiB |

Пустой слой не выводится: тип без инструкций в проекте без них — только
`platform`. `hash` — `sha256:` от канонического JSON слоёв (ключи
`source, ref, version, text`, отсортированы, строки NFC, UTF-8, без пробелов).
При старте run хэш и `[{source, ref, version}]` записываются в run
(`instructionsHash`, `instructionsRefs`) и в событие `run.started`: блок в контексте
run показывает слои как они стоят сейчас, запись в run — с чем run начинался.
Четвёртый слой — «Agent conventions», общие инструкции описания агента
(`executor.instructions`) или файл (`CONTROL_PLANE_{CLAUDE,CODEX,OPENCODE}_PROMPT_FILE`) —
добавляет сам исполнитель, ядро его не видит. Соглашения репозитория в этот слой
не входят: репозиторий несёт их сам (`AGENTS.md`), исполнитель читает их сам.

## Skill runtime (M2.1–M2.2, ADR-0056)

Версия Skill с `contract` вызываема ядром; без него — строка каталога (все
скиллы до M2.1, в т.ч. http-скиллы BidOps): её можно назначать и требовать, но
вызов даёт `409 skill_not_invocable`. Сервер Skill **не исполняет**: он
проверяет вызов, хранит его и отдаёт исполнителю под lease.

Публикация (`POST /api/v1/skills`):

```json
{
  "name": "repo.search", "version": "1.0.0",
  "sideEffects": "none | external_read | external_write",
  "riskLevel": "low | medium | high",
  "contract": {
    "inputs":  {"type": "object", "...": "JSON Schema 2020-12"},
    "outputs": {"type": "object"},
    "requiredPermissions": ["tasks.read"],
    "preconditions": [], "postconditions": [],
    "timeoutSeconds": 60,
    "retryPolicy": {"maxAttempts": 1, "backoffSeconds": 0},
    "idempotency": "required | natural | none",
    "costModel": {"unit": "call", "estimate": 1},
    "implementation": {"protocol": "http | local | mcp",
                       "endpoint": "...", "auth": {"audience": "..."}, "entrypoint": "..."}
  }
}
```

- Обязательны `inputs`, `outputs`, `implementation`, а рядом с contract —
  `sideEffects` и `riskLevel`. Остальное получает значения по умолчанию, и
  сохраняется нормализованный contract. `protocol` берётся из
  `implementation.protocol` (передать другой — 422); `inputSchema`/`outputSchema`
  версии заполняются из `inputs`/`outputs`.
- Проверки: схемы — валидный JSON Schema 2020-12 без внешних `$ref`
  (`invalid_json_schema`); `$schema`, если указан, — только 2020-12;
  `requiredPermissions` — известные права; `http` требует `endpoint` http(s),
  `local` — `entrypoint` вида `module:function`, `mcp` — `entrypoint` = имя
  инструмента; в `implementation.auth` нельзя класть секреты
  (`secret_material_rejected`); `maxAttempts` 1..10, `backoffSeconds` 0..3600,
  `timeoutSeconds` 1..3600. Непустые `preconditions`/`postconditions` пока
  отклоняются (`422 unsupported_skill_condition`): язык выражений — M1.3.
- Опубликованная версия иммутабельна и в БД (триггер `skills_immutable`):
  меняются только `description` и `status` вперёд; DELETE запрещён.

Вызов и исполнение:

```
POST /api/v1/skills/{ref}:invoke                 skills.invoke   ref = id | name@version | name
     body: {inputs, idempotencyKey?, taskId?, runId?, approvalId?}
     → 201 новый вызов (status=pending); 200 — повтор с тем же idempotencyKey
GET  /api/v1/skill-invocations/{id}              skills.invoke | skills.execute
     (виден authority и исполнителю; держатель skills.execute видит все; прочим — 404)
POST /api/v1/skill-invocations:claim             skills.execute
     body: {protocols: [http|local|mcp], localEntrypoints: [...], httpOrigins: [...],
            mcpEndpoints: [...], audiences: [...], sessionId?, leaseSeconds?, invocationId?}
     → 200 {invocation, skill, settings} | 204 нечего исполнять
     (skill — {id, name, version, protocol, sideEffects, riskLevel, contract}; config каталога
      исполнителю не отдаётся; settings — {package, version, schemaRevision, values}
      пакета скилла на момент выдачи, null — скилл не из пакета или пакет без настроек,
      CP-ADR-0081 §8)
POST /api/v1/skill-invocations/{id}:heartbeat    skills.execute  {fencingToken, leaseSeconds?, sessionId?}
POST /api/v1/skill-invocations/{id}:complete     skills.execute  {fencingToken, output, cost?, sessionId?}
POST /api/v1/skill-invocations/{id}:fail         skills.execute
     body: {fencingToken, error: {code, message?, retryable?, details?}, sessionId?}
POST /api/v1/skill-invocations/{id}:cancel       authority вызова (skills.invoke) | org.manage
     body: {reason?} → 200 status=cancelled; повтор — 200 тот же; завершённый — 409
     invocation_terminal; прочим — 404
```

- `:invoke` проверяет по порядку: `skills.invoke`; версию (`404` нет такой);
  повтор `idempotencyKey` (с теми же inputs от того же principal —
  существующий вызов `200`, даже если версию с тех пор отключили; иначе
  `409 idempotency_key_reuse`); вызываемость версии (`409 skill_not_invocable`
  с `details.reason` = `disabled` | `no_contract` | `protocol_not_invocable`);
  при `idempotency=required` без ключа — `400 idempotency_key_required`;
  `inputs` по схеме, включая `format` (`400 invalid_skill_inputs`,
  `details.errors[].path`); для `runId` — run вызывающего (`403
  run_owner_mismatch`), в статусе `running` (`409 run_not_active`), при
  `running` run под child handle у principal — сам под handle (`403
  run_id_required`), и потолок child handle на `skills.invoke`; без `runId` у principal с `running` run под child
  handle — `403 run_id_required`; при задаче (`taskId` или задача run) —
  `tasks.write` на её workspace (`403 permission_denied`) и потолок run на
  `tasks.write`; права `requiredPermissions` на workspace задачи (без задачи —
  tenant; `403 skill_permission_denied`, `details.missing`, `details.resource`)
  и, при `runId`, каждое — в потолке run (`403 child_grant_exceeded`); при
  `runId` — effective tool policy run для Skill (`403 child_grant_exceeded`
  или `tool_not_authorized`); side effects: `external_write` требует
  `approvalId` — gate-approval на той же нетерминальной задаче (`409
  task_terminal`) в статусе `approved` (иначе `403
  skill_side_effect_not_authorized`), не использованный ранее для этой версии
  (`409 approval_already_used`; расходует его любой принятый вызов, в том числе
  завершившийся `failed`), **или** основание `execution`: вызов под `running`
  run задачи, тип которой объявляет `execution` именно на эту версию, задача
  нетерминальна и на ней нет `pending` gate-approval (`409
  approval_required`). Основание `execution` записывается при любых side
  effects, и такой вызов у run — один (`409 execution_already_invoked`).
- Резолюция `name` без версии — как ADR-0021: новейшая `active`, иначе
  новейшая `deprecated`.
- `requestedBy` = `{kind: principal, ref: principalId}` или `{kind: run, ref:
  runId}`; `authorityPrincipalId` — вызывающий. Виды `rule|approval|verification`
  зарезервированы за внутренними вызывающими.
- `:claim` берёт самый ранний `pending` (с учётом backoff), который исполнитель
  может выполнить: протокол из `protocols`, а для `local` — точный `entrypoint`
  из `localEntrypoints`; для `http` — `endpoint`, равный origin из
  `httpOrigins` (`scheme://host[:port]`, без учёта регистра) или лежащий под
  ним (`origin/…`); для `mcp` — то же по `mcpEndpoints`, где допустим и точный
  `stdio:<имя>`; и для `http`/`mcp` — `implementation.auth.audience` пуст или
  входит в `audiences`. Удалённый протокол без объявленных эндпоинтов не
  получает ничего. Не-origin в `httpOrigins`/`mcpEndpoints` — `422
  invalid_executor_endpoint` (ADR-0056, амендмент M2.2, D). `invocationId`
  сужает выбор до одного вызова. Перед
  выдачей основание перепроверяется под блокировкой строки: версия `disabled`,
  закрытая задача вызова с основанием, отозванный approval, завершённый run
  основания `execution` → вызов `cancelled` (`error.code=basis_revoked`,
  `message` — причина) и берётся следующий; `pending` gate-approval на задаче
  `external_write`-вызова с основанием `execution` — вызов остаётся `pending`. Выдача: `status=running`, `attempt+1`,
  `fencingToken+1`, lease = `timeoutSeconds + 30` c (в пределах
  `CP_CLAIM_TTL_MIN/MAX_SECONDS`); heartbeat не продлевает lease дальше
  начала попытки + `timeoutSeconds×2 + 30` c. Перед выдачей истёкшие lease тенанта
  возвращаются в очередь (лениво; воркер делает то же фоном): попытка считается
  потраченной — есть попытки → `pending`, нет → `failed` с `lease_expired`.
- `:heartbeat`/`:complete`/`:fail` требуют текущий lease: чужой исполнитель,
  не тот `fencingToken`, истёкший lease, завершённый вызов или — если claim
  был с `sessionId` — другой либо отсутствующий `sessionId` → `409
  stale_invocation_lease`.
- `:complete` повторно валидирует `output` по схеме: нарушение — вызов
  `failed` с `error.code=output_contract_violation` (`retryable=false`,
  отклонённый выход — в `error.details.rejectedOutput`, свыше 64 КиБ — только
  `rejectedOutputBytes`); ответ всё равно `200`, вердикт — в теле. Успех при `taskId` создаёт артефакт
  `skill_result` (автор — authority, `content.output`), id — в `artifactId`.
- `:fail` с `retryable=true` при `attempt < maxAttempts` возвращает вызов в
  `pending` с `availableAt = now + backoffSeconds`; иначе — `failed`.
- `:cancel` останавливает `pending` или `running` вызов; у `running` исполнитель
  теряет lease (его `:heartbeat`/`:complete`/`:fail` → `409
  stale_invocation_lease`), но мог успеть подействовать —
  `error.details.wasRunning=true`. Событие `skill.invocation_cancelled`.
  `error.details.initiator` (и то же поле в событии): `principal` — отменил
  вызывающий через `:cancel`, `system` — ядро (основание утрачено, истёк срок
  ожидания исхода approval'а); `cancelledBy` — principal, от чьего имени
  записана отмена.
- Публикация `external_write` с `idempotency=none` допускает только
  `retryPolicy.maxAttempts = 1` (иначе `422 invalid_skill_contract`,
  `details.field=retryPolicy.maxAttempts`): повтор без идемпотентности — второе
  внешнее действие.

Work, исполняемая Skill (ADR-0056 §3): версия типа задачи с `execution =
{skill, version, inputs}` — версия Skill закреплена, должна существовать и иметь
contract (`422 invalid_task_execution`). `inputs` — JSON-path (`$`, `.name`,
`[n]`) по задаче в представлении API: строка (весь вход, по умолчанию
`$.customFields`) или `{имяВхода: путь}`; отсутствующий путь вход не
заполняет. Такую задачу исполняет демон `control-plane-agent` с исполнителем
Skills: claim, run, один вызов с `runId` и `idempotencyKey = execution:<runId>`,
результат — `skill_result` от ядра и `succeed_run` (или `fail_run` с
`skill_invocation_<status>: <code>`). Runner'у нужны `skills.invoke`,
`skills.execute`, `task_types.read` и назначение Skill (effective tool policy run).

Типизированные выходы такой задачи (CP-ADR-0072, амендмент 2026-10-01). Успешный
`:complete` вызова-исполнения (`authorizationBasis.kind = execution`) до
`skill_result` сдаёт выходы `artifactSchema.outputs` типа задачи. Значение
выхода — поле `output` скилла с тем же именем, что `key` выхода (`outputs[].key
= draft` ← `output.draft`); явного отображения нет. Для каждого выхода:

- поля нет или оно `null` — артефакта нет, статус `absent` (`missing`, если
  выход `required: true`: тогда неявный критерий проверки провалит задачу с
  `artifact_missing`);
- иначе значение проверяется по последней версии типа артефакта: по её
  `metadataSchema` (форматы — аннотации), media type `application/json` — по
  `mediaTypes` типа и сужению выхода, размер — по `maxBytes`. Прошло —
  артефакт `type = <type выхода>`, `name = <key>.json`, содержимое — значение
  в UTF-8 JSON в хранилище (`contentState = stored`, `mediaType =
  application/json`), `taskId`/`runId` — задачи и run вызова, автор —
  исполнитель скилла (вызывающий `:complete`), `metadata = {output, skill,
  version, invocationId, authorityPrincipalId}`, `typeVersion`; вытесняет
  (`supersedesArtifactId`) прежнюю head-ревизию того же типа у задачи. Не
  прошло — статус `rejected` с `reason = {code, message, details?}`: коды
  `invalid_output_value` (`details.errors[{path, message}]`),
  `media_type_not_allowed`, `artifact_too_large`, `content_store_unavailable`.

Вызов при этом остаётся `succeeded`, `skill_result` сохраняется: что стало с
выходами — в его `metadata.outputs` и в `outputs` события
`skill.invocation_succeeded` (v2): `[{key, type, status: created | absent |
missing | rejected, artifactId?, reason?}]`; у вызова не для исполнения задачи
— `[]`. Видимость артефакта — как у задачи (`artifacts.read` на задаче, вход
задачи-получателя через `?forTask=`).

SDK: `register_skill`, `describe_skill`, `invoke_skill`, `get_skill_invocation`,
`wait_skill_invocation` (ограниченное ожидание), `claim_skill_invocation`,
`heartbeat_skill_invocation`, `complete_skill_invocation`,
`fail_skill_invocation`, `cancel_skill_invocation`; `create_task_type(execution=)`. MCP: `cp_describe_skill` (read-only, полный contract) и
`cp_invoke_skill` (mutating: создаёт вызов в контексте текущего run/task и ждёт
результата до `wait_seconds`, максимум 120 c; иначе возвращает вызов с `id`).

Права `skills.invoke` и `skills.execute` — отдельные: вызывающий не получает
права исполнять, исполнитель — транспорт без собственных прав на вызов. В
bootstrap-admin они входят автоматически (как все права); выдача runner-binding'у
на стендах — отдельное операционное действие.

## Формат ошибок

Единый конверт, честные HTTP-коды (`200` с ошибкой внутри не бывает):

```json
{
  "error": {
    "code": "task_already_claimed",
    "message": "Task already has an active claim",
    "details": {"taskId": "...", "claimId": "...", "expiresAt": "..."},
    "requestId": "req_..."
  }
}
```

| Код | Типичные `error.code` |
|---|---|
| 400 | `invalid_request` (нарушение контракта, в т.ч. неизвестный query-параметр), `invalid_if_match`, `invalid_skill_inputs`, `idempotency_key_required` |
| 401 | `invalid_credentials` |
| 403 | `permission_denied`, `principal_not_active`, `delegation_required`, `claim_holder_mismatch`, `session_owner_mismatch`, `bootstrap_disabled`, `permission_escalation`, `not_eligible`, `run_holder_mismatch`, `tool_not_authorized`, `child_grant_exceeded`, `skill_permission_denied`, `skill_side_effect_not_authorized`, `run_owner_mismatch`, `run_id_required` |
| 404 | `not_found`, `tool_not_found` (в т.ч. чужой tenant и инструмент вне effective policy — существование не раскрывается), `content_not_found` (у артефакта нет содержимого, CP-ADR-0072) |
| 409 | `project_exists`, `workspace_type_exists`, `external_reference_conflict`, `retention_blocked_by_consumer`, `task_already_claimed`, `version_conflict`, `stale_claim`, `task_claimed`, `session_expired`, `session_not_active`, `claim_expired`, `claim_not_active`, `claim_not_expired`, `idempotency_key_reused`, `idempotency_in_flight`, `already_bootstrapped`, `task_already_completed`, `task_not_ready`, `run_already_active`, `run_not_active`, `run_in_progress`, `workspace_slug_conflict`, `role_slug_conflict`, `capability_exists`, `skill_exists`, `relation_exists`, `approval_already_decided`, `approval_required` (v0.3 gate), `verification_pending` (ADR-0067), `budget_exceeded`, `action_already_finished`, `child_handle_revoked`, `child_handle_expired`, `child_run_already_bound`, `skill_not_invocable`, `skill_version_immutable`, `invalid_status_transition`, `idempotency_key_reuse`, `stale_invocation_lease`, `approval_already_used`, `task_terminal`, `outcome_not_replayable`, `approval_precondition_failed`, `snapshot_stale`, `pack_version_conflict`, `input_missing` (CP-ADR-0072), `content_not_stored` (CP-ADR-0072), `cannot_disable_self`, `use_agent_retire`, `use_agent_publish` (CP-ADR-0077), `incompatible_status`, `approval_pending` (ADR-0048, амендмент 2026-09-30) |
| 410 | `content_purged` (содержимое удалено `:purge-content`, CP-ADR-0072) |
| 413 | `request_too_large` (в т.ч. загрузка сверх `CP_ARTIFACT_MAX_BYTES`) |
| 422 | `validation_error` (в т.ч. символ NUL в строке JSON-тела — `details.errors[{path, code: "nul_character"}]` с JSON Pointer в `path`, в параметре запроса или пути — с `path` вида `query.<имя>` или `path.<имя>`, CP-ADR-0083), `invalid_*` (доменная валидация), `task_not_claimable`, `task_cancelled`, `empty_update`, `unknown_requirement`, `dependency_cycle`, `workspace_cycle`, `workspace_archived`, `workspace_has_active_children`, `task_not_runnable`, `artifact_mismatch`, `invalid_approval`, `skill_disabled`, `unsupported_protocol_version`, `invalid_harness`, `invalid_budget`, `invalid_checkpoint`, `invalid_action`, `invalid_tool_query`, `invalid_correlation_id`, `invalid_child_grant`, `invalid_child_result`, `invalid_child_handle_ref`, `invalid_child_handle_token`, `invalid_entity_type`, `invalid_entity_reference`, `invalid_external_lookup`, `status_not_in_lifecycle`, `invalid_transition`, `invalid_lifecycle_schema`, `invalid_status_category`, `system_task_type_required`, `child_grant_exceeds_parent`, `child_depth_exceeded`, `child_result_too_large`, `invalid_skill_contract`, `unsupported_skill_condition`, `secret_material_rejected`, `invalid_approval_schema`, `workspace_not_root`, `pack_invalid`, `pack_not_found`, `pack_version_required`, `invalid_pack_ref` (`GET /knowledge/packs/{ref}`), `snapshot_invalid`, `invalid_context_schema`, `invalid_recall_request`, `entities_query_invalid` (`/knowledge/entities:query`, CP-ADR-0060 K031); CP-ADR-0072: `invalid_artifact_content`, `content_ref_not_found`, `invalid_artifact_metadata`, `media_type_not_allowed`, `artifact_too_large`, `invalid_artifact_type`, `invalid_artifact_schema`, `unknown_artifact_type`; CP-ADR-0077: `principal_kind_not_disableable`, `principal_kind_not_enableable` |
| 428 | `if_match_required` |
| 500 | `internal_error` (без стектрейса) |
| 502 | `memory_unavailable` (память не обработала проксируемый запрос, в т.ч. отвергла credential ядра — `403`; `details.memoryStatus`, `details.retryable`; текст ответа памяти клиенту не отдаётся) |
| 503 | readiness: БД недоступна или миграции не применены; `decision_unavailable`; `memory_disabled`; `memory_timeout` (`/context/recall`, `:replay`, `/knowledge/entities:query`); `content_store_unavailable` (хранилище содержимого выключено или недоступно, CP-ADR-0072); `catalog_key_busy` (`call` не ждёт ключ дочернего процесса, занятый применением пакета, публикацией или выводом — повторить; CP-ADR-0074, амендмент 2026-09-29) |

## События

`type` в журнале: `tenant.bootstrapped`, `principal.created|updated|disabled|enabled`,
`api_key.created|revoked`, `delegation.created|revoked`,
`session.opened|closed|expired`, `task.created|updated|claimed|completed|context_pack_recorded`,
`claim.released|expired`, `workspace.created|updated|archived|moved`,
`workspace.member_added|member_removed`, `role.created|updated|assigned|revoked`,
`capability.created|assigned|revoked`, `skill.registered|updated|assigned|revoked`,
`task.relation_added|relation_removed`,
`run.started|succeeded|failed|cancelled|suspended|cancel_requested|checkpointed|handoff_prepared`,
`artifact.created` (без content), `approval.requested|approved|rejected|cancelled`
(payload несёт `gate`; `approved|rejected` — ещё `outcomeStatus`),
`approval.outcome_executed|outcome_failed|outcome_deferred` (ADR-0061: `outcome`,
`actions[]` — evidence по каждому действию, у `outcome_failed` — `failedAction` с
`code` и `failureWorkTaskId`, у `outcome_deferred` — `waitingAction` и claim, которого
ждёт исход), `task.completion_work_executed|completion_work_failed` (CP-ADR-0061,
амендмент 2026-09-25: entity — завершённая задача, `actorId` — завершивший,
`taskTypeId`, `actions[]` — evidence, у `failed` — `failedAction` с `code`),
`observation.recorded` (v0.4 explicit remember),
`knowledge.snapshot_reconciled` (ADR-0060: snapshotId, pack, source, observedAt,
namespace, число сущностей/связей, `duplicate` и числовые счётчики ответа памяти —
без содержимого снимка), `knowledge.pack_registered` (entity `knowledge_pack`;
name, version, status, с v2 — scope `common|tenant`; без манифеста), `knowledge.packs_configured` (entity
`workspace`; namespace, закреплённые packs, strict), `knowledge.document_stored`
(ADR-0060 амендмент, п. К3, entity `workspace`: naturalKey, title, type, namespace,
число фрагментов и связей — без текста), `knowledge.changed`
(CP-ADR-0076 п.7, entity `workspace`: `changes[{kind, key, change opened|changed|
closed}]`, `truncated` — после сверки снимка, пустая сверка события не даёт).

process-packages (CP-ADR-0074 п.13, схемы — `docs/events/`):
`process.definition_published` (entity `process_definition`),
`process.started|correlated|data_changed|stage_entered|stage_exited|milestone_reached|
timer_fired|timer_rescheduled|escalated|suspended|resumed|compensated|recall_completed|
recall_timed_out|migrated|completed|cancelled|failed` (entity `process_instance`;
`started|data_changed|completed` несут вычисленную проекцию дела `memory`,
`recall_completed` — счётчики и хэш ответа, не узлы), `calendar.published`
(entity `calendar`).

M2.1 (ADR-0056): `skill.invocation_requested|claimed|retry_scheduled|succeeded|failed`
(entity `skill_invocation`; payload — ids, skill/version, attempt, код ошибки,
без inputs и output; `claimed` не уходит в outbox). Успех с `taskId` пишет
также `artifact.created` для `skill_result`, а вызов-исполнение — ещё и для
каждого созданного типизированного выхода (`skillInvocationId`); `succeeded` v2
добавляет `outputs` — статусы выходов без значений.

v0.7 (HRS-7): `run.child.launched|started|resolved|revoked|cancel_requested`.
Payload несёт ids, correlationId, outcome, `resultHash` и размеры grant;
title, summary и data дочерней работы в журнал не попадают.

v0.5: `workspace_type.created|updated|archived`,
`project_template.created|deprecated`,
`project.created|updated|archived|status_changed`,
`project.config_revision_created|config_revision_activated`,
`project.external_reference_added|external_reference_updated`,
`context_adapter.redriven|rebuilt`, `event_journal.archived|pruned`.

v0.8 (ADR-0048): `task_type.created|deprecated`. `task.created` несёт `typeKey`,
`typeVersion` и `systemStatusCategory`; `task.updated` при смене статуса несёт
`fromStatus`, `status` и `systemStatusCategory` рядом с `changes`;
`task.claimed` и `task.completed` несут пару статуса, `claim.released` —
`taskStatus` и `taskSystemStatusCategory`. Имена и смысл существующих полей не
меняются: подписчик, читающий `status`, продолжает читать пользовательский ключ.

CP-ADR-0066: `run.started` несёт `instructionsHash` и `instructionsRefs` —
`[{source, ref, version}]` слоёв инструкций исполнителю, с которыми запущен run
(текст слоёв в журнал не попадает); `task_type.created` — `declaresInstructions`.

v0.8 (ADR-0047): тип события внешней ссылки выводится из типа сущности —
`<entityType>.external_reference_added|external_reference_updated`. Для проекта
это прежние `project.external_reference_*`; для задачи —
`task.external_reference_*`. `metadata` в payload не попадает.

v0.8 (ADR-0050): `task.comment_added|comment_edited` — по потоку **задачи**,
чтобы подписчик work item видел обсуждение там же, где смену статуса. Payload
несёт `commentId`, `authorPrincipalId`, `version`, `bodyLength` и
необязательные `runId`/`artifactId`; тело комментария в журнал не попадает
никогда. Прежние версии текста живут в append-only `task_comment_revisions`
(UPDATE/DELETE запрещены триггером) и читаются через
`/tasks/{ref}/comments/{id}/revisions`.
Ответ комментария (создание, правка, один, страница ленты) рядом с
`authorPrincipalId` несёт `author {kind, displayName}` — текущие вид и имя
автора, читаемые с одним `tasks.read` (поправка ADR-0050 от 2026-09-30).

M1.1 (ADR-0062): `goal.created|updated` (entity `goal`). `goal.created` несёт
`title`, `status`, `workspaceId`, `ownerId`, `parentGoalId`, `criteriaCount` и
сводку `createdFrom`; `goal.updated` — `changes` (`desired_state` → `true`,
`criteria` → число), `fromStatus`/`status` при смене статуса, `version`.
`task.created` дополнен `goalId`, `origin` (kind, ref, ruleId и id фактов —
без `note`) и `acceptanceChecks`; `acceptance`/`evidence` в `changes`
`task.updated` — числа. Желаемое состояние и `spec` проверок в журнал не
попадают.

M1.6 (ADR-0067): `task.verification_started|verified|verification_failed`
(entity — задача, корреляция — завершение, открывшее попытку): `taskId`,
`verificationId`, `attempt`, `trigger`, `checks` (число критериев);
`verification_started` — ещё `triggerRef`; `verified` — `results`
(`[{key, kind, status, evidence}]`, без текста причин) и `artifactId`,
пишется в одной транзакции с `task.completed` (тот несёт `verificationId` и
`attempt`); `verification_failed` — `results`, `failedCheck`, `reason` (код),
`consecutiveFailures`, `blocked`, `fromStatus`/`status`/`systemStatusCategory`.

M1.3 (ADR-0063): `rule.created|updated|enabled|disabled|archived` (entity
`rule`) — сводка правила (ключ, версия, статус, вид и тип триггера, скилл,
вид действия и тип задачи; шаблоны и условие — нет), у `rule.updated` ещё
`changes` (имена полей). Каждая завершённая оценка — `rule.evaluated` (entity
`rule`): `ruleId`, `ruleKey`, `ruleVersion`, `evaluationId`, `triggerRef`,
`result`, `conditionMatched`, `evidence`, `skillInvocationId`, `work`,
`error.code`. `work.derived` (работа заведена) и `work.reconciled` (изменена
или отменена) — по потоку **задачи**, с правилом, версией, оценкой, ключом и
evidence. Всё, что пишет правило, несёт `correlationId = work-rule:<id>`;
такие события правила не оценивают.

CP-ADR-0072 (фича `artifact-handoff`):
`artifact.created` v2 — плюс `sizeBytes`, `mediaType`, `sha256`,
`contentState`, `typeVersion` (содержимое по-прежнему не пишется);
`artifact.content_read` (entity `artifact`, actor — читающий; каждая выдача
байтов): `artifactId`, `taskId`, `forTaskId` (задача-получатель при чтении
как вход по `?forTask=`, иначе `null`), `runId` (running run читающего на
задаче артефакта, при чтении как вход — на задаче-получателе), `sha256`, `sizeBytes` — в память
не переносится; `artifact.content_purged` (entity `artifact`): `artifactId`,
`taskId`, `sha256`, `sizeBytes`, `reason` (секреты вычищены, до 1000
символов), `objectDeleted`; `artifact_type.created` (entity
`artifact_type`): `key`, `version`, `mediaTypes`, `maxBytes`,
`declaresMetadataSchema`; `task_type.created` v2 — плюс
`declaresArtifactSchema` и числа `inputs`/`outputs`. Каталог `docs/events/`
пополняется в шаге, который начинает писать событие.

ADR-0064: `task.context_pack_recorded` (entity `task`) — пакет контекста claim
записан: `publicId`, `contextPackId`, `claimId`, `asOf`, `asOfMode`, счётчики
`entities`/`facts`/`snapshots`. Версию задачи событие не поднимает, содержимое
пакета в журнал не пишется; в память событие не переносится.


Run actions в журнал НЕ пишутся (execution audit — отдельная таблица,
ADR-0019). Каждое событие несёт `sequence`, `entityType`, `entityId`,
`actorId`, `sessionId`, `correlationId`, `requestId`, `traceRunId` (v0.5,
может быть `null` у исторических событий), `payload`, `occurredAt`. Журнал
append-only (запрет UPDATE/DELETE триггером БД); единственное исключение —
операторская архивация (ADR-0038), которая переносит подтверждённые события
в `event_archive` под транзакционным флагом и пишет собственное audit-событие.

Active Turn Control messages хранятся в `run_control_messages` и остаются
авторитетными после restart. События `run.control_message.*` содержат только
ids, seq, operation, status, causalPosition и safeBoundary; directive/reason
никогда не копируются в event/outbox. `request_cancel` блокирует новые Run
actions только после `applied` acknowledgement. `force_cancel` атомарно
переводит Run в `cancelled`, освобождает Claim и каскадирует terminal control
message по активным `spawned_by` descendants — независимо от cancellation
policy дочерних handle: `detach` избавляет только от кооперативного каскада.

Child run handles живут в `run_child_handles`, их bounded terminal results — в
append-only `run_child_results` (UPDATE/DELETE запрещены триггером). Handle не
хранит execution status: он выводится из дочерних Task/Run на каждом чтении.

В durable memory уходит НЕ весь журнал: Context Adapter применяет явный
whitelist полей (`application/context/mapping.py`, `mappingVersion = 5`; с
версии 5 — `goalId`/`origin` из `task.created`, `goal.created|updated` и
scope `goal`).
Для проектных событий он намеренно узкий — идентичность и lifecycle;
`customFields` и `config` не передаются никогда.

## SDK/MCP operator contract (v0.6)

Официальный `ControlPlaneClient` предоставляет `create_task`, типизированный
`list_tasks` (project/workspace/status/assignee + cursor), `update_task` с
обязательным `expected_version`, `add_task_relation`,
`remove_task_relation`, `prepare_handoff` и `continue_after_handoff`.
Mutating методы используют существующий idempotent retry: один логический
вызов сохраняет один `Idempotency-Key` на все transport retries.

MCP adapter вызывает только SDK и публикует соответствующие tools
`cp_create_task`, `cp_list_tasks`, `cp_update_task`,
`cp_add_task_relation`, `cp_remove_task_relation`, `cp_prepare_handoff`.
Mutating tools помечены `readOnlyHint=false`; их descriptions требуют явного
решения человека. Annotations — UI hint, не authorization boundary.

Словарь статусов принадлежит типу задачи тенанта (ADR-0048), поэтому реестр
виден и в MCP: read-only `cp_list_task_types` и `cp_get_task_type`, плюс
`typeKey`/`typeVersion` у `cp_create_task`. Допустимые цели перехода агент
получает не из адаптера, а из проекции ядра `GET /tasks/{ref}/transitions`
(SDK `get_task_transitions`), которую `cp_get_task` возвращает рядом с
задачей: одно и то же правило кормит и проверку записи, и подсказку читателю,
поэтому они не могут разойтись. Создание и депрецирование типов через MCP не
публикуются намеренно — это мутация конфигурации тенанта, а не координация
(SPEC TASK-000025 §12); реестр меняется через HTTP/SDK.

Обсуждение задачи доступно харнессу как `cp_list_comments` (read-only),
`cp_comment` и `cp_edit_comment` (mutating); в SDK — `list_task_comments`,
`add_task_comment`, `get_task_comment`, `edit_task_comment` и
`list_task_comment_revisions`. Автор реплики берётся из credential сессии, а не
из аргументов, поэтому реплика агента отличима от реплики человека без
конвенций в тексте. Описания инструментов повторяют границу ADR-0015 — тред
для координации, Artifact для результата, никаких prompts, transcripts и
credentials — потому что именно эта поверхность чаще всего оказывается местом,
куда их вставляют по инерции.

Work graph (ADR-0062): SDK `create_goal`, `list_goals`, `get_goal`,
`update_goal`, `list_goal_work`, а `create_task` / `update_task` / `list_tasks`
принимают `goal_id`, `origin` (только при создании), `acceptance`, `evidence`.
В MCP — `cp_create_goal` и `cp_update_goal` (mutating; `cp_update_goal`
закрывает цель статусом `achieved`/`abandoned`, `clear_owner` / `clear_parent`
снимают владельца и родителя), `cp_list_goals` и `cp_get_goal` (read-only,
вместе с первой страницей работы цели) и те же поля у `cp_create_task` /
`cp_update_task` (`clear_goal` отвязывает).

Стадия проверки (ADR-0067): SDK `list_task_verifications`; `cp_get_task`
возвращает рядом с задачей `verification` — сводку последней попытки.

Правила вывода работы (ADR-0063): SDK `create_rule`, `list_rules`, `get_rule`,
`update_rule`, `enable_rule`, `disable_rule`, `archive_rule`,
`list_rule_evaluations`; журнал действий run — `list_run_actions`. В MCP — только чтение: `cp_list_rules` и
`cp_get_rule` (правило и первая страница истории оценок) — чтобы агент мог
объяснить задачу с `origin.kind = rule`; правило пишет владелец через API
или пакет.

Контекст задачи из графа знаний (ADR-0064): SDK `create_task_type(context_schema=…)`,
`recall(anchor|query, relations, direction, depth, limit, as_of, task_ref,
workspace_id, budget_tokens)`, `get_context_pack(id)`, `replay_context_pack(id)`.
В MCP — `cp_recall` (read-only; задача по умолчанию — текущая; ответ — `text`,
рендер общим `control_plane_agent.context_pack` в пределах `budget_tokens`, как
раздел prompt, и якоря; сырой пакет — только с `include_pack=true`), а
`cp_list_task_types` показывает `declaresContextProfile`.
