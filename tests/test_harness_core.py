"""Core conversations stay usable as optional capabilities and mutable context change."""

import dataclasses

import pytest
from conftest import FakeModel, call, reply
from test_api import Server
from test_skills_memory import load_skill, make_skill

from nano_workbuddy import plugins, policy, settings
from nano_workbuddy.config import load
from nano_workbuddy.context import pinned
from nano_workbuddy.log import SessionLog
from nano_workbuddy.plugins import Plugin
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools import Result, ToolCtx, tool
from nano_workbuddy.tools.builtin import Args
from nano_workbuddy.types import CONTEXT_STATE, TOOL_RESULT, USER, text, text_of


async def test_core_only_chat_and_plugin_switch(home, project):
    cfg = dataclasses.replace(load(home), plugins_enabled=())
    model = FakeModel(reply("hello"), reply("again"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "hello")
    names = {t["name"] for t in model.requests[-1]["tools"]}
    assert {"read", "write", "edit", "bash", "memory", "recall", "todo", "ask_user"} <= names
    assert not names & {"feishu", "web_search", "web_fetch", "create_document", "export_document"}
    assert "# Research" not in model.requests[-1]["system"]
    async with Server(ws) as server:
        view = (await server.client.get("/api/settings")).json()
        assert all(not p["enabled"] for p in view["plugins"])
        response = await server.client.patch("/api/settings/plugins/documents", json={"enabled": False})
        assert response.status_code == 200
        response = await server.client.patch("/api/settings/plugins/documents", json={"enabled": True})
        assert response.status_code == 200
        await ws.submit(log, "again")
        assert "create_document" in {t["name"] for t in model.requests[-1]["tools"]}


@pytest.mark.parametrize("preset,executed", [("workspace", True), ("readonly", False)])
async def test_injected_plugin_uses_same_permissions_and_cleanup(home, project, preset, executed):
    class SaveArgs(Args):
        content: str

    @tool(effect="write", scope=lambda args, ctx: ("write", str(ctx.root / "note.txt")))
    async def add_note(args: SaveArgs, ctx: ToolCtx) -> Result:
        (ctx.root / "note.txt").write_text(ctx.services["prefix"] + args.content)
        return Result.ok("saved")

    cleanups = []
    plugin = Plugin("notes", "A local notes capability", (add_note,), services={"prefix": "note: "},
                    cleanup=cleanups.append)
    model = FakeModel(reply(None, call("add_note", content="hello")), reply("done"))
    cfg = dataclasses.replace(load(home), plugins_enabled=())
    ws = Workspace(cfg, connect=model.connect, extensions=(plugin,))
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": preset})
    await ws.submit(log, "save a note")
    assert (project / "note.txt").exists() is executed
    result = next(e for e in log.path() if e.type == TOOL_RESULT)
    assert result.data["is_error"] is not executed
    assert cleanups == [log.id]


async def test_broken_optional_plugin_does_not_break_conversation(home, project):
    model = FakeModel(reply("still available"))
    cfg = dataclasses.replace(load(home), plugins_enabled=("missing-plugin-for-test",))
    ws = Workspace(cfg, connect=model.connect)
    assert await ws.submit(ws.new_session(project), "hi") == "ok"
    assert any("unavailable" in text_of(m["content"]) for m in model.requests[-1]["messages"])


async def test_current_memory_replaces_snapshot_without_mutating_history(home, project, make_ws):
    home.mkdir(parents=True)
    (home / "USER.md").write_text("prefers long answers")
    model = FakeModel(reply("one"), reply("two"), reply("three"))
    ws = make_ws(model)
    log = ws.new_session(project)
    initial = dict(log.path()[0].data)
    await ws.submit(log, "first")
    (home / "USER.md").write_text("prefers concise answers")
    await ws.submit(log, "second")
    assert "prefers concise answers" in model.requests[-1]["system"]
    assert "prefers long answers" not in model.requests[-1]["system"]
    notices = [text_of(m["content"]) for m in model.requests[-1]["messages"]
               if "Saved context was refreshed at this point" in text_of(m["content"])]
    assert len(notices) == 1 and "prefers concise answers" in notices[0]
    (home / "USER.md").unlink()
    await ws.submit(log, "third")
    assert "prefers concise answers" not in model.requests[-1]["system"]
    notices = [text_of(m["content"]) for m in model.requests[-1]["messages"]
               if "Saved context was refreshed at this point" in text_of(m["content"])]
    assert len(notices) == 1 and "No saved user memory or project rules" in notices[0]
    assert log.path()[0].data == initial
    assert any(e.type == CONTEXT_STATE for e in log.path())


@pytest.mark.parametrize("change", ["disable", "delete", "update"])
async def test_loaded_skill_lifecycle_changes_model_context(home, project, make_ws, change):
    folder = make_skill(home / "skills", "sample", body="OLD_SKILL_BODY")
    model = FakeModel(reply(None, load_skill("sample")), reply("loaded"), reply("next"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "use sample")
    if change == "disable":
        ws.cfg = settings.set_skill_enabled(home, "sample", False)
    elif change == "delete":
        (folder / "SKILL.md").unlink()
    else:
        file = folder / "SKILL.md"
        file.write_text(file.read_text().replace("OLD_SKILL_BODY", "NEW_SKILL_BODY"))
    await ws.submit(log, "continue")
    sent = "\n".join(text_of(m["content"]) for m in model.requests[-1]["messages"])
    assert "OLD_SKILL_BODY" not in sent
    assert ("NEW_SKILL_BODY" in sent) is (change == "update")
    # Raw history still retains what was actually loaded.
    assert any(e.type == TOOL_RESULT and "OLD_SKILL_BODY" in text_of(e.data["content"]) for e in log.path())


def test_untrusted_project_rules_do_not_become_system_instructions(home, project, make_ws):
    (project / "AGENTS.md").write_text("UNTRUSTED_PROJECT_INSTRUCTIONS")
    ws = make_ws(FakeModel())
    log = ws.new_session(project, trusted=False)
    assert "UNTRUSTED_PROJECT_INSTRUCTIONS" not in log.path()[0].data["text"]


def test_cached_session_observes_external_append_without_repairing_tail(home, project, make_ws):
    ws = make_ws(FakeModel())
    log = ws.new_session(project)
    other = SessionLog.open(log.file)
    other.append(USER, {"content": [text("from another process")]})
    with log.file.open("ab") as f:
        f.write(b'{"partial":')
    before = log.file.read_bytes()
    assert ws.open_session(log.id) is log
    assert text_of(log.path()[-1].data["content"]) == "from another process"
    assert log.file.read_bytes() == before


async def test_branch_switch_discards_reads_from_abandoned_branch(home, project, make_ws):
    (project / "note.txt").write_text("original")
    model = FakeModel(reply(None, call("read", path="note.txt")), reply("read"),
                      reply(None, call("edit", path="note.txt", old_string="original", new_string="changed")),
                      reply("done"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "read")
    first = next(e for e in log.path() if e.type == USER)
    ws.checkout(log, first.id)
    await ws.submit(log, "edit")
    assert (project / "note.txt").read_text() == "original"
    assert any(e.type == TOOL_RESULT and "before modifying" in text_of(e.data["content"]) for e in log.path())


async def test_removing_last_mcp_server_stops_connection(home, project, make_ws):
    ws = make_ws(FakeModel())

    class StaleServer:
        spec = object()
        stopped = False

        async def stop(self):
            self.stopped = True

    old = StaleServer()
    ws.mcp.servers["removed"] = old
    await ws.toolset(ws.cfg.model(), project)
    assert old.stopped and not ws.mcp.servers


async def test_available_feishu_does_not_preflight_identity_in_plain_chat(home, project, monkeypatch):
    class Connector:
        available = True
        async def status(self):
            pytest.fail("ordinary chat must not preflight an office account")

    monkeypatch.setattr(plugins, "feishu_client", lambda cfg, state: Connector())
    model = FakeModel(reply("hello"))
    ws = Workspace(load(home), connect=model.connect)
    assert await ws.submit(ws.new_session(project), "hello") == "ok"


async def test_custom_permission_rules_are_visible_and_still_enforced(home, project):
    model = FakeModel(reply(None, call("bash", command="touch denied.txt")), reply("blocked"))
    cfg = dataclasses.replace(load(home), plugins_enabled=(),
                              rules=(policy.Rule("bash", "*", "deny"),))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    try:
        assert await ws.submit(log, "run the command") == "ok"
        shown = "\n".join(text_of(m["content"]) for m in model.requests[0]["messages"])
        assert '"action": "bash", "resource": "*", "effect": "deny"' in shown
        assert "override the preset defaults" in shown
        assert not (project / "denied.txt").exists()
        assert next(e for e in log.path() if e.type == TOOL_RESULT).data["is_error"]
    finally:
        await ws.mcp.close()
        log.close()
        if ws._knowledge:
            ws._knowledge.db.close()
        ws.index.db.close()


@pytest.mark.parametrize("change", ["update", "disable", "delete"])
async def test_compacted_skill_and_memory_refresh_after_workspace_restart(home, project, change):
    """A compressed old skill must not revive after restart or shadow current files."""
    folder = make_skill(home / "skills", "sample", body="OLD_SKILL_BODY")
    (home / "USER.md").write_text("OLD_USER_PREFERENCE")
    (project / "AGENTS.md").write_text("OLD_PROJECT_RULE")
    model = FakeModel(reply(None, load_skill("sample")), reply("loaded"), reply("second"))
    ws = Workspace(load(home), connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "load sample")
    await ws.submit(log, "another turn")
    assert await ws.compact(log) == "ok"
    before = log.file.read_bytes()
    await ws.mcp.close()
    log.close()
    if ws._knowledge:
        ws._knowledge.db.close()
    ws.index.db.close()

    (home / "USER.md").unlink()
    (project / "AGENTS.md").write_text("CURRENT_PROJECT_RULE")
    if change == "update":
        f = folder / "SKILL.md"
        f.write_text(f.read_text().replace("OLD_SKILL_BODY", "CURRENT_SKILL_BODY"))
    elif change == "disable":
        settings.set_skill_enabled(home, "sample", False)
    else:
        (folder / "SKILL.md").unlink()

    continued = FakeModel(reply("continued"))
    reopened = Workspace(load(home), connect=continued.connect)
    log = reopened.open_session(log.id)
    try:
        assert await reopened.submit(log, "continue") == "ok"
        request = continued.requests[-1]
        shown = "\n".join(text_of(m["content"]) for m in request["messages"])
        assert "CURRENT_PROJECT_RULE" in request["system"]
        assert "OLD_PROJECT_RULE" not in request["system"]
        assert "OLD_USER_PREFERENCE" not in request["system"]
        assert "OLD_SKILL_BODY" not in shown
        assert ("CURRENT_SKILL_BODY" in shown) is (change == "update")
        assert pinned(log.path())["skill:sample"] in shown
        assert log.file.read_bytes().startswith(before)
        # Compact away that refresh event too: the current-context notice must be
        # reconstructed once after the summary, even when files no longer change.
        assert await reopened.compact(log) == "ok"
        continued.script.append(reply("after compaction"))
        assert await reopened.submit(log, "continue after compaction") == "ok"
        shown = "\n".join(text_of(m["content"]) for m in continued.requests[-1]["messages"])
        assert shown.count("Saved context was refreshed at this point") == 1
        assert "CURRENT_PROJECT_RULE" in shown
        assert "OLD_USER_PREFERENCE" not in shown
    finally:
        await reopened.mcp.close()
        log.close()
        if reopened._knowledge:
            reopened._knowledge.db.close()
        reopened.index.db.close()
