"""The agent loop: derive the next step from the log, do it, append the outcome, repeat.

Normal runs and crash recovery are the same code path (D6): `next_step` looks only at the
current log path, so a half-finished run continues from whatever was durably written.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .context import spill, to_messages
from .llm import Delta, Model, Response
from .log import SessionLog
from .tools.registry import Registry, Result, ToolCtx, ToolError, execute, parse_args
from .types import ASSISTANT, STATE, TOOL_RESULT, TOOL_STARTED, USER, Entry, ToolCall, text

TRUNCATED = ("Not executed: the response hit the output token limit, so the arguments may be incomplete. "
             "Re-issue the call with complete arguments.")
UNKNOWN = ("Execution was interrupted before its result was recorded; the outcome is unknown. "
           "Check the current state before deciding whether to retry.")
RECOVERED = "This call completed before an interruption; recovery verified its effect."
CANCELLED = "Cancelled by the user before completion."
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


def next_step(path: list[Entry]) -> Step:
    """Pure function of the log path; the whole loop hinges on it."""
    answered: set[str] = set()
    for e in reversed(path):
        if e.type == TOOL_RESULT:
            answered.add(e.data["call_id"])
        elif e.type in (TOOL_STARTED, STATE):
            continue
        elif e.type == USER:
            return CallModel()
        elif e.type == ASSISTANT:
            calls = e.tool_calls
            missing = [c for c in calls if c["id"] not in answered]
            if missing:
                return FailCalls(missing, TRUNCATED) if e.data["stop"] == "length" else RunTools(missing)
            return CallModel() if calls else Done()
        else:
            return Done()
    return Done()


@dataclass
class Run:
    log: SessionLog
    model: Model
    tools: Registry
    ctx: ToolCtx
    run_id: str
    emit: Callable[[dict[str, Any]], None] = lambda event: None
    max_turns: int = 50
    usage: dict[str, int] = field(default_factory=dict)


async def drive(run: Run) -> str:
    """Run until the log says there is nothing left to do. Returns "ok" or "limit"."""
    turns = 0
    while True:
        match next_step(run.log.path()):
            case Done():
                return "ok"
            case CallModel():
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


async def call_model(run: Run, tool_choice: str | None = None) -> Entry:
    system, messages = to_messages(run.log.path())
    partial: list[str] = []
    resp: Response | None = None
    try:
        async for item in run.model.stream(system, messages, run.tools.schemas(), tool_choice):
            if isinstance(item, Delta):
                if item.kind == "text":
                    partial.append(item.text)
                run.emit({"type": "assistant.delta", "kind": item.kind, "text": item.text})
            else:
                resp = item
    except asyncio.CancelledError:
        if partial:  # keep what the user already saw; never keep half-streamed tool calls
            run.log.append(ASSISTANT, {"content": [text("".join(partial))], "stop": "aborted"}, run_id=run.run_id)
        raise
    assert resp is not None, "model stream ended without a Response"
    e = run.log.append(ASSISTANT, {"content": resp.content, "stop": resp.stop, "usage": resp.usage, **resp.meta},
                       run_id=run.run_id)
    for k, v in resp.usage.items():
        run.usage[k] = run.usage.get(k, 0) + v
    run.emit({"type": "assistant.message", "entry": e.to_json()})
    return e


async def run_tools(run: Run, calls: list[ToolCall]) -> None:
    started = {e.data["call_id"] for e in run.log.path() if e.type == TOOL_STARTED}
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
    except asyncio.CancelledError:
        answered = {e.data["call_id"] for e in run.log.path() if e.type == TOOL_RESULT}
        for c in calls:
            if c["id"] not in answered:
                _finish(run, c, Result.error(CANCELLED))
        raise


async def _one(run: Run, c: ToolCall) -> None:
    run.log.append(TOOL_STARTED, {"call_id": c["id"], "name": c["name"]}, run_id=run.run_id)
    run.emit({"type": "tool.started", "call_id": c["id"], "name": c["name"], "args": c["args"]})
    t = run.tools.get(c["name"])
    r = await execute(t, c, replace(run.ctx, call_id=c["id"])) if t else Result.error(f"unknown tool {c['name']!r}")
    _finish(run, c, r)


def _finish(run: Run, c: ToolCall, r: Result) -> None:
    r = spill(r, run.ctx.spill_dir, c["id"])
    e = run.log.append(TOOL_RESULT, {"call_id": c["id"], "name": c["name"], "content": r.content,
                                     "is_error": r.is_error}, run_id=run.run_id)
    run.emit({"type": "tool.result", "entry": e.to_json()})


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
