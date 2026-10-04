# ADR-0072: Содержимое артефактов, типы артефактов, входы и выходы типа задачи

Статус: Accepted (2026-09-26), фича `artifact-handoff`, задача A002
(TASK-000518). Spec/plan/tasks — `specs/artifact-handoff/` суперпроекта
(FR-001…FR-006, FR-009; конституция ст. V, VI); дизайн — TASK-000406, ворота
плана одобрены владельцем 2026-09-26 (отступления III и VIII приняты),
разбиение — TASK-000515. Реализуется по шагам: A003 (TASK-000519) — хранилище
содержимого и потоковые маршруты (п.1–5, 10, 11); A004 (TASK-000520) — реестр
типов артефактов (п.6); A005 (TASK-000521) — `artifactSchema`, входы, отказ
claim (п.7, 8); A006 (TASK-000522) — выход как критерий проверки (п.9 и
амендмент [ADR-0067](0067-verification-stage.md)); A007 (TASK-000523) —
клиент, MCP и раннеры. Номер 0071 занят `me/attention` фичи `human-harness`.
Амендмент 2026-09-28 (company-knowledge, K003): содержимое артефакта
исполнителю скилла по `artifacts.read` на workspace задачи (реализован в K012,
TASK-000772). Амендмент 2026-10-01 (integrations-connections, хвост I021, план
Р5): задача, которую исполняет скилл, сдаёт типизированные выходы из `output`
скилла (TASK-001264).

Контекст: ADR-0013 (артефакт — append-only запись со ссылкой; амендмент
2026-09-26 — этот ADR), ADR-0020 (ревизии через `supersedes`, head —
ревизия без вытесняющих), ADR-0011 (связи задач и readiness), ADR-0048 (версия
типа задачи неизменяема), ADR-0067 (стадия проверки; амендмент 2026-09-26 —
этот ADR), ADR-0068 (каталог событий, версии только добавляют поля), ADR-0051
(секреты в артефактах — правило исполнителя), ADR-0055 (`authorize()`),
TAI-ADR-0044 (виды каталога пакетов; амендмент `ArtifactType` — A001,
TASK-000517).

## Контекст

Результат одной задачи доходит до следующей обходными путями: исполнитель
следующего шага сам ищет его по пути в git, по ссылке в комментарии или в
чате. Для процессов вне разработки git нет вовсе — договор, счёт, акт сверки
не лежат в репозитории. Решение владельца 2026-09-25: передача результатов
между задачами — механизм ядра, а не соглашение исполнителей.

Что есть сейчас: артефакт — append-only запись с `type`, `name`, `uri`,
небольшим JSON `content` (в пределах лимита тела 1 МиБ), `metadata` и
ревизиями через `supersedesArtifactId`. Хранилища байтов у ядра нет; `type` —
свободная строка без реестра; тип задачи не объявляет ни входов, ни выходов;
стадия проверки не умеет требовать результат.

Ограничения: ядро нейтрально к домену (ст. II) — что такое «спецификация»,
«заключение по счёту» или «подписанный акт», приходит данными пакета;
поставщик S3 не должен быть зашит (FR-007); выдача содержимого — только по
праву и со следом в журнале (FR-001, FR-009).

## Решение

### 1. Артефакт — ссылка или содержимое

Артефакт остаётся append-only записью ADR-0013 и сдаётся одним из трёх
способов:

- **ссылка** на систему учёта клиента (`uri`) — ядро содержимым не владеет и
  его не копирует;
- **небольшой JSON** (`content`) — как раньше, в пределах лимита тела;
- **содержимое в хранилище ядра** (`contentRef`) — байты любого media type до
  `CP_ARTIFACT_MAX_BYTES`.

`contentRef` исключает `content` и `uri` — вместе с ними `422
invalid_artifact_content`; пара `uri` + `content` допустима, как раньше.
Запись получает поля `sizeBytes`, `mediaType`, `sha256`, `contentState`
(`none | stored | purged`) и `typeVersion` (версия зарегистрированного типа,
п.6; `null` у незарегистрированного). У артефакта-ссылки и JSON-артефакта
`contentState = none`, остальные новые поля — `null`. Изменения `ArtifactOut`
только добавляют поля: раннеры, MCP, bidops, human-harness совместимы.

### 2. Поток через API ядра: загрузка, затем запись

Сдача файла — два запроса:

1. `PUT /api/v1/artifact-contents` — тело запроса — байты файла,
   `Content-Type` — их media type (обязателен; без него — `400
   invalid_request`). Ядро читает тело потоком во временный файл на диске,
   по пути считает sha256 и размер, затем кладёт объект в хранилище.
   Ответ `201`: `{contentRef, sizeBytes, mediaType, sha256, expiresAt}`.
2. `POST /api/v1/artifacts` с `contentRef` — обычная запись артефакта
   (задача, run, тип, имя, метаданные, `supersedesArtifactId`).

Свойства:

- Тело загрузки не буферизуется в памяти процесса; общий лимит тела 1 МиБ на
  этот маршрут не действует — у него свой `path_limits` =
  `CP_ARTIFACT_MAX_BYTES` (по умолчанию 104857600). Больше лимита — `413
  request_too_large`, временный файл удаляется, объект не создаётся.
- Оборванная загрузка ничего не оставляет: объект пишется в хранилище только
  после полного чтения тела.
- `contentRef` — непрозрачный идентификатор **загрузки** (`cref_<uuid>`), а
  не контрольная сумма. Сослаться на него в `POST /artifacts` может только
  загрузивший principal того же tenant и только до `expiresAt` (24 ч,
  `CP_ARTIFACT_UPLOAD_TTL_SECONDS`); чужой, неизвестный или истёкший —
  `422 content_ref_not_found` (без различия причин). Так дедупликация (п.4) не
  становится каналом чтения: знание sha256 чужого файла не даёт на него
  сослаться.
- Право на загрузку — `artifacts.write` вызывающего. Задачи на этом шаге нет;
  авторизация на задаче артефакта — при `POST /artifacts` (п.5). Байты до
  привязки к артефакту никому не выдаются.
- `Idempotency-Key` у `PUT` не поддерживается: повтор загрузки — новый
  `contentRef` на тот же объект, лишний без ссылок убирает worker (п.10).
  `POST /artifacts` сохраняет свою идемпотентность.

Отвергнуто:

- presigned PUT/GET — хранилище пришлось бы публиковать наружу, а раннеры и
  харнессы ходят только в API ядра; авторизация и аудит раздвоились бы.
  Presigned — возможная оптимизация позже без смены контракта записи;
- multipart в одном `POST /artifacts` — смешивает JSON-контракт с байтами,
  ломает общий лимит тела и идемпотентность по каноническому телу;
- `contentRef = sha256:<hex>` — позволил бы сослаться на чужое содержимое
  tenant'а по известной контрольной сумме.

### 3. Порт `ContentStore`

Ядро работает с хранилищем через порт `ContentStore`
(`infrastructure/content_store/`): `put(key, file, size)`, `open(key) →
stream` (размер и байты по кускам), `exists(key)`, `delete(key)`,
`ensure_bucket()`. Контрольную сумму и размер считает не порт, а `spool` —
по пути тела во временный файл; ключ объекта зависит от суммы, поэтому
объект кладётся уже готовым файлом.
Реализации — S3-совместимая (`boto3` через `asyncio.to_thread`, `s3v4`,
path-style при своём endpoint) и in-memory для тестов. Поставщик меняется
конфигурацией: `CP_S3_ENDPOINT_URL`, `CP_S3_BUCKET` (`artifacts`),
`CP_S3_REGION`, `CP_S3_ACCESS_KEY_ID`, `CP_S3_SECRET_ACCESS_KEY`. Бакет ядро
создаёт при старте; отдельного bootstrap-контейнера нет. Недоступное при
старте хранилище API не останавливает: маршруты содержимого отвечают 503,
бакет создаётся при первом обращении.

Ключ объекта — `tenants/<tenantId>/sha256/<hex>`: tenant'ы разделены
префиксом, внутри tenant адресация по содержимому.

Без `CP_S3_ENDPOINT_URL` хранилище выключено, как и при его недоступности:
загрузка и выдача содержимого — `503 content_store_unavailable`; записи
артефактов, ссылки, метаданные и JSON-артефакты работают. Задачи не
блокируются, кроме тех, чей обязательный выход требует содержимого (п.9).

### 4. Дедупликация и учёт загрузок

Один объект на содержимое внутри tenant: повторная загрузка тех же байтов
объект не пишет заново, но заводит новую загрузку. Ответ `PUT` не зависит
от того, был ли объект: существование чужого файла не раскрывается.

Учёт — таблица `artifact_contents`, **строка на загрузку**: `id`
(`contentRef`), `tenant_id`, `uploaded_by_principal_id`, `sha256`,
`size_bytes`, `media_type`, `storage_key`, `created_at`, `expires_at`,
`referenced_at` (первая ссылка артефакта). Артефакт хранит `sha256`,
`size_bytes`, `media_type` сам — выдача (п.5) не зависит от строки загрузки.
Объект удаляется из хранилища, только когда на его `sha256` в tenant не
ссылается ни один артефакт с `contentState = stored` и нет неистёкшей
загрузки.

Всё, что меняет, нужен ли объект, — загрузка, ссылка артефакта на загрузку,
purge, проход worker'а — идёт под транзакционной advisory-блокировкой на
пару (tenant, sha256). Поэтому объект не удаляется между проверкой «объект
уже есть» у загрузки и появлением её строки, и ссылка на загрузку на границе
`expiresAt` не остаётся без объекта.

Отличие от плана (строка на объект): строка на загрузку нужна, чтобы
`contentRef` был привязан к загрузившему (п.2), а media type — к загрузке, а не к
байтам.

### 5. Выдача содержимого и авторизация на задаче

`GET /api/v1/artifacts/{id}/content` отдаёт байты потоком:

- `Content-Type` — `mediaType` артефакта, `Content-Length`, `ETag:
  "sha256:<hex>"`, `X-Content-Type-Options: nosniff`, `Cache-Control:
  private, no-store`;
- `Content-Disposition: attachment; filename*=UTF-8''<name>` для активного
  содержимого (`text/html`, `application/xhtml+xml`, `image/svg+xml`,
  `text/xml`, `application/xml`, `text/javascript`, `application/javascript`
  и любое `+xml`), для остального — `inline` с тем же `filename*`;
- диапазонов (`Range`) в первом срезе нет;
- `contentState = none` — `404 content_not_found`, `purged` — `410
  content_purged`, хранилище недоступно — `503 content_store_unavailable`.

Каждая выдача пишет `artifact.content_read` (п.11): кто, какой артефакт,
когда, для какой задачи.

Авторизация артефактов — **на задаче артефакта** (`ResourceRef("task")`, как
объявлено в `authz/catalog.yaml`): чтение записи и содержимого —
`artifacts.read` на задаче, запись — `artifacts.write` на задаче. Артефакт
без задачи — на своём workspace, без обоих — право уровня tenant (как
сейчас). Нет права — `403 permission_denied` на выдаче; чужой tenant —
`404`.

Вход (п.7) читается и **для задачи-получателя** (A005): `?forTask=<ref>` у
`GET /artifacts/{id}` и `GET /artifacts/{id}/content` проверяет `tasks.read`
на задаче-получателе и то, что артефакт сейчас — разрешённый вход этой задачи;
иначе — обычная проверка на задаче артефакта. Исполнитель следующего шага
может не иметь права на задачу предыдущего; вход ему выдаётся потому, что тип
его задачи этот вход объявил. В событии выдачи — `forTaskId`.

Хранилище наружу не публикуется, адрес объекта клиентам не отдаётся: байты
идут только через этот маршрут (SC-004).

### 6. Тип артефакта — объект каталога

Тип артефакта — версионируемый неизменяемый объект tenant'а, как тип задачи
(ADR-0048): `key`, `version` (выдаёт сервер), `displayName`, `description?`,
`metadataSchema` (JSON Schema для `metadata` артефакта, ≤ 16 KiB),
`mediaTypes` (список media type или шаблонов `type/*`, `*/*` — любой),
`maxBytes` (≤ `CP_ARTIFACT_MAX_BYTES`), `status` `active | deprecated`.
Пакеты объявляют его видом каталога `ArtifactType` (TAI-ADR-0044, амендмент
A001) и ставят до `TaskType`.

- `POST /api/v1/artifact-types` — следующая версия ключа
  (`artifact_types.manage`); неверная схема, пустой `mediaTypes`, `maxBytes`
  сверх глобального лимита — `422 invalid_artifact_type`.
- `GET /api/v1/artifact-types` (`?key=&status=`),
  `GET /api/v1/artifact-types/{key}` (последняя версия),
  `GET /api/v1/artifact-types/{key}@{version}` — `artifact_types.read`.
- Права `artifact_types.read` (выдаётся всем, кто читает задачи) и
  `artifact_types.manage` (администратор каталога) — в `Permission` и
  `authz/catalog.yaml`.

Проверка при `POST /artifacts`, если `type` зарегистрирован в tenant: по его
**последней** версии — `metadata` по `metadataSchema` (`422
invalid_artifact_metadata`, `details.errors`), `mediaType` загрузки по
`mediaTypes` (`422 media_type_not_allowed`), `sizeBytes` по `maxBytes` (`422
artifact_too_large`); в запись — `typeVersion`. Незарегистрированные типы
(`commit`, `report`, `transcript`, `skill_result`, `verification` и любые
другие) принимаются, как раньше, без проверки (`typeVersion = null`).

Ядро знает о типе только ключ, схему метаданных, media types и размер —
кода под тип нет.

Реализация (A004, TASK-000520):

- `maxBytes` необязателен — по умолчанию `CP_ARTIFACT_MAX_BYTES` на момент
  создания версии; `metadataSchema` по умолчанию `{}` (любой объект);
  `mediaTypes` — от 1 до 50 элементов, приводятся к нижнему регистру,
  повторы убираются. `details.field` у `invalid_artifact_type` — поле
  определения (`metadataSchema`, `mediaTypes[<i>]`, `maxBytes`); причина
  отказа схемы — в `details.reason` и `details.cause`.
- Media type содержимого сравнивается без параметров и без учёта регистра
  (`text/markdown; charset=utf-8` — это `text/markdown`). Media type и размер
  есть только у содержимого из `contentRef` (A003); артефакт-ссылка и
  JSON-артефакт зарегистрированного типа проверяются по `metadata`.
- Последняя версия — наибольшая по номеру, независимо от `status`: маршрута
  `:deprecate` у типов артефактов в первом срезе нет, неизменяемость
  (триггер) допускает только `active → deprecated`.
- Проверка — только у `POST /artifacts`: артефакты, которые пишет само ядро
  (`skill_result`, `verification`, отчёт правила), не проверяются.

### 7. `artifactSchema` типа задачи: входы и выходы

Версия типа задачи получает секцию `artifactSchema` (колонка
`task_types.artifact_schema`, неизменяема вместе с версией — триггер
неизменяемости пересоздаётся с новым полем):

```yaml
artifactSchema:
  inputs:
    - key: spec                  # имя входа в контексте исполнителя
      type: spec-document        # ключ типа артефакта
      from: spawned_by           # depends_on | spawned_by | parent
      required: true
  outputs:
    - key: plan
      type: plan-document
      required: true
      mediaTypes: [text/markdown]  # сужение типа, необязательно
      content: required            # required | optional, по умолчанию required
```

- `key` — `^[a-z0-9][a-z0-9_-]{0,55}$`, уникален внутри `inputs` и внутри
  `outputs`; не больше 32 элементов в каждом списке; `required` по
  умолчанию `false`.
- `type` — ключ типа артефакта, **зарегистрированного** в tenant на момент
  создания версии типа задачи, иначе `422 unknown_artifact_type`
  (`details.field`). Версия типа артефакта не закрепляется: проверка идёт по
  последней (п.6).
- `mediaTypes` выхода — подмножество `mediaTypes` типа артефакта.
- Нарушение грамматики — `422 invalid_artifact_schema` (`details.field`).
  Грамматика — `domain/artifact_schema.py`; JSON Schema той же формы в
  `packages/schema/v1` суперпроекта (A001) должна с ней совпадать.

`from` — связь **задачи-получателя** с источником, только прямая (без
транзитивности): `depends_on` — задачи, от которых она зависит; `spawned_by` —
задача, породившая её; `parent` — её родитель. Направление — как в ADR-0011:
источник — `to` связи, выходящей из задачи-получателя.

Разрешение входа: у каждой задачи-источника — **head-ревизии** (ADR-0020:
на артефакт не ссылается ни один `supersedesArtifactId`) артефактов с
`type = <type>`. Статус источника не важен: у `sdd` работа `feature-tasks`
порождается до завершения дизайна. Несколько источников или несколько
head-ревизий — все подходящие артефакты, по элементу на каждый. Содержимое
для наличия входа не требуется: вход с `contentState = purged` выдаётся с
этой пометкой.

### 8. Входы у исполнителя и отказ claim

- `inputs` — в `GET /runs/{id}/context` и в `operational.focus.inputs` у
  `POST /context` (раннеры читают его); одинаково для агента и человека.
  Элемент: `{key, type, artifactId, name, mediaType, sizeBytes, sha256,
  contentState, uri, sourceTask: {id, publicId, relation}}`. Содержимое
  скачивается отдельным запросом (п.5, с `?forTask=`). Тип без
  `artifactSchema` — `inputs: []`.
- Claim задачи, у которой нет хотя бы одного обязательного входа, — `409
  input_missing`, `details.missing: [{key, type, from}]`. Проверка — сразу после
  `check_task_readiness`, как незакрытая зависимость. То же условие — причина
  `input_missing` (`missing`) в `GET /tasks/{ref}/claimability`, и такая
  задача не попадает в `GET /work/available`: раннер не должен крутиться на
  задаче, которую нельзя взять.

Реализация (A005, TASK-000521):

- Колонка `task_types.artifact_schema` (`{}` — нет ни входов, ни выходов),
  миграция `c3f8a2d6e1b7` пересоздаёт триггер неизменяемости с этим полем.
  Грамматика — `domain/artifact_schema.py`: неизвестные поля элементов и
  секции отвергаются; `key` типа артефакта — по грамматике ключа `POST
  /artifact-types`; `mediaTypes` выхода приводятся к нижнему регистру, повторы
  убираются, элемент должен сужать типы артефакта (`text/markdown` сужает
  `text/*`, но не наоборот), иначе `invalid_artifact_schema` с
  `details.field` = `artifactSchema.outputs[<i>].mediaTypes[<j>]`.
  `unknown_artifact_type` — `details.field` = `artifactSchema.<inputs|outputs>[<i>].type`
  и `details.artifactType`.
- `task_type.created` — схема v2 (плюс `declaresArtifactSchema`, `inputs`,
  `outputs`).
- Разрешение входов — `application/commands/task_inputs.py`, один запрос на
  задачу: связи из задачи-получателя нужного вида, артефакты источников
  нужного типа без вытесняющих ревизий. Порядок — по объявлению входа, внутри
  входа — по времени создания артефакта. Неполученный необязательный вход
  claim не мешает.
- `?forTask=` читает артефакт как вход при двух условиях: `tasks.read` на
  задаче-получателе и артефакт среди её разрешённых входов сейчас. Если хоть
  одно не выполнено — обычная проверка `artifacts.read` на задаче артефакта;
  несуществующая задача-получатель — `404`. `forTaskId` в
  `artifact.content_read` заполняется, только когда выдача разрешена как
  вход; `runId` тогда — running run читающего на задаче-получателе.
- `/work/available` отсеивает задачи без обязательного входа после выборки
  кандидатов, как и по eligibility: страница может быть короче `limit` при
  непустом `nextCursor`.

### 9. Выход — критерий стадии проверки

Критерий `deterministic` получает альтернативную форму `spec`: `{artifact:
{type, mediaTypes?, content?}}` — `skill` и `artifact` взаимоисключают
друг друга. Обязательные выходы типа задачи становятся **неявными
критериями**, которые добавляются к acceptance задачи при каждом завершении.
Грамматику, исполнение и порядок задаёт амендмент 2026-09-26 к
[ADR-0067](0067-verification-stage.md).
Реализация — A006 (TASK-000522), см. «Реализация A006» в амендменте.

### 10. Удаление содержимого

Содержимое хранится бессрочно: артефакты — след работы, закрытие или отмена
задачи его не трогает. `POST /api/v1/artifacts/{id}:purge-content` — только
`admin` tenant, тело `{reason}` (строка 1…2000, обязательна). Запись остаётся
с `contentState = purged` (размер, media type и sha256 сохраняются — след),
объект удаляется из хранилища, если на него не ссылаются другие артефакты с
`contentState = stored` и неистёкшие загрузки tenant'а (п.4). Повтор на уже
удалённом — `200` с той же записью, событие не повторяется; артефакт без
содержимого — `409 content_not_stored`. Отдельного `DELETE` нет: артефакт по
ADR-0013 не удаляется, удаляются только байты. Объект удаляется до фиксации
транзакции: если хранилище недоступно, запрос — `503
content_store_unavailable`, запись остаётся `stored`. В событии `reason`
проходит через ту же очистку, что комментарий решения approval (секреты
вычищаются, текст режется до 1000 символов); в теле запроса — до 2000.

Worker раз в проход удаляет загрузки без ссылок после `expiresAt` и объекты,
на которые больше ничто не ссылается (п.4). Строки загрузок, на которые
сослался артефакт, остаются как след и объект не держат. Недоступное
хранилище оставляет строки до следующего прохода.

### 11. События

- `artifact.created` — схема v2 (ADR-0068: только новые поля): `sizeBytes`,
  `mediaType`, `sha256`, `contentState`, `typeVersion`. Содержимое в журнал
  не пишется, как и раньше.
- `artifact.content_read` (entity `artifact`, actor — читающий): `artifactId`,
  `taskId`, `forTaskId`, `runId` (run вызывающего, если есть), `sha256`,
  `sizeBytes`. Пишется при каждой выдаче байтов (FR-009); Context Adapter
  его в память не переносит.
- `artifact.content_purged` (entity `artifact`, actor — администратор):
  `artifactId`, `taskId`, `sha256`, `sizeBytes`, `reason`, `objectDeleted`
  (удалён ли объект или он ещё нужен другим артефактам).
- `artifact_type.created` (entity `artifact_type`): `key`, `version`,
  `mediaTypes`, `maxBytes`, `declaresMetadataSchema`.
- `task_type.created` дополняется `declaresArtifactSchema`, `inputs` и
  `outputs` — числа элементов.

Все новые типы регистрируются в `domain/event_catalog.py` в том шаге, который
их пишет (ADR-0068: неизвестный тип `record_event` отвергает).

### 12. Нейтральность

Модули хранилища, реестра типов, `artifactSchema` и критерия `artifact`
знают только артефакты, типы, media types, размеры, контрольные суммы и связи
задач. Кода и ветвлений под домен нет (FR-008): какие типы и какие входы и
выходы — данные пакета. Доказательство — фикстура `invoice-payment` проходит
сценарии сдачи, входа и выхода без кода под неё (A009, SC-002) — и пробы
`absent` ниже.

### 13. Клиент, MCP и раннеры

Реализация (A007, TASK-000523):

- SDK: `upload_artifact_content(source, media_type=…)` — `PUT
  /artifact-contents`, путь к файлу читается потоком кусками по 1 МиБ (или
  байты); без `Idempotency-Key` и без повтора при сбое транспорта (п.2:
  повтор — новый `contentRef`). `create_artifact(content_ref=…)`.
  `download_artifact_content(id, destination, for_task=…)` — `GET
  /artifacts/{id}/content` потоком во временный файл рядом с целью; sha256
  сверяется с `ETag`, несовпадение — `content_integrity_failed`, файл
  переименовывается в цель только целым.
- MCP: `cp_create_artifact(type, file=…, media_type=…)` — загрузка файла и
  запись артефакта по `contentRef` в текущие задачу и run; `file` исключает
  `uri` и `content`, имя по умолчанию — имя файла, media type — по
  расширению, иначе `application/octet-stream`.
  `cp_get_artifact_content(artifact_id, path=…, for_task=…)` — read-only:
  скачивает содержимое в файл (по умолчанию — во временный каталог вне
  рабочей копии), `forTask` — текущая задача; текст до 64 KiB — ещё и в
  ответе.
- Демон раннера (`control_plane_agent/inputs.py`): с каталогом
  `CONTROL_PLANE_AGENT_RUNTIME_DIR` (по умолчанию `<WORKTREE_ROOT>/.runtime`)
  перед запуском адаптера скачивает входы run-контекста с `contentState =
  stored` в `<runtime>/<publicId>/inputs/<key>/<name>` — вне рабочей копии,
  чтобы вход не попал в коммит. Каталог пересоздаётся на каждый run и
  удаляется после успеха. Имя файла — одна компонента пути (разделители,
  управляющие символы, ведущие точки заменяются), совпадение имён внутри
  входа различается префиксом id артефакта. Сбой скачивания run не валит:
  вход помечается в prompt с кодом ошибки. Адаптер получает список
  аргументом `inputs`, только если его `execute` его объявляет — прежние
  адаптеры работают как раньше.
- `build_prompt` получает секцию «Входы» после задачи и до памяти: ключ,
  тип, задача-источник и связь, имя, media type, размер, локальный путь или
  причина его отсутствия (`purged`, ссылка, ошибка). Секция огорожена как
  данные — `<task_inputs>` с тем же правилом, что `<recalled_memory>`
  (строки очищаются, тег ограды вырезается); не очищается только путь,
  который строит сам раннер. Без скачанных входов перечисляются входы
  рабочего контекста (`operational.focus.inputs`) без файлов.
- Claude Code: адаптер передаёт агенту `CONTROL_PLANE_TASK` и
  `CONTROL_PLANE_RUN_ID`, MCP-сервер агента принимает их как текущие задачу и
  run — артефакты, checkpoints и действия агента пишутся в run раннера. Claim
  и fencing token остаются у раннера.

## Границы

- Presigned-ссылок, `Range`-выдачи, антивирусной проверки, сканирования на
  секреты, вложений из Telegram нет. Запрет секретов в артефактах — правило
  исполнителя (ADR-0051).
- Поиска по содержимому, редактирования, согласования версий документа нет:
  это системы учёта клиента или memory-service. Индексация содержимого в
  память — не в этой фиче.
- Входы — только по прямым связям; цепочки (`spawned_by` от `spawned_by`)
  нет.
- Бакет документов platform-core ядро не читает и в него не пишет
  (отступление III плана, до M4.4).

## Последствия

- Результат одной задачи становится входом следующей без поиска: `inputs` в
  контексте, отказ claim без обязательного входа, выход — критерий проверки.
- API проксирует байты. Для масштаба первого среза (десятки файлов в день)
  приемлемо; presigned — оптимизация без смены контракта записи.
- Профиль `core` получает MinIO (девятый контейнер, отступление VIII плана);
  установка со своим S3 задаёт `CP_S3_ENDPOINT_URL` и отключает `minio`.
- Прежние клиенты не замечают изменений: новые поля только добавляются,
  незарегистрированные типы работают как раньше, у типа задачи без
  `artifactSchema` нет ни входов, ни неявных критериев.

## Амендмент 2026-09-28 (company-knowledge): содержимое артефакта исполнителю скилла

Основание — фича `company-knowledge` (`specs/company-knowledge/`
суперпроекта). Дизайн — TASK-000748, ворота одобрены владельцем 2026-09-28.
Требования FR-005, FR-007, конституция ст. VI. Амендмент записан в K003
(TASK-000763), реализован в K012 (TASK-000772). Маршрут и его ответы не
меняются, меняется только авторизация.

**Проблема.** Скилл загрузки (`knowledge.import_plan@1`,
`knowledge.document_extract@1`) исполняется у исполнителя скиллов
(ADR-0056 §5) и читает файл, приложенный к задаче. SDK делает это вызовом
`ctx.artifacts.read(artifactId)` — через ядро, учётной записью исполнителя.
Эта identity транспортная: у неё `skills.execute` уровня tenant, но нет
`tasks.read` на задаче. В режиме `policy` `artifacts.read` на задаче
выводится из `tasks.read` (`authz/catalog.yaml`), поэтому исполнитель
получает `403`. `?forTask=` здесь тоже не помогает: он требует `tasks.read`
на задаче-получателе.

**Решение.** `GET /api/v1/artifacts/{id}` и `GET /api/v1/artifacts/{id}/content`
пробуют ещё один путь — после проверки на задаче артефакта и до отказа:

- вызывающий держит `skills.execute` (он — исполнитель скиллов tenant'а), и
- у него есть `artifacts.read` на **workspace задачи артефакта**
  (`ResourceRef("workspace", <workspace задачи>)`; в каталоге у
  `artifacts.read` для ресурса `workspace` вывода нет — это прямая выдача
  права на scope workspace).

Артефакт без задачи и так решается на своём workspace (п.5). Право выдаётся
исполнителю на те workspace, чьи файлы его скиллам нужны. Это не право на
задачи: список задач, их поля и чужие артефакты других workspace исполнитель
по-прежнему не видит.

- Нет права — `403 permission_denied`, как сейчас. Чужой tenant — `404`.
- Принципал с `artifacts.read` на workspace, но без `skills.execute` путь не
  получает. Для людей и агентов авторизация остаётся на задаче, так что
  амендмент не расширяет ничьих прав, кроме исполнителя.
- `artifact.content_read` пишется как при любой выдаче. Кто прочитал — актор
  события, identity исполнителя. `forTaskId` пуст: это не чтение входа.
- В режиме `local` проверка — плоские права ключа исполнителя:
  `skills.execute` и `artifacts.read`.

`authz/catalog.yaml` не меняется: ресурс `workspace` у `artifacts.read` уже
объявлен.

Conformance: contract-тесты доступа K012
(`tests/integration/test_skill_executor_artifact_read.py`) — исполнитель с правом на workspace
читает запись и содержимое; без права — `403`; с правом на другой workspace —
`403`; чужой tenant — `404`; принципал без `skills.execute` с тем же правом —
`403`.

## Амендмент 2026-10-01 (integrations-connections): выходы задачи, которую исполняет скилл

Основание — ревью I021 (TASK-001008), решение Р5 плана `integrations-connections`
суперпроекта: результат скилла передаётся дальше типизированным артефактом, а не
чтением `skill_result`. Реализация — TASK-001264.

**Проблема.** Задачу с `execution: {skill, version}` (ADR-0056 §3) исполняет
один вызов скилла, и ядро сдаёт по нему только `skill_result`. Выходы
`artifactSchema.outputs` её типа (п.7) не создаёт никто: агента-исполнителя у
такой задачи нет. Поэтому вход задачи-получателя от неё (`spawned_by`,
`depends_on`, `parent`) всегда пуст, а обязательный выход — неявный критерий
(п.9) — не проходит никогда. Это пробел ядра, а не пакета: так ведёт себя
любая задача, которую исполняет скилл.

**Решение.**

1. **Когда.** Успешный `:complete` вызова, основание которого — исполнение
   задачи (`authorizationBasis.kind = execution`), в той же транзакции и до
   `skill_result` сдаёт выходы типа задачи (версии, которую несёт задача).
   Вызовы по другим основаниям (агент вызвал скилл в своём run, approval)
   выходов задачи не сдают: их результат — только свидетельство.
2. **Отображение — по совпадению имени.** Значение выхода — поле верхнего
   уровня `output` с именем, равным `key` выхода. Явного отображения
   (`outputs[].from: "$.output.<поле>"`) нет: грамматика `artifactSchema` и её
   JSON Schema в `packages/schema/v1` суперпроекта не меняются, пакеты без
   выходов скилла не затронуты, а пакет сам называет выход так же, как поле
   контракта своего скилла. Если понадобится переименование, `from` добавится
   к элементу выхода необязательным полем — без смены смысла для тех, кто его
   не задал.
3. **Отсутствие.** Поля нет или оно `null` — артефакта нет, это не ошибка
   (`absent`). У `required: true` то же самое — `missing`: вызов не
   проваливается, а неявный критерий выхода (п.9) провалит завершение задачи
   с `artifact_missing`, как у любого исполнителя, не сдавшего выход.
4. **Форма артефакта.** Значение (любой JSON) — содержимое в хранилище ядра:
   UTF-8 JSON с сортированными ключами, `mediaType = application/json`,
   `contentState = stored`, `name = <key>.json`. Так выход проходит неявный
   критерий с `content: required` по умолчанию (JSON-артефакт в `content` его
   не прошёл бы, п.9), а раннер получателя скачивает его как файл входа
   (п.13). Объект кладётся под той же блокировкой (tenant, sha256), что
   загрузка (п.4); строки загрузки нет — на эти байты никто не ссылается по
   `contentRef`. `metadata` — происхождение: `{output, skill, version,
   invocationId, authorityPrincipalId}`.
5. **Проверка по типу.** По последней версии типа артефакта (п.6): значение —
   по `metadataSchema` (это единственная JSON Schema типа; у выхода скилла она
   описывает сам выход; форматы — аннотации, как у `metadata`), media type —
   по `mediaTypes` типа и сужению `mediaTypes` выхода, размер — по `maxBytes`.
   Проверка `metadata` из `POST /artifacts` к такой записи не применяется: её
   метаданные пишет ядро (как у `skill_result`, п.6). Не прошло — выход
   `rejected` с причиной (`invalid_output_value` с `details.errors`,
   `media_type_not_allowed`, `artifact_too_large`); хранилище не настроено или
   недоступно — `rejected` с `content_store_unavailable`. Вызов остаётся
   `succeeded`, `skill_result` сохраняется: скилл уже отработал (в том числе
   его внешнее чтение или запись), и откат результата этого не отменит.
6. **Автор и видимость.** Автор — исполнитель скилла (тот, кто вызвал
   `:complete`): он сдаёт результат работы, как агент — свой артефакт.
   `skill_result` по-прежнему от authority (ADR-0056 §2) — это свидетельство
   вызова. Артефакт привязан к задаче и run вызова, workspace не задаётся:
   видимость — как у задачи (п.5), получатель читает его как вход по
   `?forTask=`.
7. **Ревизии.** Новый выход вытесняет (`supersedesArtifactId`) самую новую
   head-ревизию того же типа у задачи: повторное исполнение после провала
   проверки не удваивает вход получателя.
8. **Отчёт.** Что стало с выходами — список `[{key, type, status: created |
   absent | missing | rejected, artifactId?, reason?}]` в `metadata.outputs`
   артефакта `skill_result` и в поле `outputs` события
   `skill.invocation_succeeded` (схема v2, ADR-0068 — только новое поле; у
   вызова не для исполнения задачи — `[]`). Значения выходов в журнал не
   попадают; `artifact.created` каждого выхода несёт `skillInvocationId`.

Модуль — `application/commands/skill_outputs.py`, проверка значения —
`check_output_value` в `domain/artifact_type.py`. Ядро знает ключи, типы
артефактов и JSON; ветвлений по пакету или типу нет (п.12).

Conformance: `tests/integration/test_skill_task_outputs.py` — нейтральный пакет
(черновик заметки): значение → артефакт объявленного типа → вход задачи,
порождённой ею по `spawned_by`; `null` — артефакта нет, вход пуст, задача
берётся; значение не по схеме, сужение media type, недоступное хранилище —
`rejected` при сохранённом `skill_result`; вытеснение прежней head-ревизии;
обязательный выход проходит проверку, `null` у обязательного — `artifact_missing`;
вызов не для исполнения выходов не сдаёт.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- absent: {path: "src/**/*.py", pattern: 'generate_presigned'}
  repo: control-plane
- absent: {path: src/control_plane/api/v1/artifacts.py, pattern: '@router\.(put|patch|delete)\('}
  repo: control-plane
- route: "PUT /artifact-contents"
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/*.py", pattern: '"/artifacts/\{[a-z_]+\}/content"'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/*.py", pattern: '"/artifacts/\{[a-z_]+\}:purge-content"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/content_store/*.py", pattern: 'class ContentStore\b'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "artifact_contents"'}
  repo: control-plane
- grep: {path: "src/control_plane/**/*.py", pattern: '"content_store_unavailable"'}
  repo: control-plane
- grep: {path: "src/control_plane/**/*.py", pattern: '"content_ref_not_found"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"artifact\.content_read"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"artifact\.content_purged"'}
  repo: control-plane
- route: "POST /artifact-types"
  repo: control-plane
- route: "GET /artifact-types"
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "artifact_types"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/enums.py, pattern: 'ARTIFACT_TYPES_MANAGE = "artifact_types\.manage"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"artifact_type\.created"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'artifact_schema: Mapped'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/claims.py, pattern: 'await check_required_inputs\('}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/discovery.py, pattern: '"input_missing"'}
  repo: control-plane
- absent: {path: src/control_plane/domain/artifact_schema.py, pattern: '(?i)\bcommit|\bbranch|repositor|invoice|payment|contract\b|\bspec-document'}
  repo: control-plane
- absent: {path: "src/control_plane/infrastructure/content_store/*.py", pattern: '(?i)invoice|payment'}
  repo: control-plane
- grep: {path: client/src/control_plane_client/client.py, pattern: 'async def upload_artifact_content\('}
  repo: control-plane
- grep: {path: client/src/control_plane_client/client.py, pattern: 'async def download_artifact_content\('}
  repo: control-plane
- grep: {path: src/control_plane_mcp/server.py, pattern: 'async def cp_get_artifact_content\('}
  repo: control-plane
- grep: {path: src/control_plane_agent/inputs.py, pattern: 'FENCE_TAG = "task_inputs"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/skill_invocations.py, pattern: 'await record_task_outputs\('}
  repo: control-plane
- absent: {path: src/control_plane/application/commands/skill_outputs.py, pattern: '(?i)crm|invoice|payment|\bdeal'}
  repo: control-plane
```
