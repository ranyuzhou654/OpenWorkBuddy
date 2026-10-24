"""Active runs for the server: at most one task per session, an inbox for messages sent while it runs,
approvals waiting for the user, and fan-out of events to every subscribed client.

Every message is made durable (index inputs table) before the client is told how it was delivered, and
stays "pending" until the user entry made from it is in the log. After a crash or restart, resume_all
hands pending messages to the resumed run, or starts a run for them: an acknowledged message is never lost.

Two kinds of events reach a client:
  durable   - "entry" / "checkout", published by the session log itself; replayable by seq
  ephemeral - deltas, tool output, run status, approvals; a reconnecting client gets their
              current value from snapshot()["live"] instead of a replay
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import policy
from .log import SessionLog, entry_json
from .loop import SHUTDOWN, Inbox, Input
from .runtime import Ask, Emit, Question, Workspace
from .tools import Result, Tool
from .types import ToolCall, new_id

log_ = logging.getLogger(__name__)

MAX_LIVE_OUTPUT = 20_000
MAX_PENDING_EVENTS = 5_000  # per subscriber; a client this far behind is cut off and reconnects from a snapshot

type Factory = Callable[[Emit, Inbox, Ask, Question], Awaitable[str | None]]


def _extra(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row.get("extra_json") or "{}")


def _input(row: dict[str, Any]) -> Input:
    """A pending input as it was accepted, attachments included."""
    extra = _extra(row)
    return Input(row["content"], row["id"], tuple(extra.get("attachments", ())), tuple(extra.get("blocks", ())))


class Busy(Exception):
    """The request conflicts with the run that is active in this session."""


class Hub:
    """Fan-out of one session's events to its SSE subscribers. Each subscriber's queue is bounded: one that
    falls too far behind (a stalled tab, a slow link) is not allowed to grow memory without limit. It is
    cut off instead — its queue is emptied and ends with None, which closes its stream — and the client
    reconnects with a fresh snapshot, which is always consistent. Events are never silently skipped."""

    def __init__(self, limit: int = MAX_PENDING_EVENTS) -> None:
        self.subs: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.limit = limit

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.subs.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(self.limit)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
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
    questions: dict[str, tuple[dict[str, Any], asyncio.Future[str]]] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "text": self.text, "thinking": self.thinking, "outputs": self.outputs,
                **self.inbox.view(),
                "approvals": [v for v, _ in self.approvals.values()],
                "questions": [v for v, _ in self.questions.values()]}


class RunManager:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.hubs: dict[str, Hub] = {}
        self.live: dict[str, Live] = {}
        # told when a run starts waiting for the user (kind "approval" or "question"); the scheduler uses it
        self.attention: Callable[[SessionLog, str, str], None] | None = None

    # events

    def hub(self, log: SessionLog) -> Hub:
        if log.id not in self.hubs:
            self.hubs[log.id] = Hub()
            log.listeners.append(lambda event: self._on_log(log.id, event))
        return self.hubs[log.id]

    def _on_log(self, sid: str, event: dict[str, Any]) -> None:
        if event["type"] == "entry" and event["entry"]["type"] == "user" and (iid := event["entry"]["data"].get(
                "input_id")):
            self.ws.index.finish_input(iid, "consumed")  # the message is in the log now: it can no longer be lost
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

    def live_view(self, sid: str) -> dict[str, Any] | None:
        return live.view() if (live := self.live.get(sid)) else None

    def snapshot(self, log: SessionLog) -> dict[str, Any]:
        """Everything a client needs to render a session; then it subscribes with after=seq."""
        self.hub(log)
        live = self.live.get(log.id)
        return {"session": self.ws.index.session(log.id), "seq": log.lines - 1, "leaf_id": log.leaf_id,
                "entries": [entry_json(e) for e in sorted(log.entries.values(), key=lambda e: e.seq)],
                "live": live.view() if live else None}

    # runs

    def send(self, log: SessionLog, content: str, *, request_id: str | None = None,
             parent_id: str | None = None, delivery: str = "auto", attachments: list[str] = ()) -> str:
        """Start a run, or hand the message to the active one. Returns how it was delivered: "run", "steer",
        "queue", or "duplicate" for a retried request_id. Raises Busy for a request_id reused with another
        message, or a branch edit while a run is active."""
        live = self.live.get(log.id)
        if live and parent_id is not None:
            raise Busy("cannot branch from an earlier message while a run is active")
        files, blocks = self.ws.attachments(log, attachments)  # KeyError for an unknown attachment
        if request_id:
            claim = self.ws.index.claim_request(request_id, log.id, f"{parent_id}\0{content}\0{','.join(attachments)}")
            if claim == "duplicate":
                return "duplicate"
            if claim == "conflict":
                raise Busy(f"request_id {request_id} was already used for a different message")
        kind = ("queue" if delivery == "queue" else "steer") if live else "run"
        extra = {**({"attachments": files, "blocks": blocks} if files else {}),
                 **({"request_id": request_id} if request_id else {})} or None
        msg = Input(content, self.ws.index.add_input(log.id, kind, content, parent_id, extra),  # durable before the ack
                    tuple(files), tuple(blocks))
        if live:
            getattr(live.inbox, kind).append(msg)
            self._publish_inbox(log.id, live.inbox)
        else:
            self._submit(log, msg, request_id=request_id, parent_id=parent_id)
        return kind

    def _submit(self, log: SessionLog, msg: Input, *, request_id: str | None = None,
                parent_id: str | None = None, inbox: Inbox | None = None) -> None:
        def factory(emit: Emit, inbox: Inbox, ask: Ask, question: Question) -> Awaitable[str]:
            return self.ws.submit(log, msg, request_id=request_id, parent_id=parent_id, emit=emit,
                                  inbox=inbox, ask=ask, question=question)

        self._start(log, factory, inbox)

    def compact(self, log: SessionLog, instructions: str | None = None) -> None:
        if log.id in self.live:
            raise Busy("cannot compact while a run is active")
        self._start(log, lambda emit, inbox, ask, question: self.ws.compact(log, instructions, emit))

    def switch_branch(self, log: SessionLog, target_id: str) -> None:
        if log.id in self.live:
            raise Busy("cannot switch branches while a run is active")
        self._start(log, lambda emit, inbox, ask, question: self.ws.switch_branch(log, target_id, emit))

    def cancel(self, session_id: str) -> None:
        if (live := self.live.get(session_id)) is None or live.task is None:
            raise KeyError(f"no active run in {session_id}")
        live.task.cancel()

    def resume_all(self) -> None:
        """At server start: continue every run a crash or shutdown left unfinished (D6), together with the
        messages that were accepted but had not reached the log yet; start a run for such messages in a
        session that has nothing to resume."""
        index = self.ws.index
        index.expire_pending_approvals()
        unfinished = [r["session_id"] for r in index.unfinished_runs()]
        pending: dict[str, list[dict[str, Any]]] = {}
        for row in index.pending_inputs():
            pending.setdefault(row["session_id"], []).append(row)
        for sid in dict.fromkeys([*unfinished, *pending]):
            try:
                log = self.ws.open_session(sid)
            except KeyError:
                continue
            rows = self._not_in_log(log, pending.get(sid, []))
            msgs = [_input(r) for r in rows]
            if sid in unfinished:
                inbox = Inbox(steer=[m for m, r in zip(msgs, rows, strict=True) if r["delivery"] != "queue"],
                              queue=[m for m, r in zip(msgs, rows, strict=True) if r["delivery"] == "queue"])
                self._start(log, lambda emit, inbox, ask, question, log=log: self.ws.resume(log, emit, inbox, ask,
                                                                                            question), inbox)
            elif msgs:  # its request_id travels along: a scheduled task finds its run by it
                self._submit(log, msgs[0], parent_id=rows[0]["parent_id"], inbox=Inbox(queue=msgs[1:]),
                             request_id=_extra(rows[0]).get("request_id"))

    def _not_in_log(self, log: SessionLog, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pending inputs minus those whose user entry was written just before a crash (marked consumed now)."""
        logged = {e.data.get("input_id") for e in log.entries.values() if e.type == "user"}
        for r in rows:
            if r["id"] in logged:
                self.ws.index.finish_input(r["id"], "consumed")
        return [r for r in rows if r["id"] not in logged]

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
            status = await factory(self._emitter(log.id, live), live.inbox, self._asker(log, live),
                                   self._questioner(log, live)) or "ok"
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
        if status == "busy":  # another process holds the session: inputs stay pending for the next start
            pass
        elif leftovers and status in ("ok", "limit"):  # messages that arrived as the run was ending
            self._submit(log, leftovers[0], inbox=Inbox(queue=leftovers[1:]))
            self._publish_inbox(log.id, self.live[log.id].inbox)
        elif leftovers:  # cancelled or failed: give the text back to the composer
            for m in leftovers:
                if m.id:
                    self.ws.index.finish_input(m.id, "dropped")
            self.hubs[log.id].publish({"type": "inbox", "steer": [], "queue": [],
                                       "dropped": [m.text for m in leftovers]})

    def _publish_inbox(self, sid: str, inbox: Inbox) -> None:
        self.hubs[sid].publish({"type": "inbox", **inbox.view()})

    # approvals

    def _asker(self, log: SessionLog, live: Live) -> Ask:
        hub = self.hubs[log.id]

        async def ask(check: policy.Check, tool: Tool, call: ToolCall) -> Result | None:
            aid = new_id("a")
            always = check.suggestion()
            view = {"id": aid, "call_id": call["id"], "tool": tool.name, "args": call["args"], "action": check.action,
                    "resource": check.resource, "preview": check.preview,
                    "always": None if check.once_only else {"action": always.action, "resource": always.resource}}
            fut: asyncio.Future[tuple[str, str | None]] = asyncio.get_running_loop().create_future()
            live.approvals[aid] = (view, fut)
            self.ws.index.add_approval(aid, log.id, live.run_id, call)
            hub.publish({"type": "approval.requested", "approval": view})
            if self.attention:
                self.attention(log, "approval", f"{tool.name} {check.resource}"[:120])
            try:
                decision, feedback = await fut
            except asyncio.CancelledError:
                self.ws.index.resolve_approval(aid, "expired")
                hub.publish({"type": "approval.resolved", "id": aid, "decision": "expired"})
                raise
            finally:
                live.approvals.pop(aid, None)
            if decision == "always" and not check.once_only:
                self.ws.index.add_rule(log.id, always)
            if decision == "reject":
                return Result.error("The user denied this call." + (f" Their feedback: {feedback}" if feedback else ""))
            return None

        return ask

    # questions (ask_user)

    def _questioner(self, log: SessionLog, live: Live) -> Question:
        hub = self.hubs[log.id]

        async def question(q: dict[str, Any]) -> str:
            view = {"id": new_id("q"), **q}
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            live.questions[view["id"]] = (view, fut)
            hub.publish({"type": "question.requested", "question": view})
            if self.attention:
                self.attention(log, "question", str(q.get("question", ""))[:120])
            try:
                return await fut
            finally:
                live.questions.pop(view["id"], None)
                hub.publish({"type": "question.resolved", "id": view["id"]})

        return question

    def answer(self, question_id: str, text: str) -> None:
        for live in self.live.values():
            if question_id in live.questions:
                if not (fut := live.questions[question_id][1]).done():
                    fut.set_result(text)
                return
        raise KeyError(f"no pending question {question_id}")

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
