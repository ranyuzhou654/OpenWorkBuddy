"""R-B: real process exits, retry delivery, outcome evidence and offline relocation."""

import asyncio
import dataclasses
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply
from test_api import Server
from test_connectors import enable
from test_connectors import lark as lark
from test_schedule import Notes, finish

from nano_workbuddy import artifacts, maintenance, schedule, scheduler
from nano_workbuddy.config import load
from nano_workbuddy.connectors.feishu import _decision, _preview, feishu
from nano_workbuddy.log import SessionLog
from nano_workbuddy.runs import Busy, RunManager
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import ASSISTANT, TOOL_STARTED, USER, text_of


def child(code, *args):
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(code), *map(str, args)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 23, result.stderr


def payload(text="SURVIVE", delivery="auto"):
    return json.dumps({"text": text, "parent": None, "attachments": [], "delivery": delivery},
                      ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize("point", ["before_input", "after_input", "after_commit", "before_user", "after_user",
                                   "after_consumed"])
async def test_input_retry_survives_real_process_exit(home, project, point):
    ws = Workspace(load(home), connect=FakeModel().connect)
    log = ws.new_session(project)
    sid = log.id
    ws.index.db.close()
    child('''
        import os, sys
        from pathlib import Path
        from nano_workbuddy.runtime import Workspace
        from nano_workbuddy.config import load
        from nano_workbuddy.types import USER
        home, sid, point, payload = sys.argv[1:]
        ws = Workspace(load(Path(home)))
        log = ws.open_session(sid)
        original = ws.index.add_input
        def insert(*args, **kwargs):
            if point == 'before_input': os._exit(23)
            iid = original(*args, **kwargs)
            if point == 'after_input': os._exit(23)
            return iid
        ws.index.add_input = insert
        row, _ = ws.index.accept_input(sid, 'run', 'SURVIVE', extra={'request_id':'stable'},
                                      request_id='stable', payload=payload)
        if point == 'after_commit': os._exit(23)
        run, _ = ws.index.start_run(sid, 'stable', ws.model_ref(log), input_id=row['id'])
        if point == 'before_user': os._exit(23)
        log.append(USER, {'content':[{'type':'text','text':'SURVIVE'}], 'input_id':row['id']}, run_id=run['id'])
        if point == 'after_user': os._exit(23)
        ws.index.finish_input(row['id'], 'consumed', run['id'])
        os._exit(23)
    ''', home, sid, point, payload())
    model = FakeModel(reply("recovered"))
    reopened = Workspace(load(home), connect=model.connect)
    runs = RunManager(reopened)
    runs.resume_all()
    await finish(runs, sid)
    ack = runs.send(reopened.open_session(sid), "SURVIVE", request_id="stable")
    await finish(runs, sid)
    repeated = runs.send(reopened.open_session(sid), "SURVIVE", request_id="stable")
    assert repeated["delivery"] == "duplicate" and repeated["input_id"] == ack["input_id"]
    assert repeated["status"] == "consumed" and repeated["run_status"] == "ok"
    users = [e for e in reopened.open_session(sid).entries.values() if e.type == USER and e.data.get("input_id")]
    assert len(users) == 1 and len(model.requests) == 1
    assert reopened.index.pending_inputs(sid) == []


async def test_request_scope_delivery_and_terminal_receipt(home, project):
    gate = asyncio.Event()

    async def hold(_):
        await gate.wait()
        return reply("done")

    ws = Workspace(load(home), connect=FakeModel(hold).connect)
    runs = RunManager(ws)
    first, other = ws.new_session(project), ws.new_session(project)
    ack = runs.send(first, "same", request_id="global")
    for log, kwargs in [(other, {}), (first, {"delivery": "queue"}), (first, {"parent_id": first.leaf_id})]:
        with pytest.raises(Busy, match="different session or payload"):
            runs.send(log, "same", request_id="global", **kwargs)
    retry = runs.send(first, "same", request_id="global")
    assert retry["input_id"] == ack["input_id"] and retry["delivery"] == "duplicate"
    gate.set()
    await finish(runs, first.id)
    assert runs.send(first, "same", request_id="global")["run_status"] == "ok"


async def test_failed_accept_rolls_back_and_connection_remains_usable(home, project, monkeypatch):
    ws = Workspace(load(home), connect=FakeModel(reply("ok")).connect)
    log = ws.new_session(project)
    runs = RunManager(ws)
    original = ws.index.add_input

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("disk write failed")

    monkeypatch.setattr(ws.index, "add_input", fail)
    with pytest.raises(OSError):
        runs.send(log, "SURVIVE", request_id="retry")
    assert not ws.index.db.in_transaction
    assert ws.index.db.execute("select count(*) from requests").fetchone()[0] == 0
    assert ws.index.pending_inputs() == []
    monkeypatch.setattr(ws.index, "add_input", original)
    runs.send(log, "SURVIVE", request_id="retry")
    await finish(runs, log.id)
    assert ws.index.run_for_request("retry")["status"] == "ok"


def scheduled(home, project, model):
    ws = Workspace(load(home), connect=model.connect)
    runs = RunManager(ws)
    notes = Notes()
    worker = scheduler.Scheduler(runs, notes)
    row = scheduler.create(ws, "test", "inspect synthetic inputs", project,
                           schedule.Rule("weekly", "09:00", "Asia/Shanghai", (1,)))
    return ws, runs, notes, worker, row


@pytest.mark.parametrize("point", ["before_input", "after_input", "after_advance", "after_commit"])
async def test_schedule_acceptance_is_atomic_across_hard_exit(home, project, point):
    ws, _, _, _, row = scheduled(home, project, FakeModel())
    ws.index.db.close()
    child('''
        import os, sys
        from pathlib import Path
        from nano_workbuddy.runtime import Workspace
        from nano_workbuddy.config import load
        home, sid, point = sys.argv[1:]
        ws = Workspace(load(Path(home)))
        row = ws.index.schedule(sid)
        original_input, original_update = ws.index.add_input, ws.index.update_schedule
        def insert(*args, **kwargs):
            if point == 'before_input': os._exit(23)
            result = original_input(*args, **kwargs)
            if point == 'after_input': os._exit(23)
            return result
        def advance(*args, **kwargs):
            result = original_update(*args, **kwargs)
            if point == 'after_advance': os._exit(23)
            return result
        ws.index.add_input, ws.index.update_schedule = insert, advance
        planned = row['next_run']
        ws.index.accept_occurrence(row, planned, 1, 'inspect synthetic inputs', f'sched:{sid}:{int(planned)}',
                                   advance=True, next_run=planned+7*86400, now=planned+1)
        os._exit(23)
    ''', home, row["id"], point)
    model = FakeModel(reply(None, call("task_result", outcome="needs_user", summary="等待人工核验")), reply("done"))
    ws2 = Workspace(load(home), connect=model.connect)
    runs = RunManager(ws2)
    worker = scheduler.Scheduler(runs, Notes())
    await worker.tick(row["next_run"] + 5)
    await finish(runs, row["session_id"])
    await worker.tick(row["next_run"] + 10)
    [occ] = ws2.index.occurrences(row["id"])
    assert occ["input_id"] and occ["outcome"] == "needs_user"
    assert ws2.index.input(occ["input_id"])["status"] == "consumed"
    assert ws2.index.schedule(row["id"])["next_run"] == row["next_run"] + 7 * 86400
    assert len([e for e in ws2.open_session(row["session_id"]).entries.values() if e.type == USER
                and e.data.get("input_id")]) == 1


async def test_busy_writer_is_retried_on_tick_without_restart_or_timeout_failure(home, project):
    ws, runs, _, worker, row = scheduled(home, project, FakeModel(reply("done")))
    held = SessionLog.open(ws.open_session(row["session_id"]).file)
    with held.writing():
        await worker.tick(row["next_run"] + 1)
        await finish(runs, row["session_id"])
        await worker.tick(row["next_run"] + 1000)
        await finish(runs, row["session_id"])
        [occ] = ws.index.occurrences(row["id"])
        assert occ["status"] == "submitted" and ws.index.input(occ["input_id"])["status"] == "pending"
    await worker.tick(row["next_run"] + 1001)
    await finish(runs, row["session_id"])
    await worker.tick(row["next_run"] + 1002)
    assert ws.index.occurrences(row["id"])[0]["outcome"] == "unknown"
    assert ws.index.pending_inputs() == []


async def test_manual_runs_with_identical_clock_values_remain_independent(home, project, monkeypatch):
    ws, runs, _, worker, row = scheduled(home, project, FakeModel(reply("one"), reply("two")))
    monkeypatch.setattr(scheduler.time, "time", lambda: 1234567890.0)
    first = await worker.run_now(row["id"])
    await finish(runs, row["session_id"])
    second = await worker.run_now(row["id"])
    await finish(runs, row["session_id"])
    assert first != second and ws.index.run_for_request(first)["id"] != ws.index.run_for_request(second)["id"]
    assert len(ws.index.occurrences(row["id"])) == 2


@pytest.mark.parametrize(("case", "expected"), [
    ("valid_file", "changed"), ("no_evidence", "unknown"), ("bad_reference", "unknown"),
    ("valid_inspection", "unchanged"), ("failed_inspection", "unknown"), ("contradictory", "unknown"),
    ("canonical_inspection", "unchanged"), ("file_uri_inspection", "unchanged"),
    ("unread_canonical_path", "unknown"),
    ("failure_after_proposal", "failed"), ("incomplete_after_proposal", "failed"), ("tampered_blob", "unknown"),
    ("cancelled_after_proposal", "cancelled"), ("budget_after_proposal", "failed"),
])
async def test_business_outcome_uses_execution_and_current_evidence(home, project, case, expected):
    (project / "previous.md").write_text("上期结果：合成材料")
    if case in ("valid_file", "tampered_blob", "bad_reference"):
        calls = [call("create_document", path="brief.md", content="# 更新\n\n合成材料")]
        calls.append(call("task_result", outcome="changed", summary="报告已交付",
                          evidence=["absent.md" if case == "bad_reference" else "brief.md"]))
    elif case == "no_evidence":
        calls = [call("task_result", outcome="changed", summary="声称已完成")]
    else:
        source = "missing.md" if case == "failed_inspection" else "previous.md"
        evidence = source
        if case == "canonical_inspection":
            evidence = str(project / source)
        elif case == "file_uri_inspection":
            evidence = (project / source).as_uri()
        elif case == "unread_canonical_path":
            (project / "unread.md").write_text("An existing file that this run did not read")
            evidence = str(project / "unread.md")
        calls = [call("read", path=source), call("task_result", outcome="unchanged",
                                                summary="与上期材料比较没有变化", evidence=[evidence])]
        if case == "contradictory":
            calls.append(call("task_result", outcome="changed", summary="又声称已变更"))
    end = RuntimeError("provider failed after proposal") if case == "failure_after_proposal" else (
        reply("partial", stop="incomplete") if case == "incomplete_after_proposal" else reply("done"))
    if case == "cancelled_after_proposal":
        end = asyncio.CancelledError()
    steps = [reply(None, *calls), end]
    if case in ("canonical_inspection", "file_uri_inspection", "unread_canonical_path"):
        # These aliases come from inspected source metadata: let the model receive the read result
        # before it proposes an outcome, as in the real scheduled trial.
        steps = [reply(None, calls[0]), reply(None, *calls[1:]), end]
    ws, runs, notes, worker, row = scheduled(home, project, FakeModel(*steps))
    if case == "budget_after_proposal":
        ws.cfg = dataclasses.replace(ws.cfg, max_turns=1)
    await worker.run_now(row["id"])
    await finish(runs, row["session_id"])
    if case == "tampered_blob":
        log = ws.open_session(row["session_id"])
        digest = next(iter(artifacts.session_records(log)[1].values()))[0]
        artifacts.blob(ws.store, digest).write_text("TAMPERED")
    await worker.tick()
    [occ] = ws.index.occurrences(row["id"])
    assert occ["outcome"] == expected
    assert bool(notes.sent) == (expected not in ("unchanged", "cancelled"))
    if expected == "unknown":
        assert "结果待核验" in notes.sent[-1][1] and "已完成" not in notes.sent[-1][1]


async def test_old_artifact_cannot_confirm_a_new_run(home, project):
    model = FakeModel(reply(None, call("create_document", path="old.md", content="# OLD")), reply("done"),
                      reply(None, call("task_result", outcome="changed", summary="新报告", evidence=["old.md"])),
                      reply("done"))
    ws, runs, _, worker, row = scheduled(home, project, model)
    await ws.submit(ws.open_session(row["session_id"]), "make old artifact")
    await worker.run_now(row["id"])
    await finish(runs, row["session_id"])
    await worker.tick()
    assert ws.index.occurrences(row["id"])[0]["outcome"] == "unknown"


async def test_restore_works_without_original_home_and_serves_old_versions(home, project, tmp_path):
    model = FakeModel(reply(None, call("create_document", path="brief.md", content="# 第一版\n\n旧版证据")),
                      reply("done"), reply(None, call("create_document", path="brief.md", content="# 第二版",
                                                    overwrite=True)), reply("done"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    upload = ws.upload(log, "input.txt", "附件原文：本地恢复验证".encode())
    runs = RunManager(ws)
    runs.send(log, "keep the attachment", attachments=[upload["id"]], request_id="initial")
    await finish(runs, log.id)
    await ws.submit(log, "update report")
    versions = ws.records(log)[1][str(project / "brief.md")]
    row = scheduler.create(ws, "pending", "do not send anything", project,
                           schedule.Rule("daily", "09:00", "Asia/Shanghai"))
    ws.index.accept_occurrence(row, row["next_run"], 1, "recover scheduled input", "sched:pending")
    archive = tmp_path / "backup.tar.gz"
    manifest = maintenance.backup(home, archive)
    assert manifest["format"] == 2 and manifest["files"] and not manifest["project_worktrees_included"]
    ws.index.db.close()
    home.rename(tmp_path / "original-unavailable")
    target = tmp_path / "restored"
    result = maintenance.restore(archive, target)
    assert result["integrity_verified"] and not home.exists()
    restored_file = target / "spill" / log.id / "uploads" / upload["id"] / "input.txt"
    model = FakeModel(reply("scheduled recovered"), reply(None, call("read", path=str(restored_file))),
                      reply("continued"))
    restored = Workspace(load(target), connect=model.connect)
    recovered = restored.open_session(log.id)
    assert Path(restored.index.session(log.id)["file_path"]).is_relative_to(target)
    attachment = next(e.data["attachments"][0] for e in recovered.entries.values()
                      if e.type == USER and e.data.get("attachments"))
    assert Path(attachment["path"]).is_relative_to(target) and Path(attachment["path"]).is_file()
    assert restored.attachments(recovered, [upload["id"]])[0][0]["sha256"] == upload["sha256"]
    async with Server(restored) as server:
        await finish(server.runs, row["session_id"])
        for digest in [*versions, upload["sha256"]]:
            response = await server.client.get(f"/api/sessions/{log.id}/files/{digest}")
            assert response.status_code == 200 and response.content
        assert restored.recall("旧版证据", kind="files")
        await restored.submit(recovered, "continue with the restored attachment")
        assert "附件原文" in text_of(next(e.data["content"] for e in reversed(recovered.path())
                                           if e.type == "tool_result"))
    assert restored.index.run_for_request("sched:pending")["status"] == "ok"


async def test_backup_and_restore_refuse_active_server(home, project, tmp_path):
    ws = Workspace(load(home), connect=FakeModel().connect)
    ws.new_session(project)
    archive = tmp_path / "backup.tar.gz"
    maintenance.backup(home, archive)
    async with Server(ws):
        with pytest.raises(RuntimeError, match="stop the server"):
            maintenance.backup(home, tmp_path / "blocked.tar.gz")
        with pytest.raises(RuntimeError, match="stop the server"):
            maintenance.restore(archive, home, force=True)


def corrupt_archive(archive, dest):
    with tarfile.open(archive, "r:gz") as src, tarfile.open(dest, "w:gz") as out:
        for member in src.getmembers():
            raw = src.extractfile(member).read()
            if member.name == "config.toml":
                raw += b"\n# corruption\n"
            member.size = len(raw)
            out.addfile(member, io.BytesIO(raw))


def test_force_restore_validates_before_moving_target_and_rolls_back(home, project, tmp_path, monkeypatch):
    ws = Workspace(load(home), connect=FakeModel().connect)
    ws.new_session(project)
    archive, damaged = tmp_path / "good.tar.gz", tmp_path / "bad.tar.gz"
    maintenance.backup(home, archive)
    corrupt_archive(archive, damaged)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("DO NOT LOSE")
    with pytest.raises(ValueError, match="integrity check failed"):
        maintenance.restore(damaged, target, force=True)
    assert (target / "keep.txt").read_text() == "DO NOT LOSE" and not list(tmp_path.glob("target.before-*"))
    replace = os.replace

    def fail(source, dest):
        if Path(source).name == "home" and Path(dest) == target:
            raise OSError("install failed")
        return replace(source, dest)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="install failed"):
        maintenance.restore(archive, target, force=True)
    assert (target / "keep.txt").read_text() == "DO NOT LOSE"


def test_hard_exit_during_restore_rolls_back_on_next_load(home, project, tmp_path):
    ws = Workspace(load(home), connect=FakeModel().connect)
    ws.new_session(project)
    archive = tmp_path / "good.tar.gz"
    maintenance.backup(home, archive)
    target = tmp_path / "target"
    load(target)
    (target / "keep.txt").write_text("PREVIOUS")
    child('''
        import os, sys
        from pathlib import Path
        from nano_workbuddy.maintenance import restore
        original = os.replace
        def die(source, dest):
            if Path(source).name == 'home' and Path(dest) == Path(sys.argv[2]): os._exit(23)
            return original(source, dest)
        os.replace = die
        restore(Path(sys.argv[1]), Path(sys.argv[2]), force=True)
    ''', archive, target)
    assert not target.exists()
    load(target)
    assert (target / "keep.txt").read_text() == "PREVIOUS"


@pytest.mark.parametrize("has_input", [False, True])
async def test_pre_rb_request_claims_are_repaired_without_duplicating_input(home, project, has_input):
    ws = Workspace(load(home), connect=FakeModel(reply("recovered")).connect)
    log = ws.new_session(project)
    sid = log.id
    ws.index.claim_request("legacy", sid, "None\0SURVIVE\0")
    if has_input:
        original = ws.index.add_input(sid, "run", "SURVIVE", extra={"request_id": "legacy"})
    ws.index.db.execute("pragma user_version=9")  # pre-link data migration
    ws.index.db.close()
    reopened = Workspace(load(home), connect=FakeModel(reply("recovered")).connect)
    runs = RunManager(reopened)
    ack = runs.send(reopened.open_session(sid), "SURVIVE", request_id="legacy")
    await finish(runs, sid)
    assert ack["delivery"] == ("duplicate" if has_input else "run")
    if has_input:
        assert ack["input_id"] == original
    assert reopened.index.db.execute("select count(*) from inputs").fetchone()[0] == 1


async def test_new_independent_input_is_queued_behind_unfinished_run(home, project):
    ws = Workspace(load(home), connect=FakeModel(reply("first"), reply("second")).connect)
    log = ws.new_session(project)
    first, _ = ws.index.accept_input(log.id, "run", "FIRST", extra={"request_id": "first"},
                                      request_id="first", payload=payload("FIRST"))
    old, _ = ws.index.start_run(log.id, "first", ws.model_ref(log), input_id=first["id"])
    log.append(USER, {"content": [{"type": "text", "text": "FIRST"}], "input_id": first["id"]}, run_id=old["id"])
    ws.index.finish_input(first["id"], "consumed", old["id"])
    second, _ = ws.index.accept_input(log.id, "run", "SECOND", extra={"request_id": "second"},
                                       request_id="second", payload=payload("SECOND"))
    runs = RunManager(ws)
    runs.resume_all()
    await finish(runs, log.id)
    assert ws.index.run_for_request("first")["id"] != ws.index.run_for_request("second")["id"]
    assert ws.index.input(second["id"])["status"] == "consumed"
    assert len([e for e in log.entries.values() if e.type == USER]) == 2


async def test_accepted_input_committed_during_live_run_is_delivered_once(home, project, monkeypatch):
    gate = asyncio.Event()

    async def hold(_):
        await gate.wait()
        return reply("first")

    model = FakeModel(hold, reply("second"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    runs = RunManager(ws)
    runs.send(log, "first")
    await asyncio.sleep(0.01)
    deliver = runs.deliver_pending

    def fail(*_):
        raise RuntimeError("exit after commit, before in-memory enqueue")

    monkeypatch.setattr(runs, "deliver_pending", fail)
    with pytest.raises(RuntimeError):
        runs.send(log, "second", request_id="recover")
    monkeypatch.setattr(runs, "deliver_pending", deliver)
    deliver()
    deliver()
    gate.set()
    await finish(runs, log.id)
    assert len([e for e in log.entries.values() if e.type == USER]) == 2 and len(model.requests) == 2


async def test_unknown_feishu_operation_remains_quarantined_after_relocation(home, project, tmp_path, lark):
    from test_authorization import effects

    (project / ".fake-lark-mode.json").write_text('{"missing_receipt":true}')
    model = FakeModel(reply("ready"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    source = ws.upload(log, "body.txt", b"APPROVED")
    op = {"kind": "send_message", "chat_id": "oc_synthetic", "content_file": source["path"]}
    model.script[:] = [reply(None, call("feishu", operation=op)), reply("unknown")]

    async def approve(*_):
        return None

    await ws.submit(log, "send", ask=approve)
    assert len(effects(project)) == 1
    archive = tmp_path / "unknown.tar.gz"
    maintenance.backup(home, archive)
    ws.index.db.close()
    home.rename(tmp_path / "unavailable")
    target = tmp_path / "restored"
    maintenance.restore(archive, target)
    retry = {**op, "content_file": str(target / Path(source["path"]).relative_to(home))}
    restored = Workspace(load(target), connect=FakeModel(reply(None, call("feishu", operation=retry)),
                                                        reply("still unknown")).connect)
    recovered = restored.open_session(log.id)
    await restored.submit(recovered, "retry", ask=approve)
    assert len(effects(project)) == 1
    assert "UNKNOWN" in text_of(next(e.data["content"] for e in reversed(recovered.path()) if e.type == "tool_result"))


async def test_confirmed_feishu_receipt_replays_locally_after_relocation(home, project, tmp_path, lark):
    from test_authorization import effects

    ws = Workspace(enable(home, lark), connect=FakeModel().connect)
    log = ws.new_session(project)
    upload = ws.upload(log, "body.txt", b"APPROVED")
    c = call("feishu", operation={"kind": "send_message", "chat_id": "oc_synthetic", "content_file": upload["path"]})
    row, _ = ws.index.start_run(log.id, None, ws.model_ref(log))
    log.append(USER, {"content": [{"type": "text", "text": "send"}]}, run_id=row["id"])
    log.append(ASSISTANT, {"content": [c], "stop": "tool_use"}, run_id=row["id"])
    log.append(TOOL_STARTED, {"call_id": c["id"], "name": c["name"]}, run_id=row["id"])
    run = ws.make_run(log, row["model_ref"], row["id"], lambda _: None)
    turn = next(e.id for e in reversed(log.path()) if e.type == "assistant")
    ctx = dataclasses.replace(run.ctx, call_key=f"{turn}:{c['id']}", call_id=c["id"])
    args = feishu.args(**c["args"])
    await _preview(args, ctx)
    await _decision(args, ctx, "approved", "a_synthetic")
    receipt = await feishu.fn(args, ctx)
    assert not receipt.is_error
    archive = tmp_path / "confirmed.tar.gz"
    maintenance.backup(home, archive)
    ws.index.db.close()
    home.rename(tmp_path / "unavailable")
    target = tmp_path / "restored"
    maintenance.restore(archive, target)
    restored = Workspace(load(target), connect=FakeModel(reply("recovered")).connect)
    runs = RunManager(restored)
    runs.resume_all()
    await finish(runs, log.id)
    assert len(effects(project)) == 1
    assert any(e.data.get("meta", {}).get("receipt", {}).get("operation_id") ==
               receipt.meta["receipt"]["operation_id"] for e in restored.open_session(log.id).entries.values())


async def test_schedule_api_distinguishes_waiting_for_user_from_running(home, project, tmp_path):
    model = FakeModel(reply(None, call("write", path=str(tmp_path / "outside.txt"), content="x")), reply("done"))
    ws, _, _, _, row = scheduled(home, project, model)
    server = Server(ws)
    worker = server.srv.config.app.state.scheduler
    worker.notify = Notes()  # no real system notifications
    async with server:
        await worker.run_now(row["id"])
        for _ in range(100):
            response = (await server.client.get("/api/schedules")).json()[0]
            if response["waiting"]:
                break
            await asyncio.sleep(0.01)
        assert response["running"] and response["waiting"]
        server.runs.cancel(row["session_id"])
        await finish(server.runs, row["session_id"])
        response = (await server.client.get("/api/schedules")).json()[0]
        assert not response["waiting"] and not response["running"]


def test_config_only_home_and_legacy_backup_remain_restorable(home, tmp_path):
    load(home)
    archive = tmp_path / "minimal.tar.gz"
    maintenance.backup(home, archive)
    target = tmp_path / "new"
    assert maintenance.restore(archive, target)["integrity_verified"]
    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(legacy, "w:gz") as out:
        for member in src.getmembers():
            raw = src.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(raw)
                manifest["format"] = 1
                manifest.pop("files")
                raw = json.dumps(manifest).encode()
            member.size = len(raw)
            out.addfile(member, io.BytesIO(raw))
    restored = maintenance.restore(legacy, tmp_path / "legacy-home")
    assert not restored["integrity_verified"]  # legacy lacks hashes; never claim a checksum validation


async def test_missing_external_project_is_reported_and_not_run(home, project, tmp_path):
    ws, _, _, _, row = scheduled(home, project, FakeModel())
    archive = tmp_path / "missing-project.tar.gz"
    maintenance.backup(home, archive)
    project.rename(tmp_path / "project-unavailable")
    target = tmp_path / "restored"
    report = maintenance.restore(archive, target)
    assert report["relocation"]["missing_projects"][0]["root_path"] == str(project)
    model = FakeModel()
    restored = Workspace(load(target), connect=model.connect)
    runs, notes = RunManager(restored), Notes()
    worker = scheduler.Scheduler(runs, notes)
    await worker.run_now(row["id"])
    await finish(runs, row["session_id"])
    await worker.tick()
    assert not model.requests
    assert restored.index.occurrences(row["id"])[0]["outcome"] == "failed" and notes.sent
