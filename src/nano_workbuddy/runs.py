"""Active runs for the server: at most one task per session, an inbox for messages sent while it runs,
approvals waiting for the user, and fan-out of events to every subscribed client.

Two kinds of events reach a client:
  durable   - "entry" / "checkout", published by the session log itself; replayable by seq
  ephemeral - deltas, tool output, run status, approvals; a reconnecting client gets their
              current value from snapshot()["live"] instead of a replay
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import policy
from .log import SessionLog, entry_json
from .loop import SHUTDOWN, Inbox
from .runtime import Ask, Emit, Workspace
from .tools import Result, Tool
from .types import ToolCall, new_id

log_ = logging.getLogger(__name__)

MAX_LIVE_OUTPUT = 20_000

type Factory = Callable[[Emit, Inbox, Ask], Awaitable[str | None]]


class Busy(Exception):
    """The request conflicts with the run that is active in this session."""


class Hub:
    """Fan-out of one session's events to its SSE subscribers."""

    def __init__(self) -> None:
        self.subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, event: dict[str, Any]) -> None:
        for q in self.subs:
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subs.discard(q)


@dataclass
class Live:
    """What a client needs about an active run that is not (yet) in the log."""

    inbox: Inbox
    task: asyncio.Task[None] | None = None
    run_id: str | None = None
    text: str = ""
    thinking: str = ""
    outputs: dict[str, str] = field(default_factory=dict)  # call_id -> streamed tool output so far
    approvals: dict[str, tuple[dict[str, Any], asyncio.Future[tuple[str, str | None]]]] = field(default_factory=dict)
    seen_requests: set[str] = field(default_factory=set)

    def view(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "text": self.text, "thinking": self.thinking, "outputs": self.outputs,
                "steer": self.inbox.steer, "queue": self.inbox.queue,
                "approvals": [v for v, _ in self.approvals.values()]}


class RunManager:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.hubs: dict[str, Hub] = {}
        self.live: dict[str, Live] = {}

    # events

    def hub(self, log: SessionLog) -> Hub:
        if log.id not in self.hubs:
            self.hubs[log.id] = Hub()
            log.listeners.append(lambda event: self._on_log(log.id, event))
        return self.hubs[log.id]

    def _on_log(self, sid: str, event: dict[str, Any]) -> None:
        if (live := self.live.get(sid)) and event["type"] == "entry":
            entry = event["entry"]
            if entry["type"] == "assistant":  # the streamed text is now durable
                live.text = live.thinking = ""
            elif entry["type"] == "tool_result":
                live.outputs.pop(entry["data"]["call_id"], None)
        self.hubs[sid].publish(event)

    def _emitter(self, sid: str, live: Live) -> Emit:
        def emit(event: dict[str, Any]) -> None:
            match event["type"]:
                case "assistant.delta" if event["kind"] == "text":
                    live.text += event["text"]
                case "assistant.delta":
                    live.thinking += event["text"]
                case "tool.output":
                    cid = event["call_id"]
                    live.outputs[cid] = (live.outputs.get(cid, "") + event["text"])[-MAX_LIVE_OUTPUT:]
                case "run.started":
                    live.run_id = event["run_id"]
            self.hubs[sid].publish(event)

        return emit

    def snapshot(self, log: SessionLog) -> dict[str, Any]:
        """Everything a client needs to render a session; then it subscribes with after=seq."""
        self.hub(log)
        live = self.live.get(log.id)
        return {"session": self.ws.index.session(log.id), "seq": log.lines - 1, "leaf_id": log.leaf_id,
                "entries": [entry_json(e) for e in sorted(log.entries.values(), key=lambda e: e.seq)],
                "live": live.view() if live else None}

    # runs

    def send(self, log: SessionLog, content: str, *, request_id: str | None = None,
             parent_id: str | None = None, delivery: str = "auto") -> str:
        """Start a run, or hand the message to the active one. Returns how it was delivered."""
        if live := self.live.get(log.id):
            if parent_id is not None:
                raise Busy("cannot branch from an earlier message while a run is active")
            if request_id and request_id in live.seen_requests:
                return "duplicate"
            if request_id:
                live.seen_requests.add(request_id)
            kind = "queue" if delivery == "queue" else "steer"
            getattr(live.inbox, kind).append(content)
            self._publish_inbox(log.id, live.inbox)
            return kind

        def factory(emit: Emit, inbox: Inbox, ask: Ask) -> Awaitable[str]:
            return self.ws.submit(log, content, request_id=request_id, parent_id=parent_id, emit=emit,
                                  inbox=inbox, ask=ask)

        self._start(log, factory)
        return "run"

    def compact(self, log: SessionLog, instructions: str | None = None) -> None:
        if log.id in self.live:
            raise Busy("cannot compact while a run is active")
        self._start(log, lambda emit, inbox, ask: self.ws.compact(log, instructions, emit))

    def switch_branch(self, log: SessionLog, target_id: str) -> None:
        if log.id in self.live:
            raise Busy("cannot switch branches while a run is active")
        self._start(log, lambda emit, inbox, ask: self.ws.switch_branch(log, target_id, emit))

    def cancel(self, session_id: str) -> None:
        if (live := self.live.get(session_id)) is None or live.task is None:
            raise KeyError(f"no active run in {session_id}")
        live.task.cancel()

    def resume_all(self) -> None:
        """At server start: continue every run a crash or shutdown left unfinished (D6)."""
        self.ws.index.expire_pending_approvals()
        for sid in dict.fromkeys(r["session_id"] for r in self.ws.index.unfinished_runs()):
            log = self.ws.open_session(sid)
            self._start(log, lambda emit, inbox, ask, log=log: self.ws.resume(log, emit, inbox, ask))

    async def shutdown(self) -> None:
        """Stop runs so that they resume on the next start instead of counting as cancelled."""
        tasks = [live.task for live in self.live.values() if live.task]
        for t in tasks:
            t.cancel(SHUTDOWN)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start(self, log: SessionLog, factory: Factory, inbox: Inbox | None = None) -> None:
        live = self.live[log.id] = Live(inbox=inbox or Inbox())
        self.hub(log)
        live.task = asyncio.create_task(self._run(log, live, factory))

    async def _run(self, log: SessionLog, live: Live, factory: Factory) -> None:
        status = "error"
        try:
            status = await factory(self._emitter(log.id, live), live.inbox, self._asker(log, live)) or "ok"
        except asyncio.CancelledError as e:
            if SHUTDOWN in e.args:
                raise
            status = "cancelled"
        except Exception as e:  # the run is already marked error in the index; tell the client why
            log_.exception("run failed in session %s", log.id)
            self.hubs[log.id].publish({"type": "run.error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if self.live.get(log.id) is live:
                del self.live[log.id]
        self.hubs[log.id].publish({"type": "session", "session": self.ws.index.session(log.id),
                                   "context": self.ws.context_usage(log)})  # title, leaf, context size
        leftovers = live.inbox.steer + live.inbox.queue
        if leftovers and status in ("ok", "limit"):  # messages that arrived as the run was ending
            first, rest = leftovers[0], leftovers[1:]
            self.send(log, first)
            self.live[log.id].inbox.queue.extend(rest)
            self._publish_inbox(log.id, self.live[log.id].inbox)
        elif leftovers:  # cancelled or failed: give the text back to the composer
            self.hubs[log.id].publish({"type": "inbox", "steer": [], "queue": [], "dropped": leftovers})

    def _publish_inbox(self, sid: str, inbox: Inbox) -> None:
        self.hubs[sid].publish({"type": "inbox", "steer": inbox.steer, "queue": inbox.queue})

    # approvals

    def _asker(self, log: SessionLog, live: Live) -> Ask:
        hub = self.hubs[log.id]

        async def ask(check: policy.Check, tool: Tool, call: ToolCall) -> Result | None:
            aid = new_id("a")
            always = check.suggestion()
            view = {"id": aid, "call_id": call["id"], "tool": tool.name, "args": call["args"], "action": check.action,
                    "resource": check.resource, "always": {"action": always.action, "resource": always.resource}}
            fut: asyncio.Future[tuple[str, str | None]] = asyncio.get_running_loop().create_future()
            live.approvals[aid] = (view, fut)
            self.ws.index.add_approval(aid, log.id, live.run_id, call)
            hub.publish({"type": "approval.requested", "approval": view})
            try:
                decision, feedback = await fut
            except asyncio.CancelledError:
                self.ws.index.resolve_approval(aid, "expired")
                hub.publish({"type": "approval.resolved", "id": aid, "decision": "expired"})
                raise
            finally:
                live.approvals.pop(aid, None)
            if decision == "always":
                self.ws.index.add_rule(log.id, always)
            if decision == "reject":
                return Result.error("The user denied this call." + (f" Their feedback: {feedback}" if feedback else ""))
            return None

        return ask

    def resolve(self, approval_id: str, decision: str, feedback: str | None = None) -> None:
        for sid, live in self.live.items():
            if approval_id not in live.approvals:
                continue
            _, fut = live.approvals[approval_id]
            self.ws.index.resolve_approval(approval_id, "denied" if decision == "reject" else "allowed", feedback)
            fut.set_result((decision, feedback))
            self.hubs[sid].publish({"type": "approval.resolved", "id": approval_id, "decision": decision})
            return
        raise KeyError(f"no pending approval {approval_id}")
