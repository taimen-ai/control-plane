# ADR-0060: Снимки знаний и доменные пакеты через Control Plane

Статус: Accepted (2026-09-23; правки по ревью TASK-000306 — 2026-09-23);
амендмент 2026-09-28 (company-knowledge, K003): предпросмотр снимка,
`expectedState`, документы базы знаний, пакеты арендатора; К1 и К2
реализованы в K008 (TASK-000768), документы (п. К3) — в K009 (TASK-000769),
пакеты арендатора (п. К4) — в K010 (TASK-000770); амендмент 2026-09-28
(company-knowledge, K031, TASK-000796): перечень сущностей
`POST /knowledge/entities:query`; амендмент 2026-09-30 (package-sdk,
TASK-001043): чтение набора пакетов workspace и пакета онтологии;
амендмент 2026-10-03 (TAI-ADR-0066 п.5, TASK-001300): связи записей
(`include.relations`) и контракт полей источника в перечне сущностей

Контекст: амендмент 2026-09-23 к TAI-ADR-0042 суперпроекта («память — только
через Control Plane») и TAI-ADR-0031 п.6: никто, кроме ядра, не ходит в
memory-service; разрешения применяет ядро. Продолжает
[ADR-0054](0054-governed-graph-memory.md) (namespace и provenance определяет
сервер) и [ADR-0055](0055-policy-authorize-and-shadow-mode.md) (`authorize()` с
ресурсом).

## Контекст

Коннекторы знаний (первый — `integrations/selfdev` суперпроекта, формат
`integrations/selfdev/SNAPSHOT.md`) периодически снимают состояние источника
(репозиторий, трекер) и сверяют его с памятью: что появилось, что изменилось,
что исчезло. До амендмента коннектор писал в memory-service напрямую со своим
ключом и сам выбирал namespace — то есть сам решал, куда и с какой видимостью
попадает знание. Это обходит модель разрешений платформы.

Доменные пакеты (набор типов сущностей и связей, которые понимает память) и
их включение для namespace — тоже операции над памятью, которые раньше
выполнялись в обход ядра.

## Решение

### `POST /api/v1/knowledge/snapshots`

Тело — документ снимка как есть плюс `workspaceId`:

| Поле | Смысл |
|---|---|
| `workspaceId` | Workspace, от имени которого пишется знание. Обязателен. |
| `pack` | Доменный пакет снимка (1..128). Необязателен, как у памяти (`pack: str = ""`): без него память проверяет виды по каталогу namespace. |
| `source` | Источник (1..200). |
| `scope` | Область снимка внутри источника — строка (до 200), часть идентичности источника у памяти; ядром не интерпретируется. Необязателен. Это не scope namespace и не scope видимости. |
| `snapshotId` | Идентификатор снимка (1..200). |
| `observedAt` | Время снимка, ISO 8601 с часовым поясом. |
| `entities[]`, `relations[]` | Объекты; содержимое проверяет память по пакету. Вместе — не больше 20000 элементов. |

Контракт строгий (как весь `/api/v1`): лишнее поле — `400 invalid_request`. В
частности `namespace` и `scopes` клиент передать не может. Границы полей
совпадают с проверками памяти (`domain/reconcile.py::parse_snapshot`:
`MAX_SOURCE_LEN`, `MAX_SCOPE_LEN`, `MAX_SNAPSHOT_ID_LEN`, `reconcile_max_items`
= 20000), поэтому заведомо непринимаемый снимок отвергается ядром (`400`) без
вызова памяти.

1. **Авторизация** — `observations.write` на ресурс
   `ResourceRef("workspace", workspaceId)` (в режиме `local` — плоское право
   ключа). Затем workspace разрешается внутри tenant'а: чужой или неизвестный —
   `404`, архивный — `422 workspace_archived`.
2. **Куда.** Ядро вычисляет namespace `tenant:<tenant>:ws:<root>`, где `root` —
   корень дерева workspace (`workspace_ancestor_ids`, последний элемент). Имя
   совпадает с объектами `memory_namespace` `ws-<id>`, которые policy-режим уже
   отдаёт памяти как видимые (`memory_visibility`). Всё дерево делит одно
   пространство: знания подпространств связываются между собой.
3. **Кому видно.** Scope видимости — `workspace:<workspaceId>`: подпространство
   пишет в namespace корня, но со своим scope, и чтение по-прежнему фильтруется
   по workspace-scope'ам читателя.
4. **Передача.** `POST /api/memory/reconcile` identity ядра — тем же клиентом и
   credential, что Context Adapter (`CP_CONTEXT_AUTH`: сервисный аккаунт IAM или
   `CP_CONTEXT_API_KEY`), с `X-Run-Id` для трассировки. Тело — модель памяти
   `ReconcileIn`: **плоский** документ снимка плюс `namespace` и `scopes` на
   верхнем уровне:

   ```json
   {"pack": "...", "source": "...", "scope": "repo:...", "snapshotId": "...",
    "observedAt": "...", "entities": [...], "relations": [...],
    "namespace": "tenant:<t>:ws:<root>", "scopes": ["workspace:<id>"]}
   ```

   Память читает снимок как `model_dump(exclude={"namespace", "scopes"})`, так
   что `scope` верхнего уровня — строка снимка, а не scope записи. Снимок
   передаётся без `workspaceId`; `null`-поля опускаются. Таймаут —
   `CP_CONTEXT_RECONCILE_TIMEOUT_SECONDS` (60 с).
5. **Ответ.** Тело ответа памяти (счётчики, `duplicate`) возвращается клиенту
   как есть, `200`. Отказы памяти отображаются по смыслу (см. «Ошибки памяти»
   ниже): `400` (снимок невалиден) → `422 snapshot_invalid`, `409` (снимок
   старее уже применённого) → `409 snapshot_stale`. Провайдер не настроен
   (`CP_CONTEXT_PROVIDER=none`) → `503 memory_disabled`. В отличие от
   `/context` деградированного ответа нет: клиент просил запись.
6. **Журнал.** После успешной сверки — событие
   `knowledge.snapshot_reconciled` (entity — workspace): `snapshotId`, `pack`,
   `source`, `observedAt`, `workspaceId`, `rootWorkspaceId`, `namespace`,
   `entityCount`, `relationCount`, `duplicate`, `counters` (целые числа ответа
   памяти, один уровень вложенности, до 32 ключей). Содержимое сущностей и
   связей в журнал не попадает. Событие не входит в whitelist context mapping и
   в память повторно не доставляется. Отказ памяти события не порождает.
7. **Размер.** Для пути снимков лимит тела —
   `CP_KNOWLEDGE_SNAPSHOT_MAX_BODY_BYTES` (8 МиБ); остальной API сохраняет
   `CP_MAX_BODY_BYTES`. Превышение — `413 request_too_large`.

   Тело читается **до аутентификации**: лимит применяет ASGI-middleware, а
   credential проверяется в зависимости эндпоинта, когда FastAPI уже прочитал
   и разобрал JSON. Значит, анонимный клиент может заставить ядро принять и
   распарсить до 8 МиБ на запрос. Риск принят: он того же рода, что и для
   любого эндпоинта с `CP_MAX_BODY_BYTES` (1 МиБ), отличается только
   множителем; объявленный `Content-Length` сверх лимита отвергается до чтения
   тела, потоковое тело обрывается на переполнении; ограничение частоты и
   размера запросов на периметре (ingress) — штатная защита платформы. Если
   этого окажется мало, аутентификацию для этого пути можно вынести в
   middleware перед чтением тела — отдельным решением.

Ни одна транзакция БД не открыта во время вызова памяти: авторизация и
разрешение workspace — в первой, событие — во второй. Если запись события
упала после успешной сверки, повтор запроса безопасен: память ответит
`duplicate`, и событие будет записано. `Idempotency-Key` этот эндпоинт не
обрабатывает — повтор снимка идемпотентен на стороне памяти по `snapshotId`.

### Доменные пакеты

Реестр пакетов в памяти **общий для всех tenant'ов**, а ссылка на пакет без
версии разрешается в последнюю зарегистрированную версию. Если бы регистрацию
мог делать администратор tenant'а, админ tenant A выпустил бы `pack@99` и
сломал strict-проверку снимков у tenant B, включившего `pack` без версии.
Решение владельца (ревью TASK-000306):

- `POST /api/v1/knowledge/packs` — манифест пакета как есть →
  `POST /api/memory/packages` (модель памяти `PackIn`: `name`, `version` и
  остальной манифест). Только **администраторы платформы**: principal'ы из
  настройки ядра `CP_KNOWLEDGE_PACK_ADMINS` (JSON-список id principal'ов
  Control Plane или IAM; вызывающий проходит, если в списке его CP principal
  id или IAM `sub`). Пустой список (по умолчанию) — эндпоинт закрыт для всех:
  `403 permission_denied` с `details.required = "knowledge_pack_admin"`. Право
  `admin` tenant'а для этого не нужно и недостаточно. Манифест без `name`
  (непустая строка) или `version` (строка или число, не bool) — ядро отвечает
  `422 pack_invalid` с `details.field` само, не спрашивая память: иначе модель
  `PackIn` ответила бы своим `422`, а клиент увидел бы `502`. Грамматику имени
  и версии и остальную структуру проверяет память: её `400` →
  `422 pack_invalid`, `409` (версия уже зарегистрирована с другим содержимым,
  версии иммутабельны) → `409 pack_version_conflict`. Ответ памяти
  (`{status: created|unchanged, pack}`) возвращается как есть, `200`.
- `PUT /api/v1/workspaces/{id}/knowledge-packs` — `{packs: [...], strict}` →
  `PUT /api/memory/namespaces/{ns}/kinds` с телом
  `{"packages": [...], "strict": bool}` (модель памяти `NamespaceKindsIn`).
  Право — `workspaces.manage` на `workspace:<id>`. Пакеты и strict — свойство
  namespace, то есть всего дерева, поэтому `id` должен быть корнем: для
  подпространства — `422 workspace_not_root` с `details.rootWorkspaceId`
  (иначе администратор поддерева менял бы память корня). Принимаются **только
  закреплённые ссылки `name@version`** (грамматика имени и версии — как у
  памяти, `core.kinds`: `^[a-z0-9][a-z0-9._-]{0,63}@[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$`);
  ссылка без версии — `422 pack_version_required` с `details.packs`, так что
  новая версия пакета, выпущенная позже, на namespace не влияет, пока его
  администратор сам не переключит ссылку. Повторы в `packs` схлопываются.
  Память отвечает `404` на неизвестный пакет → `422 pack_not_found`, `400` →
  `422 pack_invalid`.

**Аудит.** Обе операции пишут событие в журнал ядра после успешного ответа
памяти (отказ памяти события не порождает):

- `knowledge.pack_registered` — entity `knowledge_pack` с устойчивым id
  `uuid5(NAMESPACE_URL, "knowledge-pack:<name>@<version>")` (пакет живёт в
  памяти, строки в ядре у него нет), tenant — tenant администратора; payload:
  `name`, `version` (нормализованные памятью), `status`
  (`created`/`unchanged`). Кто — `actorId` и `iamActorId` события. Манифест в
  журнал не пишется.
- `knowledge.packs_configured` — entity `workspace` (корень); payload:
  `workspaceId`, `namespace`, `packs` (закреплённые ссылки), `strict`.

### Ошибки памяти

Ядро отображает ответ памяти по смыслу и не повторяет запросы с `4xx`:

| Память | Где | Ответ ядра |
|---|---|---|
| `400` (`SnapshotError`, `PackError`) | reconcile | `422 snapshot_invalid` |
| `400` (`PackError`) | packages, kinds | `422 pack_invalid` |
| `404` (`PackNotFoundError`) | kinds | `422 pack_not_found` |
| `409` (`StaleSnapshotError`) | reconcile | `409 snapshot_stale` |
| `409` (`PackConflictError`) | packages | `409 pack_version_conflict` |
| `400` (`ValueError` ингеста) | documents (амендмент, п. К3) | `422 document_invalid` |
| `401`/`403`, иной `4xx` | все | `502 memory_unavailable`, `details.retryable = false` — ошибка конфигурации ядра (credential, scope сервис-аккаунта) или wire-контракта, а не клиента |
| `5xx`, транспорт, таймаут, IAM недоступен | все | `502 memory_unavailable`, `details.retryable = true` |

`details` содержит только `memoryStatus` (и `retryable` у `502`); текст ответа
памяти клиенту не отдаётся — он пишется в лог ядра (`memory call failed`).

### Клиенты

`control-plane-client`: `submit_knowledge_snapshot(workspace_id=, snapshot=)`,
`submit_knowledge_document(workspace_id=, document=)` (амендмент, п. К3),
`register_knowledge_pack(pack)`,
`set_workspace_knowledge_packs(workspace_id, packs=, strict=)`. MCP-инструментов
нет: это машинный путь коннектора, а не инструмент агента.

## Последствия

- Коннектор `integrations/selfdev` переходит на этот эндпоинт
  (`ControlPlaneSnapshotSink`) отдельным изменением суперпроекта; его ключ
  memory-service больше не нужен.
- Wire-формат трёх вызовов памяти (`/api/memory/reconcile`,
  `/api/memory/packages`, `/api/memory/namespaces/{ns}/kinds`) зафиксирован
  здесь и в `context_provider/http.py` и закреплён двумя способами:
  `tests/unit/test_knowledge_memory_contract.py` валидирует тела, которые
  строит ядро, по снимку запросных моделей памяти
  (`tests/fixtures/memory_knowledge_contract.json`: `ReconcileIn`, `PackIn`,
  `NamespaceKindsIn` из `app.openapi()` memory-service и поля снимка из
  `parse_snapshot`, с ревизией источника); `tests/contract` проверяет
  reconcile/kinds против живого memory-service (при `CP_TEST_MEMORY_URL`).
  Снимок фикстуры нужно обновлять вместе с изменением этих эндпоинтов памяти.
- `memory:service` — identity ядра для **всех** служебных маршрутов памяти
  (reconcile, `packages`, `namespaces/{ns}/kinds`; амендмент MEM-ADR-020).
  Отдельного токена для них нет — один токен на audience (ADR-0013): scope
  входит в `CP_CONTEXT_IAM_SCOPES` по умолчанию
  (`memory:read memory:write memory:tenants memory:service`, в режиме
  `policy` плюс `memory:on-behalf`). Потолок service account ядра содержит
  его (суперпроект 777e7ad, выложено на staging). Если потолок scope не
  выдал, память отвечает `403`, ядро — `502` с `memoryStatus: 403`.
- Кэшированный токен ядра сбрасывается только на `401` от памяти (токен
  отозван или ротирован — повтор обменяет новый). `403` — валидный токен без
  права: новый токен не поможет, а сброс общего токена на каждом отказе
  лишь нагружал бы IAM.
- Риск общего реестра (tenant-admin публикует версию, ломающую strict у
  другого tenant'а) закрыт: регистрация — только администраторы платформы,
  включение — только закреплённые версии.
- Путь `/api/v1/knowledge/*` при включённом entitlement — отдельная фича
  `knowledge` (`feature_for_path`).

## Амендмент 2026-09-28 (company-knowledge): предпросмотр снимка, применение по состоянию, документы, пакеты арендатора

Основание — фича `company-knowledge`: spec, plan и документ задач лежат в
`specs/company-knowledge/` суперпроекта. Дизайн — TASK-000748, ворота одобрены
владельцем 2026-09-28. Требования FR-005, FR-006, FR-007, FR-027, конституция
ст. V, VI, IX. Опора: TAI-ADR-0056 (K001) и амендмент MEM-ADR-020 (K002).
Амендмент записан в K003 (TASK-000763). Реализуют его K008 (п. К1, К2), K009
(п. К3) и K010 (п. К4).

До реализации маршруты и поля опубликованы в OpenAPI. После проверки права
они отвечают `501 not_implemented` с `details: {adr: "CP-ADR-0060",
implementedBy: "company-knowledge K00x"}`. Память при этом не вызывается, и
события не пишутся. Прежние клиенты изменений не замечают: новые поля
необязательны, прежние тела принимаются как раньше.

**Проблема.** Загрузка таблицы должна показать план изменений до записи и
применить ровно показанный план (FR-005, FR-006). Сейчас снимок применяется
сразу, а второй загрузчик молча перезаписывает первого. Документы базы знаний
вне дела ядро не принимает (FR-007). Собственные виды компании регистрирует
только администратор платформы (FR-027).

### К1. `POST /api/v1/knowledge/snapshots:preview`

Предпросмотр сверки — план изменений снимка без записи.

- **Тело** — то же, что у `POST /knowledge/snapshots`, без `expectedState`
  (`KnowledgeSnapshotPreviewRequest`). Границы полей, `400` на лишнее поле и
  лимит тела `CP_KNOWLEDGE_SNAPSHOT_MAX_BODY_BYTES` (8 МиБ) — как у снимка.
- **Право** — `observations.write` на `workspace:<workspaceId>`, как у снимка.
  Право на запись, а не на чтение: предпросмотр раскрывает, что лежит в памяти
  по источнику, и нужен только тому, кто будет применять. Workspace
  разрешается как у снимка: чужой — `404`, архивный — `422
  workspace_archived`.
- **Передача** — `POST /api/memory/reconcile` тем же клиентом и identity,
  что снимок. Тело то же (`ReconcileIn`: плоский снимок, `namespace`,
  `scopes`) плюс `dryRun: true` (амендмент MEM-ADR-020, K002). Имена полей
  берутся из контракта памяти K002. K008 закрепляет их в фикстуре
  `tests/fixtures/memory_knowledge_contract.json`.
- **Ответ** — ответ памяти как есть, `200`: изменения (`changes`), счётчики,
  конфликты естественных ключей с элементами других источников и `stateToken`.
  `stateToken` — отпечаток последнего принятого снимка пары `(source, scope)`.
  В OpenAPI ответ описан моделью `KnowledgeSnapshotPreviewOut`: ядро называет
  в нём только `stateToken`, остальное — поля памяти.
- **Ничего не пишет.** Ни строки в БД ядра, ни события:
  `knowledge.snapshot_reconciled` и `knowledge.changed` пишет только
  применение.
- **Ошибки** — по таблице «Ошибки памяти»: `400` памяти — `422
  snapshot_invalid`, недоступность — `502`, провайдер выключен — `503`.

Отдельный маршрут, а не флаг в теле снимка. Причина: запрос предпросмотра не
должен применить снимок из-за забытого поля, а периметр и аудит различают
чтение плана и запись по пути.

### К2. `expectedState` у `POST /api/v1/knowledge/snapshots`

- Необязательная строка (1..128) — `stateToken` из предпросмотра. Для ядра
  значение непрозрачно. Граница — как у памяти (`ReconcileIn.expectedState`,
  `maxLength` 128; уточнено в K008, в K003 было 1..256): длиннее память
  отвергла бы своим `422`, и клиент получил бы `502` вместо `400`.
- Передаётся в память полем `expectedState` рядом со снимком. В документ
  снимка оно не входит (`snapshot_document()` его исключает).
- Память сверяет состояние пары `(source, scope)`. Если оно изменилось после
  предпросмотра, память отвечает `409`. Ядро отдаёт `409 snapshot_stale` —
  тот же код, что для снимка старее применённого: в обоих случаях план
  построен не на текущем состоянии. Клиент строит план заново.
- Без `expectedState` снимок применяется как раньше: коннектору, который
  снимает весь источник, план не нужен.
- Событие `knowledge.snapshot_reconciled` не меняется.

### Реализация К1 и К2 (K008)

Маршрут и поле работают, `501` у них снят.

- **Провайдер.** `KnowledgeProvider.reconcile_snapshot` принимает `dry_run` и
  `expected_state`. `HttpContextProvider` кладёт их в тело `ReconcileIn`
  полями `dryRun: true` и `expectedState` рядом со снимком, и только когда
  они заданы: тело обычной сверки не меняется. Имена и границы полей взяты
  из модели памяти `ReconcileIn` (memory-service K004, c534146) и закреплены
  в `tests/fixtures/memory_knowledge_contract.json`: `schemas.ReconcileIn` и
  `responses.reconcileState` (поля плана `dryRun`, `stateToken`; `409` с
  `detail.code = snapshot_stale`).
- **Ответ предпросмотра — план.** Ядро проверяет, что память ответила планом:
  `dryRun: true` и непустой `stateToken`. Память без предпросмотра поле
  `dryRun` не знает и применила бы снимок. Такой ответ — ошибка развёртывания
  зависимости, а не план: `502 memory_unavailable` с `details: {memoryStatus:
  200, retryable: false}`, в лог — `error`. Отменить такую запись ядро не
  может. Поэтому память с K004 разворачивается раньше ядра с K008.
- **`409` предпросмотра.** Снимок старше принятого память отвергает `409` и в
  предпросмотре. Ядро отдаёт `409 snapshot_stale`, как у применения.
- **`409` применения.** Два случая дают один код `409 snapshot_stale` с
  `details: {memoryStatus: 409}`: память отвергла `expectedState`, или снимок
  старше принятого. Текущий `stateToken` из ответа памяти клиенту не
  передаётся: план всё равно строится заново предпросмотром.
- **Клиент.** `control-plane-client`:
  `preview_knowledge_snapshot(workspace_id=, snapshot=)` и параметр
  `expected_state` у `submit_knowledge_snapshot`.

### К3. `POST /api/v1/knowledge/documents`

Документ базы знаний вне дела: лицензия, выписка, сертификат.

- **Тело** (`KnowledgeDocumentRequest`):

  | Поле | Смысл |
  |---|---|
  | `workspaceId` | Workspace, от имени которого пишется документ. Обязателен. |
  | `naturalKey` | Естественный ключ документа (1..512). Повторная запись с тем же ключом заменяет фрагменты. |
  | `title` | Заголовок (1..500). |
  | `type` | Тип узла, по умолчанию `document`. |
  | `chunks[]` | Фрагменты текста `{text, heading, order}`, 1..500 — предел одного вызова памяти (`MAX_CHUNKS_PER_REQUEST`). |
  | `links[]` | Сущности базы знаний, о которых документ: `{kind, key, rel}`, до 200. |
  | `meta` | Теги фрагментов (объект), необязателен. |

  Файлы ядро не разбирает. Текст извлекает и режет вызывающий, например
  скилл `knowledge.document_extract@1`. Лишнее поле (`namespace`, `scopes`) —
  `400`. Лимит тела — 8 МиБ, как у снимка.
- **Право** — `observations.write` на `workspace:<workspaceId>`. Разрешение
  workspace, namespace корня дерева и scope видимости `workspace:<id>` — как
  у снимка (п.2, 3 решения).
- **Передача** — `POST /api/brain/documents` (модель памяти
  `DocumentIngestRequest`):
  - `natural_key`, `title`, `type`, `chunks`, `meta` и `namespace` — прямо
    из тела;
  - scope видимости — в `properties.scopes`: видимость узла память читает
    оттуда же;
  - `links` памяти сейчас — список естественных ключей, из которых
    получаются рёбра `LINKS_TO` к существующим узлам без вида связи. Ядро
    передаёт в `links` ключи `key`, а связи целиком `{kind, key, rel}` — в
    `properties.links`;
  - типизированная связь документа с сущностью (`evidenced_by`) попадает в
    граф снимком источника `document:<ключ>` (K019), а не этим маршрутом.
    Если память даст типизированные связи документа, K009 передаст их ими.

  Wire-формат K009 закрепляет contract-тестом против модели памяти.
  Весь документ — один вызов с `replace: true`: повторная запись с тем же
  ключом заменяет фрагменты, продолжений (`replace: false`) ядро не шлёт.
  Одинаковые ключи в `links` памяти передаются один раз, в
  `properties.links` — все связи как есть. Таймаут вызова — таймаут
  сверки (`CP_CONTEXT_RECONCILE_TIMEOUT_SECONDS`): память считает эмбеддинги
  до 500 фрагментов за вызов.
- **Ответ** — ответ памяти как есть, `200` (память отвечает `201`). Ошибки —
  по таблице «Ошибки памяти». `400` памяти — `422 document_invalid`. `422`
  строгого режима видов памяти (тип документа не из включённых пакетов)
  отображается как иной `4xx` — `502`, как у снимка.
- **Журнал** — событие `knowledge.document_stored`, entity — workspace.
  Payload: `naturalKey`, `title`, `type`, `workspaceId`, `rootWorkspaceId`,
  `namespace`, `chunkCount`, `linkCount`. Текст в журнал не попадает. Событие
  не входит в whitelist context mapping. Регистрирует его K009.

### К4. Пакеты онтологии арендатора: `scope: tenant`

Решение «регистрация — только администраторы платформы» закрывало риск общего
реестра: tenant A выпускает версию, которая ломает strict-проверку у tenant B.
Пакет арендатора этот риск не открывает. Такой пакет виден и включается только
в namespace своего арендатора. Имена его видов не совпадают с видами общих
пакетов — память отказывает при регистрации (K002, K007).

- **Тело** `POST /api/v1/knowledge/packs` — манифест как есть
  (`KnowledgePackRegisterRequest`: остальные поля проходят без изменений).
  Ядро читает в нём только `name`, `version` и `scope`:
  - `scope` отсутствует — общий пакет. Правило то же: только
    `CP_KNOWLEDGE_PACK_ADMINS`, иначе `403` с `details.required =
    "knowledge_pack_admin"`. Права `knowledge.packs.manage` для общего пакета
    недостаточно;
  - `scope: tenant` — пакет арендатора вызывающего. Нужно право
    **`knowledge.packs.manage`** уровня tenant (`authz/catalog.yaml`, в режиме
    `local` — плоское право ключа). Список администраторов платформы для
    этого не нужен;
  - иное значение `scope` — `400 invalid_request`.
- **Передача** — `POST /api/memory/packages`: манифест со `scope: tenant` и
  принадлежностью арендатору. Принадлежность вычисляет ядро: tenant
  вызывающего, namespace-префикс `tenant:<tenant>`. Клиент передать её не
  может. Поле берётся из контракта памяти K002, K010 закрепляет его в фикстуре
  контракта.

  Реализация K010. Контракт памяти K007 (`PackIn` memory-service
  `feature/company-knowledge` 038496d) — поля `scope: "tenant"` и `namespace`
  (namespace-владелец). Ядро передаёт `namespace = tenant:<tenantId>`
  (`CP_CONTEXT_NAMESPACE_PREFIX` + id tenant'а вызывающего), то есть namespace
  над всеми деревьями workspace арендатора (`tenant:<t>:ws:<root>`). Память
  показывает пакет владельца в нём и в namespace с префиксом `<владелец>:`.
  Право записи в namespace-владелец память проверяет у учётной записи ядра.
  Поле `namespace` в манифесте клиента — `400 invalid_request`: принадлежность
  не подменить. Запрос отвергается до вызова памяти.
- **Ссылка** на пакет арендатора — `tenant:<name>@<version>` (K002).
  `PUT /workspaces/{id}/knowledge-packs` принимает её как закреплённую
  ссылку: K010 расширяет грамматику `PACK_REF_RE` префиксом `tenant:`. Пакет
  другого арендатора память не находит — `422 pack_not_found`.
- **Ошибки** — как у общих пакетов. Коллизия имени вида с общим пакетом — это
  `400` памяти, ядро отдаёт `422 pack_invalid`.

  Уточнение K010 по контракту памяти K007. Коллизию память отвечает не `400`,
  а `409` с `detail`-объектом `{code, message}`. Код — `pack_name_conflict`,
  `kind_conflict` или `relation_conflict`. Ядро отличает такой ответ от `409`
  иммутабельности версии (`detail` — строка) по `detail.code`. Коллизия — это
  `422 pack_invalid` с `details: {memoryStatus: 409, conflict: <code>}`. Иной
  `409` — по-прежнему `409 pack_version_conflict`.
- **Аудит** — `knowledge.pack_registered`, новая версия схемы события с
  полем `scope` (ADR-0068: версии только добавляют поля). Устойчивый id
  entity пакета арендатора — `uuid5(NAMESPACE_URL,
  "knowledge-pack:tenant:<tenantId>:<name>@<version>")`: имена пакетов разных
  арендаторов могут совпадать. Payload версии 2 — `name`, `version`,
  `status`, `scope` (`common` | `tenant`); у общего пакета `scope: common`, id
  entity прежний.

### Права и клиенты

- `knowledge.packs.manage` — новое право уровня tenant: enum `Permission`,
  `authz/catalog.yaml`, `docs/api.md`.
- `control-plane-client` получает методы в шагах реализации (K008–K010).
  SDK скиллов (`ctx.knowledge`, K013) ходит этими маршрутами с учётной
  записью исполнителя. MCP-инструментов нет, как и раньше.

### Conformance амендмента

- `tests/unit/test_knowledge_contract.py` (K003): OpenAPI валиден (схемы —
  JSON Schema 2020-12, все `$ref` разрешаются); маршруты, тела и `501`;
  `expectedState` не попадает в документ снимка; `knowledge.packs.manage` в
  enum и каталоге.
- `tests/integration/test_company_knowledge_contract.py` (K003): право
  проверяется до `501`, память не вызывается, событий нет. Для пакета
  арендатора не нужен список администраторов, общему пакету права tenant'а
  мало.
- `tests/unit/test_knowledge_memory_contract.py` (K010): тело пакета
  арендатора (`scope`, `namespace` владельца) валидно по закреплённому `PackIn`
  K007 (`tests/fixtures/memory_knowledge_contract.json`). `409` коллизии по
  закреплённой схеме `PackScopeConflict` — `422 pack_invalid`, `409` со
  строкой — `409 pack_version_conflict`.
- `tests/integration/test_knowledge_tenant_packs.py` (K010): арендатор
  регистрирует свой пакет по `knowledge.packs.manage` без
  `CP_KNOWLEDGE_PACK_ADMINS`. Ядро передаёт namespace-владельца. Событие v2
  несёт `scope: tenant` и id entity с tenant'ом. Общий пакет без прав
  администратора платформы — `403` даже с правом tenant'а. `namespace` в
  манифесте — `400`. `tenant:name@version` включается в
  `PUT …/knowledge-packs`, ссылка без версии — `422`.
- `tests/unit/test_document_memory_contract.py` (K009): тело
  `POST /api/brain/documents`, построенное схемой запроса и провайдером,
  валидно по закреплённой модели памяти `DocumentIngestRequest`
  (`tests/fixtures/memory_document_contract.json`): `namespace` в корне,
  `properties.scopes`, ключи в `links`, связи целиком в `properties.links`.
- `tests/integration/test_knowledge_documents.py` (K009): без
  `observations.write` — `403`, память не вызывается; документ дочернего
  workspace уходит в namespace корня со scope дочернего; событие
  `knowledge.document_stored` без текста; лишнее поле и пустые фрагменты —
  `400`; отображение ошибок памяти; лимит тела снимка; `recall` находит
  документ от связанной сущности и сущность от документа по `links_to`.
- `tests/integration/test_knowledge_preview.py` (K008): предпросмотр идёт в
  память с `dryRun`, ответ как есть, событий нет; право, workspace, лимит
  тела и `400` на `expectedState` в теле предпросмотра; применение по
  `stateToken`; изменившееся состояние — `409 snapshot_stale`; ответ без
  плана — `502`. `tests/unit/test_knowledge_memory_contract.py` проверяет
  тела с `dryRun` и `expectedState` по `ReconcileIn` памяти, `tests/contract`
  проверяет их против живой памяти.

## Амендмент 2026-09-28 (company-knowledge, K031): перечень сущностей

Основание — фича `company-knowledge` (`specs/company-knowledge/` суперпроекта),
требования FR-022 и FR-012. Опора — перечень сущностей памяти
`POST /api/memory/entities:query` (K030, амендмент MEM-ADR-020;
memory-service `feature/company-knowledge` 7492f04). Записан и реализован в
K031 (TASK-000796).

**Проблема.** Вопросы вида «все лицензии, действующие до конца года» или «все
предложения внутри ОКПД2 62.01» не начинаются с якоря. `cp_recall` (CP-ADR-0064)
идёт от якоря по связям и такой перечень не даёт. Память умеет отдавать
перечень постранично (K030), но ходить в неё напрямую может только ядро
(TAI-ADR-0031 п.6).

### П1. `POST /api/v1/knowledge/entities:query`

Тело (строгое, лишнее поле — `400 invalid_request`):

| Поле | Смысл |
|---|---|
| `workspaceId` | Workspace, знания которого читаются. Обязателен. |
| `kinds` | Виды сущностей, 1..20, имя `^[A-Za-z][A-Za-z0-9_]{0,62}$` (как у памяти). Неизвестный вид даёт пустой ответ, а не ошибку. |
| `where` | До 20 условий `{attr, op, value}` на атрибуты версии (все должны выполняться). Схема и правила значений — те же, что у `where` в `/context/recall` (`MemoryWhereCondition`). Только литералы: CEL ядро здесь не вычисляет. |
| `asOf` | Момент с часовым поясом. Без него — версии, действующие сейчас. |
| `limit` | 1..500, по умолчанию 100. |
| `cursor` | `nextCursor` предыдущей страницы (1..4096 символов). Ядро его не читает. Остальные поля запроса те же. |

1. **Право** — `events.read` на ресурс `ResourceRef("workspace", workspaceId)`:
   право читать контекст workspace, как у `/context/recall` (в режиме `local` —
   плоское право ключа). Без права — `403`, память не вызывается.
   Неизвестный или чужой workspace — `404`.
2. **Где читается** — namespace корня дерева workspace
   (`tenant:<t>:ws:<root>`, как в п. 2 основного решения), и только он.
   Namespace tenant'а снимков не держит. Namespace и видимость вычисляет
   `graph_scope` — та же функция, что у `/context/recall`.
3. **Видимость вызывающего.** В режиме `policy` в память уходят
   `allowedNamespaces`/`allowedScopes`, вычисленные PDP (`memory_visibility`).
   Если namespace корня вне видимого набора — `403`, память не вызывается. В
   режиме `local` — `allowedScopes`: workspace, его предки и principal. Знания
   соседнего поддерева того же корня не видны.
4. **Передача.** `POST /api/memory/entities:query` identity ядра, с
   `X-Run-Id`. Тело — модель памяти `EntitiesQueryIn`: `kinds`, `limit`, при
   наличии — `where`, `asOf`, `cursor`, а также `namespaces: [<root ns>]` и
   поля видимости. `scope` не передаётся: память принимает `namespaces` или
   `scope`, но не оба сразу.
5. **Ответ** — `200 {items, nextCursor, asOf}`. Форма записи изменена
   амендментом 2026-10-03 (С2). `items` — сущности памяти как
   есть (`EntityItem`: `kind`, `key`, `namespace`, `title`, `attributes`,
   `source`, `scope`, `snapshot_id`, `source_path`, `valid_from`, `valid_to`) в
   порядке `(kind, key, namespace)`. Конец перечня — только `nextCursor: null`:
   при редком фильтре страница бывает короче `limit` и до конца. Курсор
   keyset, страницы не повторяются. Журнал не пишется: это чтение.
6. **Ошибки памяти.** `400` (запрос, который память не читает, например чужой
   курсор) → `422 entities_query_invalid`. Остальное — как в «Ошибках памяти»:
   `502 memory_unavailable`. Провайдер не настроен — `503 memory_disabled`, нет
   ответа за `CP_CONTEXT_TIMEOUT_SECONDS` — `503 memory_timeout`. В отличие от
   `/context`, деградированного ответа нет.

Транзакция БД (право, workspace, видимость) закрывается до вызова памяти.

### П2. Клиенты

`control-plane-client`: `query_knowledge_entities(workspace_id=, kinds=, where=,
as_of=, limit=, cursor=)` — одна страница. MCP-инструмента нет.

### Conformance амендмента K031

- `tests/unit/test_graph_memory_contract.py`: тело, которое строит ядро,
  валидно по закреплённой `EntitiesQueryIn` памяти
  (`tests/fixtures/memory_graph_contract.json`, 7492f04), условия `where` — по
  правилам `typedRequest.where`. Границы `kinds`/`limit`/`where` запроса ядра
  совпадают с границами памяти. Страница по закреплённой `EntitiesQueryResult`
  отдаётся как `{items, nextCursor, asOf}`. Отказы памяти отображаются по
  смыслу.
- `tests/integration/test_knowledge_entities.py`: без `events.read` — `403`,
  память не вызывается. Перечень дочернего workspace читается в namespace
  корня с `allowedScopes` поддерева, чужое скрыто. Страницы проходят весь
  перечень ровно один раз. В режиме `policy` невидимый namespace — `403`,
  видимость PDP уходит в память. Лишние поля и нарушения границ — `400`,
  чужой курсор — `422`, сбой памяти — `502`, без провайдера — `503`.
- `tests/client/test_sdk.py`: `query_knowledge_entities` проходит перечень по
  `nextCursor`.

## Амендмент 2026-09-30 (package-sdk, TASK-001043): чтение того, что пишет установка пакетов

**Проблема** (найдено при ревью S011, вид `KnowledgePack` в package-sdk). Ядро
умело только писать пакеты онтологий: `POST /knowledge/packs` и
`PUT /workspaces/{id}/knowledge-packs`. План установки (S013) не мог показать
разницу: `PUT` заменяет набор целиком, и план не предупреждал, что пропадут
включённые ранее онтологии. SDK регистрировал каждую онтологию при каждом
`apply`, поэтому применяющему всегда требовались права администратора
онтологий, а журнал получал лишние `knowledge.pack_registered` и
`knowledge.packs_configured`.

**Решение.** Два маршрута чтения. Оба — прокси в память с identity ядра, без
транзакции БД во время вызова памяти, без событий журнала.

### Ч1. `GET /api/v1/workspaces/{id}/knowledge-packs`

1. **Право** — `workspaces.read` или `workspaces.manage` на
   `workspace:<id>`: набор пакетов — настройка дерева workspace, а тот, кто
   может его задать (`PUT`), должен видеть, что заменяет. Неизвестный
   workspace — `404`, архивный — `422 workspace_archived` (как у `PUT`); память
   не вызывается.
2. **Где читается** — namespace корня дерева (как у `PUT`). В отличие от
   `PUT`, `id` может быть любым workspace дерева: подпространство работает под
   набором корня, ответ называет корень (`rootWorkspaceId`).
3. **Передача** — `GET /api/memory/namespaces/{ns}/kinds` (ответ памяти
   `{settings: {strict, packages, updated_at, …}, catalog: {packages, …}}`).
4. **Ответ** — `200 {workspaceId, rootWorkspaceId, configured, packs, strict,
   effective, updatedAt}`. `packs` и `strict` — в той форме, в какой их
   принимает `PUT` (`settings.packages`, `settings.strict`); `effective` —
   пакеты, которые память применяет (`catalog.packages`). Память хранит
   `packages: null`, пока набор никто не задавал, — тогда действует её пакет
   по умолчанию: `configured: false`, `packs: []`. Namespace и служебный
   `updated_by` (это identity ядра) не отдаются.
5. **Ошибки** — любой отказ памяти — `502 memory_unavailable`, провайдер не
   настроен — `503 memory_disabled`.

### Ч2. `GET /api/v1/knowledge/packs/{ref}`

1. **Ссылка** — `name@version`, `name` (последняя версия) или
   `tenant:name[@version]` (пакет арендатора вызывающего); грамматика имени и
   версии — памяти (`core.kinds`), иначе `422 invalid_pack_ref`, память не
   вызывается.
2. **Право** — `events.read` (право читать журнал tenant'а, где записаны
   регистрации пакетов) или `knowledge.packs.manage` на уровне tenant'а.
   Общий пакет читает и администратор платформы из
   `CP_KNOWLEDGE_PACK_ADMINS` без этих прав — он же его регистрирует.
3. **Передача** — `GET /api/memory/packages/{name}?version=`. Пакет
   арендатора — `name = tenant:<имя>` и `?namespace=tenant:<tenantId>`
   (namespace арендатора вычисляет ядро): пакет другого арендатора с тем же
   именем не виден.
4. **Ответ** — пакет памяти как есть (`name`, `version`, `kinds` с
   `idPatterns`, `relations`, `description`, `scope`, `ref`), без
   namespace-владельца. Память отвечает `404` (нет такого пакета или версии,
   или пакет чужого арендатора) → `404 not_found` с `details.pack`; иначе —
   `502 memory_unavailable`; провайдер не настроен — `503 memory_disabled`.

Плану установки этого достаточно: версия пакета иммутабельна, поэтому
«зарегистрировано с тем же содержимым» проверяется сравнением с ответом Ч2, а
`404` значит «нужна регистрация». Регистрация по-прежнему требует прав
администратора онтологий, но только когда она действительно нужна.

### Ч3. Клиенты

`control-plane-client`: `get_workspace_knowledge_packs(workspace_id)` и
`get_knowledge_pack(ref)`. MCP-инструментов нет. Провайдер памяти:
`get_package(..., namespace=)`.

### Conformance амендмента 2026-09-30

- `tests/unit/test_knowledge_pack_reads.py`: запросы ядра — по закреплённым
  маршрутам памяти (`tests/fixtures/memory_graph_contract.json`, параметр
  `namespace` у `GET /api/memory/packages/{name}`), ответы разбираются на
  ответах кода памяти (`namespaceKinds`, `packages`); ненастроенный namespace —
  `configured: false`; пакет арендатора — в namespace арендатора, без
  владельца в ответе; права; ссылки; `404` и сбои памяти.
- `tests/unit/test_knowledge_contract.py`: оба маршрута в OpenAPI со своими
  схемами ответа и `403`/`502`/`503`, у пакета — `404`.
- `tests/integration/test_knowledge_pack_reads.py`: набор читается таким, каким
  его оставил `PUT`, из корня и из подпространства; без права — `403`,
  архивного workspace — `422 workspace_archived`, память не вызывается; пакет
  читается по закреплённой ссылке и по имени (`ref` ответа — с версией,
  `name@version`), пакет арендатора — только своим, чужая или неизвестная версия — `404`; чтения
  событий не пишут.
- `tests/client/test_sdk.py`: методы клиента читают набор и пакет.

## Амендмент 2026-10-03 (TAI-ADR-0066 п.5, TASK-001300): связи записей и поля источника

Основание — TAI-ADR-0066 суперпроекта («Экраны вертикальных пакетов
описанием», п.5): блоку вида `related` и странице документа базы знаний в
консоли нужны связи записи. У записей вроде `component` (пакет
`software-delivery@1`) нет атрибутов, их смысл — в связях (`calls`,
`defined_in`, `governs`…), а `entities:query` связей не отдавал. Второе: ядро
отдавало запись памяти как есть, и консоль уже читает поля сверх
`KnowledgeEntityOut` из OpenAPI (`source`/`sources`, `valid_from`; на staging
2026-10-03 — ещё `snapshot_id`, `source_path`, `valid_to`, `namespace`,
`scope`). Поля, которых нет в контракте, можно убрать молча.

### С1. `include` у `POST /api/v1/knowledge/entities:query`

Необязательное поле тела `include` (строгое, как всё тело; `null` — то же,
что его отсутствие):

| Поле | Смысл |
|---|---|
| `relations` | Обязательно. Имена связей, 1..20, имя `^[a-z][a-z0-9_]{0,62}$` (как `RelationSpecIn.relation` пакета памяти), повтор берётся один раз; или `"*"` — все связи, которые объявляют пакеты, включённые в namespace корня (`catalog.relations` из `GET /api/memory/namespaces/{ns}/kinds`). Связь нестрогого namespace, не объявленная пакетом, по `"*"` не читается: её надо назвать. |
| `direction` | `out` — запись субъект связи, `in` — объект, `both` (по умолчанию) — то и другое. |
| `limit` | Связей на запись, 1..200 (граница шага обхода памяти), по умолчанию 20. |

1. **Ответ.** У каждой записи страницы — `relations: [{relation, direction,
   kind, key, title}]`: связь, направление относительно записи (`out`/`in`) и
   противоположный конец. Порядок — `(relation, direction, kind, key)`,
   не больше `limit` на запись. Без `include` поля `relations` нет и память
   связей не читает.
2. **Откуда.** У перечня памяти связей нет, поэтому ядро читает их
   типизированным обходом `POST /api/memory/context/typed`: якорь — сама
   запись (`{kind, value: key}`), каждая связь — шаг глубины 1 от якоря с
   `direction` и `limit` запроса, `allow_semantic: false`, `as_of` — `asOf`
   запроса. Шагов в одном обходе не больше 10 (`MAX_STEPS` памяти): больше
   связей — несколько обходов. Один обход (или их пачка) на запись: лимит
   шага памяти общий для всех якорей, и общий обход страницы отдал бы
   «соседей» одной записи за счёт другой. Обходы страницы идут не больше 8
   одновременно, все — в пределах одного срока
   (`CP_CONTEXT_TIMEOUT_SECONDS`), что и сама страница.
3. **Видимость** — та же, что у записей: namespace корня дерева и
   `allowedNamespaces`/`allowedScopes`, вычисленные для перечня (П1 п.2–3).
   Связь на запись, которую вызывающий не видит, не выдаётся: память не
   доходит до невидимого конца, а ядро отбрасывает связь, конец которой не
   пришёл среди сущностей обхода. Ответ неотличим от отсутствия связи.
4. **Размер страницы.** С `include` — `limit` страницы не больше 100 (иначе
   `400 invalid_request`): каждая запись — свой обход.
5. **Ошибки.** Сбой любого обхода или каталога связей — сбой страницы, как
   сбой перечня (П1 п.6): `502 memory_unavailable`, срок истёк —
   `503 memory_timeout`. Частичной страницы без связей нет.

Нативное `include` в перечне памяти дешевле (один запрос на страницу); когда
память его даст, ядро перейдёт на него, не меняя С1.

### С2. Контракт записи (`KnowledgeEntityOut`)

Запись больше не отдаётся как есть: ядро строит её из `EntityItem` памяти
(MEM-ADR-022: версия, сведённая из источников).

| Поле | Контракт | Откуда |
|---|---|---|
| `kind`, `key`, `title`, `attributes` | да | как в памяти |
| `validFrom`, `validTo` | да | `valid_from`, `valid_to`; пусто — `null` (`validTo: null` — версия открыта) |
| `sources[]` | да | `sources[]` памяти по старшинству; у элемента — `source`, `sourcePath` (цитата), `snapshotId`. Память без `sources` (до MEM-ADR-022) — один элемент из верхних `source`/`source_path`/`snapshot_id` |
| `relations[]` | да, только с `include` | С1 |
| `source`, `source_path`, `snapshot_id`, `valid_from`, `valid_to`, `namespace`, `scope` | переходные | как в памяти, если память их прислала |
| `scope`, `source_path`, `snapshot_id` у элемента `sources` | переходные | как в памяти |

1. **camelCase — контракт.** Консоль и клиенты читают `validFrom`,
   `validTo`, `sources[].source`/`sourcePath`/`snapshotId`. Старший источник —
   `sources[0]`, отдельного поля для него в контракте нет.
2. **snake_case — переходные.** Сохраняются, чтобы консоль, читающая
   `source`/`sources` и `valid_from`, не сломалась до перехода. В OpenAPI они
   помечены `deprecated`. Убрать их можно только следующим амендментом этого
   ADR, после того как консоль перейдёт на camelCase. Молча не убираются.
3. **Не в контракте.** `namespace` — имя хранения в памяти (`tenant:<t>:ws:<root>`),
   его выводит ядро, и клиент его не называет (П1). `scope` — scope
   видимости источника, служебное поле сверки. Оба отдаются только на
   переходный период.
4. **Прочие поля памяти** (новые поля `EntityItem`) больше не проходят без
   решения: поле попадает в ответ, когда его вносит в контракт амендмент.

### С3. Клиенты

`control-plane-client`: `query_knowledge_entities(..., include=)` — тело
`include` как есть. MCP-инструмента по-прежнему нет.

### Conformance амендмента 2026-10-03

- `tests/unit/test_knowledge_entity_relations.py`: связи `out`/`in`/`both` с
  противоположным концом; один обход на запись от неё самой по закреплённым
  `ContextIn`/`typedRequest` с namespace, видимостью и `as_of` перечня; связь
  на невидимый конец не выдаётся (с той же связью без сужения — выдаётся);
  факт без конца среди сущностей отбрасывается; `limit` на запись — у каждой
  своей; `"*"` читает связи каталога namespace; больше 10 связей — пачками;
  нет имён или записей — нет обходов; не больше 8 обходов одновременно;
  сбой обхода или каталога — `502`, срок — `503 memory_timeout`; границы
  `include` (пустое, `null`, неверный тип, лишнее поле, `limit` страницы).
  Поля источника: сведённые `sources`, память без `sources`, разреженная или
  испорченная запись; OpenAPI — контрактные поля без `deprecated`, переходные с
  ним.
- `tests/unit/test_graph_memory_contract.py`: страница памяти по закреплённой
  `EntitiesQueryResult` (перезакреплена с memory-service master 372a52a:
  `EntityItem.sources`) отдаётся в форме С2.
- `tests/integration/test_knowledge_entities.py`: связи через HTTP из дочернего
  workspace — namespace корня, видимость перечня, невидимый вызывающий не
  выдаётся, `"*"`; поля источника в ответе; ошибки `include` — `400`, память не
  вызывается; сбой обхода — `502`.
- `tests/client/test_sdk.py`: `query_knowledge_entities(include=)` отдаёт связи.

## Conformance

```conformance
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: '"knowledge.snapshot_reconciled"'}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: 'Permission.OBSERVATIONS_WRITE, resource=ResourceRef\("workspace"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: '"/api/memory/reconcile"'}
  repo: control-plane
- grep: {path: "client/src/control_plane_client/client.py", pattern: "def submit_knowledge_snapshot"}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: '\{\*\*snapshot, "namespace": namespace, "scopes": scopes\}'}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: "settings.knowledge_pack_admins"}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: '"pack_version_required"'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/knowledge.py", pattern: '"/knowledge/snapshots:preview"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: 'body\["dryRun"\] = True'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: 'body\["expectedState"\] = expected_state'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/knowledge.py", pattern: '"/knowledge/documents"'}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: '"knowledge.document_stored"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: '"namespace": namespace, "properties": properties'}
  repo: control-plane
- grep: {path: "client/src/control_plane_client/client.py", pattern: "def submit_knowledge_document"}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: 'await authorize\(ctx, Permission.KNOWLEDGE_PACKS_MANAGE\)'}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: '"namespace": tenant_namespace\(settings, ctx.tenant_id\)'}
  repo: control-plane
- grep: {path: "src/control_plane/domain/enums.py", pattern: 'KNOWLEDGE_PACKS_MANAGE = "knowledge.packs.manage"'}
  repo: control-plane
- grep: {path: "authz/catalog.yaml", pattern: 'knowledge\.packs\.manage:'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/knowledge.py", pattern: '"/knowledge/entities:query"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/context_provider/http.py", pattern: '"/api/memory/entities:query"'}
  repo: control-plane
- grep: {path: "src/control_plane/application/queries/knowledge_entities.py", pattern: 'Permission.EVENTS_READ, resource=ResourceRef\("workspace"'}
  repo: control-plane
- grep: {path: "client/src/control_plane_client/client.py", pattern: "def query_knowledge_entities"}
  repo: control-plane
- grep: {path: "src/control_plane/application/queries/knowledge_entities.py", pattern: '"anchors": \[\{"kind": item\["kind"\], "value": item\["key"\]\}\]'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/schemas.py", pattern: 'alias="validFrom"'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/knowledge.py", pattern: '"/knowledge/packs/\{ref\}"'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/knowledge.py", pattern: "async def get_workspace_packs"}
  repo: control-plane
- grep: {path: "src/control_plane/application/commands/knowledge.py", pattern: 'Permission.EVENTS_READ, Permission.KNOWLEDGE_PACKS_MANAGE'}
  repo: control-plane
- grep: {path: "client/src/control_plane_client/client.py", pattern: "def get_workspace_knowledge_packs"}
  repo: control-plane
- grep: {path: "client/src/control_plane_client/client.py", pattern: "def get_knowledge_pack"}
  repo: control-plane
```
