# Потребитель событий: `control_plane_client.events`

SDK потребителя событий ядра ([CP-ADR-0069](../adr/0069-event-consumer-sdk.md)).
Подписка — это фильтр чтения журнала ([CP-ADR-0068](../adr/0068-event-filters-catalog-versions.md)),
курсор хранит потребитель; SDK делает остальное: сохраняет курсор, пропускает уже
обработанные события по `event.id`, повторяет упавшую страницу и просыпается по
WebSocket, не дожидаясь опроса.

## Установка

```bash
uv add 'control-plane-client[events]'   # websockets + sqlalchemy[asyncio]
```

Экстры по отдельности: `[ws]` — пробуждение по WebSocket (без него потребитель
только опрашивает), `[sqlalchemy]` — хранилище курсора в SQL. Драйвер базы
(`psycopg`, `asyncpg`, `aiosqlite`) ставит потребитель.

## Минимальный потребитель

```python
from control_plane_client import ControlPlaneClient
from control_plane_client.events import Event, EventConsumer
from control_plane_client.events.sqlalchemy import SqlAlchemyCursorStore

store = SqlAlchemyCursorStore(engine)  # AsyncEngine базы потребителя


async def handler(event: Event) -> None:
    # Исключение — событие придёт снова; возврат — событие обработано.
    await notify(event["payload"])


async with ControlPlaneClient(url, credential) as client:
    consumer = EventConsumer(
        client, ["approval."], workspace_id, store, handler, name="notifications"
    )
    await consumer.run()  # до consumer.stop() или отмены
```

Полный пример — [`client/examples/approval_consumer.py`](../../client/examples/approval_consumer.py).

Параметры `EventConsumer(client, types, workspace_id, cursor_store, handler, *, name, …)`:

| Параметр | Смысл |
|---|---|
| `types` | Префиксы типа (`approval.`, `task.verification_failed`); пусто — все типы |
| `workspace_id` | Поддерево workspace; `None` — весь tenant (нужно `events.read` на tenant) |
| `cursor_store` | Где хранится позиция и отметки обработанных событий |
| `handler` | `async (event) -> None`; событие — как в `GET /events` |
| `name` | Ключ курсора в хранилище: одно имя — одна позиция |
| `start` | Без сохранённого курсора: `"earliest"` (весь журнал, по умолчанию) или `"latest"` (только новое) |
| `poll_interval` | Опрос, секунд (30); WebSocket будит раньше |
| `page_size` | Размер страницы `GET /events` (200) |
| `websocket` | Включить пробуждение по WebSocket (да) |
| `retry_initial`, `retry_max` | Пауза перед повтором страницы: от 1 с, удваивается до 60 с |

`consumer.drain()` читает всё доступное сейчас и возвращает число обработанных
событий — для задач по расписанию и тестов.

## Гарантии

- **Порядок** — порядок журнала `(tx_id, sequence)`; обработчик вызывается по
  одному событию.
- **Без потерь.** Курсор двигается только после обработки события. Упавший
  обработчик, сетевой сбой или `5xx` — страница читается заново с сохранённого
  курсора после паузы; событие не пропускается никогда. Чтобы отбросить событие,
  обработчик возвращается без исключения.
- **Без повторов.** Событие обрабатывается внутри единицы работы хранилища:
  отметка `event.id` и новый курсор записываются вместе. Повторно доставленное
  событие (повтор страницы, сервер, который при сомнении отдаёт лишнее) пропускается.
  У `SqlAlchemyCursorStore` в эту же транзакцию попадает то, что обработчик пишет
  через `SqlAlchemyCursorStore.session()`: падение процесса до фиксации не
  оставляет ни эффекта, ни отметки, после — оставляет оба. Для эффектов вне этой
  базы (сообщение в мессенджер) гарантия — «хотя бы один раз»: передавайте
  `event.id` как ключ идемпотентности получателю.
- **Отказ в подписке** — нет `events.read` на workspace (403), нет workspace (404),
  неверный фильтр (422), не принят credential (401) — `run()` завершается
  исключением: повтор этого не изменит.
- **Один процесс на имя.** Два процесса с одним `name` SQL-хранилище сериализует по
  событию (блокировка строки курсора), но внешние эффекты они всё равно могут
  задвоить.

## Хранилища курсора

Протокол `CursorStore`: `load(consumer)`, `handle(consumer, event_id, cursor)` —
асинхронный контекст-менеджер, который входит с `True` для необработанного
события и на чистом выходе записывает отметку и курсор, — и `advance(consumer,
cursor)` для перехода через отфильтрованные события.

- `MemoryCursorStore` — в памяти процесса: для тестов и потребителей, которым
  можно начать сначала.
- `SqlAlchemyCursorStore(engine, *, metadata=None, prefix="", dedup_retention=7 дней)` —
  таблицы `<prefix>event_cursors` (потребитель → курсор) и `<prefix>handled_events`
  (потребитель × `event_id`). Отметки старше `dedup_retention` удаляются при
  движении курсора: повторная доставка бывает рядом с курсором, а не на дни позади.
  Схему создаёт миграция потребителя — передайте свой `MetaData` (Alembic
  `target_metadata`) или объявите таблицы через `cursor_tables(metadata)`; без
  миграций — `await store.create_tables()`.

## CloudEvents

`to_cloudevent(event, source=None)` — событие журнала в CloudEvents 1.0 (структурный
JSON): `id`, `type`, `time` ← `occurredAt`, `subject` = `<entityType>/<entityId>`,
`data` ← `payload`, `datacontenttype: application/json`; поля конверта без пары в
CloudEvents — расширения `tenantid`, `workspaceid`, `entitytype`, `entityid`,
`schemaversion`, `actorid`, `correlationid`, `causationid` (пустые опускаются).
`source` — URI установки (`https://cp.example.com/tenants/<id>`), по умолчанию
`/control-plane/tenants/<tenantId>`. Схема `data` — версия `schemaversion` типа в
[каталоге](catalog.md). Тот же каталог работающее ядро отдаёт по `GET /api/v1/event-types`
(право `events.read`, ETag; CP-ADR-0068, амендмент А) — с группой типа, всеми его
версиями и ключом подписи `event.<type>`.
