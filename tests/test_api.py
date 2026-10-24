"""The HTTP + SSE surface, against a real uvicorn server on a random port."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn
from conftest import FakeModel, call, reply

from nano_workbuddy.api import create_app
from nano_workbuddy.config import load
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import TOOL_STARTED, USER

TOKEN = "test-token"


class Server:
    def __init__(self, ws: Workspace):
        self.ws = ws
        app = create_app(ws, TOKEN)
        self.runs = app.state.runs
        self.srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))

    async def __aenter__(self) -> Server:
        self.task = asyncio.create_task(self.srv.serve())
        while not self.srv.started:
            await asyncio.sleep(0.01)
        self.base = f"http://127.0.0.1:{self.srv.servers[0].sockets[0].getsockname()[1]}"
        self.client = httpx.AsyncClient(base_url=self.base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()
        self.srv.should_exit = True
        await self.task

    async def session(self, project) -> str:
        p = (await self.client.post("/api/projects", json={"path": str(project), "trusted": True})).json()
        return (await self.client.post(f"/api/projects/{p['id']}/sessions", json={})).json()["id"]


class Stream:
    """Collects SSE events in the background."""

    def __init__(self, client: httpx.AsyncClient, sid: str, after: int = -1):
        self.events: list[dict] = []
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self._run(client, sid, after))

    async def _run(self, client, sid, after):
        async with client.stream("GET", f"/api/sessions/{sid}/events", params={"after": after}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    self.events.append(json.loads(line[6:]))
                    if self.events[-1]["type"] == "leaf":
                        self.ready.set()

    async def until(self, pred, timeout: float = 5) -> dict:
        async def wait():
            while True:
                for e in self.events:
                    if pred(e):
                        return e
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(wait(), timeout)

    def close(self) -> None:
        self.task.cancel()


def finished(e):
    return e["type"] == "run.finished"


def entry_of(type_):
    return lambda e: e["type"] == "entry" and e["entry"]["type"] == type_


@pytest.fixture
def ws_for(home):
    return lambda model: Workspace(load(home), connect=model.connect)


async def test_auth_and_origin_checks(ws_for):
    async with Server(ws_for(FakeModel())) as s:
        anon = httpx.AsyncClient(base_url=s.base)
        assert (await anon.get("/api/projects")).status_code == 401
        assert (await anon.get("/api/login", params={"token": "nope"})).status_code == 401
        r = await anon.get("/api/login", params={"token": TOKEN})
        assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
        assert (await anon.get("/api/projects")).status_code == 200  # cookie now set
        evil = await anon.post("/api/projects", json={"path": "/"}, headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        await anon.aclose()


async def test_run_streams_entries_and_deltas(ws_for, project):
    model = FakeModel(reply("writing", call("write", path="a.txt", content="hi")), reply("all done"))
    async with Server(ws_for(model)) as s:
        sid = await s.session(project)
        await s.client.patch(f"/api/sessions/{sid}", json={"preset": "trust"})
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        assert (await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})).json() == {
            "delivery": "run"}
        done = await stream.until(finished)
        stream.close()
        assert done["status"] == "ok"
        kinds = [e["entry"]["type"] for e in stream.events if e["type"] == "entry"]
        assert kinds == ["system_prompt", "user", "notice", "assistant", "tool_started", "tool_result", "assistant"]
        assert any(e["type"] == "assistant.delta" and e["text"] == "all done" for e in stream.events)
        assert (project / "a.txt").read_text() == "hi"


async def test_reconnect_replays_only_newer_entries(ws_for, project):
    async with Server(ws_for(FakeModel(reply("one"), reply("two")))) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "first"})
        await stream.until(finished)
        stream.close()
        snap = (await s.client.get(f"/api/sessions/{sid}")).json()
        assert snap["live"] is None and [e["type"] for e in snap["entries"]] == [
            "system_prompt", "user", "notice", "assistant"]
        assert 0 < snap["context"]["tokens"] < snap["context"]["budget"]

        later = Stream(s.client, sid, after=snap["seq"])  # a client that saw everything so far
        await later.ready.wait()
        assert later.events == [{"type": "live", "live": None}, {"type": "leaf", "leaf_id": snap["leaf_id"]}]
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "second"})
        await later.until(finished)
        later.close()
        texts = [e["entry"]["data"]["content"][0]["text"] for e in later.events
                 if e["type"] == "entry" and e["entry"]["type"] in ("user", "assistant")]
        assert texts == ["second", "two"]


async def test_approval_once_reject_and_always(ws_for, project, tmp_path):
    out = tmp_path / "outside"  # writes outside the project need approval in the workspace preset
    out.mkdir()
    model = FakeModel(
        reply(None, call("write", path=str(out / "a.txt"), content="a")),
        reply(None, call("write", path=str(out / "b.txt"), content="b")),
        reply(None, call("write", path=str(out / "c.txt"), content="c")),
        reply(None, call("write", path=str(out / "d.txt"), content="d")),
        reply("finished"),
    )
    async with Server(ws_for(model)) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})

        decisions = [("once", None), ("reject", "use a different folder"), ("always", None)]
        for i, (decision, feedback) in enumerate(decisions):
            await stream.until(lambda e, i=i: sum(x["type"] == "approval.requested" for x in stream.events) > i)
            req = [x for x in stream.events if x["type"] == "approval.requested"][i]["approval"]
            assert req["action"] == "write" and req["always"]["resource"].endswith("/outside/*")
            snap = (await s.client.get(f"/api/sessions/{sid}")).json()
            assert [a["id"] for a in snap["live"]["approvals"]] == [req["id"]]
            r = await s.client.post(f"/api/approvals/{req['id']}", json={"decision": decision, "feedback": feedback})
            assert r.status_code == 200
        await stream.until(finished)
        stream.close()

        assert sorted(f.name for f in out.iterdir()) == ["a.txt", "c.txt", "d.txt"]  # d: covered by "always"
        assert sum(e["type"] == "approval.requested" for e in stream.events) == 3
        rejected = model.requests[2]["messages"][-1]
        assert "use a different folder" in rejected["content"][0]["text"]
        rules = s.ws.index.session_rules(sid)
        assert [(r.action, r.effect) for r in rules] == [("write", "allow")]


async def test_steer_queue_and_cancel(ws_for, project):
    gate = asyncio.Event()

    async def held(msgs):
        await gate.wait()
        return reply("answer 1")

    model = FakeModel(held, reply("answer 2"), reply("answer 3"))
    async with Server(ws_for(model)) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        post = lambda body: s.client.post(f"/api/sessions/{sid}/messages", json=body)  # noqa: E731
        assert (await post({"text": "q1"})).json()["delivery"] == "run"
        assert (await post({"text": "steered", "request_id": "x"})).json()["delivery"] == "steer"
        assert (await post({"text": "steered", "request_id": "x"})).json()["delivery"] == "duplicate"
        assert (await post({"text": "queued", "delivery": "queue"})).json()["delivery"] == "queue"
        assert (await post({"text": "edit", "parent_id": "e_x"})).status_code == 400
        gate.set()
        await stream.until(lambda e: finished(e) and sum(x["type"] == "run.finished" for x in stream.events) == 2)
        stream.close()
        log = s.ws.open_session(sid)
        users = [e.data["content"][0]["text"] for e in log.path() if e.type == USER]
        assert users == ["q1", "steered", "queued"]  # steer joined run 1; queue became run 2


async def test_cancel_endpoint_and_branch_edit(ws_for, project):
    model = FakeModel(reply("A1"), reply(None, call("bash", command="sleep 30")), reply("B1"))
    async with Server(ws_for(model)) as s:
        sid = await s.session(project)
        await s.client.patch(f"/api/sessions/{sid}", json={"preset": "trust"})
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "first"})
        await stream.until(finished)
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "second"})
        await stream.until(entry_of(TOOL_STARTED))
        assert (await s.client.post(f"/api/sessions/{sid}/checkout", json={"entry_id": "x"})).status_code == 409
        assert (await s.client.post(f"/api/sessions/{sid}/cancel")).status_code == 200
        done = await stream.until(lambda e: finished(e) and e["status"] == "cancelled")
        assert done

        snap = (await s.client.get(f"/api/sessions/{sid}")).json()
        first = next(e for e in snap["entries"] if e["type"] == "user")
        edit = {"text": "first v2", "parent_id": first["parent_id"]}
        r = await s.client.post(f"/api/sessions/{sid}/messages", json=edit)
        assert r.json()["delivery"] == "run"
        await stream.until(lambda e: finished(e) and sum(x["type"] == "run.finished" for x in stream.events) == 3)
        log = s.ws.open_session(sid)
        assert [e.data["content"][0]["text"] for e in log.path() if e.type == USER] == ["first v2"]

        # switch back to the original branch: descend lands on its latest entry
        r = await s.client.post(f"/api/sessions/{sid}/checkout", json={"entry_id": first["id"], "descend": True})
        assert log.entries[r.json()["leaf_id"]].data.get("content", [{}])[0].get("text", "").startswith("Error")
        await stream.until(lambda e: e["type"] == "checkout")
        stream.close()


async def test_server_restart_resumes_interrupted_runs(ws_for, project, home):
    gate = asyncio.Event()

    async def never(msgs):
        await gate.wait()
        return reply("unreachable")

    async with Server(ws_for(FakeModel(never))) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "long job"})
        await stream.until(lambda e: e["type"] == "run.started")
        stream.close()
    assert s.ws.index.unfinished_runs(sid)[0]["status"] == "interrupted"

    async with Server(ws_for(FakeModel(reply("resumed answer")))) as s2:  # resumes during startup
        for _ in range(100):
            if not s2.ws.index.unfinished_runs(sid):
                break
            await asyncio.sleep(0.05)
        assert s2.ws.open_session(sid).path()[-1].data["content"][0]["text"] == "resumed answer"
        assert s2.ws.index.unfinished_runs(sid) == []


async def test_request_ids_are_claimed_before_the_run_starts(ws_for, project):
    """An immediate retry of the first message is a duplicate, not a steer; reusing an id for another
    message is refused (F01)."""
    gate = asyncio.Event()

    async def held(msgs):
        await gate.wait()
        return reply("done")

    async with Server(ws_for(FakeModel(held))) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        post = lambda body: s.client.post(f"/api/sessions/{sid}/messages", json=body)  # noqa: E731
        assert (await post({"text": "original", "request_id": "r1"})).json()["delivery"] == "run"
        assert (await post({"text": "original", "request_id": "r1"})).json()["delivery"] == "duplicate"
        assert (await post({"text": "changed", "request_id": "r1"})).status_code == 409
        gate.set()
        await stream.until(finished)
        stream.close()
        log = s.ws.open_session(sid)
        assert [e.data["content"][0]["text"] for e in log.path() if e.type == USER] == ["original"]
        assert s.ws.index.pending_inputs(sid) == []


async def test_queued_message_survives_a_restart(ws_for, project):
    """A message accepted while a run is active is durable: after a restart it still runs (F02)."""
    gate = asyncio.Event()

    async def never(msgs):
        await gate.wait()
        return reply("unreachable")

    async with Server(ws_for(FakeModel(never))) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "long job"})
        await stream.until(lambda e: e["type"] == "run.started")
        r = await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "queued-marker", "delivery": "queue"})
        assert r.json()["delivery"] == "queue"
        stream.close()
    assert [r["content"] for r in s.ws.index.pending_inputs(sid)] == ["queued-marker"]

    async with Server(ws_for(FakeModel(reply("resumed"), reply("queued answer")))) as s2:
        for _ in range(100):
            idle = sid not in s2.runs.live
            if idle and not s2.ws.index.pending_inputs(sid) and not s2.ws.index.unfinished_runs(sid):
                break
            await asyncio.sleep(0.05)
        log = s2.ws.open_session(sid)
        users = [e.data["content"][0]["text"] for e in log.path() if e.type == USER]
        assert users == ["long job", "queued-marker"] and log.path()[-1].data["content"][0]["text"] == "queued answer"


async def test_accepted_input_without_a_run_is_delivered_once(ws_for, project, home):
    """A crash right after accepting a message (before its run started) still delivers it at startup, and a
    message whose user entry already reached the log is not delivered twice (F02)."""
    ws = ws_for(FakeModel())
    log = ws.new_session(project)
    lost = ws.index.add_input(log.id, "run", "accepted then crashed")
    done = ws.index.add_input(log.id, "run", "already logged")
    log.append(USER, {"content": [{"type": "text", "text": "already logged"}], "input_id": done})
    log.append("assistant", {"content": [{"type": "text", "text": "ok"}], "stop": "end"})
    log.close()
    ws.index.db.close()

    async with Server(ws_for(FakeModel(reply("delivered")))) as s:
        for _ in range(100):
            if not s.ws.index.pending_inputs(log.id) and log.id not in s.runs.live:
                break
            await asyncio.sleep(0.05)
        path = s.ws.open_session(log.id).path()
        users = [e.data["content"][0]["text"] for e in path if e.type == USER]
        assert users == ["already logged", "accepted then crashed"]
        assert next(e for e in path if e.type == USER and e.data.get("input_id") == lost)


async def test_subscription_starts_with_the_live_state(ws_for, project, tmp_path):
    """An approval requested between a client's snapshot and its subscription still reaches it (F07)."""
    out = tmp_path / "outside"
    out.mkdir()
    model = FakeModel(reply(None, call("write", path=str(out / "a.txt"), content="a")), reply("done"))
    async with Server(ws_for(model)) as s:
        sid = await s.session(project)
        snap = (await s.client.get(f"/api/sessions/{sid}")).json()  # the client's snapshot: nothing live yet
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
        for _ in range(100):  # the approval appears before the client subscribes
            if (live := s.runs.live.get(sid)) and live.approvals:
                break
            await asyncio.sleep(0.02)
        stream = Stream(s.client, sid, after=snap["seq"])
        first_live = await stream.until(lambda e: e["type"] == "live")
        [approval] = first_live["live"]["approvals"]
        await s.client.post(f"/api/approvals/{approval['id']}", json={"decision": "once"})
        await stream.until(finished)
        stream.close()


async def test_a_subscriber_that_falls_behind_is_cut_off():
    """Bounded queues: a stalled client costs bounded memory and gets a clean end of stream (F08)."""
    from nano_workbuddy.runs import Hub

    hub = Hub(limit=3)
    slow, fast = hub.subscribe(), hub.subscribe()
    for i in range(3):
        hub.publish({"type": "delta", "i": i})
        fast.get_nowait()
    hub.publish({"type": "delta", "i": 3})  # slow's queue is full
    assert slow.get_nowait() is None and slow.empty() and slow not in hub.subs
    assert fast.get_nowait() == {"type": "delta", "i": 3} and fast in hub.subs


async def test_session_written_by_another_process_is_refused(ws_for, project):
    """While another process (the CLI) holds a session, the server refuses to write it: messages come back
    to the composer, checkout is a 409, and a startup resume leaves its pending inputs alone (F12)."""
    from nano_workbuddy.log import SessionLog

    async with Server(ws_for(FakeModel(reply("after")))) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        cli = SessionLog.open(s.ws.open_session(sid).file)
        with cli.writing():
            await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "while busy"})
            dropped = await stream.until(lambda e: e["type"] == "inbox" and e.get("dropped"))
            assert dropped["dropped"] == ["while busy"]
            assert any(e["type"] == "run.error" and "another process" in e["message"] for e in stream.events)
            root = s.ws.open_session(sid).path()[0].id
            r = await s.client.post(f"/api/sessions/{sid}/checkout", json={"entry_id": root})
            assert r.status_code == 409
        assert s.ws.index.pending_inputs(sid) == []  # handed back, not left pending
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "now"})
        await stream.until(lambda e: finished(e) and e["status"] == "ok")
        stream.close()
