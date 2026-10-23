"""Shell hooks in Claude Code's format, run for real through bash."""

import json

import pytest
from conftest import FakeModel, call, reply

from nano_workbuddy.config import DEFAULT_CONFIG, load
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import NOTICE, TOOL_RESULT, USER, text_of


def with_hooks(home, toml: str):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(DEFAULT_CONFIG + "\n" + toml)
    return load(home)


def lit(command: str) -> str:
    """A TOML literal string: no escaping, so shell and JSON quotes stay as written."""
    return f"'''{command}'''"


def results(log):
    return [e.data for e in log.path() if e.type == TOOL_RESULT]


def test_config_errors(home):
    with pytest.raises(ValueError, match="unknown event"):
        with_hooks(home, '[[hooks.BeforeEverything]]\ncommand = "true"\n')
    with pytest.raises(Exception, match="unterminated|missing"):
        with_hooks(home, '[[hooks.PreToolUse]]\nmatcher = "(Bash"\ncommand = "true"\n')


async def test_pre_tool_use_blocks_with_exit_code_2(home, project, tmp_path):
    seen = tmp_path / "payload.json"
    cfg = with_hooks(home, f'''[[hooks.PreToolUse]]
matcher = "Bash"
command = "cat > {seen}; echo 'no rm here' >&2; exit 2"
''')
    model = FakeModel(reply(None, call("bash", command="rm -rf build"), call("read", path="x")), reply("ok"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "clean")
    bash, read = results(log)
    assert bash["is_error"] and "Blocked by a PreToolUse hook: no rm here" in text_of(bash["content"])
    assert "Blocked" not in text_of(read["content"])  # the matcher only picked Bash
    payload = json.loads(seen.read_text())
    assert payload["hook_event_name"] == "PreToolUse" and payload["tool_name"] == "Bash"
    assert payload["tool_input"]["command"] == "rm -rf build" and payload["session_id"] == log.id
    assert payload["cwd"] == str(project) and payload["permission_mode"] == "workspace"


async def test_pre_tool_use_json_decisions(home, project, tmp_path):
    outside = tmp_path / "outside.txt"
    allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    ask = json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}})
    cfg = with_hooks(home, f"""[[hooks.PreToolUse]]
matcher = "Write"
command = {lit(f"echo '{allow}'")}

[[hooks.PreToolUse]]
matcher = "read"
command = {lit(f"echo '{ask}'")}
""")
    model = FakeModel(reply(None, call("write", path=str(outside), content="hi"), call("read", path="a")), reply("ok"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "go")  # no approval channel: anything still at "ask" fails
    write, read = results(log)
    assert outside.read_text() == "hi" and not write["is_error"]  # writing outside the project was allowed
    assert "needs the user's approval" in text_of(read["content"])  # reading was turned into a question


async def test_post_tool_use_feedback_reaches_the_model(home, project):
    ctx = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "formatted"}})
    cfg = with_hooks(home, f"""[[hooks.PostToolUse]]
matcher = "Edit|Write"
command = {lit(f"""grep -q '"file_path"' && echo '{ctx}'""")}

[[hooks.PostToolUse]]
matcher = "Bash"
command = "echo 'lint failed: line 3' >&2; exit 2"
""")
    model = FakeModel(reply(None, call("write", path="a.py", content="x=1\n")),
                      reply(None, call("bash", command="echo hi")), reply("ok"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    await ws.submit(log, "go")
    write, bash = results(log)
    assert text_of(write["content"]).endswith("[Hook feedback]\nformatted")
    assert "hi" in text_of(bash["content"]) and text_of(bash["content"]).endswith("lint failed: line 3")


async def test_user_prompt_submit_can_refuse_or_add_context(home, project):
    cfg = with_hooks(home, """[[hooks.UserPromptSubmit]]
command = "grep -q password && { echo 'do not paste passwords' >&2; exit 2; } || echo 'Today is release day.'"
""")
    model = FakeModel(reply("ok"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    events = []
    assert await ws.submit(log, "my password is hunter2", emit=events.append) == "blocked"
    assert not [e for e in log.path() if e.type == USER] and ws.index.unfinished_runs(log.id) == []
    assert {"type": "inbox", "steer": [], "queue": [], "dropped": ["my password is hunter2"]} in events
    assert "do not paste passwords" in next(e["message"] for e in events if e["type"] == "run.error")

    assert await ws.submit(log, "ship it") == "ok"
    sent = [text_of(m["content"]) for m in model.requests[0]["messages"]]
    assert any("Today is release day." in t for t in sent)
    assert [e.data["kind"] for e in log.path() if e.type == NOTICE].count("hook") == 1


async def test_stop_hook_keeps_the_model_going_once(home, project):
    cfg = with_hooks(home, """[[hooks.Stop]]
command = '''grep -q '"stop_hook_active": false' && { echo 'run the tests first' >&2; exit 2; } || true'''
""")
    model = FakeModel(reply("done"), reply("tests pass, done"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    assert await ws.submit(log, "fix it") == "ok"
    users = [text_of(e.data["content"]) for e in log.path() if e.type == USER]
    assert users == ["fix it", "[Stop hook] run the tests first"]
    assert len(model.requests) == 2


async def test_failing_hooks_are_reported_not_obeyed(home, project):
    cfg = with_hooks(home, """[[hooks.PreToolUse]]
command = "exit 1"

[[hooks.PreToolUse]]
command = "sleep 5"
timeout = 1
""")
    model = FakeModel(reply(None, call("read", path="x")), reply("ok"))
    ws = Workspace(cfg, connect=model.connect)
    log = ws.new_session(project)
    events = []
    assert await ws.submit(log, "go", emit=events.append) == "ok"
    errors = [e["message"] for e in events if e["type"] == "hook.error"]
    assert any("exit 1" in m for m in errors) and any("timed out" in m for m in errors)
    assert "Blocked" not in text_of(results(log)[0]["content"])
