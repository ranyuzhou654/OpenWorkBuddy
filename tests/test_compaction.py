"""Context control tiers 2 and 3, driven end to end through Workspace with a small context window.

The default test model has max_output 32,000. A 39,000 window leaves 7,000 tokens, enough for
the fixed prompt, scoped-recall tools and one recent user turn before history can compact."""

from pathlib import Path

import pytest
from conftest import FakeModel, call, reply

from nano_workbuddy.compaction import ContextTooLarge
from nano_workbuddy.context import to_messages, visible
from nano_workbuddy.llm import ContextOverflow
from nano_workbuddy.llm.anthropic import to_anthropic
from nano_workbuddy.llm.openai_chat import to_chat
from nano_workbuddy.types import ASSISTANT, COMPACTION, NOTICE, PRUNE, text_of

SMALL = 39_000
LONG = "x" * 7_000  # about 2,000 tokens


def texts(messages):
    return [text_of(m["content"]) for m in messages]


async def test_nothing_happens_while_the_request_fits(make_ws, project):
    ws = make_ws(FakeModel(reply("a"), reply("b")))
    log = ws.new_session(project)
    await ws.submit(log, "hi")
    await ws.submit(log, "again")
    assert not [e for e in log.path() if e.type in (PRUNE, COMPACTION)]


async def test_prune_replaces_old_tool_output_with_a_saved_copy(make_ws, project):
    for n in "abc":
        (project / f"{n}.txt").write_text(f"{n}-start\n" + ("y" * 99 + "\n") * 70)
    model = FakeModel(*(reply(None, call("read", path=f"{n}.txt")) for n in "abc"), reply("done"),
                      window=SMALL + 1_000)  # room for the fixed prompt and tools, so pruning alone is enough
    ws = make_ws(model)
    log = ws.new_session(project)
    assert await ws.submit(log, "read them") == "ok"

    prune = next(e for e in log.path() if e.type == PRUNE)
    last = model.requests[-1]["messages"]
    tool_texts = [text_of(m["content"]) for m in last if m["role"] == "tool"]
    assert tool_texts[0].startswith("[Old tool output removed")
    assert "c-start" in tool_texts[-1]  # the newest result was never pruned
    saved = tool_texts[0].split("saved at ")[1].rstrip(".]")
    assert Path(saved).read_text().split("\t")[1].startswith("a-start")
    assert not [e for e in log.path() if e.type == COMPACTION]  # pruning was enough
    assert prune.data["count"] >= 1


async def test_compaction_summarises_old_turns_and_keeps_recent_ones(make_ws, project):
    model = FakeModel(*(reply(f"answer {i}") for i in range(6)), window=SMALL, summary="SUMMARY-1")
    ws = make_ws(model)
    log = ws.new_session(project)
    for i in range(6):
        await ws.submit(log, f"question {i} {LONG}")

    comps = [e for e in log.path() if e.type == COMPACTION]
    assert comps, "a long conversation in a small window must compact"
    assert "[User]: question 0" in model.summaries[0]  # the transcript was serialised for the summariser
    sent = model.requests[-1]["messages"]
    assert "<conversation-summary>" in texts(sent)[0] and "SUMMARY-1" in texts(sent)[0]
    assert any(t.startswith("question 5") for t in texts(sent))  # the newest turn stays verbatim
    assert not any(t.startswith("question 0") for t in texts(sent))
    assert any(t.startswith("<system-notice>") for t in texts(sent))  # the permissions notice survived
    first_kept = log.entries[comps[-1].data["first_kept_id"]]
    assert first_kept.type in ("user", "assistant")
    assert len(comps) >= 2 and "<previous-summary>" in model.summaries[1]  # repeated compaction builds on the last
    assert len(log.path()) > len(visible(log.path()))  # raw history is kept, only the projection shrinks


async def test_failed_summary_appends_nothing(make_ws, project):
    model = FakeModel(*(reply(f"a{i}") for i in range(6)), window=SMALL, summary=RuntimeError("summariser down"))
    ws = make_ws(model)
    log = ws.new_session(project)
    with pytest.raises(RuntimeError, match="summariser down"):
        for i in range(6):
            await ws.submit(log, f"q{i} {LONG}")
    assert not [e for e in log.path() if e.type == COMPACTION]


async def test_provider_overflow_compacts_and_retries_once(make_ws, project):
    model = FakeModel(reply("a0"), reply("a1"), reply("a2"), ContextOverflow("too long"), reply("after"))
    ws = make_ws(model)
    log = ws.new_session(project)
    for i in range(3):
        await ws.submit(log, f"q{i} {LONG}")
    events = []
    assert await ws.submit(log, "q3", emit=events.append) == "ok"
    assert {"type": "context.overflow"} in events
    assert [e for e in log.path() if e.type == COMPACTION]
    assert text_of(log.path()[-1].data["content"]) == "after"


async def test_window_too_small_even_after_compaction_is_an_error(make_ws, project):
    ws = make_ws(FakeModel(reply("never"), window=32_200))  # budget of 200 tokens: the system prompt alone exceeds it
    log = ws.new_session(project)
    with pytest.raises(ContextTooLarge):
        await ws.submit(log, "hi")


async def test_manual_compaction(make_ws, project):
    model = FakeModel(*(reply(f"a{i} {LONG}") for i in range(4)), summary="MANUAL")
    ws = make_ws(model)
    log = ws.new_session(project)
    assert await ws.compact(log) == "nothing"  # empty conversation
    for i in range(4):
        await ws.submit(log, f"q{i} {LONG}")
    assert await ws.compact(log, "keep the file names") == "ok"
    assert "Focus: keep the file names" in model.summaries[-1]
    assert log.path()[-1].type == COMPACTION


def test_thinking_from_before_an_edit_is_not_replayed_to_anthropic(tmp_path):
    from nano_workbuddy.log import SessionLog

    log = SessionLog.create(tmp_path / "s.jsonl", "s", "p")
    log.append("system_prompt", {"text": "sys"})
    u = log.append("user", {"content": [{"type": "text", "text": "q"}]})
    thinking = {"type": "thinking", "text": "hmm", "provider": "claude", "meta": {"signature": "sig"}}
    log.append(ASSISTANT, {"content": [thinking, {"type": "text", "text": "a"}], "stop": "end"})
    _, before = to_messages(log.path())
    assert to_anthropic(before, "claude")[1]["content"][0]["type"] == "thinking"

    log.append(COMPACTION, {"summary": "S", "first_kept_id": u.id, "files": {}})
    log.append(NOTICE, {"kind": "permissions", "text": "n"})
    _, after = to_messages(log.path())
    assert [b["type"] for b in to_anthropic(after, "claude")[1]["content"]] == ["text"]  # stripped, text kept
    reasoning = {**thinking, "provider": "ds", "meta": {"field": "reasoning_content"}}
    log.append(ASSISTANT, {"content": [reasoning], "stop": "end"})
    _, latest = to_messages(log.path())
    assert to_chat("sys", latest, "ds")[-1]["reasoning_content"] == "hmm"  # newer than the edit: replayed
