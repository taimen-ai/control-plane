# ADR-0056: Skill contract v1 и вызов Skill ядром

Статус: Accepted (2026-09-23, M2.1/M2.2; амендменты M2.1 и M2.2, правки по ревью M2.2 — D; scopes токена скилла — D.9, 2026-09-25; основание шага процесса — амендмент 2026-09-30)

Контекст: TAI-ADR-0037 (Skill runtime в ядре, вертикаль как данные), TAI-ADR-0036
(правила вызывают Skill для интерпретации), TAI-ADR-0035 (deterministic-проверки
verification — это Skill), TAI-ADR-0041, Proposed (действие `invokeSkill` в
исходе approval); ADR-0009 (role/capability/skill), ADR-0021 (резолюция версий),
ADR-0045 (discovery не источник права).

## Контекст

Таблица `skills` сегодня — каталог: `name`, `version`, `protocol`, `config`,
`input_schema`, `output_schema`, `status`. Skill назначается principal'ам и
требуется задачам (ADR-0009/0010/0021), но ядро его **не вызывает**:
`run_actions.skill_id` на staging пуст, а 21 http-скилл BidOps исполнялся
runner'ами BidOps в обход ядра. Видение требует обратного: Skill — единица
повторно используемой способности, и вызывают её одинаково агент, правило,
харнесс человека, исход approval и стадия verification.

Вызывающих пять, путей вызова должен быть один. Иначе каждый получит свою
семантику ретраев, идемпотентности и прав, и они молча разойдутся — как
разошлись бы discovery и gate без единой функции решения (ADR-0045).

## Решение

### 1. Контракт — иммутабельная версия

Версия Skill `(tenant, name, version)` после публикации не меняется: изменить
контракт — значит выпустить новую версию (как `task_types`, ADR-0048).
Поля контракта:

| Поле | Назначение |
|---|---|
| `inputs`, `outputs` | JSON Schema (draft 2020-12); проверяются при публикации |
| `sideEffects` | `none` \| `external_read` \| `external_write` (колонка, по ней решает политика) |
| `riskLevel` | `low` \| `medium` \| `high` (колонка) |
| `requiredPermissions` | права ядра, которые должны быть у вызывающего |
| `preconditions`, `postconditions` | ограниченные JSON-выражения (язык правил TAI-ADR-0036) над входом и выходом |
| `timeoutSeconds`, `retryPolicy` | `{maxAttempts, backoffSeconds}`; ретраится только `retryable`-ошибка |
| `idempotency` | `required` \| `natural` \| `none`: нужен ли ключ от вызывающего |
| `costModel` | `{unit, estimate}` — для бюджета и метрики cost per Work |
| `implementation` | `{protocol, endpoint, auth, entrypoint}` |

`protocol`: `http`, `local`, `mcp`. Протокол `harness` (Skill, который
исполняет человек или агент в харнессе) в v1 **не вводится**: такая работа
выражается Work через `ensureWork`, а не синхронным вызовом. Прежние значения
`opencode` и `custom` остаются только у существующих строк как описательные,
вызвать их ядро не может (`409 skill_not_invocable`).

### 2. Вызов — долговечный объект `skill_invocation`

Любой вызов — строка `skill_invocations`: `skill_id` (конкретная версия),
`inputs`, `requestedBy {kind: principal|rule|approval|verification|run, ref}`,
`authority` (principal, от имени которого действует вызов), `idempotencyKey`,
`status` (`pending → running → succeeded | failed | cancelled`), `attempt`,
`output`, `error {code, retryable, message}`, `cost`, `lease`/`fencingToken`,
`taskId`/`runId`, если вызов идёт внутри Work.

- **Создание** (`POST /api/v1/skills/{name}@{version}:invoke`, MCP
  `cp_invoke_skill`, внутренне — worker для правил и approval) проверяет
  статус версии, `inputs` по схеме, `preconditions`, права вызывающего на
  `requiredPermissions` и правило side effects (п.4). Повтор с тем же
  `(skill_id, idempotencyKey)` возвращает существующий вызов.
- **Исполнение** — исполнитель Skills берёт `pending`-вызов под lease с
  fencing (как claim задачи), вызывает реализацию по протоколу и сдаёт
  результат `:complete` или `:fail`. Ядро **повторно** валидирует `output` по
  схеме и `postconditions`: нарушение — `failed` с `output_contract_violation`,
  вне зависимости от того, что сказал исполнитель. Истёкший lease возвращает
  вызов в `pending`, пока не исчерпаны попытки.
- **Результат** — событие `skill.invocation_succeeded` или `…_failed` с id;
  при `taskId` — артефакт `skill_result` с evidence. Правила и исход approval
  продолжаются по событию, а не ожиданием в транзакции.

### 3. Work, исполняемая Skill

Версия типа задачи может объявить `execution = {skill: name, version}`. Задача
такого типа исполняется исполнителем Skills как обычная Work: claim, run, внутри
run — один `skill_invocation` с входами из `customFields` и evidence задачи.
Успешный вызов завершает run и отдаёт выход в verification (TAI-ADR-0035).
Отдельного жизненного цикла у «задачи-скилла» нет.

### 4. Права и side effects

Вызов действует от имени `authority`: человек или агент, вызвавший напрямую;
автор правила — для правила; решивший approval — для исхода approval
(TAI-ADR-0041 п.4). Исполнитель Skills — только транспорт, своих прав не
добавляет. `sideEffects = external_write` требует одного из трёх: gate-approval
по этому действию решён положительно; тип задачи объявляет Skill своим
`execution` и задача прошла свои gates; delegation с этим Skill в scope.
Иначе `403 skill_side_effect_not_authorized`.

### 5. Реализации и исполнитель

- **`http`** — `POST endpoint` с телом `{invocationId, idempotencyKey, inputs}`
  (и `settings` пакета скилла — CP-ADR-0081, амендмент В3)
  и токеном IAM audience сервиса-скилла. Ответ 2xx — `outputs`; 4xx —
  неретраимая ошибка, 5xx и таймаут — ретраимая.
- **`local`** — Python entrypoint `module:function` из пакета, установленного у
  исполнителя. Deterministic-скиллы ядра (сверка ADR, `git.*`) — такие.
- **`mcp`** — вызов инструмента MCP-сервера по имени из `implementation`.

Исполнитель Skills — адаптер `skill` демона `control_plane_agent` (TAI-ADR-0037
п.2), тот же процесс и тот же контейнер, что runner. Он объявляет
поддерживаемые протоколы и набор `local`-пакетов capability сессии
(`skills.protocol.*`, ADR-0021), и получает только те вызовы, которые может
исполнить.

**Сеть и токен — исполнителя, а не контракта.** `endpoint` и `auth.audience`
пишет администратор tenant'а, а соединение и токен — это сеть и identity
runner'а. Поэтому для `http` и `mcp` по сети исполнитель ходит только в
origin'ы из своего allow-list (пустой — протокол не исполняется, ядро такие
вызовы ему не выдаёт); токен прикладывает только по `https` и только для
audience из своего allow-list, никогда — `control-plane`, `iam` или своей
собственной; адреса loopback/private/link-local/зарезервированные запрещены
после DNS-резолюции, если хост не доверен им явно, и соединение идёт на
проверенный адрес; в `error.details` уходит только начало тела ответа, без
заголовков. Подробно — амендмент M2.2, D.

## Не принято

- **Синхронный вызов Skill внутри транзакции API.** Внешний вызов под
  блокировкой строк — путь к зависаниям и к откатам уже случившихся внешних
  действий.
- **Выполнение Skills сервером control-plane.** Сервер решает и хранит, внешние
  действия идут через исполнителя с отдельной identity и сетью.
- **Диапазоны версий.** Резолюция по-прежнему `name` или `name@version` (ADR-0021).

## Последствия

- Миграция: колонки `side_effects`, `risk_level`, `contract` (JSONB) в `skills`,
  запрет изменения опубликованной версии, таблица `skill_invocations` с
  уникальностью `(skill_id, idempotency_key)`; `task_types.execution`.
- API `POST /skills/{ref}:invoke`, `GET /skill-invocations/{id}`, операции
  исполнителя `:claim` / `:complete` / `:fail` / `:heartbeat`; MCP
  `cp_describe_skill` (полный контракт) и `cp_invoke_skill`.
- 21 скилл BidOps остаются строками каталога без `implementation` и
  невызываемыми до M2.4, данные не трогаются.
- Метрика §24 «reusable skills» считается по вызываемым версиям с хотя бы
  одним успешным вызовом, а не по строкам каталога.

## Амендмент 2026-09-23: серверная часть M2.1 (§1–2, §4)

Реализация сервера уточняет решение в следующих местах; §3 (`task_types.execution`)
и §5 (исполнитель) — задача M2.2 и здесь не затронуты.

1. **Форма хранения контракта.** `side_effects` и `risk_level` — колонки,
   остальное — нормализованный JSONB `contract` (значения по умолчанию
   подставлены при публикации: `timeoutSeconds=60`, `retryPolicy={1, 0}`,
   `idempotency=none`, пустые `requiredPermissions`/условия). Все три поля
   nullable: `contract IS NULL` — строка каталога, невызываемая
   (`409 skill_not_invocable`, `reason=no_contract`); CHECK требует рядом с
   contract обе колонки и равенство `protocol` и
   `contract.implementation.protocol` ∈ {http, local, mcp}. Прежние колонки
   `input_schema`/`output_schema` сохранены и у контрактной версии заполняются из
   `inputs`/`outputs`, чтобы discovery (ADR-0045) читал ту же схему.
2. **Иммутабельность.** Триггер `skills_immutable` (по образцу
   `forbid_task_type_mutation`) замораживает всё содержимое версии, включая
   `config` и схемы строк каталога. Исключения: `description` (сводка каталога,
   не часть контракта) и движение `status` только вперёд — active→deprecated,
   active→disabled, deprecated→disabled; DELETE запрещён. `PATCH /skills/{id}`
   отвечает `409 skill_version_immutable` / `invalid_status_transition` раньше
   триггера.
3. **Условия.** Язык выражений (TAI-ADR-0036) появится в M1.3; до него
   публикация принимает только пустые `preconditions`/`postconditions`
   (`422 unsupported_skill_condition`) — контракт не обещает проверку, которую
   ядро не выполняет. Проверка при вызове и при `:complete` поэтому пока
   сводится к JSON Schema.
4. **Основание для `external_write` (§4).** В v1 сервер признаёт одно
   основание — `approvalId` gate-approval **той же задачи** (`taskId`/`runId`
   вызова) в статусе `approved`; основание записывается в
   `authorization_basis` вызова. Задача должна быть нетерминальной
   (`409 task_terminal`): закрытая Work не основание для внешнего действия.
   Approval — **одноразовое** основание на версию Skill: второй вызов той же
   версии с тем же `approvalId` — `409 approval_already_used`; гарантирует это
   частичный уникальный индекс `(skill_id, authorization_basis->>'approvalId')`
   для строк с `kind = approval`, в том числе при гонке. Одноразовость — именно
   на версию: следующая версия того же Skill может сослаться на тот же approval.
   Approval расходует любой принятый вызов, в том числе завершившийся `failed`
   (исполнитель мог успеть подействовать): повторить неудачное внешнее действие
   можно только с новым approval. Повтор того же вызова
   по `idempotencyKey` вторым использованием не считается. Привязка к задаче,
   а не к конкретному действию, — временная: у approval нет полезной нагрузки «что одобрено» до TAI-ADR-0041
   (`invokeSkill` в исходе approval). Два других основания пока недоступны и
   поэтому никогда не срабатывают: `execution` типа задачи — M2.2 (реализовано,
   амендмент M2.2, A.3), delegation
   «со Skill в scope» — у delegation нет scope, только список прав. `authority`
   прямого вызова — вызывающий principal, в том числе при основании approval.
5. **Идемпотентность.** Ключ именует один вызов: повтор с теми же `inputs` от
   того же principal возвращает существующий (`200`), иначе
   `409 idempotency_key_reuse` (чужой вызов при этом не раскрывается). Повтор
   проверяется раньше статуса версии, схемы и прав, чтобы ретрай после
   потерянного ответа не стал вторым внешним действием, а версия, отключённая
   после вызова, продолжала отвечать за уже принятый вызов. `idempotency=required` без ключа —
   `400 idempotency_key_required`.
6. **Коды ответа.** Невалидные `inputs` — `400 invalid_skill_inputs` (контракт
   здесь — данные версии, а не форма API); `output_contract_violation` при
   `:complete` — `200` с `status=failed` в теле: отчёт исполнителя принят,
   вердикт вынесло ядро. Отклонённый выход сохраняется как evidence в
   `error.details.rejectedOutput`; если он длиннее 64 КиБ (каноническая
   форма) — только `rejectedOutputTruncated=true` и `rejectedOutputBytes`.
   `format` в JSON Schema проверяется (`Draft202012Validator.FORMAT_CHECKER`)
   для форматов, известных установленному `jsonschema` (`uuid`, `email`,
   `ipv4`, `date` и др.); для форматов без проверяющего (например,
   `date-time` без `rfc3339-validator`) `format` остаётся аннотацией.
7. **Lease и попытки.** Попытка считается при выдаче (`:claim`); истёкший
   lease её расходует (исполнитель мог успеть подействовать): есть попытки —
   `pending`, нет — `failed` с `lease_expired` (`retryable=true`). Возврат
   истёкших lease делает сам `:claim` (корректность не зависит от воркера) и
   фоновый sweep воркера. Lease по умолчанию — `timeoutSeconds + 30` c в
   пределах `CP_CLAIM_TTL_MIN/MAX_SECONDS`. Heartbeat продлевает lease не дальше
   `attempt_started_at + timeoutSeconds×2 + 30` c (`attempt_started_at` —
   момент claim текущей попытки; `started_at` по-прежнему — первой) и никогда
   его не укорачивает. Строки, взятые в работу до миграции `c7d3a1f9e2b6`,
   получают `attempt_started_at = started_at` (backfill в миграции; для
   повторной попытки это раньше реального начала, т. е. срок только короче), а
   код при пустом `attempt_started_at` берёт `started_at` же. Устаревший исполнитель получает
   `409 stale_invocation_lease`. Сессия исполнителя (`sessionId`) при claim
   необязательна; если передана — проверяется как у claim задачи, и тогда
   lease держит именно эта сессия: `:heartbeat`/`:complete`/`:fail` обязаны
   передать тот же активный `sessionId`, иначе `409 stale_invocation_lease`.
8. **Права и видимость.** Новые права `skills.invoke` (вызов) и
   `skills.execute` (исполнение) — уровень tenant, добавлены в
   `authz/catalog.yaml`. Контракт читают `org.read | skills.invoke |
   skills.execute`; прежний `config` строки каталога (адреса, заголовки)
   `GET /skills/{ref}` отдаёт только держателю `org.read`, остальным — `{}`. Вызов виден своему authority, исполнителю и держателю
   `skills.execute`; остальным — 404. Выдача прав runner-binding'у на стендах —
   отдельное операционное действие, не миграция.
9. **События.** Кроме `skill.invocation_requested|succeeded|failed` пишутся
   `skill.invocation_claimed` (только журнал, без outbox) и
   `skill.invocation_retry_scheduled`. Отмены вызова (`cancelled`) API v1 не
   предоставляет: статус зарезервирован. *(Отменено амендментом M2.2, п. 3:
   отмена введена.)*
10. **Границы вызова (§4).** Помимо `skills.invoke`:
    - вызов с Work item (`taskId` или `runId`) — запись в эту Work (артефакт
      `skill_result`, ссылка на её gate), поэтому требует `tasks.write` на
      `workspace` задачи (tenant, если задача вне workspace), до того как
      задача принята как основание. Иначе `403`, и чужая задача не становится
      основанием `external_write`;
    - `requiredPermissions` проверяются на том же ресурсе — `workspace` задачи,
      без задачи — tenant: runner-binding бывает workspace-scoped;
    - `runId` должен называть `running` run вызывающего (иначе `409
      run_not_active`): завершённый run полномочий не несёт. Если у principal
      есть `running` run, ограниченный child handle, названный run обязан сам
      быть ограниченным — ссылка на собственный корневой run сняла бы потолок
      так же, как вызов без `runId` (`403 run_id_required`);
    - при `runId` каждое из `requiredPermissions` (и `tasks.write`, если есть
      задача) проходит потолок дочернего run (`enforce_run_ceiling`, HRS-7) —
      `403 child_grant_exceeded`; к самому Skill применяется решение effective
      tool policy run (`decide_visibility`, HRS-3): назначение principal'у,
      `allowedSkillProtocols` проекта и `skills` grant'а — `403
      child_grant_exceeded` или `tool_not_authorized`. Capability харнесса, как
      и у действий run, не применяется;
    - без `runId` потолок не к чему привязать, поэтому `_require_bound_run`
      отклоняет вызов principal'а, у которого сейчас есть `running` run,
      ограниченный child handle: он обязан назвать этот run, иначе `403
      run_id_required`. Прямой вызов без run остаётся за principal'ами без
      такого run.

## Амендмент 2026-09-23: M2.2 — исполнитель, `task_types.execution`, правки по ревью M2.1

### A. Work, исполняемая Skill (§3)

1. **Поле.** `task_types.execution` — JSONB, nullable, часть иммутабельного
   содержимого версии типа (функция `forbid_task_type_mutation` заменена
   миграцией `e3a9c5d7f1b4`). Форма — `{skill, version, inputs}`: версия Skill
   закреплена (не голое имя — «новейшая active» поменяла бы смысл версии типа
   под живой задачей), при публикации типа она должна существовать и иметь
   contract (`422 invalid_task_execution`, `details.reason` = `not_found` |
   `no_contract` | `disabled`). Отключённая позже версия даёт отказ при вызове
   (`409 skill_not_invocable` → run падает), а не молчание.
2. **Входы.** `inputs` — простые JSON-path по задаче в представлении API
   (camelCase): грамматика `$` + `.name` + `[n]`, без фильтров и рекурсии.
   Строка — путь ко всему объекту входа (по умолчанию `$.customFields`);
   объект `{имяВхода: путь}` — поимённо; отсутствующий путь вход не заполняет, и
   что он обязателен, решает input-схема (`400 invalid_skill_inputs` → run
   падает). Сервер проверяет только синтаксис путей; вычисляет их демон —
   транспорт по контракту, без доменной логики (TAI-ADR-0041). Evidence задачи
   во входы v1 не попадает: для этого нужен язык выражений (M1.3).
3. **Основание `execution` (§4).** Второе основание `external_write` из §4
   реализовано: вызов с `runId` `running` run, задача которого — типа,
   объявляющего `execution` именно на эту версию; задача нетерминальна
   (`409 task_terminal`) и на ней нет `pending` gate-approval (`409
   approval_required`) — это и значит «задача прошла свои gates»: те же gates
   держат claim и завершение задачи. `approvalId`, если передан, имеет
   приоритет (основание approval). `authorization_basis = {kind: execution,
   taskTypeId, taskId, runId}` записывается при любых side effects, и
   частичный уникальный индекс `uq_skill_invocations_run_execution` оставляет
   у run один такой вызов (`409 execution_already_invoked`, в том числе при
   гонке). Повтор после потерянного ответа не второй вызов: демон передаёт
   `idempotencyKey = execution:<runId>`. Новый run той же задачи — новый вызов:
   повторять ли внешнее действие после упавшего run, решает идемпотентность
   Skill (п. C.2).
4. **Исполнение.** Демон видит `execution` через `GET /task-types/{typeId}`
   (кэш на версию; runner'у нужен `task_types.read`, без него — прежнее
   поведение с предупреждением в журнале). Такую задачу он берёт, только если
   сам может исполнить Skill (протокол и `local`-entrypoint), иначе оставляет
   другому демону; адаптеру (Claude Code, Codex) она не достаётся никогда.
   Цикл: claim задачи → run → `:invoke` с `runId` → `:claim` с
   `invocationId` этого вызова (если его взял другой исполнитель — ожидание
   результата) → терминальный статус → `succeed_run(output={skillInvocationId,
   skill, status, artifactId})` или `fail_run("skill_invocation_<status>:
   <code>")`. Артефакт `skill_result` пишет ядро; workspace, коммит и
   авторевью для такой Work не создаются. Ожидание ограничено контрактом
   (`maxAttempts × (timeoutSeconds×2 + 30 + backoffSeconds) + 30` c) и живым
   claim задачи; по истечении демон отменяет вызов (`:cancel`), чтобы тот не
   подействовал за упавший run. Verification результата (TAI-ADR-0035) — когда
   появится; сейчас успешный вызов завершает задачу.
5. **Права runner'а.** Для Work со Skill runner-binding'у нужны `skills.invoke`
   (он — authority вызова), `skills.execute`, `task_types.read`, `tasks.write`
   на workspace задачи и назначение Skill principal'у (effective tool policy run,
   п. 10 амендмента M2.1). Выдача — операционное действие на стендах.

### B. Исполнитель Skills (§5)

1. **Где.** `control_plane_agent/skills.py`, в том же процессе, что runner.
   Включается окружением: `CONTROL_PLANE_SKILLS_PROTOCOLS` (список из `local`,
   `http`, `mcp`; по умолчанию `local`, если заданы пакеты, иначе исполнитель
   выключен), `CONTROL_PLANE_SKILLS_LOCAL_PACKAGES` (`module:function` или имя
   пакета/модуля: тогда объявляются entrypoint'ы всех его модулей с `CONTRACT`
   `local`-протокола; объявляется только то, что импортируется),
   `CONTROL_PLANE_SKILLS_MCP_SERVERS` (JSON `{имя: {command, args, env}}` для
   `stdio:<имя>`). Протоколы объявляются и в capability сессии
   `skills.protocol.<p>`.
2. **Цикл.** Когда Work нет — `:claim` (с `sessionId` демона) → heartbeat
   (интервал ≤ трети остатка lease) → вызов реализации под `timeoutSeconds` →
   проверка `outputs` по схеме контракта (нарушение — `:fail`
   `output_contract_violation`, неретраимая, с `details.errors`) → `:complete`
   / `:fail`. Ядро проверяет выход повторно, как и прежде.
3. **Потеря lease.** `409 stale_invocation_lease` (или `404`,
   `session_not_active`) на heartbeat, либо наступивший `leaseExpiresAt` без
   успешного heartbeat — результат **не отправляется**; исполнение прерывается
   (у `local`-функции подпроцесс убивается, D.6; в режиме `thread` — бросается:
   поток нельзя убить, его результат отбрасывается). Отмена `running`-вызова
   (C.1) приводит к тому же.
4. **`local`.** Вызываются только объявленные entrypoint'ы; функция
   `run(inputs) -> outputs` — в отдельном потоке (корутина — напрямую) под
   `asyncio.wait_for`. Исключение с `retryable = True` — ретраимая ошибка, его
   `code` (если строка вида `^[a-z0-9][a-z0-9_.-]*$`) — код, иначе
   `skill_error`; таймаут — `timeout`, ретраимая. Сообщения проходят
   `redact_local_paths`.
5. **`http`.** `POST endpoint` с `{invocationId, idempotencyKey, inputs}`;
   при `implementation.auth.audience` — Bearer-токен IAM этой audience со
   scopes из `implementation.auth.scopes` (D.9), обменянный из PAT демона
   (`CONTROL_PLANE_IAM_*`); нет IAM — ретраимая
   `executor_auth_unavailable` (другой исполнитель может её выполнить).
   Тело ответа 2xx — это и есть `outputs` (без конверта). 4xx — неретраимая
   `http_<status>`, 5xx, таймаут, транспортная ошибка — ретраимая; редиректы не
   следуются (токен не должен уйти на другой хост).
6. **`mcp`.** `tools/call` инструмента `implementation.entrypoint` на
   сервере `implementation.endpoint`: `http(s)://…` — streamable HTTP (тот же
   Bearer по `auth.audience`), `stdio:<имя>` — процесс из конфигурации
   исполнителя (командная строка — конфигурация хоста, не данные контракта).
   `outputs` — `structuredContent`, иначе единственный текстовый блок с JSON.
   `isError` и протокольная ошибка MCP — неретраимые, транспорт — ретраимая,
   неизвестный `stdio`-сервер — ретраимая `mcp_server_unknown`.

### C. Правки по ревью M2.1 (TASK-000302)

1. **Основание перепроверяется при `:claim`; отмена.** Кандидат на выдачу
   проверяется под своей блокировкой строки: версия `disabled` (для любого
   вызова), для вызова с основанием — задача терминальна, approval больше не
   `approved`, run основания `execution` не `running` → вызов `cancelled`
   (`error = {code: basis_revoked, message: <причина>, retryable: false}`,
   событие `skill.invocation_cancelled`), и берётся следующий (до 20 за один
   `:claim`). `pending` gate-approval на задаче `external_write`-вызова с
   основанием `execution` — вызов остаётся `pending` до решения. Вызовы без
   основания (не `external_write`) по состоянию задачи не отменяются: закрытая
   Work не мешает чтению или вычислению, а verification (TAI-ADR-0035) может
   идти и после завершения run. Проверка задачи — без её блокировки: закрытие
   сразу после выдачи неотличимо от закрытия во время исполнения, которое
   исполнитель и так не видит. `POST /skill-invocations/{id}:cancel {reason}`
   — authority вызова (с `skills.invoke`) или `org.manage`; прочим — 404;
   `pending`/`running` → `cancelled`, повтор — тот же вызов, завершённый —
   `409 invocation_terminal`. У `running` исполнитель теряет lease (статус уже
   не `running`), но мог успеть подействовать: `error.details.wasRunning`.
   Кто начал отмену — `error.details.initiator` (и в payload события):
   `principal` для `:cancel`, `system` для отмены ядром (основание утрачено
   при claim, исход approval'а, CP-ADR-0061 §10); `cancelledBy` — principal,
   от чьего имени она записана. Если отменить можно по двум причинам сразу,
   утраченное основание называется раньше `skill_disabled` (CP-ADR-0061 §10).
   Approval, израсходованный отменённым вызовом, остаётся израсходованным.
2. **Повтор внешней записи без идемпотентности.** Публикация
   `external_write` с `idempotency = none` и `retryPolicy.maxAttempts > 1` —
   `422 invalid_skill_contract` (`details.field = retryPolicy.maxAttempts`).
   Уже опубликованные версии не трогаются (иммутабельны); таких на момент
   правки не было.
3. **Ответ `:claim`.** `skill` в ответе — `{id, name, version, protocol,
   sideEffects, riskLevel, contract}`: всё, что нужно исполнителю, и ничего
   сверх. `config` каталога остаётся за `org.read` (п. 8 амендмента M2.1).
4. **`_require_bound_run` без блокировки — обоснование.** Проверка «у
   principal нет `running` run под child handle» читает без блокировки, и
   параллельный старт такого run может закоммититься между проверкой и
   вставкой вызова. Это не эскалация: старт run не читает
   `skill_invocations`, поэтому история сериализуема как «вызов, затем старт
   run» — вызов принят в момент, когда потолка ещё не было, с правами, которые
   у principal тогда действительно были (теми же, с которыми он мог бы
   вызвать секундой раньше). Потолок HRS-7 ограничивает действия **внутри**
   run, а вызов без `runId`, сделанный до его старта, в run не входит.
   Блокировка (advisory lock на principal в обоих путях) сериализовала бы
   ровно в тот же порядок, поэтому не вводится.

### D. Правки по ревью M2.2 (TASK-000310): сеть, токены, очередь

Ревью M2.2 нашло SSRF и утечку токена: `endpoint` и `auth.audience`
брались из контракта без ограничений. Администратор tenant'а мог
зарегистрировать скилл с `endpoint=http://attacker/…` и
`auth.audience=control-plane` и получить write-токен runner'а, или с
`endpoint=http://127.0.0.1:<порт>` — читать внутренние сервисы хоста runner'а
(тело 2xx уходило в `outputs`, 4xx/5xx — в `error.details.body`). Контракт —
данные tenant'а; сеть и identity — исполнителя. Правило:

1. **Allow-list origin'ов.** `CONTROL_PLANE_SKILLS_HTTP_ALLOWED_ORIGINS` и
   `CONTROL_PLANE_SKILLS_MCP_ALLOWED_ORIGINS` — списки `scheme://host[:port]`
   (путь, query, учётные данные в URL — ошибка конфигурации, демон не
   стартует). Пусто для `http` — протокол не исполняется; для `mcp` остаются
   только `stdio:<имя>` из `CONTROL_PLANE_SKILLS_MCP_SERVERS`. Как и для
   `local`, фильтр — на сервере при `:claim`: исполнитель объявляет
   `httpOrigins`, `mcpEndpoints` (origin'ы и свои `stdio:<имя>`) и
   `audiences`, ядро выдаёт только вызовы, чей `endpoint` равен объявленному
   origin'у или лежит под ним (`origin/…`, без учёта регистра), а audience
   пуст или объявлен. Не-origin в объявлении — `422 invalid_executor_endpoint`.
   Исполнитель перед соединением проверяет разобранный URL ещё раз
   (`endpoint_not_allowed`, ретраимая — другой исполнитель может его
   исполнить). Серверный фильтр сравнивает строку, поэтому запись с
   явным портом по умолчанию (`https://h:443/…`) не выдаётся исполнителю с
   origin'ом `https://h` — отказ в безопасную сторону.
2. **Токен — только по `https`.** Контракт с `auth.audience` и
   `http://`-эндпоинтом — неретраимая `insecure_endpoint`, вызова нет (без
   `auth` plain http допустим: токена в нём нет).
3. **Allow-list audience.** `CONTROL_PLANE_SKILLS_ALLOWED_AUDIENCES`; вне
   списка — ретраимая `audience_not_allowed`, токен не запрашивается.
   `control-plane`, `iam` и собственная audience демона
   (`CONTROL_PLANE_IAM_AUDIENCE`) запрещены **всегда**: указать их в списке —
   ошибка конфигурации, а источник токенов исполнителя их не выдаёт даже при
   обходе проверки. «По умолчанию запрещены, но можно разрешить» не
   вводится: токен ядра в руках скилла — это права runner'а у стороннего
   кода, и законного сценария для этого нет (скиллу, которому нужно ядро,
   нужен свой principal).
4. **Адреса после DNS.** Хост резолвится исполнителем; если хотя бы один
   адрес не глобальный (loopback, private, link-local — включая
   `169.254.169.254`, CGNAT, зарезервированные, multicast, IPv4-mapped IPv6 от
   них) — неретраимая `endpoint_address_forbidden`, если хост не указан в
   `CONTROL_PLANE_SKILLS_PRIVATE_HOSTS` (in-cluster сервисы). Соединение идёт
   на **проверенный** адрес, а не на повторную резолюцию (DNS rebinding):
   URL запроса переписывается на IP, `Host`, SNI и проверка сертификата — по
   имени. Так устроен транспорт и `http`, и `mcp` по HTTP; редиректы не
   следуются в обоих (у MCP SDK по умолчанию следуются — клиент свой),
   прокси из окружения игнорируются (они обошли бы проверку).
5. **Ответ сервиса в ошибке.** `error.details` при не-2xx — `{status, body,
   bodyTruncated}`: первые 256 символов тела (через `redact_local_paths`), без
   заголовков. Тело 2xx по-прежнему — `outputs` и проверяется схемой
   контракта.
6. **`local` в подпроцессе.** По умолчанию (`CONTROL_PLANE_SKILLS_LOCAL_ISOLATION
   =process`) каждый вызов `local` — дочерний процесс (`forkserver`), который
   убивается при таймауте, потере lease или отмене; выход процесса без
   результата — ретраимая `skill_crashed`. Иначе следующая попытка
   идемпотентного скилла могла бы идти параллельно с брошенной. Режим
   `thread` оставлен для entrypoint'ов, которым нужен процесс демона; у него
   прежнее ограничение: поток не убить, брошенный вызов доработает, его
   результат отбрасывается, и следующая попытка **может идти параллельно** с
   ним — такой скилл обязан быть идемпотентным по `idempotencyKey`. Входы и
   выходы передаются между процессами pickle'ом — они JSON-совместимы.
7. **Не читается тип — задача не берётся.** Без `task_types.read` демон не
   может отличить Work, исполняемую Skill (§3); раньше он отдавал её
   кодовому адаптеру (fail-open). Теперь такая задача пропускается (claim не
   берётся), в лог — предупреждение, один раз на тип; отказ не кэшируется,
   так что выданное позже право подхватывается без рестарта. Следствие:
   runner'у нужен `task_types.read`, без него он не берёт типизированных задач.
8. **Очередь вызовов не ждёт Work.** Исполнитель запускает
   `CONTROL_PLANE_SKILLS_CONCURRENCY` (по умолчанию 1) собственных
   воркеров рядом с циклом Work: каждый берёт `:claim` → исполняет → сдаёт
   по одному вызову; многочасовой кодовый прогон очередь больше не держит.
   Сессия у демона одна (её переоткрытие сериализовано). `0` — прежний режим:
   вызов берётся только когда Work нет. При остановке воркеры не берут новых
   вызовов и дожидаются текущих (ограничено их `timeoutSeconds`).
9. **Scopes токена — из контракта, потолок — у IAM** (TASK-000440).
   Обмен PAT на audience скилла раньше брал scopes из
   `CONTROL_PLANE_IAM_SCOPES` — scopes ядра (`control-plane:read
   control-plane:write`); для чужой audience IAM отвечал `403
   scope_not_allowed`, а без scopes выдавал токен, который сервис отвергал
   как `insufficient_scope`. Теперь: контракт **называет** scopes —
   `implementation.auth.scopes`, список до 20 непустых строк-scope'ов
   (`^[A-Za-z0-9_.:*/-]{1,200}$`, без пробелов; дубликаты схлопываются,
   строка, похожая на секрет, — `secret_material_rejected`; иначе —
   `invalid_skill_contract` с `field=implementation.auth.scopes`,
   проверка при публикации). Исполнитель **просит** у IAM ровно их для
   `auth.audience`; нет `auth.scopes` — обмен без scopes. Scopes ядра в чужую
   audience не переносятся никогда. IAM **режет** запрос по `scopeCeiling` PAT
   исполнителя: граница полномочий скилла — потолок PAT, который задаёт
   владелец раннера, а не контракт, который пишет администратор tenant'а.
   Кэш credential исполнителя — по паре (audience, множество scopes): два
   контракта одной audience с разными scopes получают разные токены.

## Амендмент 2026-09-23: проводной контракт skill-sdk (TAI-ADR-0045)

Скиллы теперь пишутся на `skill-sdk` — отдельном компоненте суперпроекта.
Исполнитель поддерживает его **без зависимости от SDK**: только по проводному
контракту, который SDK обязуется соблюдать.

1. **`local`.**
   - Обнаружение. Помимо модульного `CONTRACT` исполнитель находит объекты
     модуля с атрибутом `__skill_contract__`. Атрибут ищется на типе объекта,
     чтобы не вызывать чужой `__getattr__`.
   - Вызов. Если у entrypoint есть `__skill_invoke__(inputs, meta)`, исполнитель
     вызывает его синхронно — в потоке или в дочернем процессе, как и прежде.
     В `meta` передаются `invocationId`, `idempotencyKey`, `timeoutSeconds` и
     `skill`. Ответ — `{outputs, cost}`.
   - Функции `run(inputs)` работают как раньше.
2. **`http`.**
   - Заголовок ответа `X-Skill-Cost` (JSON) — это cost вызова.
   - Тело ошибки `{"error": {code, message, retryable, details}}` задаёт код и
     повторяемость. Статус без такого тела даёт прежние `http_<status>` и
     повторяемость по статусу.
   - `details` остаётся выдержкой исполнителя: статус и начало тела, без
     заголовков.
3. **`mcp`.**
   - `_meta["skill/cost"]` результата — это cost. Ключ нейтральный, как и
     `X-Skill-Cost`: имя продукта в проводной контракт ядра не входит (ADR-0022).
   - Результат `isError` с единственным текстовым блоком
     `{"error": {code, retryable, …}}` задаёт код и повторяемость. Иначе
     остаётся прежний неповторяемый `tool_error`.
4. **Cost** уходит в `:complete` (поле `cost` запроса, которое уже было в API)
   только тогда, когда реализация его сообщила.

Проверка — `tests/unit/test_skill_executor.py` (заглушка `tests/skill_stubs/sdk_like.py`
в форме SDK) и сквозной `skill-sdk/tests/test_executor_e2e.py` суперпроекта.

## Амендмент 2026-09-29: адрес реализации — не часть обещания (§1)

Перенос инсталляции на новый хост упёрся в неизменяемость:
у опубликованного `notify.send@1` в `implementation.endpoint` записан адрес
прежнего хоста, а поднять версию ради нового адреса значит переписать всех,
кто зовёт `notify.send@1` (процессы, тесты пакетов, описания агентов). Адрес
при этом — свойство инсталляции, а не того, что скилл обещает: в открытой
поставке у каждой инсталляции он свой.

1. **Изменяемо одно поле.** У опубликованной версии `PATCH /skills/{id}`
   принимает `endpoint` — новое значение `contract.implementation.endpoint`.
   Протокол, `auth`, `entrypoint`, схемы, права, политика, таймауты и
   остальной контракт по-прежнему заморожены: их изменение — новая версия.
   Новое значение проходит ту же проверку, что при публикации (`http` —
   только `http(s)://`); у `local` адреса нет — `422 invalid_skill_contract`,
   у строки каталога без контракта — `409 skill_not_invocable`.
2. **База держит ту же границу.** Триггер `skills_immutable` пропускает
   изменение `contract` только если без `implementation.endpoint` старый и
   новый документы равны, контракт не появляется и не исчезает, а адрес
   остаётся строкой (миграция `e6b3d8f1a2c9`).
3. **Аудит.** Событие `skill.updated` несёт `changedFields: ["endpoint"]` и
   `endpoint: {from, to}`; `rowVersion` растёт, `If-Match` обязателен.
4. **Безопасность не меняется.** Право то же, что у публикации
   (`org.manage`), а решает, пойдёт ли вызов по новому адресу, по-прежнему
   исполнитель: allow-list origin'ов и audience (амендмент M2.2, D). Вызовы,
   уже выданные исполнителю, дорабатывают по старому адресу; ещё не
   выданные уходят исполнителям, чей allow-list покрывает новый.
5. **Пакеты.** `tools/cp_packages.py` сравнивает контракт без адреса и,
   если отличается только он, переводит версию на новый адрес `PATCH`'ем
   вместо отказа «поднимите spec.version».

## Амендмент 2026-09-29: вызов `mcp` несёт invocationId и ключ идемпотентности (§5)

`http` кладёт в тело `invocationId` и `idempotencyKey`, `local` (skill-sdk) —
в `meta`, а `tools/call` для `mcp` уходил без них: скилл не мог отличить
повтор того же вызова (ретрай после потерянной аренды, транспортной ошибки)
от нового и дублировал эффект (ревью TASK-000946).

1. **Ключи.** Исполнитель передаёт в `_meta` запроса `tools/call`:
   - `skill/invocationId` — id вызова (`skill_invocation.id`);
   - `skill/idempotencyKey` — ключ идемпотентности вызова, тот же, что `http`
     кладёт в поле `idempotencyKey` тела (`null`, если его нет).
   Префикс `skill/` — тот же нейтральный, что у `skill/cost` результата
   (амендмент «проводной контракт skill-sdk», п.3).
2. **Семантика** та же, что у `http`: повтор с тем же ключом скилл вправе
   не исполнять заново и вернуть прежний результат. Дедупликация — на
   стороне скилла; исполнитель ключ не толкует.

Проверка — `tests/unit/test_skill_executor.py` (`_meta` запроса и повтор с тем
же ключом на заглушке MCP-сервера) и сквозной
`skill-sdk/tests/test_executor_e2e.py` суперпроекта.

## Амендмент 2026-09-30 (TASK-001197): экземпляр процесса — основание `external_write` (§4)

Шаг процесса (`call.skill`, CP-ADR-0074 п.8) вызывает скилл от личности
процесса, без задачи, прогона и approval. Ни одно из трёх оснований §4 к
нему не подходило, и вызов `external_write` из процесса всегда получал
`403 skill_side_effect_not_authorized`.

1. **Четвёртое основание — экземпляр процесса.** Вызов, который делает
   движок процессов по шагу живого экземпляра, получает основание
   `{kind: "process", instanceId, activityId, definitionKey,
   definitionVersion}`. Довод тот же, что у `execution` типа задачи: опубликованная неизменяемая
   версия процесса сама называет эту версию скилла (`name@version`). Кто её
   публикует, отвечает правом `processes.write`, а вызов идёт от личности
   процесса с её правами (`skills.invoke`).
2. **Только движок.** Экземпляр передаёт `invoke_skill` исполнитель
   намерений процесса. Ни в одном маршруте это основание не названо:
   `POST /skills/{ref}:invoke` той же личностью без gate — по-прежнему
   `403`.
3. **Основание — шаг, а не экземпляр; проверяется при выдаче** (`:claim`,
   как у остальных). Экземпляр отменён или отсутствует — `cancel`
   (`basis_revoked`, причина `process_cancelled`). Activity шага
   (`activityId`) уже закрыта — `cancel` (`process_step_closed`): таймаут
   шага, его повтор (`retry` ставит новый вызов с новым ключом
   идемпотентности), `catch`, падение экземпляра. Приостановлен — `hold`
   (`process_suspended`), вызов ждёт `:resume`. Разбор дела после
   завершения держит свою activity (`retro_skill`) открытой, поэтому его
   вызов выдаётся. Вызов, выданный до закрытия шага, уже исполняется —
   отозвать его нельзя (правка по ревью TASK-001197: основание, переживавшее
   свой шаг, давало две внешние записи на один шаг).
4. Основание записывается только у `external_write`. Остальные вызовы
   процесса, как и прежде, идут без него.

Проверка — `tests/integration/test_process_skill_basis.py`.

## Амендмент 2026-10-03: фильтр по типу в `/work/available`, демон берёт свои типы (§3, A.4; TASK-001359)

Найдено ревью TASK-001354. Демон вида `skills` (без адаптера, например
`selfdev-skills` без `work.workspace`) читал одну страницу `GET
/work/available` (`WORK_SCAN` = 50) по всему арендатору, а тип отсекал на
клиенте (A.4). Порядок очереди — приоритет, затем от старых к новым: больше
50 доступных задач чужих типов старше и важнее — и задачи его типов не
брались, без ошибки и без следа в журнале.

### Ж1. `typeKey` у `GET /work/available`

Параметр **`typeKey`** — ключ типа задачи, повторяемый (`?typeKey=a&typeKey=b`
— задачи любого из типов), совпадает с любой версией типа. Он только сужает
выборку: применяется в запросе кандидатов до страницы, поэтому курсор и
`nextCursor` идут по отфильтрованной очереди; права (`tasks.read`), видимость,
eligibility и прочие условия доступности те же. Сочетается с `workspaceId`
/`includeDescendants`, `projectId`/`includeSubprojects`, `assigneeId`,
`assignedToMe` и курсором. Ключ, которого нет ни у одного типа, даёт пустую
выборку, а не всю очередь. Пустой или пробельный ключ и больше 50 значений —
`422 invalid_type_key` (не молчаливое «без фильтра»). Клиент —
`list_available_work(type_keys=...)`; пустой список он отвергает
(`ValueError`): без параметра запрос вернул бы всю очередь.

### Ж2. Демон

- **Вид `skills`** (нет адаптера, есть исполнитель скиллов) передаёт `typeKey`
  — ключи типов, у которых хотя бы одна версия объявляет `execution` со
  скиллом, который он исполняет (то же решение, что `_takes`/`can_execute`
  по задаче; `work.taskTypes`, если задан, сужает дальше). Список читается из
  `GET /task-types` (до 20 страниц по 100) и держится 60 с: тип,
  опубликованный позже, берётся после этого. Нет ни одного такого типа —
  очередь не читается вовсе. Нет `task_types.read`, типов больше 50 или
  больше 20 страниц — список без фильтра, как раньше, с предупреждением в
  журнале. Проверка по каждой задаче (A.4) остаётся: фильтр совпадает с
  любой версией ключа, а `execution` у версий может различаться.
- **Остальные виды** фильтр не передают — их выборка прежняя.
- **Страницы — у всех видов.** Если из страницы не взята ни одна задача
  (клиентский фильтр отсеял всё, claim проиграл гонку или отказан по самой
  задаче) и есть `nextCursor`, демон читает следующую страницу — не больше
  `WORK_PAGES` = 5 за цикл (250 задач), дальше — следующий цикл. Отказ claim
  уровня tenant, как и прежде, останавливает цикл (CP-ADR-0073 Е8).

### Ж3. Проверки

`tests/integration/test_discovery_v03.py` (`typeKey` с курсором и по
версиям, повтор, сочетание с workspace и `assignedToMe`, eligibility не
расширяется, неверные значения — `422`),
`tests/client/test_skill_executor.py` (60 доступных задач чужого типа старше
и важнее и одна задача типа исполнителя — демон её берёт одним запросом с
`typeKey`; без исполнимых типов очередь не читается; обычный демон с
`work.taskTypes` переходит на вторую страницу),
`tests/unit/test_agent_work_listing.py` (выбор ключей, кэш, откаты к
выборке без фильтра, потолок страниц, пустой фильтр в клиенте).

## Conformance

```conformance
- route: "POST /skills/{skill_ref}:invoke"
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/*.py", pattern: '"/skill-invocations'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/db/models.py", pattern: '__tablename__ = "skill_invocations"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/db/models.py", pattern: "side_effects"}
  repo: control-plane
- grep: {path: "src/control_plane_agent/**/*.py", pattern: "skill_invocation"}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/*.py", pattern: '"/skill-invocations/\{invocation_id\}:cancel"'}
  repo: control-plane
- grep: {path: "src/control_plane/infrastructure/db/models.py", pattern: "uq_skill_invocations_run_execution"}
  repo: control-plane
- grep: {path: "src/control_plane_agent/skills.py", pattern: "CONTROL_PLANE_SKILLS_LOCAL_PACKAGES"}
  repo: control-plane
- grep: {path: "src/control_plane_agent/skills.py", pattern: "CONTROL_PLANE_SKILLS_HTTP_ALLOWED_ORIGINS"}
  repo: control-plane
- grep: {path: "src/control_plane_agent/skills.py", pattern: 'RESERVED_AUDIENCES = frozenset\(\{"control-plane", "iam"\}\)'}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/schemas.py", pattern: "http_origins"}
  repo: control-plane
- grep: {path: "src/control_plane/domain/skill_contract.py", pattern: "def replace_endpoint"}
  repo: control-plane
- grep: {path: "src/control_plane/api/v1/harness.py", pattern: 'alias="typeKey"'}
  repo: control-plane
- grep: {path: "src/control_plane_agent/main.py", pattern: "async def _listing_type_keys"}
  repo: control-plane
```
