# CP-0055: `authorize()` и внешний PDP — режимы local / shadow / policy

Дата: 2026-09-12. Статус: Accepted.

## Решение

Доменная авторизация Control Plane получает второй источник решения —
policy-service (TAI-ADR-0025, дизайн v0 суперпроекта). Рядом с
`require(ctx, *any_of)` появляется `authorize(ctx, *any_of, resource=...)`:
асинхронная проверка с явной ссылкой на ресурс (`platform_auth.ResourceRef`).
Все вызовы `require` в async-коде переведены на `authorize`; без `resource`
вопрос ставится на уровне tenant, а команды, которым ресурс известен
(создание задачи в воркспейсе, изменение и чтение задачи, claim, решение
approval, создание воркспейса под родителем), спрашивают про него. Единственный
sync-вызов (`_require_session_access`) остаётся на `require`.

`CP_AUTHZ_MODE` выбирает, кто решает:

| Режим | Кто решает | Что делает PDP |
|---|---|---|
| `local` (умолчание) | `require` — плоские permissions credential | не вызывается |
| `shadow` | `require` | спрашивается параллельно; расхождения считаются (`authz_shadow_divergence_total`) и пишутся в журнал с decision id; сбой PDP — счётчик, не ошибка |
| `policy` | PDP для credential с IAM-идентичностью | `require` только для legacy API key; недоступность PDP — 503 `policy_unavailable`, не allow |

В режиме `policy` списки фильтруются серверно: `visible_objects(ctx, action,
type)` → `list_objects` policy-service; `list_tasks` ограничивает выдачу
воркспейсами, где разрешено `tasks.read`, плюс задачами, которыми principal
владеет, которые ему назначены или которые он создал — зеркало правила
`task` в модели PDP. В `local` и `shadow` фильтра нет (кроме режима
видимости `members` — амендмент 2026-10-03 ниже, ADR-0082 п.3.4).

## Identity субъекта

Субъект решения — IAM principal id (`sub` access token), не `principals.id`.
`AuthContext` несёт `iam_principal_id` (из `iam_principal_bindings`; для
legacy ключа — `None`), Control Plane вызывает PDP своей service identity
с `on_behalf_of`. Журнал событий получает колонку `iam_actor_id` (миграция
`a9c4e2d7f1b3`), заполняемую из contextvar `current_iam_actor` на пути IAM-
аутентификации: проекция policy-service строит отношения `owner`, `holder`,
`requested_by` на том же субъекте, что и bindings.

## Граница и последствия

Транзакционные гейты (claim, lease, fencing, approval eligibility, child
ceiling) не переносятся в PDP и остаются как были. Каталог действий —
`authz/catalog.yaml`, регистрируется bootstrap суперпроекта. Переход:
`shadow` не меньше недели с нулём расхождений на tenant-level bindings, затем
`policy`; миграция плоских permissions в bindings policy-service —
`deploy/policy/migrate_bindings.py` суперпроекта. Проверки:
`tests/unit/test_authorizer.py` (три режима, any-of, деградация, legacy) и
существующая матрица интеграционных тестов без изменения поведения в `local`.

## Амендмент 2026-09-29 (TASK-000905): «могу ли я X на Y» — `POST /authz:check`

Решение владельца 2026-09-29 по вопросу из R011 (TASK-000823): консоль
скрывала управляющие действия по плоским правам tenant'а из `whoami`. Право,
выданное на один workspace (роль approval в воркспейсе, binding PDP на
воркспейс), кнопку скрывало, хотя ядро действие разрешило бы; и наоборот —
кнопка «одобрить» у своего же запроса была видна, а ядро отвечало 403.

### А1. Один механизм — пакетная проверка

`POST /api/v1/authz:check` `{checks: [{action, resourceType, resourceId}]}`
→ `{results: [{action, resourceType, resourceId, allowed, reason}]}`, по
ответу на каждый элемент в порядке запроса; `reason` — `null` при `allowed`,
иначе `{code, message, details}` — **тот же** код и те же details, что
вернул бы отказ самого эндпоинта (`permission_denied`, `not_eligible`,
`separation_of_duties_violation`, `run_holder_mismatch`, `outside_purpose`,
`not_found`).

Поле `allowedActions` в карточках ресурсов не вводится. Причины: одна точка
вместо шести сериализаторов (task, run, approval, экземпляр процесса,
правило, агент) и их списков; права в каждом элементе списка — лишние
вызовы PDP на каждую выдачу, даже когда кнопок нет; интерфейс спрашивает
про то, что показывает, одним запросом и без повторного чтения карточки.

`action` — глагол эндпоинта, а не permission: одно permission не отвечает
на вопрос (отмена прогона — `tasks.claim` **и** держатель прогона либо
`claims.manage`; решение approval — `approvals.decide` **и** eligibility, и
не исключённый principal). Реестр действий (`resourceType` → `action`):

| resourceType | action | Эндпоинт |
|---|---|---|
| `approval` | `approve`, `reject` | `POST /approvals/{id}:approve\|:reject` |
| `process_instance` | `suspend`, `resume`, `cancel` | `POST /process-instances/{id}:…` |
| `run` | `request-cancel`, `cancel` | `POST /runs/{id}:request-cancel\|:cancel` |
| `rule` | `enable`, `disable` | `POST /rules/{id}:enable\|:disable` |
| `agent` | `update-state` | `PATCH /agents/{key}/state` (state и replicas) |
| `principal` | `enable`, `disable` | `POST /principals/{id}:enable\|:disable` (CP-ADR-0077, TASK-000906) |

`resourceId` — UUID; у агента — ключ. Новое действие добавляется в реестр
вместе с воротами своей команды (А2).

### А2. Тот же код, что у эндпоинта

Каждая команда R011 проходит через функцию-«ворота», и проверка вызывает её
же: `approvals.decision_gate` (purpose-bound credential, `approvals.decide`
на tenant и на `approval:<id>`, исключённые principals, назначенный
principal или роль в воркспейсе approval и его предках),
`process_instances.get_instance(…, processes.operate)` (tenant и воркспейс
процесса), `runs.request_cancel_gate`, `runs.cancel_gate` +
`require_cancel_holder`, `work_rules.rule_write_gate` (tenant и воркспейс
правила), `agents.state_gate`, `principal_enable.enable_gate` и
`principal_disable.disable_gate` (CP-ADR-0077 п.8). Режим `CP_AUTHZ_MODE`, делегирования и сужение
credential приходят с тем же `AuthContext`, admin — тем же `ctx.has`.
Изменение правила доступа делается в воротах и меняет оба ответа сразу.

Проверка отвечает только на вопрос прав. Ворота не берут блокировок и не
проверяют состояние, которое идёт после прав: решённый approval, завершённый
прогон, экземпляр не в том статусе, невыполненные предусловия типа задачи —
это 409/422 эндпоинта, и интерфейс читает их из самого ресурса. Исключение —
ворота principal'а (CP-ADR-0077 п.8): вид цели, «это ты» и принадлежность
реестру агентов по карточке не читаются и со временем не меняются, поэтому эти
409/422 входят в ответ проверки с кодом эндпоинта. Исключение перечислено
явно (`authz_check.RESOURCE_REFUSALS`: `principal` `enable|disable`); 409/422
любых других ворот — нарушение этого правила, и проверка не превращает его в
`allowed=false`, а отвечает им на весь пакет (TASK-000973).

`approve` gate-approval, тип задачи которого объявляет preconditions
(TAI-ADR-0041 п.7), после ворот читает от имени решающего то, на что
ссылаются выражения условий: `tasks.read` на задачу approval и на
`spawnedBy`, `artifacts.read` — если условие читает артефакт
(`approval_outcomes.read_context`). Без этих прав эндпоинт отвечает 403,
поэтому проверка `approve` вызывает то же чтение
(`approval_preconditions.read_preconditions`) — без вычисления самих
условий: `allowed: true` не заканчивается 403 (TASK-000964, ревью
TASK-000905). `reject` условий не имеет и контекста не читает — его проверка
остаётся только воротами решения. Порядок проверок —
эндпоинта: без права на tenant ответ `permission_denied` и для
несуществующего ресурса (эндпоинт тоже не доходит до поиска).

### А3. Границы

- Права на сам запрос не нужны: любой аутентифицированный principal
  спрашивает о себе; о другом principal спросить нельзя. Существование
  ресурса раскрывается ровно настолько, насколько его раскрывает эндпоинт.
- Токен решения (`control-plane:decide`, CP-ADR-0070) вызвать проверку не
  может: `iam.decision_purpose` пропускает с ним только
  `POST /approvals/{свой id}:approve|:reject` и отвечает на `/authz:check`
  `403 outside_purpose` раньше, чем запрос дойдёт до ворот. Ветка
  `outside_purpose` в `decision_gate` для проверки поэтому недостижима; она
  остаётся защитой на случай, если граница токена когда-нибудь расширится, и
  закреплена тестом через подмену `AuthContext` (`dependency_overrides`).
  Консоли с токеном решения кнопки не нужны: у неё одно действие.
- Без побочных эффектов: ворота только читают, событий и записей нет; в
  режиме `shadow` вызов PDP считается метриками, как любой другой.
- Пакет — от 1 до 100 элементов, иначе `400 invalid_request`. `action` и
  `resourceType` в схеме — перечисления (Literal): незнакомый глагол или тип
  отклоняет схема — `400 invalid_request`. `422 unknown_action` со списком
  поддерживаемых — только для известных глагола и типа, пара которых не в
  реестре (`approve` у `rule`); `resourceId` не UUID у типа с UUID —
  `422 invalid_check`. Весь пакет отклоняется целиком.
- Совместимость (решение по ревью TASK-000905): реестр растёт вместе с
  ядром, и консоль новее ядра, спросившая о действии, которого ядро ещё не
  знает, получает `400` на весь пакет, а не `allowed: false` по одному
  элементу. Так и оставлено: частичный ответ выглядел бы как отказ в праве,
  а незнакомое действие — это несовместимость версий, которую консоль должна
  увидеть. Консоль спрашивает только о действиях из реестра своего ядра
  (`openapi.json`: перечисление `action`) или при `400` скрывает кнопки
  новых действий. PDP недоступен — `503 policy_unavailable`
  для всего пакета, как у эндпоинта.
- Ответ — момент времени: права и роли могут измениться до нажатия кнопки,
  эндпоинт проверяет их снова.

Проверки: `tests/integration/test_authz_check.py` — для каждого действия
матрица «спросил → сделал» (admin, право на tenant, роль в воркспейсе, роль
в другом воркспейсе, нет права, своё одобрение, держатель прогона и чужой, в
режиме `policy` — binding PDP на один воркспейс и purpose-bound credential):
`allowed` ⇔ 200, отказ ⇔ тот же код и details; в локальном режиме роль в
одном воркспейсе не даёт операций над экземпляром процесса и правилом этого
воркспейса, право ключа действует во всех; `approve` при preconditions без
`tasks.read` (локально — без права, в `policy` — без binding на задачу
`spawnedBy`); незнакомый глагол или тип — `400`;
`tests/unit/test_authz_check_contract.py` — схема, реестр и openapi
совпадают, токен решения отсекается `decision_purpose`.

## Амендмент 2026-10-03 (TASK-001333): видимость по пространствам в `local` и `shadow`

Абзац «В `local` и `shadow` фильтра нет» раздела «Решение» заменён
[ADR-0082](0082-principal-profile-and-workspace-visibility.md) п.3.4: для
контекста человека, у которого хотя бы одна активная связка в режиме
`members`, `visible_objects(ctx, action, "workspace")` в `local` и `shadow`
отдаёт множество видимых пространств (участие и потомки), а в `policy` —
пересечение ответа PDP с этим множеством; `authorize(…,
resource=ResourceRef("workspace", id))` вне множества — `404`, не `403`
(ADR-0082 п.3.6–3.7). В режиме `tenant` прежний абзац действует как был:
фильтра нет, ответ `None`.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/config.py, pattern: 'authz_mode: str = "local"'}
  repo: control-plane
- grep: {path: src/control_plane/application/authorization.py, pattern: '^async def authorize\('}
  repo: control-plane
- grep: {path: src/control_plane/application/authorization.py, pattern: 'authz_shadow_divergence_total'}
  repo: control-plane
- grep: {path: src/control_plane/application/authorization.py, pattern: '"policy_unavailable"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/lists.py, pattern: 'visible_objects\(ctx, Permission\.TASKS_READ, "workspace"\)'}
  repo: control-plane
- file: authz/catalog.yaml
  repo: control-plane
- grep: {path: src/control_plane/api/v1/authz.py, pattern: '"/authz:check"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/authz_check.py, pattern: 'approvals\.decision_gate'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/authz_check.py, pattern: 'approval_preconditions\.read_preconditions'}
  repo: control-plane
```
