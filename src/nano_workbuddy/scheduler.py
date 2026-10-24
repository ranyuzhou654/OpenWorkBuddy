"""Scheduled tasks (M3): submit work when it is due; the existing run machinery does the work.

The scheduler only decides *when*. It never sleeps until a task's time: every tick (30 s) it compares each
schedule's next_run with the clock, so a machine waking from sleep or a server restarted after hours simply
finds the missed occurrences on its next tick and applies the schedule's catch-up rule (schedule.decide).

  due      → claim the occurrence (unique per schedule and planned instant) → send the task's message into
             the schedule's own session with request_id "sched:<id>:<instant>" (durable input, M0)
  finished → read the run's outcome (the task_result tool; otherwise "unknown") → record it → notify the
             user when something changed, failed or needs them; an unchanged result stays quiet
  waiting  → an approval or a question in a scheduled session notifies the user at once

Only one process schedules: the one holding an exclusive lock on ~/.nwb/scheduler.lock.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from . import schedule as sched
from .log import SessionLog
from .notify import notify as system_notify
from .tools.builtin import Args
from .tools.registry import Result, ToolCtx, tool
from .types import ASSISTANT, TOOL_RESULT, text, text_of

log = logging.getLogger(__name__)

TICK = 30
LOST_AFTER = 600  # seconds: a submitted occurrence whose run never appeared is recorded as failed
OUTCOMES = ("changed", "unchanged", "failed", "needs_user")
QUIET = ("unchanged", "skipped", "cancelled")  # outcomes that do not interrupt the user

type Notify = Callable[[str, str, str], Awaitable[Any]]


# --- the tool a scheduled run reports with ---


class TaskResultArgs(Args):
    outcome: Literal["changed", "unchanged", "failed", "needs_user"] = Field(
        description="changed: there is something new for the user; unchanged: nothing worth their attention; "
                    "failed: the task could not be done; needs_user: a decision or login only the user can give")
    summary: str = Field(description="One or two sentences for the notification: what changed, or why not")


@tool(effect="read", scope=lambda args, ctx: ("task_result", ""))
async def task_result(args: TaskResultArgs, ctx: ToolCtx) -> Result:
    """Report the outcome of this scheduled task. Call it once, at the end. The user is notified only when the
    outcome is changed, failed or needs_user, so an honest "unchanged" keeps them from being disturbed."""
    return Result([text(f"Recorded: {args.outcome}.")], meta={"task_result": args.model_dump()})


SCHEDULED_NOTICE = ("This run was started by a scheduled task, not by the user typing: nobody is watching it live. "
                    "Do the task as described, never send or publish anything outside this computer, and finish by "
                    "calling task_result exactly once with an honest outcome.")


def rule_of(row: dict[str, Any]) -> sched.Rule:
    return sched.Rule(row["kind"], row["at_time"], row["tz"],
                      tuple(int(d) for d in row["weekdays"].split(",") if d), row["day"])


def local_time(ts: float, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def message(row: dict[str, Any], planned: float, missed: int, now: float) -> str:
    late = f"；补跑：错过了 {missed} 次执行，只补这一次" if missed > 1 or now - planned > sched.GRACE else ""
    return f"[定时任务「{row['name']}」· 计划时间 {local_time(planned, row['tz'])}{late}]\n\n{row['prompt']}"


def outcome_of(session: SessionLog, run: dict[str, Any]) -> tuple[str, str]:
    """A finished run's outcome: what task_result reported, else derived from how the run ended."""
    entries = sorted((e for e in session.entries.values() if e.run_id == run["id"]), key=lambda e: e.seq)
    reported = [e.data["meta"]["task_result"] for e in entries
                if e.type == TOOL_RESULT and "task_result" in e.data.get("meta", {})]
    if reported:
        return reported[-1]["outcome"], reported[-1]["summary"]
    last = next((text_of(e.data["content"]) for e in reversed(entries) if e.type == ASSISTANT
                 and text_of(e.data["content"]).strip()), "")
    status = run["status"]
    if status == "cancelled":
        return "cancelled", "已取消"
    if status in ("error", "blocked"):
        return "failed", last[:200] or f"运行出错（{status}）"
    return "unknown", (last[:200] or "运行结束，但没有报告结果")


class Scheduler:
    def __init__(self, runs: Any, notify: Notify | None = None, tick: float = TICK):
        self.runs, self.ws = runs, runs.ws
        self.notify = notify or system_notify
        self.tick_seconds = tick
        self._task: asyncio.Task[None] | None = None
        self._lock_fd: int | None = None
        runs.attention = self._attention

    # lifecycle

    def start(self) -> bool:
        """Begin ticking if no other process is scheduling for this home. Returns whether it started."""
        path = self.ws.cfg.home / "scheduler.lock"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            log.warning("another nano-workbuddy process is running the scheduler; this one will not")
            return False
        self._lock_fd = fd
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        return True

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # one broken schedule must not stop the others, now or later
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick_seconds)

    # the work

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for row in self.ws.index.schedules():
            if row["enabled"]:
                await self._due(row, now)
        await self._collect(now)

    async def _due(self, row: dict[str, Any], now: float) -> None:
        rule = rule_of(row)
        d = sched.decide(rule, row["next_run"], now, row["catch_up"])
        if d.action == "none":
            return
        index = self.ws.index
        if d.action == "run":
            if row["session_id"] in self.runs.live:
                return  # the user is working in that session: try again on the next tick
            rid = f"sched:{row['id']}:{int(d.planned)}"
            if index.claim_occurrence(row["id"], d.planned, d.missed, "submitted", rid):
                log_ = self.ws.open_session(row["session_id"])
                self.runs.send(log_, message(row, d.planned, d.missed, now), request_id=rid)
        elif index.claim_occurrence(row["id"], d.planned, d.missed, "done"):
            outcome = "skipped" if d.action == "skip" else "missed"
            summary = f"错过了 {d.missed} 次执行" + ("，已跳过" if d.action == "skip" else "，等你决定是否立即运行")
            index.finish_occurrence(row["id"], d.planned, outcome, summary)
            if d.action == "ask":
                await self.notify("nano-workbuddy", summary + "（在任务页点“立即运行”）", row["name"])
        index.update_schedule(row["id"], next_run=d.next_run, **({"last_run_at": now} if d.action == "run" else {}))

    async def _collect(self, now: float) -> None:
        """Record the outcome of submitted occurrences whose runs have finished."""
        index = self.ws.index
        for occ in index.occurrences(status="submitted", limit=200):
            row = index.schedule(occ["schedule_id"])
            run = index.run_for_request(occ["request_id"])
            if row is None:
                continue
            if run is None:  # submitted while the session was idle, so a run row appears within seconds
                if now - occ["created_at"] > LOST_AFTER and not self._still_pending(row["session_id"]):
                    self._finish(row, occ, "failed", "任务没有开始运行（消息未能送达）")
                continue
            if run["status"] in ("running", "interrupted"):
                continue
            outcome, summary = outcome_of(self.ws.open_session(row["session_id"]), run)
            self._finish(row, occ, outcome, summary)
            if outcome not in QUIET:
                label = {"changed": "有新结果", "failed": "失败", "needs_user": "需要你处理",
                         "unknown": "已完成（未报告结果）"}[outcome]
                await self.notify("nano-workbuddy", f"{label}：{summary}", row["name"])

    def _still_pending(self, session_id: str) -> bool:
        return session_id in self.runs.live or bool(self.ws.index.pending_inputs(session_id))

    def _finish(self, row: dict[str, Any], occ: dict[str, Any], outcome: str, summary: str) -> None:
        self.ws.index.finish_occurrence(row["id"], occ["planned_at"], outcome, summary)
        self.ws.index.update_schedule(row["id"], last_outcome=outcome, last_summary=summary)

    def _attention(self, session: SessionLog, kind: str, what: str) -> None:
        """A scheduled run is waiting for an approval or an answer: nobody is watching, so say so now."""
        if row := self.ws.index.schedule_for_session(session.id):
            label = "需要你确认" if kind == "approval" else "需要你回答"
            asyncio.get_running_loop().create_task(self.notify("nano-workbuddy", f"{label}：{what}", row["name"]))

    async def run_now(self, schedule_id: str) -> str:
        """The user's "立即运行": an occurrence at this instant, outside the rule."""
        row = self.ws.index.schedule(schedule_id)
        if row is None:
            raise KeyError(f"no schedule {schedule_id}")
        if row["session_id"] in self.runs.live:
            raise RuntimeError("the task's session is busy; try again when it has finished")
        now = time.time()
        rid = f"sched:{row['id']}:{int(now)}"
        self.ws.index.claim_occurrence(row["id"], now, 1, "submitted", rid)
        self.runs.send(self.ws.open_session(row["session_id"]), message(row, now, 1, now), request_id=rid)
        return rid


# --- templates ---


def weekly_research(topic: str, file: str, extra: str = "") -> str:
    """The first template (2026-10-24): a recurring research update that only produces a new version of the
    brief when there is something new, and never publishes it."""
    return json.dumps({"template": "weekly_research", "topic": topic, "file": file}, ensure_ascii=False), (
        f"主题：{topic}\n\n"
        "请完成本期调研更新：\n"
        "1. 用 web_search 搜索这个主题最近一段时间的新信息（recency 选 week；按需要多换几个关键词），"
        "对重要结果用 web_fetch 阅读原文，不要只凭搜索摘要下结论。\n"
        f"2. 如果项目里已有上一期简报 {file}，先用 read 读它，只关注相对上一期新增或变化的内容。\n"
        f"3. 有实质性的新信息：用 create_document 生成新一期 {file}（已有文件时 overwrite=true，作为新版本），"
        "开头写“本期变化”，每条事实后标注来源编号 [S#]，区分事实、推断和建议。\n"
        "4. 没有实质性的新信息：不要生成文件。\n"
        "5. 不要发送或发布任何内容到这台电脑以外。\n"
        "6. 最后调用 task_result：有新一期简报用 changed，没有新内容用 unchanged，做不了用 failed，"
        "缺搜索密钥或需要用户决定时用 needs_user，并用一两句话说明。"
        + (f"\n\n补充要求：{extra}" if extra else ""))


# --- creating and editing schedules ---

RULE_FIELDS = ("kind", "at_time", "tz", "weekdays", "day")


def create(ws: Any, name: str, prompt: str, project_root: Any, rule: sched.Rule, catch_up: str = "once",
           model: str | None = None, template_json: str = "{}") -> dict[str, Any]:
    """A schedule with its own session (in the project, or the chat folder): every run of the task lands in
    that one conversation, so a run sees what the previous ones found and delivered."""
    rule.validate()
    if catch_up not in sched.CATCH_UP:
        raise ValueError(f"catch_up must be one of {', '.join(sched.CATCH_UP)}")
    if not name.strip() or not prompt.strip():
        raise ValueError("a task needs a name and instructions")
    log = ws.new_session(project_root, model)
    ws.index.rename_session(log.id, f"定时：{name.strip()}")
    return ws.index.add_schedule(log.id, name=name.strip(), prompt=prompt.strip(), kind=rule.kind, at_time=rule.at,
                                 tz=rule.tz, weekdays=",".join(map(str, rule.weekdays)), day=rule.day,
                                 catch_up=catch_up, enabled=1, next_run=rule.next_after(time.time()),
                                 template_json=template_json)


def update(ws: Any, schedule_id: str, **fields: Any) -> dict[str, Any]:
    """Change a schedule. A changed rule, or switching it back on, moves next_run to the next occurrence from now
    (occurrences missed while it was off are not caught up)."""
    row = ws.index.schedule(schedule_id)
    if row is None:
        raise KeyError(f"no schedule {schedule_id}")
    if "weekdays" in fields and not isinstance(fields["weekdays"], str):
        fields["weekdays"] = ",".join(map(str, fields["weekdays"]))
    merged = {**row, **{k: v for k, v in fields.items() if v is not None}}
    rule = rule_of(merged)
    rule.validate()
    if merged["catch_up"] not in sched.CATCH_UP:
        raise ValueError(f"catch_up must be one of {', '.join(sched.CATCH_UP)}")
    changes = {k: v for k, v in fields.items() if v is not None}
    if "enabled" in changes:
        changes["enabled"] = int(bool(changes["enabled"]))
    if any(k in changes for k in RULE_FIELDS) or changes.get("enabled") and not row["enabled"]:
        changes["next_run"] = rule.next_after(time.time())
    return ws.index.update_schedule(schedule_id, **changes)


def view(ws: Any, row: dict[str, Any]) -> dict[str, Any]:
    """A schedule as the task page shows it."""
    rule = rule_of(row)
    return {**{k: row[k] for k in ("id", "name", "prompt", "session_id", "kind", "at_time", "tz", "day", "catch_up",
                                   "next_run", "last_run_at", "last_outcome", "last_summary", "created_at")},
            "weekdays": [int(d) for d in row["weekdays"].split(",") if d], "enabled": bool(row["enabled"]),
            "template": json.loads(row["template_json"] or "{}"), "describe": rule.describe(),
            "next_local": local_time(row["next_run"], row["tz"]) if row["next_run"] else None,
            "running": row["session_id"] in ws.running,
            "recent": ws.index.occurrences(row["id"], limit=8)}
