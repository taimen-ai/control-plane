"""Artifact content uploads (CP-ADR-0072 §2).

``PUT /artifact-contents`` takes the raw bytes of a file as the request body,
spools them to disk while hashing and stores them as an upload. The answer is
a ``contentRef`` that ``POST /artifacts`` references. The body never sits in
memory whole; its ceiling is ``CP_ARTIFACT_MAX_BYTES`` (the body-size
middleware applies it to this path instead of the general limit).
"""

import asyncio

import regex
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import (
    AuthDep,
    ContentStoreDep,
    SessionFactoryDep,
    SettingsDep,
)
from control_plane.api.errors import BodyTooLargeError
from control_plane.api.v1.schemas import ERROR_RESPONSES, ArtifactContentOut
from control_plane.application.commands import artifacts as commands
from control_plane.domain.errors import BadRequestError
from control_plane.infrastructure.content_store import SpoolLimitExceeded, spool
from control_plane.infrastructure.db.engine import transaction

router = APIRouter(tags=["artifacts"])

_MEDIA_TYPE_MAX = 255
# type "/" subtype (RFC 6838 restricted names), optional parameters.
_MEDIA_TYPE = regex.compile(
    r"^([A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126})"
    r"(\s*;.*)?$"
)


def normalize_media_type(value: str | None) -> str:
    """The declared media type, base lower-cased; 400 when absent or malformed."""
    text = (value or "").strip()
    match = _MEDIA_TYPE.match(text) if len(text) <= _MEDIA_TYPE_MAX else None
    if match is None:
        raise BadRequestError(
            "invalid_request",
            "Content-Type with the media type of the file is required",
            details={"header": "Content-Type"},
        )
    params = (match.group(2) or "").strip()
    return match.group(1).lower() + (f"; {params.lstrip(';').strip()}" if params else "")


# visibility: tenant — an upload is its uploader's until an artifact (the seam) uses it
@router.put(
    "/artifact-contents",
    response_model=ArtifactContentOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Upload the bytes of an artifact; returns a contentRef",
)
async def upload_artifact_content(
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: ContentStoreDep,
) -> JSONResponse:
    await commands.authorize_upload(ctx)
    media_type = normalize_media_type(request.headers.get("content-type"))
    active_store = commands.require_store(store)
    try:
        spooled = await spool(request.stream(), limit=settings.artifact_max_bytes)
    except SpoolLimitExceeded:
        raise BodyTooLargeError() from None
    try:
        async with transaction(session_factory) as session:
            upload = await commands.record_upload(
                session,
                ctx,
                active_store,
                spooled=spooled,
                media_type=media_type,
                ttl_seconds=settings.artifact_upload_ttl_seconds,
            )
            body = ArtifactContentOut(
                content_ref=commands.content_ref(upload.id),
                size_bytes=upload.size_bytes,
                media_type=upload.media_type,
                sha256=upload.sha256,
                expires_at=upload.expires_at,
            ).model_dump(mode="json", by_alias=True)
    finally:
        await asyncio.to_thread(spooled.remove)
    return JSONResponse(status_code=201, content=body)
