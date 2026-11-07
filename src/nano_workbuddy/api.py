"""HTTP commands + SSE events (D3). Routing, auth and validation only; no agent logic lives here."""

from __future__ import annotations

import asyncio
import dataclasses
import getpass
import json
import platform
import secrets
import shutil
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from . import artifacts, documents, llm, sandbox, schedule, search, settings, skills
from . import scheduler as sched_mod
from .config import McpServer, key_status, secret
from .connectors.feishu import Feishu
from .log import SessionBusy, entry_json
from .maintenance import home_guard
from .memory import project_file
from .policy import PRESETS
from .runs import Busy, RunManager
from .runtime import Workspace
from .scheduler import Notify, Scheduler
from .types import Message, text

COOKIE = "nwb_token"


class NewProject(BaseModel):
    path: str
    trusted: bool = False


class ProjectPatch(BaseModel):
    name: str | None = None
    trusted: bool | None = None
    no_index: bool | None = None


class NewSession(BaseModel):
    model: str | None = None
    project_id: str | None = None  # POST /api/sessions: None starts a chat without a project


class SessionPatch(BaseModel):
    model: str | None = None
    preset: Literal["readonly", "workspace", "trust"] | None = None
    title: str | None = None  # "" goes back to the title taken from the first message
    archived: bool | None = None
    recall_scope: Literal["project", "session", "selected", "all"] | None = None
    recall_sessions: list[str] | None = None
    budget: dict[Literal["model_calls", "tool_calls", "wall_seconds"], int] | None = None


class Compact(BaseModel):
    instructions: str | None = None


class SendMessage(BaseModel):
    text: str
    attachments: list[str] = []  # ids from PUT /api/sessions/{sid}/attachments
    request_id: str | None = None
    parent_id: str | None = None  # set when editing an earlier message: the new message branches from here
    delivery: Literal["auto", "steer", "queue"] = "auto"


class Reveal(BaseModel):
    path: str


class Checkout(BaseModel):
    entry_id: str
    descend: bool = False  # jump to the latest entry under entry_id (switching branches)
    summarize: bool = False  # first summarise the branch being left, and attach the summary at the new position


class Fork(BaseModel):
    entry_id: str


class ProviderBody(BaseModel):
    protocol: Literal["openai-chat", "anthropic", "openai-responses"]
    base_url: str
    api_key: str | None = None  # a new key to store; omitted keeps the current one
    key_ref: str | None = None  # or point at env:NAME / keyring:NAME instead


class ModelBody(BaseModel):
    provider: str
    id: str
    context_window: int
    max_output: int
    params: dict[str, Any] = {}
    vision: bool | None = None  # None: guess from the protocol


class SearchBody(BaseModel):
    provider: str = "tavily"
    api_key: str | None = None  # plaintext, stored in secrets.json
    key_ref: str | None = None  # or env:NAME / keyring:NAME
    clear: bool = False  # remove the key: web_search is no longer offered


class FeishuBody(BaseModel):
    enabled: bool | None = None
    cli: str | None = None


class BrowserBody(BaseModel):
    enabled: bool | None = None
    headless: bool | None = None


class ScheduleBody(BaseModel):
    name: str
    prompt: str = ""
    template: dict[str, str] | None = None  # {"kind": "weekly_research", "topic": ..., "file": ..., "extra": ...}
    project_id: str | None = None  # None: the chat folder
    kind: Literal["once", "daily", "weekly"] = "weekly"
    at: str = "09:00"
    weekdays: list[int] = [1]
    day: str = ""
    tz: str | None = None  # default: the machine's zone
    catch_up: Literal["once", "skip", "ask"] = "once"
    model: str | None = None


class SchedulePatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    enabled: bool | None = None
    kind: Literal["once", "daily", "weekly"] | None = None
    at: str | None = None
    weekdays: list[int] | None = None
    day: str | None = None
    tz: str | None = None
    catch_up: Literal["once", "skip", "ask"] | None = None


class DefaultModel(BaseModel):
    name: str


class McpBody(BaseModel):
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    secret_keys: list[str] = []  # env/header names whose values go to secrets.json
    enabled: bool = True
    exposure: Literal["auto", "direct", "deferred"] = "auto"
    sandbox: bool = True
    network: bool = True
    writable: list[str] = []
    timeout: int = 300
    description: str = ""


class Enabled(BaseModel):
    enabled: bool


class SkillDirs(BaseModel):
    dirs: list[str]


class MemoryText(BaseModel):
    text: str


class Answer(BaseModel):
    answer: str


class Decision(BaseModel):
    decision: Literal["once", "always", "reject"]
    feedback: str | None = None


def create_app(ws: Workspace, token: str, static_dir: Path | None = None, *, scheduler_enabled: bool = True,
               notify: Notify | None = None) -> FastAPI:
    """Every route is async: the run manager, logs and SQLite connection live on the event loop thread,
    and plain `def` routes would run in a thread pool."""
    runs = RunManager(ws)
    scheduler = Scheduler(runs, notify)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with home_guard(ws.cfg.home):
            indexing = asyncio.create_task(ws.index_loop(), name="record-index")
            runs.resume_all()
            if scheduler_enabled:
                scheduler.start()
            try:
                yield
            finally:
                await scheduler.stop()
                await runs.shutdown()
                indexing.cancel()
                await asyncio.gather(indexing, return_exceptions=True)
                await ws.mcp.close()

    app = FastAPI(title="nano-workbuddy", lifespan=lifespan)
    app.state.runs = runs
    app.state.scheduler = scheduler

    @app.exception_handler(SessionBusy)
    async def session_busy(request: Request, e: SessionBusy) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, 409)  # another process (the CLI) is writing this session

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
        return {"models": [{"name": m.name, "provider": m.provider.name, "context_window": m.context_window}
                           for m in ws.cfg.models.values()],
                "default_model": ws.cfg.default_model, "presets": {k: p.label for k, p in PRESETS.items()},
                "user": getpass.getuser(), "version": version("nano-workbuddy")}

    @app.get("/api/usage")
    async def usage(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
        """Token use per day and per model over the last `days` days, summed from the runs table."""
        start = time.time() - days * 86400
        per_day: dict[str, dict[str, int]] = {}
        per_model: dict[str, dict[str, int]] = {}
        for run in ws.index.usage(start):
            day = time.strftime("%Y-%m-%d", time.localtime(run.pop("at")))
            for bucket in (per_day.setdefault(day, {}), per_model.setdefault(run.pop("model") or "?", {})):
                bucket["runs"] = bucket.get("runs", 0) + 1
                for k, v in run.items():
                    if isinstance(v, int):
                        bucket[k] = bucket.get(k, 0) + v
        return {"days": days, "per_day": [{"day": d, **v} for d, v in sorted(per_day.items())],
                "per_model": [{"model": m, **v} for m, v in sorted(per_model.items(), key=lambda x: -x[1]["runs"])]}

    @app.get("/api/system")
    async def system() -> dict[str, Any]:
        """What this machine offers the agent, and where its files live; the developer view of the config."""
        cfg = ws.cfg
        picker = sys.platform == "darwin" or shutil.which("zenity") is not None
        return {"version": version("nano-workbuddy"), "python": platform.python_version(),
                "os": f"{platform.system()} {platform.release()}", "home": str(cfg.home),
                "config": str(cfg.home / "config.toml"), "scratch_dir": str(cfg.scratch_dir),
                "sandbox": sandbox.backend(), "ripgrep": shutil.which("rg"), "folder_picker": picker,
                "hooks": [dataclasses.asdict(h) for h in cfg.hooks],
                "rules": [{"action": r.action, "resource": r.resource, "effect": r.effect} for r in cfg.rules]}

    def project_view(p: dict[str, Any]) -> dict[str, Any]:
        last = ws.index.last_runs(p["id"])
        sessions = [{**s, "last_run": last.get(s["id"])} for s in ws.index.sessions(p["id"])]
        return {**p, "trusted": bool(p["trusted"]), "scratch": ws.is_scratch(p),
                "no_index": json.loads(p["settings_json"]).get("no_index", False),
                "sessions": sessions}

    def project_row(pid: str) -> dict[str, Any]:
        if (project := ws.index.project(pid)) is None:
            raise HTTPException(404, "no such project")
        return project

    def idle(sids: list[str]) -> None:
        if any(sid in runs.live for sid in sids):
            raise HTTPException(409, "stop the active run first")

    @app.get("/api/projects")
    async def projects() -> list[dict[str, Any]]:
        return [project_view(p) for p in ws.index.projects()]

    @app.post("/api/projects")
    async def add_project(body: NewProject) -> dict[str, Any]:
        """Adding a folder that is already a project returns it unchanged, trust included."""
        root = Path(body.path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"{root} is not a directory")
        return project_view(ws.index.project_for(root, body.trusted))

    @app.patch("/api/projects/{pid}")
    async def update_project(pid: str, body: ProjectPatch) -> dict[str, Any]:
        project_row(pid)
        ws.index.update_project(pid, (body.name or "").strip() or None, body.trusted, body.no_index)
        if body.no_index is not None:
            await ws.sync_records([r["id"] for r in ws.index.sessions(pid)])
        return project_view(ws.index.project(pid))

    @app.delete("/api/projects/{pid}")
    async def remove_project(pid: str) -> dict[str, bool]:
        """Forgets the project and deletes its sessions; the folder itself is not touched."""
        project_row(pid)
        idle([r["id"] for r in ws.index.sessions(pid)])
        ws.delete_project(pid)
        return {"ok": True}

    @app.post("/api/pick-folder")
    async def pick_folder() -> dict[str, str | None]:
        """A native folder dialog on the machine the server runs on: a browser cannot reveal a real path."""
        try:
            path = await choose_folder()
        except FileNotFoundError as e:
            raise HTTPException(501, str(e)) from e
        return {"path": str(Path(path).resolve()) if path else None}

    @app.post("/api/sessions")
    async def start_session(body: NewSession) -> dict[str, Any]:
        project = project_row(body.project_id) if body.project_id else ws.scratch_project()
        try:
            log = ws.new_session(Path(project["root_path"]), body.model)
        except KeyError as e:
            raise HTTPException(400, str(e)) from e
        return ws.index.session(log.id)

    @app.post("/api/projects/{pid}/sessions")
    async def new_session(pid: str, body: NewSession) -> dict[str, Any]:
        return await start_session(body.model_copy(update={"project_id": pid}))

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
        if body.title is not None:
            ws.index.rename_session(sid, body.title.strip() or None)
        if body.archived is not None:
            ws.index.archive_session(sid, body.archived)
        recall_patch = body.model_dump(include={"recall_scope", "recall_sessions"}, exclude_none=True)
        if recall_patch:
            if any(ws.index.session(s) is None for s in body.recall_sessions or []):
                raise HTTPException(400, "selected recall session does not exist")
            ws.index.update_settings(sid, recall_patch)
        if body.budget is not None:
            if any(type(v) is not int or not 1 <= v <= 100_000 for v in body.budget.values()):
                raise HTTPException(400, "budget values must be positive integers <= 100000")
            ws.index.update_settings(sid, {"budget": body.budget})
        session = ws.index.session(sid)
        runs.hub(log).publish({"type": "session", "session": session, "context": ws.context_usage(log)})
        return session

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str) -> dict[str, bool]:
        session_log(sid)
        idle([sid])
        ws.delete_session(sid)
        runs.hubs.pop(sid, None)
        return {"ok": True}

    @app.get("/api/sessions/{sid}/events", response_class=EventSourceResponse)
    async def events(sid: str, request: Request, after: int = -1) -> AsyncIterator[ServerSentEvent]:
        """Replay durable entries with seq > after, then the current state of the active run, then stream
        live events until the client leaves. The ephemeral state (streamed text, approvals, questions, inbox)
        is sent inside the subscription: whatever happened between the client's snapshot request and this
        subscription — an approval card appearing, the run ending — is not lost in that gap."""
        log = session_log(sid)
        last = max(after, int(request.headers.get("last-event-id") or -1))
        q = runs.hub(log).subscribe()  # subscribe and read the state with no await between: no gap, no dupes
        backlog = sorted((e for e in log.entries.values() if e.seq > last), key=lambda e: e.seq)
        leaf, live = log.leaf_id, runs.live_view(sid)
        try:
            for e in backlog:
                yield ServerSentEvent(data={"type": "entry", "seq": e.seq, "entry": entry_json(e)}, id=str(e.seq))
            yield ServerSentEvent(data={"type": "live", "live": live})
            yield ServerSentEvent(data={"type": "leaf", "leaf_id": leaf})
            while (event := await q.get()) is not None:  # None: this client fell too far behind
                yield ServerSentEvent(data=event, id=str(event["seq"]) if "seq" in event else None)
        finally:
            runs.hub(log).unsubscribe(q)

    @app.post("/api/sessions/{sid}/messages")
    async def send(sid: str, body: SendMessage) -> dict[str, Any]:
        log = session_log(sid)
        if body.parent_id is not None and body.parent_id not in log.entries:
            raise HTTPException(400, "unknown parent_id")
        if not body.text.strip() and not body.attachments:
            raise HTTPException(400, "empty message")
        try:
            return runs.send(log, body.text, request_id=body.request_id, parent_id=body.parent_id,
                             delivery=body.delivery, attachments=body.attachments)
        except Busy as e:
            raise HTTPException(409, str(e)) from e
        except KeyError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/api/sessions/{sid}/attachments")
    async def upload(sid: str, request: Request, name: str = Query(..., min_length=1)) -> dict[str, Any]:
        """The raw file as the request body (no multipart parser needed); returns the attachment to send."""
        log = session_log(sid)
        if int(request.headers.get("content-length") or 0) > artifacts.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file is larger than 50 MB")
        data = await request.body()
        try:
            return await asyncio.to_thread(ws.upload, log, name, data)
        except ValueError as e:
            raise HTTPException(413, str(e)) from e

    @app.get("/api/sessions/{sid}/deliveries/{run_id}")
    async def deliveries(sid: str, run_id: str) -> dict[str, Any]:
        """Inspect already delivered bytes locally; no model call, replay, or status mutation."""
        log = session_log(sid)
        row = ws.index._one("select * from runs where id=? and session_id=?", run_id, sid)
        if not row or not any(e.run_id == run_id for e in log.path()):
            raise HTTPException(404, "no such run on this session branch")
        files = [{**f, "availability": artifacts.availability(ws.store, f["artifact"]["sha256"])}
                 for f in artifacts.run_deliveries(log, run_id)]
        return {"run_id": run_id, "status": row["status"], "files": files}

    @app.get("/api/sessions/{sid}/files/{digest}")
    async def file(sid: str, digest: str, name: str = "file", download: bool = False) -> FileResponse:
        """A kept version of a delivered file, or an attachment: only those this session's log refers to."""
        log = session_log(sid)
        if digest not in artifacts.files_of(log):
            raise HTTPException(404, "no such file in this session")
        if artifacts.availability(ws.store, digest) != "available":
            raise HTTPException(410, "the kept version is missing or damaged; no file was regenerated")
        name = artifacts.safe_name(name)
        return FileResponse(artifacts.blob(ws.store, digest), media_type=artifacts.mime_of(Path(name)),
                            filename=name, content_disposition_type="attachment" if download else "inline")

    @app.get("/api/sessions/{sid}/files/{digest}/text")
    async def file_text(sid: str, digest: str, name: str) -> dict[str, Any]:
        """The text of a kept document, for previewing Word, PowerPoint and Excel files in the browser."""
        log = session_log(sid)
        if digest not in artifacts.files_of(log):
            raise HTTPException(404, "no such file in this session")
        kind = documents.KINDS.get(Path(name).suffix.lower())
        if kind is None:
            raise HTTPException(400, "not a document type with a text preview")
        try:
            doc = await asyncio.to_thread(documents.parse, artifacts.blob(ws.store, digest), kind)
        except documents.DocumentError as e:
            raise HTTPException(422, str(e)) from e
        return {"description": doc.describe(), "notes": doc.notes, "text": doc.text()}

    @app.post("/api/sessions/{sid}/reveal")
    async def reveal(sid: str, body: Reveal) -> dict[str, bool]:
        """Show a delivered file in Finder (or the file manager): the server runs on the user's own machine."""
        log = session_log(sid)
        if body.path not in artifacts.session_records(log)[1]:
            raise HTTPException(404, "not a file delivered in this session")
        if not Path(body.path).exists():
            raise HTTPException(410, "the file no longer exists at that path; download the kept version instead")
        argv = (["open", "-R", body.path] if sys.platform == "darwin"
                else ["xdg-open", str(Path(body.path).parent)])
        if not shutil.which(argv[0]):
            raise HTTPException(501, f"{argv[0]} is not available")
        await asyncio.create_subprocess_exec(*argv)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/checkout")
    async def checkout(sid: str, body: Checkout) -> dict[str, str | None]:
        log = session_log(sid)
        if sid in runs.live:
            raise HTTPException(409, "cannot switch branches while a run is active")
        if body.entry_id not in log.entries:
            raise HTTPException(404, "no such entry")
        target = log.latest_leaf(body.entry_id) if body.descend else body.entry_id
        if body.summarize:  # needs a model call: runs in the background, the new leaf arrives as events
            runs.switch_branch(log, target)
            return {"leaf_id": None}
        ws.checkout(log, target)
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

    @app.post("/api/questions/{qid}")
    async def answer(qid: str, body: Answer) -> dict[str, bool]:
        if not body.answer.strip():
            raise HTTPException(400, "empty answer")
        try:
            runs.answer(qid, body.answer.strip())
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    # --- model and key settings (keys go in, never come out: only their status and last 4 characters) ---

    def apply(change) -> dict[str, Any]:
        try:
            ws.cfg = change()
        except settings.SettingsError as e:
            raise HTTPException(400, str(e)) from e
        return settings_payload()

    def settings_payload() -> dict[str, Any]:
        from .plugins import catalog

        cfg = ws.cfg
        specs = ws.mcp_specs()
        return {"default_model": cfg.default_model,
                "providers": [{"name": p.name, "protocol": p.protocol, "base_url": p.base_url,
                               "key": key_status(p.api_key, cfg.home)} for p in cfg.providers.values()],
                "models": [{"name": m.name, "provider": m.provider.name, "id": m.id, "context_window": m.context_window,
                            "max_output": m.max_output, "params": m.params, "vision": m.vision,
                            "sees_images": m.sees_images} for m in cfg.models.values()],
                "feishu": {"enabled": cfg.feishu.enabled, "cli": cfg.feishu.cli},
                "browser": {"enabled": cfg.browser.enabled, "headless": cfg.browser.headless,
                            "profile": str(cfg.home / "browser" / "profile"),
                            **(ws.mcp.status({"browser": specs["browser"]})["browser"]
                               if "browser" in specs else {"status": "disabled", "error": "", "tools": []})},
                "search": {"provider": cfg.search.provider,
                           "key": key_status(cfg.search.api_key, cfg.home) if cfg.search.api_key else None},
                "mcp": [{**dataclasses.asdict(m), **status, "keys": {k: key_status(v, cfg.home)
                                                                     for k, v in (*m.env.items(), *m.headers.items())
                                                                     if v.startswith(("env:", "keyring:", "secret:"))}}
                        for m, status in ((m, ws.mcp.status(cfg.mcp)[m.name]) for m in cfg.mcp.values())],
                "skills": skill_views(), "skill_dirs": list(cfg.skill_dirs),
                "plugins": [{"name": name, "description": desc, "enabled": name in cfg.plugins_enabled}
                            for name, desc in catalog().items()]}

    @app.patch("/api/settings/plugins/{name}")
    async def enable_plugin(name: str, body: Enabled) -> dict[str, Any]:
        return apply(lambda: settings.set_plugin_enabled(ws.cfg.home, name, body.enabled))

    def skill_views() -> list[dict[str, Any]]:
        """User skills once, then each project's own; on/off is by name, like the model sees them."""
        out = [skill_view(s, None) for s in ws.skills(None, enabled=False).values()]
        for p in ws.index.projects():
            if not p["trusted"]:
                continue  # its skills are not loaded
            found = skills.discover(Path(p["root_path"]), ws.cfg.home)
            out += [skill_view(s, p["name"]) for s in found.values() if s.source == "project"]
        return out

    def skill_view(s: skills.Skill, project: str | None) -> dict[str, Any]:
        return {"name": s.name, "description": s.description, "path": str(s.path), "source": s.source,
                "project": project, "enabled": s.name not in ws.cfg.skills_disabled}

    @app.get("/api/settings")
    async def settings_view() -> dict[str, Any]:
        return settings_payload()

    @app.put("/api/settings/providers/{name}")
    async def put_provider(name: str, body: ProviderBody) -> dict[str, Any]:
        if body.key_ref and not body.key_ref.startswith(("env:", "keyring:")):
            raise HTTPException(400, "key_ref must be env:NAME or keyring:NAME")
        return apply(lambda: settings.put_provider(ws.cfg.home, name, body.protocol, body.base_url,
                                                   api_key=body.api_key, key_ref=body.key_ref))

    @app.delete("/api/settings/providers/{name}")
    async def delete_provider(name: str) -> dict[str, Any]:
        return apply(lambda: settings.delete_provider(ws.cfg.home, name, ws.cfg))

    @app.put("/api/settings/models/{name}")
    async def put_model(name: str, body: ModelBody) -> dict[str, Any]:
        return apply(lambda: settings.put_model(ws.cfg.home, name, body.provider, body.id, body.context_window,
                                                body.max_output, body.params, body.vision))

    @app.get("/api/schedules")
    async def schedules_list() -> list[dict[str, Any]]:
        return [sched_mod.view(ws, row, runs.live.get(row["session_id"])) for row in ws.index.schedules()]

    @app.post("/api/schedules")
    async def schedules_create(body: ScheduleBody) -> dict[str, Any]:
        root = Path(ws.index.project(body.project_id)["root_path"]) if body.project_id else ws.cfg.scratch_dir
        template_json, prompt = "{}", body.prompt
        if body.template and body.template.get("kind") == "weekly_research":
            topic = body.template.get("topic", "").strip()
            if not topic:
                raise HTTPException(400, "the research topic is required")
            template_json, prompt = sched_mod.weekly_research(topic, body.template.get("file") or "调研简报.md",
                                                              body.template.get("extra", ""))
        rule = schedule.Rule(body.kind, body.at, body.tz or schedule.local_zone(), tuple(body.weekdays), body.day)
        try:
            row = sched_mod.create(ws, body.name, prompt, root, rule, body.catch_up, body.model, template_json)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        return sched_mod.view(ws, row)

    @app.patch("/api/schedules/{schedule_id}")
    async def schedules_update(schedule_id: str, body: SchedulePatch) -> dict[str, Any]:
        fields = body.model_dump(exclude_none=True)
        if "at" in fields:
            fields["at_time"] = fields.pop("at")
        try:
            return sched_mod.view(ws, sched_mod.update(ws, schedule_id, **fields))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/schedules/{schedule_id}")
    async def schedules_delete(schedule_id: str) -> dict[str, bool]:
        """The schedule goes; its session (the history of its runs) stays, like any other conversation."""
        if ws.index.schedule(schedule_id) is None:
            raise HTTPException(404, "no such task")
        ws.index.delete_schedule(schedule_id)
        return {"ok": True}

    @app.post("/api/schedules/{schedule_id}/run")
    async def schedules_run(schedule_id: str) -> dict[str, str]:
        try:
            return {"request_id": await scheduler.run_now(schedule_id)}
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/search")
    async def search_records(q: str, kind: Literal["all", "messages", "sources", "files"] = "all",
                             limit: int = Query(30, ge=1, le=100),
                             project_id: str | None = None) -> list[dict[str, Any]]:
        """Full-text search over the assistant's own records, for the search page."""
        if project_id:
            project_row(project_id)
        return ws.recall(q, kind, limit, scope=ws.record_scope(project_id=project_id))

    @app.get("/api/records/{ref}")
    async def read_record(ref: str, start: int = Query(0, ge=0), end: int = Query(6000, ge=1),
                          session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            session_log(session_id)
        try:
            record = ws.read_record(ref, start, end, scope=ws.record_scope(session_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if record is None:
            raise HTTPException(404, "no record in the selected scope")
        return record

    @app.get("/api/sessions/{sid}/diagnostics")
    async def diagnostics(sid: str) -> dict[str, Any]:
        from .usage import events

        log = session_log(sid)
        ledger = events(ws.usage_path(log))
        out = []
        for row in ws.index._all("select * from runs where session_id=? order by started_at desc limit 20", sid):
            records = [e for e in ledger if e.get("run_id") == row["id"]]
            started = [e for e in records if e["type"] == "model.started"]
            terminal = {e["call_id"] for e in records if e["type"] == "model.response"}
            freeze = next((e for e in records if e["type"] == "budget.frozen"), {})
            frozen = freeze.get("limits")
            out.append({"run_id": row["id"], "status": row["status"], "model": row["model_ref"],
                        "started_at": row["started_at"], "ended_at": row["ended_at"],
                        "usage": json.loads(row["usage_json"] or "{}"), "limits": frozen,
                        "model_calls": len(started),
                        "tool_calls": sum(e["type"] == "budget.tool" for e in records)
                                      + freeze.get("legacy_tool_calls", 0) if frozen else None,
                        "unknown_calls": sum(e["call_id"] not in terminal for e in started),
                        "sdk_max_retries": list({e.get("sdk_max_retries") for e in started}),
                        "failures": [{k: e[k] for k in ("type", "dimension", "error_type", "request_id") if k in e}
                                     for e in records if e["type"] in ("budget.exhausted", "model.error", "run.error")],
                        "tool_errors": sum(e.type == "tool_result" and e.data.get("is_error", False)
                                           for e in log.entries.values() if e.run_id == row["id"]),
                        "tools": [{"entry_id": e.id, "call_id": e.data["call_id"], "name": e.data["name"],
                                   "is_error": e.data.get("is_error", False),
                                   "exit_code": e.data.get("meta", {}).get("exit_code")}
                                  for e in log.entries.values()
                                  if e.run_id == row["id"] and e.type == "tool_result"][-50:],
                        "cost": None, "cost_status": "unknown: no verified package billing/price table"})
        return {"runs": out, "recall_scope": ws.record_scope(sid)["label"],
                "index": {"pending": len(ws._index_pending), "recent_errors": ws._index_errors},
                "budget_defaults": dataclasses.asdict(ws.cfg.budget)}

    @app.put("/api/settings/feishu")
    async def put_feishu(body: FeishuBody) -> dict[str, Any]:
        return apply(lambda: settings.put_table(ws.cfg.home, "feishu", body.model_dump(exclude_none=True)))

    @app.put("/api/settings/browser")
    async def put_browser(body: BrowserBody) -> dict[str, Any]:
        return apply(lambda: settings.put_table(ws.cfg.home, "browser", body.model_dump(exclude_none=True)))

    @app.get("/api/connectors/feishu")
    async def feishu_status() -> dict[str, Any]:
        """Whether lark-cli is installed, its version, the app and whether the user identity is logged in.
        Never the tokens: `auth status` does not print them."""
        lark = Feishu(ws.cfg.feishu.cli)
        if not lark.available:
            return {"available": False, "cli": ws.cfg.feishu.cli}
        status = await lark.status(max_age=0)
        user = status.get("identities", {}).get("user", {})
        return {"available": True, "cli": lark.cli, "version": await lark.version(), "app_id": status.get("appId"),
                "brand": status.get("brand"), "user": {"available": bool(user.get("available")),
                                                       "name": user.get("userName"), "message": user.get("message")},
                "error": status.get("error")}

    @app.put("/api/settings/search")
    async def put_search(body: SearchBody) -> dict[str, Any]:
        if body.key_ref and not body.key_ref.startswith(("env:", "keyring:")):
            raise HTTPException(400, "key_ref must be env:NAME or keyring:NAME")
        return apply(lambda: settings.put_search(ws.cfg.home, body.provider, body.api_key, body.key_ref, body.clear))

    @app.post("/api/settings/search/test")
    async def test_search() -> dict[str, Any]:
        """One real query with the saved key, so a wrong key or a missing proxy shows up here, not mid-task."""
        if (backend := ws.search_provider()) is None:
            raise HTTPException(400, "no search key is configured")
        started = time.monotonic()
        try:
            hits = await backend.search("nano-workbuddy test", 1, None, "general")
        except search.SearchError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "seconds": round(time.monotonic() - started, 2), "results": len(hits)}

    @app.delete("/api/settings/models/{name}")
    async def delete_model(name: str) -> dict[str, Any]:
        return apply(lambda: settings.delete_model(ws.cfg.home, name))

    @app.put("/api/settings/default-model")
    async def set_default(body: DefaultModel) -> dict[str, Any]:
        return apply(lambda: settings.set_default(ws.cfg.home, body.name))

    @app.put("/api/settings/mcp/{name}")
    async def put_mcp(name: str, body: McpBody) -> dict[str, Any]:
        spec = McpServer(name, body.command.strip(), tuple(body.args), body.env, body.url.strip(), body.headers,
                         body.enabled, body.exposure, body.sandbox, body.network, tuple(body.writable), body.timeout,
                         body.description.strip())
        return apply(lambda: settings.put_mcp(ws.cfg.home, spec, tuple(body.secret_keys)))

    @app.patch("/api/settings/mcp/{name}")
    async def enable_mcp(name: str, body: Enabled) -> dict[str, Any]:
        return apply(lambda: settings.set_mcp_enabled(ws.cfg.home, name, body.enabled))

    @app.delete("/api/settings/mcp/{name}")
    async def delete_mcp(name: str) -> dict[str, Any]:
        return apply(lambda: settings.delete_mcp(ws.cfg.home, name))

    @app.post("/api/settings/mcp/{name}/connect")
    async def connect_mcp(name: str) -> dict[str, Any]:
        """Start (or restart) servers now instead of at the next run, to see their tools or error."""
        if name not in ws.cfg.mcp:
            raise HTTPException(404, "no such MCP server")
        if srv := ws.mcp.servers.get(name):
            await srv.stop()
        await ws.mcp.connect(ws.cfg.mcp)
        return settings_payload()

    @app.patch("/api/settings/skills/{name}")
    async def enable_skill(name: str, body: Enabled) -> dict[str, Any]:
        return apply(lambda: settings.set_skill_enabled(ws.cfg.home, name, body.enabled))

    @app.put("/api/settings/skill-dirs")
    async def skill_dirs(body: SkillDirs) -> dict[str, Any]:
        return apply(lambda: settings.set_skill_dirs(ws.cfg.home, body.dirs))

    @app.get("/api/skills")
    async def project_skills(project_id: str | None = None) -> list[dict[str, str]]:
        """Enabled skills for a project (or for chats without one): what "/name" in the composer can invoke."""
        found = ws.skills(Path(project_row(project_id)["root_path"]) if project_id else None)
        return [{"name": s.name, "description": s.description} for s in found.values()]

    # --- memory files (D18): the user can always read and correct what the model saved ---

    def memory_files() -> list[dict[str, Any]]:
        files = [{"key": "user", "label": "个人（USER.md）", "path": ws.cfg.home / "USER.md"}]
        files += [{"key": p["id"], "label": "对话文件夹" if ws.is_scratch(p) else f"项目 {p['name']}",
                   "path": project_file(Path(p["root_path"]))}
                  for p in ws.index.projects()]
        return [{**f, "path": str(f["path"]), "text": f["path"].read_text(encoding="utf-8") if f["path"].is_file()
                 else ""} for f in files]

    @app.get("/api/memory")
    async def memory() -> list[dict[str, Any]]:
        return memory_files()

    @app.put("/api/memory/{key}")
    async def put_memory(key: str, body: MemoryText) -> list[dict[str, Any]]:
        f = next((Path(m["path"]) for m in memory_files() if m["key"] == key), None)
        if f is None:
            raise HTTPException(404, "no such memory file")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body.text, encoding="utf-8")
        return memory_files()

    @app.get("/api/settings/providers/{name}/models")
    async def remote_models(name: str) -> list[dict[str, Any]]:
        if (provider := ws.cfg.providers.get(name)) is None:
            raise HTTPException(404, "no such connection")
        try:
            return await asyncio.wait_for(llm.list_models(provider, secret(provider.api_key, ws.cfg.home)), 30)
        except Exception as e:  # shown to the user: wrong key, unreachable host, no /models endpoint ...
            raise HTTPException(502, f"{type(e).__name__}: {e}"[:500]) from e

    @app.post("/api/settings/models/{name}/test")
    async def test_model(name: str) -> dict[str, Any]:
        """One tiny real request: proves URL, key and model id work together."""
        if name not in ws.cfg.models:
            raise HTTPException(404, "no such model")
        started, reply, usage = time.monotonic(), [], {}
        try:
            model = ws.connect(dataclasses.replace(ws.cfg.model(name), max_output=256))

            async def run() -> None:
                prompt = [Message(role="user", content=[text("ping")])]
                async for item in model.stream("Connection test. Reply with the single word OK.", prompt, []):
                    if isinstance(item, llm.Delta) and item.kind == "text":
                        reply.append(item.text)
                    elif isinstance(item, llm.Response):
                        usage.update(item.usage)

            await asyncio.wait_for(run(), 60)
        except Exception as e:  # the point of this endpoint is to report what went wrong
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
        return {"ok": True, "reply": "".join(reply)[:200], "ms": int((time.monotonic() - started) * 1000),
                "usage": usage}

    if static_dir and static_dir.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        async def web(path: str) -> FileResponse:
            f = (static_dir / path).resolve()
            if path and f.is_file() and f.is_relative_to(static_dir.resolve()):
                return FileResponse(f)
            return FileResponse(static_dir / "index.html")  # single-page app: every other path is the UI

    return app


async def choose_folder() -> str | None:
    """Show the OS folder picker; None if the user cancelled. FileNotFoundError if there is none."""
    if sys.platform == "darwin":
        argv = ["osascript", "-e", "activate", "-e", 'POSIX path of (choose folder with prompt "选择项目文件夹")']
    elif zenity := shutil.which("zenity"):
        argv = [zenity, "--file-selection", "--directory", "--title=选择项目文件夹"]
    else:
        raise FileNotFoundError("no folder dialog on this system; type the path instead")
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return (out.decode().strip() or None) if proc.returncode == 0 else None  # cancelling exits non-zero
