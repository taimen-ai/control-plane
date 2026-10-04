# Фикстурные журналы экземпляров — ворота replay

Фича `process-observability`, пункт P018 (FR-028, FR-031, SC-009). Здесь лежит то,
что ядро **без фичи** сохранило о своих экземплярах. Код фичи должен переиграть эти
журналы без единого расхождения: решения, намерения и итоговое состояние.

## Источник

- Записаны кодом `main` на коммите `5f352a3867952e8e157bcf7628d4ebc35938d018`
  (2026-09-30, «Merge TASK-001165»). Фичи в этом коммите нет: у
  `process_definitions` нет столбца `engine_revision`, у версий — ревизия 1.
- Код ветки фичи при записи не участвовал. Запись шла по публичному API и воркеру
  `main`, как в работе: публикация, наблюдения, завершение задач, срабатывание
  таймеров воркером, команды оператора, `packages:plan/apply` с миграцией.
- С точки ветвления фичи (`35ad4ae`) движок в `main` менялся один раз: чтение
  ответа `recall` (TASK-001134). Шагов `recall` в фикстурах нет, так что эта
  разница на replay не влияет.

## Состав

| Файл | Процесс | Что покрыто |
|---|---|---|
| `due-and-escalations.json` | `replay-due` | `due: {at}` по календарю (`cal.addWorkdays`) и от данных, эскалации `notify` и `raise` с перехватом, перенос срока сменой данных, новая версия календаря, приостановка с возобновлением и без, отмена |
| `duration-due.json` | `replay-duration` | `due: PT2S`, таймер эскалации, сработавший у живого воркера |
| `migration.json` | `replay-migrate` | миграция 1 → 2 через `packages:apply` с переименованием шага (`map`), шаг до и после миграции, приостановка на новой версии |

В каждом файле:

- `recordedWith` — ветка и коммит записи;
- `catalog` — агент и типы задач, которые ссылаются на процесс;
- строки таблиц как есть:
  - `calendars`;
  - `definitions` (`process_definitions`);
  - `instances` (`process_instances`), у каждого экземпляра — `journal`
    (`process_instance_events`) и `story`, одна строка о том, что с ним было.

## Кто читает

- `tests/unit/test_process_replay_fixtures.py` — чистый replay каждого экземпляра под
  ревизией 1 (`process_replay.replay` и `first_divergence`, как у
  `POST /process-definitions/{key}:replay`). Там же проверки, что фикстуры записаны
  `main` и покрывают `due`, эскалации, приостановку и миграцию, и перенос
  приостановленного экземпляра `main` на ревизию 2.
- `tests/integration/test_process_replay_gate.py` — строки фикстур пишутся в
  PostgreSQL как есть, и скрипт `scripts/process_replay_all.py` переигрывает
  tenant через API.

## Перезапись

Фикстуры не правят руками: правленый журнал уже не записан `main`. Перезаписывают
только на коммите `main` без фичи и меняют коммит здесь и в `recordedWith`.

```sh
git worktree add /tmp/cp-main <коммит main>   # рядом должны лежать ../../sdk/platform-auth-sdk и ../memory-service
cp tests/fixtures/process_journals/recorder.py /tmp/cp-main/tests/integration/test_record_process_journals.py
cd /tmp/cp-main && uv sync --frozen
PROCESS_JOURNALS_OUT=<каталог> PROCESS_JOURNALS_COMMIT=<коммит main> \
  uv run pytest -q tests/integration/test_record_process_journals.py
```

Нужна тестовая PostgreSQL (`CP_TEST_DATABASE_URL`). Записанные файлы из
`<каталог>` скопировать сюда. Время в журналах — момент записи, поэтому после
перезаписи меняются все файлы целиком.

## Прогон по стенду (выкатка, P025)

Legacy-ключей на стенде нет: скрипт берёт credential так же, как канонический
клиент (`control_plane_client.credentials.resolve_credential`). Обычный путь —
access token IAM: оператор один раз входит `iam auth login`, Platform Access
Token лежит в локальном хранилище IAM, а скрипт обменивает его на access token
аудитории `control-plane` и обменивает заново, когда тот истекает:

```sh
CONTROL_PLANE_IAM_URL=<адрес IAM> CONTROL_PLANE_IAM_TENANT=<tenant IAM> \
  uv run python scripts/process_replay_all.py --base-url https://cp.taimen.ai --out replay-report.json
```

На раннере без хранилища PAT передаётся через окружение IAM
(`IAM_CREDENTIAL_MODE=environment`, `IAM_PLATFORM_ACCESS_TOKEN`). Если IAM
недоступен — аварийный ключ (CP-ADR-0065): его выпускает владелец хоста
(`python -m control_plane.break_glass issue …` в контейнере API), скрипт берёт
его из `CONTROL_PLANE_API_KEY`, а после прогона ключ отзывают
(`… break_glass revoke`). На compose без IAM подходит ключ установки в
`CONTROL_PLANE_API_KEY` и `--base-url http://127.0.0.1:8000` (адрес по
умолчанию, или `CONTROL_PLANE_SERVER`). Principal нужны права
`processes.read` и `packages.test`.

Скрипт проходит все процессы, которые видит principal, или только названные
`--process <ключ>` (повторяемый). По каждой версии, на которой живут
экземпляры, он отправляет спецификацию этой версии и её экземпляры пачками (до
200) в `POST /process-definitions/{key}:replay`. Код выхода:

- `0` — расхождений нет, переиграны все экземпляры;
- `1` — есть расхождение, отказ проверки версии или пропущенный экземпляр;
- `2` — вызов API не удался, процесса из `--process` нет (или он не виден),
  credential не найден или IAM отказал в обмене.

Credential не принимается аргументом командной строки и в отчёт не попадает. В
отчёте есть ключи экземпляров, а при расхождении — фрагмент журнала: что
записано и что решает код.
