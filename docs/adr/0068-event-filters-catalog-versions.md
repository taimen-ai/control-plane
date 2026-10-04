# ADR-0068: Журнал событий как подписка — фильтры и права по workspace, каталог и версии данных, payload approvals v2, держатели роли

Статус: Accepted (2026-09-25), фича `notifications`, задача N002 (TASK-000411).
Spec/plan — `specs/notifications/` суперпроекта; дизайн одобрен владельцем в
TASK-000408. Платформенное решение — TAI-ADR-0049 «Событийная модель
платформы» (N001); этот ADR — его реализация в ядре.

Контекст: ADR-0003 (состояние + неизменяемые события), ADR-0005 (outbox —
точка будущего брокера, не меняется), ADR-0023/0024 (курсор журнала и порядок
`(tx_id, sequence)` под стабильным горизонтом), ADR-0038 (архив журнала),
ADR-0018 (approvals и право решать), ADR-0058 (неизвестный query-параметр —
400); конституция 1.0.0, ст. II (нейтральность) и V (контракт прежде
реализации).

## Контекст

Потребители событий ядра (сервис уведомлений, мост процессов, правила)
читают весь журнал tenant'а и сами отбрасывают лишнее. Для этого им нужно
право `events.read` на весь tenant, даже если их дело — approvals одного
workspace. Контракта данных события нет: какие поля в `payload` у
`approval.requested`, знает только код, и поменять их можно незаметно.
Событие `approval.requested` не говорит, что решается и кто просит, — каждый
потребитель дочитывает approval и задачу, с лишним кругом и гонкой с
отменой. Список адресатов решения по роли (кто вправе решить) снаружи
вычислить нельзя: правило видимости роли по дереву workspace живёт в ядре.

## Решение

1. **Подписка = фильтр чтения.** `GET /events` и `WS /events/ws` принимают
   `types` — префиксы типа (`approval.` — все события approvals,
   `task.verified` — этот тип; повторяемый параметр и/или через запятую, до 20;
   формат `^[a-z][a-z0-9_]*(\.[a-z0-9_]*)*$`, иначе `422
   invalid_event_type_filter`, в WS — код закрытия 4400) и `workspaceId` —
   события этого workspace и его потомков (поддерево вычисляется в момент
   чтения). Фильтры сочетаются с `entityType`/`entityId`, `cursor`, `tail`.
   Фильтр не меняет ни порядок, ни смысл курсора: отфильтрованный читатель
   продолжает с `nextCursor` так же, как нефильтрованный. Страница, дошедшая
   до конца журнала, переносит `nextCursor` через отброшенные фильтром события
   до позиции, стабильной до запроса (`past_filtered_out`), — читатель редкого
   типа не пересканирует один и тот же хвост при каждом опросе. Серверных
   подписок с состоянием нет: курсор хранит потребитель.
2. **Право — на workspace.** С `workspaceId` `events.read` спрашивается на
   ресурсе `workspace:<id>` (в режиме `policy` PDP учитывает гранты на
   предках — `authz/catalog.yaml` уже объявлял `events.read` на workspace);
   несуществующий workspace — 404 (WS — 4404). Без `workspaceId` — прежнее
   поведение: право на tenant. Отказ — 403 (WS — 4403). События уровня tenant
   (principal'ы, ключи, bootstrap) под фильтром по workspace не видны.
3. **Workspace события** — колонка `events.workspace_id` (и в
   `event_archive`), её заполняет `record_event` по сущности события:
   собственный workspace сущности (`task`, `approval`, `artifact`, `goal`,
   `rule`, `role`, `project`, сам `workspace`), иначе workspace задачи, к
   которой сущность относится (`run`, `claim`, `skill_invocation`, approval
   или артефакт без своего workspace). Остальные события — уровня tenant
   (`NULL`). Журнал append-only: у событий, записанных до этого решения,
   `workspace_id` пуст, и фильтр по workspace их не видит. В конверте события
   — поле `workspaceId`.
4. **Версия данных — `schemaVersion` в конверте** (колонка
   `events.schema_version`, у старых событий `1`). Реестр типов в коде —
   `domain/event_catalog.py`: тип → сущность → версии → JSON Schema `payload`.
   `record_event` ставит текущую версию типа и **отказывает в типе, которого
   нет в каталоге** (ошибка программирования, а не данных). Правило эволюции:
   версия только **добавляет** поля; поле, меняющее смысл или исчезающее, —
   новый тип. Старые версии остаются в каталоге, пока журнал может хранить
   события под ними. Экспорт в CloudEvents — функция SDK (N003,
   [ADR-0069](0069-event-consumer-sdk.md)), не формат ядра.
5. **Каталог публикуется из кода**: `make event-catalog` пишет
   `docs/events/catalog.md` и `docs/events/catalog.json` (JSON Schema всех
   версий); тест держит их в согласии с кодом. Каталог нейтрален — называет
   только сущности ядра.
6. **Payload approvals v2** (поля только добавлены):
   - `approval.requested` — `workspaceId` (approval'а; с амендмента
     2026-09-25 это и workspace его задачи),
     `taskPublicId`, `taskTitle`, `requestedBy`, `comment`, `gate` (был);
   - `approval.approved` / `approval.rejected` — `decisionBy`, `comment`
     (комментарий решения, `null` без него), `channel` (канал, через который
     пришло решение; `null` для прямого вызова API — заполняется из канала
     входа в N005, ADR-0070);
   - `approval.cancelled` — `cancelledBy`.
   Текст в событии (`comment`) проходит редакцию материала, похожего на
   секрет (`redact_secret_material`: тот же список, что у
   `reject_secret_text`, совпадение заменяется на `[redacted]`), и
   обрезается до 1000 символов. Полный текст остаётся в approval.
   Потребители v1 (правила, мост процессов, bidops) совместимы: схема v1
   принимает payload v2 (тест).
7. **Держатели роли**: `GET /roles/{id}/principals?workspaceId=` —
   principal'ы, у которых роль назначена на уровне tenant или на этом
   workspace / его предке; без `workspaceId` — только назначения уровня
   tenant. Это ровно правило права решать approval с `requiredRoleId`
   (`role_assignment_scope` — одна функция для обоих). Право — `org.read` или
   `principals.read` (уровень tenant, как у `GET /principals/{id}/roles`).
   Элемент — `{id, kind, displayName, status}`, страница — как у прочих
   списков; статус principal'а не фильтруется — решает адресующий.

## Contract-проверки

- После **каждого** интеграционного теста все события журнала проверяются по
  каталогу: известная пара (тип, версия) и `payload`, валидный по её JSON
  Schema (`tests/event_contract.py`). Весь интеграционный набор — contract-тест
  каталога. Тесты, пишущие события SQL-ом мимо ядра, помечены `raw_journal`.
- Статический тест: каждый строковый `event_type=` в `src/control_plane`
  есть в каталоге; динамические типы покрывает отказ `record_event`.
- Схемы — валидный JSON Schema 2020-12; версии только добавляют поля.

## Последствия

- Потребитель approvals одного workspace живёт с `events.read` на этом
  workspace, а не на tenant, и строит сообщение из события без дочитывания.
- Новый тип события без записи в каталог падает в первом же тесте, который
  его пишет; изменение `payload` без новой версии — в contract-проверке.
- Запись события стоит одного `flush` и, если сущность ещё не в identity map,
  одного-двух чтений по первичному ключу.
- Индекса под фильтры нет: они сужают упорядоченный скан так же, как
  `entityType`. Редкий фильтр на большом журнале сканирует до конца страницы;
  перенос курсора (п.1) не даёт повторять это на каждом опросе.
- Миграция `c5e1a7d3f9b2` добавляет две колонки без перезаписи таблицы;
  downgrade их удаляет (workspace новых событий и версии теряются, payload
  остаётся).

## Амендмент 2026-09-25: approval задачи живёт в workspace задачи (TASK-000443)

Найдено на staging (N009, TASK-000418): `request_approval` записывал в
approval только явно переданный `workspaceId`. Approval правила
`request_decision`, `cp_request_approval` раннера и харнесса, исход
`ensureWork.requestApproval` оставались без workspace, и решить их могли
только держатели роли уровня tenant. При этом `approval.requested` называл
workspace задачи, а `GET /roles/{id}/principals?workspaceId=` (п.7) — его
держателей: уведомление уходило тому, кто получал `403 not_eligible`.

Решение:

1. **Approval с задачей живёт в workspace задачи.** Без явного
   `workspaceId` `request_approval` ставит `approval.workspace_id =
   task.workspace_id` (у задачи уровня tenant — `NULL`, как прежде).
2. **Явный `workspaceId` может только расширить круг решающих**: он равен
   workspace задачи или является его предком, иначе `422 invalid_approval`.
   Workspace сбоку или ниже задачи отдал бы решение тем, кто задачу не видит;
   у задачи уровня tenant сузить некуда — любой явный workspace отказ.
   Approval без задачи — как прежде, workspace только явный.
3. `workspaceId` в `approval.requested`, workspace события в конверте (п.3) и
   `workspaceId` проекции approval — одно значение: workspace approval'а.
   Держатели роли по п.7 для него — ровно те, кто может решить.
4. **Миграция данных `d2f8b4a6e1c3`**: pending approvals с задачей и
   `workspace_id IS NULL` получают workspace задачи. Решённые и отменённые —
   история, не меняются; approval без задачи не меняются. Downgrade — no-op:
   заполненные строки неотличимы от созданных с workspace задачи и валидны
   для прежнего кода.

Следствие: роль, выданная на уровне tenant как обход (staging, invoice-payment,
finance-director), после выкатки не нужна — достаточно роли в workspace
задачи.

## Амендмент 2026-10-04: каталог событий по API — `GET /event-types` (TASK-001357)

Каталог (п.4–5) публиковался только файлами `docs/events/`. Консоль для
экрана «Журнал» держала подписи и группы событий своим списком, и он
расходился с ядром при каждом новом типе.

### А1. Маршрут и право

`GET /api/v1/event-types?locale=` — все типы каталога, по имени. Право —
`events.read` на уровне tenant, как у `GET /events` без `workspaceId`; без
права — `403`, без аутентификации — `401`. Страниц нет: каталог конечен
(около двух сотен типов) и меняется только с релизом, поэтому отдаётся
целиком. Другой query-параметр — `400` (ADR-0058).

### А2. Форма ответа — `catalog.json` плюс группа и ключ подписи

`{locale, items: [EventType]}`. Элемент — ровно запись типа из
`docs/events/catalog.json` (`entityType`, `description`, `currentVersion`,
`versions: {"<n>": {changes?, schema}}`) и четыре поля сверху:

- `type` — имя типа;
- `group` — префикс до первой точки (`approval` у `approval.requested`): то,
  что принимает фильтр `types=` (п.1) в виде `approval.`;
- `supportedVersions` — все версии, которые может хранить журнал, по
  возрастанию (п.4: старые версии остаются в каталоге);
- `labelKey` — `event.<type>`, ключ словаря консоли.

Конверт события в ответ не входит — он один на все типы и описан в
`docs/events/catalog.md`. Тест держит ответ в согласии с
`docs/events/catalog.json`.

### А3. Подписи на языке запроса

Язык выбирается цепочкой видов (CP-ADR-0080 §5; общая функция
`resolve_locale`): сам язык (без учёта регистра), его базовый язык, иначе
язык по умолчанию — `en`. Ответ называет выбранный язык в `locale`.

Решение: **строки ядра — только английские**, это описания каталога. Перевод
подписей — дело консоли: она показывает свой текст по `labelKey`, если он
есть в её словаре, иначе `description`. Поэтому сегодня любой `?locale=`
даёт `locale: en`. Почему не русский словарь в ядре сразу: второй язык
пришлось бы вести рядом с каталогом при каждом новом типе, а именно такое
расхождение и закрывает этот маршрут; словарь консоли по `event.<type>`
расходится безопасно — недостающий ключ показывает английское описание.
Если ядро заведёт строки языка, они встанут в ту же цепочку без изменения
формы ответа.

### А4. Признаки типа

Признаков вроде «без значений» или «содержит ПДн» каталог как данных не
знает — они есть только в тексте описаний отдельных типов. В ответе их нет;
когда каталог их заведёт, они придут новым полем элемента (поля только
добавляются).

### А5. Кэширование

Тело для языка строится один раз на процесс; `ETag` —
`"event-types-<sha256 канонического JSON тела>"`, `Cache-Control: private,
max-age=300`. `If-None-Match` с текущим тегом (в том числе слабым `W/`) —
`304` без тела с тем же `ETag`. Право проверяется до сравнения тегов:
закэшированный тег не отвечает `304` вызывающему без `events.read`. Новый
релиз с изменённым каталогом меняет тег — клиент получает `200`.

## Амендмент 2026-10-03: автор, период и одно пространство — журнал для аудита (TASK-001356)

Экран «Журнал» консоли для аудиторов читает `GET /events` от новых к старым.
Ядро сужало выборку только по группе (`types`) и объекту
(`entityType`/`entityId`); автора, период и поддерево консоль отбрасывала по
прочитанному и дочитывала не дальше 10 000 записей — аудит за квартал от этого
лимита зависел. П.1 дополняется тремя фильтрами страницы `GET /events`; WS
`/events/ws` их не принимает (подписка на живой поток по периоду смысла не
имеет, по автору — не просили).

### Б1. `actorId` — автор события

`actorId=<uuid>` — события, у которых `actorId` конверта (колонка
`events.actor_id`, principal, от имени которого записано событие) равен этому
principal. Principal'а с таким id нет или он из другого tenant — пустая
страница, не ошибка: фильтр ничего не сообщает о существовании principal'а.
События без автора (`actorId: null`) под фильтром не видны. Не UUID —
`400 invalid_request` (ADR-0058).

### Б2. `occurredFrom` / `occurredTo` — период

Границы по `occurredAt` события: `occurredFrom` **включительно**
(`occurredAt >= occurredFrom`), `occurredTo` **исключительно**
(`occurredAt < occurredTo`) — соседние периоды (квартал за кварталом)
стыкуются без повторов и пропусков. Значение — ISO 8601 date-time **с зоной**
(`2026-07-01T00:00:00Z`, `…+03:00`); без зоны — `422 invalid_event_period`
(период аудита не должен зависеть от часового пояса сервера). `occurredFrom`
позже `occurredTo` — тоже `422 invalid_event_period`; равные границы —
пустой период, пустая страница. Неразбираемое значение — `400
invalid_request`. Одна граница — полупрямая.

Порядок страницы и курсор не меняются: события по-прежнему идут в порядке
`(tx_id, sequence)`, а период — ещё одно сужение. `occurredAt` близок к этому
порядку, но не обязан с ним совпадать (время ставит пишущий, порядок —
транзакция), поэтому период — фильтр, а не граница курсора: страница периода
читается теми же `cursor`/`before`/`tail`/`order`, что и любая другая.

### Б3. `includeDescendants` вместе с `workspaceId`

`includeDescendants=true` — поддерево, `false` — только сам workspace, как у
`GET /tasks`. **Без параметра — поддерево**: так `workspaceId` работал с
п.1, и подписки потребителей (сервис уведомлений, SDK) на это опираются.
Здесь умолчание расходится с `GET /tasks` (там без параметра — только сам
workspace) сознательно: смена умолчания молча сузила бы существующие подписки.
Без `workspaceId` параметр ни на что не влияет, как у `GET /tasks`. Право —
по п.2, на запрошенном workspace, в обоих случаях.

### Б4. Сочетание, права и индексы

- Все фильтры сочетаются между собой и с прежними как пересечение; право
  прежнее — `events.read` (п.2), фильтры только сужают выборку. Сужение по
  видимости пространств (фича people-access, CP-ADR-0082) — ещё одно условие
  того же пересечения: новый фильтр не может вернуть событие, которое
  видимость скрыла. Вызывающему в `members` (CP-ADR-0082 §4, В7) `actorId` и
  период отбирают только среди событий видимых пространств и tenant;
  `workspaceId` невидимого пространства — 404 как у несуществующего при любом
  `includeDescendants`, у видимого `includeDescendants=false` — только оно
  само (тесты `test_event_audit_filters_visibility.py`).
- Перенос `nextCursor` через отброшенное (п.1, `past_filtered_out`) действует
  и для новых фильтров.
- Индексы (миграция `8601ebd0d794`, на `events` и `event_archive`; со
  строкой people-access её сводит пустая merge-ревизия `b7e3d9a1c4f2`):
  `(tenant_id, actor_id, tx_id, sequence)` и
  `(tenant_id, workspace_id, tx_id, sequence)` — равенство на автора или
  workspace, за ним порядок журнала: страница — упорядоченный проход по
  индексу в обе стороны; `(tenant_id, occurred_at)` — период как диапазон
  индекса: квартал читается в пределах квартала, а не сканом журнала (план
  проверяет тест `test_period_and_actor_pages_use_their_indexes`). Поддерево
  из нескольких workspace — `IN` по тому же индексу; пространство без потомков
  — равенство. Пункт «Индекса под фильтры нет» раздела «Последствия» для этих
  фильтров больше не действует; `types` по-прежнему сужает скан.
- Цена — три индекса на каждой из двух таблиц журнала: запись события
  обновляет на три индекса больше; сборка индексов в миграции не
  `CONCURRENTLY` и на большом журнале требует окна обслуживания.

## Амендмент 2026-10-04: выгрузка журнала за период — `GET /events:export` (TASK-001358)

Экран «Журнал» консоли выгружал в файл только прочитанные страницы: аудитору
за квартал приходилось листать журнал до конца в браузере. Нужна серверная
выгрузка периода с фильтрами амендмента Б, которую не ограничивает память ни
сервера, ни консоли.

### В1. Маршрут, формат и фильтры

`GET /api/v1/events:export?format=jsonl|csv` — все события периода, которые
отдал бы `GET /events` с теми же фильтрами (п.1, Б1–Б4): `types`,
`entityType`/`entityId`, `actorId`, `occurredFrom`/`occurredTo`, `workspaceId`
с `includeDescendants`. Смысл фильтров, их проверка и коды отказа те же
(`invalid_event_type_filter`, `invalid_event_period`, `400 invalid_request` на
неразбираемое значение и неизвестный параметр). Курсоров, `limit`, `tail` и
`order` нет: выгрузка — весь период от начала хранимого журнала в порядке
`(tx_id, sequence)`. `format` обязателен.

- **JSONL** (`application/x-ndjson; charset=utf-8`) — одна строка на событие,
  тело ровно как элемент `items` у `GET /events` (с `cursor`).
- **CSV** (`text/csv; charset=utf-8`, RFC 4180, строки через CRLF) — строка
  заголовка и плоские колонки `id, occurredAt, type, schemaVersion, actorId,
  entityType, entityId, workspaceId, payload`; `null` — пустое поле, `payload` —
  JSON-строка. Остальные поля конверта (`sequence`, `correlationId`,
  `requestId`…) есть только в JSONL. Защита от формул (OWASP CSV injection):
  ячейка, которая начинается с `=`, `+`, `-`, `@`, табуляции или `\r`, получает
  префикс `'` — табличный редактор не читает её как формулу. Правило действует
  на все колонки, включая `payload` (JSON-объект начинается с `{`, но, например,
  числовой `payload` `-1` выйдет как `'-1`), и не опирается на то, что
  скалярные колонки формирует сервер.

Ответ — `Content-Disposition: attachment; filename="events-<from>-<to>.<format>"`,
`Cache-Control: private, no-store` и `X-Event-Count` — число событий в теле:
клиент сверяет с ним прочитанное (обрыв потока посередине иначе не отличить
от конца).

### В2. Предел периода и объёма — отказ до первого байта

Период обязателен: без `occurredFrom` или `occurredTo` — `422
export_period_required` (`details.missing`, `details.maxPeriodDays`). Длина
периода — не больше `events_export_max_period_days` (по умолчанию **92 дня**:
квартал целиком, `2026-07-01Z`…`2026-10-01Z`); длиннее — `422
export_period_too_long`. Совпадающие границы — пустой период: `200`, пустое
тело (у CSV — одна строка заголовка), `X-Event-Count: 0`.

Объём — не больше `events_export_max_events` событий (по умолчанию
**100 000**); больше — `422 export_too_large` (`details.maxEvents`): сузьте
период или фильтры. Число считается одним запросом по `events` и
`event_archive` (один снимок — перенос в архив не учтёт событие дважды) и
останавливается на пределе + 1, так что подсчёт стоит не больше предела.

Почему отказ, а не усечение: статус потокового ответа отправляется до тела,
и обрезанная выгрузка выглядела бы полной. Обе проверки идут до первого байта,
отказ — обычная ошибка JSON.

### В3. Снимок и поток

Верхняя граница выгрузки — позиция журнала на момент подготовки
(`current_position`, под стабильным горизонтом): всё ниже неё окончательно, так
что подсчёт и тело совпадают, а события, записанные во время скачивания, в
выгрузку не попадают (их забирает следующая выгрузка или `GET /events`).

Тело читается страницами по 500 событий тем же читателем, что и
`GET /events` (архив, затем горячий журнал; повтор страницы, если архив сдвинул
границу), **каждая страница — в своей сессии**: в памяти одна страница, и
транзакция не висит открытой, пока медленный клиент качает. Если retention
удалит ещё не отданную часть, чтение страницы падает с
`cursor_below_journal_floor`, поток обрывается, и ответ короче `X-Event-Count`.

Видимость (В4) проверяется заново на каждой странице, а не фиксируется в
снимке. Если между подсчётом и потоком задача (или другая сущность) сменила
пространство, тело может разойтись с `X-Event-Count`: стало меньше видимых
событий — тело короче; стало больше — поток обрезается по `count`, и лишние
события в выгрузку не попадают. Расхождение «короче» клиент не отличит от
обрыва — выгрузку повторяют.

Цена: страница выгрузки с периодом — тот же запрос, что страница `GET /events`
(Б4): индекс `(tenant_id, occurred_at)` даёт строки периода, порядок журнала —
сортировка внутри периода на каждую страницу. При пределе объёма это сотни
страниц по ≤ 100 000 строк; если станет узким местом — курсор сервера в одной
транзакции.

### В4. Право и видимость

Право — **`events.export`**, отдельное действие каталога политик
(`authz/catalog.yaml`, ресурс `workspace`, как у `events.read`), и вдобавок
`events.read` по п.2: `events.read` говорит, что вызывающий может видеть,
`events.export` — что он может унести это массово. Ни одно не включает
другое; без любого — `403`. С `workspaceId` оба права спрашиваются на нём,
без — на уровне tenant. `admin` подразумевает оба.

Видимость — как у `GET /events` (Б4, CP-ADR-0082 В7): вызывающему в `members`
выгружается только видимое, `workspaceId` невидимого пространства — `404` как
у несуществующего. Чужой tenant ничего не получает.

### В5. Аудит выгрузки

Каждая принятая выгрузка пишет событие `event_journal.exported` (сущность
`event_journal`, `entityId` — id tenant, уровень tenant) в той же транзакции,
что и проверки, — **до** первого байта тела. `payload` — только фильтры и
итог: `format`, `types`, `entityType`, `entityId`, `actorId` (фильтр, автор
выгрузки — `actorId` конверта), `occurredFrom`, `occurredTo`, `workspaceId`,
`includeDescendants`, `events` (число), `throughCursor` (граница снимка);
событий журнала в нём нет. Отказ (`403`, `422`) не журналируется. Фильтры
лежат на верхнем уровне `payload`, поэтому `workspaceId`, `entityType` и
`entityId` выгрузки проходят ту же проверку видимости событий без пространства
(CP-ADR-0082 В7): выгрузку невидимого пространства читатель в `members` не
видит. Само событие аудита записано после снимка и в свою выгрузку не входит.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/application/events.py, pattern: 'schema_version = current_version\(event_type\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/events.py, pattern: 'workspace_id = await event_workspace\(session, entity_type, entity_id\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'Permission\.EVENTS_READ, resource=ResourceRef\("workspace"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'startswith\(p, autoescape=True\)'}
  repo: control-plane
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"workspaceId, taskPublicId, taskTitle, requestedBy, comment"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: 'scope_filter = await role_assignment_scope\('}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: 'workspace_id = await _task_approval_workspace\(session, ctx, task, workspace_id\)'}
  repo: control-plane
- grep: {path: tests/integration/conftest.py, pattern: 'journal_violations\(sync_engine\)'}
  repo: control-plane
- grep: {path: tests/unit/test_event_catalog.py, pattern: 'def test_every_literal_type_in_the_core_is_in_the_catalog'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/event_types.py, pattern: 'await authorize\(ctx, Permission\.EVENTS_READ\)'}
  repo: control-plane
- grep: {path: tests/unit/test_event_types_api.py, pattern: 'def test_the_answer_is_the_published_catalog'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/events.py, pattern: 'stmt = stmt\.where\(model\.occurred_at < self\.occurred_to\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'Index\("ix_events_tenant_occurred_at", "tenant_id", "occurred_at"\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/event_export.py, pattern: 'await authorize\(ctx, Permission\.EVENTS_EXPORT, resource=resource\)'}
  repo: control-plane
- grep: {path: tests/unit/test_event_export.py, pattern: 'def test_a_cell_read_as_a_formula_gets_an_apostrophe'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/event_export.py, pattern: '"export_too_large"'}
  repo: control-plane
```
