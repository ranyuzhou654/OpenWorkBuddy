"""Scheduled tasks, search over past records, backup and restore (M3)."""

import asyncio
import io
import plistlib
import tarfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModel, call, reply
from test_api import Server

from nano_workbuddy import maintenance, schedule
from nano_workbuddy import scheduler as sched_mod
from nano_workbuddy.config import load
from nano_workbuddy.runs import RunManager
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import TOOL_RESULT

SH = "Asia/Shanghai"


def ts(s: str, tz: str = SH) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(tz)).timestamp()


# --- rules ---


def test_next_occurrences():
    daily = schedule.Rule("daily", "09:00", SH)
    assert daily.next_after(ts("2026-10-24 08:59")) == ts("2026-10-24 09:00")
    assert daily.next_after(ts("2026-10-24 09:00")) == ts("2026-10-25 09:00")  # strictly after
    weekly = schedule.Rule("weekly", "08:30", SH, (1, 5))  # Monday and Friday; 2026-10-24 is a Saturday
    assert weekly.next_after(ts("2026-10-24 12:00")) == ts("2026-10-26 08:30")
    assert weekly.next_after(ts("2026-10-26 09:00")) == ts("2026-10-30 08:30")
    once = schedule.Rule("once", "18:00", SH, day="2026-10-25")
    assert once.next_after(ts("2026-10-24 00:00")) == ts("2026-10-25 18:00")
    assert once.next_after(ts("2026-10-26 00:00")) is None
    assert weekly.describe() == "每周一、五 08:30" and schedule.Rule("weekly", "9:00", SH, (1, 2, 3, 4, 5)).describe() \
        == "工作日 9:00"


def test_local_time_survives_daylight_saving():
    ny = schedule.Rule("daily", "09:00", "America/New_York")
    before = ny.next_after(ts("2026-10-31 08:00", "America/New_York"))  # DST ends 2026-11-01 02:00
    after = ny.next_after(before)
    for t in (before, after):
        assert datetime.fromtimestamp(t, ZoneInfo("America/New_York")).strftime("%H:%M") == "09:00"
    assert after - before == 25 * 3600  # the clock went back: a 25-hour day, still 9 o'clock


@pytest.mark.parametrize(("rule", "why"), [
    (schedule.Rule("weekly", "09:00", SH, ()), "weekdays"), (schedule.Rule("daily", "25:00", SH), "HH:MM"),
    (schedule.Rule("daily", "09:00", "Mars/Base"), "time zone"), (schedule.Rule("once", "09:00", SH), "date"),
])
def test_invalid_rules(rule, why):
    with pytest.raises(ValueError, match=why):
        rule.validate()


def test_decide_on_time_late_and_missed():
    rule = schedule.Rule("daily", "09:00", SH)
    nine = ts("2026-10-24 09:00")
    on_time = schedule.decide(rule, nine, nine + 60, "skip")
    assert (on_time.action, on_time.missed, on_time.next_run) == ("run", 1, ts("2026-10-25 09:00"))
    asleep_three_days = nine + 3 * 86400 - 3600  # woke at 08:00 three days later: 3 occurrences missed
    for catch_up, action in (("once", "run"), ("skip", "skip"), ("ask", "ask")):
        d = schedule.decide(rule, nine, asleep_three_days, catch_up)
        assert (d.action, d.missed, d.planned) == (action, 3, ts("2026-10-26 09:00"))
        assert d.next_run == ts("2026-10-27 09:00")
    woke_at_nine = schedule.decide(rule, nine, nine + 2 * 86400 + 30, "skip")
    assert (woke_at_nine.action, woke_at_nine.missed) == ("run", 3)  # today's run is on time, misses folded in
    assert schedule.decide(rule, nine, nine - 1, "once").action == "none"


# --- the scheduler ---


class Notes:
    def __init__(self):
        self.sent = []

    async def __call__(self, title, body, subtitle=""):
        self.sent.append((subtitle, body))


def research_model(outcome: str = "changed", summary: str = "新增两条进展"):
    return FakeModel(reply(None, call("create_document", path="brief.md", content="# 新进展\n\n本地合成验证。"),
                          call("task_result", outcome=outcome, summary=summary, evidence=["brief.md"])), reply("done"),
                     reply(None, call("read", path="brief.md"),
                           call("task_result", outcome="unchanged", summary="与上期对比无新内容",
                                evidence=["brief.md"])),
                     reply("done"))


async def finish(runs, sid):
    for _ in range(200):
        if sid not in runs.live:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


@pytest.fixture
def setup(home, project):
    def make(model):
        ws = Workspace(load(home), connect=model.connect)
        runs, notes = RunManager(ws), Notes()
        s = sched_mod.Scheduler(runs, notes)
        template, prompt = sched_mod.weekly_research("本地 Agent 框架", "brief.md")
        row = sched_mod.create(ws, "周报", prompt, project, schedule.Rule("weekly", "09:00", SH, (1,)),
                               template_json=template)
        return ws, runs, s, notes, row

    return make


async def test_due_task_runs_once_and_reports(setup):
    model = research_model()
    ws, runs, s, notes, row = setup(model)
    assert ws.index.session(row["session_id"])["title"] == "定时：周报"
    planned = row["next_run"]
    await s.tick(planned + 5)
    await s.tick(planned + 10)  # a second tick (or a restart) never submits the same occurrence again
    await finish(runs, row["session_id"])
    await s.tick(planned + 20)
    [occ] = ws.index.occurrences(row["id"])
    assert (occ["status"], occ["outcome"], occ["summary"]) == ("done", "changed", "新增两条进展")
    assert notes.sent == [("周报", "有新结果：新增两条进展")]
    first = model.requests[0]
    assert "task_result" in [t["name"] for t in first["tools"]]
    sent = [m for m in first["messages"] if m["role"] == "user"]
    assert any("[定时任务「周报」" in m["content"][0]["text"] and "本地 Agent 框架" in m["content"][0]["text"]
               for m in sent)
    assert any("nobody is watching" in m["content"][0]["text"] for m in sent)
    assert ws.index.schedule(row["id"])["next_run"] == planned + 7 * 86400

    await s.tick(planned + 7 * 86400 + 5)  # next week: nothing new, so no notification
    await finish(runs, row["session_id"])
    await s.tick(planned + 7 * 86400 + 30)
    assert ws.index.schedule(row["id"])["last_outcome"] == "unchanged" and len(notes.sent) == 1


async def test_missed_occurrences_follow_catch_up(setup):
    ws, runs, s, notes, row = setup(research_model())
    ws.index.update_schedule(row["id"], catch_up="ask")
    late = row["next_run"] + 2 * 7 * 86400 + 3600  # two weeks asleep, woke an hour after the time
    await s.tick(late)
    [occ] = ws.index.occurrences(row["id"])
    assert occ["outcome"] == "missed" and occ["missed"] == 3 and row["session_id"] not in runs.live
    assert "错过了 3 次执行" in notes.sent[0][1]
    await s.tick(late + 60)  # that occurrence was handled (by asking): it is never run on its own afterwards
    assert row["session_id"] not in runs.live and len(ws.index.occurrences(row["id"])) == 1
    ws.index.update_schedule(row["id"], catch_up="once", next_run=row["next_run"])
    await s.tick(late + 7 * 86400)  # a week later, catch-up "once": the four missed fold into one run
    await finish(runs, row["session_id"])
    user = next(e for e in ws.open_session(row["session_id"]).path() if e.type == "user")
    assert "补跑：错过了 4 次执行" in user.data["content"][0]["text"]


async def test_busy_session_defers_and_waiting_notifies(setup, tmp_path):
    gate = asyncio.Event()

    async def held(msgs):
        await gate.wait()
        return reply("still chatting")

    outside = tmp_path / "outside.txt"
    model = FakeModel(held, reply(None, call("write", path=str(outside), content="x")), reply("ok"))
    ws, runs, s, notes, row = setup(model)
    log = ws.open_session(row["session_id"])
    runs.send(log, "the user is talking here")
    await asyncio.sleep(0.05)
    await s.tick(row["next_run"] + 5)
    assert ws.index.occurrences(row["id"]) == []  # deferred, not lost
    gate.set()
    await finish(runs, row["session_id"])
    await s.tick(row["next_run"] + 35)
    for _ in range(100):  # the scheduled run writes outside the project: it waits for an approval
        if notes.sent:
            break
        await asyncio.sleep(0.02)
    assert notes.sent[0][0] == "周报" and notes.sent[0][1].startswith("需要你确认：write")
    runs.cancel(row["session_id"])
    await finish(runs, row["session_id"])


async def test_run_now_and_task_result_only_in_scheduled_runs(setup, project):
    model = FakeModel(reply("plain chat"), reply(None, call("task_result", outcome="failed", summary="缺少搜索密钥")),
                      reply("ok"))
    ws, runs, s, notes, row = setup(model)
    await ws.submit(ws.new_session(project), "hello")
    assert "task_result" not in [t["name"] for t in model.requests[0]["tools"]]
    await s.run_now(row["id"])
    await finish(runs, row["session_id"])
    await s.tick(time.time())
    assert notes.sent == [("周报", "失败：缺少搜索密钥")]


async def test_scheduled_input_keeps_its_request_id_across_restart(home, project):
    """Accepted but not yet run when the server stopped: redelivered with its request id, so the occurrence
    still finds its run."""
    ws = Workspace(load(home), connect=FakeModel().connect)
    row = sched_mod.create(ws, "t", "do it", project, schedule.Rule("daily", "09:00", SH))
    rid = f"sched:{row['id']}:123"
    ws.index.claim_occurrence(row["id"], 123, 1, "submitted", rid)
    ws.index.add_input(row["session_id"], "run", "do it", extra={"request_id": rid})
    ws.index.db.close()
    model = FakeModel(reply(None, call("task_result", outcome="changed", summary="ok")), reply("done"))
    ws2 = Workspace(load(home), connect=model.connect)
    runs = RunManager(ws2)
    runs.resume_all()
    await finish(runs, row["session_id"])
    assert ws2.index.run_for_request(rid)["status"] == "ok"


async def test_schedule_api(home, project):
    async with Server(Workspace(load(home), connect=FakeModel().connect)) as s:
        pid = (await s.client.post("/api/projects", json={"path": str(project), "trusted": True})).json()["id"]
        r = await s.client.post("/api/schedules", json={
            "name": "调研周报", "template": {"kind": "weekly_research", "topic": "Agent 框架", "file": "周报.md"},
            "project_id": pid, "kind": "weekly", "at": "09:30", "weekdays": [1, 4]})
        made = r.json()
        assert made["describe"] == "每周一、四 09:30" and made["template"]["topic"] == "Agent 框架"
        assert "周报.md" in made["prompt"] and made["next_local"].endswith("09:30")
        bad = await s.client.post("/api/schedules", json={"name": "x", "prompt": "y", "kind": "weekly", "weekdays": []})
        assert bad.status_code == 400
        off = (await s.client.patch(f"/api/schedules/{made['id']}", json={"enabled": False})).json()
        assert off["enabled"] is False
        moved = (await s.client.patch(f"/api/schedules/{made['id']}", json={"kind": "daily", "at": "07:00"})).json()
        assert moved["describe"] == "每天 07:00"
        assert [x["id"] for x in (await s.client.get("/api/schedules")).json()] == [made["id"]]
        assert (await s.client.delete(f"/api/schedules/{made['id']}")).json()["ok"]
        assert s.ws.index.session(made["session_id"]) is not None  # its history stays


# --- search over past records ---


async def test_recall_finds_chinese_text_sources_and_files(home, project):
    model = FakeModel(
        reply("我们决定先做调研工作流，周报每周一发布。"),
        reply(None, call("create_document", path="brief.md", content="# 竞品对比\n\nOpenCode 的事件流设计值得借鉴。")),
        reply("done"),
        reply(None, call("recall", query="调研 周报")), reply("found"))
    ws = Workspace(load(home), connect=model.connect)
    first = ws.new_session(project)
    await ws.submit(first, "定一下下季度的重点")
    await ws.submit(ws.new_session(project), "做一份对比文档")
    assert [h["kind"] for h in ws.recall("调研工作流")] == ["message"]  # 5 characters: trigram match
    assert ws.recall("周报")[0]["session_id"] == first.id  # 2 characters: substring fallback
    hits = ws.recall("事件流设计", kind="files")
    assert hits and hits[0]["kind"] == "file" and hits[0]["ref"].endswith("brief.md")
    assert ws.recall("事件流设计", kind="messages") == []
    later = ws.new_session(project)
    await ws.submit(later, "上次说的周报是哪天？")
    result = next(e.data for e in later.path() if e.type == TOOL_RESULT)
    assert "[conversation]" in result["content"][0]["text"] and first.id in result["content"][0]["text"]
    ws.delete_session(first.id)  # forgetting follows deletion
    assert all(h["session_id"] != first.id for h in ws.recall("周报"))


# --- backup and restore ---


async def test_backup_and_restore_round_trip(home, project, tmp_path):
    ws = Workspace(load(home), connect=FakeModel(reply("记住了")).connect)
    log = ws.new_session(project)
    await ws.submit(log, "备份测试")
    (home / "secrets.json").write_text('{"x": "sk-secret"}')
    (home / "browser" / "profile").mkdir(parents=True)
    (home / "browser" / "profile" / "Cookies").write_text("cookie")
    archive = tmp_path / "b.tar.gz"
    m = maintenance.backup(home, archive)
    assert "secrets.json" not in m["included"] and "browser/profile" not in m["included"]
    assert oct(archive.stat().st_mode & 0o777) == "0o600"

    restored = tmp_path / "restored"
    maintenance.restore(archive, restored)
    ws2 = Workspace(load(restored), connect=FakeModel().connect)
    assert ws2.index.session(log.id)["title"] == "备份测试"
    assert not (restored / "secrets.json").exists() and not (restored / "browser").exists()
    with pytest.raises(FileExistsError):
        maintenance.restore(archive, restored)
    maintenance.restore(archive, restored, force=True)
    assert list(tmp_path.glob("restored.before-restore-*"))
    assert "secrets.json" in maintenance.backup(home, tmp_path / "s.tar.gz", with_secrets=True)["included"]


def test_restore_refuses_unsafe_archives(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        for name, data in (("manifest.json", b"{}"), ("../escaped.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(tarfile.FilterError):
        maintenance.restore(evil, tmp_path / "home")
    assert not (tmp_path / "escaped.txt").exists()
    plain = tmp_path / "plain.tar.gz"
    with tarfile.open(plain, "w:gz"):
        pass
    with pytest.raises(ValueError, match="not a nano-workbuddy backup"):
        maintenance.restore(plain, tmp_path / "home2")


def test_launch_agent_plist(home):
    p = maintenance.service_plist(home, 8765)
    assert p["Label"] == maintenance.LABEL and p["RunAtLoad"] and p["ProgramArguments"][-3:] == ["--no-open", "--port",
                                                                                                "8765"]
    assert plistlib.loads(plistlib.dumps(p))["StandardOutPath"].endswith("serve.log")
