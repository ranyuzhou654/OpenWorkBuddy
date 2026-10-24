"""Feishu through lark-cli and the browser through Playwright MCP (M2), with a fake lark-cli and fake tools."""

import json
import stat
import sys
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply
from test_api import Server, Stream, finished

from nano_workbuddy import policy
from nano_workbuddy.config import load
from nano_workbuddy.connectors import browser
from nano_workbuddy.connectors.feishu import Feishu, _confine, _key, _recover, classify, feishu
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools.registry import AnyArgs, Result, Tool, ToolCtx, ToolError
from nano_workbuddy.types import TOOL_RESULT

FIXTURE = Path(__file__).parent / "fixtures" / "fake_lark.py"


@pytest.fixture
def lark(tmp_path):
    """An executable fake lark-cli."""
    exe = tmp_path / "bin" / "lark-cli"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n" + FIXTURE.read_text())
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def enable(home, lark, preset_extra=""):
    load(home)
    with open(home / "config.toml", "a") as f:
        f.write(f'\n[feishu]\nenabled = true\ncli = "{lark}"\n{preset_extra}')
    return load(home)


def calls(project):
    f = project / ".lark-calls.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def ran(project):
    """Calls that did something (not --help, auth status or --version)."""
    return [c for c in calls(project) if "--help" not in c and c[:2] != ["auth", "status"] and c != ["--version"]]


def test_classify_refuses_what_is_never_allowed():
    assert classify(["docs", "+fetch", "--doc", "x"]) == ("docs", "+fetch", False)
    assert classify(["im", "+messages-send", "--chat-id", "oc_1", "--text", "hi"])[2] is True
    assert classify(["im", "+messages-send", "--chat-id", "oc_1", "--dry-run"])[2] is False  # a preview reads
    for argv, why in ((["mail", "+send"], "not enabled"), (["api", "GET", "/x"], "not enabled"),
                      (["im", "messages", "create"], "shortcuts"), (["docs", "+fetch", "--as", "bot"], "--as"),
                      (["docs", "+fetch", "--profile=other"], "--profile"), (["docs"], "at least")):
        with pytest.raises(ToolError, match=why):
            classify(argv)


def test_local_files_stay_inside_the_project(project, tmp_path):
    ctx = ToolCtx(root=project, session_id="s", spill_dir=tmp_path / "spill")
    (project / "brief.md").write_text("x")
    _confine(["docs", "+create", "--content", "@brief.md"], ctx)
    _confine(["im", "+messages-send", "--image", "img_v2_abc"], ctx)  # a key, not a path
    for argv in (["docs", "+create", "--content", "@../../etc/passwd"], ["drive", "+import", "--file", "/etc/hosts"],
                 ["docs", "+create", "--content=@~/.ssh/id_rsa"], ["docs", "+update", "--content", "-"]):
        with pytest.raises(ToolError):
            _confine(argv, ctx)


def test_replay_and_idempotency_keys():
    send = feishu.args(command=["im", "+messages-send", "--chat-id", "oc_1", "--text", "hi"])
    create = feishu.args(command=["docs", "+create", "--title", "T"])
    read = feishu.args(command=["docs", "+fetch", "--doc", "d"])
    ctx = ToolCtx(root=Path("/"), session_id="s", spill_dir=Path("/tmp"))
    assert (_recover(send, ctx), _recover(create, ctx), _recover(read, ctx)) == ("not_done", "unknown", "not_done")
    one = ToolCtx(root=Path("/"), session_id="s", spill_dir=Path("/tmp"), call_key="e_1:call_0")
    again = ToolCtx(root=Path("/"), session_id="s", spill_dir=Path("/tmp"), call_key="e_1:call_0")
    later = ToolCtx(root=Path("/"), session_id="s", spill_dir=Path("/tmp"), call_key="e_9:call_0")  # call_0 reused
    argv = send.command
    assert _key(one, argv) == _key(again, argv) != _key(later, argv) and len(_key(one, argv)) == 40


async def test_reads_run_as_the_user_and_cite_sources(home, project, lark):
    model = FakeModel(reply(None, call("feishu", command=["docs", "+fetch", "--doc", "https://x.feishu.cn/docx/d1"])),
                      reply("done"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    assert await ws.submit(log, "read it") == "ok"  # no approval needed (no ask callback given)
    assert "feishu" in [t["name"] for t in model.requests[0]["tools"]]
    notice = next(e.data["text"] for e in log.path() if e.type == "notice" and e.data["kind"] == "feishu")
    assert "as Tester" in notice and "Never run lark-cli through bash" in notice
    result = next(e.data for e in log.path() if e.type == TOOL_RESULT)
    assert not result["is_error"] and result["content"][0]["text"].startswith("[S1] Feishu document")
    assert result["meta"]["sources"][0]["title"] == "季度计划"
    assert ran(project) == [["docs", "+fetch", "--doc", "https://x.feishu.cn/docx/d1", "--as", "user"]]


@pytest.mark.parametrize("preset", ["workspace", "trust"])
async def test_writes_are_approved_per_call_with_a_dry_run(home, project, lark, preset):
    send = ["im", "+messages-send", "--chat-id", "oc_team", "--markdown", "周报已发布"]
    model = FakeModel(reply(None, call("feishu", command=send)), reply("sent"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": preset})
    seen = []

    async def ask(check, tool, c):
        seen.append(check)
        return None  # approve once

    await ws.submit(log, "send it", ask=ask)
    [check] = seen  # asked even in the full-trust preset
    assert check.action == "feishu.write" and check.once_only and "chat-id=oc_team" in check.resource
    assert check.preview["account"] == "Tester" and check.preview["dry_run"]["dry_run"] is True
    executed = ran(project)[-1]
    assert executed[:6] == send and executed[-2:] == ["--as", "user"] and "--idempotency-key" in executed
    receipt = next(e.data["meta"]["receipt"] for e in log.path() if e.type == TOOL_RESULT)
    assert receipt["target"] == "chat-id=oc_team" and receipt["links"] == ["https://x.feishu.cn/docx/abc"]


async def test_rejected_write_is_not_executed(home, project, lark):
    model = FakeModel(reply(None, call("feishu", command=["docs", "+create", "--title", "T"])), reply("ok"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)

    async def ask(check, tool, c):
        return Result.error("The user denied this call.")

    await ws.submit(log, "make a doc", ask=ask)
    assert not [c for c in ran(project) if "--dry-run" not in c]


@pytest.mark.parametrize(("argv", "why"), [
    (["drive", "+delete", "--file-token", "x"], "high-risk"),
    (["calendar", "+create", "--summary", "x"], "not among the enabled writes"),
    (["docs", "+nonsense"], "unknown command"),
])
async def test_refused_commands_never_run(home, project, lark, argv, why):
    model = FakeModel(reply(None, call("feishu", command=argv)), reply("ok"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": "trust"})
    await ws.submit(log, "do it")
    result = next(e.data for e in log.path() if e.type == TOOL_RESULT)
    assert result["is_error"] and why in result["content"][0]["text"]
    assert ran(project) == []


async def test_expired_login_tells_the_model_what_the_user_must_do(home, project, lark):
    model = FakeModel(reply(None, call("feishu", command=["docs", "+fetch", "--doc", "expired"])), reply("ok"))
    ws = Workspace(enable(home, lark), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "read")
    text = next(e.data for e in log.path() if e.type == TOOL_RESULT)["content"][0]["text"]
    assert "lark-cli auth login" in text and "Do not try to log in yourself" in text


def test_lark_cli_through_bash_always_asks(home, project):
    ws = Workspace(load(home), connect=FakeModel().connect)
    log = ws.new_session(project)
    places = {"root": str(project), "home": str(home), "spill": "/s"}
    for preset in ("workspace", "trust"):
        rules = ws.rules(log, preset)
        assert policy.check("bash", "lark-cli im +messages-send --text hi", rules, places).effect == "ask"
        assert policy.check("feishu.write", "im +messages-send chat-id=oc", rules, places).effect == "ask"
    assert policy.check("feishu", "docs +fetch", ws.rules(log, "readonly"), places).effect == "allow"


async def test_feishu_disabled_or_missing(home, project, tmp_path):
    model = FakeModel(reply("ok"), reply("ok"))
    ws = Workspace(load(home), connect=model.connect)
    await ws.submit(ws.new_session(project), "hi")
    assert "feishu" not in [t["name"] for t in model.requests[0]["tools"]]
    ws = Workspace(enable(home, tmp_path / "nowhere" / "lark-cli"), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "hi")
    assert "feishu" not in [t["name"] for t in model.requests[1]["tools"]]
    notices = [e.data["text"] for e in log.path() if e.type == "notice" and e.data["kind"] == "feishu"]
    assert "was not found" in notices[0]


async def test_api_approval_has_no_always_and_status_endpoint(home, project, lark):
    model = FakeModel(reply(None, call("feishu", command=["im", "+messages-reply", "--message-id", "om_1",
                                                          "--text", "收到"])), reply("ok"))
    async with Server(Workspace(enable(home, lark), connect=model.connect)) as s:
        status = (await s.client.get("/api/connectors/feishu")).json()
        assert status["available"] and status["version"] == "9.9.9" and status["user"]["name"] == "Tester"
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "reply"})
        req = (await stream.until(lambda e: e["type"] == "approval.requested"))["approval"]
        assert req["always"] is None and req["preview"]["target"] == "message-id=om_1"
        await s.client.post(f"/api/approvals/{req['id']}", json={"decision": "always"})  # counts as once
        await stream.until(finished)
        stream.close()
        assert s.ws.index.session_rules(sid) == []
        r = await s.client.put("/api/settings/feishu", json={"enabled": False})
        assert r.json()["feishu"]["enabled"] is False


# --- browser ---


def fake_tool(name, effect="exec"):
    async def fn(args, ctx):
        return Result.ok(f"{name} ran")

    return Tool(f"mcp__browser__{name}", name, AnyArgs, fn, effect, False, None,
                lambda args, ctx, n=name: (f"mcp__browser__{n}", ""))


def test_browser_spec_uses_its_own_profile(home):
    s = browser.spec(home)
    assert s.command == "npx" and browser.PACKAGE in s.args and not s.sandbox
    assert str(home / "browser" / "profile") in s.args and "--headless" not in s.args
    assert "--headless" in browser.spec(home, headless=True).args


async def test_browser_tools_wrapped_with_lease_and_site_scope(project, tmp_path):
    lease, running = browser.Lease(), set()
    tools = {t.name: t for t in browser.wrap([fake_tool(n) for n in ("browser_navigate", "browser_click",
                                                                      "browser_run_code_unsafe")], lease, running)}
    assert "mcp__browser__browser_run_code_unsafe" not in tools
    nav = tools["mcp__browser__browser_navigate"]
    args = AnyArgs.model_validate({"url": "https://mail.example.com/inbox"})
    a = ToolCtx(root=project, session_id="s_a", spill_dir=tmp_path)
    assert nav.permission_scope(args, a) == ("mcp__browser__browser_navigate", "https://mail.example.com/inbox")
    check = policy.check(*nav.permission_scope(args, a), list(policy.PRESETS["workspace"].rules), {})
    assert check.effect == "ask" and check.suggestion().resource == "https://mail.example.com/*"

    running.add("s_a")
    assert (await nav.fn(args, a)).content[0]["text"] == "browser_navigate ran" and lease.owner == "s_a"
    b = ToolCtx(root=project, session_id="s_b", spill_dir=tmp_path)
    with pytest.raises(ToolError, match="being used by another session"):
        await tools["mcp__browser__browser_click"].fn(args, b)
    running.discard("s_a")  # a's run ended without releasing: b may take over
    await tools["mcp__browser__browser_click"].fn(args, b)
    assert lease.owner == "s_b"


def test_browser_permissions_by_what_a_tool_does():
    rules, places = list(policy.PRESETS["workspace"].rules), {}
    effect = lambda name: policy.check(f"mcp__browser__{name}", "", rules, places).effect  # noqa: E731
    assert effect("browser_snapshot") == effect("browser_take_screenshot") == "allow"
    assert effect("browser_click") == effect("browser_type") == effect("browser_evaluate") == "ask"
    assert effect("browser_file_upload") == "ask"


def test_browser_enabled_adds_the_built_in_server(home):
    ws = Workspace(load(home), connect=FakeModel().connect)
    assert "browser" not in ws.mcp_specs()
    with open(home / "config.toml", "a") as f:
        f.write("\n[browser]\nenabled = true\nheadless = true\n")
    ws.cfg = load(home)
    assert ws.mcp_specs()["browser"].args[-1] == "--headless"
    with open(home / "config.toml", "a") as f:
        f.write('\n[feishu]\nenabled = "yes"\n')
    with pytest.raises(ValueError, match="must be a bool"):
        load(home)


def test_feishu_without_cli(tmp_path):
    assert not Feishu(str(tmp_path / "missing")).available
