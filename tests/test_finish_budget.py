"""Finishing preserves the original total limit and permits existing-document delivery."""
import dataclasses
import time

from conftest import FakeModel, call, reply

from nano_workbuddy.budgets import Limits, RunBudget
from nano_workbuddy.config import load
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import text_of


def test_finishing_uses_remaining_calls_and_time_without_reset(tmp_path, monkeypatch):
    now = time.time()
    b = RunBudget(tmp_path / "calls.jsonl", "run", Limits(model_calls=12, wall_seconds=180), now, {})
    assert not b.finishing()
    for _ in range(9):
        b.model()
    assert b.finishing() and b.remaining()["model_calls"] == 3 and b.limits.model_calls == 12
    other = RunBudget(tmp_path / "time.jsonl", "run", Limits(model_calls=12, wall_seconds=180), now, {})
    monkeypatch.setattr(time, "time", lambda: now + 140)
    assert other.finishing() and other.remaining()["model_calls"] == 12
    assert other.deadline == now + 180


async def test_finishing_blocks_new_fetch_but_allows_saved_document_export(home, project, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: (_ for _ in ()).throw(AssertionError("no HTTP")))
    (project / "facts.txt").write_text("Existing facts")
    model = FakeModel(reply(None, call("read", path="facts.txt")), reply(None, call("read", path="facts.txt")),
        reply(None, call("web_fetch", url="https://example.test/new"),
              call("create_document", path="report.md", content="# Existing facts\nUnknown new facts"),
              call("export_document", source="report.md", path="report.docx")), reply("Delivered"))
    ws = Workspace(dataclasses.replace(load(home), budget=Limits(model_calls=4)), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "Finish with existing evidence")
    assert len(model.requests) == 4 and (project / "report.docx").is_file()
    blocked = next(e for e in log.path() if e.type == "tool_result" and e.data.get("name") == "web_fetch")
    assert blocked.data["is_error"] and "收尾" in text_of(blocked.data["content"])
    assert "预算收尾" in str(model.requests[-1]["messages"])


async def test_unknown_tool_lists_declared_alternatives_without_running_it(home, project):
    model = FakeModel(reply(None, call("not_a_tool", command="anything")), reply("done"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "Try an unavailable tool")
    assert not any(e.type == "tool_started" for e in log.path())
    result = next(e for e in log.path() if e.type == "tool_result")
    assert "Available tools:" in text_of(result.data["content"])
    assert "export_document" in text_of(result.data["content"])
