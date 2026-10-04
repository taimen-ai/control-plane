"""``GET /event-types``: the event catalog of the core as a console reads it
(CP-ADR-0068, amendment of 2026-10-04).

Each type is what ``docs/events/catalog.json`` says of it — entity, description,
current version and the payload schema of every version — plus its ``group``
(the prefix before the first dot, what a ``types=`` filter of ``GET /events``
takes), its ``supportedVersions`` and ``labelKey`` (``event.<type>``, the key of
the console's own dictionary).

Captions go in the language asked by the chain of views (CP-ADR-0080 §5): the
locale itself, its base language, else the default. The core holds its strings
in English only — the catalog descriptions — so today every request gets
``locale: en``; a console that has its own caption for ``event.<type>`` shows
that one.

The catalog is code: it changes only with a release. The body of a locale is
built once per process and its sha256 is the ETag.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from control_plane.application.authorization import AuthContext, authorize
from control_plane.domain.enums import Permission
from control_plane.domain.event_catalog import EventType, event_types
from control_plane.domain.views import resolve_locale

DEFAULT_LOCALE = "en"
LABEL_KEY_PREFIX = "event."


def _core_strings() -> dict[str, Mapping[str, str]]:
    """Captions of the types by locale; English is the catalog itself."""
    return {DEFAULT_LOCALE: {entry.type: entry.description for entry in event_types()}}


CORE_LOCALES: tuple[str, ...] = tuple(_core_strings())


@dataclass(frozen=True)
class EventTypesView:
    body: dict[str, Any]
    etag: str


def group_of(event_type: str) -> str:
    return event_type.split(".", 1)[0]


def _item(entry: EventType, captions: Mapping[str, str]) -> dict[str, Any]:
    return {
        "type": entry.type,
        "group": group_of(entry.type),
        "entityType": entry.entity_type,
        "description": captions.get(entry.type, entry.description),
        "labelKey": LABEL_KEY_PREFIX + entry.type,
        "currentVersion": entry.current.version,
        "supportedVersions": [v.version for v in entry.versions],
        "versions": {
            str(v.version): {**({"changes": v.changes} if v.changes else {}), "schema": v.schema}
            for v in entry.versions
        },
    }


@cache
def event_types_view(locale: str) -> EventTypesView:
    """The body for one of :data:`CORE_LOCALES` and its ETag; built once per process."""
    captions = _core_strings()[locale]
    body = {"locale": locale, "items": [_item(entry, captions) for entry in event_types()]}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return EventTypesView(body=body, etag=f'"event-types-{digest}"')


async def list_event_types(ctx: AuthContext, locale: str | None) -> EventTypesView:
    """Every type of the catalog with captions in ``locale`` by the chain of views."""
    await authorize(ctx, Permission.EVENTS_READ)
    return event_types_view(resolve_locale(CORE_LOCALES, DEFAULT_LOCALE, locale))
