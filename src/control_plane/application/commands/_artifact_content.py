"""Content fields every writer of ``artifact.created`` puts in its event
(schema v2, CP-ADR-0072 §11), the content lock, and storing bytes the core
hands in itself. Kept apart from ``artifacts`` so the skill, rule and
verification commands can use them without importing the upload path."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.infrastructure.content_store import (
    ContentStore,
    SpooledFile,
    object_key,
    spool,
)
from control_plane.infrastructure.db.models import Artifact

CONTENT_NONE = "none"
CONTENT_STORED = "stored"
CONTENT_PURGED = "purged"


def artifact_event_fields(artifact: Artifact) -> dict[str, Any]:
    """Size, media type, checksum and state — never the content itself."""
    return {
        "sizeBytes": artifact.size_bytes,
        "mediaType": artifact.media_type,
        "sha256": artifact.sha256,
        "contentState": artifact.content_state or CONTENT_NONE,
        "typeVersion": artifact.type_version,
    }


async def lock_content(session: AsyncSession, tenant_id: uuid.UUID, sha256: str) -> None:
    """Serialize every change of who needs the object (tenant, sha256)."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:ac:{tenant_id}:{sha256}", 0)))
    )


async def store_core_content(
    session: AsyncSession, store: ContentStore, tenant_id: uuid.UUID, data: bytes
) -> SpooledFile:
    """Put bytes the core itself hands in (a skill's typed output) into the store.

    No upload row: nobody references these bytes by ``contentRef``. The
    content lock is held until the transaction ends, so the object is not
    swept between ``exists`` and the artifact that points at it. Raises
    ``ContentStoreUnavailable``; the returned file is already removed.
    """

    async def once() -> AsyncIterator[bytes]:
        yield data

    spooled = await spool(once(), limit=len(data))
    try:
        await lock_content(session, tenant_id, spooled.sha256)
        key = object_key(tenant_id, spooled.sha256)
        if not await store.exists(key):
            await store.put(key, spooled.path, spooled.size)
    finally:
        spooled.remove()
    return spooled
