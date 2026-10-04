# ADR-0082: Профиль участника и видимость связки по пространствам

Статус: Accepted (2026-10-03), фича `people-access`, задача T001
(TASK-001333). Решение суперпроекта — TAI-ADR-0068 «Сведения о человеке, его
права и видимость по пространствам» (принято владельцем 2026-10-03); spec,
plan и документ задач — `specs/people-access/` суперпроекта (FR-001, FR-002,
FR-005, FR-006, FR-007, FR-009; конституция ст. V, VI, IX). Реализация — T002
(профиль), T003 (видимость: связка и шов), T004 (покрытие маршрутов и
реестр-тест).

Связано: [ADR-0008](0008-workspace-hierarchy.md) (дерево пространств),
[ADR-0010](0010-task-requirements-eligibility.md) (участие и роли в
поддереве), [ADR-0053](0053-iam-identity-source-and-binding-api.md) (связка
IAM-identity → principal и её upsert),
[ADR-0055](0055-policy-authorize-and-shadow-mode.md) (`authorize()` и
`visible_objects()`, режимы `local`/`shadow`/`policy`; п.3.4 заменяет его
абзац «в `local` и `shadow` фильтра нет», амендмент там же),
CP-ADR-0081 «Настройки пакета» (форма ошибок полей; ветка
`feature/package-settings`, файла на этой ветке ещё нет),
[ADR-0068](0068-event-filters-catalog-versions.md) (журнал по пространствам,
каталог и версии событий), [ADR-0073](0073-agent-registry.md) (principal и
связка агента реестра), [ADR-0077](0077-principal-disable.md) (отключение
principal, кэш связок).

## Контекст

Владелец хочет менять сведения о человеке, его права, роли и видимость. Роли
уже правятся (`/principals/{id}/roles`). Чего нет в ядре:

- **Сведений.** У principal есть `displayName` и `metadata`, но маршрута
  правки нет: имя на staging меняли в базе. `metadata` — свободная форма, её
  нельзя отдать консоли на правку: неизвестная форма и место для секретов.
- **Видимости.** Права связки (`iam_principal_bindings.permissions`) действуют
  на весь tenant: человек с `tasks.read` видит работу всех пространств.
  Участие (`workspace_members`) — метаданные подбора исполнителей, не
  ограничение. Шов для ограничения есть — `authorize(…,
  resource=ResourceRef("workspace", id))` и `visible_objects(ctx, action,
  "workspace")` (ADR-0055), — но в режиме `local` он ничего не сужает:
  сужение давал только замороженный policy-service.

Права — плоский набор permissions связки; его замена уже есть
(`POST /principals/{id}/iam-bindings`). Наборы прав («наблюдает»,
«работает», «управляет организацией») — константа консоли поверх каталога
прав ядра; ядро их не заводит (TAI-ADR-0068 п.4), и это решение их не
касается.

## Решение

### 1. Профиль участника: `PATCH /principals/{id}`

1. **Маршрут** `PATCH /api/v1/principals/{principal_id}`, право
   `principals.write` (нового права нет). Тело — `PrincipalUpdateRequest`
   `{displayName?, profile?}`, закрытая схема.
   - `displayName` — строка 1…200 (как при создании);
   - `profile` — **полная замена** профиля объектом `PrincipalProfile`; поле,
     которого нет в объекте, удаляется из профиля; `{}` очищает профиль.

   Ошибки формы тела — по п.1.4, не общим обработчиком ядра.
2. **`PrincipalProfile`** — закрытая схема; все поля необязательны, убрать
   поле — не передать его:

   | Поле | Тип | Ограничение |
   |---|---|---|
   | `jobTitle` | string | 1…200 |
   | `email` | string | формат почты (`format: email`), ≤ 254 |
   | `phone` | string | 1…50 |
   | `note` | string | 1…2000 |

   Профиль — отображаемые сведения в организации. Identity и вход остаются в
   IAM; имя identity ведёт IdP и в ядро обратно не копируется (TAI-ADR-0068
   п.3). `metadata` маршрут не меняет.
3. **Версия.** У principal появляется `version` (int, у существующих — `1`).
   `If-Match: "principal-<version>"` обязателен; отказы `428`/`400`/`409` —
   общая форма `api/etag.py`, как у `PATCH /workspaces/{id}` и настроек
   пакета (порядок — п.1.4). Версия растёт на каждое изменение `displayName`
   или `profile` любым писателем (и публикацией агента реестра, меняющей имя
   его principal); смена статуса (`:disable`/`:enable`) версию не трогает.
   `GET /principals/{id}` и ответ `PATCH` отдают заголовок `ETag:
   "principal-<version>"`.
4. **Порядок проверок и форма ошибок.** Форма ошибок полей одна на
   платформе — та же, что у настроек пакета (CP-ADR-0081 п.4.3) и что
   читает консоль (`docs/people.md` консоли, решение 2026-10-03):
   `details.errors` — массив, `path` элемента — JSON Pointer поля в теле
   (`/displayName`, `/profile/<поле>`, `/` — корень тела). **Значения поля
   в ошибке нет** — ни в `message`, ни в отдельном поле, ни эхом входа.
   Порядок — от дешёвого к содержательному, первая сработавшая отвечает:

   1. Право `principals.write` — иначе `403 forbidden` (право — первым, как
      у всех маршрутов ядра).
   2. `If-Match` нет — `428 if_match_required`; не той формы — `400
      invalid_if_match` (`api/etag.py`), как в CP-ADR-0081 п.4.1.
   3. Principal не найден (в том числе чужого tenant'а) — `404 not_found`.
   4. **Материал секрета** в любой строке тела — `displayName` и каждой
      строке `profile` — проверка `reject_secret_text`
      (`domain/redaction.py`) — `422 secret_material_rejected`,
      `details.errors: [{path}]`: `/displayName`, `/profile/<поле>`; для
      имени члена объекта `profile` (лишнего поля) — путь объекта, в
      котором оно стоит (`/profile`, для члена корня — `/`). Поля `field`
      и `match` (вид найденного материала, без самого материала) допустимы
      только **в дополнение** к `path` внутри элемента. Проверка идёт до
      схемы: значение не попадает ни в базу, ни в журнал, ни в эхо ошибки
      схемы.
   5. **Форма тела** — `422 validation_error`, `details.errors: [{path,
      code?, message?}]` — все нарушения сразу; `code` — ключевое слово JSON
      Schema (`type`, `minLength`, `maxLength`, `format`,
      `additionalProperties`), `message` — пояснение без значения. Сюда
      входят: `null` в `displayName` или `profile` (`type`), пустая строка
      (`minLength`), превышение длины (`maxLength`), неверная почта
      (`format`), лишнее поле тела или профиля (`additionalProperties`,
      `path` — путь лишнего поля: `/profile/age`), тело не объект (`type`,
      `path` `/`). Общий обработчик ядра (`400 invalid_request` с `loc`) для
      тела этого маршрута не отвечает: маршрут принимает тело сам и
      проверяет его в этом порядке.
   6. **Агент реестра** (principal с неотозванной записью `agents`) — `409
      principal_managed_by_registry` (`details.agent` — ключ агента): его
      имя и описание задаёт ревизия агента (ADR-0073), правка мимо реестра
      разошлась бы с ней при следующей публикации. Агент и сервис без
      записи реестра, человек — правятся. Отключённый (`disabled`)
      principal правится: сведения об ушедшем человеке остаются в истории
      работы.
   7. `If-Match` не совпал с текущей версией — `409 version_conflict`,
      `details.currentVersion` (и `details.expectedVersion`, как в
      `api/etag.py`). Проверка — под блокировкой строки principal
      (`SELECT … FOR UPDATE`), поэтому из двух одновременных `PATCH` с одной
      версией проходит ровно один.
5. **Повтор.** Маршрут идёт через общий write-flow: повтор с тем же
   `Idempotency-Key` и телом — сохранённый ответ, с другим телом — `409
   idempotency_key_reused`. Тело, которое ничего не меняет (те же значения),
   — `200` без роста версии и без события.
6. **Событие `principal.updated`** (v1, сущность `principal`, автор —
   вызывающий) в той же транзакции:
   `{"principalId", "version", "changes": ["displayName", "profile.email", …]}`
   — `changes` — имена изменённых полей (поле профиля — `profile.<поле>`),
   **без значений**: событие уходит в журнал, память и подписчиков, а
   почта и телефон человека — не их данные. Новое значение читают через
   `GET /principals/{id}` по праву `principals.read`.
7. **`PrincipalOut`** получает `profile` (объект `PrincipalProfile`, у
   существующих — `{}`) и `version`. Поля только добавлены: прежние
   клиенты не ломаются. `RoleHolderOut` и прочие краткие формы не меняются.

### 2. Режим видимости связки: `iam_principal_bindings.visibility`

1. **Колонка** `iam_principal_bindings.visibility` — text, `NOT NULL`, по
   умолчанию `tenant`, `CHECK (visibility IN ('tenant', 'members'))`.
   Миграция ставит `tenant` всем существующим связкам: поведение не
   меняется, пока администратор не переведёт человека в `members` (SC-004).
2. **Upsert связки** (`POST /principals/{id}/iam-bindings`,
   `IamBindingUpsertRequest`) принимает `visibility?: "tenant" | "members"`.
   Отсутствие поля — **без изменения**: у новой связки — `tenant`, у
   существующей — прежнее значение (консоль, меняющая только права, не
   сбрасывает видимость). `null`, другая строка или не строка — `422
   validation_error`, `details.errors: [{path: "/visibility", code}]` — та
   же форма, что в п.1.4 (`code` — `type` или `enum`), а не `400
   invalid_request` общего обработчика. `IamBindingOut` получает
   `visibility`.
3. **Только человек.** `members` принимается лишь для principal `kind =
   human`; для `agent`/`service` — `422 visibility_requires_human`,
   `details.errors: [{path: "/visibility"}]`.
   Видимость агента задаёт его описание в пакете (`spec.work.workspace`,
   `identity.permissions`, ADR-0073; TAI-ADR-0068 п.5), а связку агента
   реестра upsert и так не принимает (`409 agent_identity_conflict`).
4. **След (FR-009).** `iam_binding.created`/`iam_binding.updated` получают
   поле `visibility` — **payload v2** (поле только добавлено, ADR-0068 п.4);
   изменение одной лишь видимости — тоже `iam_binding.updated`. Участие уже
   оставляет след (`workspace.member_added|member_removed`).
5. **Кэш связки.** Upsert, как сейчас для прав, сбрасывает кэш связки в
   своём процессе; права связки в других процессах действуют не позже TTL
   кэша (`iam_binding_cache_ttl_seconds`) — известное ограничение
   ADR-0077. **Видимость из кэша связки не читается** (п.3.2): смена режима
   действует со следующего запроса в любом процессе и для любого входа
   principal, без ожидания TTL.

### 3. Семантика `members` и локальный шов

1. **Видимые пространства** человека в режиме `members` — пространства, где
   он участник (`workspace_members`), и все их потомки (как охват роли в
   поддереве, ADR-0010). Участие и в родителе, и в потомке даёт поддерево
   родителя один раз. Источник истины «где человек» — только участие;
   второго списка у связки нет (TAI-ADR-0068, «Отвергнуто»).
2. **Режим контекста.** `AuthContext` получает `visibility` (`tenant` |
   `members`). Он `members`, если **хотя бы одна** активная связка principal
   вызывающего — `members`, для любого способа входа: IAM-токена любой из
   его identity и API-ключа. Иначе человек в `members` обходил бы
   ограничение вторым входом. Контекст агента, сервиса и `SystemContext`
   (worker) — всегда `tenant`, чтений нет.
3. **Режим и множество — на запрос, не из кэша.** Кэш связки ключуется
   identity (`issuer`, `iam_principal_id`) и сбрасывается только по ней, а
   режим — свойство principal по всем его связкам: из кэша смена видимости
   одной связки не дошла бы до записей других identity и API-ключа. Поэтому
   для контекста человека (`kind = human`) резолвер при входе читает
   режим одним запросом `EXISTS` по активным связкам principal с
   `visibility = 'members'` (индекс `ix_iam_bindings_tenant_principal`).
   При `members` локальный authorizer тем же соединением читает участие и
   дерево одним рекурсивным запросом (как `workspace_subtree_ids`) и
   держит множество до конца запроса. Смена режима любой связки и смена
   участия действуют со следующего запроса в любом процессе. Цена — одно
   индексное чтение на запрос человека в режиме `tenant`; у агентов и
   сервисов её нет.
4. **`visible_objects(ctx, action, "workspace")`** в режимах `local` и
   `shadow` отдаёт это множество при `members` и `None` при `tenant` — тот
   же контракт, что у режима `policy` (ADR-0055), поэтому существующие места
   вызова (список работы, цели, правила, процессы, контекст памяти) сужаются
   без правки. В режиме `policy` при `members` результат — **пересечение**
   ответа PDP и множества участия: видимость — ограничение ядра поверх
   любого источника решений, а не замена его. Для прочих типов ресурса
   (`memory_namespace` и др.) локальный ответ не меняется.
5. **Строго, без личных исключений.** Место вызова, которое в режиме
   `policy` добавляет к множеству «свою» работу (`queries/lists.py`:
   владелец, исполнитель, автор), при `members` этого не делает: работа
   человека в чужом пространстве ему не видна. Консоль предупреждает до
   перевода в `members`, сколько его работы окажется вне видимости, и что без
   участия он не увидит никакой работы (TAI-ADR-0068, «Последствия»).
6. **`authorize(…, resource=ResourceRef("workspace", id))`** при `members` и
   `id` вне множества — **`404`**, не `403`: ответ неотличим от
   отсутствующего пространства (`not_found`, то же сообщение и `details`,
   что у маршрута для несуществующего id). Проверка прав идёт первой:
   нехватка права — по-прежнему `403`, видимость его не маскирует.
7. **Объект пространства.** Резолвер объекта, живущего в пространстве
   (задача, согласование, артефакт, прогон, захват, экземпляр процесса,
   проект), после чтения проверяет `workspace_id` объекта по множеству; вне
   множества — тот же `404`, что для отсутствующего объекта этого маршрута
   (`not_found`, «Task not found» и т.п.). Списки фильтруют по множеству
   (`IN`), объекта вне множества в них нет; `nextCursor` не выдаёт его
   существования. Решения, изменения и вызовы над невидимым объектом
   отклоняются так же, как чтение (FR-007): `:approve` чужого согласования —
   `404`.

   **Тот же ответ, что «не существует», байт в байт.** Резолверы, которые
   уже зовут `authorize(…, resource=ResourceRef("workspace", <пространство
   объекта>))` после чтения (`get_goal`, `get_rule`, `get_instance` и
   действия экземпляра процесса, определение процесса с пространством),
   получили бы из шва `404` **пространства** — по тексту («Workspace not
   found» вместо «Goal not found») и `details` было бы видно, что объект
   есть. Поэтому отказ по видимости в `authorize()` — отдельное исключение
   (`WorkspaceNotVisible`, подкласс `NotFoundError`), а резолвер объекта
   переводит его в **свой** `NotFoundError` с тем же сообщением и теми же
   `details`, что у ветки «строки нет» (`{"goalId": …}`, `{"instanceId":
   …}`). Не перехваченное резолвером исключение отвечает `404` пространства
   — это верно только для маршрутов, где ресурс и есть пространство (п.3.6).
   Тест T003 сравнивает тело ответа для невидимого и несуществующего id по
   каждому такому резолверу.
8. **Объекты уровня tenant** видимость не ограничивает — их ограничивают
   права: principals, роли, способности, скиллы, типы работы, типы
   пространств и артефактов, агенты, пакеты и определения процессов,
   календари, правила без пространства, формы экранов, инструменты, события
   без `workspace_id`. Сессии вызывающего — его собственные.
9. **Дерево.** `GET /workspaces` и `/workspaces/tree` при `members` отдают
   только видимые пространства; `parentId` видимого корня, чей родитель
   невидим, отдаётся `null` — ответ не называет невидимое пространство.
10. **Агентов решение не трогает** (п.2.2). Делегирование человека агенту
    видимость не переносит: агент действует своим контекстом. Сужать
    делегирование человека в `members` — отдельное решение, если понадобится
    (см. «Открытые вопросы»).

### 4. Маршруты, обязанные пройти шов

Аудит `main` на 2026-10-03 (plan, «Исследование» п.5). **Уже проходят**
через `visible_objects`: `GET /tasks` (`queries/lists.py`), `/goals`,
`/rules`, `/process-instances` и журнал экземпляра, `/process-definitions`
(список с пространством), `/context` и `/context/recall`, `:replay`; часть
команд — артефакты, знания, скиллы, связи пакетов. В режиме `members` они
сужаются по п.3.4 без правки, кроме личных исключений `lists.py` (п.3.5).

**Обязаны пройти** (T003 — первая строка, T004 — остальные):

| Область | Маршруты | Как |
|---|---|---|
| Работа по ссылке и всё «под работой» | `GET/PATCH /tasks/{task_ref}`, `/claimability`, `/transitions`, `/verifications`, `/relations` (и удаление), `/requirements`, `:claim`, `:complete`, `:migrate-type`, `:start-run`, `/tasks/{task_ref}/comments/**` | одна проверка в разрешении задачи по ссылке (`queries.get_task`, `relations.resolve_task`, `tasks.resolve_task_for_update`): пространство задачи |
| Создание работы | `POST /tasks` в пространство | `authorize(resource=workspace)` → `404` |
| Согласования | `GET /approvals`, `GET /approvals/{id}`, `:approve`, `:reject`, `:cancel`, `/outcome`, `:replay-outcome`, `POST /approvals` | пространство согласования (с задачей — пространство задачи, ADR-0068 амендмент 2026-09-25); без пространства — уровень tenant |
| Артефакты | `GET /artifacts`, `GET /artifacts/{id}`, `/content`, `:purge-content`, `POST /artifacts` | пространство задачи артефакта |
| Прогоны и захваты | `GET /runs`, `/runs/{run_id}/**` (контекст, checkpoints, actions, control-messages, переходы), `/runs/{run_id}/child-handles`, `/child-handles/**`, `GET /claims`, `/claims/{claim_id}/**` | пространство задачи прогона и захвата |
| Вызовы навыков | `/skill-invocations/{id}` и переходы | пространство задачи вызова; вызов без задачи — уровень tenant |
| Журнал событий | `GET /events`, `WS /events/ws` | `events.workspace_id` в множестве или `NULL` |
| «Ждёт вас» | `GET /me/attention`, `:feedback` | элементы видимых пространств (`queries/attention.py`) |
| Экраны пакетов | `GET /views/{view_key}` и данные источников вида | данные источника — по множеству (`queries/views.py`); форма вида — уровень tenant |
| База знаний | `POST /knowledge/entities:query`, `/workspaces/{id}/knowledge-packs` | `workspaceId` запроса вне множества — `404` |
| Пространства и участники | `GET /workspaces`, `/workspaces/tree`, `GET/PATCH /workspaces/{id}`, `:archive`, `:move`, `/members`, `/participants`, `:remove` | `authorize(resource=workspace)` и п.3.9 |
| Проекты | `/projects/{project_id}/**`, `GET /projects` | пространство проекта |
| Пакеты контекста | `GET /context-packs/{pack_id}`, `:replay` | пространство задачи пакета: невидимая — `404` того же вида, что «пакета нет» |
| Контекст исполнителя | `GET /harness/context`, `GET /work/available` | `/work/available` — по множеству, как `GET /tasks`; в контексте сессии — только задачи видимых пространств |
| Внешние ссылки | `GET/POST /external-references`, `/projects/{project_id}/external-references` | пространство сущности ссылки (задача, проект, цель); поиск по `externalId` не выдаёт сущность вне множества; ссылка на сущность уровня tenant — уровень tenant |
| Документы знаний | `POST /knowledge/documents` | `workspaceId` тела вне множества — `404`, как у `entities:query` |
| Процессы по id | `GET /process-instances/{instance_id}`, `/journal`, `:suspend`, `:resume`, `:cancel` | `get_instance` → пространство экземпляра, ответ «не существует» по п.3.7 |

**Уровень tenant, названы явно** (метка `# visibility: tenant — причина`):

| Маршруты | Почему |
|---|---|
| `GET/POST /delegations`, `/delegations/{id}:revoke` | у делегирования нет пространства: это связь principal-человека с principal-агентом; ограничено правом и тем, что человек делегирует только своё. Сужение делегирования — «Открытые вопросы» |
| Настройки пакета (CP-ADR-0081): `GET /package-settings`, `GET/PUT /packages/{key}/settings`, `/settings/versions` | tenant-объект: значения настроек пакета общие для организации; метки поставлены при сходимости people-access с main (2026-10-04) |
| Подключения (CP-ADR-0079): `/connections/**`, `/connection-types/**`, `/agents/me/connections/**`, секреты агента `/agents/{key}/secrets/**` | tenant-объект: подключение — уровень организации, пространства у него нет; подключения и секреты агента реестра — как сам агент (п.3.8); метки поставлены при той же сходимости |
| Каталог событий (CP-ADR-0068, амендмент А): `GET /event-types` | описание типов событий платформы, данных пространств в нём нет; сами события фильтрует `GET /events` (В7) |

**Реестр-тест** (T004, `tests/unit/test_visibility_coverage.py`, по образцу
правила `authz-coverage`) обходит все маршруты `/api/v1` приложения. Каждый
маршрут — ровно в одном классе:

- **шов** — запись в реестре теста `(метод, путь) → резолвер или фильтр`,
  которым он проходит шов; запись о несуществующем маршруте роняет тест;
- **tenant** — над декоратором маршрута комментарий
  `# visibility: tenant — <причина>` (английский текст причины, RUF003);
  метка без причины роняет тест.

Маршрут без класса роняет тест: новый маршрут решает про видимость явно.
Реестр доказывает только классификацию; поведение — сквозной тест T004 на
пакете `invoice-payment`: человек `members` в своём пространстве не получает
работы, согласования, артефакта, прогона, события другого пространства ни
списком, ни по ссылке, ни журналом (SC-002).

### 5. Черновик изменений OpenAPI

Только добавления; окончательную схему публикует код T002/T003 (снимок
openapi — контракт консоли, ст. V).

```yaml
paths:
  /api/v1/principals/{principal_id}:
    get:
      responses:
        "200":
          headers:
            ETag: {schema: {type: string, example: '"principal-3"'}}
          content:
            application/json: {schema: {$ref: "#/components/schemas/PrincipalOut"}}
    patch:
      summary: Change the display name and profile of a principal
      parameters:
        - {name: principal_id, in: path, required: true, schema: {type: string, format: uuid}}
        - {name: If-Match, in: header, required: true, schema: {type: string, example: '"principal-3"'}}
        - {name: Idempotency-Key, in: header, required: false, schema: {type: string}}
      requestBody:
        required: true
        content:
          application/json: {schema: {$ref: "#/components/schemas/PrincipalUpdateRequest"}}
      responses:
        "200":
          headers:
            ETag: {schema: {type: string}}
          content:
            application/json: {schema: {$ref: "#/components/schemas/PrincipalOut"}}
        "400": {description: invalid_if_match}
        "403": {description: forbidden (principals.write)}
        "404": {description: not_found}
        "409": {description: version_conflict | principal_managed_by_registry | idempotency_key_reused}
        "422":
          description: validation_error | secret_material_rejected (details.errors, no field values)
          content:
            application/json: {schema: {$ref: "#/components/schemas/FieldErrorResponse"}}
        "428": {description: if_match_required}
  /api/v1/principals/{principal_id}/iam-bindings:
    post:
      responses:
        "422":
          description: validation_error (path /visibility) | visibility_requires_human
          content:
            application/json: {schema: {$ref: "#/components/schemas/FieldErrorResponse"}}
components:
  schemas:
    FieldError:            # one platform shape, as in CP-ADR-0081
      type: object
      required: [path]
      properties:
        path: {type: string, description: JSON Pointer into the request body, example: /profile/email}
        code: {type: string, description: JSON Schema keyword, example: format}
        message: {type: string, description: explanation without the value}
        field: {type: string, description: secret_material_rejected only, in addition to path}
        match: {type: string, description: secret_material_rejected only, kind of material, never the material}
    FieldErrorResponse:
      type: object
      required: [error]
      properties:
        error:
          type: object
          required: [code, message, details]
          properties:
            code: {type: string, enum: [validation_error, secret_material_rejected, visibility_requires_human]}
            message: {type: string}
            details:
              type: object
              required: [errors]
              properties:
                errors: {type: array, minItems: 1, items: {$ref: "#/components/schemas/FieldError"}}
    PrincipalProfile:
      type: object
      additionalProperties: false
      properties:
        jobTitle: {type: string, minLength: 1, maxLength: 200}
        email: {type: string, format: email, maxLength: 254}
        phone: {type: string, minLength: 1, maxLength: 50}
        note: {type: string, minLength: 1, maxLength: 2000}
    PrincipalUpdateRequest:
      type: object
      additionalProperties: false
      properties:
        displayName: {type: string, minLength: 1, maxLength: 200}
        profile: {$ref: "#/components/schemas/PrincipalProfile"}
    PrincipalOut:          # added properties
      required: [profile, version]
      properties:
        profile: {$ref: "#/components/schemas/PrincipalProfile"}
        version: {type: integer, minimum: 1}
    BindingVisibility:
      type: string
      enum: [tenant, members]
    IamBindingUpsertRequest:   # added property; absent = unchanged
      properties:
        visibility: {$ref: "#/components/schemas/BindingVisibility"}
    IamBindingOut:             # added property
      required: [visibility]
      properties:
        visibility: {$ref: "#/components/schemas/BindingVisibility"}
```

Каталог событий (`make event-catalog`): новый тип `principal.updated` v1;
`iam_binding.created`/`iam_binding.updated` — v2 с полем `visibility`.

### 6. Данные

Одна или две миграции (T002 и T003 — по своей части, одна голова alembic):
`principals.version` (int, `NOT NULL`, по умолчанию `1`),
`principals.profile` (jsonb, `NOT NULL`, по умолчанию `{}`),
`iam_principal_bindings.visibility` (text, `NOT NULL`, по умолчанию
`tenant`, CHECK). Индекс `ix_workspace_members_principal` уже есть и
покрывает чтение участия.

## Последствия

- После выкатки ничего не меняется: все связки `tenant`, все профили пусты,
  версии `1` (SC-004).
- Каждый новый маршрут объекта пространства обязан пройти шов или носить
  метку `# visibility: tenant — причина` — иначе падает реестр-тест.
- Запрос человека делает одно индексное чтение режима (п.3.3), в режиме
  `members` — ещё одно чтение участия и дерева; фильтр — `IN` по множеству. Человек с участием в корне дерева видит всё,
  как в `tenant`, но платит это чтение.
- Видимость не заменяет права: `members` с `tasks.read` видит работу своих
  пространств, без `tasks.read` — ничью.
- Права на управление людьми: `principals.write` (сведения, права, видимость),
  `workspaces.manage` (участие), `org.manage` (роли). Выдать право шире
  своего ядро не даёт, как и прежде (ADR-0053).

## Отвергнуто

- **Разморозить policy-service** для видимости — решает больше, чем нужно,
  и вводит вторую модель прав (TAI-ADR-0068).
- **Список видимых пространств у связки** — второй источник «где человек»
  рядом с участием.
- **Режим видимости у principal** — у агента своя модель видимости, у
  человека без входа видимость смысла не имеет; связка — уже то место, где
  лежит «что этот вход может здесь».
- **`403` для невидимого** — раскрывает существование и номер работы.
- **Только пространства участия, без потомков** — ломает дерево
  «Компания → отдел».
- **Значения полей в `principal.updated`** — почта и телефон уехали бы в
  журнал, память и подписчиков.
- **Режим по связке входа, а не по principal** (п.3.2) — человек в
  `members` обходил бы ограничение API-ключом или второй identity.

## Открытые вопросы

- Делегирование человека в режиме `members` агенту (`POST /delegations`) видимость не
  сужает; нужно ли это — решение владельца по первому сценарию.

## Conformance

- `tests/unit/test_visibility_coverage.py` (T004) — реестр маршрутов.
- Интеграционные тесты T002: правка имени и профиля, `409 version_conflict`
  с `details.currentVersion`, `428` без `If-Match`, `400` при неверной форме,
  `409 principal_managed_by_registry`, `403` без `principals.write`, `422
  secret_material_rejected` с `details.errors[].path` (`/displayName`,
  `/profile/note`) и без значения в теле ответа, `422 validation_error` с
  JSON Pointer на `null`, пустую строку, длину, почту и лишнее поле (не
  `400 invalid_request`), порядок п.1.4 (секрет в теле с неверной формой
  другого поля — `secret_material_rejected`), `principal.updated` с именами
  полей.
- Интеграционные тесты T003: `tenant` без изменений; `members` видит своё
  поддерево; работа чужого пространства по ссылке — `404`; тело `404` для
  невидимого и несуществующего id совпадает у `get_task`, `get_goal`,
  `get_rule`, `get_instance` (п.3.7); upsert без `visibility` не меняет
  режим; неверный `visibility` — `422 validation_error` с `path
  /visibility`; `members` у агента — `422`; смена видимости одной связки
  действует для запроса другой identity того же principal и API-ключа без
  ожидания TTL (п.3.3); после upsert следующий запрос — с новыми правами и
  видимостью (SC-003).
- Сквозной тест T004 на `invoice-payment` (SC-002).

## Амендмент 2026-10-03 (T002, TASK-001334): профиль — уточнения реализации

Профиль (п.1) реализован; ниже — то, что п.1 не называл или называл
описательно. Контракт консоли — openapi маршрута, его закрепляет
`tests/unit/test_principal_profile.py`.

### А1. Код отказа по праву

Отказ без `principals.write` (п.1.4, шаг 1) — `403 permission_denied`: это
код всех отказов по праву в ядре (`AuthorizationError`); «`403 forbidden`»
п.1.4 и черновика п.5 — описание, а не код. Новый код не заводится.

### А2. Тело, которое не разобрать

Тело, которое не JSON (в том числе пустое), для проверок п.1.4 — «тело не
объект»: `422 validation_error`, `details.errors: [{path: "/", code:
"type"}]`, без эха присланного. Проверка секрета над ним ничего не находит:
строк в нём нет. `displayName` перед проверкой формы обрезается по краям, как
в `POST /principals`: имя из одних пробелов — `minLength`. Строки профиля не
обрезаются. Почта — проверка вида `x@y.z` без пробелов (`format`), а не
доставляемости.

### А3. Где что лежит

- Миграция `c8a4e2f6b1d3`: `principals.version` (`NOT NULL`, `1`, `CHECK
  version >= 1`) и `principals.profile` (jsonb, `NOT NULL`, `{}`, `CHECK
  jsonb_typeof(profile) = 'object'`).
- Проверка тела — `domain/principal_profile.py` (`UPDATE_SCHEMA`,
  `parse_update`, `changed_fields`); модели openapi — `PrincipalProfile`,
  `PrincipalUpdateRequest`, `FieldError*` в `api/v1/schemas.py`. Тело маршрут
  читает сам, поэтому в openapi оно описано явно (`openapi_extra`), а
  совпадение описанного и проверяемого — тест.
- Блокировка: целевой principal `FOR UPDATE` вместе с вызывающим в порядке id
  (`lock_caller_and_principal_for_update`, как у `:disable`), поэтому правка
  своего профиля и встречные правки двух администраторов не дают взаимной
  блокировки.
- Порядок шагов 6 и 7 п.1.4: агент реестра называется и при устаревшем
  `If-Match`.
- Повтор с `Idempotency-Key` отдаёт сохранённый ответ с заголовком `ETag`
  его версии.
- Тесты: `tests/integration/test_principal_profile.py` (Conformance T002),
  `tests/concurrency/test_principal_profile_races.py` (из пяти одновременных
  `PATCH` одной версии проходит один, событие одно).

## Амендмент 2026-10-03 (T003, TASK-001335): реализация видимости связки

Б1–Б4 уточняют п.2–3 тем, что решение не называло явно; поведение п.2–3
они не меняют. Б5–Б9 добавлены по ревью первой сдачи T003: Б5 — новое
правило записи связки (по принципу ADR-0053), Б9 меняет цену п.3.3, Б6–Б8
закрывают места, где множество не доходило до ответа.

### Б1. Где вычисляется множество

Режим и множество видимых пространств читает резолвер контекста при входе
(`infrastructure/auth/service.py` → `application/visibility.py`) одним
соединением: `EXISTS` по активным (`status = 'active'`, `revoked_at IS
NULL`) связкам principal с `visibility = 'members'`, при `members` — один
рекурсивный запрос участия и потомков. `AuthContext` несёт `visibility` и
`visible_workspaces` (`None` при `tenant`) до конца запроса; authorizer
(`application/authorization.py`) читает их из контекста и в базу не ходит.
Для API-ключа человека — то же, в транзакции проверки ключа. Контексты,
которые ядро строит само (worker, исход согласования, правила, процессы),
остаются `tenant`.

### Б2. Работа без пространства

Задача без `workspace_id` при `members` невидима — по ссылке `404`, в
списке её нет: множество не содержит `NULL`, как и фильтр `IN` списка
(п.3.7). Цели, правила и экземпляры процессов без пространства — объекты
уровня tenant (п.3.8) и видны, как и в их списках.

### Б3. Резолверы объекта

Проверку п.3.7 делают сами резолверы строки: `queries.get_task`,
`relations.resolve_task`, `tasks.resolve_task_for_update`,
`goals.get_tenant_goal`, `work_rules.get_tenant_rule`,
`process_instances.get_instance` — ветка «строки нет» и ветка «невидимо»
общие, поэтому тело `404` совпадает байт в байт и для правок и действий над
объектом. Определение процесса с пространством переводит
`WorkspaceNotVisible` в свой `404` («Process definition not found»).

### Б4. Identity, переданная не человеку

Upsert, переводящий identity на principal `agent`/`service` без поля
`visibility`, ставит связке `tenant`: сужение человека не переходит к
агенту или сервису (п.2.3). Связки реестра агентов и bootstrap — всегда
`tenant`; их события `iam_binding.created|updated` тоже v2 с `visibility`.

### Б5. Шире своего не выдать: `403 visibility_escalation`

Принцип выдачи ключей и связок (ADR-0053: вызывающий не выдаёт больше, чем
держит сам) распространяется на видимость. Вызывающий в режиме `members` не
оставляет связку `tenant` ни себе, ни другому:

- явный `visibility: "tenant"`;
- новая связка без поля (по умолчанию `tenant`, п.2.2);
- повторное открытие отозванной или отключённой `tenant`-связки без поля;
- перенос identity на другого principal без поля, когда связка `tenant` или
  принимающий principal — агент или сервис (у них только `tenant`, Б4).

Отказ — `403 visibility_escalation`, `details.errors: [{path:
"/visibility"}]`, до записи. Без поля разрешено только менять права уже
активной `tenant`-связки, остающейся на своём principal: видимость от этого
не расширяется (права проверяет правило ADR-0053). Следствие: человек в
`members` не привязывает identity агентам и сервисам.

Порядок проверок upsert: `principals.write` — первой, затем значение
`visibility` (`422 validation_error`, п.2.2), затем `visibility_requires_human`
и `visibility_escalation`. Без права ответ — `403` при любом теле.

### Б6. Предикаты прав и `WorkspaceNotVisible`

Места, где `authorize` — предикат («можно ли», а не «запретить»): данные
источников вида (`queries/view_data.py`, `queries/views.py`), календари,
испытания пакета, чтение артефакта исполнителем навыка, — считают
`WorkspaceNotVisible` тем же «нельзя», что и `AuthorizationError`, через
общий помощник `permits()` (`application/authorization.py`); иначе `404`
пространства уходил бы наружу и называл его. Чтение артефакта
пространства, публикация новой версии и вывод из оборота определения
процесса чужого пространства переводят `WorkspaceNotVisible` в свой `404`
(«Artifact not found», «Process definition not found»).

### Б7. Перенос работы

`PATCH /tasks/{task_ref}` с `workspaceId` проверяет целевое пространство по
множеству **до** его статуса и разрешённых типов работы: невидимое
отвечает `404` «Workspace not found» с теми же `details`, что
отсутствующее, — ни `workspace_archived`, ни `task_type_not_allowed` его не
выдают.

### Б8. Контекст и память

`POST /context` и `POST /context/recall` (и всё, что строит область графа:
`:replay`, `entities:query`) с `workspaceId` вне множества — `404`
«Workspace not found», как для отсутствующего; `projectId` проекта
невидимого пространства — «Project not found». Чтение памяти при `members`
в режимах `local` и `shadow` сужается так же, как `policy` (п.3.4):
`allowedScopes` — `workspace:<id>` видимых пространств и scopes самого
principal; `allowedNamespaces` — namespace tenant и namespaces корней деревьев, в
которых лежат видимые пространства (как и прежде в режиме `local`, личный
namespace principal ядро не читает). В режиме `policy` из ответа PDP остаются только namespaces
этих корней. Элемент памяти без scope видимости — уровень namespace
(MEM-ADR-019) и виден, как и прежде.

### Б9. Вид principal — локальный

Человек ли principal, резолвер видимости читает из `principals.kind` тем же
запросом `EXISTS`, а не из `AuthContext.principal_kind`, который у
IAM-токена берётся из claim `principal_type`: identity человека с claim
агента или сервиса сужения не обходит. Цена п.3.3 меняется: одно индексное
чтение на запрос платят и агенты, и сервисы. Вместе с множеством тот же
запрос отдаёт корни деревьев видимых пространств (для Б8).

## Амендмент 2026-10-03 (T004, TASK-001336): покрытие маршрутов, реестр-тест, хвосты T003

T004 проводит шов п.3.7 через все маршруты таблицы п.4 и закрывает хвосты
ревью T003 (комментарий владельца задачи от 2026-10-03). В1–В4 — новые
решения, о которых спрашивало ревью; В5–В9 называют то, что п.3–4 говорили
описательно, и места, где шов проходит.

### В1. API-ключ человека без активной связки

Режим — свойство principal по его **активным** связкам (п.3.2). Когда у
человека не остаётся ни одной активной связки (отозвана единственная
`members`-связка), входом остаются его API-ключи, и по п.3.2 они расширились
бы до `tenant`. Решение: человек без активной связки сохраняет **самую узкую
видимость, которую записала любая его связка** — если хоть одна его связка
(в любом статусе) `members`, режим `members`. Пока активные связки есть,
решают только они. Расширить видимость — явное действие администратора:
открыть связку заново с `visibility: "tenant"` (или выдать другую активную
`tenant`-связку); отзывать ключи ядро не стало — отзыв связки не должен
молча ломать автоматизацию человека. Тот же запрос `EXISTS` п.3.3 (Б9),
индекс `ix_iam_bindings_tenant_principal`.

### В2. Контексты из снимка полномочий человека

Б1 оставлял `tenant` контекстам, которые ядро строит само. Для контекстов,
собранных из **снимка полномочий человека**, это расширяло его видимость:
правило без пространства, включённое человеком в `members`, заводило работу
в чужом пространстве; исход согласования (`ensureWork` по ключу,
`spawnedBy`) читал работу вне видимости решившего. Решение — **сужать**: такой
контекст получает видимость человека, прочитанную заново при исполнении
(`with_visibility`, как запрос, п.3.3), а не снятую при включении или
решении. Это контекст правила со снимком включившего (`rule_context`),
решающего согласование при исполнении исхода (`_decider_context`),
завершившего работу и решившего согласование для проверок приёмки
(`verification`). Контекст агента правила или процесса и контекст ядра
(`core`) по-прежнему `tenant`: их видимость — описание в пакете (п.2.3).
Отказ шва в таком контексте — отказ действия (`404` пространства, работы
или «Task not found» по ключу), как у запроса человека.

### В3. Связка агента через реестр: Б5

`POST /agents/{key}/identity:replace` (право `agents.manage`) и
`PUT /agents/{key}/identity` создают или открывают связку агента или
сервиса — всегда `tenant` (Б4). Вызывающий в `members` получает `403
visibility_escalation` (форма Б5) до записи: registry identity и связки не
меняются. Повтор `identity:replace` с уже привязанной identity ничего не
пишет и проходит.

### В4. Невидимые предусловия и шлюзы не называются

Предусловие (`depends_on`, `blocks`) и шлюзовое согласование вне видимости
задачу по-прежнему держат — готовность одна для всех, — но не называются:
ни id, ни `publicId`, ни статус. В `/claimability` и в отказе `:claim`
(`409 task_not_ready`, `409 approval_required`) список
`blockedBy`/`pendingApprovals` содержит только видимые, а число невидимых —
`hiddenBlockers`/`hiddenApprovals` (поле есть, только если число больше
нуля; поля добавлены, прежние клиенты не ломаются). Без числа задача
выглядела бы заблокированной без причины. Связь с невидимой задачей не
отдаётся в `GET /tasks/{ref}/relations`, в контексте прогона и не удаляется
(`DELETE …/relations/{id}` — `404` «Relation not found»).

### В5. Архивные пространства — в множестве

Множество п.3.1 строится по участию и дереву без учёта статуса: архивное
пространство, где человек участник, и архивные потомки видимого — видимы.
Видимость отвечает «чьё», а не «живое ли»: архивную работу человек читает,
как и в `tenant`; запись в архивное пространство отклоняет прежняя проверка
(`workspace_archived`), после проверки видимости.

### В6. Объекты и как они проходят шов

Помощники — `application/visibility.py`: условия для списков
(`workspace_condition`, `task_condition`, `approval_condition`,
`artifact_condition`) и предикаты для резолверов (`task_visible`,
`approval_visible`, `artifact_visible`); в режиме `tenant` они ничего не
добавляют. `permits_task` (`application/authorization.py`) — `permits()` по
задаче, прочитанной из базы: `authorize(resource=task:…)` строк не читает и
пространства задачи не видит, поэтому каждое чтение задачи по id для
вызывающего (шаги экрана пакета, `spawnedBy` контекст-пакета и исхода,
работа по ключу) спрашивает `permits_task`.

- **Согласование** видно, если видно его пространство и его работа; без
  обоих — объект tenant. Согласование в пространстве-предке видимой работы
  (ADR-0068) при невидимом предке невидимо.
- **Артефакт** — пространство его работы и его собственное; без обоих —
  объект tenant. Невидимый артефакт — «Artifact not found» и как вход
  видимой работы (`forTask`), и как `supersedesArtifactId`, и в
  `POST /approvals`, и в комментарии (вместо `comment_mismatch`).
- **Наблюдение** — событие журнала без своего пространства: видимо, если
  журнал отдал бы вызывающему его событие (В7). `POST /observations` с
  `runId` невидимой работы и `supersedes` невидимого наблюдения — `404`
  «Run not found» / «Superseded observation not found», как у
  несуществующих; прогон проверяется по своей работе и тогда, когда `task`
  названа отдельно.
- **Доказательства** (`evidence` задачи в `POST /tasks` и `PATCH /tasks`,
  `evidence` происхождения задачи и цели — `origin`, `createdFrom`) не
  называют невидимого: артефакт — по правилу артефакта, наблюдение — по
  правилу наблюдения, контекст-пакет — по своей работе. Невидимый —
  тот же `404` («Evidence … not found» со списком id), что и
  несуществующий: иначе ссылка была бы и оракулом, и ссылкой на чужое.
- **Прогон, захват, контекст-пакет** — пространство их работы; резолверы
  (`_get_tenant_run`, `_get_tenant_claim`, `get_run`, `get_claim`,
  `get_pack_record`) общие для чтения и команд. **Хэндл дочернего
  прогона** — обе его работы. **Вызов навыка** — его работа; без работы —
  объект tenant; очередь исполнителя в `members` не выдаёт вызовов чужой
  работы.
- **Проект** — его пространство (`get_tenant_project`); родительский проект
  невидимого пространства не называется (`parentProjectId: null`).
- **Пространство**: `get_tenant_workspace` и `GET /workspaces/{id}` отвечают
  на невидимое как на отсутствующее, поэтому всё, что принимает
  `workspaceId` (создание работы, целей, правил, проектов, процессов,
  наблюдений, ролей держателей, исполнителей типа), отказывает так же.
  Корнем для вызывающего считается видимое пространство с невидимым
  родителем (`rootsOnly`, дерево), `parentId` такого корня — `null`.
- **Внешние ссылки**: обратный поиск по ключу не находит сущность вне
  множества (та же пустая страница, что для неизвестного ключа); конфликт
  ключа с невидимой сущностью — `409` без `details` о ней.
- **Оракулы**: `includeDescendants=true` с невидимым `workspaceId` у
  `GET /tasks`, `/work/available`, `/me/attention` — `404` пространства, как
  у несуществующего; `GET /artifacts?workspaceId=<невидимое>` — пустая
  страница, как у несуществующего; `POST /context` с `runId` невидимого
  прогона — «Run not found», как у несуществующего.
- **Контекст исполнителя**: `/harness/context` — захваты, прогоны и
  согласования только видимой работы; `/work/available` — по множеству, как
  `GET /tasks`; входы задачи (`inputs` в контексте прогона и `/context`) не
  называют источник из невидимого пространства (готовность по входам
  считается по всем).
- **Массовая миграция типа** (`:migrate-tasks`) не трогает и не считает
  работу невидимых пространств — как работу, на которую нет права.
- **Оценка правила** по id — «Rule evaluation not found», если правило в
  невидимом пространстве.
- **Проверка пакета** (`given.event` теста правила): задача, названная в
  `payload` события (`taskId`, `id` у `task.*`), для вызывающего в
  `members` читается по строке, как `permits_task`: вне видимости или
  несуществующая — один отказ `given_refused … not_found: Task not found`.

### В7. Журнал событий

Читатель в `members` (`GET /events`, `WS /events/ws`) получает события, чей
`events.workspace_id` в множестве, и события без пространства — **кроме
событий о работе без пространства** и о том, что на ней висит (`task`,
`run`, `claim`; `approval`, `artifact`, `skill_invocation` с работой): по Б2
такая работа невидима, а её событие несёт заголовок и номер. Это уточнение
«или `NULL`» таблицы п.4. Подписка WebSocket перечитывает видимость
читателя на каждой порции (`refresh_visibility`): смена режима или участия
доходит до открытой подписки без переподключения, как до следующего
запроса.

Событие без пространства, которое не о сущности с пространством, всё же
может называть работу в `payload`: наблюдение (`observation.recorded` —
`taskId`, `runId`, `workspaceId`), отзыв о «Ждёт вас»
(`attention.feedback_recorded` — `entityType`/`entityId`), событие
проверки пакета. Такое событие читатель в `members` получает, только если
видно всё, что названо: задача — по множеству, прогон — по своей работе,
пространство — в множестве, `entityId` задачи или согласования — по их
правилу. Фильтр смотрит в `payload`, а не в `events.workspace_id`: так он
закрывает и события, записанные до T004, без перезаписи журнала и без
смены `workspaceId` в outbox (его читает память). Наблюдение без
привязки — объект tenant, как прежде.

### В8. Реестр-тест

`tests/unit/test_visibility_coverage.py` обходит маршруты `api_v1_router`
(тот, что монтирует приложение). Шов — словарь `SEAM` `(метод, путь) →
"модуль.функция"`: функция обязана существовать, маршрут — тоже. Tenant —
комментарий `# visibility: tenant — <причина>` строкой прямо над первым
декоратором маршрута (причина по-английски, RUF003). Маршрут в обоих классах
или ни в одном, метка без причины — падение. Маршрут, который принимает
ссылки на несколько объектов (наблюдение — пространство, работу, прогон,
заменяемое наблюдение; задача и цель — доказательства), называет в `SEAM`
кортеж резолверов, по одному на ссылку. Тело каждого резолвера обязано
дойти до шва: тест строит неподвижную точку по AST функций `application`
— функция «видит», если называет примитив шва (`visible_workspaces`,
`sees_workspace`, `permits_task`, условия и предикаты `visibility.py`) или
функцию, которая видит. Это ловит запись `SEAM` с резолвером, не
спрашивающим о видимости; что внутри видящей функции проверена каждая
ссылка, по-прежнему доказывают интеграционные тесты (предел — по имени
функции, а не по ветке кода). Класс tenant получили: агенты
реестра, типы артефактов, задач и пространств, шаблоны проектов, календари,
делегирования, пакеты, роли, способности, скиллы, principals и их связки,
ключи и роли, сессии вызывающего, инструменты, операции журнала и адаптера
памяти, загрузка содержимого артефакта (её использует артефакт — он шов),
регистрация и чтение пакетов знаний, bootstrap.

### В9. Границы

- `effectiveTaskTypes` пространства и эффективная конфигурация проекта
  наследуют значения от невидимых предков: значения отдаются, предок не
  называется.
- Глобальные счётчики и курсоры (`eventCursor`, позиция журнала) общие для
  tenant: они не называют объектов.
- Делегирование — по-прежнему «Открытые вопросы».
- Объекты tenant (В8) называют id пространств, в том числе невидимых:
  `GET /principals/{id}/roles`, `GET /roles?workspaceId=`,
  `GET /roles/{id}/principals` (пространство держателя роли),
  `GET /agents` (пространства агента). Это осознанная граница: роли,
  principals и реестр агентов — объекты tenant под своими правами, а id
  пространства без самого пространства (`GET /workspaces/{id}` — `404`)
  не отдаёт ни названия, ни работы. Сужать эти ответы по множеству —
  отдельное решение, не T004.
- Ключ дедупликации наблюдения `(source, dedupKey)` уникален в tenant:
  повтор ключа, которым уже записано наблюдение невидимой работы, вернёт
  его id (`deduplicated: true`). Ключ — идентичность внешнего факта, а не
  ссылка на работу; само наблюдение читателю в `members` не отдаётся (В7),
  а другой ответ на повтор сломал бы идемпотентность источника.

Conformance T004: `tests/unit/test_visibility_coverage.py` (реестр),
`tests/integration/test_workspace_visibility_routes.py` (маршруты п.4: тело
`404` невидимого и несуществующего id совпадает),
`tests/integration/test_visibility_invoice_payment.py` (сквозной SC-002 на
`invoice-payment`), `tests/integration/test_workspace_visibility_tails.py`
(В1–В3), `tests/integration/test_workspace_visibility_observations.py`
(наблюдения и доказательства, В6–В7), `tests/unit/test_visibility_task_reads.py` (`permits_task` в шагах
экрана, `spawnedBy` контекст-пакета и исхода, `ensureWork` по ключу).
