"""Wires config, index, logs, models and tools together. Shared by the CLI and the HTTP server."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import artifacts, compaction, hooks, llm, plugins, policy, prompt, skills
from . import usage as usage_log
from .budgets import BudgetExceeded, RunBudget
from .config import Config, McpServer, ModelSpec, load, secret
from .context import budget, estimate_tokens, in_view, loaded_tools, pinned, request_tokens, to_messages
from .index import Index
from .knowledge import Knowledge
from .log import SessionBusy, SessionLog
from .loop import SHUTDOWN, Inbox, Input, Run, close_pending, drive, user_data
from .maintenance import home_guard
from .memory import memory_tool
from .tools import CORE, Registry, Result, Tool, ToolCtx, ToolError
from .tools.mcp import McpHub, Server, tool_search
from .tools.registry import parse_args
from .types import ASSISTANT, CONTEXT_STATE, NOTICE, SYSTEM_PROMPT, USER, ToolCall, new_id, text

MAX_RESUMES = 3
DEFER_SHARE = 0.05  # exposure "auto": defer MCP tools when their schemas would take more of the window than this

type Emit = Callable[[dict[str, Any]], None]
type Ask = Callable[[policy.Check, Tool, ToolCall], Awaitable[Result | None]]  # asks the user; None = allowed
type Question = Callable[[dict[str, Any]], Awaitable[str]]  # ask_user: shows a question, returns the answer


class Workspace:
    """Process-wide state. One instance per process; no globals."""

    def __init__(self, cfg: Config, connect: Callable[[ModelSpec], llm.Model] | None = None,
                 tools: Registry | None = None, *, extensions: tuple[plugins.Plugin, ...] = ()):
        self.extensions = extensions
        with home_guard(cfg.home):
            self._initialize(cfg, connect, tools)

    def _initialize(self, cfg, connect, tools):
        self.cfg = cfg
        self.index = Index(cfg.home / "index.db")
        self.connect = connect or self._connect
        self.tools = tools or Registry([*CORE, memory_tool(cfg.home)])
        self._custom_tools = tools is not None
        self.mcp = McpHub(cfg.home)
        # each session's tools as of its latest run: context estimates and manual compaction between runs use
        # them. Per session, because another session in another project may have other skills and servers.
        self.session_tools: dict[str, Registry] = {}
        self._logs: dict[str, SessionLog] = {}  # one live object per session, so listeners see every append
        self._stamps: dict[str, dict[str, tuple[int, int]]] = {}  # per-session read stamps survive across runs
        self._records: dict[str, tuple[dict, dict]] = {}  # per session: sources by url, delivered versions by path
        self._plugin_state: dict[str, Any] = {}
        self.running: set[str] = set()  # sessions with a run in progress (the browser lease needs to know)
        self._knowledge: Knowledge | None = None
        self._index_lock = asyncio.Lock()
        self._index_pending: set[str] = set()  # coalesced bounded queue; a periodic sweep covers overflow
        self._index_errors: list[dict[str, str]] = []
        self._index_cursor = 0
        self.index.reindex(self.sessions_dir)

    @classmethod
    def open(cls, home: Path | None = None) -> Workspace:
        return cls(load(home))

    def _connect(self, spec: ModelSpec) -> llm.Model:
        return llm.connect(spec, secret(spec.provider.api_key, self.cfg.home))

    def model_ref(self, log: SessionLog) -> str:
        """The session's model, or the default if it was deleted from the settings since."""
        ref = self.index.session(log.id)["model_ref"]
        return ref if ref in self.cfg.models else self.cfg.default_model

    @property
    def sessions_dir(self) -> Path:
        return self.cfg.home / "sessions"

    @property
    def store(self) -> Path:
        """Parsed-document cache, uploads and kept versions of delivered files (artifacts.py)."""
        return self.cfg.home / "files"

    def spill_dir(self, session_id: str) -> Path:
        return self.cfg.home.resolve() / "spill" / session_id

    def search_provider(self):
        """The web_search backend, or None when no key is configured (then the tool is not offered)."""
        return plugins.search_provider(self.cfg)

    def uploads_dir(self, session_id: str) -> Path:
        return self.spill_dir(session_id) / "uploads"

    def upload(self, log: SessionLog, name: str, data: bytes) -> dict[str, Any]:
        """Keep a file the user attached; returns its description (id, name, kind, ...)."""
        return artifacts.save_upload(self.store, self.uploads_dir(log.id), name, data)

    def attachments(self, log: SessionLog, ids: list[str] | tuple[str, ...]) -> tuple[list[dict], list[dict]]:
        """(descriptions, model blocks) of uploaded files for a message, for the session's current model."""
        files = [artifacts.find_upload(self.uploads_dir(log.id), i, self.store) for i in dict.fromkeys(ids)]
        vision = self.cfg.model(self.model_ref(log)).sees_images
        sources, _ = self.records(log)
        ctx = ToolCtx(root=self.root_of(log), session_id=log.id, spill_dir=self.spill_dir(log.id),
                      store=self.store, sources=sources)
        blocks = []
        for f in files:
            p = Path(f["path"])
            f["source"] = artifacts.source_read(ctx, p.as_uri(), p.read_bytes(), f["name"], path=str(p))
            blocks.extend(artifacts.upload_blocks(f, self.store, vision))
            artifacts.merge_source(sources, f["source"])
        return files, blocks

    def feishu(self):
        """The Feishu connector when enabled in the settings (whether or not lark-cli is installed)."""
        return plugins.feishu_client(self.cfg, self._plugin_state)

    def plugins(self) -> list[plugins.Plugin]:
        return list(self.extensions) if self._custom_tools else plugins.load(self.cfg, self._plugin_state,
                                                                           self.extensions)

    def mcp_specs(self, active: list[plugins.Plugin] | None = None) -> dict[str, McpServer]:
        """Configured servers plus servers contributed by enabled plugins."""
        specs = dict(self.cfg.mcp)
        for plugin in active if active is not None else self.plugins():
            specs.update(plugin.mcp)
        return specs

    @property
    def knowledge(self) -> Knowledge:
        if self._knowledge is None:
            self._knowledge = Knowledge(self.cfg.home / "knowledge.db", self.store)
        return self._knowledge

    def record_scope(self, session_id: str | None = None, *, project_id: str | None = None) -> dict[str, Any]:
        """The host, never model arguments, chooses the accessible records using current settings."""
        projects = {p["id"]: p for p in self.index.projects()
                    if not json.loads(p["settings_json"]).get("no_index", False)}
        rows = self.index._all("select id,project_id,file_path from sessions")
        ids = [r["id"] for r in rows if r["project_id"] in projects and Path(r["file_path"]).is_file()]
        target_session = None
        label = "全部未排除项目（用户全局搜索）"
        if session_id:
            row = self.index.session(session_id)
            selected = row["settings"].get("recall_scope", "project")
            project = self.index.project(row["project_id"])
            if selected == "session" or (selected == "project" and self.is_scratch(project)):
                target_session, label = session_id, "当前会话"
            elif selected == "project":
                project_id, label = row["project_id"], "当前项目"
            elif selected == "selected":
                allowed = set(row["settings"].get("recall_sessions", [])) | {session_id}
                ids = [sid for sid in ids if sid in allowed]
                label = "用户明确选定的会话"
            elif selected == "all":
                label = "跨项目（用户已启用）"
            else:
                target_session, label = session_id, "当前会话（未知设置按最小范围处理）"
        if project_id:
            project_sessions = {r["id"] for r in rows if r["project_id"] == project_id}
            ids = [sid for sid in ids if sid in project_sessions]
        if target_session:
            ids = [sid for sid in ids if sid == target_session]
        return {"session_ids": ids, "project_id": project_id, "session_id": target_session,
                "access_scope": "local", "label": label}

    def recall(self, query: str, kind: str = "all", limit: int = 20, *,
               scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Read only the cache. Background updates never run on the search/request path."""
        scope = scope or self.record_scope()
        return self.knowledge.search(query, kind, limit, **{k: v for k, v in scope.items() if k != "label"})

    def read_record(self, ref: str, start: int, end: int, *, scope: dict[str, Any]) -> dict | None:
        return self.knowledge.read_record(ref, start, end, **{k: v for k, v in scope.items() if k != "label"})

    async def sync_records(self, session_ids: list[str]) -> None:
        """One worker/connection, at most 128 sessions x 256 new lines per batch, including parsing."""
        _ = self.knowledge  # create/migrate the cache once on the owner thread
        async with self._index_lock:
            rows = [self.index.session(sid) for sid in session_ids[:128]]
            allowed = set(self.record_scope()["session_ids"])

            def update():
                knowledge = Knowledge(self.cfg.home / "knowledge.db", self.store)
                errors = []
                try:
                    for row in rows:
                        if row is None:
                            continue
                        try:
                            if row["id"] in allowed:
                                knowledge.sync([row])
                            else:
                                knowledge.forget(row["id"])
                        except Exception as exc:
                            errors.append({"session_id": row["id"], "error": type(exc).__name__})
                finally:
                    knowledge.db.close()
                return errors

            # Shield the thread on shutdown: it must finish before backup's home lock is released.
            task = asyncio.create_task(asyncio.to_thread(update))
            try:
                errors = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            self._index_errors = (self._index_errors + errors)[-20:]

    async def index_loop(self) -> None:
        while True:
            rows = self.index._all("select id from sessions order by id limit 64 offset ?", self._index_cursor)
            self._index_cursor = self._index_cursor + len(rows) if len(rows) == 64 else 0
            for row in rows:
                if len(self._index_pending) < 128:
                    self._index_pending.add(row["id"])
            batch = sorted(self._index_pending)[:128]
            self._index_pending.difference_update(batch)
            try:
                await self.sync_records(batch)
            except Exception:
                logging.getLogger(__name__).exception("background record indexing failed")
            await asyncio.sleep(2)

    def records(self, log: SessionLog) -> tuple[dict, dict]:
        """The session's numbered sources and delivered versions, rebuilt from its log once per process."""
        if log.id not in self._records:
            self._records[log.id] = artifacts.session_records(log)
        return self._records[log.id]

    # sessions

    def new_session(self, root: Path, model: str | None = None, *, trusted: bool = True,
                    initial_prompt: dict[str, str] | None = None) -> SessionLog:
        """trusted applies only if this creates the project (the caller has asked the user). An untrusted
        project's sessions start read-only (D12)."""
        project = self.index.project_for(root, trusted)
        sid = new_id("s")
        log = SessionLog.create(self.sessions_dir / project["id"] / f"{sid}.jsonl", sid, project["id"])
        log.append(SYSTEM_PROMPT, initial_prompt if initial_prompt is not None else
                   prompt.build(Path(project["root_path"]), self.cfg.home, trusted=bool(project["trusted"])))
        self.index.sync_session(log, self.cfg.model(model).name)
        self.index.update_settings(sid, {"preset": "workspace" if project["trusted"] else "readonly"})
        self._logs[sid] = log
        return log

    def scratch_project(self) -> dict[str, Any]:
        """The project that chats without a project run in; ours, so trusted."""
        self.cfg.scratch_dir.mkdir(parents=True, exist_ok=True)
        return self.index.project_for(self.cfg.scratch_dir, trusted=True)

    def is_scratch(self, project: dict[str, Any]) -> bool:
        return project["root_path"] == str(self.cfg.scratch_dir.resolve())

    def delete_session(self, session_id: str) -> None:
        """For good: the log, its saved tool outputs and scratch files, and its rows. Not the project's files."""
        row = self.index.session(session_id)
        if row is None:
            raise KeyError(f"no session {session_id}")
        if log := self._logs.pop(session_id, None):
            log.close()
        self._stamps.pop(session_id, None)
        self._records.pop(session_id, None)
        self.session_tools.pop(session_id, None)
        self.knowledge.forget(session_id)
        Path(row["file_path"]).unlink(missing_ok=True)
        (self.cfg.home / "usage" / f"{session_id}.jsonl").unlink(missing_ok=True)
        for d in (self.cfg.home / "spill" / session_id, tmp_dir(session_id)):
            shutil.rmtree(d, ignore_errors=True)
        self.index.delete_session(session_id)

    def delete_project(self, project_id: str) -> None:
        """Removes the project from the list together with its sessions; its folder on disk is untouched."""
        for row in self.index.sessions(project_id):
            self.delete_session(row["id"])
        shutil.rmtree(self.sessions_dir / project_id, ignore_errors=True)
        self.index.delete_project(project_id)

    def open_session(self, session_id: str) -> SessionLog:
        if log := self._logs.get(session_id):
            if log.refresh():
                self._records.pop(session_id, None)
                self._stamps.pop(session_id, None)
                self.index.sync_session(log)
            return log
        row = self.index.session(session_id)
        if row is None:
            raise KeyError(f"no session {session_id}")
        log = self._logs[session_id] = SessionLog.open(Path(row["file_path"]))
        return log

    def fork(self, log: SessionLog, entry_id: str) -> SessionLog:
        sid = new_id("s")
        new = log.fork(entry_id, self.sessions_dir / log.header["project_id"] / f"{sid}.jsonl", sid)
        row = self.index.session(log.id)
        self.index.sync_session(new, row["model_ref"])
        self.index.update_settings(sid, row["settings"])
        self._logs[sid] = new
        return new

    def root_of(self, log: SessionLog) -> Path:
        return Path(self.index.project(log.header["project_id"])["root_path"])

    def checkout(self, log: SessionLog, target_id: str | None) -> None:
        log.checkout(target_id)
        self._records.pop(log.id, None)
        self._stamps.pop(log.id, None)
        self.index.sync_session(log)

    def set_model(self, log: SessionLog, model: str) -> None:
        """Takes effect on the next run; a running run keeps the model it started with."""
        self.index.sync_session(log, self.cfg.model(model).name)

    def context_usage(self, log: SessionLog) -> dict[str, int]:
        """Estimated size of the next request versus what the session's model allows."""
        root = self.root_of(log)
        system, messages = to_messages(log.path(), current_context=prompt.context(root, self.cfg.home,
                                                                                self.trusted(root)))
        spec = self.cfg.model(self.model_ref(log))
        tools = self.tools_of(log).schemas(loaded_tools(log.path()))
        return {"tokens": request_tokens(system, messages, tools), "budget": budget(spec)}

    # tools (D14)

    def trusted(self, root: Path) -> bool:
        return bool((p := self.index.project_at(root)) and p["trusted"])

    def skills(self, root: Path | None, enabled: bool = True) -> dict[str, skills.Skill]:
        """Skills found for a project; by default minus those switched off in the settings. An untrusted
        project's own skills are not loaded: they are instructions written by whoever wrote the project."""
        root = root if root and self.trusted(root) else None
        found = skills.discover(root, self.cfg.home, self.cfg.skill_dirs)
        return {n: s for n, s in found.items() if not (enabled and n in self.cfg.skills_disabled)}

    def refresh_context(self, log: SessionLog, run_id: str | None = None) -> None:
        """Keep mutable context current, separate from raw conversation and long-term files."""
        root, path = self.root_of(log), log.path()
        available = self.skills(root)
        versions = {}
        for key in pinned(path):
            if key.startswith("skill:"):
                name = key.removeprefix("skill:")
                versions[key] = (skills.render(available[name]) if name in available else
                                 f"Skill {name!r} is inactive; its old instructions no longer apply.")
        current = {"text": prompt.context(root, self.cfg.home, self.trusted(root)), "skills": versions}
        previous = next((e.data for e in reversed(path) if e.type == CONTEXT_STATE),
                        {"text": path[0].data.get("context"), "skills": {}})
        if current != previous:
            log.append(CONTEXT_STATE, current, run_id=run_id)

    async def toolset(self, spec: ModelSpec, root: Path, scheduled: bool = False) -> tuple[Registry, dict[str, str]]:
        """Built-in tools, the skill tool and the tools of every enabled MCP server, read fresh at each run
        start so that a skill or server switched off in the settings is gone from the next run.
        Also returns the notices that tell the model what is available."""
        notices: dict[str, str] = {}
        active = self.plugins()
        base = list(self.tools.tools.values())
        services = dict(self.tools.services)
        for plugin in active:
            base.extend(plugin.tools)
            services.update(plugin.services)
            notices.update(plugin.notices)
        if scheduled:  # started by a schedule: the run reports its outcome, and knows nobody is watching
            from .scheduler import SCHEDULED_NOTICE, task_result

            base.append(task_result)
            notices["scheduled"] = SCHEDULED_NOTICE
        found = self.skills(root, enabled=False)
        enabled = {name: skill for name, skill in found.items() if name not in self.cfg.skills_disabled}
        if found:
            notices["skills"] = skills.listing(enabled) or "No skills are enabled."
        base += [skills.skill_tool(enabled)] if enabled else []
        specs = self.mcp_specs(active)
        # Reconcile even an empty configuration so removing the last server actually stops it.
        servers = await self.mcp.connect(specs)
        live = [s for s in servers if s.client]
        mcp_tools = {s.spec.name: s.as_tools() for s in live}
        for plugin in active:
            if plugin.wrap_mcp:
                mcp_tools = {name: plugin.wrap_mcp(name, tools, self.running) for name, tools in mcp_tools.items()}
        auto = sum(estimate_tokens(json.dumps([t.schema() for t in mcp_tools[s.spec.name]])) for s in live
                   if s.spec.exposure == "auto")
        lazy = {s.spec.name for s in live
                if s.spec.exposure == "deferred" or (s.spec.exposure == "auto" and auto > DEFER_SHARE *
                                                     spec.context_window)}
        deferred = [t.name for s in live if s.spec.name in lazy for t in mcp_tools[s.spec.name]]
        tools = [*base, *([tool_search] if deferred else []), *(t for ts in mcp_tools.values() for t in ts)]
        if specs:
            notices["mcp"] = mcp_notice(specs, servers, lazy)
        return Registry(tools, deferred, services=services,
                        cleanup=[p.cleanup for p in active if p.cleanup]), notices

    def tools_of(self, log: SessionLog) -> Registry:
        """Latest run's frozen tools, or a local-only composition before the first run."""
        if log.id in self.session_tools:
            return self.session_tools[log.id]
        active = self.plugins()
        services = dict(self.tools.services)
        for plugin in active:
            services.update(plugin.services)
        return Registry([*self.tools.tools.values(), *(t for p in active for t in p.tools)],
                        services=services, cleanup=[p.cleanup for p in active if p.cleanup])

    # permissions (D12)

    def preset(self, log: SessionLog) -> str:
        return self.index.session(log.id)["settings"]["preset"]

    def rules(self, log: SessionLog, preset: str) -> list[policy.Rule]:
        """Last match wins: preset defaults < config.toml < approved for this session < fixed rules."""
        return [*policy.PRESETS[preset].rules, *self.cfg.rules, *self.index.session_rules(log.id), *policy.FIXED]

    # runs

    async def submit(self, log: SessionLog, content: str | Input, *, request_id: str | None = None,
                     parent_id: str | None = None, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, ask: Ask | None = None, question: Question | None = None) -> str:
        """Append a user message (as a child of parent_id when editing an earlier message) and run.
        The whole run holds the session's write lock; if another process holds it, nothing is written."""
        msg = content if isinstance(content, Input) else Input(content)
        try:
            with home_guard(self.cfg.home), log.writing():
                return await self._submit(log, msg, request_id, parent_id, emit, inbox, ask, question)
        except SessionBusy as e:  # refused like a blocking hook: the text goes back to the composer
            if msg.id:
                return "busy"  # accepted input stays durable; the dispatcher retries after the writer releases
            self._give_back(msg, emit, str(e))
            return "blocked"

    async def _submit(self, log: SessionLog, msg: Input, request_id: str | None, parent_id: str | None,
                      emit: Emit, inbox: Inbox | None, ask: Ask | None, question: Question | None) -> str:
        if not self.root_of(log).is_dir():
            self._give_back(msg, emit, f"Project folder is missing: {self.root_of(log)}; "
                                      "reconnect it before continuing.")
            return "blocked"
        run, created = self.index.start_run(log.id, request_id, self.model_ref(log), input_id=msg.id)
        if not created:  # a retried request: never start the same work twice
            return run["status"]
        try:
            accepted = await self._begin_input(log, msg, run, parent_id, emit)
        except Exception:
            self.index.finish_run(run["id"], "error")
            if msg.id:
                self.index.finish_input(msg.id, "failed")
            raise
        if not accepted:
            return "blocked"
        return await self._drive(log, run, emit, inbox, ask, question)

    async def _begin_input(self, log, msg, run, parent_id, emit) -> bool:
        hook = await self._hook(log, "UserPromptSubmit", {"prompt": msg.text}, emit)
        if hook.block:  # the message is refused before anything is written; the text goes back to the composer
            self.index.finish_run(run["id"], "blocked")
            self._give_back(msg, emit, f"A UserPromptSubmit hook refused this message: {hook.reason}")
            return False
        if parent_id is not None:
            self.checkout(log, parent_id)
        close_pending(log, run["id"], "Interrupted; the user sent a new message instead.")
        if hook.context:
            log.append(NOTICE, {"kind": "hook", "text": "\n".join(hook.context)}, run_id=run["id"])
        if forced := self._invoked_skill(log, msg.text):  # "/name ...": load the skill for the model (D15)
            body, key = skills.render(forced), f"skill:{forced.name}"
            if in_view(log.path()).get(key) != body:  # unless the model already sees this exact text
                log.append(NOTICE, {"kind": key, "text": body, "pin": True}, run_id=run["id"])
        log.append(USER, user_data(msg), run_id=run["id"])
        return True

    def _give_back(self, msg: Input, emit: Emit, why: str) -> None:
        """A message that will not run: tell the client why and return the text to its composer."""
        if msg.id:
            self.index.finish_input(msg.id, "dropped")
        emit({"type": "run.error", "message": why})
        emit({"type": "inbox", "steer": [], "queue": [], "dropped": [msg.text]})

    async def _hook(self, log: SessionLog, event: str, payload: dict[str, Any], emit: Emit,
                    tool: str | None = None) -> hooks.Outcome:
        """Run the configured hooks for an event; their failures are shown to the user, not the model."""
        base = {"session_id": log.id, "transcript_path": str(log.file), "permission_mode": self.preset(log)}
        out = await hooks.run(self.cfg.hooks, event, base | payload, self.root_of(log), tool)
        for message in out.errors:
            emit({"type": "hook.error", "message": message})
        return out

    def _invoked_skill(self, log: SessionLog, content: str) -> skills.Skill | None:
        if not content.startswith("/"):
            return None
        return self.skills(self.root_of(log)).get(content[1:].split(maxsplit=1)[0] if content[1:].strip() else "")

    async def resume(self, log: SessionLog, emit: Emit = lambda e: None,
                     inbox: Inbox | None = None, ask: Ask | None = None,
                     question: Question | None = None) -> str | None:
        """Continue runs a crash or restart left unfinished (D6). Gives up after MAX_RESUMES attempts.
        A run another process is still driving (it holds the session's lock) is left alone: "busy"."""
        try:
            with home_guard(self.cfg.home), log.writing():
                return await self._resume(log, emit, inbox, ask, question)
        except SessionBusy:
            return "busy"

    async def _resume(self, log: SessionLog, emit: Emit, inbox: Inbox | None, ask: Ask | None,
                      question: Question | None) -> str | None:
        status = None
        for run in self.index.unfinished_runs(log.id):
            # The journal may have committed the failure just before the process lost its index update.
            terminal = next((e for e in reversed(log.path()) if e.run_id == run["id"] and e.type == NOTICE
                             and e.data.get("kind") == "delivery_recovery"
                             and e.data.get("status") in ("error", "limit", "cancelled")), None)
            if terminal:
                known = json.loads(run.get("usage_json") or "{}")
                measured = usage_log.totals(self.usage_path(log), run["id"])
                status = terminal.data["status"]
                self.index.finish_run(run["id"], status, {k: max(known.get(k, 0), measured.get(k, 0))
                                                          for k in known.keys() | measured.keys()})
                continue
            primary = next((r for r in self.index.pending_inputs(log.id) if r["run_id"] == run["id"]), None)
            if primary and not any(e.type == USER and e.data.get("input_id") == primary["id"]
                                   for e in log.entries.values()):
                extra = json.loads(primary.get("extra_json") or "{}")
                msg = Input(primary["content"], primary["id"], tuple(extra.get("attachments", ())),
                            tuple(extra.get("blocks", ())))
                if inbox:
                    inbox.steer[:] = [m for m in inbox.steer if m.id != msg.id]
                    inbox.queue[:] = [m for m in inbox.queue if m.id != msg.id]
                if not await self._begin_input(log, msg, run, primary["parent_id"], emit):
                    status = "blocked"
                    continue
            if self.index.bump_resume(run["id"]) > MAX_RESUMES:
                self.index.finish_run(run["id"], "error")
                status = "error"
                continue
            emit({"type": "run.resumed", "run_id": run["id"]})
            status = await self._drive(log, run, emit, inbox, ask, question)
        return status

    async def compact(self, log: SessionLog, instructions: str | None = None, emit: Emit = lambda e: None) -> str:
        """Manual compaction (/compact): summarise now, whatever the budget says."""
        run = self.make_run(log, self.model_ref(log), None, emit)
        emit({"type": "run.started", "run_id": None, "kind": "compaction"})
        status = "error"
        try:
            with home_guard(self.cfg.home), log.writing():
                status = "ok" if await compaction.compact(run, instructions) else "nothing"
        finally:
            emit({"type": "run.finished", "run_id": None, "status": status, "usage": run.usage})
        return status

    async def switch_branch(self, log: SessionLog, target_id: str, emit: Emit = lambda e: None) -> str:
        """Checkout with a summary of the branch being left (Pi's branch summary)."""
        run = self.make_run(log, self.model_ref(log), None, emit)
        emit({"type": "run.started", "run_id": None, "kind": "branch_summary"})
        status = "error"
        try:
            with home_guard(self.cfg.home), log.writing():
                status = "ok" if await compaction.summarize_branch(run, target_id) else "nothing"
        finally:
            self._records.pop(log.id, None)
            self._stamps.pop(log.id, None)
            self.index.sync_session(log)
            emit({"type": "run.finished", "run_id": None, "status": status, "usage": run.usage})
        return status

    def make_run(self, log: SessionLog, model_ref: str, run_id: str | None, emit: Emit,
                 inbox: Inbox | None = None, ask: Ask | None = None, tools: Registry | None = None,
                 notices: dict[str, str] | None = None, question: Question | None = None,
                 run_budget: RunBudget | None = None) -> Run:
        """Freeze one run's settings: model, preset and tools are read once (D12); approved rules are read live."""
        tools = tools or self.tools_of(log)
        preset_name = self.preset(log)
        preset = policy.PRESETS[preset_name]
        root, home = self.root_of(log), self.cfg.home.resolve()
        spill = self.spill_dir(log.id)
        model_ref = model_ref if model_ref in self.cfg.models else self.cfg.default_model
        sources, versions = self.records(log)
        ctx = ToolCtx(root=root, session_id=log.id, spill_dir=spill, emit=emit,
                      read_stamps=self._stamps.setdefault(log.id, {}),
                      sandbox=policy.sandbox_policy(preset, root, tmp_dir(log.id), home, spill),
                      tmp=tmp_dir(log.id), tools=tools, question=question, store=self.store,
                      vision=self.cfg.model(model_ref).sees_images, services=dict(tools.services),
                      sources=sources, versions=versions, document_records=artifacts.document_records(log),
                      recall=lambda q, k, n: self.recall(q, k, n, scope=self.record_scope(log.id)),
                      read_record=lambda ref, start, end: self.read_record(ref, start, end,
                                                                          scope=self.record_scope(log.id)),
                      recall_scope=lambda: self.record_scope(log.id)["label"])
        places = {"root": str(root), "home": str(home), "spill": str(spill)}
        def child_rules() -> Callable[[str, str], bool]:
            rules = self.rules(log, preset_name)  # snapshot on the event loop; search workers never use SQLite
            return lambda action, resource: policy.check(action, resource, rules, places).effect == "allow"

        ctx.allowed = child_rules()

        async def gate(tool: Tool, call: ToolCall) -> Result | None:
            ctx.allowed = child_rules()
            try:
                args = parse_args(tool, call)
                action, resource = tool.permission_scope(args, ctx)
            except ToolError as e:
                # Scope validation must fail before tool_started, too.
                return Result.error(str(e))
            check = policy.check(action, resource, self.rules(log, preset_name), places)
            if check.effect == "deny":
                return Result.error(f"Blocked by a permission rule: {action} on {resource} is not allowed.")
            if self.cfg.hooks:  # a PreToolUse hook can block, or turn ask into allow and allow into ask
                hook = await self._hook(log, "PreToolUse", {**hooks.tool_payload(tool.name, call["args"]),
                                                            "tool_use_id": call["id"]}, emit, tool.name)
                if hook.block:
                    return Result.error(f"Blocked by a PreToolUse hook: {hook.reason}")
                if hook.decision in ("allow", "ask"):
                    check = dataclasses.replace(check, effect=hook.decision)
            fixed = policy.check(action, resource, list(policy.FIXED), places)
            if fixed.rule is not None:  # mandatory constraints run after extensions; hooks cannot relax them
                check = dataclasses.replace(check, effect=fixed.effect, rule=fixed.rule)
            if check.effect == "allow":
                return None
            if ask is None:
                return Result.error(f"{action} on {resource} needs the user's approval, which this client "
                                    "cannot ask for.")
            turn = next((e.id for e in reversed(log.path()) if e.type == ASSISTANT), "")
            call_ctx = dataclasses.replace(ctx, call_id=call["id"], call_key=f"{turn}:{call['id']}")
            check = dataclasses.replace(check, approval_id=new_id("a"))
            if tool.preview:  # freeze before showing the card; an unverifiable preview is not an approval
                try:
                    check = dataclasses.replace(check, preview=await asyncio.wait_for(tool.preview(args, call_ctx), 40))
                except Exception as e:
                    return Result.error(f"Cannot prepare approval preview: {type(e).__name__}: {e}")
            try:
                denied = await ask(check, tool, call)
            except asyncio.CancelledError:
                if tool.on_decision:
                    await tool.on_decision(args, call_ctx, "expired", check.approval_id)
                raise
            if tool.on_decision:
                try:
                    await tool.on_decision(args, call_ctx, "denied" if denied is not None else "approved",
                                           check.approval_id)
                except ToolError as e:
                    return Result.error(str(e))
            return denied

        async def after_tool(tool: Tool, call: ToolCall, r: Result) -> Result:
            response = {"content": "".join(b["text"] for b in r.content if b["type"] == "text"), "is_error": r.is_error}
            hook = await self._hook(log, "PostToolUse", {**hooks.tool_payload(tool.name, call["args"]),
                                                         "tool_use_id": call["id"], "tool_response": response},
                                    emit, tool.name)
            if feedback := (hook.reasons if hook.block else []) + hook.context:
                return dataclasses.replace(r, content=[*r.content, text("[Hook feedback]\n" + "\n".join(feedback))])
            return r

        async def on_stop(asked: bool) -> str | None:
            hook = await self._hook(log, "Stop", {"stop_hook_active": asked}, emit)
            return f"[Stop hook] {hook.reason}" if hook.block else None

        has = {h.event for h in self.cfg.hooks}
        row = self.index._one("select started_at from runs where id=?", run_id) if run_id else None
        limits = run_budget or (RunBudget(self.usage_path(log), run_id, self.cfg.budget, row["started_at"],
                                          self.index.session(log.id)["settings"].get("budget", {}),
                                          sum(e.type == "tool_started" for e in log.entries.values()
                                              if e.run_id == run_id)) if row else None)
        model = usage_log.MeasuredModel(self.connect(self.cfg.model(model_ref)), self.usage_path(log), run_id,
                                       limits.model if limits else None)
        permissions = preset.notice.format(root=root)
        overrides = [*self.cfg.rules, *self.index.session_rules(log.id)]
        if overrides:
            permissions += ("\nAdditional permission rules override the preset defaults, in the order below "
                            "(last matching rule wins; mandatory approval constraints still apply). "
                            "Use allowed alternatives instead of attempting denied operations:\n" +
                            json.dumps([dataclasses.asdict(rule) for rule in overrides], ensure_ascii=False))
        return Run(log=log, model=model, tools=tools, ctx=ctx, run_id=run_id,
                   emit=emit, max_turns=self.cfg.max_turns, inbox=inbox or Inbox(), gate=gate,
                   after_tool=after_tool if "PostToolUse" in has else None,
                   on_stop=on_stop if "Stop" in has else None,
                   run_budget=limits,
                   refresh_context=lambda: self.refresh_context(log, run_id),
                   notices={"permissions": permissions, **(notices or {})})

    async def _drive(self, log: SessionLog, run_row: dict[str, Any], emit: Emit, inbox: Inbox | None,
                     ask: Ask | None, question: Question | None = None) -> str:
        """Unknown errors propagate to the caller; the run is still marked so it is not resumed."""
        status, usage, failure_type = "error", {}, ""
        tools = None
        previous = usage_log.totals(self.usage_path(log), run_row["id"])
        stored = json.loads(run_row.get("usage_json") or "{}")
        # Older runs have no per-call ledger. Preserve their known aggregates without inventing raw usage.
        if not previous:
            for entry in log.entries.values():
                if entry.run_id == run_row["id"]:
                    for k, v in entry.data.get("usage", {}).items():
                        previous[k] = previous.get(k, 0) + v
            stored = {k: max(stored.get(k, 0), previous.get(k, 0)) for k in stored.keys() | previous.keys()}
            previous = {}
        legacy = {k: max(v - previous.get(k, 0), 0) for k, v in stored.items()}
        emit({"type": "run.started", "run_id": run_row["id"]})
        self.running.add(log.id)
        try:
            limits = RunBudget(self.usage_path(log), run_row["id"], self.cfg.budget, run_row["started_at"],
                               self.index.session(log.id)["settings"].get("budget", {}),
                               sum(e.type == "tool_started" for e in log.entries.values() if e.run_id == run_row["id"]))
            timeout = asyncio.timeout(max(0, limits.deadline - time.time()))
            try:
                async with timeout:
                    limits.check_wall()
                    await self.sync_records([r["id"] for r in self.index.sessions(
                        self.index.session(log.id)["project_id"])])
                    ref = run_row["model_ref"] if run_row["model_ref"] in self.cfg.models else self.cfg.default_model
                    tools, notices = await self.toolset(self.cfg.model(ref), self.root_of(log),
                        scheduled=(run_row.get("request_id") or "").startswith("sched:"))
                    for kind in ("skills", "mcp", "feishu", "browser"):
                        if kind not in notices and kind in in_view(log.path()):
                            notices[kind] = f"No {kind} capabilities are currently enabled or available."
                    self.session_tools[log.id] = tools
                    run = self.make_run(log, ref, run_row["id"], emit, inbox, ask, tools, notices, question,
                                        run_budget=limits)
                    usage = run.usage
                    status = await drive(run)
            except TimeoutError as exc:
                if timeout.expired():
                    raise BudgetExceeded("wall_seconds") from exc
                raise
        except BudgetExceeded as exc:
            status = "limit"
            failure_type = "BudgetExceeded"
            usage_log.append(self.usage_path(log), {"type": "budget.exhausted", "run_id": run_row["id"],
                                                    "dimension": str(exc)})
            close_pending(log, run_row["id"], f"Run budget reached: {exc}")
            log.append(NOTICE, {"kind": "run_budget", "text": f"Run stopped: budget reached ({exc}). "
                               "Inspect completed work and unknown effects before retrying."}, run_id=run_row["id"])
        except asyncio.CancelledError as e:
            status = "interrupted" if SHUTDOWN in e.args else "cancelled"  # interrupted runs resume on restart
            failure_type = "CancelledError"
            raise
        except Exception as exc:
            failure_type = type(exc).__name__
            usage_log.append(self.usage_path(log), {"type": "run.error", "run_id": run_row["id"],
                                                    "error_type": type(exc).__name__})
            raise
        finally:
            if status in ("error", "limit", "cancelled") and (files := artifacts.run_deliveries(log, run_row["id"])):
                log.append(NOTICE, {"kind": "delivery_recovery", "status": status, "error_type": failure_type,
                    "deliveries": files, "pin": True,
                    "text": f"运行状态仍为 {status}，已有 {len(files)} 个交付版本保留。整个任务可能尚未完成。"
                    "确认或取回已有结果时，先核验并复用这些版本，不要重新生成正文或重复执行其他操作。"
                    "用户明确要求修改或补做时，再按新需求执行。文件路径与 hash：\n" + "\n".join(
                        f"{f['artifact']['path']} · {f['artifact']['sha256']}" for f in files)}, run_id=run_row["id"])
            usage = usage_log.totals(self.usage_path(log), run_row["id"])
            for k, v in legacy.items():
                usage[k] = usage.get(k, 0) + v
            self.running.discard(log.id)
            if tools:
                for cleanup in tools.cleanup:
                    try:
                        cleanup(log.id)
                    except Exception:
                        logging.getLogger(__name__).exception("Plugin cleanup failed for session %s", log.id)
            self.index.finish_run(run_row["id"], status, usage)
            self.index.sync_session(log)
            if len(self._index_pending) < 128:
                self._index_pending.add(log.id)
            emit({"type": "run.finished", "run_id": run_row["id"], "status": status, "usage": usage})
            await self.sync_records([log.id])
        return status

    def usage_path(self, log: SessionLog) -> Path:
        return self.cfg.home / "usage" / f"{log.id}.jsonl"


def mcp_notice(specs: dict[str, Any], servers: list[Server], lazy: set[str]) -> str:
    """What the model is told about MCP servers. It changes only when servers do, so it is appended
    as a new notice (D9) instead of rewriting the system prompt."""
    by_name = {s.spec.name: s for s in servers}
    lines = []
    for name in specs:
        s = by_name.get(name)
        if s is None:
            continue  # disabled: say nothing, its tools are simply gone
        if s.client is None:
            lines.append(f"- {name}: unavailable ({s.error or 'not started'}); its tools cannot be used now.")
            continue
        about = s.spec.description or s.instructions.strip()
        how = "deferred: load its tools with tool_search" if name in lazy else "tools declared directly"
        lines.append(f"- {name} ({len(s.tools)} tools, {how})" + (f": {about}" if about else ""))
    if not lines:
        return "No MCP servers are enabled."
    text = "MCP servers (their tools are named mcp__<server>__<tool>):\n" + "\n".join(lines)
    if lazy:
        text += ("\nDeferred tools are not declared yet. Call tool_search with keywords, or \"select:<name>\", "
                 "to load the ones you need; they become callable from your next step.")
    return text


def tmp_dir(session_id: str) -> Path:
    """Scratch space for sandboxed commands, outside ~/.nwb (which the sandbox hides)."""
    return Path(tempfile.gettempdir()).resolve() / f"nwb-{session_id}"
