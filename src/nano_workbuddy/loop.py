"""The agent loop: derive the next step from the log, do it, append the outcome, repeat.

Normal runs and crash recovery are the same code path (D6): `next_step` looks only at the
current log path, so a half-finished run continues from whatever was durably written.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .compaction import fit
from .context import in_view, loaded_tools, pinned, spill, to_messages
from .llm import ContextOverflow, Delta, Model, Response
from .log import SessionLog
from .tools.registry import Registry, Result, Tool, ToolCtx, ToolError, execute, parse_args
from .types import (
    ASSISTANT,
    BRANCH_SUMMARY,
    COMPACTION,
    NOTICE,
    PRUNE,
    STATE,
    TOOL_RESULT,
    TOOL_STARTED,
    USER,
    Entry,
    Message,
    ToolCall,
    text,
)

TRUNCATED = ("Not executed: the response hit the output token limit, so the arguments may be incomplete. "
             "Re-issue the call with complete arguments.")
INCOMPLETE = ("Not executed: the response did not finish normally (the stream was cut off, failed or was "
              "refused), so this call may not be what was intended. Re-issue it if it is still needed.")
UNKNOWN = ("Execution was interrupted before its result was recorded; the outcome is unknown. "
           "Check the current state before deciding whether to retry.")
RECOVERED = "This call completed before an interruption; recovery verified its effect."
CANCELLED = "Cancelled by the user before completion."
SHUTDOWN = "shutdown"  # cancel message used when the server stops; such runs resume on next start
INTERRUPTED = "Interrupted by a server restart before completion."
WRAP_UP = ("[Step limit reached] Stop working now and do not call tools. Report briefly: what is done, "
           "which files were actually created or changed, and where to resume.")


@dataclass(frozen=True, slots=True)
class CallModel:
    pass


@dataclass(frozen=True, slots=True)
class RunTools:
    calls: list[ToolCall]


@dataclass(frozen=True, slots=True)
class FailCalls:
    calls: list[ToolCall]
    reason: str


@dataclass(frozen=True, slots=True)
class Done:
    pass


type Step = CallModel | RunTools | FailCalls | Done


# bookkeeping: never decides the next step
TRANSPARENT = (TOOL_STARTED, STATE, NOTICE, PRUNE, COMPACTION, BRANCH_SUMMARY)


def next_step(path: list[Entry]) -> Step:
    """Pure function of the log path; the whole loop hinges on it."""
    answered: set[str] = set()
    for e in reversed(path):
        if e.type == TOOL_RESULT:
            answered.add(e.data["call_id"])
        elif e.type in TRANSPARENT:
            continue
        elif e.type == USER:
            return CallModel()
        elif e.type == ASSISTANT:
            calls = e.tool_calls
            missing = [c for c in calls if c["id"] not in answered]
            if missing:  # only a normally finished response may cause side effects
                match e.data["stop"]:
                    case "tool_use" | "end":
                        return RunTools(missing)
                    case "length":
                        return FailCalls(missing, TRUNCATED)
                    case _:
                        return FailCalls(missing, INCOMPLETE)
            return CallModel() if calls else Done()
        else:
            return Done()
    return Done()


@dataclass(frozen=True, slots=True)
class Input:
    """A message from the user. id is its row in the index's inputs table (None when nothing persists it,
    as in the CLI); the user entry made from it carries the id, which is how recovery tells whether it
    already reached the log."""

    text: str
    id: str | None = None


@dataclass
class Inbox:
    """Messages the user sent while a run was active (D6 steer / queue)."""

    steer: list[Input] = field(default_factory=list)  # join this run before its next model call
    queue: list[Input] = field(default_factory=list)  # start their own run after this one ends

    def view(self) -> dict[str, list[str]]:
        return {"steer": [m.text for m in self.steer], "queue": [m.text for m in self.queue]}


def user_data(m: Input) -> dict[str, Any]:
    return {"content": [text(m.text)], **({"input_id": m.id} if m.id else {})}


type Gate = Callable[[Tool, ToolCall], Awaitable[Result | None]]  # None = go ahead, Result = blocked
type AfterTool = Callable[[Tool, ToolCall, Result], Awaitable[Result]]  # may add to a result before it is logged
type OnStop = Callable[[bool], Awaitable[str | None]]  # a message to keep going with, or None to stop


@dataclass
class Run:
    log: SessionLog
    model: Model
    tools: Registry
    ctx: ToolCtx
    run_id: str | None  # None for work outside a run, such as manual compaction
    emit: Callable[[dict[str, Any]], None] = lambda event: None  # ephemeral events; entries come from the log
    max_turns: int = 50
    usage: dict[str, int] = field(default_factory=dict)
    inbox: Inbox = field(default_factory=Inbox)
    gate: Gate | None = None  # permission check before any tool runs
    after_tool: AfterTool | None = None
    on_stop: OnStop | None = None  # asked when the model is done (Stop hooks); argument: asked before in this run
    notices: dict[str, str] = field(default_factory=dict)  # kind -> text the model must currently see

    def schemas(self) -> list[dict[str, Any]]:
        """Tools declared in the next request: the direct ones plus whatever tool_search loaded."""
        return self.tools.schemas(loaded_tools(self.log.path()))


async def drive(run: Run) -> str:
    """Run until the log says there is nothing left to do. Returns "ok" or "limit"."""
    turns, stop_asked = 0, False
    while True:
        match next_step(run.log.path()):
            case Done():
                if _take_steer(run):
                    continue
                if run.on_stop and (more := await run.on_stop(stop_asked)):
                    stop_asked = True
                    run.log.append(USER, {"content": [text(more)], "synthetic": True}, run_id=run.run_id)
                    continue
                return "ok"
            case CallModel():
                if _take_steer(run):
                    continue
                if turns >= run.max_turns:
                    await wrap_up(run)
                    return "limit"
                turns += 1
                await call_model(run)
            case RunTools(calls):
                await run_tools(run, calls)
            case FailCalls(calls, reason):
                for c in calls:
                    _finish(run, c, Result.error(reason))


def _take_steer(run: Run) -> bool:
    msgs, run.inbox.steer = run.inbox.steer, []
    for m in msgs:
        run.log.append(USER, user_data(m), run_id=run.run_id)
    if msgs:
        run.emit({"type": "inbox", **run.inbox.view()})
    return bool(msgs)


def sync_notices(run: Run) -> bool:
    """Append a notice whenever what the model can currently see differs from what it should see:
    run.notices (first run, preset changed, ...) and pinned content such as loaded skills. Either may
    have been summarised away by a compaction; then it is put back."""
    path = run.log.path()
    seen = in_view(path)
    changed = [(k, v) for k, v in (run.notices | pinned(path)).items() if seen.get(k) != v]
    for kind, body in changed:
        run.log.append(NOTICE, {"kind": kind, "text": body}, run_id=run.run_id)
    return bool(changed)


async def prepare(run: Run, overflow: bool = False) -> tuple[str, list[Message]]:
    sync_notices(run)
    system, messages = await fit(run, overflow)
    if sync_notices(run):  # a compaction may have folded a notice into the summary
        system, messages = to_messages(run.log.path())
    return system, messages


async def call_model(run: Run, tool_choice: str | None = None) -> Entry:
    system, messages = await prepare(run)
    partial: list[str] = []
    resp: Response | None = None
    try:
        for attempt in range(2):
            try:
                async for item in run.model.stream(system, messages, run.schemas(), tool_choice):
                    if isinstance(item, Delta):
                        if item.kind == "text":
                            partial.append(item.text)
                        run.emit({"type": "assistant.delta", "kind": item.kind, "text": item.text})
                    else:
                        resp = item
                break
            except ContextOverflow:  # our estimate was low: shrink once and retry
                if attempt:
                    raise
                run.emit({"type": "context.overflow"})
                system, messages = await prepare(run, overflow=True)
    except asyncio.CancelledError:
        if partial:  # keep what the user already saw; never keep half-streamed tool calls
            run.log.append(ASSISTANT, {"content": [text("".join(partial))], "stop": "aborted"}, run_id=run.run_id)
        raise
    assert resp is not None, "model stream ended without a Response"
    e = run.log.append(ASSISTANT, {"content": resp.content, "stop": resp.stop, "usage": resp.usage, **resp.meta},
                       run_id=run.run_id)
    for k, v in resp.usage.items():
        run.usage[k] = run.usage.get(k, 0) + v
    return e


def _this_turn(path: list[Entry], type: str) -> set[str]:
    """call_ids of `type` entries after the latest assistant message. Providers may reuse ids across
    turns (e.g. "call_0"), so looking further back would match calls from earlier turns."""
    ids: set[str] = set()
    for e in reversed(path):
        if e.type == ASSISTANT:
            break
        if e.type == type:
            ids.add(e.data["call_id"])
    return ids


async def run_tools(run: Run, calls: list[ToolCall]) -> None:
    started = _this_turn(run.log.path(), TOOL_STARTED)
    try:
        fresh = []
        for c in calls:
            if c["id"] in started and (r := _recover(run, c)):
                _finish(run, c, r)
            else:
                fresh.append(c)
        if len(fresh) > 1 and all((t := run.tools.get(c["name"])) and t.parallel for c in fresh):
            await asyncio.gather(*(_one(run, c) for c in fresh))
        else:
            for c in fresh:  # a batch with any writer runs in order: order is part of its meaning
                await _one(run, c)
    except asyncio.CancelledError as e:
        reason = INTERRUPTED if SHUTDOWN in e.args else CANCELLED
        answered = _this_turn(run.log.path(), TOOL_RESULT)
        for c in calls:
            if c["id"] not in answered:
                _finish(run, c, Result.error(reason))
        raise


async def _one(run: Run, c: ToolCall) -> None:
    t = run.tools.get(c["name"])
    if t is None:
        return _finish(run, c, Result.error(f"unknown tool {c['name']!r}"))
    if run.gate and (blocked := await run.gate(t, c)):  # asked before tool_started: nothing has happened yet
        return _finish(run, c, blocked)
    run.log.append(TOOL_STARTED, {"call_id": c["id"], "name": c["name"]}, run_id=run.run_id)
    r = await execute(t, c, replace(run.ctx, call_id=c["id"]))
    _finish(run, c, await run.after_tool(t, c, r) if run.after_tool else r)


def _finish(run: Run, c: ToolCall, r: Result) -> None:
    r = spill(r, run.ctx.spill_dir, c["id"])
    data = {"call_id": c["id"], "name": c["name"], "content": r.content, "is_error": r.is_error}
    run.log.append(TOOL_RESULT, data | ({"meta": r.meta} if r.meta else {}), run_id=run.run_id)


def _recover(run: Run, c: ToolCall) -> Result | None:
    """A call has a durable tool_started but no result. Returns a result, or None to run it again."""
    t = run.tools.get(c["name"])
    if t is None or t.effect == "read":
        return None
    if t.recover is None:
        return Result.error(UNKNOWN)  # e.g. bash: never replay side effects automatically
    try:
        verdict = t.recover(parse_args(t, c), run.ctx)
    except ToolError:
        return None
    return {"done": Result.ok(RECOVERED), "not_done": None, "unknown": Result.error(UNKNOWN)}[verdict]


async def wrap_up(run: Run) -> None:
    """One extra request without tools so a run cut off by the step limit still reports where it stands."""
    run.log.append(USER, {"content": [text(WRAP_UP)], "synthetic": True}, run_id=run.run_id)
    await call_model(run, tool_choice="none")
    close_pending(run.log, run.run_id, "Not executed: the step limit was reached.")


def close_pending(log: SessionLog, run_id: str | None, reason: str) -> None:
    """Answer any unanswered tool calls so the history stays valid before new input is appended."""
    step = next_step(log.path())
    if isinstance(step, RunTools | FailCalls):
        for c in step.calls:
            log.append(TOOL_RESULT, {"call_id": c["id"], "name": c["name"], "content": [text(f"Error: {reason}")],
                                     "is_error": True}, run_id=run_id)
