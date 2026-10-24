"""HTTP commands + SSE events (D3). Routing, auth and validation only; no agent logic lives here."""

from __future__ import annotations

import asyncio
import dataclasses
import getpass
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

from . import llm, sandbox, settings, skills
from .config import McpServer, key_status, secret
from .log import SessionBusy, entry_json
from .memory import project_file
from .policy import PRESETS
from .runs import Busy, RunManager
from .runtime import Workspace
from .types import Message, text

COOKIE = "nwb_token"


class NewProject(BaseModel):
    path: str
    trusted: bool = False


class ProjectPatch(BaseModel):
    name: str | None = None
    trusted: bool | None = None


class NewSession(BaseModel):
    model: str | None = None
    project_id: str | None = None  # POST /api/sessions: None starts a chat without a project


class SessionPatch(BaseModel):
    model: str | None = None
    preset: Literal["readonly", "workspace", "trust"] | None = None
    title: str | None = None  # "" goes back to the title taken from the first message
    archived: bool | None = None


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


def create_app(ws: Workspace, token: str, static_dir: Path | None = None) -> FastAPI:
    """Every route is async: the run manager, logs and SQLite connection live on the event loop thread,
    and plain `def` routes would run in a thread pool."""
    runs = RunManager(ws)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runs.resume_all()
        yield
        await runs.shutdown()
        await ws.mcp.close()

    app = FastAPI(title="nano-workbuddy", lifespan=lifespan)
    app.state.runs = runs

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
        return {**p, "trusted": bool(p["trusted"]), "scratch": ws.is_scratch(p),
                "sessions": ws.index.sessions(p["id"])}

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
        ws.index.update_project(pid, (body.name or "").strip() or None, body.trusted)
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
        target = log.latest_leaf(body.entry_id) if body.descend else body.entry_id
        if body.summarize:  # needs a model call: runs in the background, the new leaf arrives as events
            runs.switch_branch(log, target)
            return {"leaf_id": None}
        log.checkout(target)
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
        cfg = ws.cfg
        return {"default_model": cfg.default_model,
                "providers": [{"name": p.name, "protocol": p.protocol, "base_url": p.base_url,
                               "key": key_status(p.api_key, cfg.home)} for p in cfg.providers.values()],
                "models": [{"name": m.name, "provider": m.provider.name, "id": m.id, "context_window": m.context_window,
                            "max_output": m.max_output, "params": m.params} for m in cfg.models.values()],
                "mcp": [{**dataclasses.asdict(m), **status, "keys": {k: key_status(v, cfg.home)
                                                                     for k, v in (*m.env.items(), *m.headers.items())
                                                                     if v.startswith(("env:", "keyring:", "secret:"))}}
                        for m, status in ((m, ws.mcp.status(cfg.mcp)[m.name]) for m in cfg.mcp.values())],
                "skills": skill_views(), "skill_dirs": list(cfg.skill_dirs)}

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
                                                body.max_output, body.params))

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
