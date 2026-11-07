import asyncio
import dataclasses
import json

import pytest
from conftest import HANG, FakeModel, call, reply

from nano_workbuddy import usage
from nano_workbuddy.compaction import ContextTooLarge
from nano_workbuddy.loop import drive


async def test_usage_survives_later_model_failure_and_resume(make_ws, project):
    model = FakeModel(reply(None, call("read", path="x.txt")), RuntimeError("offline"), reply("recovered"))
    (project / "x.txt").write_text("x")
    ws = make_ws(model)
    log = ws.new_session(project)
    with pytest.raises(RuntimeError, match="offline"):
        await ws.submit(log, "go")
    row = ws.index._one("select * from runs")
    assert json.loads(row["usage_json"]) == {"input": 10, "output": 5}
    ws.index.finish_run(row["id"], "interrupted", json.loads(row["usage_json"]))
    assert await ws.resume(log) == "ok"
    row = ws.index._one("select * from runs")
    assert json.loads(row["usage_json"]) == {"input": 20, "output": 10}
    events = usage.events(ws.usage_path(log))
    assert [e["type"] for e in events if e["type"].startswith("model.")] == [
                                          "model.started", "model.response", "model.started", "model.error",
                                          "model.started", "model.response"]
    assert len({e["call_id"] for e in events if "call_id" in e}) == 3
    assert sum(e["type"] == "budget.frozen" for e in events) == 1
    replies = [e for e in log.path() if e.type == "assistant"]
    assert replies and all(e.data["seconds"] >= 0 for e in replies)  # step timings travel with the reply


async def test_cancelled_call_and_partial_tail_remain_unknown(make_ws, project):
    ws = make_ws(FakeModel(HANG))
    log = ws.new_session(project)
    task = asyncio.create_task(ws.submit(log, "go"))
    await asyncio.sleep(.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    path = ws.usage_path(log)
    events = usage.events(path)
    assert [e["type"] for e in events] == ["budget.frozen", "model.started", "model.error"]
    assert events[-1]["error_type"] == "CancelledError"
    with path.open("ab") as f:
        f.write(b'{"partial":')
    assert usage.events(path) == events
    usage.append(path, {"type": "test.after_restart"})
    assert usage.events(path)[-2:] == [{"type": "ledger.corrupt", "bytes": 12}, {"type": "test.after_restart",
                                     "ts": usage.events(path)[-1]["ts"]}]


async def test_summary_calls_have_their_own_usage_evidence(make_ws, project):
    model = FakeModel(reply("first"), reply("second"), summary="summary")
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "a")
    await ws.submit(log, "b")
    await ws.compact(log)
    events = usage.events(ws.usage_path(log))
    responses = [e for e in events if e["type"] == "model.response"]
    assert [r["purpose"] for r in responses] == ["agent", "agent", "compaction"]
    assert usage.totals(ws.usage_path(log))["input"] == 30


async def test_b0_disables_spill_prune_and_compaction_but_keeps_budget(make_ws, project):
    (project / "large.txt").write_text("a" * 80 + "\n" + ("b" * 80 + "\n") * 600)
    model = FakeModel(reply(None, call("read", path="large.txt")), reply("done"), window=1_000_000)
    ws = make_ws(model)
    log = ws.new_session(project)
    log.append("user", {"content": [{"type": "text", "text": "read"}]})
    run = ws.make_run(log, ws.cfg.default_model, None, lambda e: None)
    run.context_governance = False
    assert await drive(run) == "ok"
    result = next(e for e in log.path() if e.type == "tool_result")
    assert len(result.data["content"][0]["text"]) > 30_000
    assert not list(run.ctx.spill_dir.glob("*.txt"))
    model.spec = dataclasses.replace(model.spec, context_window=model.spec.max_output + 200)
    run.model.spec = model.spec
    log.append("user", {"content": [{"type": "text", "text": "again"}]})
    with pytest.raises(ContextTooLarge, match="B0"):
        await drive(run)
    assert not [e for e in log.path() if e.type in ("prune", "compaction")]


async def test_incomplete_plain_answer_is_not_runtime_success(make_ws, project):
    ws = make_ws(FakeModel(reply("partial", stop="error")))
    assert await ws.submit(ws.new_session(project), "go") == "error"


async def test_legacy_run_retains_known_log_usage_without_fabricating_raw_events(make_ws, project):
    ws = make_ws(FakeModel(reply("recovered")))
    log = ws.new_session(project)
    row, _ = ws.index.start_run(log.id, None, ws.cfg.default_model)
    c = call("read", path="x")
    log.append("user", {"content": [{"type": "text", "text": "go"}]}, run_id=row["id"])
    log.append("assistant", {"content": [c], "stop": "tool_use", "usage": {"input": 7, "output": 2}},
               run_id=row["id"])
    assert await ws.resume(log) == "ok"
    assert json.loads(ws.index._one("select * from runs")["usage_json"]) == {"input": 17, "output": 7}
    assert usage.totals(ws.usage_path(log))["input"] == 10  # no fake legacy billing evidence
    ws.delete_session(log.id)
    assert not ws.usage_path(log).exists()
