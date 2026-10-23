"""HTTP commands + SSE events (D3). Routing, auth and validation only; no agent logic lives here."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from .log import entry_json
from .policy import PRESETS
from .runs import Busy, RunManager
from .runtime import Workspace

COOKIE = "nwb_token"


class NewProject(BaseModel):
    path: str


class NewSession(BaseModel):
    model: str | None = None


class SessionPatch(BaseModel):
    model: str | None = None
    preset: Literal["readonly", "workspace", "trust"] | None = None


class Compact(BaseModel):
    instructions: str | None = None


class SendMessage(BaseModel):
    text: str
    request_id: str | None = None
    parent_id: str | None = None  # set when editing an earlier message: the new message branches from here
    delivery: Literal["auto", "steer", "queue"] = "auto"


class Checkout(BaseModel):
    entry_id: str
    descend: bool = False  # jump to the latest entry under entry_id (switching branches)


class Fork(BaseModel):
    entry_id: str


class Decision(BaseModel):
    decision: Literal["once", "always", "reject"]
    feedback: str | None = None


def create_app(ws: Workspace, token: str, static_dir: Path | None = None) -> FastAPI:
    """Every route is async: the run manager, logs and SQLite connection live on the event loop thread,
    and plain `def` routes would run in a thread pool."""
    runs = RunManager(ws)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runs.resume_all()
        yield
        await runs.shutdown()

    app = FastAPI(title="nano-workbuddy", lifespan=lifespan)
    app.state.runs = runs

    @app.middleware("http")
    async def auth(request: Request, call_next):
        """Loopback is not authentication (D16): require the token, and refuse cross-site writes."""
        path = request.url.path
        if path.startswith("/api/") and path != "/api/login":
            given = request.cookies.get(COOKIE) or request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(given, token):
                return JSONResponse({"detail": "unauthorized"}, 401)
            origin = request.headers.get("origin")
            if request.method != "GET" and origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request refused"}, 403)
        return await call_next(request)

    def session_log(sid: str):
        try:
            return ws.open_session(sid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/login")
    async def login(token_: str = Query("", alias="token"), next: str = "/") -> Response:
        if not secrets.compare_digest(token_, token):
            raise HTTPException(401, "bad token")
        resp = RedirectResponse(next if next.startswith("/") else "/", 303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=400 * 86400)
        return resp

    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        return {"models": [{"name": m.name, "context_window": m.context_window} for m in ws.cfg.models.values()],
                "default_model": ws.cfg.default_model, "presets": {k: p.label for k, p in PRESETS.items()}}

    @app.get("/api/projects")
    async def projects() -> list[dict[str, Any]]:
        return [{**p, "sessions": ws.index.sessions(p["id"])} for p in ws.index.projects()]

    @app.post("/api/projects")
    async def add_project(body: NewProject) -> dict[str, Any]:
        root = Path(body.path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"{root} is not a directory")
        return ws.index.project_for(root)

    @app.post("/api/projects/{pid}/sessions")
    async def new_session(pid: str, body: NewSession) -> dict[str, Any]:
        if (project := ws.index.project(pid)) is None:
            raise HTTPException(404, "no such project")
        try:
            log = ws.new_session(Path(project["root_path"]), body.model)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e
        return ws.index.session(log.id)

    @app.get("/api/sessions/{sid}")
    async def snapshot(sid: str) -> dict[str, Any]:
        log = session_log(sid)
        return {**runs.snapshot(log), "context": ws.context_usage(log)}

    @app.patch("/api/sessions/{sid}")
    async def update_session(sid: str, body: SessionPatch) -> dict[str, Any]:
        log = session_log(sid)
        if body.model:
            try:
                ws.set_model(log, body.model)
            except KeyError as e:
                raise HTTPException(400, str(e)) from e
        if body.preset:
            ws.index.update_settings(sid, {"preset": body.preset})  # next run; the model is told via a notice
        session = ws.index.session(sid)
        runs.hub(log).publish({"type": "session", "session": session, "context": ws.context_usage(log)})
        return session

    @app.get("/api/sessions/{sid}/events", response_class=EventSourceResponse)
    async def events(sid: str, request: Request, after: int = -1) -> AsyncIterator[ServerSentEvent]:
        """Replay durable entries with seq > after, then stream live events until the client leaves."""
        log = session_log(sid)
        last = max(after, int(request.headers.get("last-event-id") or -1))
        q = runs.hub(log).subscribe()  # subscribe and read the backlog with no await between: no gap, no dupes
        backlog = sorted((e for e in log.entries.values() if e.seq > last), key=lambda e: e.seq)
        leaf = log.leaf_id
        try:
            for e in backlog:
                yield ServerSentEvent(data={"type": "entry", "seq": e.seq, "entry": entry_json(e)}, id=str(e.seq))
            yield ServerSentEvent(data={"type": "leaf", "leaf_id": leaf})
            while True:
                event = await q.get()
                yield ServerSentEvent(data=event, id=str(event["seq"]) if "seq" in event else None)
        finally:
            runs.hub(log).unsubscribe(q)

    @app.post("/api/sessions/{sid}/messages")
    async def send(sid: str, body: SendMessage) -> dict[str, str]:
        log = session_log(sid)
        if body.parent_id is not None and body.parent_id not in log.entries:
            raise HTTPException(400, "unknown parent_id")
        try:
            return {"delivery": runs.send(log, body.text, request_id=body.request_id, parent_id=body.parent_id,
                                          delivery=body.delivery)}
        except Busy as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/sessions/{sid}/checkout")
    async def checkout(sid: str, body: Checkout) -> dict[str, str | None]:
        log = session_log(sid)
        if sid in runs.live:
            raise HTTPException(409, "cannot switch branches while a run is active")
        if body.entry_id not in log.entries:
            raise HTTPException(404, "no such entry")
        log.checkout(log.latest_leaf(body.entry_id) if body.descend else body.entry_id)
        ws.index.sync_session(log)
        return {"leaf_id": log.leaf_id}

    @app.post("/api/sessions/{sid}/fork")
    async def fork(sid: str, body: Fork) -> dict[str, Any]:
        log = session_log(sid)
        if body.entry_id not in log.entries:
            raise HTTPException(404, "no such entry")
        return ws.index.session(ws.fork(log, body.entry_id).id)

    @app.post("/api/sessions/{sid}/compact")
    async def compact(sid: str, body: Compact) -> dict[str, bool]:
        try:
            runs.compact(session_log(sid), body.instructions)
        except Busy as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.post("/api/sessions/{sid}/cancel")
    async def cancel(sid: str) -> dict[str, bool]:
        try:
            runs.cancel(sid)
        except KeyError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.post("/api/approvals/{aid}")
    async def approve(aid: str, body: Decision) -> dict[str, bool]:
        try:
            runs.resolve(aid, body.decision, body.feedback)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    if static_dir and static_dir.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        async def web(path: str) -> FileResponse:
            f = (static_dir / path).resolve()
            if path and f.is_file() and f.is_relative_to(static_dir.resolve()):
                return FileResponse(f)
            return FileResponse(static_dir / "index.html")  # single-page app: every other path is the UI

    return app
