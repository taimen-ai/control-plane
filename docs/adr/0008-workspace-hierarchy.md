# ADR-0008. Иерархия workspaces: adjacency list + advisory lock

Статус: принято (2026-08-11, v0.2)

## Контекст

Workspace задаёт организационный scope (отделы, команды, проекты) и участвует
в scope-семантике ролей. Нужны: иерархия, уникальность slug среди siblings,
запрет циклов, move поддерева. Варианты хранения: adjacency list, materialized
path, nested sets, ltree.

## Решение

- **Adjacency list** (`parent_id`), `parent_id IS NULL` = корневой уровень
  (корней может быть несколько).
- Уникальность slug среди siblings — два частичных уникальных индекса
  (отдельный для корневого уровня, т.к. NULL в обычном UNIQUE не сравнивается).
- Обходы (ancestors, subtree) — recursive CTE по запросу; глубина иерархий
  организаций мала, денормализация не нужна.
- **Структурные мутации** (create, move, смена slug, archive) сериализуются
  per-tenant advisory lock'ом `pg_advisory_xact_lock(hash('cp:ws:<tenant>'))`:
  проверка цикла (CTE по поддереву) и проверка slug выполняются без гонок; два
  конкурентных move не могут собрать цикл совместно. Индексы остаются backstop.
- Archive вместо delete; архивирование требует отсутствия активных детей;
  архивный workspace не принимает новые под-workspaces и задачи.

## Обоснование

Adjacency list — простейшая модель, полностью покрывающая MVP-операции;
materialized path/nested sets ускоряют массовые subtree-запросы ценой сложных
инвариантов при move — у нас таких запросов нет. Advisory lock на дерево
тенанта дешевле и проще, чем блокировка путей: структурные изменения дерева
редки, конкуренция за лок незначима.

## Последствия

- Ancestor-запросы стоят один CTE (мал при реальных глубинах).
- Все структурные мутации одного тенанта сериализованы — приемлемо.

## Амендмент 2026-10-03: разрешённые типы работ у workspace (TASK-001306)

Решение владельца 2026-10-03: консоль показывает только те типы задачи,
что доступны в выбранном workspace. До амендмента типы работ общие на весь
tenant: связи «workspace → тип работы» в ядре не было.

### А1. Настройка `taskTypes` и наследование вниз

- У workspace — необязательная настройка `taskTypes`: список ключей типов
  работ (`TaskType.key`, без версии), разрешённых в нём. Хранится в
  `workspaces.task_types` (JSONB, `NULL` по умолчанию; у всех workspace до
  амендмента — `NULL`, поведение не меняется).
- **`null` и `[]` — разные значения.** `null` (настройки нет) — workspace
  наследует список ближайшего предка, у которого он задан; не задан нигде на
  пути до корня — разрешены **все** типы. `[]` — явное «ни одного»: работу
  в этом workspace завести нельзя, и потомки без своей настройки наследуют
  именно «ни одного». Пустой список прекращает подъём к предкам так же, как
  непустой.
- Вычисленное значение — `effectiveTaskTypes`: собственный список или
  список ближайшего задавшего предка (обход — тот же recursive CTE предков,
  ближайший первым); `null` — разрешены все. Потомок может как сузить, так и
  расширить список предка: настройка — не верхняя граница, как governance
  (ADR-0033), а значение по умолчанию для поддерева. Архивность предка на
  наследование не влияет.
- Список задаёт `PATCH /workspaces/{id}` полем `taskTypes` (право — то же,
  что у остальной правки workspace: `workspaces.manage`, `If-Match`).
  Поле не передано — настройка не меняется; `null` — сбросить к
  наследованию; список — заменить целиком. Ключ проверяется по
  зарегистрированным типам tenant'а (любая версия, любой статус — как
  `action.taskType` правила): неизвестный — `422 unknown_task_type`,
  `details: {field: "taskTypes[i]", taskType}`. Неверная форма (не список,
  не строка, не ключ типа, повтор, больше 100 элементов) — `400
  invalid_request` моделью запроса. То же значение, что уже стоит, — не
  изменение: без других полей это `422 empty_update`, как у прочих полей
  `PATCH`. Порядок ключей сохраняется как передан.
- `POST /workspaces` настройку не принимает (новый workspace наследует);
  установка списка пакетом — отдельная задача.

### А2. Чтение и события

- `WorkspaceOut` (списки, дерево не меняется) несёт собственное
  `taskTypes`. Ответы об одном workspace — `GET`, `PATCH`, `POST
  /workspaces`, `:archive`, `:move` — несут ещё `effectiveTaskTypes`
  (`WorkspaceDetailOut`): перенос под другого родителя меняет наследуемое
  значение.
- `GET /task-types?workspaceId=<id>` — только версии типов, чей ключ входит
  в `effectiveTaskTypes` workspace (при `null` — все, при `[]` — ни одной);
  сочетается с остальными фильтрами. Право — `task_types.read`; workspace
  другого tenant'а или несуществующий — `404`, как отсутствующий. Без
  параметра — как прежде.
- Событие `workspace.updated` — **версия 2** (CP-ADR-0068 п.4): при смене
  настройки `changes.taskTypes: true` и необязательное поле `taskTypes` —
  новое собственное значение (`null` — наследование). Ключи типов не
  секретны, в отличие от содержимого `customFields`.

### А3. Отказ в заведении и переносе работы

- `POST /tasks` с `workspaceId`, тип которой (ключ разрешённой версии) не
  входит в `effectiveTaskTypes` workspace, — `422 task_type_not_allowed`,
  `details: {workspaceId, typeKey}`; ничего не пишется. Тип по умолчанию
  (системный, когда тип не указан) проверяется так же. Работа без
  `workspaceId` не ограничивается.
- Перенос работы в другой workspace (`PATCH /tasks/{id}` с `workspaceId`)
  проверяется так же по целевому workspace; снятие `workspaceId` (`null`) и
  `PATCH` без смены workspace не проверяются.
- Правила, исходы approval и процессы заводят работу через ту же команду и
  получают тот же отказ как ошибку действия.
- Уже существующие работы не трогаются: сужение списка не переводит, не
  закрывает и не запрещает править работы, заведённые раньше. Проверка
  делается по состоянию на момент записи, без блокировки предков: работа,
  заведённая параллельно с сужением списка, становится такой же «уже
  существующей» — это совместимо с правилом выше.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"task_type_not_allowed"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"uq_workspaces_root_slug"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: '"uq_workspaces_sibling_slug"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: 'pg_advisory_xact_lock\(func\.hashtextextended\(f"cp:ws:\{tenant_id\}"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_cycle"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_has_active_children"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/workspaces.py, pattern: '"workspace_archived"'}
  repo: control-plane
```
