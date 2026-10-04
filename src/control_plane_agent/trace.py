"""Execution trace of an autonomous run: a bounded transcript and tool-call actions.

ADR-0051. Until now an adapter published one summary artifact and one run
action per turn; everything the agent said and every tool it called stayed in
a local log on the runner. Operators reading the console could see *that* a
run happened, not *what* it did. This module is the shared, vendor-neutral
half of the change: the adapters (Claude Code, Codex) feed their own event
streams into a ``TranscriptBuilder`` and publish what it produced.

Two rules keep this inside the existing artifact and audit contracts:

- **The transcript is an artifact, bounded and redacted.** It is one JSON
  document (``agent-transcript/1``) capped at ``MAX_TRANSCRIPT_BYTES``; every
  string goes through ``redact_local_paths`` and ``redact_credentials`` before
  it is kept; hidden reasoning (``thinking`` blocks, reasoning summaries) is
  counted but never stored. A transcript that still trips the portability
  guard is withheld as a whole rather than failing the run.
- **The transcript carries no NUL.** The API rejects U+0000 anywhere in a JSON
  body (CP-ADR-0083), so one tool that printed binary data would cost the whole
  transcript; every string is cleaned with ``replace_nul`` before it is kept.
- **Run actions carry references, not payloads** (ADR-0019). One action per
  tool call: its name, a short summary of the input and a pointer into the
  transcript. Inputs and outputs live in the artifact, not in ``run_actions``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from control_plane_agent.workspace import _LOCAL_PATH_RE

TRANSCRIPT_SCHEMA = "agent-transcript/1"
TRANSCRIPT_ARTIFACT_TYPE = "transcript"
MAX_TRANSCRIPT_BYTES = 512 * 1024
MAX_TEXT_CHARS = 20_000
MAX_TOOL_INPUT_CHARS = 6_000
MAX_TOOL_RESULT_CHARS = 6_000
MAX_FINAL_CHARS = 60_000
# What a run action may say about a tool call: enough to read the audit trail
# without opening the transcript, not enough to be a second copy of it.
MAX_ACTION_SUMMARY_CHARS = 160
MAX_ACTION_NAME_CHARS = 200
# U+0000 is stored nowhere and answered with 422 by the API (CP-ADR-0083): it
# becomes the replacement character, as an undecodable byte would.
NUL_REPLACEMENT = "\ufffd"
# Tool names from the CLI are trusted to be identifiers, not free text.
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_.:/-]+")

# Values that look like credentials are replaced wherever they appear in free
# text. The prefixes mirror ``workspace._TOKEN_PREFIXES``; the key=value form
# catches `.env`-style lines a tool result may echo; the JWT form catches a
# bearer token pasted into a header.
_CREDENTIAL_RES = (
    re.compile(r"\b(?:cp_|sk-|ghp_|github_pat_|xox[abpors]-)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|"
        r"password|passwd|authorization|client[_-]?secret|token|secret)\s*[=:]\s*)"
        r"(?:\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;\"']{4,})"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def replace_nul(text: str) -> str:
    """``text`` with every U+0000 replaced by ``NUL_REPLACEMENT``."""
    return text.replace("\x00", NUL_REPLACEMENT)


def _label(value: Any, limit: int) -> str:
    """An identifier-like field (tool, call id, model): text, no NUL, bounded."""
    return replace_nul(str(value))[:limit]


def redact_credentials(text: str) -> str:
    """Blank out anything that looks like a secret, keeping the surrounding text."""
    for pattern in _CREDENTIAL_RES[:2]:
        text = pattern.sub("<redacted>", text)
    text = _CREDENTIAL_RES[2].sub(lambda m: f"{m.group(1)}<redacted>", text)
    return _CREDENTIAL_RES[3].sub("<redacted private key>", text)


# A host path is redacted, but the thing the agent touched inside the working
# copy is what a reader wants to know: `<path>/README.md` says which file,
# `<path>` alone says nothing. The tail is kept only for paths deep enough that
# it cannot be the user's or the workspace's own name (root + owner + one dir).
_KEEP_TAIL_MIN_SEGMENTS = 4


def _redact_path_keep_name(match: re.Match[str]) -> str:
    path = match.group(0)
    segments = [s for s in re.split(r"[\\/]", path) if s]
    if len(segments) >= _KEEP_TAIL_MIN_SEGMENTS and not path.endswith(("/", "\\")):
        return f"<path>/{segments[-1]}"
    return "<path>"


def redact_paths_keep_name(text: str) -> str:
    """``redact_local_paths`` that keeps the last segment of a deep path."""
    return _LOCAL_PATH_RE.sub(_redact_path_keep_name, text)


def sanitize_text(text: str, limit: int) -> tuple[str, bool]:
    """Replace NUL, redact paths and credentials, then cut to ``limit`` characters."""
    clean = redact_credentials(redact_paths_keep_name(replace_nul(text)))
    if len(clean) <= limit:
        return clean, False
    return clean[:limit] + f"… [truncated {len(clean) - limit} chars]", True


def sanitize_value(value: Any, limit: int) -> tuple[Any, bool]:
    """Sanitize a JSON-ish value: strings in place, everything else as pretty JSON."""
    if isinstance(value, str):
        return sanitize_text(value, limit)
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)
    except (TypeError, ValueError):
        rendered = repr(value)
    return sanitize_text(rendered, limit)


def tool_action_name(tool: str) -> str:
    """``tool.<name>`` for ``run_actions.action``: identifier characters only."""
    cleaned = _TOOL_NAME_RE.sub("_", tool.strip()) or "unknown"
    return f"tool.{cleaned}"[:MAX_ACTION_NAME_CHARS]


def action_summary(value: Any) -> str:
    """One line about a tool input for the audit trail — short and redacted."""
    if isinstance(value, Mapping):
        # The most telling field first: a command, a path, a query, a pattern.
        keys = ("command", "cmd", "file_path", "path", "pattern", "query", "url", "prompt", "skill")
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                text, _ = sanitize_text(candidate.strip().splitlines()[0], MAX_ACTION_SUMMARY_CHARS)
                return text
    text, _ = sanitize_value(value, MAX_ACTION_SUMMARY_CHARS)
    return " ".join(text.split())[:MAX_ACTION_SUMMARY_CHARS]


@dataclass(frozen=True)
class TraceSettings:
    """What the runner is allowed to publish about a run.

    ``transcript`` — publish the transcript artifact; ``actions`` — record one
    run action per tool call; ``tool_results`` — keep tool outputs in the
    transcript (off: only their size and error flag). All default on; a
    deployment that must not let tool output leave the host turns the last
    one off and still gets the conversation and the audit trail.
    """

    transcript: bool = True
    actions: bool = True
    tool_results: bool = True

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> TraceSettings:
        values = os.environ if environ is None else environ

        def flag(name: str) -> bool:
            return values.get(name, "1").strip().lower() not in ("0", "false", "no", "off")

        return cls(
            transcript=flag("CONTROL_PLANE_TRACE_TRANSCRIPT"),
            actions=flag("CONTROL_PLANE_TRACE_ACTIONS"),
            tool_results=flag("CONTROL_PLANE_TRACE_TOOL_RESULTS"),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class TranscriptBuilder:
    """Accumulates a bounded ``agent-transcript/1`` document.

    Entries are appended in stream order. Once the byte budget is spent, new
    entries are counted as dropped instead of stored — except the final
    answer, which always has its own slot. Nothing here talks to the server.
    """

    harness_type: str
    session_id: str = ""
    keep_tool_results: bool = True
    max_bytes: int = MAX_TRANSCRIPT_BYTES
    entries: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    tools: list[str] = field(default_factory=list)
    final: str = ""
    final_truncated: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "assistantMessages": 0,
            "userMessages": 0,
            "toolCalls": 0,
            "toolErrors": 0,
            "hiddenReasoningBlocks": 0,
            "truncatedEntries": 0,
            "droppedEntries": 0,
        }
    )
    _bytes: int = 0
    _seq: int = 0
    _call_seq: dict[str, int] = field(default_factory=dict)

    # -- what the adapters feed in --------------------------------------------

    def system(self, *, model: str = "", tools: list[str] | None = None) -> None:
        if model:
            self.model = _label(model, MAX_ACTION_NAME_CHARS)
        if tools:
            self.tools = [_label(t, MAX_ACTION_NAME_CHARS) for t in tools[:200]]

    def assistant_text(self, text: str, *, at: str | None = None) -> None:
        if not text.strip():
            return
        clean, truncated = sanitize_text(text, MAX_TEXT_CHARS)
        self.stats["assistantMessages"] += 1
        self._append({"kind": "assistant", "text": clean}, at=at, truncated=truncated)

    def user_text(self, text: str, *, at: str | None = None) -> None:
        if not text.strip():
            return
        clean, truncated = sanitize_text(text, MAX_TEXT_CHARS)
        self.stats["userMessages"] += 1
        self._append({"kind": "user", "text": clean}, at=at, truncated=truncated)

    def hidden_reasoning(self) -> None:
        """A thinking block or reasoning summary: counted, never stored."""
        self.stats["hiddenReasoningBlocks"] += 1

    def tool_call(self, call_id: str, name: str, input: Any, *, at: str | None = None) -> int:
        """Record a tool invocation; returns its ordinal within the run."""
        self.stats["toolCalls"] += 1
        ordinal = self.stats["toolCalls"]
        self._call_seq[call_id] = ordinal
        rendered, truncated = sanitize_value(input, MAX_TOOL_INPUT_CHARS)
        self._append(
            {
                "kind": "tool_call",
                "call": ordinal,
                "callId": _label(call_id, 120),
                "tool": _label(name, MAX_ACTION_NAME_CHARS),
                "input": rendered,
            },
            at=at,
            truncated=truncated,
        )
        return ordinal

    def tool_result(
        self, call_id: str, output: Any, *, is_error: bool = False, at: str | None = None
    ) -> int | None:
        """Record what a tool returned; returns the ordinal of its call, if known."""
        ordinal = self._call_seq.get(call_id)
        if is_error:
            self.stats["toolErrors"] += 1
        entry: dict[str, Any] = {
            "kind": "tool_result",
            "call": ordinal,
            "callId": _label(call_id, 120),
            "isError": bool(is_error),
        }
        truncated = False
        if self.keep_tool_results:
            entry["output"], truncated = sanitize_value(output, MAX_TOOL_RESULT_CHARS)
        else:
            entry["withheld"] = True
            entry["outputChars"] = (
                len(output)
                if isinstance(output, str)
                else len(json.dumps(output, ensure_ascii=False, default=str))
            )
        self._append(entry, at=at, truncated=truncated)
        return ordinal

    def final_answer(self, text: str) -> None:
        self.final, self.final_truncated = sanitize_text(text, MAX_FINAL_CHARS)

    def record_usage(self, **fields: Any) -> None:
        for key, value in fields.items():
            if value is not None:
                self.usage[key] = value

    # -- output ---------------------------------------------------------------

    @property
    def truncated(self) -> bool:
        return bool(
            self.stats["truncatedEntries"] or self.stats["droppedEntries"] or self.final_truncated
        )

    def content(self) -> dict[str, Any]:
        return {
            "schema": TRANSCRIPT_SCHEMA,
            "harnessType": self.harness_type,
            "sessionId": replace_nul(self.session_id),
            "model": self.model,
            "tools": list(self.tools),
            "entries": list(self.entries),
            "final": {"text": self.final, "truncated": self.final_truncated},
            "usage": dict(self.usage),
            "stats": dict(self.stats),
            "truncated": self.truncated,
        }

    def metadata(self) -> dict[str, Any]:
        """The numbers a reader wants before opening the document."""
        meta: dict[str, Any] = {
            "schema": TRANSCRIPT_SCHEMA,
            "harnessType": self.harness_type,
            "entries": len(self.entries),
            "toolCalls": self.stats["toolCalls"],
            "toolErrors": self.stats["toolErrors"],
            "assistantMessages": self.stats["assistantMessages"],
            "truncated": self.truncated,
        }
        if self.model:
            meta["model"] = self.model
        if self.session_id:
            meta["sessionId"] = replace_nul(self.session_id)
        for key in ("inputTokens", "outputTokens", "costUsd"):
            if key in self.usage:
                meta[key] = self.usage[key]
        return meta

    # -- internals ------------------------------------------------------------

    def _append(self, entry: dict[str, Any], *, at: str | None, truncated: bool) -> None:
        self._seq += 1
        entry = {"seq": self._seq, "at": at or _now(), **entry}
        if truncated:
            entry["truncated"] = True
            self.stats["truncatedEntries"] += 1
        size = len(json.dumps(entry, ensure_ascii=False))
        if self._bytes + size > self.max_bytes:
            self.stats["droppedEntries"] += 1
            return
        self._bytes += size
        self.entries.append(entry)


# -- server side of the trace ---------------------------------------------------


class TraceRecorder:
    """Feeds a ``TranscriptBuilder`` and mirrors tool calls into run actions.

    Everything here is auxiliary to the run: a failed action write is logged
    and swallowed, never raised, because bookkeeping must not turn finished
    work into a failed run (same rule as checkpoints). The action budget of
    the run (``max_actions``) does apply — a run that hits it keeps working,
    it just stops being narrated in ``run_actions``; the transcript artifact
    still carries the whole story.
    """

    def __init__(
        self,
        client: Any,
        run_id: str,
        *,
        builder: TranscriptBuilder,
        settings: TraceSettings,
        reference_prefix: str,
        logger: Any = None,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.builder = builder
        self.settings = settings
        self.reference_prefix = reference_prefix
        self.logger = logger
        self._actions: dict[str, str] = {}
        self._budget_exhausted = False

    async def tool_started(
        self, call_id: str, name: str, input: Any, *, at: str | None = None
    ) -> None:
        ordinal = self.builder.tool_call(call_id, name, input, at=at)
        if not self.settings.actions or self._budget_exhausted:
            return
        try:
            action = await self.client.record_action(
                self.run_id,
                action=tool_action_name(name),
                status="started",
                external_reference=replace_nul(f"{self.reference_prefix}#call/{ordinal}"),
                metadata={
                    "tool": _label(name, MAX_ACTION_NAME_CHARS),
                    "call": ordinal,
                    "summary": action_summary(input),
                },
            )
        except Exception as exc:
            self._note_failure("record_action", exc)
            return
        action_id = action.get("id") if isinstance(action, Mapping) else None
        if action_id:
            self._actions[str(call_id)] = str(action_id)

    async def tool_finished(
        self, call_id: str, output: Any, *, is_error: bool = False, at: str | None = None
    ) -> None:
        self.builder.tool_result(call_id, output, is_error=is_error, at=at)
        action_id = self._actions.pop(str(call_id), None)
        if action_id is None:
            return
        try:
            await self.client.finish_action(
                self.run_id, action_id, status="failed" if is_error else "completed"
            )
        except Exception as exc:
            self._note_failure("finish_action", exc)

    async def close(self, *, failed: bool = False) -> None:
        """Finish any action whose result never arrived (crash, timeout)."""
        for call_id, action_id in list(self._actions.items()):
            self._actions.pop(call_id, None)
            try:
                await self.client.finish_action(self.run_id, action_id, status="failed")
            except Exception as exc:
                self._note_failure("finish_action", exc)

    def artifact(self, *, name: str, extra_metadata: Mapping[str, Any] | None = None) -> Any:
        """The transcript as an ``ArtifactSpec``, or None when publishing is off.

        The document is checked against the portability guard here, where a
        failure can be handled — the daemon runs the same guard and would fail
        the whole run on a miss. A transcript that still carries something the
        guard rejects is withheld: its counters are published, its text is not.
        """
        if not self.settings.transcript:
            return None
        from control_plane_agent.main import ArtifactSpec
        from control_plane_agent.workspace import UnsafePayloadError, assert_portable

        content = self.builder.content()
        metadata = {**self.builder.metadata(), **(extra_metadata or {})}
        try:
            assert_portable({"content": content, "metadata": metadata}, where="transcript")
        except UnsafePayloadError as exc:
            self._note_failure("assert_portable", exc)
            content = {
                "schema": TRANSCRIPT_SCHEMA,
                "harnessType": self.builder.harness_type,
                "withheld": True,
                "reason": "unsafe_payload",
                "stats": dict(self.builder.stats),
                "usage": dict(self.builder.usage),
            }
            metadata["withheld"] = True
        return ArtifactSpec(
            type=TRANSCRIPT_ARTIFACT_TYPE, name=name, content=content, metadata=metadata
        )

    def _note_failure(self, what: str, exc: BaseException) -> None:
        code = getattr(exc, "code", "")
        if code == "budget_exceeded":
            self._budget_exhausted = True
        if self.logger is not None:
            self.logger.info("trace %s failed: %s", what, exc)


def text_of_blocks(content: Any) -> str:
    """Flatten a content-block list (Anthropic message shape) to text.

    Non-text blocks are named, not dropped: a reader of the transcript should
    know an image or a tool reference was there even if its bytes are not.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, default=str) if content is not None else ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif kind == "tool_reference":
                parts.append(f"[tool: {block.get('tool_name', '')}]")
            elif kind:
                parts.append(f"[{kind}]")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)
