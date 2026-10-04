"""Execution trace (ADR-0051): bounded transcript artifact and tool-call actions.

What must hold: paths and credentials never reach the document, hidden
reasoning is counted but not stored, the byte budget drops entries rather than
growing, tool calls become run actions with references only, and a document
the portability guard still rejects is withheld instead of failing the run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from control_plane_agent.trace import (
    TRANSCRIPT_ARTIFACT_TYPE,
    TRANSCRIPT_SCHEMA,
    TraceRecorder,
    TraceSettings,
    TranscriptBuilder,
    action_summary,
    redact_credentials,
    replace_nul,
    sanitize_text,
    sanitize_value,
    text_of_blocks,
    tool_action_name,
)
from control_plane_agent.workspace import assert_portable
from control_plane_client import ControlPlaneError


class FakeClient:
    def __init__(self, *, fail_with: str | None = None) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.finished: list[tuple[str, str]] = []
        self.fail_with = fail_with

    async def record_action(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.fail_with:
            raise ControlPlaneError(self.fail_with, "nope")
        self.recorded.append(kwargs)
        return {"id": f"action-{len(self.recorded)}"}

    async def finish_action(self, run_id: str, action_id: str, **kwargs: Any) -> dict[str, Any]:
        self.finished.append((action_id, kwargs["status"]))
        return {"id": action_id}


def recorder_for(client: FakeClient, **settings: Any) -> TraceRecorder:
    builder = TranscriptBuilder("claude-code", session_id="s1")
    return TraceRecorder(
        client,
        "run-1",
        builder=builder,
        settings=TraceSettings(**settings),
        reference_prefix="claude-code:session/s1",
    )


def test_credentials_and_paths_are_redacted_in_every_string() -> None:
    builder = TranscriptBuilder("claude-code")
    builder.assistant_text("Read /Users/alice/project/.env: API_KEY=sk-abcdef0123456789 done")
    builder.tool_call(
        "c1",
        "Bash",
        {"command": "curl -H 'Authorization: Bearer eyJabcdefghijk.eyJabcdefghijk.sigsigsigsig' x"},
    )
    key_block = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    builder.tool_result("c1", f"token: ghp_0123456789abcdef0123\n{key_block}")

    text = json.dumps(builder.content(), ensure_ascii=False)
    assert "/Users/" not in text
    assert "sk-abcdef" not in text
    assert "eyJabcdefghijk" not in text
    assert "ghp_0123" not in text
    assert "BEGIN RSA" not in text
    assert "<path>" in text and "<redacted>" in text
    # The guard the daemon applies is satisfied by the redacted document.
    assert_portable({"content": builder.content(), "metadata": builder.metadata()})


def test_deep_paths_keep_their_file_name_shallow_ones_do_not() -> None:
    builder = TranscriptBuilder("claude-code")
    builder.tool_call(
        "c1", "Read", {"file_path": "/opt/runner/worktrees/TASK-1/control-plane/README.md"}
    )
    builder.tool_call(
        "c2", "Bash", {"command": "ls /Users/alice /home/bob/project C:\\Users\\x\\y\\z.txt"}
    )
    entries = builder.content()["entries"]
    assert "<path>/README.md" in entries[0]["input"]
    assert entries[1]["input"].count("<path>") == 3
    assert "alice" not in entries[1]["input"] and "project" not in entries[1]["input"]
    assert "<path>/z.txt" in entries[1]["input"]
    assert (
        action_summary({"file_path": "/opt/runner/worktrees/TASK-1/control-plane/README.md"})
        == "<path>/README.md"
    )
    assert_portable({"content": builder.content()})


def test_redact_credentials_keeps_ordinary_text() -> None:
    assert redact_credentials("tokens per second: 42, see https://example.com/a/b") == (
        "tokens per second: 42, see https://example.com/a/b"
    )
    assert redact_credentials("password = hunter22") == "password = <redacted>"


def test_hidden_reasoning_is_counted_not_stored() -> None:
    builder = TranscriptBuilder("claude-code")
    builder.hidden_reasoning()
    builder.assistant_text("visible")
    content = builder.content()
    assert content["stats"]["hiddenReasoningBlocks"] == 1
    assert [e["kind"] for e in content["entries"]] == ["assistant"]


def test_byte_budget_drops_entries_but_keeps_the_final_answer() -> None:
    builder = TranscriptBuilder("codex", max_bytes=2_000)
    for i in range(50):
        builder.assistant_text(f"message {i} " + "x" * 200)
    builder.final_answer("the end")
    content = builder.content()
    assert content["stats"]["droppedEntries"] > 0
    assert len(json.dumps(content["entries"])) <= 2_000 + 100
    assert content["final"]["text"] == "the end"
    assert content["truncated"] is True
    assert builder.metadata()["truncated"] is True


def test_long_tool_output_is_cut_and_marked() -> None:
    builder = TranscriptBuilder("claude-code")
    builder.tool_call("c1", "Read", {"file_path": "README.md"})
    builder.tool_result("c1", "y" * 50_000)
    entry = builder.content()["entries"][-1]
    assert entry["truncated"] is True
    assert "[truncated" in entry["output"]
    assert len(entry["output"]) < 7_000


def test_tool_results_can_be_withheld_by_settings() -> None:
    builder = TranscriptBuilder("claude-code", keep_tool_results=False)
    builder.tool_call("c1", "Bash", {"command": "cat secret.txt"})
    builder.tool_result("c1", "top secret content")
    entry = builder.content()["entries"][-1]
    assert entry["withheld"] is True
    assert "output" not in entry
    assert entry["outputChars"] == len("top secret content")


@pytest.mark.asyncio
async def test_tool_calls_become_run_actions_with_references_only() -> None:
    client = FakeClient()
    recorder = recorder_for(client)

    await recorder.tool_started("c1", "Bash", {"command": "pytest -q tests/unit", "timeout": 120})
    await recorder.tool_finished("c1", "32 passed")
    await recorder.tool_started(
        "c2", "mcp__control-plane__cp_checkpoint", {"kind": "x", "data": {}}
    )
    await recorder.tool_finished("c2", "error: nope", is_error=True)

    assert [a["action"] for a in client.recorded] == [
        "tool.Bash",
        "tool.mcp__control-plane__cp_checkpoint",
    ]
    first = client.recorded[0]
    assert first["status"] == "started"
    assert first["external_reference"] == "claude-code:session/s1#call/1"
    assert first["metadata"] == {"tool": "Bash", "call": 1, "summary": "pytest -q tests/unit"}
    # No payload travels in the action: the output is in the transcript only.
    assert "32 passed" not in json.dumps(client.recorded)
    assert client.finished == [("action-1", "completed"), ("action-2", "failed")]
    entries = recorder.builder.content()["entries"]
    assert [e["kind"] for e in entries] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert entries[1]["call"] == 1 and entries[3]["isError"] is True


@pytest.mark.asyncio
async def test_action_budget_exhaustion_stops_narration_not_the_run() -> None:
    client = FakeClient(fail_with="budget_exceeded")
    recorder = recorder_for(client)

    await recorder.tool_started("c1", "Bash", {"command": "ls"})
    await recorder.tool_finished("c1", "ok")
    client.fail_with = None
    await recorder.tool_started("c2", "Bash", {"command": "ls"})

    assert client.recorded == []  # narration stopped after the budget error
    assert recorder.builder.stats["toolCalls"] == 2  # the transcript still has both


@pytest.mark.asyncio
async def test_unfinished_calls_are_failed_on_close() -> None:
    client = FakeClient()
    recorder = recorder_for(client)
    await recorder.tool_started("c1", "Bash", {"command": "sleep 999"})
    await recorder.close(failed=True)
    assert client.finished == [("action-1", "failed")]


@pytest.mark.asyncio
async def test_actions_can_be_switched_off() -> None:
    client = FakeClient()
    recorder = recorder_for(client, actions=False)
    await recorder.tool_started("c1", "Bash", {"command": "ls"})
    await recorder.tool_finished("c1", "ok")
    assert client.recorded == [] and client.finished == []
    assert recorder.builder.stats["toolCalls"] == 1


def test_transcript_artifact_shape_and_opt_out() -> None:
    recorder = recorder_for(FakeClient())
    recorder.builder.assistant_text("hello")
    recorder.builder.final_answer("done")
    spec = recorder.artifact(name="claude-code transcript for TASK-1", extra_metadata={"turns": 2})
    assert spec is not None
    assert spec.type == TRANSCRIPT_ARTIFACT_TYPE
    assert spec.content["schema"] == TRANSCRIPT_SCHEMA
    assert spec.content["final"]["text"] == "done"
    assert spec.metadata["turns"] == 2 and spec.metadata["entries"] == 1
    assert recorder_for(FakeClient(), transcript=False).artifact(name="x") is None


def test_document_the_guard_still_rejects_is_withheld_not_raised() -> None:
    recorder = recorder_for(FakeClient())
    # A bare absolute path that the redactor does not know as a host root, but
    # the guard rejects as a whole-string path.
    recorder.builder.assistant_text("/srv/data/report.txt")
    spec = recorder.artifact(name="t")
    assert spec is not None
    assert spec.content["withheld"] is True and spec.content["reason"] == "unsafe_payload"
    assert spec.metadata["withheld"] is True
    assert "entries" not in spec.content
    assert_portable({"content": spec.content, "metadata": spec.metadata})


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        ("", ""),
        ("plain", "plain"),
        ("\x00", "\ufffd"),
        ("a\x00b\x00\x00", "a\ufffdb\ufffd\ufffd"),
        ("\\u0000", "\\u0000"),  # an escaped backslash is text, not NUL
    ],
)
def test_replace_nul(text: str, clean: str) -> None:
    assert replace_nul(text) == clean
    assert replace_nul(replace_nul(text)) == clean  # idempotent


def test_sanitize_replaces_nul_before_redaction_and_limit() -> None:
    # A NUL inside a secret does not hide it from the redactor.
    text, truncated = sanitize_text("token=abc\x00defgh tail", 1000)
    assert text == "token=<redacted> tail" and not truncated
    text, truncated = sanitize_text("\x00" * 10, 4)
    assert text.startswith("\ufffd" * 4) and truncated
    rendered, _ = sanitize_value({"out": "bin\x00ary"}, 1000)
    assert "\x00" not in rendered


@pytest.mark.asyncio
async def test_binary_tool_output_does_not_cost_the_transcript() -> None:
    client = FakeClient()
    recorder = recorder_for(client)
    recorder.builder.session_id = "s\x001"
    recorder.builder.system(model="m\x00", tools=["Bash\x00"])
    recorder.builder.user_text("go\x00")
    recorder.builder.assistant_text("reading\x00 a file")
    await recorder.tool_started("c\x001", "Read\x00", {"file_path": "a.bin\x00", "n": "\x00"})
    await recorder.tool_finished("c\x001", "\x7fELF\x02\x01\x01\x00\x00\x00")
    await recorder.tool_started("c2", "Bash", "raw\x00input")
    await recorder.tool_finished("c2", [{"type": "text", "text": "\x00"}], is_error=True)
    recorder.builder.final_answer("done\x00")

    spec = recorder.artifact(name="t")
    assert spec is not None
    assert "withheld" not in spec.content
    assert not _has_nul(spec.content) and not _has_nul(spec.metadata)
    assert not _has_nul(client.recorded)
    entries = spec.content["entries"]
    assert entries[3]["output"] == "\x7fELF\x02\x01\x01\ufffd\ufffd\ufffd"
    assert entries[2]["tool"] == "Read\ufffd" and entries[3]["call"] == 1
    assert spec.content["final"]["text"] == "done\ufffd"
    assert client.recorded[0]["metadata"]["summary"] == "a.bin\ufffd"


def test_helpers() -> None:
    assert (
        tool_action_name("mcp__control-plane__cp_context") == "tool.mcp__control-plane__cp_context"
    )
    assert tool_action_name("weird name!") == "tool.weird_name_"
    assert action_summary({"file_path": "/Users/me/x.py", "limit": 5}) == "<path>"
    assert action_summary({"file_path": "/Users/me/proj/x.py"}) == "<path>/x.py"
    assert action_summary({"query": "select:a,b"}) == "select:a,b"
    assert (
        action_summary("plain " * 100).endswith("plain") is False
        or len(action_summary("plain " * 100)) <= 160
    )
    assert (
        text_of_blocks(
            [
                {"type": "text", "text": "a"},
                {"type": "image"},
                {"type": "tool_reference", "tool_name": "cp_x"},
            ]
        )
        == "a\n[image]\n[tool: cp_x]"
    )
    assert text_of_blocks("s") == "s"
