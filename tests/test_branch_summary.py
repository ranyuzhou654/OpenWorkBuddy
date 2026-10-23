"""Switching branches with a summary of the branch left behind (Pi's branch summary)."""

import asyncio

from conftest import FakeModel, call, reply
from test_api import Server

from nano_workbuddy.config import load
from nano_workbuddy.loop import Done, next_step
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import ASSISTANT, BRANCH_SUMMARY, USER, text_of


async def two_branches(ws, project):
    """q1 -> a1 (branch A), then q1 edited into q2 -> writes notes.md -> a2 (branch B, current)."""
    log = ws.new_session(project)
    await ws.submit(log, "q1 try the simple fix")
    first = next(e for e in log.path() if e.type == USER)
    leaf_a = log.leaf_id
    await ws.submit(log, "q2 try the rewrite", parent_id=first.parent_id)
    return log, leaf_a


async def test_summary_is_attached_where_you_land(make_ws, project):
    model = FakeModel(reply("a1"), reply(None, call("write", path="notes.md", content="x")), reply("a2"),
                      reply("continuing"), summary="- tried a rewrite\n- wrote notes.md")
    ws = make_ws(model)
    log, leaf_a = await two_branches(ws, project)
    events = []
    assert await ws.switch_branch(log, leaf_a, events.append) == "ok"

    summary = log.entries[log.leaf_id]
    assert summary.type == BRANCH_SUMMARY and summary.parent_id == leaf_a
    assert summary.data["files"]["modified"] == ["notes.md"]
    assert "[User]: q2 try the rewrite" in model.summaries[0] and "q1" not in model.summaries[0]
    assert "Permission preset" not in model.summaries[0]  # harness notices are not summarised
    assert {"type": "run.started", "run_id": None, "kind": "branch_summary"} in events
    assert isinstance(next_step(log.path()), Done)  # a summary alone does not start the model

    await ws.submit(log, "go on")
    sent = [text_of(m["content"]) for m in model.requests[-1]["messages"]]
    branch = next(t for t in sent if t.startswith("<branch-summary>"))
    assert "tried a rewrite" in branch
    assert "Files modified on that branch (still changed on disk unless reverted): notes.md" in branch
    assert sent.index(branch) < sent.index("go on") and "a1" in sent


async def test_nothing_left_behind_means_a_plain_checkout(make_ws, project):
    model = FakeModel(reply("a1"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "q1")
    leaf = log.leaf_id
    log.checkout(next(e for e in log.path() if e.type == USER).id)  # step back on the same branch
    assert await ws.switch_branch(log, leaf) == "nothing"  # moving forward abandons nothing
    assert log.leaf_id == leaf and not model.summaries


async def test_api_switch_with_summary(home, project):
    model = FakeModel(reply("a1"), reply("a2"), summary="- other approach")
    ws = Workspace(load(home), connect=model.connect)
    log, leaf_a = await two_branches(ws, project)
    async with Server(ws) as s:
        r = await s.client.post(f"/api/sessions/{log.id}/checkout", json={"entry_id": leaf_a, "summarize": True})
        assert r.json() == {"leaf_id": None}  # the summary runs in the background
        for _ in range(100):
            if log.entries[log.leaf_id].type == BRANCH_SUMMARY:
                break
            await asyncio.sleep(0.02)
    assert log.entries[log.leaf_id].type == BRANCH_SUMMARY
    assert log.entries[log.entries[log.leaf_id].parent_id].type == ASSISTANT
