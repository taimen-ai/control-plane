# ADR-0027: Явные replayable-наблюдения («remember») как событие журнала

Статус: Принято (v0.4)

## Контекст

Harness (человек или агент) должен уметь явно сохранить finding/decision в
долговременную память. Если такая запись живёт только во внешней Memory,
rebuild Memory её теряет; если harness пишет в Memory напрямую, он получает
Memory-credentials и непроверяемую provenance.

## Решение

`POST /api/v1/observations` (permission `observations.write`) — обычная
команда Control Plane, которая пишет **одно доменное событие**
`observation.recorded` (append-only журнал, одна транзакция, идемпотентность
через `Idempotency-Key`). Отдельной rich-таблицы нет: журнал и есть
authoritative replayable envelope; adapter доставляет его в Memory как любое
другое memory-worthy событие.

- Provenance обогащается сервером из аутентифицированного контекста:
  tenant, principal (actor), session; подделать чужую атрибуцию клиент не
  может. Scope-ссылки (task/run/workspace/session) резолвятся строго внутри
  tenant'а вызывающего — чужие id дают 404 до какой-либо записи.
- Kinds: baseline `finding, decision, constraint, note, summary, result,
  preference, external_fact`; ontology не закрыта — допустим любой kind по
  регексу Memory-контракта (`^[a-z0-9][a-z0-9._-]{0,127}$`).
- Пределы: content ≤ 64 KiB, structured data ограничена; человек и агент
  идут одним путём — никаких `human_memory`/`agent_memory` и привилегий по
  `principal.kind`.
- Запрещено сохранять скрытые рассуждения: API принимает только
  intentionally externalized знание; harness-интеграции (MCP `cp_remember`)
  не автозаписывают chain-of-thought, сырые промпты или историю терминала —
  это контрактное требование, зафиксированное в описаниях инструментов.

## Последствия

Explicit-знание переживает rebuild Memory (replay журнала), наследует
audit-контур событий и «one command = one transaction», не создаёт циклов с
core lock ordering (команда не трогает session→task→claim→run локи, кроме
безлоковых SELECT-проверок принадлежности).

## Дополнение (ADR-0057)

Внешние наблюдения несут `source`, `dedupKey`, `observedAt`, `supersedes`,
`externalRef`; повтор пары `(source, dedupKey)` тем же автором в tenant'е
возвращает существующее наблюдение (`200`) без нового события. Для этого введена
таблица ключей `observation_dedup_keys`; authoritative записью по-прежнему
остаётся событие журнала — см. [ADR-0057](0057-external-observation-intake.md).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- route: "POST /observations"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/observations.py, pattern: 'event_type="observation\.recorded"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/observations.py, pattern: 'await authorize\(ctx, Permission\.OBSERVATIONS_WRITE\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/observations.py, pattern: '_MAX_CONTENT_CHARS = 65_536'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/observations.py, pattern: 'return await execute_write\('}
  repo: control-plane
- absent: {path: src/control_plane/infrastructure/db/models.py, pattern: '__tablename__ = "(observations|human_memory|agent_memory)"'}
  repo: control-plane
```
