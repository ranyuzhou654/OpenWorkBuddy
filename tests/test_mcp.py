"""MCP servers through the real SDK and a real stdio server (tests/fixtures/mcp_server.py)."""

import sys
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply

from nano_workbuddy import settings
from nano_workbuddy.config import McpServer, SecretStore, load
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools.registry import Registry
from nano_workbuddy.types import NOTICE, TOOL_RESULT, text_of

SERVER = str(Path(__file__).parent / "fixtures" / "mcp_server.py")
ALLOW = '\n[[rules]]\naction = "mcp__demo__*"\nresource = "*"\neffect = "allow"\n'


def configure(home, *extra_args, allow=True, **fields):
    """Default config plus one stdio server called demo."""
    load(home)
    spec = McpServer("demo", sys.executable, (SERVER, *extra_args), sandbox=False, **fields)
    settings.put_mcp(home, spec)
    if allow:
        with open(home / "config.toml", "a") as f:
            f.write(ALLOW)
    return load(home)


@pytest.fixture
async def workspace(home):
    made = []

    def make(model, cfg=None):
        ws = Workspace(cfg or load(home), connect=model.connect)
        made.append(ws)
        return ws

    yield make
    for ws in made:
        await ws.mcp.close()


def results(log):
    return [e.data for e in log.path() if e.type == TOOL_RESULT]


def tool_names(request):
    return [t["name"] for t in request["tools"]]


async def test_direct_tools_are_declared_and_called(home, project, workspace):
    model = FakeModel(reply(None, call("mcp__demo__add", a=2, b=3), call("mcp__demo__pixel")), reply("done"))
    ws = workspace(model, configure(home))
    log = ws.new_session(project)
    assert await ws.submit(log, "add") == "ok"

    assert {"mcp__demo__add", "mcp__demo__echo", "read"} <= set(tool_names(model.requests[0]))
    assert "tool_search" not in tool_names(model.requests[0])
    add, pixel = results(log)
    assert text_of(add["content"]) == "5" and not add["is_error"]
    assert pixel["content"][0]["type"] == "image" and pixel["content"][0]["mime"] == "image/png"
    notice = next(e.data["text"] for e in log.path() if e.type == NOTICE and e.data["kind"] == "mcp")
    assert "demo (5 tools, tools declared directly): Demo tools for tests." in notice


async def test_mcp_tools_need_approval_like_any_other_tool(home, project, workspace):
    model = FakeModel(reply(None, call("mcp__demo__echo", text="hi")), reply("ok"))
    ws = workspace(model, configure(home, allow=False))
    log = ws.new_session(project)
    await ws.submit(log, "go")
    assert "needs the user's approval" in text_of(results(log)[0]["content"])

    seen = []

    async def ask(check, tool, c):
        seen.append((check.action, check.resource, check.suggestion()))
        return None

    model.script = [reply(None, call("mcp__demo__echo", text="hi")), reply("ok")]
    await ws.submit(log, "again", ask=ask)
    action, resource, always = seen[0]
    assert (action, resource) == ("mcp__demo__echo", "")
    assert (always.action, always.resource) == ("mcp__demo__echo", "")  # "always" allows this tool, any arguments
    assert text_of(results(log)[-1]["content"]) == "hi"


async def test_deferred_tools_load_through_tool_search(home, project, workspace):
    model = FakeModel(reply(None, call("tool_search", query="add integers")),
                      reply(None, call("mcp__demo__add", a=1, b=1)), reply("2"))
    ws = workspace(model, configure(home, exposure="deferred"))
    log = ws.new_session(project)
    assert await ws.submit(log, "add") == "ok"

    first, second, _ = model.requests
    assert "tool_search" in tool_names(first) and not any(n.startswith("mcp__") for n in tool_names(first))
    assert tool_names(second)[-1] == "mcp__demo__add"  # loaded tools are appended: the prefix stays cached
    assert results(log)[0]["meta"] == {"load": ["mcp__demo__add"]}
    assert text_of(results(log)[1]["content"]) == "2"
    notice = next(e.data["text"] for e in log.path() if e.type == NOTICE and e.data["kind"] == "mcp")
    assert "deferred: load its tools with tool_search" in notice

    model.script = [reply("still here")]  # the next run keeps what this session loaded
    await ws.submit(log, "again")
    assert "mcp__demo__add" in tool_names(model.requests[-1])


async def test_auto_exposure_defers_large_servers(home, project, workspace):
    configure(home, "60")
    cfg = settings.put_model(home, "small", "deepseek", "small", 200_000, 8_000)  # 60 bulky tools > 5% of this
    model = FakeModel(reply("ok"))
    ws = workspace(model, cfg)
    log = ws.new_session(project, "small")
    await ws.submit(log, "hi")
    assert "tool_search" in tool_names(model.requests[0])
    assert not any(n.startswith("mcp__") for n in tool_names(model.requests[0]))


async def test_disabled_server_is_gone_from_the_next_run(home, project, workspace):
    model = FakeModel(reply("one"), reply("two"))
    ws = workspace(model, configure(home))
    log = ws.new_session(project)
    await ws.submit(log, "hi")
    assert "mcp__demo__echo" in tool_names(model.requests[0])

    ws.cfg = settings.set_mcp_enabled(home, "demo", False)
    await ws.submit(log, "again")
    assert not any(n.startswith("mcp__") for n in tool_names(model.requests[1]))
    assert "demo" not in ws.mcp.servers  # its process was stopped
    notices = [e.data["text"] for e in log.path() if e.type == NOTICE and e.data["kind"] == "mcp"]
    assert notices[-1] == "No MCP servers are enabled."


async def test_a_broken_server_does_not_break_the_run(home, project, workspace):
    load(home)
    settings.put_mcp(home, McpServer("ghost", "/nonexistent/server", sandbox=False))
    model = FakeModel(reply("fine"))
    ws = workspace(model, load(home))
    log = ws.new_session(project)
    assert await ws.submit(log, "hi") == "ok"
    notice = next(e.data["text"] for e in log.path() if e.type == NOTICE and e.data["kind"] == "mcp")
    assert "ghost: unavailable" in notice and "can" in notice
    assert ws.mcp.status(ws.cfg.mcp)["ghost"]["status"] == "error"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt sandbox")
async def test_stdio_servers_run_in_the_sandbox_with_secrets_resolved(home, project, workspace, tmp_path):
    load(home)
    spec = McpServer("demo", sys.executable, (SERVER,), env={"NWB_TEST_SECRET": "tok-123"})
    settings.put_mcp(home, spec, secret_keys=("NWB_TEST_SECRET",))
    assert "tok-123" not in (home / "config.toml").read_text()
    assert SecretStore(home).get("mcp-demo-NWB_TEST_SECRET") == "tok-123"
    with open(home / "config.toml", "a") as f:
        f.write(ALLOW)

    outside = Path.home() / f".nwb-mcp-test-{tmp_path.name}"
    model = FakeModel(reply(None, call("mcp__demo__whoami"), call("mcp__demo__touch", path=str(outside))),
                      reply("ok"))
    ws = workspace(model, load(home))
    log = ws.new_session(project)
    await ws.submit(log, "go")
    whoami, touch = results(log)
    assert "secret=tok-123" in text_of(whoami["content"])
    assert touch["is_error"] and not outside.exists()


def test_settings_mcp_round_trip(home):
    load(home)
    (home / "config.toml").write_text("# keep me\n" + (home / "config.toml").read_text())
    spec = McpServer("gh", url="https://example.com/mcp", headers={"Authorization": "Bearer abc"})
    cfg = settings.put_mcp(home, spec, secret_keys=("Authorization",))
    text = (home / "config.toml").read_text()
    assert "# keep me" in text and "Bearer abc" not in text
    assert cfg.mcp["gh"].headers == {"Authorization": "secret:mcp-gh-Authorization"}

    # editing without touching the key keeps the reference; renaming the header drops the old secret
    cfg = settings.put_mcp(home, McpServer("gh", url="https://example.com/mcp",
                                           headers={"Authorization": "secret:mcp-gh-Authorization"}))
    assert SecretStore(home).get("mcp-gh-Authorization") == "Bearer abc"
    settings.put_mcp(home, McpServer("gh", url="https://example.com/mcp", headers={"X-Token": "t"}),
                     secret_keys=("X-Token",))
    assert SecretStore(home).names() == ["mcp-gh-X-Token"]
    cfg = settings.delete_mcp(home, "gh")
    assert "gh" not in cfg.mcp and SecretStore(home).names() == []
    with pytest.raises(settings.SettingsError):
        settings.put_mcp(home, McpServer("bad", command="x", url="https://y"))


def test_registry_search_and_declaration_order():
    from nano_workbuddy.tools import bash, edit, read, write

    reg = Registry([read, write, edit, bash], deferred=["edit", "bash"])
    assert [s["name"] for s in reg.schemas()] == ["read", "write"]
    assert [s["name"] for s in reg.schemas(["bash", "edit", "bash"])] == ["read", "write", "bash", "edit"]
    assert [t.name for t in reg.search("shell command")] == ["bash"]
    assert [t.name for t in reg.search("select:edit")] == ["edit"]
    assert reg.search("image") == []  # only read mentions images, and direct tools are not search results


async def test_settings_api_for_mcp(home):
    from test_api import Server as ApiServer

    load(home)
    ws = Workspace(load(home))
    async with ApiServer(ws) as s:
        c = s.client
        body = {"command": sys.executable, "args": [SERVER], "env": {"NWB_TEST_SECRET": "s3cr3t-value"},
                "secret_keys": ["NWB_TEST_SECRET"], "sandbox": False}
        r = await c.put("/api/settings/mcp/demo", json=body)
        assert r.status_code == 200 and "s3cr3t" not in r.text
        demo = next(m for m in r.json()["mcp"] if m["name"] == "demo")
        assert demo["status"] == "idle" and demo["keys"]["NWB_TEST_SECRET"]["hint"] == "…alue"

        view = (await c.post("/api/settings/mcp/demo/connect")).json()
        demo = next(m for m in view["mcp"] if m["name"] == "demo")
        assert demo["status"] == "connected" and {t["name"] for t in demo["tools"]} >= {"echo", "add"}

        view = (await c.patch("/api/settings/mcp/demo", json={"enabled": False})).json()
        assert next(m for m in view["mcp"] if m["name"] == "demo")["status"] == "disabled"
        bad = await c.put("/api/settings/mcp/x", json={"command": "a", "url": "https://b"})
        assert bad.status_code == 400
        assert (await c.delete("/api/settings/mcp/demo")).status_code == 200
    assert ws.mcp.servers == {}  # closed on shutdown
