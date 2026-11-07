"""R-A regressions: observable effects and durable outcomes, using isolated local connectors."""

import asyncio
import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply
from test_connectors import enable, ran
from test_connectors import lark as lark
from test_loop import crashed_session, results

from nano_workbuddy import hooks, policy
from nano_workbuddy.config import load
from nano_workbuddy.connectors import browser, feishu_ops
from nano_workbuddy.connectors.feishu import Feishu, _decision, _preview, _recover, feishu
from nano_workbuddy.log import SessionLog
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools import search
from nano_workbuddy.tools.mcp import Server
from nano_workbuddy.tools.registry import AnyArgs, Result, Tool, ToolCtx, ToolError
from nano_workbuddy.types import TOOL_STARTED, text_of


def effects(project):
    p = project / ".lark-effects.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def records(ws, log):
    return [json.loads(p.read_text()) for p in feishu_ops.directory(
        ToolCtx(ws.root_of(log), log.id, ws.spill_dir(log.id))).glob("op_*.json")]


def send():
    return {"kind": "send_message", "chat_id": "oc_team", "content": "APPROVED"}


@pytest.mark.parametrize("kind", ["send_message", "create_document", "import_document"])
async def test_approval_executes_frozen_content_after_source_changes(home, project, lark, kind):
    source = project / "draft.md"
    original = b"APPROVED_CONTENT\n@literal\n--dry-run=false"
    source.write_bytes(original)
    op = ({"kind": kind, "file": str(source), "type": "docx"} if kind == "import_document" else
          {"kind": kind, "content_file": str(source), **({"chat_id": "oc_team"} if kind == "send_message" else {})})
    model = FakeModel(reply(None, call("feishu", operation=op)), reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    seen = []

    async def ask(check, *_):
        seen.append(check)
        source.write_text("CHANGED_AFTER_PREVIEW")

    await ws.submit(log, "send the approved version", ask=ask)
    [effect] = effects(project)
    if kind == "import_document":
        assert effect["file_sha256"] == hashlib.sha256(original).hexdigest()
        assert seen[0].preview["files"][0]["sha256"] == effect["file_sha256"]
        assert not any(str(source) in flag for flag in effect["argv"])
    else:
        assert effect["content"] == original.decode() == seen[0].preview["payload"]["content"]
    [record] = records(ws, log)
    assert record["status"] == "confirmed"
    assert record["receipt"]["approval_id"] == seen[0].approval_id
    assert record["receipt"]["payload_hash"] == seen[0].preview["payload_hash"]


@pytest.mark.parametrize("change", ["account", "binary", "snapshot", "target", "content"])
async def test_changed_identity_target_or_frozen_attachment_cannot_execute(home, project, lark, monkeypatch, change):
    source = project / "input.txt"
    source.write_text("APPROVED")
    op = {"kind": "import_document", "file": str(source), "type": "docx"} if change == "snapshot" else send()
    model = FakeModel(reply(None, call("feishu", operation=op)), reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)

    async def ask(check, tool, c):
        if change == "account":
            status = await ws.feishu().status(max_age=0)
            status["identities"]["user"]["openId"] = "ou_another"

            async def switched(**_):
                return status

            monkeypatch.setattr(ws.feishu(), "status", switched)
        elif change == "binary":
            lark.write_text(lark.read_text() + "\n# changed after approval\n")
        elif change == "snapshot":
            p = Path(check.preview["files"][0]["snapshot"])
            p.chmod(0o600)
            p.write_bytes(b"UNAPPROVED")
        else:
            c["args"]["operation"]["chat_id" if change == "target" else "content"] = "UNAPPROVED"

    await ws.submit(log, "write", ask=ask)
    assert not effects(project)
    assert results(log)[0]["is_error"]


@pytest.mark.parametrize("preset", ["workspace", "trust"])
@pytest.mark.parametrize("decision", ["approve", "deny", "cancel"])
async def test_hook_allow_cannot_remove_once_only_confirmation(home, project, lark, monkeypatch, preset, decision):
    model = FakeModel(reply(None, call("feishu", operation=send())), reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    ws.cfg = dataclasses.replace(ws.cfg, hooks=(hooks.Hook("PreToolUse", "true"),))
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": preset})

    async def allow(*_, **__):
        return hooks.Outcome(decision="allow")

    monkeypatch.setattr(ws, "_hook", allow)
    seen = []

    async def ask(check, *_):
        seen.append(check)
        assert check.once_only and check.effect == "ask"
        if decision == "deny":
            return Result.error("user declined")
        if decision == "cancel":
            raise asyncio.CancelledError

    if decision == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await ws.submit(log, "send", ask=ask)
    else:
        await ws.submit(log, "send", ask=ask)
    assert len(seen) == 1
    assert len(effects(project)) == (1 if decision == "approve" else 0)
    assert records(ws, log)[0]["status"] == {"approve": "confirmed", "deny": "denied", "cancel": "expired"}[decision]


async def test_explicit_deny_beats_fixed_confirmation_and_preview(home, project, lark):
    model = FakeModel(reply(None, call("feishu", operation=send())), reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    ws.index.add_rule(log.id, policy.Rule("feishu.write", "*", "deny"))
    await ws.submit(log, "send")
    assert "Blocked by a permission rule" in text_of(results(log)[0]["content"])
    assert not ran(project) and not effects(project)


@pytest.mark.parametrize("flags", [
    ["--dry-run", "--dry-run=false"], ["--dry-run=false"], ["--as=bot"], ["--profile=other"],
    ["--idempotency-key=chosen"], ["--chat-id=other"], ["--text=other"], ["--json=true"],
])
async def test_ambiguous_or_host_owned_write_flags_never_reach_cli(home, project, lark, flags):
    command = ["im", "+messages-send", "--chat-id=oc_team", "--text=APPROVED", *flags]
    model = FakeModel(reply(None, call("feishu", command=command)), reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "send")
    assert results(log)[0]["is_error"] and not ran(project) and not effects(project)
    assert not [e for e in log.path() if e.type == TOOL_STARTED]


@pytest.mark.parametrize("mode", ["preview", "preview_fail", "denied_source"])
async def test_preview_modes_and_failures_never_write(home, project, lark, mode):
    source = project / "private.txt"
    source.write_text("SECRET_SYNTHETIC")
    op = send()
    if mode == "denied_source":
        op.pop("content")
        op["content_file"] = str(source)
    model = FakeModel(reply(None, call("feishu", operation=op, mode="preview" if mode == "preview" else "execute")),
                      reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    if mode == "preview_fail":
        (project / ".fake-lark-mode.json").write_text('{"preview_fail":true}')
    elif mode == "denied_source":
        ws.index.add_rule(log.id, policy.Rule("read", str(source), "deny"))
    seen = []

    async def ask(*args):
        seen.append(args)

    await ws.submit(log, "preview", ask=ask)
    assert not seen and not effects(project)
    assert results(log)[0]["is_error"] == (mode != "preview")


@pytest.mark.parametrize("failure", ["timeout", "missing_receipt", "fail_after_effect"])
async def test_unknown_result_blocks_new_call_ids_after_restart(home, project, lark, monkeypatch, failure):
    model = FakeModel(reply(None, call("feishu", operation=send())), reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    if failure == "timeout":
        real_run = ws.feishu().run

        async def timeout(argv, *args, **kwargs):
            if argv[:2] == ["im", "+messages-send"] and "--dry-run" not in argv and "--help" not in argv:
                raise TimeoutError
            return await real_run(argv, *args, **kwargs)

        monkeypatch.setattr(ws.feishu(), "run", timeout)
    else:
        (project / ".fake-lark-mode.json").write_text(json.dumps({failure: True}))

    async def approve(*_):
        return None

    await ws.submit(log, "send", ask=approve)
    assert "UNKNOWN" in text_of(results(log)[0]["content"])
    assert records(ws, log)[0]["status"] == "unknown"
    before = len(effects(project))
    file = log.file
    log.close()
    ws.index.db.close()
    model = FakeModel(reply(None, call("feishu", operation=send())), reply("done"))
    reopened = Workspace(load(home), connect=model.connect)
    log = SessionLog.open(file)
    await reopened.submit(log, "retry", ask=approve)
    assert len(effects(project)) == before
    assert "UNKNOWN" in text_of(results(log)[-1]["content"])


async def test_confirmed_receipt_is_recovered_without_another_external_write(home, project, lark):
    model = FakeModel(reply("recovered"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    c = call("feishu", operation=send())
    log = crashed_session(ws, project, c)
    run_row = ws.index.unfinished_runs(log.id)[0]
    run = ws.make_run(log, run_row["model_ref"], run_row["id"], lambda _: None)
    turn = next(e.id for e in reversed(log.path()) if e.type == "assistant")
    ctx = dataclasses.replace(run.ctx, call_id=c["id"], call_key=f"{turn}:{c['id']}")
    args = feishu.args(**c["args"])
    await _preview(args, ctx)
    await _decision(args, ctx, "approved", "a_synthetic")
    result = await feishu.fn(args, ctx)
    assert not result.is_error and len(effects(project)) == 1
    recovered = _recover(args, ctx)
    assert isinstance(recovered, Result) and recovered.meta["receipt"] == result.meta["receipt"]
    await ws.resume(log)
    assert len(effects(project)) == 1
    assert results(log)[0]["meta"]["receipt"] == result.meta["receipt"]


async def test_prepared_approval_is_consumed_once_and_message_keys_are_per_turn(project, lark, tmp_path):
    ctx = ToolCtx(project, "s", tmp_path / "spill", feishu=Feishu(str(lark)), call_key="turn1:call1")
    args = feishu.args(operation=send())
    await _preview(args, ctx)
    first = ctx.prepared[ctx.call_key].argv
    await _preview(args, ctx)
    assert ctx.prepared[ctx.call_key].argv == first
    await _decision(args, ctx, "approved", "a_one")
    returned = await asyncio.gather(feishu.fn(args, ctx), feishu.fn(args, ctx), return_exceptions=True)
    assert sum(isinstance(r, Result) and not r.is_error for r in returned) == 1
    assert sum(isinstance(r, ToolError) for r in returned) == 1
    assert len(effects(project)) == 1
    with pytest.raises(ToolError, match="confirmed receipt"):
        await _preview(args, ctx)
    later = dataclasses.replace(ctx, call_key="turn2:call1")
    await _preview(args, later)
    keys = [next(a.split("=", 1)[1] for a in argv if a.startswith("--idempotency-key="))
            for argv in (first, later.prepared[later.call_key].argv)]
    assert keys[0] != keys[1] and all(len(k) <= 50 for k in keys)
    assert _recover(feishu.args(operation={**send(), "chat_id": "oc_other"}), ctx) == "unknown"


async def test_fake_cli_uses_final_boolean_flag_value(project, lark):
    code, out = await Feishu(str(lark)).run(["im", "+messages-send", "--chat-id=oc_synthetic", "--text=SYNTHETIC",
                                            "--dry-run", "--dry-run=false"], project)
    assert code == 0 and json.loads(out).get("dry_run") is not True
    assert len(effects(project)) == 1  # isolated fixture effect, never a real Feishu send


def browser_tools(*names, seen=None):
    async def perform(args, ctx):
        if seen is not None:
            seen.append(dict(args.model_extra))
        return Result.ok("done")

    tools = [Tool("mcp__browser__" + n, "fake read-only hint", AnyArgs, perform, "read", False, None, None)
             for n in names]
    return browser.wrap(tools, browser.Lease(), set())


def test_browser_tab_capabilities_and_site_rules_share_navigation_scope(project, tmp_path):
    [tabs] = browser_tools("browser_tabs")
    ctx = ToolCtx(project, "s", tmp_path / "spill")
    nav = tabs.permission_scope(AnyArgs(action="new", url="https://approved.test/path"), ctx)
    assert nav == (browser.NAVIGATION, "https://approved.test/path")
    rules = [*policy.PRESETS["workspace"].rules, policy.Rule(browser.NAVIGATION, "https://approved.test/*", "allow"),
             *policy.FIXED]
    assert policy.check(*nav, rules, {}).effect == "allow"
    other = tabs.permission_scope(AnyArgs(action="new", url="https://other.test/"), ctx)
    assert policy.check(*other, rules, {}).effect == "ask"
    assert tabs.permission_scope(AnyArgs(action="list"), ctx) == ("mcp__browser__browser_tabs", "list")
    for action in ("close", "select"):
        scope = tabs.permission_scope(AnyArgs(action=action, index=0), ctx)
        check = policy.check(*scope, [*policy.PRESETS["trust"].rules, *policy.FIXED], {})
        assert check.effect == "ask" and check.once_only


def test_browser_mcp_workspace_includes_controlled_session_output(home, project):
    server = Server(browser.spec(home, headless=True), home)
    ctx = ToolCtx(project, "s", home / "files/s")
    assert browser._output("page.png", ctx).is_relative_to(server._params().cwd)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@host.test/"])
def test_browser_rejects_unsafe_navigation_from_all_entries(project, tmp_path, url):
    for t in browser_tools("browser_tabs", "browser_navigate"):
        with pytest.raises(ToolError):
            t.permission_scope(AnyArgs(action="new", url=url), ToolCtx(project, "s", tmp_path / "spill"))


async def test_browser_interactions_remain_once_only_under_trust_and_hooks(home, project, monkeypatch):
    names = ("browser_click", "browser_hover", "browser_navigate_back")
    invoked = []
    tools = browser_tools(*names, seen=invoked)
    model = FakeModel(reply(None, *(call(t.name) for t in tools)), reply("done"))
    ws = Workspace(load(home), connect=model.connect)
    ws.tools.tools.update({t.name: t for t in tools})
    ws.cfg = dataclasses.replace(ws.cfg, hooks=(hooks.Hook("PreToolUse", "true"),))
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": "trust"})

    async def allow(*_, **__):
        return hooks.Outcome(decision="allow")

    monkeypatch.setattr(ws, "_hook", allow)
    asked = []

    async def reject(check, *_):
        asked.append(check)
        return Result.error("declined")

    await ws.submit(log, "click", ask=reject)
    assert len(asked) == 3 and all(c.once_only for c in asked) and not invoked
    assert not browser_tools("browser_evaluate", "browser_run_code_unsafe", "browser_file_upload", "browser_set_config")


async def test_browser_read_hint_does_not_make_click_replayable(home, project):
    invoked = []
    [tool] = browser_tools("browser_click", seen=invoked)
    model = FakeModel(reply("check the unknown outcome"))
    ws = Workspace(load(home), connect=model.connect)
    ws.tools.tools[tool.name] = tool
    log = crashed_session(ws, project, call(tool.name))
    await ws.resume(log)
    assert not invoked and "unknown" in text_of(results(log)[0]["content"])


@pytest.mark.parametrize("filename", ["../escape.txt", "/tmp/escape.txt", "linked/escape.txt", ""])
def test_browser_output_cannot_escape_session_directory(project, tmp_path, filename):
    spill = tmp_path / "spill"
    output = spill / "browser-output"
    output.mkdir(parents=True)
    (output / "linked").symlink_to(project, target_is_directory=True)
    [tool] = browser_tools("browser_snapshot")
    with pytest.raises(ToolError):
        tool.permission_scope(AnyArgs(filename=filename), ToolCtx(project, "s", spill))


async def test_browser_output_rewrites_safe_filename_and_rejects_linked_output_root(project, tmp_path):
    seen = []
    [tool] = browser_tools("browser_take_screenshot", seen=seen)
    spill = tmp_path / "spill"
    ctx = ToolCtx(project, "s", spill)
    await tool.fn(AnyArgs(filename="nested/page.png"), ctx)
    dest = Path(seen[0]["filename"])
    assert dest == spill / "browser-output/nested/page.png" and dest.parent.is_dir()
    dest.write_bytes(b"existing")
    with pytest.raises(ToolError):
        await tool.fn(AnyArgs(filename="nested/page.png"), ctx)
    shutil.rmtree(spill / "browser-output")
    (spill / "browser-output").symlink_to(project, target_is_directory=True)
    with pytest.raises(ToolError):
        await tool.fn(AnyArgs(filename="escape.png"), ctx)
    assert not (project / "escape.png").exists()


@pytest.mark.parametrize("engine", ["python", "rg"])
@pytest.mark.parametrize("mode", ["files", "content", "count", "glob"])
async def test_recursive_search_respects_child_denies_in_real_runtime(home, project, monkeypatch, engine, mode):
    if engine == "python":
        monkeypatch.setattr(shutil, "which", lambda _: None)
    elif not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    (project / "private").mkdir()
    (project / "private/denied.txt").write_text("MATCH SECRET_FOLDER")
    (project / "blocked.txt").write_text("MATCH SECRET_FILE")
    (project / "allowed.txt").write_text("MATCH PUBLIC")
    original_open = Path.open

    def permitted_open(p, *args, **kwargs):
        if p.is_relative_to(project):
            assert p.name != "blocked.txt" and "private" not in p.relative_to(project).parts
        return original_open(p, *args, **kwargs)

    monkeypatch.setattr(Path, "open", permitted_open)
    searched = []
    original_search = search._rg_files

    async def authorized_search(rg, args, files, ctx):
        assert all(Path(f).is_file() and Path(f).name == "allowed.txt" for f in files)
        searched.extend(files)
        return await original_search(rg, args, files, ctx)

    monkeypatch.setattr(search, "_rg_files", authorized_search)
    c = call("glob", pattern="**/*.txt") if mode == "glob" else call("grep", pattern="MATCH", mode=mode)
    model = FakeModel(reply(None, c), reply("done"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    for name in ("private", "private/*", "blocked.txt"):
        ws.index.add_rule(log.id, policy.Rule("read", str(project / name), "deny"))
    await ws.submit(log, "search")
    [result] = results(log)
    output = text_of(result["content"])
    assert not result["is_error"] and "allowed.txt" in output
    assert all(s not in output for s in ("private", "blocked.txt", "SECRET"))
    if engine == "rg" and mode != "glob":
        assert searched
