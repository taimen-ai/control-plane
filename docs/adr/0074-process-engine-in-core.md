# ADR-0074: Движок процессов в ядре — чистая функция шага, намерения, журнал экземпляра, симуляция и план по хэшу

Статус: Accepted (2026-09-27), фича `process-packages`, задача P002
(TASK-000718). Spec/plan/tasks — `specs/process-packages/` суперпроекта
(FR-003, FR-016…FR-020, FR-024, FR-027, FR-028; конституция ст. II, V, VI);
дизайн — TASK-000715 (редакция 2, ворота плана одобрены владельцем
2026-09-27, отступление по ст. VIII принято), разбиение — TASK-000716.
Контракт (маршруты, тела, права, события) опубликован этим шагом (п.16);
реализация — P004, P006–P009, P013–P015 (таблица п.16); MCP-инструменты
автора — P016 (п.17). Амендмент 2026-09-29 (фича `process-observability`,
P001, TASK-000869): события шагов, таймеры срока, пересчёт сроков при
миграции, ревизия семантики движка — п.3, 8, 11, 13, раздел «Амендмент
2026-09-29: события шагов, сроки SLA…»; сроки SLA — [ADR-0078](0078-process-sla-and-working-time.md).
Амендмент 2026-09-29 (TASK-000903): план и применение
по хэшу — для всех видов каталога ядра (п.11, после уточнения P015).
Амендмент 2026-09-29 (TASK-000904): привязка объекта каталога к пакету —
`package` в ответах, `?package=`, `POST /packages:record` (Е1–Е6).
Амендмент 2026-09-29 (фича `package-sdk`, S004 — TASK-000928): вывод
процессов и календарей из оборота (`:retire`, Ж1–Ж5; реализация — S012) и
субъекты `rule` и `taskType` в `/packages:test` (З1–З5; реализация — S020,
TASK-000944, уточнения — З6; замечания ревью S020 — З7, TASK-001066).
Амендмент 2026-09-30 (TASK-001188, хвост S023): обстановка тестов `rule` и
`taskType` — переменные вида principal, role и workspace, workspace теста,
содержимое артефактов, вход по расписанию и задача до входа у `rule`,
отказ обстановки — красный тест (З8; З1 и З5 обновлены). Замечания ревью
TASK-001188 — там же: `mediaTypes` типа артефакта по умолчанию, principal
теста для переменной всегда, роль пакета не занимается переменной; хвост
ревью (TASK-001204) — там же: предел содержимого и числа `given.artifacts`,
право на workspace переменной, право на задачу `given.event`.
Амендмент 2026-09-30 (TASK-001197, из S027): план свежей установки сверяет
объекты пакета со скиллами, ролями и типами артефактов того же пакета,
правило в тесте включено с начала времени теста, шаг процесса — основание
вызова `external_write` (И1–И4).
Амендмент 2026-10-01 (TASK-001237, из приёмки S033 №11): шаг `human`
предзаполняет поля создаваемой задачи данными дела — `human.customFields`
(п.7, Л1–Л4); в тесте типа задачи — `approve.expectRefused`
([ADR-0061](0061-approval-outcomes-declared-by-task-type.md), амендмент 4).

Контекст: TAI-ADR-0054 «Пакеты процессов с памятью» (решения Р1–Р20 plan;
виды каталога `Process` и `Calendar`, схема `packages/schema/v1` — P001,
TASK-000717); [ADR-0075](0075-cel-expression-profile.md) (язык выражений);
[ADR-0076](0076-processes-and-memory.md) (процессы и память); ADR-0003
(состояние + события); ADR-0023 (порядок журнала `(tx_id, sequence)`);
ADR-0056 (вызов скиллов); ADR-0061 (исходы approval); ADR-0063 (правила
вывода работы, курсор журнала, личность правила Г1); ADR-0067 (стадия
проверки); ADR-0068 (каталог событий); ADR-0073 (реестр агентов, `agent:<key>`).

## Контекст

Процесс организации нельзя описать данными: нет состояния дела, таймеров от
дат, кворума, отмены с компенсацией, приостановки, и ничего нельзя проверить
без стенда (TAI-ADR-0054, «Контекст»). BPMN-движок `process-runtime` жил
отдельно от ядра со своим состоянием, и его задачи не были задачами ядра.

Владелец решил (2026-09-27): процессы исполняет ядро. Отдельный сервис дал бы
второй источник состояния, а процесс как набор правил без экземпляра не
выражает таймеры, кворум и компенсацию (plan, «Отступления»).

Ограничения:

- исполнение детерминировано: при тех же событиях, времени и ответах решения
  совпадают (FR-017), иначе нет ни тестов без стенда, ни replay;
- каждое решение оставляет след с причиной и автором (FR-018);
- действия процесса выполняются от личности пакета, а не применившего
  (FR-020);
- staging — 2 vCPU и 5,8 ГиБ: без нового процесса-воркера;
- ядро нейтрально к предметной области (ст. II).

## Решение

### 1. Процесс и календарь — объекты ядра с неизменяемыми версиями

Процесс — запись tenant'а с ключом `key` (`^[a-z0-9][a-z0-9_-]*$`, до 63
символов). Тело `POST /api/v1/process-definitions` — объект каталога без
конверта: `{key, spec}`, где `spec` — ровно `spec` файла пакета вида `Process`
(`$defs.processSpec`). Установщик пакетов подставляет переменные установки
`${…}` (например `spec.workspaceId`) и больше ничего не меняет.

- Версия — `spec.version`. Пара `(key, version)` неизменяема; у версии есть
  `definitionHash` — `sha256:<hex>` канонического JSON `spec` (ключи
  отсортированы, без пробелов — та же функция, что у ревизии агента,
  ADR-0073 п.2).
- Повтор той же версии с тем же хэшем — `200` без записи. Та же версия с другим
  содержимым — `409 process_version_conflict`. Версия не больше последней —
  тоже `409 process_version_conflict` (`details.latestVersion`).
- Процесс уровня workspace (`spec.workspaceId`) виден и правится по правам
  на этом workspace, без него — на tenant, как правило (ADR-0063).
- Владелец процесса — `spec.owner`, цепочка назначения (`assignChain`, как у
  `human.assign`; амендмент TAI-ADR-0054 2026-09-27): ему адресуются задачи о
  самом процессе — расхождение с регламентом, ошибки экземпляров. Схема
  каталога его не требует; версия хранит его в `spec` и отдаёт полем `owner`
  `ProcessDefinitionOut`, без владельца проверка даёт предупреждение
  `process_owner_missing`.
- Адрес — `key` (последняя версия) или `key@version`:
  `GET /process-definitions/{ref}`, список версий —
  `GET /process-definitions/{key}/versions`.

Календарь (п.9) устроен так же: `POST /api/v1/calendars {key, spec}`, версия
выдаётся ядром при отличии хэша канонического JSON `spec`.

### 2. Проверка определения: форма по схеме каталога, затем язык

Публикация проходит две ступени (реализация — P006):

1. **Форма** — JSON Schema вида из схемы каталога суперпроекта
   (`$defs.processSpec`). Ядро держит её копию; contract-тест держит копию
   равной суперпроекту (`tests/fixtures/superproject/object.schema.json`).
   Язык описывается одной схемой, и ядро не дублирует её моделью pydantic:
   тело маршрута в OpenAPI — объект, а его форма — схема каталога.
2. **Язык**:
   - типы всех выражений CEL (ADR-0075), включая `memory`, `recall` и
     `context` (ADR-0076);
   - неизвестные поля данных; `input`/`output`/`export` против схемы данных;
   - уникальность и стабильность id элементов; достижимость шагов и стадий,
     тупики;
   - ссылки на таблицы решений, типы задач, скиллы (вход и выход — по схемам
     скилла из каталога), календарь, агента личности (п.14);
   - пробелы и перекрытия таблиц решений.

Найденное — список `problems` в одной форме для всех маршрутов и
MCP-инструментов (`ProcessProblemOut`):

```json
{"code": "unknown_data_field", "severity": "error",
 "path": "/spec/stages/1/steps/0/output/as/decison",
 "file": "processes/tender.yaml", "line": 42,
 "message": "data has no field decison", "hint": "did you mean decision?"}
```

`file` и `line` есть, когда объект пришёл файлом пакета (п.10); у
`POST /process-definitions` они `null`. Хоть одна ошибка —
`422 invalid_process` с `details.problems`; предупреждения (например,
документ `governedBy`, которого нет в памяти, ADR-0076 п.6) не мешают
публикации и возвращаются в `warnings` версии.

### 3. Экземпляр закреплён за версией

Экземпляр — запись `process_instances`: `definition_key`,
`definition_version`, `instance_key`, `workspace_id`, `status`
(`running | suspended | completed | failed | cancelled`), `outcome`, данные
(`data`, JSON по схеме процесса) и состояние движка (стадии, открытые
элементы, стек областей, счётчики повторов).

- **Старт** — по `start.on` (событие журнала или наблюдение) и `start.key`
  (CEL от события). Уникальный индекс `(tenant_id, definition_key,
  instance_key)` даёт ровно один экземпляр на ключ (FR-013). Событие старта
  с ключом существующего экземпляра становится входом этого экземпляра и
  событием `process.correlated`, а не вторым экземпляром.
- **Явный старт** (TAI-ADR-0055, решение владельца 2026-09-28: цели —
  процессами) — `POST /api/v1/process-instances {process, key, data?,
  workspaceId?}` с правом `processes.operate` (на workspace экземпляра):
  экземпляр без внешнего события, ключ и начальные данные заданы (`data`
  проверяется по схеме данных процесса — иначе `422 invalid_process_data`;
  `start.set` не исполняется). Повтор с тем же ключом — `409
  process_instance_exists` с `details.instanceId` существующего экземпляра,
  не второй экземпляр. `process.started` несёт `triggerType: command` и
  `triggerEventId: null`. Так заводятся постоянные цели — процессы-сверки
  без `complete`, чья веха достигается и снимается вслед за данными
  (`process.milestone_lost`, п.13). Задачам экземпляра `goalId` не
  выставляется: цель выводится из ядра.
- **Версия** — последняя опубликованная на момент старта; экземпляр остаётся
  на ней до конца или до миграции (п.11, FR-019).
- Workspace экземпляра — workspace процесса; задачи и approvals экземпляра
  заводятся в нём.

Амендмент 2026-09-29: ревизия семантики `engine_revision` у версии процесса —
раздел «Амендмент 2026-09-29».

### 4. Чистая функция шага

Модуль домена `domain/process_engine.py`:

```python
def step(definition, state, input) -> tuple[state, list[Decision], list[Intent]]
```

- Без ввода-вывода: ни базы, ни HTTP, ни часов, ни случайности. Время движка —
  время входа (`input.at`): время события журнала или `due_at` сработавшего
  таймера. Идентификаторы, которые движок порождает (таймер, намерение,
  recall), выводятся из `(instance_id, seq, index)` детерминированно (UUIDv5).
- `Decision` — запись журнала экземпляра (п.5): переход, вход и выход
  стадии, веха, установка таймера, голос, компенсация, ошибка и её
  обработчик, с причиной (`reason`: какое условие, какая строка таблицы,
  какой вход).
- `Intent` — что должно случиться вне состояния экземпляра (п.6). Движок не
  знает, как намерение исполняется.
- Ошибки — RFC 7807 (`type`, `status`, `detail`); поднимаются до ближайшего
  `try`, иначе экземпляр `failed` и событие `process.failed`.

Живой прогон, тесты пакета и replay зовут одну и ту же функцию (FR-025): у
них разный только исполнитель намерений.

### 5. Входы и журнал экземпляра

Вход движка — одно из:

| Вход | Откуда |
|---|---|
| событие старта или `correlate` | событие журнала или наблюдение (`observation.recorded`), совпавшее с `on` и ключом |
| завершение, отмена задачи шага | `task.completed` / `task.updated` задачи с внешней ссылкой `process/<instance>/<element>` (ADR-0047) |
| решение согласования | `approval.approved`, `approval.rejected`, `approval.cancelled` approvals, заведённых экземпляром |
| результат скилла | `skill.invocation_succeeded` / `skill.invocation_failed` вызова, заведённого экземпляром |
| срабатывание таймера | строка `process_timers`, у которой наступил `due_at` |
| ответ памяти | ответ `recall` или его таймаут (ADR-0076 п.4) |
| команда оператора | `:suspend`, `:resume`, `:cancel` |

**Журнал экземпляра** — таблица `process_instance_events` (только
добавление): `seq` (по экземпляру), `at` (время движка), `kind` входа,
нормализованный вход (`event_id` источника, тип, нужные движку поля), решения и
намерения шага, `actor_id` (кто стоит за входом). Это журнал решений FR-018 —
`GET /process-instances/{id}/journal`. Replay и объяснение экземпляра читают
только его: вход записан целиком, поэтому повтор не зависит ни от журнала
ядра, ни от памяти, ни от текущих версий каталога. Уникальность
`(instance_id, source_ref)` делает повторную доставку того же входа пустой.

**Подача входов.** Воркер читает журнал ядра своим курсором `processes` в
`event_consumer_cursors` — тот же механизм, что у правил (ADR-0063): порядок
`(tx_id, sequence)`, стабильный горизонт, пакет по tenant'у в одной
транзакции под `FOR UPDATE SKIP LOCKED`, backoff при ошибке. Цикл
`process_events()` сопоставляет событие экземплярам (по внешней ссылке задачи,
id approval и вызова скилла, по `start`/`correlate`) и для каждого делает шаг
в транзакции пакета. Таймеры — отдельный цикл `process_timers()` (п.8). Новый
процесс в контейнере не появляется: оба цикла живут в `control-plane-worker`.

### 6. Намерения и их исполнение

| Намерение | Чем исполняется |
|---|---|
| `create_task`, `cancel_task` | команды задач: тип, форма, назначение, срок, внешняя ссылка `process/<instance>/<element>`, профиль контекста (ADR-0076 п.5) |
| `request_approvals`, `close_approvals` | команды approvals: по approval на согласующего, `excludedPrincipals` (п.7) |
| `invoke_skill` | долговечный `skill_invocation` (ADR-0056); агент — задачей на `agent:<key>` |
| `recall` | запрос к памяти вне транзакции (ADR-0076 п.4) |
| `remember` | наблюдение ядра (ADR-0076 п.5) |
| `set_timer`, `cancel_timer` | строки `process_timers` |
| `emit_event` | событие `process.*` журнала ядра |
| `start_child` | экземпляр вложенного процесса с родителем |
| `complete` | статус и исход экземпляра, `process.completed` |

Намерения исполняет прикладной слой (`commands/process_instances.py`)
имеющимися командами ядра **в той же транзакции**, что сохраняет `state'` и
запись журнала: либо записано всё, либо ничего. Обычные проверки команд
(права, eligibility, схемы) проходят от личности процесса (п.14). Отказ
команды — ошибка шага (`type: intent_failed`, `detail` — код команды); её
ловит `try` процесса, иначе экземпляр `failed`. Только `recall` уходит
наружу, и его ответ — новый вход (ADR-0076 п.4); вызов скилла ждёт своего
события, как у правил.

### 7. Люди и согласования

- **Шаг `human`** — задача ядра: `taskType`, форма (JSON Schema и uischema у
  типа задачи, шаг может сузить), `assign` — цепочка кандидатов (`principal`,
  `role`, `agent:<key>` по ADR-0073 А1, CEL), `due`, эскалации. Результат —
  `customFields` задачи при завершении, проверенные по форме; в данные — через
  `output.as`. Эскалации — таймеры шага; уровень даёт `process.escalated`,
  уведомления — обычные правила уведомлений по этому событию.
- **Шаг `approve`** — по approval ядра на каждого согласующего; `mode`
  `parallel | sequential`, кворум `all | any | {atLeast} | {percent}`,
  досрочное решение. Кворум считает чистая функция
  `domain/approval_quorum.py`; лишние approvals закрываются (`:cancel`),
  уход согласующего (`approval.cancelled`) пересчитывает кворум.
  Правила функции (P008): `tally(approvers, votes, quorum, mode,
  earlyDecision)` отвечает исходом `pending | approved | rejected`, причиной
  и списком `active` — согласующие, чей approval должен быть открыт сейчас;
  движок запрашивает недостающие и закрывает открытые вне списка (после
  решения список пуст). Нужно одобрений: `all` — все оставшиеся, `any` — 1,
  `{atLeast: n}` — n, `{percent: p}` — ⌈p·N/100⌉, не меньше 1, где N —
  оставшиеся (доля считается точно: `66.7` — это 667/10, а не двоичная дробь). Ушедший
  согласующий не считается: `all` перестаёт его ждать, `percent` берёт долю
  от меньшего числа, `atLeast`, недостижимый оставшимися, — отказ
  (`quorum_unreachable`); ушли все — отказ (`no_approvers`: пустое «все» не
  согласие). С досрочным решением шаг решается, как только кворум набран или
  недостижим; без него — когда проголосовали все оставшиеся (`all_voted`).
  `sequential` держит открытым один approval — первого в порядке, кто ещё не
  голосовал; `parallel` — всех.
- **Разделение обязанностей проверяет ядро, а не движок.** У approval
  появляется поле `excludedPrincipals` (запрос `POST /approvals`, ответ
  `ApprovalOut`, колонка `approvals.excluded_principals`). Решение principal'а
  из списка команда решения отвергает `403 separation_of_duties_violation`
  при любом пути — движок, консоль, канал, MCP. Движок заполняет список из
  `separationOfDuties` шага. Реализовано в P008: колонка — JSON-массив id
  principal'ов (`[]` — никто не исключён, как у approval до ревизии);
  запрос принимает до 100 principal'ов tenant'а (повторы схлопываются,
  неизвестный — `404`, исключённый `assignedPrincipalId` — `422
  invalid_approval`: решать было бы некому). Проверка стоит в общей проверке
  eligibility команды решения, поэтому действует и на `:reject`, и на отмену
  чужого gate (`:cancel` требует полномочий решающего), и при любой роли
  исключённого. Список «Важное» (ADR-0071) исключённому такой approval не
  показывает. `approval.requested` v3 несёт `excludedPrincipals` (только
  добавление).
- **Шаг `approve` с `separationOfDuties`** (амендмент 2026-09-30,
  TASK-001230; до него исполнение отвечало `not_implemented`, и дело с таким
  шагом всегда уходило в `failed`). Исполнение `request_approvals` передаёт
  `excludedPrincipals` в каждый approval шага, в последовательном режиме — и
  в approvals следующих согласующих (список хранится в записи шага в
  `refs`). Значение — id principal'ов tenant'а: значение, которое не id
  principal'а (адрес, `agent:<key>`, не список), и неизвестный principal —
  отказ намерения (`422 invalid_approval`, `404`), то есть `intent_failed`:
  запрет не пропадает молча. Пустое значение (`null`, `""` — например, не
  заполнен `uploadedBy`) — такой же отказ: движок передаёт вычисленный
  список как есть, ничего не отфильтровывая, и проверку делает только
  исполнение (ревью 2026-09-30: фильтр в движке молча снимал исключение, и
  согласовать мог кто угодно). Явный согласующий из списка исключённых —
  `principal` или `agent:<key>`, чей principal исключён (агент разрешается
  в principal до проверки), — тоже `422 invalid_approval` до запроса первого
  approval: решить за него некому, и последовательный шаг иначе упал бы
  посреди голосования. Решение исключённого отвергает общая проверка команды
  решения, поэтому запрет один на все пути решения согласования шага:
  API (`:approve`, `:reject`), кнопка канала через notification-service
  (токен канала решает той же командой, CP-ADR-0070), харнесс, MCP;
  досрочное решение (`earlyDecision`) считает только принятые ядром голоса,
  отвергнутый голос до движка не доходит. Песочница тестов пакета
  (`packages:test`) отвечает так же: голос исключённого —
  `separation_of_duties_violation`, исключённый явный согласующий и пустое
  значение — `intent_failed`.
- **Отмена approval с исключёнными — тоже решение** (ревью 2026-09-30).
  Шаг засчитывает отменённый не самим экземпляром approval как «на одного
  согласующего меньше» (`total` входа `approval`, п.14), поэтому `:cancel`
  чужого approval с непустым `excludedPrincipals` требует того же, что
  отмена чужого gate: `approvals.decide` и eligibility решающего. Исключённый
  не отменяет такой approval вовсе — `403 separation_of_duties_violation`,
  даже если он автор запроса и держит `approvals.manage`. Автор запроса
  (у approval шага — identity процесса) закрывает свои approvals, как и
  раньше, — так экземпляр закрывает согласования шага сам.
- **Исключение действует по principal и по делегированию.** Проверка
  сравнивает principal вызывающего со списком; кроме того, агент, которому
  исключённый principal выдал действующее делегирование (не отозвано, не
  истекло, уже началось), считается исключённым тоже — решает ли он в
  сессии `onBehalfOf` или без неё: `403 separation_of_duties_violation` с
  `details.delegationId` на `:approve`, `:reject` и `:cancel`. Отозванное
  делегирование агента больше не связывает.
- **Согласовать некому.** Если все держатели роли approval в его области
  исключены (единственный держатель роли загрузил счёт), approval всё равно
  создаётся и ждёт; экземпляр не падает. Владелец процесса видит в «Важном»
  (ADR-0071) элемент правила `approval.undecidable@1` (`kind: undecidable`,
  `reasonCode: undecidable_process_owner`) с действиями «открыть approval» и
  «открыть экземпляр». Владелец — первый разрешимый кандидат `spec.owner`
  (как у событий SLA, CP-ADR-0078 §3), вычисленный на момент запроса шага и
  записанный в запись шага в `refs`. Условие проверяется при каждом чтении
  списка: выдали роль другому — элемент исчезает сам, и approval решает
  новый держатель; отменили или решили approval — тоже. Approval без
  процесса (прямой `POST /approvals`) с тем же условием видит его автор
  (`undecidable_requester`). Отдельного события и уведомления нет:
  «Важное» — единственный сигнал. Запасной адресат (ревью 2026-09-30): если
  `spec.owner` не разрешился ни в кого (нет такой роли, выражение пустое),
  сигнал получает тот, кто запустил экземпляр (`startedBy`,
  `reasonCode: undecidable_process_starter`). Экземпляр, запущенный без
  principal'а (`startedBy` пуст) и без владельца, сигнала не даёт никому —
  это известное ограничение; владельца процессу с `separationOfDuties`
  стоит объявлять. Правило читает одним запросом только approvals, которые
  действительно некому решить (проверка держателей роли — в SQL, по
  workspace), не более 500 старейших за вызов; экземпляр для них находится
  одним запросом по GIN-индексу `refs`.

### 8. Таймеры

- Строка `process_timers(instance_id, element, due_at, expr_hash,
  reads, state, remaining_seconds, provisional)`; `state` —
  `pending | frozen | fired | cancelled`.
- `due` задаётся длительностью, датой из данных или сдвигом по календарю
  (CEL, ADR-0075). `reads` — поля данных, которые выражение читает (разбор
  CEL).
- Цикл `process_timers()` берёт наступившие строки под `FOR UPDATE SKIP
  LOCKED` и делает вход «таймер» в той же транзакции; событие
  `process.timer_fired`. Сработавший таймер не откатывается.
- **Пересчёт.** Шаг, изменивший данные, пересчитывает `due` несработавших
  таймеров, чьи `reads` пересекаются с изменёнными полями, — событие
  `process.timer_rescheduled` со старым и новым временем. Процесс может
  реагировать на перенос срока (`on: data.changed`).
- **Приостановка** замораживает таймеры: хранится остаток, `due_at` — `null`.
  При возобновлении `due_at` = время возобновления + остаток. Таймер от даты в
  данных остатка не хранит и пересчитывается от данных.

Амендмент 2026-09-29: виды таймера `sla` и `sla_warning`,
`process_timers.remaining_unit` — раздел «Амендмент 2026-09-29».

### 9. Календарь

Вид каталога `Calendar` (`$defs.calendarSpec`): часовой пояс, выходные дни
недели, по годам — праздники, перенесённые рабочие дни, сокращённые дни,
`provisional` и источник. Таблица `calendars (key, version, calendar_hash,
spec)`; версия — по хэшу, как у процесса (п.1). Чтение
`GET /calendars[/{key}[@version]]` — любому аутентифицированному: календарь
не секрет и не персональные данные. Запись — `calendars.write`.

- `cal.*` (ADR-0075) на предварительном годе помечает результат; таймер и
  экземпляр показывают `provisional: true` («срок предварительный»).
- Каждое вычисление записывает в журнал экземпляра версию календаря, на
  которой оно сделано: replay берёт её, а не текущую.
- Новая версия календаря пересчитывает несработавшие таймеры экземпляров,
  чьи выражения зовут `cal.*` с этим ключом (`process.timer_rescheduled`,
  `cause: calendar_changed`) — так подтверждённый год снимает пометку.

Уточнение (P004, 2026-09-27). Арифметика — `domain/calendar.py`, чистые
функции над версией календаря:

- день опубликованного года рабочий, если он в `workdays`; иначе — если он
  не в `holidays` и не выходной день недели (`weekend`, по умолчанию `[6, 7]`);
  день вне опубликованных годов знает только выходные дни недели;
- `addWorkdays(ts, n)` — `n`-й рабочий день после дня `ts` (при `n < 0` — до
  него), сам день `ts` не считается, `n = 0` — тот же момент; время суток
  сохраняется в часовом поясе календаря;
- `workdaysBetween(a, b)` — число рабочих дней в `(a, b]`, при `b < a` —
  со знаком минус: для рабочего дня `b` после `a`
  `addWorkdays(a, workdaysBetween(a, b)) = b`;
- «предварительно» — если вычисление посмотрело хоть один день года с
  `provisional: true` или года, которого в календаре нет;
- шаг ограничен 50 годами просмотра: календарь без рабочих дней даёт ошибку,
  а не бесконечный цикл.

Хэш версии считается от `spec`, где годы упорядочены по номеру, даты и
`weekend` — по возрастанию: пакет, переставивший строки, новой версии не
даёт. Сверх схемы ядро проверяет часовой пояс (IANA), один элемент на год,
принадлежность дат своему году и что день не одновременно праздник и
рабочий — иначе `422 invalid_calendar` с `details.code` и `details.path`.
Строки `calendars` неизменяемы (триггер). Объект пакета вида `Calendar`
(`{apiVersion, kind: Calendar, key, spec}`, конверт уже сверен со схемой
каталога проверкой пакета) ставит
`api/v1/calendars.install_calendar` — та же публикация под `calendars.write`
применяющего; `packages:apply` (P015) публикует календарь той же командой
`publish_calendar`.

### 10. Симуляция в трёх режимах — в ядре

Все три режима исполняет ядро тем же `step` (FR-024, FR-025).

**Тело — пакет файлами.** `PackageSource {files: [{path, content}]}`: пути
внутри пакета (без `..`, пустых частей и абсолютных путей), содержимое — текст
YAML 1.2 (TAI-ADR-0054 п.11: `on`, `yes`, `no` — строки). Ядро само разбирает
файлы, поэтому находка называет файл и строку. До 1000 файлов, до 1 000 000
символов в файле.

1. **Тест по сценарию — `POST /packages:test`** (`packages.test`).
   - Ядро собирает определения пакета в памяти, не записывая их в каталог, и
     прогоняет тесты `tests/*.test.yaml` (контракт —
     `packages/schema/v1/test.schema.json`; фильтр `tests` — пути файлов).
   - Намерения исполняет **песочница**: задачи, approvals и таймеры — объекты
     в памяти; скиллы, агенты и `recall` — заглушки теста (`mocks`), чей выход
     проверяется по схеме скилла из каталога (не по схеме — провал теста);
     время виртуальное (`advance: P3D`).
   - Транзакция базы — только чтение (`SET TRANSACTION READ ONLY`): каталог,
     роли, календари. Исходящих вызовов нет: у песочницы нет клиентов HTTP,
     памяти и хранилища содержимого. `noSideEffects` в тесте — счётчик
     записей песочницы.
   - Ответ `PackageTestOut`: `status` (`passed | failed | invalid`),
     `problems` (проверка п.2 по всем объектам пакета), результат по тестам с
     провалами по шагам, покрытие по процессам — элементы, переходы, строки
     таблиц, обработчики ошибок, с перечнем непройденного.
   - `?checkOnly=true` — только проверка п.2, без тестов. Её зовёт
     `cp_packages check`, когда ядро доступно.
2. **Replay — `POST /process-definitions/{key}:replay`** (`packages.test`).
   Тело — кандидат `spec`. Ядро берёт журналы экземпляров (`instanceIds` или
   последние `limit` ≤ 200 текущей версии, только из workspace'ов, где у
   вызывающего есть `processes.read`), подаёт их входы кандидату и сравнивает
   решения и намерения с записанными. Ответ — расхождения по экземплярам:
   первая запись журнала, где пути расходятся, элемент, записанное и
   полученное. Ответы `recall` берутся из журнала (ADR-0076 п.4). Ничего не
   пишется.
3. **Пробный прогон на стенде** — тот же `packages:test` со сценарием
   `given.fromInstance`: начальное состояние копируется из живого экземпляра
   (нужно `processes.read` на его workspace). Записи — как у теста: никаких.

### 11. План и применение по хэшу

**`POST /packages:plan`** (`packages.plan`) — ответ `PackagePlanOut`:

- `changes` — структурный diff по объектам и полям: `create | update | rename
  | restore | retire | unchanged` (`restore` — амендмент package-sdk, Ж3), у поля — `before`, `after` и владелец (`package` или
  `console`: поле правил человек после последнего применения; такое поле не
  перетирается без флага, TAI-ADR-0044) и `applies`;
- `processes` — у изменённого процесса поведенческий diff (replay п.10 на
  `replayLimit` экземплярах) и судьба открытых экземпляров по версиям: `pin`,
  `migrate` или `unaffected`, `migrationRequired`;
- `regulationCoverage` — покрытие разделов регламентов элементами по
  `governedBy` (ADR-0076 п.6);
- `problems` — проверка п.2;
- `catalogEtag` — `sha256` канонического списка `(kind, key, версия или
  хэш)` объектов, которые пакет затрагивает, и отметок владения их полей;
- `planHash` — `sha256` канонического JSON `{package: хэш файлов,
  catalogEtag, changes, processes}`.

**`POST /packages:apply`** `{package, planHash}` применяет ровно этот план
(FR-028). Ядро строит план заново из тех же файлов и сравнивает: другой
`catalogEtag` или `planHash` — `409 plan_stale` (стенд изменился после
показа). Применение — одна транзакция; каждое изменение проходит право своего
вида (`processes.write`, `calendars.write`, `task_types.manage`…), право
`packages.plan` нужно на сам маршрут.

- **Переименования** объектов — `package.yaml → renames: [{kind, from, to}]`
  (как `moved` в Terraform): план переносит объект, а не удаляет и создаёт.
- **Миграции экземпляров** — `migrations: [{from, to, policy, map}]` процесса.
  `pin` — экземпляры дорабатывают на старой версии; `migrate` — состояние
  переносится по карте `старый элемент → новый`, событие `process.migrated` и
  запись журнала у каждого экземпляра. Элемент, на котором стоят открытые
  экземпляры, исчез без покрывающей миграции — ошибка плана
  `migration_required`; `apply` такого плана отказывает `422
  migration_required`.

Амендмент 2026-09-29: вход `migrated` пересчитывает сроки при миграции, раздел
`deadlines` плана — раздел «Амендмент 2026-09-29».

### 12. Права

| Право | Ресурс | Что даёт |
|---|---|---|
| `processes.read` | workspace процесса (без него — tenant) | определения, экземпляры, журнал |
| `processes.write` | workspace процесса | публикация версии процесса |
| `processes.operate` | workspace процесса | `:suspend`, `:resume`, `:cancel` экземпляра |
| `packages.test` | tenant | `packages:test`, `:replay` |
| `packages.plan` | tenant | `packages:plan`, `packages:apply` (плюс права видов) |
| `calendars.write` | tenant | публикация календаря |

Автор процесса и оператор дела — разные роли: остановить или отменить чужое
дело не следует из права описать процесс. Тест и план ничего не пишут и
потому отделены от применения. Имена — в `Permission` и `authz/catalog.yaml`.

*Амендмент 2026-09-29 (Ж4): `processes.write` даёт и `:retire` процесса,
`calendars.write` — `:retire` календаря.*

### 13. События

`entityType` — `process_instance` (id экземпляра), у публикации —
`process_definition`, у календаря — `calendar`. Общие поля экземпляра:
`instanceId, definitionKey, version, instanceKey`.

| Тип | Когда | Payload сверх общих полей |
|---|---|---|
| `process.definition_published` | новая версия процесса | `key, version, definitionHash, previousVersion, workspaceId, identityAgent, elements` |
| `process.started` | старт экземпляра | `triggerEventId, triggerType, memory` |
| `process.correlated` | событие попало в существующий экземпляр | `triggerEventId, triggerType, changedFields` |
| `process.data_changed` | изменились данные | `changedFields, element, memory` |
| `process.stage_entered`, `process.stage_exited` | вход, выход стадии | `stage` |
| `process.milestone_reached` | веха | `milestone, stage` |
| `process.milestone_lost` | достигнутая веха перестала выполняться (TAI-ADR-0055) | `milestone, stage` |
| `process.timer_fired` | таймер сработал | `timerId, element, dueAt` |
| `process.timer_rescheduled` | срок сдвинулся (п.8, п.9) | `timerId, element, previousDueAt, dueAt, provisional, cause, changedFields` |
| `process.escalated` | уровень эскалации | `element, level, action, taskId, to`; с версии 2 — `addressees, unresolved` (ADR-0078, амендмент 2026-09-30) |
| `process.suspended`, `process.resumed` | приостановка, возобновление | `cause, reason` / `cause` |
| `process.compensated` | компенсации выполнены | `scope, steps` |
| `process.recall_completed`, `process.recall_timed_out` | ответ памяти или его отсутствие (ADR-0076 п.4) | `step, recallId, asOf, nodeCount, edgeCount, truncated, resultHash` / `step, recallId, reason` |
| `process.migrated` | экземпляр перенесён (п.11) | `fromVersion, map, policy` |
| `process.completed` | исход | `outcome, memory` |
| `process.cancelled` | отмена оператором | `reason, compensated` |
| `process.failed` | ошибка без обработчика | `error, element` |
| `calendar.published` | новая версия календаря | `key, version, calendarHash, previousVersion, years, provisionalYears` |

*Амендмент 2026-09-29 (Ж2, Ж3): `process.definition_retired` (`key,
latestVersion, workspaceId, reason, openInstances, byVersion`) и
`calendar.retired` (`key, latestVersion, reason`); действие `restore` плана
пишет `process.definition_restored` и `calendar.restored` (`key,
latestVersion, packageKey, packageVersion`).*

`memory` — вычисленная проекция дела (ADR-0076 п.2). Решения движка целиком
живут в журнале экземпляра (п.5); событие журнала ядра говорит остальной
платформе, что с делом случилось. Каталог (`domain/event_catalog.py`,
`docs/events/`) публикует схемы этим шагом.

Амендмент 2026-09-29: события шагов `process.step_entered`/`step_exited` и
сроков `process.sla_*` — раздел «Амендмент 2026-09-29».

### 14. Личность процесса

`spec.identity: {agent: <key>}` — ключ описания агента (CP-ADR-0073), как у
правила (ADR-0063 Г1): при публикации агент есть и не выведен из оборота
(иначе `422 unknown_agent`), `identity.kind` — `agent` или `service`, права
агента ⊆ права публикующего (`403 permission_escalation`). Процесс без
личности не публикуется — `422 process_identity_required` (FR-020): схема
каталога поле не требует, это проверка языка. Все намерения экземпляра
исполняются полномочиями principal'а агента; он автор задач, approvals,
наблюдений и событий `process.*` (`actorId`). Агент без principal или
выведенный из оборота — ошибка шага `credential_inactive`, экземпляр ждёт
её обработчика, как правило ждёт связки.

### 15. Нейтральность

В `domain/process_*`, `domain/cel_profile.py` и схеме вида `Process` нет
понятий предметных областей; страж-тест ищет в них слова доменов (список в
тесте, P009). Процесс оплаты счёта — второй потребитель того же языка.

### 16. Контракт до реализации

Маршруты объявлены в `api/v1/processes.py`, `calendars.py`, `packages.py` и
опубликованы в OpenAPI. До своего шага каждый проверяет право и отвечает
`501 not_implemented` с `details: {adr: CP-ADR-0074, implementedBy:
"process-packages <шаг>"}`:

| Маршрут | Шаг |
|---|---|
| `POST/GET /calendars`, `GET /calendars/{ref}` | P004 (реализован) |
| `POST/GET /process-definitions`, `GET …/{ref}`, `GET …/{key}/versions` | P006 (реализован) |
| `POST /approvals` с непустым `excludedPrincipals` | P008 (реализован) |
| `GET /process-instances[/{id}[/journal]]`, `:suspend`, `:resume`, `:cancel`, `POST /process-instances` | P009 (реализован) |
| `POST /packages:test` | P013 (реализован) |
| `POST /process-definitions/{key}:replay` | P014 (реализован) |
| `POST /packages:plan`, `POST /packages:apply` | P015 (реализован) |

Шаг заменяет тело обработчика и не меняет подпись. Таблицы
(`process_definitions`, `process_instances`, `process_timers`,
`process_instance_events`, `calendars`) вводит ревизия alembic фичи
`b8e3f1c6d2a9`, колонку `approvals.excluded_principals` — следующая за ней
`e3b7c1d9a4f2` (P008); существующие таблицы, кроме approvals, не меняются.

Уточнение (P006, 2026-09-27). Проверка определения —
`domain/process_definition.py`, таблицы решений — `domain/decision_table.py`,
публикация и чтение — `application/commands/process_definitions.py`:

- **Схема вида** — `domain/process_spec.schema.json`: `$defs.processSpec`
  схемы каталога со всеми `$defs`, до которых он дотягивается
  (`process_schema_from_catalog()`); тест держит её равной закреплённой
  копии схемы каталога. `spec.workspaceId` сверяется отдельно: в каталоге это
  переменная установки `${…}`, ядро получает id workspace; неподставленная
  переменная — `unresolved_install_variable`. Нарушение схемы — находка
  `schema_violation` с указателем на самое глубокое место, а не `400` маршрута:
  тело маршрута — объект.
- **Хэш.** `spec` нормализуется (строки NFC, целые числа, записанные дробью, —
  целыми) и хэшируется канонически, как ревизия агента, но с двумя отличиями:
  дробные числа разрешены (кворум `percent`, числа таблиц решений), глубина —
  до 64 уровней, строка — до 20 000 символов. Порядок ключей версии не даёт.
- **Личность** проверяется в два шага: отсутствие (`process_identity_required`)
  и неизвестный или выведенный агент (`unknown_agent`) — находки проверки,
  поэтому их видит и проверка пакета; затем, для прошедшей проверку версии,
  вид агента и права — как у правила: `422 invalid_process` для вида не
  `agent`/`service`, `403 permission_escalation`. Поэтому
  `process_identity_required` и `unknown_agent` п.14 приходят не кодом ошибки,
  а находками в `details.problems` ответа `422 invalid_process`.
- **Находки** (`severity`): `schema_violation`, `invalid_document`,
  `unresolved_install_variable`, `invalid_data_schema`, `unresolved_data_ref`,
  `invalid_form_schema`; выражения — коды ADR-0075 (`expression_syntax_error`,
  `expression_type_error`, `expression_too_complex`), в том числе «ожидается
  bool / string / timestamp или duration / list» по месту выражения; запись в
  данные — `unknown_data_field`, `data_type_mismatch`; вход скилла —
  `unknown_skill_input`, `skill_input_type_mismatch`, `skill_input_missing`;
  ссылки — `unknown_skill`, `unknown_task_type`, `unknown_agent`,
  `unknown_calendar`, `unknown_event_type`, `unknown_decision_table`,
  `unknown_table_input`, `unknown_compensation_target`,
  `memory_case_undeclared`, `invalid_error_binding`, `invalid_escalation`,
  `invalid_governed_by`; id — `duplicate_element_id`, `element_kind_changed`,
  `invalid_migration`, `unknown_element`; достижимость — `unreachable_step`,
  `unreachable_stage`, `dead_end`; таблицы — `invalid_table_cell`,
  `duplicate_table_input|output`, `unknown_table_output`,
  `table_output_missing`, `table_output_type_mismatch`, `table_overlap`.
  Предупреждения: `process_owner_missing`, `element_removed`,
  `unwritten_data_field`, `unreachable_milestone`, `unused_decision_table`,
  `nothing_to_compensate`, `duplicate_governed_by`, `unknown_process`
  (вложенный процесс ещё не опубликован), `unknown_artifact_type`,
  `unknown_skill` у `retrospective` без явного скилла, `table_gap`,
  `table_rule_unreachable`, `table_check_truncated`; проверка пакета в ядре
  добавляет `governed_by_unknown_document` и `governed_by_unchecked`
  (CP-ADR-0076 п.7).
- **Типы переменных по месту.** `event` — payload события из каталога событий
  для триггера `event:` (`start`, `correlate`, `onEvent`, `listen.any[].on`
  и их блоков), иначе `map(string, dyn)`; `step.result` в `output.as` и
  `export.as` — выход скилла по его схеме, форма шага `human` (иначе
  `fieldSchema` типа задачи), выходы таблицы у `decide` (у `collect` —
  `{items: [выходы строки]}`), `{nodes, edges, truncated}` у `recall`;
  `task.customFields` — `fieldSchema` типа задачи шага. Сверх профиля
  выражения видят `milestone` (`map(string, bool)`, вехи стадий), имя
  ошибки `try.catch[].as` в её обработчиках и `compensated` в блоке
  `onCompensate` (компенсируемый шаг, типизирован по его выходу; уточнение
  P027) — это привязки окружения (`bindings`), не новые переменные профиля.
- **Достижимость.** Шаг после безусловного `complete` или `raise` того же
  блока недостижим. Сторож, который читает только поля данных, которые ничто
  не пишет (`start.set`, `correlate[].set`, `set`, `output.as`, `export.as`),
  или постоянно ложен, никогда не станет истинным: у `exit` — тупик
  (`dead_end`), у `entry` — стадия недостижима, у вехи — предупреждение.
  Стадия достижима без `entry` или если её `entry` читает событие, экземпляр,
  записываемые данные или состояние другой достижимой стадии; взаимно
  ждущие стадии недостижимы обе.
- **Стабильность id.** Id элементов (стадии, шаги, вехи, таймеры, ветви,
  таблицы) уникальны в процессе. Против последней опубликованной версии:
  id не меняет вид (`element_kind_changed`), исчезнувший элемент без карты
  `migrations` из этой версии — предупреждение (экземпляры остаются на своей
  версии; обязательность миграции решает план, п.11). Карта миграции
  называет элементы своих версий, `to` не больше публикуемой версии.
- **Таблицы решений.** Ячейка — `-`, литерал, список `a,b`, диапазон
  `[a..b)` (открытый конец пустой: `[10..)`) или сравнение `<`, `<=`, `>`,
  `>=`; диапазоны — у `number`, `date` (целые дни), `timestamp`. Тип входа без
  `type` — по типу его выражения, иначе по ячейкам. Перекрытие строк у
  `unique` — ошибка; строка `first`, которую покрывают предыдущие, —
  предупреждение; пробел у `first` и `unique` — предупреждение с примером
  входа (вход может быть ограничен раньше по процессу), у `collect` пробела
  нет — пустой список и есть ответ. Поиск идёт по ячейкам, которые различают
  условия строк, с бюджетом 50 000 шагов; сверх — `table_check_truncated`.
  Там же — вычисление таблицы для движка (`evaluate`): `first` без совпадения
  и `unique` без ровно одного — `decision_no_match`, `decision_ambiguous`.
- **Версии и чтение.** Версия процесса workspace требует `processes.write` на
  новом workspace и на workspace последней версии. `GET …/{key}/versions`
  отдаёт `ProcessVersionOut` (без `spec`). `?governedBy=` ищет документ среди
  `governedBy` процесса, стадий, шагов, таблиц и их строк последней версии
  (колонка `governed_by`, индекс GIN). В `elements` события
  `process.definition_published` — стадии, шаги, вехи и таблицы; таймеры и
  ветви `fork` — нет. Объект пакета вида `Process` ставит
  `api/v1/processes.install_process` — та же публикация; `packages:apply`
  (P015) зовёт `publish_process_definition` напрямую.

Уточнение (P007, 2026-09-27). Чистая функция шага —
`domain/process_engine.py`:

- **Определение для движка** — `Definition.build(key, spec, catalog)`:
  проверка P006 (`check_process`), при ошибках — `DefinitionError`; движок
  исполняет только прошедшие проверку версии. Выражения компилируются один
  раз и типизированы так же, как их типизировала проверка (`CheckedProcess`
  отдаёт `programs` по JSON-указателю и разобранные таблицы `tables`). Блоки
  адресуются ключами из id элементов (`stage:<id>/steps`, `step:<id>/try`,
  `branch:<id>`, `timer:<id>`…), поэтому позиция потока называет элементы, а
  не места в файле. Прикладному слою — `start_key(definition, event)` и
  `correlation_keys(definition, event)` для поиска экземпляра по ключу.
- **Вход** — `Input(kind, at, body, actor_id, calendars)`; виды: `start`,
  `event` (событие или наблюдение: `observation` и `source` рядом с полями
  события), `task`, `approval`, `skill`, `child` (вложенный процесс
  закончился), `recall`, `timer`, `intent_failed`, `calendar` (новая версия
  календаря), `command` (`suspend`, `resume`, `cancel`,
  `start_discretionary`). Ответы адресуются `activityId` — детерминированным
  id ожидания, который несёт намерение; ответ на то, чего уже не ждут, —
  решение `ignored` (`stale`). `calendars` — версии календарей, на которых
  вычисляется вход (их пишет журнал, replay подаёт те же). У `approval` —
  `total`: сколько approvals активности открыто или решено (не отменено);
  кворум считается от него.
- **Состояние** — JSON (`jsonb`), от порядка ключей не зависит: данные,
  стадии и вехи, потоки со стеком кадров (последовательность с позицией,
  открытый `try`, `fork`, идущая компенсация), ожидания (`activities`),
  таймеры, завершённые шаги с `onCompensate`, отложенные входы, `seq`.
  Id, которые делает движок, — UUIDv5 от `(instance, seq, index)`.
- **Намерения** — как в п.6, плюс `reassign_task` (эскалация `reassign`) и
  `cancel_child` (область отмены закрыла вложенный процесс); `create_task`
  несёт форму, назначение (выражения вычислены: `agent:<key>`,
  `role:<slug>`, иначе principal), срок, план эскалаций, профиль `context` с
  вычисленными якорями (`{case: true}` — ключ дела) и внешнюю ссылку;
  `request_approvals` — `excludedPrincipals` из `separationOfDuties`;
  каждое событие `process.*` — намерение `emit_event` с общими полями
  экземпляра; `complete` — статус и исход (`completed`, `failed`,
  `cancelled`).
- **Кейс.** Стадия без `entry` входит при старте, с `entry` — когда сторож
  истинен. Стадия с `exit` выходит, когда он истинен, и завершает свою
  открытую работу (отмена задач, approvals, таймеров); без `exit` — когда
  закончились её потоки. `repeatable` стадия с `entry` входит снова на
  следующем входе после выхода, не на том же. Веха достигается один раз, пока
  стадия активна. Необязательная работа запускается командой
  `start_discretionary`. Экземпляр завершается шагом `complete` или сам с
  исходом `completed`, когда все стадии завершены и потоков нет.
- **Блоки.** `listen`: первое подошедшее событие по порядку `any`;
  `step.result` — `{option, event}`, `event` в следующих шагах потока —
  пришедшее событие. Таймаут `listen` и `recall` без `onTimeout` ведёт поток
  дальше без результата (`output` не применяется). `fork` `compete`
  завершается первой закончившейся ветвью, остальные закрываются. `retry`:
  `limit` повторов, пауза `delay`, при `exponential` — удвоение, потолок
  `maxDelay`; затем `catch`. `recall` без `timeout` ждёт 10 минут
  (`DEFAULT_RECALL_TIMEOUT`, ADR-0076 п.4: не бесконечно). Результат задачи
  проверяется по `form` шага (`form_invalid`).
- **Ошибки движка** (RFC 7807, `type`): коды выражений ADR-0075
  (`expression_error`, `expression_cost_exceeded`), `decision_no_match`,
  `decision_ambiguous`, `form_invalid`, `task_cancelled`, `timeout` (408,
  `call.timeout`), `intent_failed`, `child_failed`, код ошибки скилла,
  `step_limit_exceeded` (больше 10 000 действий на один вход). Ошибка
  сторожа стадии или вехи не имеет потока и сразу переводит экземпляр в
  `failed`; ошибка вычисления проекции памяти не останавливает процесс —
  поле проекции пустое, решение `projection_incomplete`.
- **Приостановка.** Таймеры замораживаются (п.8); ответы на работу стадий
  откладываются и подаются после возобновления по порядку; события
  (`correlate`, `onEvent`) исполняются, поэтому `resume` из `onEvent`
  работает.
- **Отмена оператором**: открытая работа закрывается, компенсации
  сделанных шагов идут в обратном порядке, затем `cancelled`. Ошибка
  компенсации (здесь или в шаге `compensate`) — `failed` с
  `attention: compensation_failed`, а не отмена.
- **Разбор дела** (`retrospective`, ADR-0076 п.8): после `completed` —
  `invoke_skill` (вход — по контракту скилла, журнал прикладывает
  исполнитель, `attach`; уточнение P027 и ADR-0076 п.6), затем задача
  `taskType` с предложенными уроками во входе; результат задачи —
  `customFields.lessons: [{key, text, appliesTo: [{kind, key}], evidence,
  decision: confirm | edit | reject, editedText}]`; `remember` — только
  `confirm` и `edit`, узел `lesson` со связями `learned_from` (дело) и
  `applies_to`.
- **Кворум** до P008 считает сам движок (`all`, `any`, `atLeast`,
  `percent`, досрочно при `earlyDecision`); P008 выносит расчёт в
  `domain/approval_quorum.py`, форма входа `approval` не меняется.

Уточнение (P009, 2026-09-27). Экземпляры, таймеры, воркер, API —
`application/commands/process_instances.py`, циклы `process_events` и
`process_timers` воркера, маршруты `/process-instances`:

- **Таблицы** — в той же ревизии фичи `b8e3f1c6d2a9`, что `calendars` и
  `process_definitions`: `process_instances` (уникальность `(tenant_id,
  definition_key, instance_key)`; `state` — состояние движка, `data` — его
  копия для чтения; `refs` — маршрутизация фактов к ожиданиям, п.5),
  `process_timers` (`id` — id таймера движка; `timer_kind` вместо `expr_hash`
  п.8: рецепт срока живёт в состоянии экземпляра; частичный индекс по
  `due_at` ожидающих), `process_instance_events` (первичный ключ `(instance_id,
  seq)`, уникальность `(instance_id, source_ref)`, только добавление —
  триггер). Колонку `approvals.excluded_principals` вводит P008 своей
  ревизией после этой.
- **Шаг** (`take`): вход берётся один раз (`source_ref`: `event:<id>`,
  `timer:<id>`, `command:<uuid>`, `start`); `step` движка; намерения
  исполняются командами ядра от личности процесса (п.14), каждое в
  savepoint; запись журнала (вход целиком, решения, намерения с полем
  `executed` — что из намерения вышло: `{ok, taskId | approvalIds |
  invocationId | instanceId}` или `{ok: false, code}`) и новое состояние —
  в одной транзакции. `executed` — след исполнения, не решение движка:
  replay (P014) его не сравнивает.
- **Отказ команды.** Отказ намерения, открывающего работу (`create_task`,
  `request_approvals`, `invoke_skill`, `start_child`), — следующий вход
  `intent_failed` (`detail` — код отказа) в той же транзакции; не больше 20
  таких входов на один вход. Отказ закрыть работу (`cancel_task` задачи под
  чужим claim, `close_approvals`, `reassign_task`, `cancel_child`) только
  записывается: движок её уже не ждёт.
- **Личность.** Контекст — principal и IAM-привязка агента `spec.identity`
  на момент шага (та же функция, что у правила, ADR-0063 Г1), с проверкой, что
  привязка активна. Нет личности — каждое намерение, которому она нужна,
  отказывает `credential_inactive`. `actorId` событий `process.*` — principal
  агента, `correlationId` всего, что пишет экземпляр, — `process:<id>`.
- **Задача шага** — `create_task` в workspace экземпляра: тип `taskType`
  (у `call.agent` — системный тип), заголовок, описание со входом шага, срок,
  исполнитель — первое разрешимое звено цепочки `assign` (principal,
  `agent:<key>`; `role` — требование роли без исполнителя), `origin: {kind:
  process, ref: process/<instance>/<element>}`, `goalId` не выставляется.
  Внешняя ссылка ADR-0047 — `external_references(system control-plane, type
  process_step, id process/<instance>/<element>)` с `metadata {instanceId,
  activityId}`; повтор элемента (повторная стадия, `retry`) переносит ссылку
  на новую задачу. Отмена — переход в первый статус категории
  `terminal_cancelled`, доступный из текущего, от личности процесса.
- **Согласования** — по approval на согласующего (`assignedPrincipalId` или
  `requiredRoleId` роли по slug — workspace экземпляра прежде tenant'а), без
  задачи, в workspace экземпляра. `sequential` просит следующего после
  голоса, пока ожидание открыто. `total` входа `approval` — число
  согласующих минус отменённые не самим экземпляром. Непустой
  `excludedPrincipals` передаётся в каждый approval шага (п.7, амендмент
  2026-09-30); до него — отказ `not_implemented`, то есть `intent_failed`.
- **Скилл** — `invoke_skill` с ключом идемпотентности
  `process:<instance>:<activity>`. **Вложенный процесс** — явный старт
  последней версии с ключом `<parent>/<activity>` и родителем; его
  `process.completed | failed | cancelled` — вход `child` родителя.
  **`remember`** — наблюдение ядра от личности процесса в той же
  транзакции, **`recall`** — строка очереди `process_recalls`, которую
  воркер исполняет после транзакции шага (ADR-0076 п.4–5, P010).
- **Подача входов** (`process_events`): курсор `processes` создаётся при
  публикации первой версии процесса tenant'а (как у правил — в настоящем).
  Событие журнала — вход экземпляров, чьи `refs` называют его сущность
  (`task.completed`, `task.updated` в категорию `terminal_cancelled`,
  `approval.*`, конец вызова скилла, конец вложенного экземпляра), затем
  `calendar.published` — вход `calendar` экземплярам процессов, которые
  зовут этот календарь, затем `start`/`correlate` последних версий всех
  процессов (событие раньше первой версии процесса и событие чужого
  workspace процесса не доходят). Событие, которое экземпляр записал сам
  (`correlationId = process:<id>`), к нему не возвращается. Отказ
  (определение, которое больше не проходит проверку) записывается в лог и
  пропускается; прочая ошибка откатывает пакет tenant'а, курсор ждёт с
  backoff. `onEvent` и `listen` получают события, которые дошли до
  экземпляра по `correlate`.
- **Таймеры** (`process_timers`): наступившие строки по `due_at`; строка
  экземпляра берётся `FOR UPDATE SKIP LOCKED` (занятый экземпляр ждёт
  следующего цикла), таймер перечитывается; время входа — `due_at`, но не
  раньше часов экземпляра. Взятый движком таймер — `fired`, что бы тот ни
  решил (отложенный при приостановке срабатывает из состояния).
- **Время** события — `occurred_at`, команды — момент команды; время входа
  не меньше часов экземпляра (`state.clock`).
- **Маршруты.** `GET /process-instances` — новые первыми, фильтры п.16,
  видимость по `processes.read` на workspace. `ProcessInstanceOut` —
  стадии, открытые элементы (элемент, вид шага, с какого момента, задача и
  approvals ожидания), ожидающие и замороженные таймеры. `GET …/journal` —
  записи `ProcessJournalEntryOut` по шагам: вход (`kind: input`,
  `eventId`, `actorId`), каждое решение (вид — по решению: `stage`,
  `milestone`, `timer` вместе с эскалацией, `vote`, `recall`,
  `compensation`, `migration`, `error`, прочее — `transition`) и каждое
  намерение (`intent`); курсор — `(seq, номер записи)`. `:suspend`,
  `:resume`, `:cancel` — `409 invalid_process_instance_state`, если статус
  не тот (приостановить можно `running`, возобновить — `suspended`,
  отменить — не закрытый и не отменяемый); `:cancel {compensate: false}` —
  без компенсаций. `actorId` входа-команды — оператор.
- **Вехи** (TAI-ADR-0055): веха следует своему сторожу — достигается, когда
  он истинен, и снимается (`process.milestone_lost`, решение
  `milestone_lost`), когда перестаёт быть истинным, пока стадия активна;
  одна веха меняется не больше раза на вход, поэтому сторож, читающий свою
  же веху, не раскачивается. Это заменяет «веха достигается один раз» из
  уточнения P007.
- **Нейтральность** (п.15): `tests/unit/test_process_neutrality.py` ищет
  слова доменов (закупки, счета и оплата, соседние) в `domain/process_*`,
  `domain/cel_profile.py` и `process_spec.schema.json`.

Уточнение (P013, 2026-09-27). Тесты пакета — `domain/package_source.py`
(разбор файлов), `domain/process_sandbox.py` (песочница и покрытие),
`application/commands/package_test.py` (транзакция, каталог, проверка),
маршрут `POST /packages:test`:

- **Файлы пакета.** YAML 1.2 (`on`, `yes`, `no` — строки) с картой «JSON
  pointer → строка»: находка без своей строки берёт строку ближайшего
  родителя. Конверт `{apiVersion, kind, key, spec}`: ядро проверяет, что
  `apiVersion` называет версию `v1` формата каталога, а само имя каталога —
  дело `cp_packages check`. `package.yaml` — объект `Package`; объекты
  каталога — прочие `*.yaml` вне `tests/`, `schemas/`, `.layout/`; тесты —
  `tests/*.test.yaml`, сверенные со схемой `test.schema.json` (копия ядра —
  `domain/package_test.schema.json`, тест держит её равной закреплённой).
  `data: {$ref: <файл>}` процесса ядро раскрывает само (путь от файла
  процесса, не выходя из пакета; JSON или YAML), как `cp_packages`. Находки
  разбора: `invalid_yaml`, `invalid_document`, `unknown_kind`,
  `duplicate_object`, `unresolved_data_ref`, `invalid_test`,
  `unknown_test_process`, `unknown_test` (фильтр `tests` называет не тест),
  `test_version_mismatch`.
  Пределы разбора (TASK-001231, 2026-09-30): файл, который после раскрытия
  алиасов держит больше 100 000 узлов (или больше 20 млн символов их JSON
  pointer, или больше 4 млн символов строк), вложен глубже 100 уровней,
  держит строку с одиночным суррогатом (`"\ud800"`) или символ, который YAML
  не допускает (`\x00`), — `invalid_yaml` этого файла. Алиасы разрешены,
  но каждый считается столько раз, сколько он раскрывается: YAML-бомба на
  алиасах в несколько сотен байт и длинная строка, повторённая алиасами,
  отвергаются до построения документа. Файл без алиасов (не длиннее 1 млн
  символов по API) предела строк не достигает. Файл, на который ссылается
  `data: {$ref}`, проходит те же пределы (JSON — глубину, узлы, строки и
  суррогаты; алиасов в нём нет), а отказ — `unresolved_data_ref` процесса.
  Суррогатная пара, записанная в YAML двумя `\u`-escape, — тоже одиночные
  суррогаты (PyYAML их не склеивает); символ вне BMP пишется сам или как
  `\U0001F600`.
  Кроме пределов файла у всего разбора пакета (один вызов `parse_package`)
  общий бюджет: 200 000 узлов, 20 млн символов pointer, 4 млн символов строк
  и 4 млн символов прочитанного текста. Файл, на который ссылается `$ref`,
  расходует бюджет при каждом раскрытии. Бюджет расходует и файл, отвергнутый
  по пределу файла. Бомба, разнесённая на много файлов (300 файлов по 250 байт
  — это 22 млн узлов), исчерпывает бюджет. Файл, на котором бюджет превышен,
  и все следующие за ним (по порядку путей) получают `invalid_yaml` (или
  `unresolved_data_ref` для `$ref`) с сообщением о бюджете пакета. Самый
  крупный пакет суперпроекта (`tenders`: 13,8 тыс. узлов, 103 тыс. символов
  строк) остаётся в пределах бюджета с десятикратным запасом. Разбор идёт в
  пуле потоков (`asyncio.to_thread`) и не держит event loop API.
  Значения пакета — только значения JSON. Теги, из которых выходит не JSON
  (`!!binary`, `!!timestamp`, `!!set`, `!!omap`, `!!pairs`), и `.nan`/`.inf`
  (в JSON-файле — `NaN`/`Infinity`) дают `invalid_yaml`. Неявных дат в
  YAML 1.2 нет: `2026-09-30` читается как строка, в той же форме, в какой
  дату принимают вход таблицы решений и JSON спецификации.
  Числа (TASK-001247, 2026-10-01). Целые — по ядру YAML 1.2: десятичные
  (`[-+]?[0-9]+`), `0o…` и `0x…`. Шестидесятеричные `1:59:59` из YAML 1.1
  читаются как строка (мегабайт такого значения раньше стоил `int()` 27 с
  на запрос), `0b101`, `1_000` и `-0x1F` — тоже строки, а `012` — двенадцать,
  а не восьмеричные десять. Целое длиннее 1000 цифр (без знака и префикса) —
  `invalid_yaml` в строке значения ещё до `int()`; в JSON под `$ref` —
  `unresolved_data_ref`. Каждый float проверяется на конечность при
  построении документа: `1.0e+999`, `!!float nan`, `!!float inf`,
  шестидесятеричный float за пределом float — `invalid_yaml`, а `1e999` в
  JSON под `$ref` — `unresolved_data_ref`. Явный тег, который не читает своё
  значение (`!!int abc`, `!!int 1:30`, `!!float ''`, `!!bool maybe`,
  `!!bool yes`), — тоже `invalid_yaml` в строке значения, а не 500.
  Неявный float остаётся по YAML 1.1 (`1:30.5` — 90.5, `1e3` — строка).
  YAML-файлы фикстур пакетов читаются как раньше.
- **Определения в памяти.** Каждый процесс пакета проверяется (п.2) против
  каталога tenant'а, поверх которого лежат объекты самого пакета: его
  `TaskType` (`fieldSchema`), `Skill` (`inputSchema`/`outputSchema` или
  `contract.inputs`/`outputs`), `Agent`, `ArtifactType`, `Calendar`,
  `Process` известны его процессам до применения. Прошлая версия для
  стабильности id — последняя опубликованная ниже версии пакета.
  `workspaceId: ${…}` (переменная установки, `cp_packages test` её не
  подставляет) в тесте — workspace запроса или его отсутствие. `Calendar`
  проверяется как `POST /calendars` (`invalid_calendar` и коды п.9). Прочие
  виды здесь не сверяются по форме — это дело `cp_packages check` и плана.
- **`governedBy`** — единственный исходящий вызов маршрута: после закрытия
  транзакции ядро спрашивает память о документах (ADR-0076 п.7),
  предупреждения `governed_by_unknown_document` / `governed_by_unchecked`
  идут в `problems` в обоих режимах. Песочница клиентов не имеет: страж-тест
  проверяет, что `domain/process_sandbox.py` импортирует только домен.
- **Только чтение.** Первая команда транзакции — `SET TRANSACTION READ ONLY`;
  на сессию на время прогона вешается счётчик записей: оператор `INSERT`,
  `UPDATE`, `DELETE`; сырой SQL (`text(...)`), чьё первое ключевое слово —
  `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `COPY`, `TRUNCATE`, или `WITH`, в
  теле которого есть такой оператор (кроме блокировки `FOR [NO KEY] UPDATE`);
  объект, который flush вставил бы, изменил или удалил. Чтение — в том числе
  сырое (`text("SELECT …")`, `WITH RECURSIVE … SELECT`, например предки
  рабочего пространства для `governedBy`) — не запись. Тесты идут внутри
  транзакции; `expect: {noSideEffects: true}` — этот счётчик равен нулю.
  Интеграционный тест сверяет все таблицы базы построчно до и после прогона.
- **Песочница.** Тот же `process_engine.step`, состояние между входами
  проходит через JSON, как `jsonb`. Намерения:
  `create_task` — задача песочницы, исполнитель — первое известное звено
  цепочки (principal теста, `agent:<key>` активного или пакетного агента,
  `role:<slug>` роли каталога, пакета или `given.principals`), иначе
  `intent_failed` (`unknown_role`, `unknown_agent`); `request_approvals` —
  approval на согласующего (`sequential` — следующий после голоса);
  `invoke_skill` — скилл каталога или пакета, вход по его `inputSchema`
  (иначе `intent_failed invalid_skill_inputs`), ответ — заглушка
  `mocks.skills["name@version"]`: `output` сверяется с `outputSchema` —
  **не по схеме — провал теста**, `error` — ответ `failed` с `{code: type,
  status, message: detail}`, `timeout` — ответа нет; `recall` —
  `mocks.recall` (`process_replay.mock_recall`, ADR-0076: ответ не в форме
  ответа памяти — провал теста, `timeout` и `error` — таймаут шага с
  причиной `timeout` или `error.type`, без заглушки — ответа нет, шаг ждёт
  своего таймаута); `call.agent` — `mocks.agents[key]` завершает задачу агента;
  `remember` и события `process.*` — записи для `expect`; `start_child` —
  вложенный экземпляр в той же песочнице (процесс пакета или последняя
  версия каталога), его исход — вход `child` родителя. Вызов без заглушки
  остаётся без ответа, как скилл, который ещё не ответил. Ответ заглушки
  приходит следующим входом, после текущего. Выбор ответа: подходящие по
  `step` и `when` (CEL над `input` вызова) — по порядку вызовов, после
  последнего повторяется последний.
- **Шаги теста.** `emit` — событие (`observation.recorded` с `observation`
  и `source` или тип `event`) идёт как в живом цикле: старт или повтор
  старта процесса теста, затем `correlate` открытых экземпляров; `by` —
  автор события, его `actorId` (З8, TASK-001235);
  `advance` — сдвиг на длительность фиксированной длины или `until:<id или
  элемент таймера>`: ожидающие таймеры срабатывают по порядку `dueAt`, каждый
  в свой момент; `complete` — открытая задача шага (`by` — исполнитель или
  держатель роли по `given.principals`; `output` сверяется с `fieldSchema`
  типа задачи, форма шага — движком, `form_invalid`; `cancel: true` — отмена);
  `approve` — ожидающий approval шага: исключённый principal —
  `separation_of_duties_violation`, не согласующий — `not_eligible`
  (`expectRefused` ждёт именно этот код, неожиданный отказ останавливает
  тест); `expect` — стадии (`open`, `completed`, `not_started`, `skipped` —
  не начатая у закрытого экземпляра), вехи, задачи (`assignee` — исполнитель
  или держатель его роли), таймеры, данные (путь `a.b` или `/a/b`), события
  с прошлого `expect` (события движка и события шагов `process.step_*`,
  спроецированные как в `take()`, §13), `memory.recalled` / `remembered`
  (частичное совпадение), `status`, `outcome`, `error` (тип ошибки),
  `noSideEffects`, `sla` — состояние срока шага или процесса (CP-ADR-0078 §7).
  Невыполнимый шаг останавливает тест; несбывшееся ожидание — провал шага,
  тест идёт дальше. Время без `given.clock` — `2026-01-05T09:00:00Z`, чтобы
  тест давал один ответ. `given.data` — явный старт с ключом `test`;
  `given.calendar` подменяет календарь процесса; `given.stage` движок не
  умеет (провал теста), `given.fromInstance` — пробный прогон P014.
  Ошибка движка — тест `error`.
- **Покрытие** — по всем тестам процесса вместе, у каждого счётчика —
  `missing`: элементы — стадии (вход), шаги (исполнение; `do` и `try` — когда
  исполнился шаг внутри), вехи, таймеры стадий и процесса; переходы —
  `<стадия>:entry`, `<стадия>:exit`, `<шаг>:when` / `<шаг>:skip`,
  `<шаг>:any/<i>` и `<шаг>:timeout` у `listen`, `<шаг>:answered` /
  `<шаг>:timeout` у `recall`, `<шаг>:approved` / `<шаг>:rejected`, ветви
  `fork`, `correlate/<i>`, `onEvent/<i>`; строки таблиц — `<таблица>/<строка>`;
  обработчики — `<шаг>/catch/<i>`, `<шаг>/retry`, `<шаг>/onTimeout`,
  `<шаг>/onCompensate`, `<шаг>/escalations/<i>`, `<шаг>/onDue`.
  `coverage.minimum` теста — порог доли элементов процесса этим тестом.
- **Ответ** — `200` всегда, когда пакет удалось принять телом: `status
  invalid` (есть находка-ошибка, тесты не идут), `failed`, `passed`;
  `checkOnly` — только проверка, `tests` и `coverage` пусты. `workspaceId`
  запроса требует ещё `processes.read` на него; его роли читаются вместе с
  ролями tenant'а.
- **PyYAML** становится зависимостью ядра (раньше приходил транзитивно через
  `uvicorn[standard]`).

Уточнение (P014, 2026-09-27). Replay и пробный прогон —
`application/commands/process_replays.py` поверх `domain/process_replay.py`,
пробный прогон — `Sandbox.start_from` в `domain/process_sandbox.py`.

- **Кандидат** проверяется, как при публикации (п.2): каталог tenant'а,
  прошлая версия для стабильности id — последняя опубликованная ниже версии
  кандидата. Находки — `problems` ответа; с ошибкой экземпляры не
  прогоняются (`replayed: 0`), ответ всё равно `200`, как у `packages:test`.
  Нечитаемый `spec` — находка `invalid_document`. Процесса с ключом нет —
  `404`.
- **Экземпляры.** `instanceIds` — ровно эти (повторы схлопываются), каждый
  читается как `GET /process-instances/{id}`: без `processes.read` на его
  workspace — `403`, неизвестный id или экземпляр другого процесса — `404`. Без
  `instanceIds` — последние `limit` (по умолчанию 50, не больше 200)
  экземпляров текущей (последней) версии по `started_at`, только из
  workspace'ов, где у вызывающего `processes.read`. Закрытые экземпляры
  участвуют наравне с открытыми.
- **Номер версии — не поведение.** Движок пишет номер версии в состояние и
  в каждое событие `process.*`; кандидат под своим номером расходился бы с
  журналом на каждом `emit_event`. Поэтому кандидат идёт под номером версии
  экземпляра (`process_replay.as_version`): проверка и программы — его,
  номер — экземпляра. Выражение, читающее `instance.version`, видит номер
  экземпляра.
- **Сравнение.** Входы журнала по `seq`, календари — версий, записанных у
  входа; решения и намерения шага (намерения без `executed`) против
  записанных. Прогон экземпляра останавливается на первом расходящемся
  шаге: дальше пути разошлись, и каждое следующее сравнение шло бы с другой
  историей. Если все шаги совпали, итоговое состояние сверяется с
  сохранённым: `set`, вычисливший другое значение, своего решения не
  пишет — его видно только в данных.
- **Расхождение** — у экземпляра не больше одного, первое: `journalSeq`
  (запись журнала; первая — старт, `0`), `kind` — `decision` или `intent`
  (первое отличающееся из списка шага, с его `element`), `input` (кандидат
  отказал записанному входу, `replayed` — текст отказа), `data`, `timer`,
  `state` (шаги совпали, отличается итоговое состояние; `journalSeq` —
  последняя запись), `recorded` и `replayed` — отличающиеся элементы.
  Изменённая строка таблицы решений даёт расхождение `decision` с
  `element` шага `decide` ровно у тех экземпляров, чьи входы она решает
  иначе (SC-005).
- **Ничего не пишется**: транзакция `READ ONLY`; память не зовётся — ответы
  `recall` — входы журнала (ADR-0076 п.4).
- **Пробный прогон** — `given.fromInstance: <id экземпляра>` теста
  `packages:test`. Экземпляр читается в той же транзакции только на чтение,
  как `GET /process-instances/{id}` (без `processes.read` на его workspace
  — `403`, неизвестный или не-UUID id — `404` всего запроса), вместе с его ожидающими
  approvals (согласующий — principal или `role:<slug>`, исключённые),
  оставшимися согласующими последовательного шага и их числом. Песочница
  продолжает **копию** его состояния на версии процесса из пакета (экземпляр
  другого процесса — провал теста): открытые задачи шагов (`human` и
  `call.agent`) становятся задачами песочницы с исполнителем по цепочке
  назначения, ожидающие approvals — её approvals; вызов скилла, `recall` и
  вложенный процесс, которых экземпляр ждёт, остаются без ответа, как вызов
  без заглушки (их таймауты — таймеры состояния). Часы — `given.clock` или
  время последнего входа экземпляра. `given.data` и `given.stage` с
  `fromInstance` несовместимы (провал теста). Живой экземпляр не меняется.
  Состояние, которое версия пакета не может продолжить (элемент исчез),
  — ошибка движка, тест `error`; перенос по карте — `migrations` P015.

Уточнение (P015, 2026-09-28). План и применение —
`application/commands/package_plan.py` поверх `domain/package_plan.py`
(поля, владельцы, переименования, хэши) и `domain/process_migration.py`
(перенос экземпляра по карте), маршруты `POST /packages:plan` и
`POST /packages:apply`:

- **Виды.** Ядро планирует и применяет `Calendar` и `Process` (в этом порядке:
  процесс называет календарь); прочие виды пакета ставит установщик
  (`cp_packages`, `PLAN_KINDS`), в `changes` их нет (*заменено амендментом
  2026-09-29 ниже: ядро планирует и типы задач, агентов, правила*). Пакет без `package.yaml`
  — ошибка плана `package_manifest_missing`: применение записывает объекты под
  ключом пакета. `workspaceId` запроса подставляется в `spec.workspaceId`
  вида `${…}` (иначе `unresolved_install_variable`) и требует
  `processes.read` на него.
- **Поле** — член верхнего уровня `spec` (`/spec/stages`,
  `/spec/displayName`…). Что последнее применение хотело от объекта, хранит
  таблица `package_objects` (ревизия `d7f2a9c4e1b8`): `(tenant, kind, key)`,
  ключ пакета, `spec` пакета, его хэш, версия, `planHash`, кто и когда
  применил, `retired_at` (заменено амендментом 2026-09-29 package-sdk, Ж1:
  признак вывода — таблица `catalog_retirements`, колонка удаляется).
  Поле, чьё значение в последней версии отличается от
  хотевшегося, правил человек после применения: владелец `console`, такое поле
  не перетирается (`applies: false`) — публикуемая версия берёт его последнее
  значение, — пока план не построен с `overwriteConsole: true`. Запись хранит
  то, что хотел пакет, а не опубликованное: сохранённое поле консоли остаётся
  полем консоли и в следующем плане; человек вернул значение пакета — поле
  снова пакета. `version` процесса — номер, не поле человека. Объект, который
  пакет ещё не применял, — все поля пакета. Флаг — часть плана (входит в
  хэш): `apply` передаёт тот же флаг, с другим — `plan_stale`.
- **Действия.** `create` — ключа нет; `update` — публикуемая спецификация
  отличается от последней версии; `unchanged` — совпадает (с учётом
  сохранённых полей консоли), а у выведенного процесса или календаря это
  `restore` (амендмент package-sdk, Ж3); `rename` — ниже. Процесс `update`/`rename` с
  номером версии не выше последней — ошибка `process_version_conflict` в
  плане. Каждый публикуемый процесс проходит проверку п.2 (как
  `packages:test`, поверх каталога — объекты пакета); ошибки — в `problems`.
  `retire` в плане ядра не возникает: ключ, переименованный прочь, выводится
  вместе с `rename`.
- **Переименование** `renames: [{kind, from, to}]` видов ядра: `to` — объект
  пакета, `from` — нет, ключ переименовывается один раз (иначе
  `invalid_rename`). Действует, когда `from` есть в каталоге и не выведен, а
  `to` нет: объект `to` публикуется версией пакета, его прошлая версия для
  стабильности id и сторона `from` миграций — версии старого ключа
  (`publish_process_definition(renamed_from=…)`), номер — выше последнего
  номера старого ключа; запись владения переезжает с объектом. Старый ключ
  выводится (`package_objects.retired_at`; по амендменту 2026-09-29
  package-sdk, Ж1 — строка `catalog_retirements`): выведенный процесс не заводит
  новых экземпляров ни событием старта, ни `POST /process-instances`
  (`409 process_retired`), а его открытые экземпляры — если миграция их не
  перенесла — идут дальше: старт-событие существующего ключа и `correlate` до
  них доходят. Публикация версии выведенного ключа в обход пакета возвращает
  его. Обе стороны в каталоге и не выведены — ошибка `rename_target_exists`;
  переименование уже применено (старый выведен или его нет) — план о нём
  молчит, повтор того же пакета — `unchanged`.
- **Экземпляры.** У процесса `update`/`rename` открытые экземпляры (`running`,
  `suspended`) ключа, который он продолжает, группируются по версиям. Миграция
  версии — та из `migrations` публикуемой спецификации, у которой `from` —
  версия группы, а `to` — публикуемая (прочие — история прошлых версий):
  `pin` — остаются, `migrate` — переносятся; без миграции — `unaffected`,
  остаются на своей версии. Экземпляр **стоит** на элементе
  (`process_migration.standing`): активная стадия, элемент активности
  (задачи, голосования, вызова, ожидания), блок и позиция потока, `try` и
  `fork` со своими ветвями, шаги незавершённой компенсации, элемент и стадия
  ждущего таймера, шаг с `onCompensate`, чья компенсация ещё не исполнена.
  `migrationRequired` — у `migrate` перенос хоть одного экземпляра не
  удаётся, у `unaffected` — хоть один стоит на элементе, которого после
  карты нет в новой версии или который сменил вид (вид шага — его вид шага);
  с этим план несёт ошибку `migration_required` (файл и строка процесса, путь
  `/spec/migrations/<i>` или `/spec`, подсказка с формой миграции), а
  `apply` отказывает `422 migration_required`. `pin` не проверяется.
- **Перенос по карте** (`migrate_state`, чистая функция): каждый id —
  по карте (не названный остаётся), позиция потока — «после того же
  элемента» в блоке с переименованным именем, а не индекс списка (вставленный
  до шага элемент не исполняется); указатели выражений таймеров переезжают со
  своим элементом (`/spec/stages/0/steps/1/human/due/at` →
  `/spec/stages/0/steps/2/…`); новые стадии — `available`, вехи — только
  известные новой версии. Не переносится — `MigrationError` с кодом
  (`migration_required`, `element_moved` — элемент ушёл из блока,
  `block_gone`, `expression_gone`, `element_gone`) и элементами. Следующий вход
  — обычный шаг новой версии: движок миграций не знает.
- **Применение** одной транзакцией: блокировка применений tenant'а
  (`pg_advisory_xact_lock`), записей владения и открытых экземпляров (`FOR
  UPDATE`), план строится заново и сравнивается по `planHash`: другой —
  `409 plan_stale` (`details.planHash`, `currentPlanHash`, `catalogEtag`);
  затем `migration_required` — `422 migration_required`, прочие ошибки —
  `422 invalid_package` (оба с `details.problems`). Публикация — обычными
  командами (`publish_calendar`, `publish_process_definition`) под правом
  вида применяющего; перенос экземпляра: `definition_id`, `definition_key`,
  `definition_version`, состояние, элементы его таймеров
  (`process_timers.element`), ожидающих `recall` и ссылок `refs`; запись
  журнала `kind: migrate` (`source_ref` `migration:<id версии>`, вход
  `{fromKey, fromVersion, toVersion, policy, map, planHash, state}` — с
  перенесённым состоянием, решение `migrated`) и событие `process.migrated`
  (`actorId` — применивший: перенос решил он, не процесс; корреляция
  `process:<id экземпляра>`). Внешние ссылки открытых задач
  (`process/<экземпляр>/<элемент>`) не переписываются — это история. Ответ —
  `applied[kind, key, action, version]` и `catalogEtag` после применения.
  Маршрут идёт через ключ идемпотентности, как прочие записи.
- **Replay после миграции.** Записи журнала до переноса исполнялись другой
  версией: `process_replay.replay` начинает с состояния последней записи
  `migrate`. Так replay экземпляра на своей версии и `:replay` кандидата
  сравнивают только то, что шло на текущей версии. Кандидат переименованного
  процесса в `behaviour` плана идёт под старым ключом (ключ — не поведение,
  как номер версии).
- **Хэши.** `catalogEtag` — `sha256` канонического списка `{kind, key,
  version, hash, applied, retired}` объектов пакета и переименованных прочь
  ключей: последняя версия, хэш записи владения, выведен ли. `planHash` —
  `sha256` канонического `{package: хэш файлов, catalogEtag, changes,
  processes, overwriteConsole}`, где от `processes` берутся номера версий и
  по группам экземпляров `version`, `fate`, `migrationRequired`: число
  открытых экземпляров и `behaviour` (выборка replay) — отчёт, их меняет
  каждый вход живого экземпляра, а применение от них не зависит. Появилась
  или исчезла группа, судьба или нужда в миграции — `plan_stale`.
- **Покрытие регламентов** (FR-058) — только в плане, после закрытия
  транзакции, как `governedBy` у `packages:test`. Разделы документа — узлы,
  указывающие на него связью `section_of` (онтология `process-knowledge`,
  P019): один типизированный запрос на документ (якорь и `traverse
  [{relation: section_of, direction: in, depth: 1, limit: 200}]`, без поиска
  по смыслу) в namespace процесса. Имя раздела — `attributes.section` узла,
  иначе его ключ без ключа документа и разделителя (`doc#4.2` → `4.2`).
  `regulationCoverage[]`: `found` — документ разрешён точно, `covered` —
  раздел → элементы (`<процесс>/<id элемента>`, у ссылки процесса в целом —
  ключ процесса; ссылки без `section` раздел не покрывают), `uncovered` —
  разделы памяти без элементов. Ссылка на раздел, которого у документа в
  памяти нет, — предупреждение `governed_by_unknown_section`. Память не
  настроена или не ответила — покрытие пусто и одно предупреждение
  `governed_by_unchecked`; план от памяти не зависит.

Амендмент 2026-09-29 (TASK-000903; решение владельца по вопросу R012,
TASK-000824). План и применение по хэшу охватывают все виды каталога, которые
держит ядро: консоль показывает и применяет пакет целиком, а не только
процессы и календари. Механизм прежний — изменения по объектам и полям с
владельцем поля, находки, `catalogEtag`, `planHash`, `409 plan_stale`,
`overwriteConsole`; добавлены виды и то, как каждый сравнивается и
публикуется (`application/commands/package_catalog.py`):

- **Виды и порядок.** `PLANNED_KINDS = (TaskType, Agent, Calendar, Process,
  WorkRule)` — порядок установщика: агент называет типы задач, процесс —
  типы задач, агента и календари, правило — типы задач и действует как агент.
  В этом порядке идут `changes`, `applied` и публикация. `PlanChangeOut.kind`
  — перечисление этих видов.
- **Форма объекта.** Поле — член верхнего уровня формы: того, что каталог
  держит об объекте, в именах пакета. Формы повторяют то, что сравнивает
  `cp_packages apply --install`, поэтому ключ, поставленный установщиком тем
  же файлом, план показывает `unchanged` (приёмка: оба пути дают один
  каталог).
  - `TaskType` — поля `POST /task-types` (`displayName`, `description`,
    `fieldSchema`, `lifecycleSchema`, `execution`, `approvalSchema`,
    `contextSchema`, `instructions`, `completionSchema`, `artifactSchema`,
    `acceptance`), нормализованные, как их хранит ядро. Последняя версия —
    старшая **активная**. Поля, которые установщик сравнивает, только если
    они есть в файле (`lifecycleSchema`, `contextSchema`, `instructions`,
    `completionSchema`, `artifactSchema`, `acceptance`), без них в файле
    берут значение последней версии — и при сравнении, и в публикуемой
    версии. Это осознанное расхождение с установщиком: `apply --install`,
    заводя новую версию из-за другого поля, шлёт только файл, и такие поля
    новой версии получают умолчания ядра (инструкции, критерии приёмки
    последней версии теряются). План их переносит: поле, которого пакет не
    задаёт, — не его, и молча стирать заданное в консоли он не должен. На
    `unchanged` расхождение не влияет (оба пути сравнивают одинаково), а
    новая версия по плану несёт эти поля от прошлой, по установщику — нет;
    после перевода установщика на plan → apply путь один. Версия неизменяема: `update` —
    следующая версия (номер — старший номер ключа + 1); все прочие активные
    версии ключа выводятся — новое поле изменения `deprecates: [версии]`,
    входит в хэш; у `unchanged` в нём — активные версии, кроме последней
    (так делает установщик).
  - `Agent` — тело ревизии (каноническое, CP-ADR-0073 §2) плюс желаемое
    состояние: `state` и `placement.replicas`. Изменилось тело — новая
    ревизия, только состояние — ревизия прежняя (`version` в плане и
    `applied` — номер ревизии). Выведенный ключ — ошибка плана
    `agent_retired`: пакет его не возвращает.
  - `WorkRule` — `description`, `trigger`, `condition`, `interpretation`,
    `action` (нормализованные, как хранит ядро), `identity`, `status`,
    `workspaceId` живого (не архивного) правила ключа. Первые шесть —
    `PATCH` (новая версия правила), `status` — `:enable`/`:disable` без
    новой версии (`version` в плане и `applied` у изменения только статуса —
    прежний номер, как у `set_rule_status`); другой `workspaceId` — ошибка `rule_workspace_immutable`
    (правило выводится в установке и заводится заново). `workspaceId: ${…}`
    подставляется из `workspaceId` запроса, как у процесса; без него —
    `unresolved_install_variable`.
- **Форма запроса.** Файл проверяется моделью запроса маршрута своего вида
  (`TaskTypeCreateRequest`, `AgentPublishRequest`, `RuleCreateRequest`; у
  правила без `goalId`) — находки `invalid_task_type`, `invalid_agent`,
  `invalid_rule` с путём в файле.
- **Проба команд в плане.** То, на что объект ссылается (скиллы, типы
  задач, агенты, роли, права, которые пишущий одалживает агенту), проверяет
  команда вида. План больше не `READ ONLY`: он строится в транзакции,
  которую всегда откатывает, и прогоняет команды типов задач, агентов и
  правил на том, что опубликовало бы применение, — каждую в своей точке
  сохранения, в порядке применения (правило видит тип задачи и агента из
  того же пакета). Отказ команды — находка объекта (код и сообщение
  команды, путь из `details`). Нет права вида — предупреждение
  `permission_required` на объекте, и проба останавливается: применению
  право нужно всё равно, а дальше пошли бы ложные находки. Объект со своей
  ошибкой плана не пробуется. Ничего из пробы не остаётся (тест: план не
  создаёт тип задачи).
- **Применение** — одна транзакция, как прежде: план заново под
  блокировкой, сравнение хэша (`409 plan_stale` для изменения любого вида:
  новая версия типа задачи, ревизия или состояние агента, версия или статус
  правила — всё это в `catalogEtag` через номер и хэш формы), ошибки плана —
  `422 invalid_package`. До построения плана применение берёт блокировки,
  которые берут команды видов: ключа типа задачи, агента и правила
  (advisory-блокировки `task_types`/`agents`/`work_rules`) и строки их
  объектов: агента — `FOR UPDATE`, типа задачи и правила — `FOR NO KEY
  UPDATE`. На строки типа задачи и правила ссылаются внешние ключи задач и
  оценок правил, их вставка берёт `FOR KEY SHARE`; движок процессов
  вставляет задачу, держа экземпляр, который применение блокирует следом, —
  `FOR UPDATE` на типе дал бы взаимоблокировку (тест: экземпляр под
  блокировкой и вставка задачи того же типа при ждущем применении).
  Порядок один у применения и у пробы плана: виды — в порядке
  `PLANNED_KINDS`, ключи внутри вида — по возрастанию; проба берёт все ключи
  до первой команды (отпущенная точка сохранения блокировки не отпускает,
  и по одной на команду они пришли бы в другом порядке). Публикация того же ключа, открытая в момент
  применения, либо фиксируется до плана (и хэш устарел — `plan_stale`), либо
  ждёт применения; между планом и командой она не встаёт (иначе применение
  вывело бы виденные версии типа и оставило чужую активной рядом со своей;
  тест `tests/concurrency/test_package_apply_races.py`). Затем каждый объект публикуется обычной командой
  своего вида под его правом (`task_types.manage`, `agents.manage`,
  `calendars.write`, `processes.write`, `rules.write`). Отказ команды при
  применении — её ошибка (`403`, `422 …`), и откатывается всё применение:
  частично применённого пакета не бывает. Ревизия агента записывает
  источник `package` с ключом и версией пакета из `package.yaml` (CP-ADR-0073,
  история ревизий) — как у установщика, а не `manual`. IAM-личности, чью привязку
  изменила ревизия агента, маршрут сбрасывает из кэша после фиксации.
- **Владение полями** — та же таблица `package_objects`: её `kind` допускает
  новые виды (ревизия `b5d1e7a3c9f4` амендмента TASK-000904, Е4); `spec` —
  форма, которую хотел пакет. Строка без `spec` (связь, записанная
  установщиком через `packages:record`, Е6) — как строки нет: все поля
  `package`.
  Поле, изменённое в консоли (новая версия типа задачи, `PATCH` правила,
  `:disable`, ревизия или состояние агента), — `console` и сохраняется без
  `overwriteConsole`.
- **Переименования** по-прежнему только у `Calendar` и `Process`: версии
  других видов не переезжают на новый ключ. `renames` другого
  планируемого вида — предупреждение `rename_not_planned` (старый ключ
  остаётся, новый применяется сам по себе).
- **Граница.** Остальные виды пакета план не применяет и перечисляет в новом
  поле `outside: [{kind, key, appliedBy}]`: `NotificationRule` —
  `notification-service` (правила уведомлений живут в сервисе уведомлений,
  ADR-0005 notification-service, у ядра их нет), `WorkspaceType`, `Role`,
  `Capability`, `Skill`, `ArtifactType`, `ProjectTemplate` — `installer`
  (`cp_packages apply`). Онтологии и доменные пакеты знаний — не объекты
  каталога пакета (ADR-0060): плана у них нет. Вывод ключей из оборота
  (`Installation.retire`) — тоже установщика: это объект установки, а не
  пакета.
- **Установщик.** Перевод `tools/cp_packages.py` на один путь plan → apply
  для этих видов (`PLAN_KINDS` = виды ядра, `apply --install` — через
  `packages:plan`/`packages:apply`, остальное — как прежде) — отдельная
  задача суперпроекта после вливания этой ветки. До перевода порядок
  такой: `cp_packages apply --install` ставит пакет (включая виды вне
  ядра), затем `packages:plan` того же пакета показывает `unchanged` и
  дальше пакет может применяться через консоль; записи владения
  (`package_objects`) у ключей, поставленных только установщиком, нет — все
  их поля считаются полями пакета до первого `packages:apply`. Применять
  один пакет попеременно обоими путями не следует: новая версия типа задачи
  у них различается полями, которых нет в файле (см. выше).

Уточнение (TASK-000969, 2026-09-29; ревью TASK-000903):

- **План берёт вызывающего первым.** Проба команд в плане — пишущая
  транзакция, хотя и откатываемая, поэтому `packages:plan` первым оператором
  берёт principal вызывающего (CP-ADR-0077 п.3, правило 1): `:disable`,
  зафиксированный, пока план ждал, — `403 principal_not_active`.
- **Объект другого пакета.** Применение пишет связь каждого объекта плана со
  своим пакетом (Е2) и так переводит объект, поставленный другим пакетом, на
  себя. План говорит об этом предупреждением `package_owner_changed` на
  файле объекта (`«вид/ключ» belongs to package A: the apply moves it to
  package B`); отказа нет — переименованный пакет законно забирает свои
  объекты. Пакет-владелец входит в `catalogEtag` (поле `package` записи
  каждого объекта): смена владельца между планом и применением — `409
  plan_stale`.
- **`packages:record` и `packages:apply` не блокируют друг друга.**
  Применение держит строки `package_objects` своего плана `FOR UPDATE`
  (одним запросом, по `kind, key`) и вставляет недостающие в конце; запись
  установщика апсертит те же строки по одной в порядке видов каталога.
  Встретившись, они ждали бы друг друга (`40P01`, `500`). Поэтому
  `packages:record` до первой строки берёт ту же advisory-блокировку
  применений tenant'а (`package_links.lock_applies`) и ждёт применения
  целиком (тест `tests/concurrency/test_package_apply_races.py`).

Уточнение (P027, 2026-09-28). Компенсация и разбор дела — ошибки, которые
нашёл пакет `tenders` (P023) в песочнице:

- **`step` в `onCompensate`** — текущий шаг блока, как в любом блоке: после
  шага блока `step` — его результат, и `output.as` шага компенсации читает
  свой результат (`step.result`). Компенсируемый шаг — отдельная привязка
  **`compensated`** (`{id, status, result}`), видна во всём блоке
  `onCompensate`; проверка типизирует её по выходу компенсируемого шага
  (скилл, форма, таблица, `recall` — как `step.result` в его `output.as`), вне
  блока её нет (`expression_type_error`). `try.catch[].as: compensated`
  внутри `onCompensate` — `invalid_error_binding`. Кадр компенсации несёт
  `bindings: {compensated}`; кадр прежнего движка (`stepVar`) читается как
  `compensated`, поэтому экземпляр посреди компенсации доходит её по новой
  семантике.
- **Разбор дела** — вход `process.retrospective@1` по его контракту
  (`packages/process-knowledge/skills/process.retrospective.yaml`
  суперпроекта): `case` — узел дела `{kind, key, title}` из проекции памяти,
  `definitionKey`, `version`, `instanceId`, `outcome`, `data`, `entities`
  проекции (до 200), `appliesToKinds` — `retrospective.appliesTo` процесса;
  `journal` прикладывает исполнитель (`attach: [journal]`). Процесс без ключа
  дела разбор не заводит: `retrospective_skipped` с `error.type:
  case_unknown`. Подробности — ADR-0076 п.6.

### 17. MCP-инструменты автора процессов

> Амендмент К (2026-09-30, S025): `cp_pkg_check`, `cp_pkg_test`,
> `cp_pkg_plan` и `cp_pkg_apply` удалены из MCP-сервера оператора — автор
> пакетов работает через `package-sdk mcp`. Строки о них ниже — история;
> действуют `cp_process_get`, `cp_process_explain` и форма ошибок.

Уточнение (P016, 2026-09-27; FR-031, FR-033, FR-034). MCP-сервер
оператора (`control_plane_mcp/server.py`) даёт автору процесса шесть
инструментов — тонкие адаптеры маршрутов ядра; правил и состояния у них
нет, ядро проверяет права и инварианты само:

| Инструмент | Маршрут | Пишет |
|---|---|---|
| `cp_pkg_check(path)` | `POST /packages:test?checkOnly=true` → `{status, problems}` | нет |
| `cp_pkg_test(path, tests?)` | `POST /packages:test` (`PackageTestOut`) | нет |
| `cp_pkg_plan(path, workspaceId?, replayLimit?, overwriteConsole?)` | `POST /packages:plan` (`PackagePlanOut` с `planHash`) | нет |
| `cp_pkg_apply(path, planHash, …)` | `POST /packages:apply` | да |
| `cp_process_get(ref)` | `GET /process-definitions/{ref}` и первая страница `…/{key}/versions` | нет |
| `cp_process_explain(instanceId)` | экземпляр, его журнал и версия, на которой он идёт | нет |

- **Пакет — каталог на диске** (`path`): все файлы `*.yaml`/`*.yml` под
  ним, путь относительно каталога через `/`; скрытые файлы и каталоги
  (`.layout`, `.git`) в пакет не входят. Ограничения числа и размера файлов
  проверяет ядро (п.10).
- **Применение — только по хэшу плана.** `planHash` у `cp_pkg_apply`
  обязателен; пустой или не `sha256:<64 hex>` — отказ
  `plan_hash_required` без обращения к ядру. Старый хэш (каталог, открытые
  экземпляры или файлы изменились после плана) — `409 plan_stale` ядра
  (п.11) с подсказкой построить план заново. `cp_pkg_apply` — единственный
  пишущий инструмент автора: он не read-only и не доказательство работы,
  поэтому вложенному исполнителю не выдаётся (`withheld_tool_names`,
  ADR-0046); проверка, тест, план и чтение — read-only.
- **Ошибки — в форме находки п.2** (`ProcessProblemOut`, P006):
  `{error, message, details?, hint?, problems: [{code, severity, path,
  file, line, message, hint}]}`. Находки отказа ядра берутся из
  `details.problems` (`invalid_package`, `migration_required`); ошибка
  тела запроса — по находке на `details.errors`; любой другой отказ (право,
  `not_found`, `plan_stale`) и ошибки самого адаптера (`package_not_found`,
  `package_empty`, `package_file_unreadable`, `plan_hash_required`) —
  одна находка с `severity: error`. Отчёты проверки, теста и плана
  возвращаются как есть: их `problems` уже в этой форме.
- **Объяснение экземпляра** читает то же, что replay (п.5): экземпляр,
  журнал решений (до 1000 записей; дальше — `journalCursor`) и определение
  версии экземпляра (`key@version`). Ответ: `instance`; `process` — ключ,
  версия, хэш и `governedBy` процесса; `steps` — по записи журнала: вход
  (что пришло, `actorId`, `eventId`, тело входа), решения и намерения с
  `reason` и `data`; у каждого решения и намерения `governedBy` — документы
  его элемента и объемлющих элементов, ближайший первым, затем процесса,
  каждый с `element`, где он объявлен (`null` — процесс; элемент
  `<шаг>:<часть>` — таймер или ветка шага — отвечает регламентам шага);
  `memory` — ответы памяти на шаги `recall` (вход `recall` журнала:
  `status`, `result` или `reason`, шаг).
- Клиент (`control_plane_client`) получает методы `get_process_definition`,
  `list_process_versions`, `get_process_instance`, `list_process_journal`,
  `test_package`, `plan_package`, `apply_package`.

## Границы

- Движок не исполняет код: только объявленные блоки. Всё остальное —
  имеющиеся задачи, approvals, скиллы, наблюдения и правила.
- Нет BPMN XML и импорта из внешних редакторов; нет `goto`.
- Нет локальной библиотеки движка у `cp_packages`: без ядра работает только
  статическая проверка схемой.
- Визуальный редактор в решение не входит; раскладка — файлы
  `.layout/<process>.json` пакета, ядро их не читает.

## Последствия

- Ядро получает подсистему процессов: определения, экземпляры, таймеры,
  календари, песочницу тестов, replay и план (отступление по ст. VIII принято
  владельцем).
- `process-runtime` выводится из суперпроекта (TAI-ADR-0032 → Superseded by
  TAI-ADR-0054).
- Задачи и approvals экземпляров — обычные объекты ядра: консоль, «Важное»,
  MCP и исполнители видят их без доработки.
- `control-plane-worker` получает два цикла (`process_events`,
  `process_timers`) и третий курсор журнала.

## Не принято

- Компиляция процесса в набор правил (`WorkRule`): у правила нет состояния
  экземпляра — таймеров, кворума и компенсаций не выразить.
- Отдельный сервис процессов или процесс-воркер: второй источник состояния,
  лишняя память на staging.
- Кворум внутри одного approval ядра: пришлось бы менять модель approval для
  всех потребителей.
- Разделение обязанностей в движке: голос в обход движка его бы не заметил.
- Синхронная память в транзакции движка (ADR-0076).

## Амендмент 2026-09-29: события шагов, сроки SLA, пересчёт сроков при миграции (§3, §8, §11, §13)

Статус амендмента: Accepted (2026-09-29), конституция 1.0.0. Фича
`process-observability`, задача P001 (TASK-000869). Основание — spec и plan
`specs/process-observability/` суперпроекта (FR-001…FR-007, FR-016,
FR-023…FR-025, FR-028, FR-031; решения владельца В1, В5–В7 от 2026-09-29),
TAI-ADR-0059. Сроки SLA и рабочее время календаря —
[ADR-0078](0078-process-sla-and-working-time.md).

### Что меняется

#### §13 События — шаги процесса в общем журнале

Прежняя формулировка: «решения движка целиком живут в журнале экземпляра;
событие журнала ядра говорит остальной платформе, что с делом случилось».
Она остаётся в силе для решений. Дополняется так: **вход экземпляра в
ожидающий шаг и выход из него — тоже то, что случилось с делом.** Потребители
(консоль, правила уведомлений, правила вывода работы, другие процессы)
должны видеть их без чтения журнала экземпляра.

Новые типы каталога, `entityType = process_instance`, общие поля экземпляра
плюс `workspaceId`:

| Тип | Когда | Payload сверх общих полей |
|---|---|---|
| `process.step_entered` | открыт ожидающий шаг (activity) | `element, stage, stepKind, waitsFor, attempt, activityId, enteredAt, taskId, approvalIds, skillInvocationId, childInstanceId, due, warnAt, provisional` |
| `process.step_exited` | шаг закрыт | `element, stage, stepKind, attempt, activityId, enteredAt, exitedAt, outcome, durationSeconds, due, breached, overdueSeconds` |

- `stepKind` — вид шага проекции (`human, approve, call, recall, listen, wait`),
  `waitsFor` — `task | approval | skill | agent | child | event | time | memory`.
- `outcome` — `completed | cancelled | withdrawn | interrupted | failed | timed_out | migrated`.
  `withdrawn` — работу шага отменил участник вне процесса (решение владельца
  2026-09-29): кто — `actorId` события. У выхода `withdrawn` `actorId` — актор
  входа, закрывшего activity (тот, кто перевёл задачу в `terminal_cancelled`
  или отменил approval); у остальных событий шага — личность процесса.
- `attempt` — номер входа в этот элемент в экземпляре, с 1.
- Данных экземпляра события шага не несут (право `events.read`, а не
  `processes.read`).
- Мгновенные шаги (`set`, `decide`, `remember`, `do`, `complete`, `raise`)
  событий шага не дают.

**Как пишутся.** События шагов — **проекция журнала экземпляра, а не новые
решения или намерения движка.** После каждого шага движка приложение
(`take()`) сравнивает `state.activities` до и после шага. Открытые — это
`step_entered`, закрытые — `step_exited`. Исход выводится из вида входа и
решений шага по таблице:

| Признак в шаге | `outcome` |
|---|---|
| вход закрыл свою activity (задача, решение approval, результат скилла, ответ памяти, событие `listen`, завершение дочернего) | `completed` |
| решение `activity_cancelled` | `cancelled` |
| вход — задача шага в категории `terminal_cancelled` или отменённый approval шага, после которого голосовать некому (отмена участником, а не процессом) | `withdrawn` |
| таймер `timeout` activity | `timed_out` |
| прерывающий граничный таймер закрыл поток | `interrupted` |
| экземпляр ушёл в `failed` | `failed` |
| запись миграции элемента без пары в новой версии | `migrated` |
| эскалация `action: raise`, ошибку поймал `try` (решение `activity_cancelled`, причина `error`) | `cancelled` |
| эскалация `action: raise` без обработчика — экземпляр ушёл в `failed` | `failed` |
| задачу отменили вне процесса: вход `task` со статусом `cancelled` закрыл свою activity | `withdrawn` |
| approval шага отменили, и голосовать больше некому (все approval отозваны: `total` входа ≤ 0; в журнале это `approval_decided` с `cause: "quorum"`, понятие `no_approvers` — из `approval_quorum`) | `withdrawn` |
| approval шага отменили, и шаг решило правило голосования по оставшимся (`approved` или `rejected` по `quorum`, например `all` с одобрившим и отозванным, недостижимый `atLeast`) | `completed` |
| approval шага отклонили (`rejected`), задача дошла до `terminal_success` | `completed` |
| выход activity, открытой до выкатки событий шагов (попытка при входе не сохранена) | исход по строкам выше, `attempt` = 1 |

Запись идёт `record_event` в той же транзакции, что и запись журнала
экземпляра. `pg_notify`, outbox и курсоры работают без изменений.
Уникальность `(instance_id, source_ref)` делает повторную доставку входа
пустой — повторных событий шагов нет. Номер попытки хранится в
`process_instances.step_attempts` (`{element: n}`) — счётчике приложения, а
не движка.

Replay (`domain/process_replay.py`) сравнивает решения и намерения. События
шагов не являются ни тем, ни другим, поэтому replay старых и новых журналов
они не затрагивают (FR-028).

Уточнение P007 (реализация). Проекция — чистая функция
`domain/process_steps.py::step_events`; `take()` вызывает её после каждого
шага движка (и после каждого follow-up `intent_failed`) и пишет события
`_record_steps` после исполнения намерений, от имени личности процесса;
выход `withdrawn` — от имени актора входа (`actor_id` записи журнала
экземпляра, `actorId` события `task.updated` / `approval.cancelled`). Решения
движка и replay от этого не меняются. Если задачу или approval отменила система, а не участник (у события
нет актора, например каскадная отмена), `actorId` выхода `withdrawn` —
`null`. Резервная ветка `_withdrawn`, берущая `total` из activity при
его отсутствии во входе, — защита для входов без счётчика; `_deliver_answer`
и песочница пакетов `total` кладут всегда.

- Ожидающий шаг — activity видов `task, approval, skill, agent, child,
  recall, listen, wait`. Пауза `retry` между попытками `try` и activity
  ретроспективы (`retro_*`, после закрытия экземпляра) шагами процесса не
  являются и событий не дают.
- `stage` — стадия элемента в определении; `taskId`, `approvalIds`,
  `skillInvocationId`, `childInstanceId` — из `instance.refs` после исполнения
  намерений шага (у последовательного approve на входе — первый запрос).
- Порядок в шаге: сначала выходы, затем входы, каждые — в порядке открытия
  activity. Исход выхода проверяется в порядке: вход миграции → `migrated`;
  сработал таймер `timeout` этой activity → `timed_out`; вход этой activity
  (сохранённый вход журнала, `activityId` в теле) — задача со статусом
  `cancelled`, или approval с исходом `cancelled`, после которого не осталось
  ни одного approval (`total` входа ≤ 0), → `withdrawn`, даже если
  экземпляр из-за этого ушёл в `failed`; approval с исходом `cancelled`, после
  которого кворум решил шаг по оставшимся голосам, — `completed`: шаг решило
  правило, а не отзыв (решение владельца 2026-09-29; настраиваемая судьба
  отозванного голоса — изменение языка процессов, вне этой фичи); экземпляр перешёл в
  `failed` в этом шаге → `failed`; вход `intent_failed` по элементу activity
  (ядро отказало намерению, открывшему шаг) → `failed`; `activity_cancelled`
  с причиной `interrupted` → `interrupted`, с иной → `cancelled`; иначе
  `completed` — в том числе когда ответ сам несёт ошибку (скилл вернул
  сбой), а обработчик `try` её поймал. Отмена одного из нескольких
  approval шага, после которой activity остаётся открытой (голоса ещё
  ждут), выхода не даёт.
- `attempt` выхода — номер, с которым эта activity входила. Движок держит
  несколько activity одного элемента сразу (`onEvent`, `correlate`), поэтому
  текущее значение счётчика элемента для выхода не годится: при входе
  номер запоминается в `refs["activity:<id>"]["attempt"]` (рядом с записью
  approval, если она есть), при выходе берётся оттуда и из `refs` убирается —
  `refs` не растут с числом закрытых шагов. У activity, открытой до выкатки
  (номера нет), `attempt` выхода — 1, её выход приходит без входа.
- Миграция сейчас переносит activity с прежними id (без пары в новой
  версии она отказывает `migration_required`), поэтому выходов `migrated`
  пока не бывает; `packages:apply` переименовывает ключи `step_attempts` по
  карте миграции вместе с элементами.
- Поля срока берутся из записи срока activity (`activity.sla`, ставит
  движок при ревизии 2, уточнение P011 в ADR-0078 §3). Вход несёт `due`,
  `warnAt`, `provisional`. Выход несёт `due`, `breached` и `overdueSeconds`:
  `breached` — шаг закрыт позже срока (момент выхода позже `dueAt`) или срок
  уже сработал; `overdueSeconds` — `exitedAt − due` при нарушении без
  приостановок, пережитых уже прошедшим сроком (`overdueStops`, ADR-0078 §4,
  уточнение P016 — срок, прошедший до приостановки), иначе `null`. Без
  срока и при сбое срока поля пусты: `due`, `warnAt`, `overdueSeconds` —
  `null`, `breached` и `provisional` — `false`. У версий
  ревизии 1 срока в записи нет, поэтому поля пусты и у шагов с `due`.

#### §13 События — сроки

Типы `process.sla_warning`, `process.sla_breached`, `process.sla_failed` пишет
движок намерением `emit_event` при срабатывании таймеров срока
(ADR-0078 §3). Они в каталоге рядом с остальными `process.*`.
`process.escalated` не меняется (его `addressees` добавляет приложение при
записи, ADR-0078, амендмент 2026-09-30). Намерение `emit_event` событий SLA несёт,
кроме payload, `addressees` — цепочки кандидатов `owner` и `assignee` с
вычисленными выражениями; в ids адресатов их разрешает приложение
(уточнение P013 в ADR-0078 §3).

#### §8 Таймеры — новые виды

Добавляются виды таймера `sla` и `sla_warning` (колонка `timer_kind` —
строка, миграции схемы для вида не нужно). Остаток замороженного таймера
срока в рабочем времени хранится в единице срока: новая колонка
`process_timers.remaining_unit` — `wall | working_seconds | workdays`, по
умолчанию `wall`. Прочие таймеры не меняются. Реализовано P012: заморозка,
возобновление и пересчёт замороженного остатка по новой версии календаря —
ADR-0078 §4, уточнение P012; `set_timer` несёт `remainingUnit`, только если
единица не `wall`.

#### §11 Миграции — пересчёт сроков

Прежде: у таймеров переносились указатели выражений, `dueAt` оставался
прежним. Теперь после переноса состояния по карте ядро делает шаг движка со
входом `migrated`. Это новый вид входа; в старых журналах его нет, поэтому
их replay не меняется. Вход `migrated` пересчитывает сроки SLA открытых
шагов от их `openedAt` и срок процесса от старта экземпляра по новой версии.

- новый срок — `process.timer_rescheduled` с `cause: migrated`, срок задачи
  шага обновляется (`update_task_due`, новое намерение);
- срок уже прошёл — одно `process.sla_breached` с `detectedBy: migration`;
- уровни эскалации, чей момент прошёл, отменяются без срабатывания (решение
  `escalation_skipped`); будущие ставятся;
- срок у шага появился впервые — так же, от `openedAt`.

`pin` не меняется. План пакета (`POST /packages:plan`) в разделе миграций
перечисляет затронутые экземпляры: `deadlines: [{instanceId, element,
previousDueAt, dueAt, breached}]` (FR-023). Уточнение P004: раздел — поле
`deadlines` процесса в `processes[]` ответа (`PlanProcessOut`, элемент —
`PlanDeadlineOut`); `element: null` — срок процесса, `previousDueAt: null` —
срок появился, `dueAt: null` — снят. Раздел считает P017 — уточнение ниже.

Уточнение P016 (реализация, TASK-000884):
- Вход движка — `migrated` (`process_migration.RECOUNT_INPUT`), тело
  `{fromVersion, toVersion}` — только для читателя журнала. `packages:apply`
  (`_migrate` в `commands/package_plan.py`) берёт его обычным шагом
  (`process_instances.take`, `source_ref` `migration:<id версии>/migrated`,
  `actorId` — применивший) сразу за записью `migrate` и событием
  `process.migrated`, в той же транзакции, если у целевой версии ревизия
  со сроками SLA (`recounts`). Под ревизией 1 движок отвечает на вход
  решением `ignored` (`reason: no_deadlines`). Приостановленный экземпляр
  вход не откладывает.
- Пересчитываются activity шагов `human`, `approve`, `call`, `recall`,
  `listen` по `due` элемента новой версии (база — `openedAt`) и срок
  процесса по `spec.due` (база — `startedAt`), значения выражений — текущие
  данные экземпляра. Живой таймер срока переносится на новый рецепт на
  месте (тот же `timerId`): решение `timer_rescheduled` с
  `cause: migrated`, `set_timer` и `process.timer_rescheduled`, если момент
  изменился; недостающий таймер ставится (`timer_set`), лишний снимается
  (`cancel_timer`). Запись срока (`activity.sla`, `state.sla`) пишется
  заново; у шага `human` до ревизии 2 «прежним сроком» считается срок
  задачи (`activity.due`).
- Прошёл ли срок, судится по моменту, на котором стоят часы таймера: у
  замороженного — `frozenAt`, иначе — время входа. Прошедший срок: таймеры
  снимаются, запись `breached`, решение `sla_breached` и
  `process.sla_breached` с `detectedBy: migration`, `detectedAt` — время
  входа, `overdueSeconds` — от срока до момента, на котором стоят часы, без
  приостановок, пережитых уже прошедшим сроком (`overdueStops`, ADR-0078 §4):
  столько же, сколько дало бы срабатывание таймера в тот же момент. Срок,
  уже нарушенный на старой версии и прошедший и по новой, второго события
  не даёт. Продлённый за момент нарушения срок снова
  `pending` со своим таймером. Прошедший порог предупреждения не
  сообщается задним числом: таймера нет, запись в состоянии `warning`.
- Замороженный таймер переносится замороженным: новый остаток считается от
  `frozenAt` до нового момента в единице срока (как при новой версии
  календаря, ADR-0078 §4); решение `timer_rescheduled` с `dueAt: null`,
  событие даст возобновление.
- Прошлые приостановки не теряются: к сроку, посчитанному от `openedAt`
  (`startedAt`), прибавляются паузы, которые хранит его таймер `sla`
  (`paused`, ADR-0078 §4, уточнение P016); порог откладывается назад от
  срока с паузами. Уровень эскалации и `onDue` прибавляют паузы своего
  таймера, новый уровень — паузы срока. Миграция на тот же `due` после
  приостановки и возобновления срок не меняет, нарушения и снятых уровней
  не даёт.
- Эскалации и `onDue` шага `human`/`approve` считаются от нового срока
  (по номеру уровня). Уровень, чей момент прошёл, снимается без
  срабатывания: решение `escalation_skipped` (`reason: migrated`, `level`,
  `dueAt`; у `onDue` — `level: null` и `onDue`). Будущий уровень
  переносится или ставится, уровень, которого нет в новой версии,
  снимается. Уровень, сработавший на старой версии, таймера уже не имеет,
  и от нового уровня не отличим: если его момент по новой версии ещё
  впереди, он ставится снова. Срок не вычислился — `process.sla_failed`
  (если прежняя запись не была `failed` с той же ошибкой) и
  `escalation_skipped` с `reason: due_failed`.
- Что изменилось, записывает решение `deadline_migrated` (`scope`,
  `activity`, `previousDueAt`, `dueAt`, `breached`; вид записи журнала —
  `migration`) — только если срок изменился, появился, снят или стал
  нарушенным. Его поля — те, что нужны разделу `deadlines` плана (P017).
- Срок задачи шага `human`: намерение `update_task_due` (`activityId`,
  `element`, `due`, `null` — срок снят); исполнитель
  `do_update_task_due` меняет `dueDate` открытой задачи activity обычной
  командой `update_task` с правом личности процесса. Отказ только
  записывается, как у `reassign_task`. У `approve` срока задачи нет.
- `migrate_state` не отказывает `expression_gone` для таймеров, которые
  вход `migrated` посчитает заново (`sla`, `sla_warning`, `escalation`,
  `due`), когда у целевой версии ревизия со сроками: смена формы `due` (с
  `{at}` на `{workdays}`) не требует выражения старой формы.
- Replay: вход `migrated` — первый шаг после записи `migrate`, с которой
  начинается переигрывание; он записан с версиями календарей, по которым
  считал, и переигрывается без расхождений. `pin` не трогается: экземпляр
  остаётся на старой версии, записей в журнале и таймеров не прибавляется.

Уточнение P017 (реализация, TASK-000885):
- `packages:plan` (`_deadlines` в `commands/package_plan.py`) для каждой
  группы экземпляров с `migrate`, без `migrationRequired`, при ревизии
  целевой версии со сроками делает тот же шаг, что `apply`: состояние
  переносится по карте (`migrate_state`) на копии, `seq` — следующий, и
  движок берёт вход `migrated` в момент плана (`engine_time`) с
  календарями, какие оставит `apply`: календарь из пакета — его
  публикуемая спека, прочие — последние версии. Раздел — решения
  `deadline_migrated` этого шага: `element` — элемент решения (`null` у
  срока процесса), `previousDueAt`, `dueAt`, `breached` — его поля. В базу
  ничего не пишется: шаг берётся на копии, а транзакция плана откатывается
  (амендмент TASK-000903 сделал её откатываемой вместо `READ ONLY`).
- Поэтому раздел совпадает с тем, что `apply` того же плана запишет в
  журнал, если между планом и применением не прошло время, меняющее
  `breached`: срок, прошедший между ними, `apply` найдёт нарушенным.
- Срок, уже нарушенный на старой версии и прошедший и по новой с тем же
  моментом, в раздел не попадает — решения о нём нет. У `pin`,
  `unaffected`, группы с `migrationRequired` и версии ревизии 1 раздел
  пуст.
- `deadlines`, как `behaviour`, — отчёт и в `planHash` не входит
  (`plan_body`): `breached` зависит от времени плана, и хэш применения,
  построенного позже, не должен от него меняться.

Уточнение (ревью P017, TASK-001161):
- Раздел `deadlines` процесса ограничен, как `instanceIds` у `behaviour`:
  не больше `MAX_PLAN_DEADLINES` (200) записей в порядке групп по версии и
  экземпляров по id; `deadlinesTotal` в `PlanProcessOut` — число всех
  записей, больше длины раздела, когда он усечён. Шаг `migrated` при этом
  берут все экземпляры: без него число не узнать.
- Состояние переносится по карте один раз на экземпляр: `_instances`
  (проверка переноса) сохраняет перенесённую копию (`_move`: состояние,
  следующий `seq`, момент `engine_time`), и `_deadlines` берёт шаг на ней.
  `_migrate` в `apply` строит запись `migrate` и тело входа `migrated` тем
  же `_move`. Календари плана и шага `take` выбирает один хелпер
  `calendars_named` (`commands/process_instances.py`): план подставляет
  календари пакета их публикуемой спекой.

#### §3 Экземпляр закреплён за версией — ревизия семантики

У строки `process_definitions` появляется `engine_revision`. У версий,
опубликованных до амендмента, она равна 1, у новых — 2. Движок получает её в
`Definition`. Сроки SLA (таймеры `sla`/`sla_warning`, события `process.sla_*`,
срок у шагов кроме `human`/`approve`, срок процесса) действуют только при
ревизии 2. Экземпляр старой версии исполняется и переигрывается как прежде,
пока не мигрирован на новую версию (FR-031, решение В7). Миграция — запись
журнала — несёт ревизию целевой версии.

Уточнение P010 (реализация): ревизии — `ENGINE_REVISIONS = (1, 2)` в
`domain/process_engine.py`, последняя — `ENGINE_REVISION`. `Definition`
несёт `engine_revision`; версия опубликованная собирается с ревизией своей
строки (`definition_of`). Спека пакета в тесте и плане собирается с той
ревизией, под которой её исполнит `apply` (`engine_revision_for`): спека,
которую публикация создаст заново, — с последней; совпадающая по
`definitionHash` с опубликованной `key@version` — с ревизией этой версии.
Неизвестная ревизия — `EngineError`; у опубликованной версии (например, после
отката кода) это `409 process_definition_unusable`, а не 500. Публикация
(`publish_process_definition`, в том числе из `packages:apply`) пишет
`ENGINE_REVISION`; строки до миграции фичи получили 1 умолчанием столбца.
Повторная публикация той же спеки возвращает существующую версию с её
ревизией: чтобы процесс перешёл на ревизию 2, нужна новая версия. Ревизию
версии показывают `engineRevision` в `ProcessDefinitionOut` и
`ProcessVersionOut`.
Replay экземпляра идёт под ревизией версии, к которой он закреплён, а после
миграции — с записи миграции, то есть под ревизией целевой версии. Кандидат
`:replay` и раздел поведения плана переигрываются под номером **и ревизией**
версии экземпляра (`as_version(definition, version, engine_revision)`):
журнал записан под ней, и смена ревизии — дело миграции, а не отличие
кандидата. Запись миграции несёт `engineRevision` целевой версии и во входе
(`input.body`), и в решении `migrated` (`migration_record` в
`domain/process_migration.py`). Ревизия 2 отличается от 1 сроками SLA
(P011, ADR-0078 §3): журнал переигрывается без расхождений под ревизией,
под которой записан; процесс без сроков ведёт себя под обеими одинаково.

Исключение — поправка TASK-001134 (амендмент CP-ADR-0075 2026-09-30
«значение-сообщение при записи в данные»): запись пустых списков в
`process_engine._message` изменила семантику движка без новой ревизии.
Журнал, записанный до неё, при `:replay` и в разделе поведения плана может
разойтись с записанным под той же ревизией; что и где расходится, разобрано
в «Следствиях поправки» того амендмента. Правило впредь: семантическая правка
движка — то, после чего прежний журнал переигрывается с другими решениями или
данными, — вводится новой ревизией движка (`ENGINE_REVISION`), а прежнее
поведение остаётся за прежней ревизией; правка без новой ревизии допустима
только тогда, когда ни один записанный журнал не меняет решений.

### Права

Не меняются. События шагов и SLA — `events.read` на workspace процесса
(CP-ADR-0068), проекция — `processes.read`.

### Conformance

```conformance
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"process\.step_entered"'}
  repo: control-plane
- grep: {path: src/control_plane/domain/event_catalog.py, pattern: '"process\.sla_breached"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/process_instances.py, pattern: 'step_entered'}
  repo: control-plane
- grep: {path: src/control_plane/domain/process_steps.py, pattern: 'def step_events'}
  repo: control-plane
```

## Амендмент 2026-09-29 (TASK-000904): привязка объекта каталога к пакету

Решение владельца 2026-09-29 по вопросу из R012 (TASK-000824): ни один ответ
ядра не говорил, каким пакетом поставлен объект каталога, и консоль
группировала установленное по виду, а не по пакетам. `package_objects`
(п.11) знала пакет только у видов, которые планирует ядро (`Process`,
`Calendar`); остальные виды установщик (`cp_packages --install`) применяет
обычными маршрутами, и ядро пакета не видело.

### Е1. Связь объекта с пакетом

Объект каталога — `(kind, key)` tenant'а: все версии типа задачи, процесса,
календаря, все ревизии агента — один объект. `package_objects` становится
связью объекта с пакетом, который его поставил, для всех видов каталога,
которые держит ядро: `ArtifactType`, `TaskType`, `ProjectTemplate`,
`WorkspaceType`, `Role` (роль tenant'а; роль workspace пакету не
принадлежит), `Capability`, `Skill`, `WorkRule`, `Agent`, `Process`,
`Calendar`. `NotificationRule` живёт в сервисе уведомлений, `Package` и
`Installation` — не объекты каталога tenant'а. Нет строки — объект создан
вручную.

Связь принадлежит объекту, а не версии: версия, опубликованная человеком
позже, остаётся в пакете своего ключа (кто правил поле — вопрос плана,
`owner` в п.11). История источника по ревизиям у агента остаётся своей
(CP-ADR-0073 Г2).

Строка хранит ключ пакета, **версию пакета** (`package.yaml → spec.version`)
и **хэш установки**: `planHash` применённого плана — для `packages:apply`,
`installHash`, названный установщиком, — для `packages:record`. Поля
«что хотело применение» (`version`, `spec`, `spec_hash`) обязательны только
у планируемых видов (проверка `ck_package_objects_planned_spec`).

### Е2. Кто пишет связь

- **`POST /packages:apply`** пишет связь каждого объекта плана, включая
  `unchanged`: применение следующей версии пакета переводит связь на неё,
  даже если объект не изменился; переименованный прочь ключ сохраняет
  связь с пометкой `retired_at` (как прежде; по амендменту 2026-09-29
  package-sdk, Ж1, пометка переезжает в `catalog_retirements`, а связь
  остаётся без неё).
- **`POST /packages:record`** `{package: {key, version}, installHash?,
  objects: [{kind, key}]}` — маршрут установщика. После применения
  непланируемых видов установщик называет **все** объекты пакета, которые он
  применил, в том числе без изменений: связь переходит на эту версию пакета.
  Виды — только непланируемые (перечисление в схеме запроса; `Process`,
  `Calendar` и чужие виды — `400 invalid_request`); объект, которого нет в
  каталоге, — `422 unknown_object` со списком, и ничего не пишется (запрос
  целиком или никак). Права: `packages.plan` и право записи каждого названного
  вида (`task_types.manage`, `artifact_types.manage`,
  `project_templates.manage`, `workspaces.manage`, `org.manage` для ролей,
  возможностей и скиллов, `agents.manage`, `rules.write` на workspace
  каждого живого правила): связь говорит, чей объект, и пишет её тот, кто
  может писать сам объект. Ответ — `PackageRecordOut {package, installHash,
  recorded}` в порядке видов каталога. Событий нет: связь — не изменение
  объекта. Запись идёт под блокировкой применений tenant'а (уточнение
  TASK-000969 в п.11): применение и запись связей одного tenant'а не
  пересекаются.
- **`POST /agents`** с `package` (CP-ADR-0073 Г2) пишет и связь агента — с
  ревизией и без неё; `installHash` у такой связи пуст, пока установщик не
  назовёт агента в `packages:record`.

Связь — заявление применяющего с правом вида, как источник ревизии агента:
ядро не сверяет ключ и версию с файлами пакета.

### Е3. `package` в ответах и `?package=`

Списки и карточки всех видов Е1 (`GET /task-types`, `/task-types/{id}`,
`/artifact-types[/{ref}]`, `/project-templates[/{id}]`,
`/workspace-types[/{id}]`, `/roles[/{id}]`, `/capabilities[/{id}]`,
`/skills[/{ref}]`, `/rules[/{id}]`, `/agents[/{ref}]`, `/agents/me`,
`/process-definitions[/{ref}]`, `/calendars[/{ref}]`), ответы их записей и
вложенные объекты назначений принципала отдают поле
`package: PackageLinkOut | null` — `{key, version, installHash,
installedAt}`; `version` пуст у строк, применённых до амендмента. Поле
добавлено, прежние поля не менялись: ответ обратно совместим.

Списки этих видов принимают `?package=<key>` — только объекты, которые
поставил пакет с этим ключом (`EXISTS` по `package_objects`: страница,
порядок и курсор — прежние). Консоль группирует установленное по пакетам
теми же запросами списков, что и по видам: поле `package.key` у каждого
элемента или фильтр по ключу пакета.

### Е4. Миграция

Ревизия `b5d1e7a3c9f4`: проверка вида расширена до видов Е1; колонка
`package_version`; `plan_hash`, `version`, `spec`, `spec_hash` — nullable
с проверкой `ck_package_objects_planned_spec`; индекс
`ix_package_objects_tenant_package (tenant_id, package_key, kind)` под
фильтр. Агенты, чьи ревизии называют пакет, получают связь по новейшей такой
ревизии. Откат удаляет связи непланируемых видов и версии пакетов.

### Е5. Что осталось установщику

`cp_packages` суперпроекта должен после применения каждого пакета вызвать
`POST /packages:record` со всеми применёнными объектами непланируемых видов
(ядро их не видит), передавать `package` в `POST /agents` и `installHash`
(например, хэш файлов пакета). Без этого связь есть только у процессов,
календарей и агентов, опубликованных с `package`.

### Е6. Согласование с планом всех видов (TASK-000903)

Амендмент п.11 от 2026-09-29 (TASK-000903) делает `TaskType`, `Agent` и
`WorkRule` планируемыми видами, а установщик применяет их обычными
маршрутами, пока не перейдёт на plan → apply. Поэтому:

- `packages:record` принимает все виды Е1, кроме видов движка (`Process`,
  `Calendar`): `TaskType`, `Agent`, `WorkRule` — тоже (переходный путь
  установщика). Запись установщика очищает у строки «что хотело применение»
  (`version`, `spec`, `spec_hash`): объект перезаписан маршрутом вида, и
  следующий план считает все поля `package`. Так же пишет связь
  `POST /agents` с `package`.
- `packages:apply` пишет строку этих видов с `version`, `spec`, `spec_hash`
  сам; `POST /agents`, вызванный изнутри применения, связь не пишет.
- Проверка `ck_package_objects_planned_spec` остаётся на видах движка:
  у `TaskType`, `Agent`, `WorkRule` строка без `spec` законна (связь
  установщика или миграции Е4).
- Отдельной миграции у TASK-000903 нет: проверку вида расширила ревизия
  `b5d1e7a3c9f4`.

## Амендмент 2026-09-29 (package-sdk): вывод процессов и календарей из оборота, субъекты тестов пакета

Фича `package-sdk` (spec/plan/tasks — `specs/package-sdk/` суперпроекта;
решения Р6 и Р11 plan, FR-016, FR-025; конституция ст. V, VI, IX), пункт S004
(TASK-000928). Контракт записан этим шагом. Реализация: Ж — S012, З — S020.
Ж реализован S012 (TASK-000936): таблица `catalog_retirements` (ревизия
`c3e8f1a6d2b4`), маршруты `:retire`, находки `process_retired` и
`calendar_retired`, методы клиента. Уточнения реализации:

- выведенный процесс или календарь, который пакет ставит без изменений, план
  показывает действием **`restore`** (Ж3: пакет, который ставит объект,
  возвращает его в оборот). `packages:apply` удаляет строку
  `catalog_retirements`, версию не публикует (`version` — последняя) и пишет
  событие `process.definition_restored` или `calendar.restored` (`key,
  latestVersion, packageKey, packageVersion`). Так план и применение по одному
  `planHash` совпадают: план смотрит на календари пакета как на действующие, и
  применение делает их действующими. Процесс `restore` проходит из проверки
  п.2 только `calendar_retired`: вернувшись в оборот, он не должен ссылаться на
  выведенный календарь, которого пакет не ставит. `create`/`update` возвращают
  ключ публикацией, как в Ж1;
- вывод держит advisory-блокировку ключа (ту же, что публикация), пока
  считает, кому ключ нужен. Явный старт (`POST /process-instances`) берёт
  блокировку ключа процесса разделяемой, публикация процесса — ключей своих
  календарей, прежде чем читать признак вывода: они ждут вывод и видят его, а
  не проскакивают после подсчёта. `call` берёт ключ дочернего процесса тоже
  разделяемым, но **не ждёт** (см. порядок блокировок ниже): занятый ключ —
  `catalog_key_busy`, шаг повторяется целиком. Определение процесса (последняя
  версия) оба читают после блокировки ключа (TASK-001122): старт, дождавшийся
  применения или публикации, стартует на версии, которую они опубликовали, а
  не на прочитанной до ожидания. Старт событием читает признак раз на
  пачку наблюдений без блокировки: экземпляр, заведённый в окне вывода, живой
  и доживает, как прочие;
- порядок блокировок ключей (TASK-001062, замечания ревью S012).
  `packages:apply` до построения плана, после ключей типов задач, агентов и
  правил, берёт эксклюзивно все ключи `Process`, затем все ключи `Calendar`
  плана (и источники переименований), по возрастанию внутри вида: публикация
  процесса держит свой ключ и затем берёт разделяемыми ключи своих
  календарей, поэтому процессы — раньше календарей. `call` (старт дочернего
  процесса) берёт разделяемую блокировку ключа **без ожидания**
  (`pg_try_advisory_xact_lock_shared`): пачка воркера держит ключи
  запущенных ею дочерних процессов до фиксации, и ожидание замкнуло бы цикл
  с применением, которое держит этот ключ и ждёт другой, взятый пачкой
  (Postgres отменил бы одну из транзакций). Занятый ключ — ошибка
  `catalog_key_busy` (503, `DependencyUnavailableError`): она проходит точки
  сохранения шага, пачка откатывается, отпускает ключи и повторяется;
  `intent_failed` родитель не получает; запрос, шаг которого вызвал `call`,
  отвечает `503 catalog_key_busy`. Для пачки журнала воркера занятый ключ —
  не сбой, а мягкий повтор (TASK-001122): применение держит ключи всех
  процессов плана всю свою транзакцию, это ожидаемое состояние. Курсор не
  сдвигается, `next_attempt_at` откладывается на `outbox_backoff_base_seconds`
  без роста; `failure_count` и `parked_*` не меняются, в журнал — строка
  `info` без трассировки. Таймер и recall, шаг которых упёрся в занятый ключ,
  так же пишут `info` и повторяются своим обычным путём (таймер — на
  следующем цикле, recall — по истечении аренды). Явный старт
  (`POST /process-instances`) ключ своего процесса ждёт, как прежде; перед
  ним он берёт принципалов процессов (`lock_process_identities`, CP-ADR-0077
  §3), которых его шаг всё равно возьмёт, — как пачка воркера, по правилам 1
  и 3 порядка строк. Цикла с применением это не закрывает и не требует:
  применение держит принципалов только `FOR KEY SHARE`, совместимой с той же
  блокировкой старта, и тест такого цикла не воспроизводит (замечание ревью
  TASK-001062);
- `restore` процесса берёт разделяемыми ключи своих календарей
  (`references(spec).calendars` последней версии) и перечитывает их вывод
  перед снятием пометки: вывод календаря, зафиксированный после плана (он
  видел процесс выведенным и не нуждающимся в календаре), отказывает
  применению `409 calendar_retired` вместо возврата процесса со ссылкой на
  выведенный календарь;
- список `calendar_in_use` берёт `references(spec).calendars` — на `main` это
  `spec.calendar` (`cal.*` считает по нему), `due.calendar` ADR-0078 ещё не
  влит.
Статус амендмента — Accepted после одобрения ревью владельцем (критерий
приёмки S004); до одобрения это предложение.

**Проблема.**

1. Процесс и календарь нельзя вывести из оборота. Ключ, переименованный
   пакетом прочь, выводится (п.11), но только внутри `packages:apply` и только
   записью владения пакета. Процесс, который установка больше не ставит,
   продолжает заводить экземпляры по своему `start.on`. Установке
   (`Installation.spec.retire`, plan Р4–Р5) нечем его остановить, кроме
   удаления, а удаления нет: версии неизменяемы, на них стоят экземпляры и
   журналы.
2. `packages:test` исполняет только процессы. Правила вывода работы и исходы
   типов задач автор проверяет питоном в суперпроекте (`tools/tests`), а
   автору вне дерева платформы это недоступно (FR-016). Симулятор правил в
   SDK стал бы вторым источником истины (plan Р6).

### Ж1. Признак «выведен» — у ключа, одна таблица

Версии процессов и календарей неизменяемы (триггеры `process_definitions` и
`calendars`, п.1, п.9), поэтому признак не может быть колонкой версии. Вывод
относится к ключу: все версии ключа выведены вместе.

Признак хранит таблица `catalog_retirements`: `(tenant_id, kind, key)` —
первичный ключ; `kind` принимает значения `Process` и `Calendar`; остальные
колонки — `retired_at`, `retired_by`, `reason`. Добавляет её одна миграция
(S012). Это единственный признак вывода для обоих видов:

- переименование пакетом (п.11) тоже пишет строку в `catalog_retirements`.
  Колонка `package_objects.retired_at` признаком больше не служит: миграция
  переносит её непустые значения в новую таблицу (`retired_by` — `applied_by`
  записи владения, `reason` — `renamed by package <key>`) и удаляет колонку.
  Откат миграции возвращает колонку из строк с этой причиной;
- `retired_processes()` и `retired` в `catalogEtag` плана (п.11, P015)
  читают новую таблицу. Значения хэша для существующих строк не меняются;
- S012 переписывает на `catalog_retirements` всё, что сейчас читает и пишет
  `record.retired_at`. Сюда входит и план всех видов из TASK-000903 (п.11,
  амендмент TASK-000903, и Е6; `application/commands/package_plan.py`):
  - признак `retired` в `catalogEtag`;
  - база сравнения: запись владения выведенного ключа базой не служит;
  - условие переименования «источник не выведен»;
  - пометка источника при применении переименования и её снятие при
    публикации ключа пакетом.

  Проверки `PackageObject.retired_at` в `process_definitions.py` и
  `process_instances.py` (старт выведенного процесса) переходят туда же.
  Переименовываются только `Process` и `Calendar` (`RENAMED_KINDS`), и
  других видов в колонке нет, поэтому `kind` новой таблицы их покрывает;
- публикация новой версии выведенного ключа (`POST /process-definitions`,
  `POST /calendars`, `packages:apply` с `create`/`update`) удаляет строку и
  возвращает ключ в оборот. Так уже работает переименование (п.11): вывод —
  решение об использовании ключа, а не запрет его имени. Ключ агента, в
  отличие от этого, после вывода не переиспользуется (ADR-0073 п.9), потому
  что за ним стоит личность.

В ответах признак виден у каждой версии:

- `ProcessDefinitionOut`, `ProcessVersionOut` и `CalendarOut` получают
  `status: active | retired` и `retired: {at, by, reason} | null`;
- `GET /process-definitions` и `GET /calendars` принимают
  `?status=active|retired`. Без параметра список прежний: в нём оба статуса;
- чтение по ключу и версии, список версий, журнал и `:replay` работают и для
  выведенного ключа. На версиях стоят живые экземпляры, и replay (п.10) нужен
  им до конца.

### Ж2. `POST /process-definitions/{key}:retire`

Тело — `{reason}`, строка 1…500, как у `POST /agents/{key}:retire`. Причина
редактируется и обрезается так же, как прочие причины в журнале. Право —
`processes.write` на workspace последней версии (у процесса tenant'а — на
tenant): вывод — решение автора процесса, а не оператора дела (п.12).

- Все версии ключа получают `status: retired`.
- **Новые экземпляры не заводятся**:
  - событие старта (`start.on`) с новым ключом экземпляра экземпляра не
    заводит. Воркер пропускает его без ошибки, потому что отвечать некому;
  - `POST /process-instances` — `409 process_retired`
    (`details.process`), как у переименованного ключа;
  - `call` (намерение `start_child`) — отказ команды, то есть вход
    `intent_failed` с `detail: process_retired` родителю (P009). Отказ ловит
    `try` родителя, иначе родитель переходит в `failed`.
- **Живые доживают**. Экземпляры `running` и `suspended` идут на своих версиях
  до конца. До них доходят событие старта с ключом существующего экземпляра
  (`process.correlated`), `correlate`, `onEvent`, таймеры, ответы задач,
  approvals и скиллов. Команды оператора (`:suspend`, `:resume`, `:cancel`)
  работают.
- **Проверка определения** (п.2): `call` другого процесса на выведенный ключ
  даёт предупреждение `process_retired` с путём шага, а не ошибку. Процесс
  публикуется, и ошибкой такой вызов становится только при исполнении.
- Ответ — `200 ProcessRetireOut`:
  - `key`;
  - `status: retired`;
  - `retired {at, by, reason}`;
  - `openInstances` — число живых экземпляров;
  - `byVersion: [{version, openInstances}]` — только версии с живыми.

  Считаются экземпляры всех workspace'ов процесса: право на ключ даёт и
  знание о его делах, как у плана (п.11).
- **Повтор** — `200` с тем же телом, где `retired` — первого вывода. Нового
  события нет.
- **Неизвестный ключ** — `404`.
- **`?dryRun=true`** — те же проверки и тот же ответ, но без записи и события.
  Раздел `retire` единого плана `package-sdk.plan/v1` (plan Р5, Р11) строится
  этим вызовом. Так SDK не повторяет правило «сколько живых» у себя.

Событие `process.definition_retired`: `entityType` — `process_definition`,
`entityId` — id последней версии. Payload — `key, latestVersion, workspaceId,
reason, openInstances, byVersion`.

### Ж3. `POST /calendars/{key}:retire`

Тело — `{reason}` (1…500). Право — `calendars.write` (tenant).

- **`409 calendar_in_use`**, если на календарь ссылается хоть один процесс,
  которому он ещё нужен. Процесс ссылается на календарь, если ключ есть в
  `references(spec).calendars` его версии. Это та же выборка, по которой
  новая версия календаря пересчитывает таймеры (п.9):
  - `spec.calendar`;
  - `cal.*` в выражениях;
  - `due.calendar` шагов и `spec.due.calendar` процесса — сроки в рабочем
    времени (ADR-0078 §1, фича `process-observability`, пока на ветке
    `feature/process-observability`). S012 берёт выборку такой, какой она
    будет на `main` к моменту реализации. Если ADR-0078 влит раньше S012,
    выборка включает `due.calendar`, и тест `calendar_in_use` проверяет
    ссылку из срока шага.

  Календарь нужен:
  - последней версии каждого **невыведенного** процесса: она заводит новые
    экземпляры;
  - версии каждого **живого экземпляра** (`running`, `suspended`) любого
    процесса, включая выведенный: его таймеры и `cal.*` ещё вычисляются.

  `details: {calendar, processes: [{key, version, openInstances}], total}` —
  до 50 элементов. Процесс workspace'а, где у вызывающего нет
  `processes.read`, в список не попадает, но входит в `total`: право на
  календарь не даёт права читать чужие процессы.
- **Вывод удался.** Все версии календаря получают `status: retired`. Чтение
  `GET /calendars/{key}[@version]` работает как прежде: версии нужны журналам
  и replay (п.9).
- **Проверка определения** (п.2): ссылка новой версии процесса на выведенный
  календарь — ошибка `calendar_retired` (путь `/spec/calendar`, путь
  выражения `cal.*` или `…/due/calendar`). Предупреждения здесь мало: версия с такой ссылкой снова
  сделала бы календарь используемым, и условие `calendar_in_use` нарушилось
  бы задним числом. `packages:test` и `packages:plan` видят календарь пакета
  поверх каталога: пакет, который ставит этот календарь, публикует его заново
  (Ж1) или, если календарь в пакете не изменился, возвращает его в оборот
  действием `restore` — и ошибки не получает.
- Повтор, `404` и `?dryRun=true` — как у процесса (Ж2). Ответ —
  `200 CalendarRetireOut {key, status, retired}`.

Событие `calendar.retired`: `entityType` — `calendar`, `entityId` — id
последней версии. Payload — `key, latestVersion, reason`.

### Ж4. Права, события, коды — сводка

| Что | Где |
|---|---|
| `processes.write` даёт и `POST /process-definitions/{key}:retire` | п.12, `authz/catalog.yaml` |
| `calendars.write` даёт и `POST /calendars/{key}:retire` | п.12, `authz/catalog.yaml` |
| `process.definition_retired`, `calendar.retired` | п.13, `domain/event_catalog.py`, `docs/events/` |
| `409 process_retired` — старт выведенного процесса | Ж2, как у переименования (п.11) |
| `409 calendar_in_use` — календарь нужен процессу | Ж3 |
| находки `process_retired` (предупреждение), `calendar_retired` (ошибка) | п.2 |

Новых прав нет: вывод — такая же запись каталога, как публикация.
`authz.coverage_check` требует право у каждого маршрута записи, и оба
маршрута его проверяют первым делом.

Клиент SDK (`control-plane-client`): `retire_process_definition(key, reason,
dry_run=False)` и `retire_calendar(key, reason, dry_run=False)`.

### Ж5. OpenAPI (фрагмент, S012)

```yaml
paths:
  /api/v1/process-definitions/{key}:retire:
    post:
      operationId: retire_process_definition
      summary: Retire a process — no new instances, open ones run to the end
      parameters:
        - {name: key, in: path, required: true, schema: {type: string}}
        - {name: dryRun, in: query, required: false,
           schema: {type: boolean, default: false}}
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/CatalogRetireRequest'}
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: '#/components/schemas/ProcessRetireOut'}
        '403': {description: 'forbidden — processes.write on the workspace of the process'}
        '404': {description: 'not_found — no process with this key'}
  /api/v1/calendars/{key}:retire:
    post:
      operationId: retire_calendar
      summary: Retire a calendar no process needs any more
      parameters:
        - {name: key, in: path, required: true, schema: {type: string}}
        - {name: dryRun, in: query, required: false,
           schema: {type: boolean, default: false}}
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/CatalogRetireRequest'}
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: '#/components/schemas/CalendarRetireOut'}
        '403': {description: 'forbidden — calendars.write'}
        '404': {description: 'not_found — no calendar with this key'}
        '409': {description: 'calendar_in_use — details.processes, details.total'}
components:
  schemas:
    CatalogRetireRequest:
      type: object
      additionalProperties: false
      required: [reason]
      properties:
        reason: {type: string, minLength: 1, maxLength: 500}
    RetirementOut:
      type: object
      required: [at, by, reason]
      properties:
        at: {type: string, format: date-time}
        by: {type: string, format: uuid}
        reason: {type: string}
    ProcessRetireOut:
      type: object
      required: [key, status, retired, openInstances, byVersion]
      properties:
        key: {type: string}
        status: {const: retired}
        retired: {$ref: '#/components/schemas/RetirementOut'}
        openInstances: {type: integer, minimum: 0}
        byVersion:
          type: array
          items:
            type: object
            required: [version, openInstances]
            properties:
              version: {type: integer, minimum: 1}
              openInstances: {type: integer, minimum: 1}
    CalendarRetireOut:
      type: object
      required: [key, status, retired]
      properties:
        key: {type: string}
        status: {const: retired}
        retired: {$ref: '#/components/schemas/RetirementOut'}
    # added to ProcessDefinitionOut, ProcessVersionOut, CalendarOut:
    #   status:  {enum: [active, retired]}
    #   retired: {oneOf: [{$ref: '#/components/schemas/RetirementOut'}, {type: 'null'}]}
    # added to GET /process-definitions and GET /calendars:
    #   status query parameter {enum: [active, retired]}
```

### З1. Субъект теста: `process`, `rule`, `taskType`

Файл теста (`tests/*.test.yaml`) описывает схема `schema/v1/test.schema.json`
package-sdk (S003, коммит `d16a959`; владелец схемы — package-sdk). Копия
ядра — `domain/package_test.schema.json`, её обновляет S020. Ядро принимает
ровно то, что принимает схема. Всё, чего в схеме нет, в этом контракте
отсутствует тоже (З5). Схема вводит дискриминатор `subject`:

| `subject` | Ключ объекта | Что исполняет ядро |
|---|---|---|
| `process` (по умолчанию) | `process` | движок процесса в песочнице (п.10, P013) — без изменений |
| `rule` | `rule` — ключ `WorkRule` пакета | вычисление правила кодом `rules` ядра |
| `taskType` | `taskType` — ключ `TaskType` пакета | исходы гейтов, работа после завершения и приёмка кодом ядра |

Файл без `subject` остаётся тестом процесса, как прежде: существующие тесты
не меняются. Объект теста должен быть объектом пакета (находки
`unknown_test_rule`, `unknown_test_task_type`, как `unknown_test_process`).
Каталог tenant'а лежит под пакетом (P013): правило пакета видит типы задач,
скиллы, роли и агентов и пакета, и tenant'а.

Форма файла совпадает с plan Р6 (суперпроект, `9910218`) и со схемой:

```yaml
# tests/claim-reopened.test.yaml
subject: rule
rule: claim-reopened
name: повторное обращение по закрытой претензии заводит разбор ответственному
given:
  observation:
    kind: helpdesk.ticket_reopened
    data: {ticketId: "T-1", claimKey: "T-1", text: "…"}
mocks:
  skills:
    claims.classify@1:
      - output: {category: complaint, confidence: 0.91}
steps:
  - expect:
      result: matched
      ensureWork:
        - type: claim-review
          customFields: {ticketId: "T-1", category: complaint}
```

```yaml
# tests/refund-approved.test.yaml
subject: taskType
taskType: refund-approval
name: одобренный возврат вызывает скилл выплаты и закрывает задачу
given:
  task: {customFields: {amount: 72000, ticketId: "T-1"}}
mocks:
  skills:
    helpdesk.reply@1:
      - output: {sent: true}
steps:
  - approve: {gate: default, decision: approved}
  - expect:
      invokeSkill: [{skill: helpdesk.reply@1, inputs: {ticketId: "T-1"}}]
      status: {category: terminal_success}
```

Заглушка `helpdesk.reply@1` в этом примере добавлена при реализации (S020).
Грамматика исходов закрывает задачу только в `onSuccess`/`onFailure` своего
`invokeSkill` (ADR-0061): без ответа скилла задача остаётся открытой, и
ожидание `status: terminal_success` провалилось бы.

**Общее для всех субъектов** (верхний уровень схемы): обязательны `name` и
`steps`. Необязательны `description`, `mocks` (`skills`, `agents`, `recall`)
и `coverage.minimum`. `version` относится только к процессу. У `rule` и
`taskType` исполняется только `mocks.skills`: `agents` и `recall` им не
нужны. Схема v1 эти поля у `rule` и `taskType` не запрещает, но молча
пропускать их ядро не должно: автор решил бы, что заглушка или версия
действуют. Поэтому `version`, `mocks.agents` и `mocks.recall` в тесте `rule`
или `taskType` дают находку `test_field_ignored` уровня `warning` (`path` —
поле, `file` — файл теста, `hint` — «поле действует только для
`subject: process`»). Тест при этом исполняется, `ok` пакета находка не
меняет. Запретить поля может правка схемы в package-sdk (З5).

**`given` субъекта `rule`** (`$defs.ruleGiven`):

- ровно одно из трёх:
  - `observation {kind, data?, content?, source?, externalRef?}` —
    наблюдение, которое ядро записывает командой наблюдений (событие
    `observation.recorded`);
  - `event {type, payload?}` — событие журнала;
  - `schedule {at?}` — срабатывание расписания правила (З8);
- `task {type, title?, status?, assignee?, customFields?}` — задача,
  заведённая до входа (З8);
- `clock` — начальное виртуальное время;
- `variables` — значения переменных установки (`${…}` в объектах пакета).
  Переменные, которых нет, берут `default` из манифеста.

Вход у теста правила один — тот, что в `given`. Шаг у `rule` тоже один вид
— `expect` (`$defs.ruleStep`):

- `result` — исход вычисления в словаре события `rule.evaluated`
  (`matched`, `not_matched`, …);
- `ensureWork: [{type?, title?, assignee?, customFields?, relation?}]` —
  работа, которую завело правило. Элемент сравнивается частично: заданные
  поля сравниваются, незаданные не проверяются. Число элементов должно
  совпасть, поэтому `ensureWork: []` значит «ничего не заведено»;
- `invokeSkill: [{skill, inputs?}]` — вызовы скиллов, `inputs` сравниваются
  частично;
- `noSideEffects: true` — ничего не вышло за транзакцию теста: не было ни
  попытки исходящего вызова (З2), ни записи, которую не отменит откат.
  Запись в базу внутри транзакции побочным эффектом не считается — её
  отменяет откат.

Несколько `expect` подряд проверяют одно и то же вычисление: каждый
сравнивает всё, что накоплено с начала теста.

**`given` субъекта `taskType`** (`$defs.taskTypeGiven`):

- `task {title?, status?, assignee?, customFields?}` — задача последней
  версии типа из пакета;
- `artifacts: [{type, key?, metadata?, content?, mediaType?}]` — артефакты
  задачи, которые читают исходы и приёмка; `content` и `mediaType` —
  только вместе (З8);
- `principals` — роль → вымышленные principal'ы теста, как у процесса;
- `clock`, `variables` — как у `rule`.

**Шаги `taskType`** (`$defs.taskTypeStep`, ровно одно поле в шаге):

- `approve {gate?, decision: approved|rejected, by?, comment?}`. `gate` по
  умолчанию — `default`. Если гейт ещё не открыт, тест его открывает
  (approval-гейт на задаче от principal'а теста) и затем решает. Отказ
  команды решения — например, `approval_precondition_failed` — это провал
  теста с кодом отказа.
- `verify {check, result: passed|failed, output?}` — итог критерия приёмки
  `check` текущей попытки. Ядро записывает его тем путём, которым
  записывается итог проверяющего (ADR-0067), и сам критерий не исполняет.
  Так закрываются критерии `human` и `llm_judge` и любой критерий, у
  которого нет заглушки скилла. Критерий со скиллом, у которого заглушка
  есть, исполняется сам (З2).
- `complete {output?}` — завершение задачи: `output` проверяется по
  `completionSchema`, затем исполняются работа после завершения и приёмка
  (ADR-0067).
- `expect`:
  - `ensureWork`, `invokeSkill` и `noSideEffects` — как у `rule`, но
    считаются с прошлого `expect`;
  - `status {key? | category?}` — статус задачи;
  - `comments: [<подстрока>]` — комментарии, которые оставили исходы и
    работа после завершения.

`decision: approved | rejected` у `taskType` называет исход гейта, действия
которого исполняются, а не голос согласующего. У процесса
`approve.decision` остаётся `approve | reject` (`$defs.testStep`).

### З2. Исполнение: тот же код в откатываемой транзакции

Правило и тип задачи исполняются **прикладным кодом ядра**, а не моделью в
памяти. Песочница процесса (P013) здесь не подходит: исходы и правила
пишут командами ядра (`create_task`, `request_approval`, `invoke_skill`,
`complete_task`), и повторить их в памяти значило бы держать второй движок
(plan Р6, «Отвергнуто»). Поэтому:

- **Транзакция теста пишущая и всегда откатывается.** Она не `READ ONLY`,
  как у процессов, и никогда не фиксируется. Каждый тест идёт в своей
  транзакции, которая откатывается после теста, поэтому тесты не видят друг
  друга (уточнено З7: прежде — `SAVEPOINT` в одной транзакции на запрос).
  Тесты процессов идут, как прежде, в своей транзакции `READ ONLY`.
- **Объекты пакета публикуются внутри транзакции** теми же командами, что у
  установщика: версия `TaskType` — командой типов задач, `WorkRule` —
  `create_rule` или `update_rule` с `status: enabled`, агент личности правила
  — `publish_agent` и связка. Каждый объект публикуется в своей точке
  сохранения (`SAVEPOINT`), в порядке применения: тип задачи и агент раньше
  правила, которое на них ссылается. Отказ команды — находка объекта с кодом
  и сообщением команды и путём из `details`: формы — `invalid_rule`,
  `invalid_task_type`, `invalid_agent`, как у плана (TASK-000903). Тест
  объекта с находкой и тесты, которым этот объект нужен, получают
  `status: error` и не идут.
- **Права — как у плана, а не только `packages.test`.** Команды каталога
  проверяют свои права от вызывающего `packages:test`:
  - `task_types.manage` — тип задачи;
  - `rules.write` — правило;
  - `agents.manage` и проверку «не шире прав применяющего» — агент (ADR-0073
    п.5, `permission_escalation`).

  Отказ в праве не превращается в `403` маршрута. Это предупреждение
  `permission_required` на объекте (`hint` — недостающие права), и
  публикация дальше не идёт, как у пробы команд в плане (TASK-000903,
  п.11 «Проба команд в плане», Е6): дальше пошли бы ложные находки.
  Тесты `rule` и `taskType` при этом получают `status: error` с тем же
  кодом, а тесты процессов идут как прежде. Причина проста: откат не делает
  публикацию безопасной для любого вызывающего. Тест исполняет правило и
  исходы от principal'ов с правами из описаний (ниже), и без этих проверок
  `packages.test` давал бы исполнить действия с правами, которых у
  вызывающего нет. Отдельного права «тестировать с правами вида» нет, как
  нет его у плана.
- **Вход и вычисление.**
  - `rule`: наблюдение или событие записывается обычной командой. Затем
    вычисляется **только правило под тестом** — функцией, которой воркер
    вычисляет одно правило на одном событии (`rule_evaluations`), без
    курсора журнала. Прочие правила tenant'а и циклы воркера не
    запускаются.
  - `taskType`: решение гейта — командой решения. Исполнение исхода —
    `execute_outcome` (как у воркера, синхронно в той же транзакции), работа
    после завершения — `file_completion_work`, приёмка — `open_verification`
    и `execute_verification`.
  - Ждущее скилла тест доводит сам, как только есть ответ заглушки.
    Ожидание по времени (отложенный исход, повтор с задержкой, срок
    приёмки) в v1 не сдвигается: шага `advance` у `rule` и `taskType` в
    схеме нет (З5), и тест видит состояние на `given.clock`.
- **Скиллы — только заглушки.** Интерпретация правила, `invokeSkill` исхода
  и проверка приёмки со скиллом ставят `skill_invocation` обычным путём.
  Исполнителя у вызова в тесте нет. Ответ даёт заглушка
  `mocks.skills["name@version"]`: `output` проверяется по `outputSchema`
  скилла (не по схеме — провал теста, как у процесса), `error` и `timeout`
  дают соответствующий исход. Вызов завершается тем же путём, что и ответ
  исполнителя (`skill.invocation_succeeded` / `failed`), поэтому исполняются
  реакции `onSuccess` и `onFailure`, а правило возобновляется. Без заглушки
  вызов остаётся без ответа: исход `waiting` или `deferred`.
- **Исходящих вызовов нет.** У транзакции теста нет клиентов наружу:
  - хранилище содержимого артефактов (результат скилла как артефакт)
    подменяется хранилищем в памяти запроса;
  - HTTP, память и PDP недоступны. Любая попытка — ошибка теста
    `sandbox_outgoing_call`;
  - авторизация идёт локальным авторизатором (`authorize` в режиме
    `local`): временных principal'ов теста PDP не знает. Исключение —
    чтение от имени вызывающего в режиме `policy` (З7).

  Единственный исходящий вызов маршрута — проверка `governedBy` после
  закрытия транзакции — остаётся, как был (P013).
- **Полномочия — как на стенде**, но у principal'ов теста. Временные
  principal'ы заводятся в той же транзакции и исчезают с откатом:
  - правило с `identity.agent` исполняется от principal'а этого агента.
    Агента без связки тест связывает временной IAM-идентичностью с правами
    его описания (пакета или каталога);
  - правило без личности исполняется от вызывающего `packages:test`, как на
    стенде от того, кто его включил. Это оговаривается в руководстве;
  - решение гейта принимает `by` — principal теста с ролями из
    `given.principals` (только у `taskType`). Без `by` решает principal
    с ролью `approverRole` гейта. Если роли нет, решает principal с правом `approvals.decide` на
    workspace задачи;
  - завершение (`complete`) исполняется от `given.task.assignee`, если он
    principal теста, иначе от вызывающего.
- **Блокировки.** Незафиксированная запись держит блокировки до отката.
  Поэтому:
  - номер задачи (`next_public_id`: строка `task_counters` tenant'а) в тесте
    выдаёт последовательность запроса (`TEST-000001`, …), а не счётчик.
    Иначе прогон тестов останавливал бы заведение задач всего tenant'а;
  - транзакция ставит `SET LOCAL lock_timeout = '2s'`. Ключ правила или типа,
    который сейчас правят на стенде, даёт тесту `error` с кодом
    `lock_timeout` — это не провал пакета;
  - события и outbox пишутся и откатываются вместе с остальным.
    Уведомлений `NOTIFY` при откате нет, и потребители ничего не видят.
- **Время.** Часы прикладного слоя в транзакции теста — виртуальное время
  теста (`given.clock`, по умолчанию `2026-01-05T09:00:00Z`, как у процесса).
  Ответ теста не зависит от часов стенда.
- **Следа нет.** Интеграционный тест S020 сверяет все таблицы базы построчно
  до и после прогона — так же, как у процессов (P013).

### З3. Покрытие

У каждого `WorkRule` и у каждого `TaskType` пакета с гейтами, работой после
завершения или приёмкой есть строка покрытия, в том числе у объекта без
тестов (`tests: 0`). По этому списку SDK строит отчёт SC-008. У каждого
счётчика — `{covered, total, missing}`, как у процессов:

- **правило**:
  - `branches` — каждый операнд `and`/`or`, `not` и сравнение в `condition`
    и `where` вычислены и в `true`, и в `false`. Id — JSON-указатель с
    исходом: `/condition/and/1:true`;
  - `outcomes` — `matched`, `not_matched`, а при `interpretation` ещё
    `interpretation:answered` и `interpretation:failed`;
- **тип задачи**:
  - `outcomes` — объявленные исходы гейтов `<гейт>/<исход>`
    (`default/approved`) и их реакции `<гейт>/<исход>/<i>/onSuccess|onFailure`;
  - `preconditions` — `<гейт>/preconditions/approved/<i>:held|refused`;
  - `completion` — `completion/<i>`;
  - `acceptance` — `acceptance/<ключ>:passed|failed`.

`coverage.minimum` теста — порог доли ветвей правила или исходов типа этим
тестом.

### З4. Ответ и запрос

Запрос `POST /packages:test` не меняется: `PackageTestRequest {package,
tests?, workspaceId?}`, `?checkOnly`, право `packages.test`.

- В `checkOnly` и перед тестами проверяются по форме и словарю и документы
  `WorkRule` и `TaskType` пакета — теми же чистыми функциями домена
  (`domain/work_rules.py`, `domain/approval_outcomes.py`, схема полей типа).
  Это находки `invalid_rule` и `invalid_task_type` с файлом и строкой.
  Раньше прочие виды здесь не сверялись (P013). Ссылки на объекты
  проверяются чтением каталога. Проверка без тестов по-прежнему идёт только
  на чтение.
- `PackageTestResultOut` получает `subject` и `object` (ключ объекта теста).
  `process` заполнен только у `subject: process`, у прочих — `null`.
  Ответы на пакеты с одними тестами процессов прежние.
- `PackageTestOut` получает `ruleCoverage` и `taskTypeCoverage`. Прежнее
  `coverage` остаётся покрытием процессов.

OpenAPI (фрагмент, S020):

```yaml
components:
  schemas:
    PackageTestResultOut:
      type: object
      required: [file, name, subject, object, status, durationMs, failures]
      properties:
        file: {type: string}
        name: {type: string}
        subject: {enum: [process, rule, taskType]}
        object: {type: string, description: 'Key of the process, rule or task type under test'}
        process:
          type: [string, 'null']
          description: 'Key of the process; null for rule and taskType tests'
        status: {enum: [passed, failed, error]}
        durationMs: {type: integer}
        failures:
          type: array
          items: {$ref: '#/components/schemas/PackageTestFailureOut'}
    RuleCoverageOut:
      type: object
      required: [rule, tests, branches, outcomes]
      properties:
        rule: {type: string}
        tests: {type: integer, minimum: 0}
        branches: {$ref: '#/components/schemas/CoverageCounterOut'}
        outcomes: {$ref: '#/components/schemas/CoverageCounterOut'}
    TaskTypeCoverageOut:
      type: object
      required: [taskType, version, tests, outcomes, preconditions, completion, acceptance]
      properties:
        taskType: {type: string}
        version: {type: integer}
        tests: {type: integer, minimum: 0}
        outcomes: {$ref: '#/components/schemas/CoverageCounterOut'}
        preconditions: {$ref: '#/components/schemas/CoverageCounterOut'}
        completion: {$ref: '#/components/schemas/CoverageCounterOut'}
        acceptance: {$ref: '#/components/schemas/CoverageCounterOut'}
    PackageTestOut:
      type: object
      required: [status, checkOnly, problems, tests, coverage,
                 ruleCoverage, taskTypeCoverage, durationMs]
      properties:
        # status, checkOnly, problems, tests, coverage, durationMs — as before
        ruleCoverage:
          type: array
          items: {$ref: '#/components/schemas/RuleCoverageOut'}
        taskTypeCoverage:
          type: array
          items: {$ref: '#/components/schemas/TaskTypeCoverageOut'}
```

`test.schema.json` (S003, package-sdk) вводит `subject` через `allOf` из
трёх `if`/`then`. На верхнем уровне обязательны `name` и `steps`, `subject`
необязателен (`default: process`):

- без `subject` или с `subject: process` — `required: [process]`, `given`
  по `$defs.processGiven`, шаги по `$defs.testStep`;
- `subject: rule` — `required: [rule]`, `$defs.ruleGiven`, `$defs.ruleStep`;
- `subject: taskType` — `required: [taskType]`, `$defs.taskTypeGiven`,
  `$defs.taskTypeStep`.

Копию ядра обновляют из package-sdk, и contract-тест держит её равной
закреплённой.

### З5. Чего нет

- Правило не исполняется «по-настоящему» на стенде. Тест не публикует правило
  и ничего не оставляет: откат — часть контракта, а не оптимизация.
- Каскада правил нет: работа, заведённая правилом под тестом, не будит другие
  правила tenant'а. Цепочку правил проверяет сценарий на стенде, а не
  `packages:test`.
- Тест типа задачи не запускает исполнителя задачи: прогон, claim и
  `start-run` не моделируются. Задача завершается шагом `complete`.
- Того, чего нет в схеме v1 (S003), нет и в контракте ядра. У `rule` нет
  второго входа (`emit`): вход у теста правила один. Срабатывание по
  расписанию (`given.schedule`) и задача, заведённая до входа
  (`given.task`), с амендментом 2026-09-30 есть (З8). Работа, заведённая
  прежде самим правилом (с его `dedupKey`), в `given` по-прежнему не
  задаётся, поэтому идемпотентность `ensure_work` по `dedupKey`,
  `update_work`, `cancel_work` и `complete_work` тестом пакета не
  проверяется. У `rule` и `taskType` нет `advance`, `expectRefused`,
  ожиданий `changedWork`, `approvals`, `outcome`, `verification` и `error`, а
  у `taskType` — задачи-родителя в `given`. Если такие поля понадобятся, их
  сначала добавляют в схему отдельной задачей package-sdk, и только потом
  ядро их принимает. Исключение — поля З8: их грамматику предложило ядро,
  и package-sdk принимает её задачей-парой (З8, «Схема»).
- Обратное тоже верно: схема v1 принимает у `rule` и `taskType` `version`,
  `mocks.agents` и `mocks.recall`, а ядро их не исполняет. Ядро их не
  отвергает (иначе отвергло бы принятое схемой), а предупреждает
  `test_field_ignored` (З1). Сузить схему `if subject … then not` —
  решение package-sdk.

### З6. Реализация (S020, TASK-000944)

Код — `application/commands/package_trials.py`, крючки процесса на время
теста — `control_plane/sandbox.py`. Решения, которых З1–З5 не фиксировали:

- **Что видит тест.** Всё, что тест сделал, — события его транзакции
  (`events.tx_id` = `pg_current_xact_id()`). `ensureWork` сравнивается с
  задачами из `task.created`: `{type, title, assignee, customFields,
  relation}`. Здесь `assignee` — имя principal'а теста, `agent:<key>` или
  `role:<slug>` требования задачи, а `relation` — `{spawnedBy|dependsOn:
  <publicId>}`. `invokeSkill` сравнивается с `skill.invocation_requested`
  и входом вызова. Задача под тестом (`taskType`) и то, что записало её
  заведение из `given`, в `ensureWork` не входят.
- **Заглушка отвечает как исполнитель.** Вызов берёт исполнитель-заглушка
  теста: строка вызова получает аренду, как при `:claim`. Затем ответ
  записывается командами исполнителя `complete`/`fail`. Выбор ответа — как
  у процесса: по порядку вызовов среди ответов, чей `when` держится;
  последний повторяется. `timeout` — ответа нет. После каждого шага тест
  доводит до покоя всё ждущее: исход approval, попытку проверки,
  вычисление правила, вызовы с заглушкой. Покой — когда журнал транзакции и
  попытка проверки перестали меняться.
- **Переменные установки.** Значение — из `given.variables`, иначе
  `default` манифеста (`spec.variables`). Объект, в котором осталась
  переменная без значения, в тесте не публикуется. Если он нужен тесту (это
  объект теста или объект называет его ключ), тест получает `error`
  `unresolved_install_variable` с именами переменных. Проверка формы (З4)
  такой объект пропускает: его форма зависит от значения. `workspaceId`
  вида `${…}` у правила — workspace запроса, как у процесса и плана.
- **Что ещё публикуется.** Тип артефакта, роль и скилл пакета, которых нет
  у tenant'а, заводятся командами своих маршрутов раньше типов задач и
  правил: типу задачи нужен скилл исхода, правилу — скилл интерпретации.
  Существующие ключ или `name@version` берутся из каталога как есть. Права
  этих команд (`artifact_types.manage`, `org.manage`) проверяются так же:
  без них — предупреждение `permission_required`.
- **Находки публикации.** Отказ команды — находка объекта. Тест получает
  `error`, если это объект теста или объект, который он называет по ключу.
  Правило пакета со `status: disabled` публикуется включённым. Правило без
  `identity` действует от вызывающего: если ключ уже включил другой
  principal, тест выключает правило и включает его заново. Агент без
  связки получает временную IAM-идентичность (issuer
  `control-plane:package-test`).
- **Principal'ы теста.** Имена из `given.principals`, `approve.by` и
  `given.task.assignee` — временные principal'ы вида `human` с ключом API.
  У них права человека, работающего с задачами: `tasks.read|write`,
  `approvals.read|decide|manage`, `skills.invoke`, `artifacts.read|write`,
  `observations.write`, `events.read`. Роль из `given.principals` берётся
  у tenant'а по slug, иначе заводится временная. Наблюдение `given`
  записывает временный principal с `observations.write` в workspace правила.
  Событие `given.event` записывается в журнал как есть: сущность — первое
  слово типа, её id — `<сущность>Id` или `id` из `payload`. Тип не из
  каталога событий — `error`.
- **Шаги `taskType`.**
  - `approve` без открытого гейта открывает его на решающего, а без `by`
    решает principal теста `approver`. `by` решает с ролями из
    `given.principals`, и может ли он решать, отвечает ядро. Гейт, который
    открыли исходы или проверка, без `by` решает его назначенный principal,
    а при `approverRole` — principal теста `approver`, которому выдана эта
    роль. Гейт — только `default`:
    ядро других не исполняет (ADR-0061).
  - `verify` пишет итог критерия так, как его записал бы проверяющий:
    - `human`/`llm_judge` — решение гейта, который попытка запросила;
    - скилл без заглушки — ответ на его вызов: `output`, иначе `expect`
      критерия; `failed` — отказ вызова;
    - факт (`external_state`) — `passed` пишет задаче evidence с `check`;
      `failed` доводит попытку с истёкшим ожиданием, `no_result`;
    - критерий артефакта решают артефакты задачи из `given.artifacts`, и
      `verify` для него — провал шага.
  - `complete.output` пишется в `customFields` задачи
    (`PATCH`-путь, проверка по `fieldSchema` типа), затем задача
    завершается `:complete` от её исполнителя-principal'а теста или от
    вызывающего. `completionSchema` ядра — работа после завершения, а не
    схема выхода. Выход задачи ядро держит в её полях, поэтому «проверяется по
    `completionSchema`» (З1) читается как «видим выражениям
    `completionSchema` через `$.task.customFields`».
- **Правило не сработало.** Если вход не подходит под `trigger` правила,
  воркер его не вычислил бы. Тест в этом случае проваливается сообщением
  «the input does not fire the rule» и `result` не получает.
- **Ошибки теста** — `status: error` и сообщение с кодом в начале:
  `permission_required`, `unresolved_install_variable`,
  `sandbox_outgoing_call`, `lock_timeout` (SQLSTATE 55P03 под
  `SET LOCAL lock_timeout = '2s'`).
- **Версия в покрытии типа** — версия, которую тип получил бы при apply
  (`package_catalog.planned_version`).
- **Проверка формы (З4)** теперь идёт для любого пакета, и с одними
  процессами тоже. `TaskType` без `displayName` или с документом, который
  отверг бы `POST /task-types`, даёт `invalid_task_type` и `status: invalid`.
  Проверка идёт теми же функциями, что у маршрута:
  `task_types.check_type_documents` вынесена из
  `create_task_type_version`.

### З7. Замечания ревью S020 (TASK-001066)

- **Транзакция на тест.** Пишущая транзакция держит `pg_snapshot_xmin`.
  Журнал читается только ниже этого горизонта (`tx_id < xmin`,
  `event_cursor`), поэтому, пока транзакция открыта, никакое событие базы,
  зафиксированное после её начала, не доставляется. Это касается правил,
  процессов, уведомлений, fleet и `GET /events`. Одна транзакция на весь
  запрос задерживала доставку на весь прогон, поэтому теперь у каждого теста
  своя сессия и транзакция, откат — сразу после теста. Задержка ограничена
  одним тестом и тремя пределами:
  - `MAX_SUBJECT_TESTS` (100) тестов `rule` и `taskType` на запрос. Больше —
    находка `too_many_tests` (`error`, путь `/tests`, запрос `invalid`, тесты
    не идут); `hint` предлагает выбрать файлы в `tests`;
  - `SET LOCAL statement_timeout = 5000` у каждой транзакции теста.
    Превышение — `error` теста с кодом `statement_timeout` (SQLSTATE 57014);
  - 30 с на тест, проверяются между шагами и кругами доведения до покоя.
    Превышение — `error` теста `test_timeout`.

  Версии типов для покрытия читаются после тестов отдельной транзакцией
  `READ ONLY`.
- **PDP для чтений вызывающего.** В режиме `policy` правило без личности
  действует от вызывающего (З2), и на стенде его чтения решает PDP. Прежний
  локальный авторизатор в тесте решал их по плоским правам. Тест правила,
  в `given.event` которого id задачи чужого workspace, читал эту задачу, и её
  поля попадали в `actual`. Теперь внутри теста PDP решает чтение, если
  выполнены три условия:
  - действие — чтение (`*.read`, все из `any_of`);
  - IAM-субъект — субъект вызывающего (`Trial.policy_subjects`);
  - ресурс — не строка, которую вставил сам тест. Id вставленных строк
    собирает `after_flush` сессии теста (`Trial.written`), и PDP о них не
    знает.

  То же относится к `visible_objects`. Такой вопрос — единственный
  исходящий вызов, который тест пропускает (`sandbox.permit("policy")`).
  Всё остальное решает локальный авторизатор, как прежде: записи
  вызывающего (публикацию проверяют права вида, З2), действия
  principal'ов теста, объекты теста. В режимах `local` и `shadow` тест не
  спрашивает PDP.
- **Фильтры воркера правил.** Перед вычислением тест применяет те же
  проверки, что цикл воркера (`process_tenant_events`):
  - `_caused_by_rules` — вход, который сам записан правилом (сущность
    `rule`, корреляция `rule:`, вызов с ключом правила);
  - `enabled_at` — событие раньше включения правила;
  - `_outside_workspace` — `payload.workspaceId` называет не workspace
    правила. Если `workspaceId` нет, тест подставляет workspace правила, как
    прежде.

  Отфильтрованный вход правило не вычисляет. Отчёт получает
  предупреждение `input_not_delivered` (путь `/given/event` или
  `/given/observation`) с причиной. `expect.result` проваливается
  сообщением «the rule is not evaluated: <причина>» и `actual.result: null`.
  `ensureWork: []` и `invokeSkill: []` держатся.
- **Права внутри отката шире вызывающего.** Два места намеренно действуют
  не правами вызывающего:
  - `_link_agent` связывает агента пакета без связки временной
    IAM-идентичностью. Команда связки проверяет `agents.status.write`, и
    тест даёт это право контексту вызывающего только на эту команду. Права
    самой связки — права описания агента. Их потолок у вызывающего уже
    проверила публикация агента (`agents.manage`, «не шире прав
    применяющего», ADR-0073 п.5);
  - `as_principal` решает гейт, назначенный principal'у стенда. Тест
    заводит этому principal'у временный ключ API с правами человека теста
    (`PRINCIPAL_PERMISSIONS`), и решение принимается от его имени, а не от
    вызывающего.

  Оба действия допустимы по трём причинам. Всё записанное откатывается.
  Исходящих вызовов нет. В отчёт попадает только журнал транзакции теста:
  задачи, вызовы и строки, на которые он указывает. Остаётся расхождение:
  чтения principal'ов теста и агента со временной связкой идут по плоским
  правам, а не через PDP, потому что их субъектов PDP не знает.
- **`unmocked_skill_call`.** Вызов скилла без заглушки остаётся без ответа
  (З2). Если после шагов теста правило осталось в `waiting` и для скилла
  вызова, которого оно ждёт, у теста нет ни одной заглушки, отчёт получает
  предупреждение `unmocked_skill_call` (путь `/mocks/skills`, `hint` — ключ
  заглушки). Ответ `timeout: true` и ответ,
  чей `when` не подошёл, — намеренные и предупреждения не дают. Попутно
  исправлено: сессия теста без autoflush, и `refresh` вычисления без `flush`
  терял записанное в памяти `waiting`. Правило без ответа выглядело
  `failed`.
- **Закреплённая схема теста.** Копия `test.schema.json` package-sdk — одна,
  `tests/fixtures/superproject/test.schema.json`, в перечне `PINNED_NAMES`
  механизма `tests/package_sdk.py` (TASK-001029). Копию ядра
  (`domain/package_test.schema.json`) тест сверяет с ней всегда
  (`test_the_core_holds_the_superproject_test_schema`), а закреплённую — с
  package-sdk (`test_the_pinned_schemas_are_the_package_sdk_ones`): рядом —
  сверка, в зонтике без сабмодуля — ошибка, вне зонтика — пропуск.

### З8. Обстановка теста (амендмент 2026-09-30, TASK-001188)

S023 (сценарии правил и типов задач пакетов платформы, TASK-000947) не смог
записать тестом часть правил и исходов: песочница не давала им обстановку,
которую они читают на стенде. Список `GAPS` сценариев S023 закрывают пять
решений.

- **Переменные вида `principal`, `role`, `workspace`.** Вид переменной —
  `spec.variables.<ИМЯ>.kind` манифеста (`packageVariable` схемы каталога).
  Значение такой переменной на стенде — UUID строки стенда, а в тесте
  этой строки нет, и команда ядра отвечала `Principal not found`. Теперь
  перед публикацией тест подставляет строку теста; значение берётся из
  `given.variables`, иначе из `default` манифеста:
  - `principal` — **всегда** principal теста: с именем-значением, а без
    значения или при значении-UUID — с именем переменной. UUID principal'а
    tenant'а тоже не берётся: сценарий в git не должен зависеть от стенда,
    на котором его гоняют. Это тот же principal, что под этим именем в
    `given.principals`, `approve.by` и `given.task.assignee`; в
    `ensureWork.assignee` его видно по имени;
  - `role` — id роли tenant'а остаётся как есть. Иначе — роль со
    slug-значением, без значения slug — имя переменной в нижнем регистре,
    `_` → `-`. Если такой slug объявляет `Role` самого пакета, переменная
    его не занимает: сначала `Role` пакета публикуется своей командой
    (`POST /roles`, её `name` и `description`), затем переменная получает
    id этой роли. Иначе — роль tenant'а с этим slug, а если её нет —
    временная роль теста;
  - `workspace` — id workspace tenant'а остаётся как есть, если
    вызывающий может читать его процессы (`processes.read` на workspace —
    то же, что спрашивается у `workspaceId` запроса; в режиме `policy`
    решает PDP, З7). Иначе — workspace теста (ниже). Workspace, который
    вызывающему не виден, неотличим от несуществующего: оба дают
    workspace теста без находки (TASK-001204).

  Вид `project` не подставляется:
  временного проекта тест не заводит, и S023 он не нужен.
  `given.principals` у `rule` в схеме нет: principal правила задаёт
  переменная.
- **Workspace теста.** Каждый тест `rule` и `taskType` идёт в workspace
  запроса (`workspaceId`), а без него — во временном workspace, который
  тест заводит в своей транзакции. Туда же попадают задача под тестом и
  задача `given.task`, поэтому `$.task.workspaceId!` исходов разрешается.
  `workspaceId: ${…}` правила, как и прежде, — workspace теста. Workspace
  вставляется строкой, не командой workspaces: команда берёт блокировку
  дерева workspace tenant'а, и тест держал бы её до отката. Slug —
  `package-test-<случайный>`, поэтому параллельные тесты не ждут друг
  друга на уникальном индексе. Тип — системный тип tenant'а.
- **Содержимое артефактов.** `given.artifacts[]` принимает `content` (текст)
  и `mediaType`, только вместе. Пределы (TASK-001204):
  - `content` — не больше 1 МиБ **в байтах UTF-8** и не больше
    `CP_ARTIFACT_MAX_BYTES`. Схема ограничивает `maxLength: 1048576` в
    символах, а символ UTF-8 — до 4 байт, поэтому байты сверяет ядро.
    Больше — `error` с `given_refused: given.artifacts: the content is
    larger than 1048576 bytes`;
  - артефактов в `given.artifacts` — не больше 20 (`maxItems` схемы),
    больше — `invalid_test`. YAML-алиас позволяет сослаться на одну строку
    в 1 МиБ сколько угодно раз при коротком файле, и без предела обстановка
    хэшировала бы гигабайты;
  - между артефактами проверяется `TEST_DEADLINE`, как между шагами:
    обстановка дольше 30 с — `test_timeout`. Содержимое оформляется как загрузка вызывающего
  (`artifact_contents`, `sha256` и размер — настоящие), и артефакт
  заводится командой артефактов с `contentRef`. Команда при этом
  проверяет тип артефакта: media type, размер, `metadataSchema`. Байты в
  хранилище не кладутся: хранилище тесту недоступно (З2), а критерий
  выхода (ADR-0067, CP-ADR-0072 §9) смотрит только записи. Так приёмка по
  форме артефакта (`feature-design`, `feature-tasks`) проходит, а без
  `content` даёт `artifact_content_missing`, как на стенде.
- **`mediaTypes` типа артефакта по умолчанию.** В схеме каталога
  `ArtifactType.spec.mediaTypes` необязателен («по умолчанию любой»), и
  package-sdk без него подставляет `["*/*"]`. `POST /artifact-types`
  требует поле явно, поэтому песочница отвечала на такой тип
  `invalid_artifact_type /spec/mediaTypes: Field required`, и тесты с ним
  (например, `procurement-document` пакета `tenders`) падали. Теперь
  `/packages:test` публикует тип без `mediaTypes` с `["*/*"]`, как
  package-sdk. Явное значение, в том числе пустой список, передаётся как
  есть и проверяется командой. Маршрут `POST /artifact-types` не меняется.
- **Вход — расписание.** `given.schedule {at?}` — третий вход правила,
  взаимоисключающий с `observation` и `event`. Слот падает на `at`, по
  умолчанию — на `given.clock` (или часы по умолчанию). Часы теста
  переводятся на слот, но не назад. Слот исполняет функция воркера
  `run_schedule`: `trigger.scheduledAt` и `trigger.ref` те же, что на
  стенде. Правило не по расписанию на этот вход не срабатывает: «the input
  does not fire the rule», как при несовпадении `trigger`. Фильтры воркера
  (З7) у расписания не применяются: у слота нет события.
- **Задача до входа.** `given.task {type, title?, status?, assignee?,
  customFields?}` у `rule` — задача, заведённая командой задач до входа, в
  workspace правила или теста. Событие `given.event` без `taskId` в
  `payload` — о ней: ядро ставит `payload.taskId`, и сущность события —
  эта задача. Правило читает её как задачу триггера (`task.*`), так
  проверяется `feature-expand`.
  `taskId` в `payload` (а у события `task.*` и `id`) может назвать и
  задачу стенда: `given.task` его не заменяет. Правило читает такую задачу
  от principal'а, от которого исполняется, а агент теста читает локальной
  проверкой (З2). Поэтому перед записью события ядро спрашивает право
  вызывающего `tasks.read` на эту задачу (в режиме `policy` — PDP, З7).
  Задача, созданная самим тестом, не спрашивается. Отказ — `error` с
  `given_refused: given.event.payload.taskId: … permission_denied`.
  Вопрос задаётся об id, а не о строке: отказ не говорит, есть ли такая
  задача (TASK-001204). В `ensureWork` она не входит, как и задача
  под тестом у `taskType`.
- **Отказ обстановки — красный тест.** Если ядро отвергает обстановку
  (заведение `given.task`, его `status`, `given.artifacts[i]`,
  `given.observation`), тест получает `status: error` и сообщение
  `given_refused: <где>: the core refused it: <код>: <сообщение>`, а
  `actual` — `{code, details}`. Любой другой отказ ядра, который не
  превратил в вердикт ни один шаг, тоже даёт `error` («the core refused:
  <код>: …»), а не `500` маршрута.
- **Автор события в шаге `emit` (TASK-001235).** Тест процесса задаёт
  автора события необязательным `emit.by` — непустой строкой, как
  `complete.by` и `approve.by`: principal теста (то же имя, что в
  `given.principals`) или `agent:<key>`. Песочница ставит его в
  `actorId` события и, как ядро на стенде (`actor_id` события журнала),
  передаёт актором входа `start` и `event`, которые событие кормит.
  Так проверяется процесс, который читает `event.actorId` в `start.set`
  или `correlate`. Без `by` поведение прежнее: `actorId` пуст, `event`
  выражения его не содержит. Пустая строка или не строка — находка
  `invalid_test`. Поле только добавлено в схему (одна строка в `emit`,
  без переформатирования), поэтому параллельные правки схемы на
  `feature/package-sdk` сводятся слиянием; package-sdk принимает его
  задачей-парой (абзац «Схема» ниже).

Идемпотентность `ensure_work` по `dedupKey`, то есть работа, которую правило
завело раньше, по-прежнему не задаётся (З5). `given.task` — задача
стенда, а не работа правила: связи с ключом правила у неё нет. Такую
работу можно было бы завести, только повторив вычисление правила, а это
цепочка правил, которую проверяет сценарий на стенде (З5).

**Схема.** Грамматику предлагает ядро: копия ядра
(`domain/package_test.schema.json`) и закреплённая копия
(`tests/fixtures/superproject/test.schema.json`) равны и содержат поля
выше. package-sdk принимает этот файл как есть задачей-парой: до этого в
зонтике не сходится сверка закреплённой копии с package-sdk
(`test_the_pinned_schemas_are_the_package_sdk_ones`), а CLI package-sdk,
который проверяет тесты своей схемой, новые поля не пропускает. Изменения
только добавляют поля: файл, который принимала прежняя схема, новая тоже
принимает.

### Проверки (S012, S020)

- `tests/integration/test_process_retire.py` (S012):
  - живой экземпляр выведенного процесса доживает: таймер, ответ задачи,
    `correlate`;
  - новый экземпляр не стартует ни событием, ни `POST /process-instances`
    (`409 process_retired`), ни `call` (`intent_failed process_retired` у
    родителя);
  - публикация новой версии возвращает ключ;
  - переименование пишет тот же признак;
  - ответ считает живые экземпляры по версиям;
  - повтор идемпотентен, `dryRun` ничего не пишет;
  - проверяются права и события.
- `tests/integration/test_calendar_retire.py` (S012):
  - календарь, нужный последней версии активного процесса или живому
    экземпляру выведенного процесса, не выводится
    (`409 calendar_in_use`, скрытый процесс входит только в `total`);
  - свободный календарь выводится, после чего ссылка на него —
    `calendar_retired`;
  - проверяются права и события.
- `tests/integration/test_migration_*.py` (S012): `package_objects.retired_at`
  переезжает в `catalog_retirements`, откат возвращает колонку.
- `tests/unit/test_process_contract.py` (S012, S020): маршруты `:retire`,
  `status` у версий, `PackageTestOut` с покрытием правил и типов задач в
  OpenAPI; копия `test.schema.json` равна package-sdk.
- `tests/integration/test_package_test.py` (S020):
  - примеры plan Р6 (З1) проходят схему и ядро без изменений; файл, который
    схема отвергает, ядро отвергает находкой;
  - `rule`: наблюдение → условие → интерпретация заглушкой → `ensure_work`
    (`result: matched`); вход событием; `not_matched`; заглушка не по
    схеме — провал; `noSideEffects`; ветви покрытия;
  - `taskType`: одобрение → `invokeSkill` → `onSuccess` → `completeTask`;
    отказ → `ensureWork`; предусловие не держится — провал с кодом
    `approval_precondition_failed`; приёмка со скиллом по заглушке и
    `verify` критерия `human`; работа после завершения (`complete.output`,
    `comments`);
  - права: у вызывающего без `rules.write` (`task_types.manage`,
    `agents.manage`) — `permission_required` на объекте, тесты `rule` и
    `taskType` — `error`, тесты процессов идут;
  - `version`, `mocks.agents`, `mocks.recall` в тесте `rule` и `taskType` —
    предупреждение `test_field_ignored` с путём поля, тест исполняется;
  - **все таблицы базы после прогона построчно те же**, `task_counters` не
    тронут;
  - второй домен — пакет-фикстура оплаты счёта, страж нейтральности зелёный.
- `tests/integration/test_package_test_subjects.py` (З7, TASK-001066):
  - событие стенда, зафиксированное во время первого теста, ниже горизонта
    журнала, как только этот тест откатан, — до конца прогона;
  - `too_many_tests`; выбор файлов в `tests` снимает находку;
  - режим `policy`: задачу чужого workspace правило читает через PDP; отказ
    PDP на задачу `given.event` (`taskId`, а у `task.*` и `id`) — `error`
    с `given_refused` до вычисления, одинаковый для скрытой и
    несуществующей задачи, данных задачи в отчёте нет; строки теста PDP не
    спрашиваются (З8, TASK-001204);
  - событие с `workspaceId` чужого workspace правило не вычисляет —
    `input_not_delivered`;
  - `unmocked_skill_call` у правила, оставшегося `waiting`.
- `tests/integration/test_package_test_setting.py` (З8, TASK-001188):
  - переменная вида principal — principal теста по значению или имени
    переменной, пустое значение — как отсутствующее, id principal'а
    tenant'а и чужой UUID — principal теста с именем переменной; роль по
    slug и по имени, одно имя в переменной и в `given.principals` — один
    principal; slug `Role` пакета переменная не занимает — роль заводит
    `create_role` пакета;
  - тип артефакта без `mediaTypes` принимает любой media type и проходит
    без `invalid_artifact_type`; явный `mediaTypes` по-прежнему
    ограничивает (`given_refused`), пустой список — находка типа;
  - `$.task.workspaceId!` исхода без workspace в запросе и с ним;
    переменная вида workspace; в режиме `policy` она оставляет только
    workspace, процессы которого вызывающий может читать, скрытый и
    несуществующий — workspace теста; параллельные прогоны не ждут друг
    друга;
  - `content` артефакта проходит критерий выхода, без него задача не
    закрывается; media type, которого нет у типа, — `given_refused`;
    `content` без `mediaType` — `invalid_test`; ровно 1 МиБ проходит, 1 МиБ
    символов больше 1 МиБ байт — `given_refused`; 21 артефакт —
    `invalid_test`; после `TEST_DEADLINE` артефакты не заводятся;
  - слот расписания в `at` и в `given.clock`, покрытие исходов; расписание
    у правила событий не срабатывает;
  - событие о задаче `given.task`: `matched` с `spawnedBy`, другой тип —
    `not_matched`, задача обстановки не в `ensureWork`;
  - отказ `given.task`, его `status` и типа — `error` с `given_refused`;
  - таблицы базы после прогона те же.
- `emit.by` (З8, TASK-001235): `tests/unit/test_package_test.py` — автор
  в `event.actorId` у `start.set` и `correlate`, principal и
  `agent:<key>`; актор входов `start` и `event` в журнале песочницы; без
  `by` — прежнее поведение; схема принимает непустую строку и отвергает
  пустую, `null`, число, список. `tests/integration/test_package_test.py`
  — то же через `POST /packages:test`: с `by` тест проходит, без него
  экземпляр падает на `string(event.actorId)`, пустой и нестроковый
  `by` — `invalid_test`.
- `tests/unit/test_authorizer.py` (З7): внутри теста PDP спрашивается только
  о чтениях вызывающего не своих строк; прочие исходящие вызовы
  отвергаются.

## Амендмент 2026-09-30 (TASK-001197): свежая установка, время теста правила, основание шага процесса

Найдено на сквозном примере «претензии» (S027, TASK-000951): пакет из
примера не ставился на свежий стенд одним планом, сценарии его правил,
запущенные сервером на стенде с установленным правилом, падали, а шаг
процесса не мог вызвать скилл `external_write`.

### И1. Проба плана видит вспомогательные объекты своего пакета

Проба команд плана (п.11, «Проба команд в плане») публиковала тип задачи,
агента и правило в откатываемой транзакции, но сверяла их только с тем,
что уже есть на стенде. `ArtifactType`, `Role` и `Skill` план не применяет
(`outside`, их ставит установщик), и пакет, который приносит скилл и сам
же зовёт его в правиле (`interpretation.skill`) или исходе гейта
(`invokeSkill`), на свежем стенде получал `unknown_skill` и
`invalid_approval_schema`; тип задачи с `artifactSchema` на тип артефакта
пакета — `unknown_artifact_type`.

- Перед командами типов, агентов и правил проба публикует в той же
  откатываемой транзакции типы артефактов, роли и скиллы пакета, которых у
  tenant'а нет, — командами их маршрутов, каждый в своей точке сохранения,
  в порядке `ArtifactType`, `Role`, `Skill`. Это та же функция, что у
  тестов `rule` и `taskType` (З2, `package_trials.publish_supporting`).
  Существующий ключ (скилл — `name@version`) не трогается.
- Находки формы такого объекта — предупреждения на его файле (план его не
  применяет). Объекты, которые на него ссылаются, получают свою ошибку, как
  раньше. Отказ в праве вида (`org.manage` у ролей и скиллов,
  `artifact_types.manage` у типов артефактов) — предупреждение
  `permission_required` на объекте. Объекты пакета, которые называют его
  ключ (строка спецификации — ключ или `ключ@версия`), не пробуются и
  получают то же предупреждение; остальные пробуются как обычно (правка по
  ревью). Отказ в праве типа, агента или правила по-прежнему останавливает
  пробу.
- Хэш плана не меняется: вспомогательные объекты в этаг не входят, как и
  раньше. Применение их не публикует. Порядок установки одним планом
  такой: `packages:plan` → установщик ставит `outside` своими маршрутами →
  `packages:apply` с тем же `planHash`. Применение раньше установщика
  отказывает ошибкой команды первого объекта (например, `422
  unknown_artifact_type` или `invalid_approval_schema`) и ничего не пишет.
  Процессы и раньше сверялись с каталогом пакета (`overlay_catalog`).

### И2. Правило в тесте включено с начала времени теста

Тест `rule` публикует правило в откатываемой транзакции (З2). Правило,
уже установленное на стенде, при этом только обновляется, и его
`enabled_at` остаётся реальным моментом включения. Часы теста по умолчанию
(`2026-01-05T09:00Z`) раньше этого момента, поэтому вход теста
пропускался как `input_not_delivered` («occurred before the rule was
enabled»). Теперь правило под тестом считается включённым с начала
времени теста (`given.clock` или часов по умолчанию): более поздний
`enabled_at` переносится на это начало. Перенос откатывается вместе с
тестом, правило стенда остаётся как было. Часы, заданные позже включения,
ничего не меняют.

### И3. Шаг процесса — основание `external_write`

Вызов скилла шагом процесса (`call.skill`, п.8 «Скилл») идёт от личности
процесса без задачи, прогона и approval. Для `external_write` (ADR-0056
п.4) основания у него не было — `403 skill_side_effect_not_authorized`,
намерение `intent_failed`, и экземпляр падал. По коду это касается и
`notify.send@1` процесса invoice-payment (на staging не проверялось).
Решение — основание даёт сам экземпляр (ADR-0056, амендмент 2026-09-30):

- `authorizationBasis` вызова — `{kind: process, instanceId, activityId,
  definitionKey, definitionVersion}`. Опубликованная версия процесса называет эту версию
  скилла (ссылка шага — всегда `name@version`), так же как тип задачи
  называет свой `execution`. Основание записывается только у
  `external_write`, прочие вызовы остаются без него, как раньше.
- Основание даёт только движок: `invoke_skill` получает экземпляр от
  исполнителя намерений. Маршрута, через который его можно назвать, нет,
  и прямой вызов той же личностью по-прежнему `403`.
- Основание держится, пока открыта activity шага, а не пока жив
  экземпляр (правка по ревью: с основанием экземпляра `try` с `timeout` и
  `retry` оставлял в очереди два `pending` вызова одного шага, и `:claim`
  выдавал оба — две внешние записи). При `:claim` отменённый экземпляр
  отменяет вызов (`basis_revoked`, `process_cancelled`), так как отмена
  экземпляра ожидающих вызовов сама не отменяет. Activity шага, которой
  уже нет в `state.activities` (таймаут, повтор, `catch`, падение
  экземпляра), отменяет вызов с причиной `process_step_closed`.
  Приостановленный экземпляр придерживает вызов до `:resume`
  (`process_suspended`). Разбор дела (`retrospective`) после завершения
  держит свою activity `retro_skill` открытой (`terminate_all` её не
  закрывает), и его вызов выдаётся. Уже выданный вызов не отзывается:
  если первый вызов выдан до таймаута, повтор шага даст вторую запись (у
  повтора свой ключ идемпотентности) — `retry` по таймауту вокруг
  `external_write` автор процесса выбирает сам.
- Песочница процессов (`packages:test`) и раньше не проверяла основание
  вызова, так что поведение сервера и песочницы теперь совпадает.

### И4. Проверки

- `tests/integration/test_package_fresh_install.py`:
  - свежий tenant, один план пакета со скиллами, ролью, типом артефакта,
    двумя типами задач (исход `invokeSkill`, `artifactSchema`) и правилом
    с `interpretation.skill`: план без находок и ничего не пишет,
    установщик ставит `outside`, применение по тому же хэшу проходит,
    повторный план — `unchanged`;
  - скилла нет ни на стенде, ни в пакете — прежние ошибки
    `invalid_approval_schema` и `unknown_skill`;
  - применение до установщика — `422` и ничего не записано;
  - вспомогательный объект неверной формы — предупреждение, ссылки на него
    — ошибки;
  - без права вида — `permission_required` на нём и на объектах, которые
    его называют, ложных ошибок нет; прочие объекты пробуются, их ошибки
    находятся;
  - тест правила на стенде, где оно установлено, проходит без
    `given.clock`, таблицы после прогона те же; `given.clock` раньше и
    позже включения — тоже.
- `tests/integration/test_process_skill_basis.py`: шаг процесса вызывает
  `external_write` с основанием `process`, исполнитель его забирает; прямой
  вызов той же личностью — `403`; отменённый экземпляр — `basis_revoked
  process_cancelled`; приостановленный — вызов ждёт `:resume`; скилл без
  `external_write` основания не получает; таймаут и `retry` — выдан один
  вызов, первый `basis_revoked process_step_closed`; таймаут, пойманный
  `catch`, и упавший экземпляр — ожидающий вызов отменён так же.

## Амендмент 2026-09-30 (package-sdk, S025): инструменты пакета уходят из MCP-сервера ядра

### К1. Решение

Фича `package-sdk` (FR-002, ст. VIII конституции) даёт автору пакетов
один инструмент — MCP-сервер SDK `package-sdk mcp` (S024, плагин
`package-author`). Он читает каталог пакета тем же кодом, что CLI
`package-sdk`, и строит единый план `package-sdk.plan/v1` — вместе с
видами, которые ставит установщик, а не только виды ядра. Второй набор
инструментов в MCP-сервере оператора (п.17) дублировал чтение каталога и
давал план без установщика, поэтому удалён:

- `cp_pkg_check`, `cp_pkg_test`, `cp_pkg_plan`, `cp_pkg_apply`;
- чтение каталога пакета (`_read_package`), подсказки отказов пакета
  (`_PACKAGE_HINTS`), аннотация `APPLYING` и проверка формы `planHash`
  (`plan_hash_required`) адаптера.

Остаются `cp_process_get` и `cp_process_explain`: они только читают
опубликованное и в SDK не переносятся. Их ошибки — в прежней форме
находки п.17 (`{error, message, details, problems}`), без поля `hint`
верхнего уровня: подсказки были только у отказов пакета.

Маршруты ядра (`POST /packages:test`, `:plan`, `:apply`) и методы
клиента `control_plane_client` (`test_package`, `plan_package`,
`apply_package`) не меняются — ими пользуется `package-sdk`.

### К2. Проверки

- `tests/client/test_mcp_process_tools.py`: в реестре MCP-сервера нет
  инструментов `cp_pkg_*`; `cp_process_get` и `cp_process_explain` есть и
  вложенному исполнителю выдаются; `cp_process_get` по ключу и версии
  (версии публикуются планом и применением через клиент), отказ —
  находка `not_found`; объяснение экземпляра — как в п.17.
- `grep -rn "cp_pkg_\|_read_package\|_PACKAGE_HINTS" src` ничего не
  находит.

## Амендмент 2026-10-01 (TASK-001237): шаг `human` заполняет задачу данными дела

Приёмка S033 (TASK-000957, №11): у шага `human` не было способа передать
задаче то, что дело уже знает (поставщик, сумма заявки), и человек
перепечатывал это в поля задачи вручную; данные дела доходили до задачи только
текстом описания (`input`).

### Л1. `human.customFields`

Шаг `human` получает необязательное поле **`customFields`**: объект «поле
`fieldSchema` типа задачи → выражение CEL» (до 32 полей, имена
`^[A-Za-z_][A-Za-z0-9_]*$`, выражение — профиль п.2,
[ADR-0075](0075-cel-expression-profile.md); переменные те же, что у `title`).

```yaml
- id: check
  human:
    taskType: purchase-check
    assign: [{role: buyer}]
    customFields:
      supplier: data.supplier
      amount: data.amount
  output: {as: {note: step.result.note}}
```

Схема вида (`$defs.processStep.human.customFields` в package-sdk
`object.schema.json`) — в закреплённой копии ядра и в
`process_spec.schema.json`; правка package-sdk и руководства — парной задачей.

### Л2. Проверка при публикации

Ключи и типы проверяются по `fieldSchema` типа, как `output.as` по схеме
данных (п.2): поля нет среди `properties` объектной схемы —
**`unknown_custom_field`** (с подсказкой «did you mean»), тип выражения не
подходит к типу поля — **`custom_field_type_mismatch`**; путь находки —
`/spec/…/human/customFields/<поле>`. У типа без объектной схемы (`{}`) ключи
не проверяются. Обязательность полей при публикации не проверяется: остальные
поля заполняет человек, а то, без чего задачу не создать, ловит Л3.

### Л3. Исполнение

Движок вычисляет выражения при открытии шага и кладёт результат в намерение
`create_task` (`customFields`, только у шагов, которые поле объявили;
журналы прежних экземпляров не меняются). Значение `null` поле не заполняет —
оставляет человеку. Ядро создаёт задачу с этими полями той же командой, что
`POST /tasks`, поэтому они проходят полную проверку `fieldSchema` (включая
`required`, `maximum`, форматы) и проверку на секреты; отказ —
`custom_fields_invalid`, намерение проваливается (`intent_failed`), задача не
создаётся. Человек меняет предзаполненные поля как обычно; результат шага при
завершении — поля задачи целиком (п.7), то есть предзаполненное плюс
введённое.

### Л4. Песочница тестов

Песочница (`packages:test`) делает то же: поля проверяются по `fieldSchema`
(отказ — `intent_failed` с кодом `custom_fields_invalid`), задача теста несёт
их, `complete.output` ложится поверх, а `expect.tasks[].customFields`
сравнивает названные поля (схема тестов, `test.schema.json`). Без
предзаполнения песочница, как и раньше, `fieldSchema` при создании задачи не
проверяет.

Проверки: `tests/unit/test_process_definition.py` (`unknown_custom_field`,
`custom_field_type_mismatch`, форма поля), негативные фикстуры
`tests/fixtures/processes/invalid/{unknown_custom_field,custom_field_type_mismatch}.process.yaml`,
`tests/unit/test_package_test.py` (песочница),
`tests/integration/test_process_instances.py::test_a_human_step_fills_the_task_from_the_case`
и `::test_fields_the_type_refuses_fail_the_step_and_file_nothing`.

### Л5. Граница `params.env` исполнителя `skills` — у демона (TASK-001277)

Хвост ревью TASK-001237. Запрет имён `executor.params.env` вида `skills`
(CP-ADR-0073, амендмент 2026-10-01, Ж1) дополнен: прокси, сертификаты TLS,
`XDG_*`, `GIT_*`, `NODE_*`, `DYLD_*`, `UV_*`, `PIP_*`; модель явная —
семейства по префиксу плюс список имён. Зеркальной проверки при публикации
ревизии агента в ядре нет и не вводится: ядро хранит `params` как прислан
(там же, Ж2) и ищет только секреты по именам ключей. Границу проверяет
демон, когда читает ревизию (`parse_skill_env`, отказ — `RevisionError`,
выход 2), и хост скиллов, когда читает `CONTROL_PLANE_SKILLS_LOCAL_ENV`.
Ревизия с запрещённым именем публикуется, но ни один демон её не исполнит;
та же проверка при публикации — отдельное решение, когда она понадобится
ядру не только как хранилищу.

## Conformance

- `tests/unit/test_process_contract.py`: маршруты и тела в OpenAPI, `501` у
  каждого ждущего маршрута, форма находки, план с хэшем и этагом,
  `excludedPrincipals` у approval, права в `Permission` и
  `authz/catalog.yaml`, события `process.*`, `calendar.published`,
  `knowledge.changed`; копии `object.schema.json`, `test.schema.json` и
  примеров равны суперпроекту; примеры процесса, календаря и теста проходят
  схему каталога и модели ядра; принятое схемой каталога для календаря
  принимает ядро, отвергнутое — отвергает.
- `tests/integration/test_processes_contract.py`: каждый маршрут проверяет
  своё право и отвечает `501` с шагом; путь вне пакета — `400`; approval
  с пустым `excludedPrincipals` — как прежде.
- `tests/unit/test_approval_quorum.py` (P008): `all`, `any`, `atLeast`,
  `percent`, «двое из трёх» — досрочные одобрение и отказ, без досрочного
  решения, `parallel`/`sequential`, уход согласующего.
- `tests/integration/test_approval_separation_of_duties.py` (P008): голос
  исключённого principal'а (`:approve`, `:reject`, отмена чужого gate)
  отвергается `403 separation_of_duties_violation` обычным маршрутом решения,
  approval остаётся `pending`, решает другой держатель роли; поле в ответе и
  в `approval.requested`; проверки запроса; «Важное».
- `tests/unit/test_event_catalog.py`: `docs/events/` совпадает с кодом,
  `process_instance`, `process_definition`, `calendar` — сущности ядра.
  `tests/unit/test_process_step_and_sla_events.py` (process-observability
  P002): `process.step_*` и `process.sla_*` — события `process_instance` без
  данных экземпляра, полезная нагрузка писателя проходит каталог, без поля
  ADR — отказ, адресат — ровно один из `principalId` и `roleId`,
  `timer_rescheduled` с `cause: migrated` той же версии;
  `tests/unit/test_process_neutrality.py` — каталог событий без слов домена.
- `tests/unit/test_process_definition.py` (P006): негативные фикстуры
  `tests/fixtures/processes/invalid/` по классам находок дают свой код и
  путь; файл и строка находки из файла пакета; стабильность id против прошлой
  версии; хэш не зависит от порядка ключей; копия схемы вида равна схеме
  каталога. `tests/unit/test_decision_table.py`: ячейки, политики,
  перекрытия, пробелы, вычисление.
- `tests/integration/test_process_definitions.py` (P006): версия
  публикуется один раз, другое содержимое той же версии и версия не выше
  последней — `409`; `422 invalid_process` с находками; предупреждения в
  версии; личность и эскалация прав; чтение по ключу и версии, список,
  фильтры `governedBy` и `workspaceId`, версии без `spec`; строки неизменяемы.
- `tests/unit/test_process_engine.py` (P007): каждый блок и вид шага,
  стадии, вехи и сторожа, необязательная работа, таймеры (установка,
  пересчёт по изменённым полям и по новой версии календаря, сработавший не
  откатывается, заморозка и сдвиг на длительность приостановки),
  эскалации, кворум, `recall` (ответ, таймаут, опоздавший ответ),
  `remember`, компенсации и отмена, ошибка компенсации, подъём ошибки до
  `try` через `fork`, лимит стоимости выражения, разбор дела (вход
  сверяется с закреплённым контрактом скилла, P027), `step` и `compensated`
  в `onCompensate` и их типы (P027), пример
  процесса схемы каталога от извещения до закрытия; каждое событие
  `process.*` сверяется со схемой каталога событий; один журнал, поданный
  дважды и повторно (replay), даёт те же решения, намерения и состояние.
- `tests/integration/test_process_instances.py` (P009): старт наблюдением →
  задача ядра от личности процесса с внешней ссылкой → завершение задачи →
  таймеры шага → изменение данных пересчитывает их → срабатывание,
  эскалация, закрытие; повтор события старта — `process.correlated`, не
  второй экземпляр; повторная доставка пакета ничего не меняет; ответ задачи
  доходит только до своего экземпляра; `:suspend`/`:resume`/`:cancel` с
  правом и заморозкой таймеров; явный старт — один экземпляр на ключ;
  постоянная цель теряет и снова достигает веху; кворум `any` и
  `sequential`; `separationOfDuties` (амендмент 2026-09-30): держатель роли из
  списка получает `403 separation_of_duties_violation` на `:approve` и
  `:reject`, другой держатель решает; исключение действует на следующего
  согласующего `sequential`; единственный держатель исключён — approval ждёт,
  владелец видит `approval.undecidable@1`; исключённый явный согласующий и
  значение не id principal'а — `intent_failed`; амендмент ревью 2026-09-30:
  пустое значение и исключённый `agent:<key>` — `intent_failed`, чужой
  `:cancel` исключённого и постороннего менеджера — `403`, агент с
  делегированием от исключённого — `403`, без `spec.owner` сигнал видит
  запустивший.
  `tests/integration/test_iam_enforcement.py`: токен канала исключённого
  principal'а — тот же `403`. `tests/unit/test_process_separation_of_duties.py`
  — разбор списка исключённых.
  `tests/unit/test_process_engine.py` дополнен явным стартом, отменой без
  компенсаций и потерей вехи; `tests/unit/test_process_neutrality.py` —
  страж нейтральности.
- `tests/unit/test_package_test.py` (P013): копия схемы тестов равна
  закреплённой; YAML 1.2 и строки по указателю; разбор пакета с находками и
  их файлом и строкой; `data.$ref` из пакета и отказ за его пределы; фильтр
  тестов; заглушка скилла по схеме отвечает, **не по схеме — тест падает**;
  вызов без заглушки ждёт, ошибка заглушки — ошибка шага; заглушка `recall`
  не в форме ответа памяти — провал; разделение обязанностей и
  `not_eligible` у `approve`; виртуальное время и `until:`; задача человеку
  (исполнитель, чужой, `fieldSchema`); несбывшиеся ожидания с `expected` и
  `actual`; `noSideEffects` по счётчику; пример теста суперпроекта проходит;
  покрытие перечисляет непройденные элементы, переходы, строки и
  обработчики, второй тест их закрывает; порог `coverage.minimum`; у
  песочницы нет клиентов.
- `tests/integration/test_package_test.py` (P013): пакет с тестом проходит,
  покрытие перечисляет непройденное, **все таблицы базы после прогона
  построчно те же**; `checkOnly`; неверный пакет — `invalid` с файлом и
  строкой, тесты не идут; заглушка не по схеме скилла каталога — `failed`;
  объекты пакета (тип задачи, скилл, агент, календарь) известны его процессу
  до применения; право `packages.test`.
- `tests/unit/test_process_replay.py` (P014): версия на своём журнале — ноль
  расхождений, в том числе как следующая версия; номер кандидата без
  `as_version` расходится на событиях; сдвинутая строка таблицы — ровно
  затронутые экземпляры, `decision` у шага `decide`; `set` с другим значением
  — `data`; отказ входа — `input`; итоговое состояние — `timer`, `state`.
  `tests/unit/test_package_test.py` (P014): пробный прогон с копии —
  задача и approval экземпляра, разделение обязанностей, живое состояние не
  меняется, часы экземпляра и `given.clock`, экземпляр другого процесса,
  `given.data` рядом, неизвестный экземпляр.
- `tests/integration/test_process_replay.py` (P014): на Postgres версия на
  журналах своих экземпляров — ноль расхождений, изменённая строка таблицы —
  ровно два затронутых из трёх, **все таблицы базы после replay построчно
  те же**; `instanceIds`, `limit`; кандидат с ошибкой — `problems` без
  прогона; право `packages.test`, неизвестный процесс и экземпляр — `404`;
  пробный прогон `given.fromInstance` проходит без записей, живой экземпляр
  не тронут, неизвестный — `404`.
- `tests/unit/test_process_migration.py` (P015): экземпляр стоит на стадии,
  шаге ожидания и таймере; переименованный с картой шаг — активность, позиция
  «после того же элемента» при вставленном до него шаге, таймер с
  переехавшим указателем, новая стадия, и следующий вход версии 2 доводит
  экземпляр до закрытия; без карты и со сменой вида — отказ с элементом;
  шаг, ушедший в другой блок, — `element_moved`; миграция версии — только в
  публикуемую; replay начинается с последней записи `migrate`.
- `tests/unit/test_process_sla_migration.py` (process-observability P016):
  вход `migrated` — три шага на 0,5 / 1,5 / 3 рабочих дня при `workdays: 1`
  пересчитаны, два `sla_breached` с `detectedBy: migration`, прошедшие
  уровни — `escalation_skipped`, будущие перенесены, `update_task_due`;
  replay после миграции без расхождений; уже нарушенный срок второго
  события не даёт; прошедший порог не сообщается; снятый `due` снимает
  таймеры; смена формы `due` с `{at}`; срок у шага без него; миграция с
  ревизии 1; цель ревизии 1 — `ignored`; срок процесса от старта;
  замороженный таймер — остаток от `frozenAt`; **приостановка →
  возобновление → миграция на тот же `due` — срок прежний, нарушения нет,
  уровни эскалации не сняты, replay без расхождений**; на другой `due` —
  от срока с паузой; паузы складываются; новая версия календаря после
  возобновления (ожидающий и снова замороженный срок) паузу сохраняет;
  **срок, прошедший до приостановки: после возобновления он и уровень
  эскалации на нём сохраняют объявленный момент, `sla_breached` — с ним и
  с `overdueSeconds` без паузы; миграция после возобновления даёт то же
  событие (`detectedBy: migration`), replay без расхождений; миграция во
  время приостановки считает просрочку до `frozenAt`**.
- `tests/unit/test_process_sla_pause.py` (P016, ревью): пауза хранится в
  единице срока (`working_seconds`, `workdays`, `wall`), пересчёт по
  календарю её сохраняет; `overdueSeconds` без приостановок после срока.
- `tests/integration/test_process_sla_migration.py` (P016, SC-007):
  `packages:apply` с `migrate` — три экземпляра пересчитаны, срок задачи
  обновлён, два `process.sla_breached` с `detectedBy: migration`, ни одного
  `process.escalated`, replay каждого — ноль расхождений; `pin` — сроки,
  таймеры, задача и журнал прежние.
- `tests/integration/test_package_plan.py` (P015): **переименование шага с
  картой при открытых экземплярах — все три на новой версии и на том же
  шаге**, с таймером, записью журнала и `process.migrated` у каждого, задача
  версии 1 завершает шаг версии 2; повтор пакета — `unchanged`; элемент исчез
  без миграции — `migration_required` в плане и `422`, `pin` — экземпляры
  остаются; **каталог изменился между планом и применением — `409
  plan_stale`**, другой пакет под тем же хэшем — тоже; план ничего не пишет;
  поле, правленное человеком, — `console` и сохраняется, `overwriteConsole`
  перетирает, флаг входит в хэш; переименование процесса переносит
  экземпляры, старый ключ не стартует (`409 process_retired`); **план
  перечисляет непокрытые разделы регламента** и раздел, которого нет в
  памяти; права `packages.plan` и права вида, пакет без манифеста.
- `tests/integration/test_package_plan_catalog.py` (амендмент 2026-09-29):
  **план пакета с типом задачи, агентом и правилом — три `create`, apply
  пишет их, тот же план повторно — `409 plan_stale`**, повтор пакета —
  `unchanged`, проба команд ничего не оставляет; по каждому виду `update`
  (новая версия типа и вывод старой, ревизия агента, `PATCH` и статус
  правила), состояние агента без новой ревизии, **изменение любого вида
  между планом и применением — `plan_stale`**; поле, правленное в консоли,
  у каждого вида сохраняется и перетирается `overwriteConsole`; **ключи,
  поставленные как `apply --install` (обычные маршруты с тем же spec), —
  `unchanged`**, поле, которого нет в файле, сохраняется; права каждого
  вида — предупреждение `permission_required` в плане, `403` и ничего не
  записано; отказ команды (неизвестный тип задачи правила) — находка с
  файлом и путём, форма — `invalid_task_type`; `outside` и
  `rename_not_planned`; выведенный агент и чужой workspace правила — ошибки
  плана и `422 invalid_package`; объект другого пакета — предупреждение
  `package_owner_changed`, применение переводит связь (TASK-000969).
- `tests/concurrency/test_package_apply_races.py` (TASK-000903,
  TASK-000969): публикация типа задачи, ревизия агента и `:disable` правила
  во время применения — `409 plan_stale`, чужая версия не перетёрта; проба
  плана берёт ключи в порядке применения; вставка задачи движком при
  ждущем применении; план ждёт `:disable` вызывающего и отвечает `403
  principal_not_active`; **`packages:record` во время применения того же
  пакета ждёт его на advisory-блокировке и оба отвечают `200`, без
  `40P01`**.
- `tests/client/test_mcp_process_tools.py` (P016; сужен амендментом К —
  остались `cp_process_get`, `cp_process_explain` и отсутствие `cp_pkg_*`): инструменты автора на
  фикстурном пакете-каталоге — проверка и тест с файлом и строкой находки,
  каталог не пакет — находка; `cp_pkg_apply` без хэша (или не хэш) отказывает
  `plan_hash_required`, со старым хэшем — `plan_stale`, отказ ядра —
  его находки; `cp_process_get` по ключу и версии; объяснение экземпляра —
  решения с причинами, ответ памяти на `recall`, регламенты элементов;
  вложенному исполнителю не выдаётся только `cp_pkg_apply`.
- `tests/unit/test_process_step_projection.py` (process-observability P007):
  вход human-шага с задачей, стадией и попыткой, выход `completed` с
  `durationSeconds`; мгновенные шаги — ноль событий; исходы `timed_out`,
  `cancelled`, `interrupted`, `failed` (сбой экземпляра и отказ намерения),
  `migrated`, эскалация `raise`, пойманная `try`, — `cancelled`; задача,
  отменённая вне процесса, — `withdrawn` (и с обработчиком `try`, и без
  него, когда экземпляр уходит в `failed`), отозванные все approval —
  `withdrawn`; `quorum: all` с одобрившим и отозванным и `atLeast`,
  ставший недостижимым после отзыва, — `completed`; отклонённый approval и
  выполненная задача — `completed`,
  отмена одного из нескольких approval выхода не даёт; повтор `try` — попытки 1 и 2,
  пауза `retry` событий не даёт; две activity одного элемента (`onEvent`)
  выходят каждая со своим номером попытки, activity до выкатки — с 1;
  решения и намерения движка те же с проекцией и без неё; каждый payload
  проходит схему каталога.
- `tests/integration/test_process_step_events.py` (P007): **на живом ядре
  у каждого ожидающего шага (задача, approval, пауза) ровно одна пара по
  `activityId`, мгновенные шаги — ноль событий, всего 2 на шаг**; повторная
  подача входов (курсор воркера на начало) и новый воркер между шагами —
  ноль дублей и пропусков; `step_attempts` в строке экземпляра; журнал
  решений без событий шага; отмена — выход `cancelled`; задача, отменённая
  участником, — выход `withdrawn` с `actorId` участника; approve после
  отзыва голоса — `completed` (`all`, `atLeast`) или `withdrawn` с `actorId`
  отменившего (отозваны все), replay таких журналов — ноль расхождений; `/events` и
  `/events/ws` отдают события с `events.read`, без права — `403`/`4403`.
- `tests/integration/test_process_step_events_packages.py` (P007): все тесты
  пакетов `invoice-payment` и `tenders` суперпроекта через `packages:test`
  с той же проекцией на каждом шаге песочницы — у каждой activity ровно
  пара (или вход у шага, открытого на конце теста), вход human-шага несёт
  `taskId` его задачи, approve — `approvalIds` его запросов. Пакеты читаются из
  суперпроекта (`CP_SUPERPROJECT`), вне его тест пропускается. Отступление от
  приёмки «на живом ядре»: шаги пакетов идут в песочнице `packages:test`, а
  не через `take()` сохранённого экземпляра — пакетам нужны скиллы, которые
  `packages:apply` не регистрирует, и `recall` к сервису памяти; `refs`
  восстанавливаются из задач и approvals песочницы. `take()` с `record_event`
  на живом ядре проверяет `test_process_step_events.py`.
- `tests/unit/test_package_test_sla.py`, `tests/integration/test_package_test.py`
  (P015, CP-ADR-0078 §7): `expect.sla` — `warning` и `breached` после `advance`
  по рабочим дням в обход праздника и выходных, срок процесса, чтение
  закрытого экземпляра в момент закрытия; `given.calendar` и календарь
  пакета с рабочими часами (короткий предпраздничный день, праздники,
  выходные) двигают срок; несовпадение называет шаг, ожидаемое и
  фактическое; события `process.step_*` и `process.sla_*` песочницы несут
  попытку, как в ядре, пробный прогон продолжает счётчики живого экземпляра.
  Тесты пакетов `invoice-payment` и `tenders` суперпроекта проходят без правок.
- `tests/unit/test_package_links_contract.py` (TASK-000904): виды связи
  совпадают в домене, схеме запроса, проверке таблицы и формате каталога;
  у списка и карточки каждого вида — `package` (`PackageLinkOut` или null) и
  `?package=`; маршрут `packages:record` с телами.
- `tests/integration/test_package_links.py` (TASK-000904): применение
  связывает календарь пакета, ручной — `null`, фильтр; следующая версия
  пакета переводит связь неизменившегося объекта; версия, опубликованная
  вручную, остаётся в пакете ключа; `packages:record` связывает тип задачи,
  правило, роль tenant'а (не роль workspace), возможность и скилл, фильтры
  списков, обновление версии; неизвестный объект — `422 unknown_object`,
  ничего не записано; `Process`/`Calendar`/чужой вид — `400`; права
  `packages.plan` и вида; агент с `package`; миграция вниз и вверх и связь
  агентов по ревизиям.
- Реализующие шаги дополняют раздел своими тестами.
