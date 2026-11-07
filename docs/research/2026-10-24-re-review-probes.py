"""Isolated review probes against the current checkout.

Fake models/connectors; synthetic Anthropic SSE uses loopback only.
No user data or external services. Run from the repository root.
"""

import asyncio
import dataclasses
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path.cwd() / "tests"))
import contextlib

from conftest import FakeModel, call, reply

from nano_workbuddy import artifacts, maintenance, policy, skills
from nano_workbuddy.config import load
from nano_workbuddy.connectors import browser
from nano_workbuddy.connectors.feishu import FeishuArgs, classify, feishu
from nano_workbuddy.hooks import Hook, Outcome
from nano_workbuddy.runs import RunManager
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.schedule import Rule
from nano_workbuddy.scheduler import Scheduler, create, outcome_of
from nano_workbuddy.tools.docs import citation_checks, create_document
from nano_workbuddy.tools.registry import AnyArgs, Result, Tool, ToolCtx
from nano_workbuddy.types import TOOL_RESULT


def emit(name, **data):
    print(json.dumps({"probe": name, **data}, ensure_ascii=False))


def workspace(base, name, *script):
    root = base / name / "project"
    root.mkdir(parents=True)
    ws = Workspace(load(base / name / "home"), connect=FakeModel(*script).connect)
    return ws, root


class FakeFeishu:
    available = True

    def __init__(self):
        self.bodies = []

    async def status(self):
        return {"identities": {"user": {"available": True, "userName": "Synthetic Tester"}}}

    async def risk(self, domain, command):
        return "write"

    async def run(self, argv, cwd, timeout=120):
        body = (cwd / "draft.md").read_text() if "--content" in argv else "literal"
        self.bodies.append(("preview" if "--dry-run" in argv else "execute", body))
        return 0, json.dumps({"ok": True, "data": {"received": body}})


async def main(base):
    skills.SHARED = base / "empty-skills"
    ws, root = workspace(base, "admission", reply("ok"))
    log = ws.new_session(root)
    runs = RunManager(ws)
    with (
        patch.object(ws.index, "add_input", side_effect=RuntimeError("simulated exit before input insert")),
        contextlib.suppress(RuntimeError),
    ):
        runs.send(log, "must survive retry", request_id="same-id")
    retry = runs.send(log, "must survive retry", request_id="same-id")
    emit("request_claim_gap", retry=retry, pending=len(ws.index.pending_inputs()), live=len(runs.live))

    ws, root = workspace(base, "schedule", reply("ok"))
    runs = RunManager(ws)
    scheduler = Scheduler(runs, notify=AsyncMock())
    row = create(ws, "test", "synthetic task", root, Rule("daily", "09:00", "Asia/Shanghai"))
    now = time.time()
    ws.index.update_schedule(row["id"], next_run=now - 10)
    with (
        patch.object(runs, "send", side_effect=RuntimeError("simulated exit after claim")),
        contextlib.suppress(RuntimeError),
    ):
        await scheduler.tick(now)
    await scheduler.tick(now + 1)
    await scheduler.tick(now + 700)
    occ = ws.index.occurrences(row["id"])[0]
    emit(
        "schedule_claim_gap",
        outcome=occ["outcome"],
        pending=len(ws.index.pending_inputs()),
        runs=len(ws.index._all("select * from runs")),
        next_advanced=ws.index.schedule(row["id"])["next_run"] > now,
    )

    ws, root = workspace(
        base,
        "outcome",
        reply(None, call("task_result", outcome="unchanged", summary="no change")),
        RuntimeError("synthetic provider failure"),
    )
    log = ws.new_session(root)
    with contextlib.suppress(RuntimeError):
        await ws.submit(log, "test", request_id="sched:test:1")
    row = ws.index.run_for_request("sched:test:1")
    emit("reported_outcome_masks_failure", actual_run_status=row["status"], reported=outcome_of(log, row)[0])

    ws, root = workspace(base, "restore", reply("ok"))
    log = ws.new_session(root)
    await ws.submit(log, "backup probe")
    oldhome = ws.cfg.home
    archive = base / "backup.tar.gz"
    maintenance.backup(oldhome, archive)
    ws.index.db.close()
    for session_log in ws._logs.values():
        session_log.close()
    oldhome.rename(oldhome.with_name("old-home-moved"))
    target = base / "restored"
    maintenance.restore(archive, target)
    restored = Workspace(load(target), connect=FakeModel().connect)
    try:
        restored.open_session(log.id)
        failure = None
    except Exception as e:
        failure = type(e).__name__
    emit(
        "restore_new_home",
        old_path_retained=str(oldhome) in restored.index.session(log.id)["file_path"],
        session_open_error=failure,
    )

    ws, root = workspace(
        base,
        "approval-file",
        reply(None, call("feishu", command=["docs", "+create", "--content", "@draft.md"])),
        reply("done"),
    )
    fake = FakeFeishu()
    ws.feishu = lambda: fake
    (root / "draft.md").write_text("APPROVED_CONTENT")

    async def ask(check, tool, invocation):
        (root / "draft.md").write_text("CHANGED_AFTER_PREVIEW")
        return None

    await ws.submit(ws.new_session(root), "create doc", ask=ask)
    emit("approval_file_not_frozen", calls=fake.bodies)

    ws, root = workspace(base, "hook", reply("unused"))
    ws.cfg = dataclasses.replace(ws.cfg, hooks=(Hook("PreToolUse", "unused"),))
    ws._hook = AsyncMock(return_value=Outcome(decision="allow"))
    asked = AsyncMock(return_value=None)
    run = ws.make_run(ws.new_session(root), ws.cfg.default_model, None, lambda _: None, ask=asked)
    result = await run.gate(feishu, call("feishu", command=["im", "+messages-send", "--text", "synthetic"]))
    emit("hook_overrides_fixed_approval", gate_allowed=result is None, approval_calls=asked.await_count)

    argv = ["im", "+messages-send", "--text", "synthetic", "--dry-run", "--dry-run=false"]
    args = FeishuArgs(command=argv)
    action, resource = feishu.permission_scope(args, ToolCtx(root, "s", base / "spill"))
    rules = [*policy.PRESETS["workspace"].rules, *policy.FIXED]
    emit(
        "conflicting_dry_run_flags",
        classified_write=classify(argv)[2],
        action=action,
        permission=policy.check(action, resource, rules, {}).effect,
    )

    async def no_browser(args, ctx):
        return Result.ok("fake only")

    t = Tool(
        "mcp__browser__browser_tabs",
        "fake",
        AnyArgs,
        no_browser,
        "exec",
        False,
        None,
        lambda args, ctx: ("mcp__browser__browser_tabs", ""),
    )
    wrapped = browser.wrap([t], browser.Lease(), {"s"})[0]
    a = AnyArgs(action="new", url="https://unapproved.example.invalid/")
    action, resource = wrapped.permission_scope(a, ToolCtx(root, "s", base / "spill"))
    emit(
        "browser_new_tab_bypasses_site_gate",
        action=action,
        resource=resource,
        permission=policy.check(action, resource, rules, {}).effect,
    )

    ws, root = workspace(
        base,
        "nested-permission",
        reply(None, call("grep", pattern="SYNTHETIC_DENIED_MARKER", mode="content")),
        reply("done"),
    )
    private = root / "private"
    private.mkdir()
    (private / "notes.txt").write_text("SYNTHETIC_DENIED_MARKER")
    ws.cfg = dataclasses.replace(ws.cfg, rules=(policy.Rule("read", str(private / "*"), "deny"),))
    log = ws.new_session(root)
    with patch("nano_workbuddy.tools.search.shutil.which", return_value=None):
        await ws.submit(log, "search synthetic fixture")
    results = [e.data for e in log.entries.values() if e.type == TOOL_RESULT]
    emit(
        "recursive_search_ignores_child_deny",
        direct_permission=policy.check("read", str(private / "notes.txt"), ws.rules(log, "workspace"), {}).effect,
        found="SYNTHETIC_DENIED_MARKER" in json.dumps(results),
    )

    root = base / "citation"
    root.mkdir()
    ctx = ToolCtx(root, "s", root / "spill", store=root / "store")
    artifacts.cite(ctx.sources, "https://example.invalid/source.pdf", "file", "Downloaded only")
    checks = citation_checks("Finding [S1]", ctx)
    result = await create_document.fn(create_document.args(path="brief.md", content="Finding [S1]"), ctx)
    emit(
        "downloaded_source_and_export",
        checks=checks,
        exported_source_url="https://example.invalid/source.pdf" in (root / "brief.md").read_text(),
        all_artifact_checks_pass=all(c["ok"] for c in result.meta["artifact"]["checks"]),
    )

    # Real installed Anthropic SDK, synthetic SSE over loopback only.
    import test_anthropic as fixture

    from nano_workbuddy.config import ModelSpec, Provider
    from nano_workbuddy.llm import Response
    from nano_workbuddy.llm.anthropic import AnthropicMessages
    from nano_workbuddy.loop import next_step
    from nano_workbuddy.types import ASSISTANT, Entry, text

    with patch.object(fixture, "EVENTS", fixture.EVENTS[:-2]):
        server = fixture.server.__wrapped__()
        url, _ = next(server)
        model = AnthropicMessages(
            ModelSpec("c", Provider("test", "anthropic", url, ""), "synthetic", 200000, 8000), "fake-key-not-a-secret"
        )
        try:
            response = None
            async for item in model.stream("test", [{"role": "user", "content": [text("hello")]}], fixture.TOOLS):
                if isinstance(item, Response):
                    response = item
            e = Entry("e", None, ASSISTANT, {"content": response.content, "stop": response.stop})
            emit("anthropic_missing_terminal", stop=response.stop, next_step=type(next_step([e])).__name__)
        finally:
            await model.client.close()
            server.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nwb-review-v2-") as tmp:
        asyncio.run(main(Path(tmp)))
