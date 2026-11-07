"""Scheduled tasks (M3): submit work when it is due; the existing run machinery does the work.

The scheduler only decides *when*. It never sleeps until a task's time: every tick (30 s) it compares each
schedule's next_run with the clock, so a machine waking from sleep or a server restarted after hours simply
finds the missed occurrences on its next tick and applies the schedule's catch-up rule (schedule.decide).

  due      → atomically accept occurrence + durable input + next_run → dispatch/retry in its own session
  finished → verify run status and task_result proposal against current execution evidence → record → notify the
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

from . import artifacts
from . import schedule as sched
from .log import SessionLog
from .notify import notify as system_notify
from .tools.builtin import Args
from .tools.registry import Result, ToolCtx, tool
from .types import ASSISTANT, TOOL_RESULT, new_id, text

log = logging.getLogger(__name__)

TICK = 30
OUTCOMES = ("changed", "unchanged", "failed", "needs_user")
QUIET = ("unchanged", "skipped", "cancelled")  # outcomes that do not interrupt the user

type Notify = Callable[[str, str, str], Awaitable[Any]]


# --- the tool a scheduled run reports with ---


class TaskResultArgs(Args):
    outcome: Literal["changed", "unchanged", "failed", "needs_user"] = Field(
        description="changed: there is something new for the user; unchanged: nothing worth their attention; "
                    "failed: the task could not be done; needs_user: a decision or login only the user can give")
    summary: str = Field(description="One or two sentences for the notification: what changed, or why not")
    evidence: list[str] = Field(default_factory=list, description="For changed: delivered file path/name/hash "
        "or confirmed receipt ID. For unchanged: paths/URLs/queries inspected successfully in this run. "
        "A report is only a proposal; the host checks it against execution evidence.")


@tool(effect="read", scope=lambda args, ctx: ("task_result", ""))
async def task_result(args: TaskResultArgs, ctx: ToolCtx) -> Result:
    """Propose this task's outcome once at the end, with evidence references. The host verifies completion;
    only a verified unchanged result stays quiet. A proposal cannot override a failed or incomplete run."""
    return Result([text(f"Proposed: {args.outcome}; awaiting execution checks.")],
                  meta={"task_result": args.model_dump()})


SCHEDULED_NOTICE = ("This run was started by a scheduled task, not by the user typing: nobody is watching it live. "
                    "Do the task as described, never send or publish anything outside this computer, and finish by "
                    "calling task_result exactly once with an honest outcome and evidence references. "
                    "changed needs a delivered file or confirmed receipt from this run; unchanged needs successful "
                    "inspection references and a comparison summary. Do not treat failed checks as no change.")


def rule_of(row: dict[str, Any]) -> sched.Rule:
    return sched.Rule(row["kind"], row["at_time"], row["tz"],
                      tuple(int(d) for d in row["weekdays"].split(",") if d), row["day"])


def local_time(ts: float, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def message(row: dict[str, Any], planned: float, missed: int, now: float) -> str:
    late = f"；补跑：错过了 {missed} 次执行，只补这一次" if missed > 1 or now - planned > sched.GRACE else ""
    return f"[定时任务「{row['name']}」· 计划时间 {local_time(planned, row['tz'])}{late}]\n\n{row['prompt']}"


def outcome_of(session: SessionLog, run: dict[str, Any], store: Any = None) -> tuple[str, str]:
    """A finished run's outcome: what task_result reported, else derived from how the run ended."""
    entries = sorted((e for e in session.entries.values() if e.run_id == run["id"]), key=lambda e: e.seq)
    status = run["status"]
    if status == "cancelled":
        return "cancelled", "已取消"
    if status != "ok":
        return ("unknown" if status in ("running", "interrupted") else "failed"), f"运行未正常完成（{status}）"
    reported = [e.data["meta"]["task_result"] for e in entries
                if e.type == TOOL_RESULT and not e.data.get("is_error") and "task_result" in e.data.get("meta", {})]
    if len(reported) != 1:
        return "unknown", "结果待核验：未报告结果或多次报告互相冲突"
    proposal = reported[0]
    outcome, summary = proposal["outcome"], proposal["summary"].strip()
    if not summary:
        return "unknown", "结果待核验：缺少结果或比较说明"
    if outcome in ("failed", "needs_user"):
        return outcome, summary
    results = [e.data for e in entries if e.type == TOOL_RESULT]
    if any(r.get("is_error") for r in results):
        return "unknown", "结果待核验：本轮存在未核验恢复的工具错误"
    refs = proposal.get("evidence", [])
    valid = set()
    calls = {c["id"]: c for e in entries if e.type == ASSISTANT for c in e.data["content"]
             if c["type"] == "tool_call"}
    for r in results:
        meta = r.get("meta", {})
        if outcome == "changed":
            if ((a := meta.get("artifact")) and a.get("checks")
                    and all(c["ok"] is not False for c in a["checks"])
                    and any(c["ok"] is True and c.get("layer", "structure") == "structure" for c in a["checks"])
                    and store and a.get("kept") and artifacts.blob(store, a["sha256"]).is_file()
                    and artifacts.sha256_of(artifacts.blob(store, a["sha256"])) == a["sha256"]):
                valid.update((a["path"], a["name"], a["sha256"]))
            if (receipt := meta.get("receipt")) and receipt.get("status") == "confirmed":
                valid.add(receipt["operation_id"])
                valid.update(str(v) for k, v in receipt.get("result", {}).items()
                             if k in ("message_id", "document_id", "token"))
        elif (c := calls.get(r["call_id"])) and c["name"] in (
                "read", "grep", "glob", "web_fetch", "web_search", "recall"):
            valid.add(c["id"])
            valid.update(v for k, v in c["args"].items() if k in ("path", "url", "query") and isinstance(v, str))
            valid.update(s["url"] for s in meta.get("sources", []) if s.get("kind") == "page")
            # read/grep expose canonical paths, while calls may use relative paths. Only trust
            # file sources with returned excerpts from these successful calls, never an arbitrary alias.
            if c["name"] in ("read", "grep"):
                read_numbers = {read["n"] for read in meta.get("source_reads", [])}
                for source in meta.get("sources", []):
                    if source.get("kind") == "file" and source.get("n") in read_numbers:
                        valid.update(source[k] for k in ("path", "url") if source.get(k))
    if not refs or any(ref not in valid for ref in refs):
        return "unknown", "结果待核验：报告缺少本轮有效交付或检查证据"
    return outcome, summary


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
        for occ in self.ws.index.occurrences(status="submitted", limit=200):
            if not occ["input_id"] and (row := self.ws.index.schedule(occ["schedule_id"])):
                self.ws.index.accept_occurrence(row, occ["planned_at"], occ["missed"],
                    message(row, occ["planned_at"], occ["missed"], now), occ["request_id"])
        for row in self.ws.index.schedules():
            if row["enabled"]:
                await self._due(row, now)
        self.runs.deliver_pending()
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
            index.accept_occurrence(row, d.planned, d.missed, message(row, d.planned, d.missed, now), rid,
                                    advance=True, next_run=d.next_run, now=now)
        elif index.claim_occurrence(row["id"], d.planned, d.missed, "done"):
            outcome = "skipped" if d.action == "skip" else "missed"
            summary = f"错过了 {d.missed} 次执行" + ("，已跳过" if d.action == "skip" else "，等你决定是否立即运行")
            index.finish_occurrence(row["id"], d.planned, outcome, summary)
            if d.action == "ask":
                await self.notify("nano-workbuddy", summary + "（在任务页点“立即运行”）", row["name"])
        if d.action != "run":
            index.update_schedule(row["id"], next_run=d.next_run)

    async def _collect(self, now: float) -> None:
        """Record the outcome of submitted occurrences whose runs have finished."""
        index = self.ws.index
        for occ in index.occurrences(status="submitted", limit=200):
            row = index.schedule(occ["schedule_id"])
            run = index.run_for_request(occ["request_id"])
            accepted = index.input(occ["input_id"]) if occ["input_id"] else None
            if run is None and accepted and accepted["run_id"]:
                run = index._one("select * from runs where id=?", accepted["run_id"])
            if row is None:
                continue
            if run is None:
                if accepted and accepted["status"] in ("dropped", "blocked", "failed"):
                    self._finish(row, occ, "failed", "任务输入未开始执行：" + accepted["status"])
                    await self.notify("nano-workbuddy", "失败：任务输入未开始执行", row["name"])
                continue
            if run["status"] in ("running", "interrupted"):
                continue
            outcome, summary = outcome_of(self.ws.open_session(row["session_id"]), run, self.ws.store)
            self._finish(row, occ, outcome, summary)
            if outcome not in QUIET:
                label = {"changed": "有新结果", "failed": "失败", "needs_user": "需要你处理",
                         "unknown": "结果待核验"}[outcome]
                await self.notify("nano-workbuddy", f"{label}：{summary}", row["name"])

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
        if row["session_id"] in self.runs.live or self.ws.index.pending_inputs(row["session_id"]):
            raise RuntimeError("the task's session is busy; try again when it has finished")
        now = time.time()
        rid = f"sched:{row['id']}:manual:{new_id('request')}"
        self.ws.index.accept_occurrence(row, now, 1, message(row, now, 1, now), rid, manual=True)
        self.runs.deliver_pending(row["session_id"])
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
        f"2. 先用 glob 检查上一期简报 {file} 是否存在；存在才用 read 读取并比较。"
        "成功检查后确认不存在，按首次运行准备简报；检查失败、无权限或读取失败不能当作不存在或无变化。\n"
        f"3. 有实质性的新信息：用 create_document 生成新一期 {file}（已有文件时 overwrite=true，作为新版本），"
        "开头写“本期变化”，每条事实后标注来源编号 [S#]，区分事实、推断和建议。\n"
        "4. 没有实质性的新信息：不要生成文件。\n"
        "5. 不要发送或发布任何内容到这台电脑以外。\n"
        "6. 最后调用 task_result：有新一期简报用 changed，没有新内容用 unchanged，做不了用 failed，"
        "缺搜索密钥或需要用户决定时用 needs_user，并用一两句话说明。"
        "evidence 引用本轮交付文件路径（changed），或本轮已成功检查的来源/旧报告路径（unchanged）；"
        "无变化时说明比较的材料和范围，不能把查询失败当作没有变化。"
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


def view(ws: Any, row: dict[str, Any], live: Any = None) -> dict[str, Any]:
    """A schedule as the task page shows it."""
    rule = rule_of(row)
    return {**{k: row[k] for k in ("id", "name", "prompt", "session_id", "kind", "at_time", "tz", "day", "catch_up",
                                   "next_run", "last_run_at", "last_outcome", "last_summary", "created_at")},
            "weekdays": [int(d) for d in row["weekdays"].split(",") if d], "enabled": bool(row["enabled"]),
            "template": json.loads(row["template_json"] or "{}"), "describe": rule.describe(),
            "next_local": local_time(row["next_run"], row["tz"]) if row["next_run"] else None,
            "running": live is not None or row["session_id"] in ws.running,
            "waiting": bool(live and (live.approvals or live.questions)),
            "pending": bool(ws.index.pending_inputs(row["session_id"])),
            "recent": ws.index.occurrences(row["id"], limit=8)}
