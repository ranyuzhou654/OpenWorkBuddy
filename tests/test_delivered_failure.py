"""Local failure injection: preserve delivered bytes without replaying the failed task."""
import asyncio
import dataclasses
import json

import httpx
import openai
import pytest
from conftest import FakeModel, call, reply
from test_api import Server
from test_delivery_recovery import child
from test_schedule import finish

from nano_workbuddy import artifacts, usage
from nano_workbuddy.budgets import Limits
from nano_workbuddy.config import load
from nano_workbuddy.runtime import Workspace


def timeout():
    return openai.APITimeoutError(request=httpx.Request("POST", "https://example.test/model"))


def failed_model():
    return FakeModel(reply(None,
        call("create_document", path="report.md", content="# Report\nExisting body"),
        call("export_document", source="report.md", path="report.docx")), timeout())


async def test_failure_notice_survives_reopen_and_failed_requests_never_auto_resume(home, project):
    model = failed_model()
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    with pytest.raises(openai.APITimeoutError):
        await ws.submit(log, "Create both formats", request_id="stable")
    notice = log.path()[-1]
    assert notice.type == "notice" and notice.data["kind"] == "delivery_recovery"
    assert notice.data["status"] == "error" and notice.data["error_type"] == "APITimeoutError"
    assert len(notice.data["deliveries"]) == 2 and len(model.requests) == 2
    assert ws.index._one("select status from runs where id=?", notice.run_id)["status"] == "error"
    assert await ws.submit(log, "Create both formats", request_id="stable") == "error"
    hashes = {p.name: artifacts.sha256_of(p) for p in project.glob("report.*")}
    sid = log.id
    for opened in ws._logs.values():
        opened.close()
    ws.index.db.close()
    fresh = FakeModel()
    reopened = Workspace(load(home), connect=fresh.connect)
    log = reopened.open_session(sid)
    assert log.path()[-1].data == notice.data
    assert await reopened.resume(log) is None
    assert not fresh.requests
    assert hashes == {p.name: artifacts.sha256_of(p) for p in project.glob("report.*")}
    # An explicit new task sees the recovery instruction; it can still request edits separately.
    fresh.script = [reply("Use the existing delivered versions")]
    await reopened.submit(log, "Only confirm the existing files")
    assert "不要重新生成正文" in str(fresh.requests[-1]["messages"])
    assert len(artifacts.run_deliveries(log, notice.run_id)) == 2


@pytest.mark.parametrize("state", ["available", "missing", "corrupt", "unreadable"])
async def test_local_inspection_and_download_do_not_call_models_or_change_failed_run(home, project, state, monkeypatch):
    model = failed_model()
    ws = Workspace(load(home), connect=model.connect)
    async with Server(ws) as server:
        sid = await server.session(project)
        await server.client.post(f"/api/sessions/{sid}/messages", json={"text": "Create files", "request_id": "stable"})
        await finish(server.runs, sid)
        log = ws.open_session(sid)
        rid = log.path()[-1].run_id
        entries = artifacts.run_deliveries(log, rid)
        md = next(e["artifact"] for e in entries if e["artifact"]["name"] == "report.md")
        # User changes do not affect the kept delivered version, nor get overwritten by inspection.
        (project / "report.md").write_text("USER EDIT")
        blob = artifacts.blob(ws.store, md["sha256"])
        if state == "missing":
            blob.unlink()
        elif state == "corrupt":
            blob.write_text("CORRUPTED BY TEST")
        elif state == "unreadable":
            original = artifacts.sha256_of

            def unreadable(path):
                if path == blob:
                    raise PermissionError("synthetic unreadable blob")
                return original(path)

            monkeypatch.setattr(artifacts, "sha256_of", unreadable)
        before = log.file.read_bytes()
        ledger = ws.usage_path(log).read_bytes()
        for _ in range(2):
            r = await server.client.get(f"/api/sessions/{sid}/deliveries/{rid}")
            assert r.status_code == 200 and r.json()["status"] == "error"
            assert next(f for f in r.json()["files"] if f["artifact"]["name"] == "report.md")["availability"] == state
        download = await server.client.get(f"/api/sessions/{sid}/files/{md['sha256']}", params={"name": "report.md"})
        assert download.status_code == (200 if state == "available" else 410)
        if state == "available":
            assert download.text == "# Report\nExisting body"
        assert (project / "report.md").read_text() == "USER EDIT"
        assert log.file.read_bytes() == before and ws.usage_path(log).read_bytes() == ledger
        assert len(model.requests) == 2
        duplicate = await server.client.post(f"/api/sessions/{sid}/messages",
            json={"text": "Create files", "request_id": "stable"})
        assert duplicate.json()["delivery"] == "duplicate" and duplicate.json()["run_status"] == "error"
        other = ws.new_session(project)
        assert (await server.client.get(f"/api/sessions/{other.id}/deliveries/{rid}")).status_code == 404
        log.checkout(next(e.id for e in log.path() if e.type == "user"))
        assert (await server.client.get(f"/api/sessions/{sid}/deliveries/{rid}")).json()["files"] == []
        events = usage.events(ws.usage_path(log))
        assert sum(e["type"] == "model.started" for e in events) == 2
        assert sum(e["type"] == "model.response" for e in events) == 1
        assert json.loads(ws.index._one("select usage_json from runs where id=?", rid)["usage_json"])


async def test_only_current_run_files_are_reported_after_failure(home, project):
    model = FakeModel(reply(None, call("create_document", path="old.md", content="Old body")), reply("done"), timeout())
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "Old task")
    with pytest.raises(openai.APITimeoutError):
        await ws.submit(log, "Different task")
    assert not any(e.type == "notice" and e.data.get("kind") == "delivery_recovery" for e in log.path())


async def test_process_exit_between_failure_notice_and_index_update_does_not_replay(home, project):
    ws = Workspace(load(home), connect=FakeModel().connect)
    sid = ws.new_session(project).id
    ws.index.db.close()
    child('''
        import asyncio, os, sys
        from pathlib import Path
        sys.path.insert(0, str(Path.cwd() / 'tests'))
        from conftest import FakeModel, call, reply
        from nano_workbuddy import skills
        from nano_workbuddy.config import load
        from nano_workbuddy.runtime import Workspace
        home, sid = Path(sys.argv[1]), sys.argv[2]
        skills.SHARED = home / 'no-shared-skills'
        model = FakeModel(reply(None, call('create_document', path='kept.md', content='KEPT')), TimeoutError())
        ws = Workspace(load(home), connect=model.connect)
        ws.index.finish_run = lambda *args, **kwargs: os._exit(23)
        asyncio.run(ws.submit(ws.open_session(sid), 'Create and confirm'))
    ''', home, sid)
    fresh = FakeModel()
    ws = Workspace(load(home), connect=fresh.connect)
    log = ws.open_session(sid)
    assert ws.index.unfinished_runs(sid) and log.path()[-1].data["kind"] == "delivery_recovery"
    before = log.file.read_bytes()
    assert await ws.resume(log) == "error"
    assert log.file.read_bytes() == before and not fresh.requests
    assert not ws.index.unfinished_runs(sid)
    assert (project / "kept.md").read_text() == "KEPT"
    assert json.loads(ws.index._one("select usage_json from runs where id=?", log.path()[-1].run_id)["usage_json"])


@pytest.mark.parametrize("outcome", ["limit", "cancelled"])
async def test_budget_and_user_cancellation_keep_deliveries_without_claiming_success(home, project, outcome):
    model = FakeModel(reply(None, call("create_document", path="report.md", content="BODY")), asyncio.CancelledError())
    cfg = dataclasses.replace(load(home), budget=Limits(model_calls=1 if outcome == "limit" else 60))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await ws.submit(log, "Create a file")
    else:
        assert await ws.submit(log, "Create a file") == "limit"
    assert log.path()[-1].data["kind"] == "delivery_recovery"
    assert log.path()[-1].data["status"] == outcome
    assert (project / "report.md").read_text() == "BODY"
    assert ws.index._one("select status from runs where id=?", log.path()[-1].run_id)["status"] == outcome
