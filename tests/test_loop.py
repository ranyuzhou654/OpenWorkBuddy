"""End-to-end loop behaviour through Workspace, with a scripted model."""

import asyncio

from conftest import HANG, FakeModel, call, reply

from nano_workbuddy.log import SessionLog
from nano_workbuddy.loop import CANCELLED, RECOVERED, TRUNCATED, UNKNOWN
from nano_workbuddy.tools import Result
from nano_workbuddy.types import ASSISTANT, TOOL_RESULT, TOOL_STARTED, USER, text_of


def results(log):
    return [e.data for e in log.path() if e.type == TOOL_RESULT]


def types(log):
    return [e.type for e in log.path()]


def roles(messages):
    """Message roles, with harness notices marked so tests can see where they land."""
    return ["notice" if m["role"] == "user" and text_of(m["content"]).startswith("<system-notice>") else m["role"]
            for m in messages]


async def test_multi_step_file_task(make_ws, project):
    model = FakeModel(
        reply("writing", call("write", path="notes.md", content="# hi\n")),
        reply(None, call("read", path="notes.md")),
        lambda msgs: reply("done: " + text_of(msgs[-1]["content"]).split("\t")[1]),
    )
    ws = make_ws(model)
    log = ws.new_session(project)
    events = []
    status = await ws.submit(log, "make notes", emit=events.append)

    assert status == "ok"
    assert (project / "notes.md").read_text() == "# hi\n"
    assert types(log) == ["system_prompt", "user", "notice", "assistant", "tool_started", "tool_result", "assistant",
                          "tool_started", "tool_result", "assistant"]
    assert "Permission preset: workspace" in log.path()[2].data["text"]
    assert text_of(log.path()[-1].data["content"]) == "done: # hi"
    assert model.requests[0]["system"].startswith("You are nano-workbuddy")
    assert [t["name"] for t in model.requests[0]["tools"]] == [
        "read", "write", "edit", "bash", "grep", "glob", "web_fetch", "todo", "ask_user", "memory"]
    assert {"run.started", "assistant.delta", "run.finished"} <= {e["type"] for e in events}
    run = ws.index.unfinished_runs(log.id)
    assert run == []
    assert ws.index.session(log.id)["title"] == "make notes"


async def test_truncated_tool_calls_are_not_executed(make_ws, project):
    model = FakeModel(reply(None, call("write", path="x", content="half"), stop="length"), reply("retrying"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "go")
    assert not (project / "x").exists()
    assert results(log)[0]["is_error"] and TRUNCATED in text_of(results(log)[0]["content"])
    assert TOOL_STARTED not in types(log)


async def test_unknown_tool_and_tool_errors_go_back_to_the_model(make_ws, project):
    model = FakeModel(reply(None, call("nope"), call("read", path="missing")), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    assert await ws.submit(log, "go") == "ok"
    assert [r["is_error"] for r in results(log)] == [True, True]
    tool_msgs = [m for m in model.requests[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2


async def test_parallel_reads_run_concurrently(make_ws, project):
    for n in "abc":
        (project / n).write_text(n)
    model = FakeModel(reply(None, *(call("read", path=n) for n in "abc")), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "go")
    assert sorted(text_of(r["content"]).split("\t")[1] for r in results(log)) == ["a", "b", "c"]


async def test_cancel_during_tool_answers_every_call(make_ws, project):
    model = FakeModel(reply(None, call("bash", command="sleep 30"), call("bash", command="echo never")))
    ws = make_ws(model)
    log = ws.new_session(project)
    task = asyncio.create_task(ws.submit(log, "go"))
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert [CANCELLED in text_of(r["content"]) for r in results(log)] == [True, True]
    assert ws.index.unfinished_runs(log.id) == []
    assert ws.index._one("select status from runs")["status"] == "cancelled"


async def test_cancel_while_streaming_keeps_the_visible_text(make_ws, project):
    ws = make_ws(FakeModel(HANG))
    log = ws.new_session(project)
    task = asyncio.create_task(ws.submit(log, "go"))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    last = log.path()[-1]
    assert last.type == ASSISTANT and last.data["stop"] == "aborted"
    assert text_of(last.data["content"]) == "partial answer"


async def test_step_limit_wraps_up_without_tools(make_ws, project, home):
    looping = [reply(None, call("bash", command="true")) for _ in range(3)]
    model = FakeModel(*looping, reply("summary"))
    ws = make_ws(model)
    object.__setattr__(ws.cfg, "max_turns", 3)
    log = ws.new_session(project)
    assert await ws.submit(log, "go") == "limit"
    assert model.requests[-1]["tool_choice"] == "none"
    assert text_of(log.path()[-1].data["content"]) == "summary"


async def test_edit_branch_from_an_earlier_message(make_ws, project):
    model = FakeModel(reply("A1"), reply("A2"), reply("B1"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "first")
    first_user = log.path()[1]
    await ws.submit(log, "second")
    log.checkout(first_user.parent_id)  # /edit first
    await ws.submit(log, "first, rephrased")

    assert [text_of(e.data["content"]) for e in log.path() if e.type in (USER, ASSISTANT)] == [
        "first, rephrased", "B1"]
    assert len(log.children(first_user.parent_id)) == 2
    sent = [text_of(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "user"]
    assert sent[0] == "first, rephrased" and "first" not in sent  # the abandoned branch is not in context


async def test_duplicate_request_id_does_not_start_a_second_run(make_ws, project):
    model = FakeModel(reply("once"))
    ws = make_ws(model)
    log = ws.new_session(project)
    assert await ws.submit(log, "hi", request_id="req-1") == "ok"
    assert await ws.submit(log, "hi", request_id="req-1") == "ok"
    assert len(model.requests) == 1
    assert types(log).count(USER) == 1


# --- crash recovery (D6): build a half-written log, reopen, resume ---


def crashed_session(ws, project, *calls):
    """Simulate a process that died after durably writing tool_started for every call."""
    log = ws.new_session(project)
    run, _ = ws.index.start_run(log.id, None, ws.index.session(log.id)["model_ref"])
    log.append(USER, {"content": [{"type": "text", "text": "go"}]}, run_id=run["id"])
    if calls:
        log.append(ASSISTANT, {"content": list(calls), "stop": "tool_use"}, run_id=run["id"])
    for c in calls:
        log.append(TOOL_STARTED, {"call_id": c["id"], "name": c["name"]}, run_id=run["id"])
    log.close()
    return SessionLog.open(log.file)


async def test_resume_never_replays_bash(make_ws, project):
    model = FakeModel(reply("checked"))
    ws = make_ws(model)
    log = crashed_session(ws, project, call("bash", command="touch side_effect"))
    assert await ws.resume(log) == "ok"
    assert not (project / "side_effect").exists()
    assert UNKNOWN in text_of(results(log)[0]["content"])
    assert ws.index.unfinished_runs(log.id) == []


async def test_resume_verifies_edits_and_reruns_reads(make_ws, project):
    (project / "a.txt").write_text("new text")
    (project / "b.txt").write_text("bbb")
    model = FakeModel(reply("ok"))
    ws = make_ws(model)
    log = crashed_session(ws, project, call("edit", path="a.txt", old_string="old", new_string="new"),
                          call("read", path="b.txt"))
    await ws.resume(log)
    r = results(log)
    assert RECOVERED in text_of(r[0]["content"])
    assert "bbb" in text_of(r[1]["content"])


async def test_resume_after_crash_before_the_model_answered(make_ws, project):
    model = FakeModel(reply("final"))
    ws = make_ws(model)
    log = crashed_session(ws, project)  # crashed right after the user message
    await ws.resume(log)
    assert text_of(log.path()[-1].data["content"]) == "final"


async def test_resume_gives_up_after_repeated_crashes(make_ws, project):
    ws = make_ws(FakeModel())
    log = crashed_session(ws, project)
    run_id = ws.index.unfinished_runs(log.id)[0]["id"]
    for _ in range(3):
        ws.index.bump_resume(run_id)
    assert await ws.resume(log) == "error"
    assert ws.index.unfinished_runs(log.id) == []


async def test_new_message_closes_dangling_calls(make_ws, project):
    model = FakeModel(reply("fresh"))
    ws = make_ws(model)
    log = crashed_session(ws, project, call("bash", command="true"))
    ws.index.finish_run(ws.index.unfinished_runs(log.id)[0]["id"], "error")  # user declined to resume
    await ws.submit(log, "something else")
    msgs = model.requests[0]["messages"]
    assert roles(msgs) == ["user", "assistant", "tool", "user", "notice"]


# --- steer and gate (phase 2) ---


async def test_steer_joins_the_run_before_the_next_model_call(make_ws, project):
    from nano_workbuddy.loop import Inbox, Input

    inbox = Inbox()

    def first(msgs):
        inbox.steer.append(Input("also do B"))  # arrives while the model is answering
        return reply(None, call("bash", command="true"))

    model = FakeModel(first, reply("done A and B"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "do A", inbox=inbox)
    assert roles(model.requests[1]["messages"]) == ["user", "notice", "assistant", "tool", "user"]
    assert text_of(model.requests[1]["messages"][-1]["content"]) == "also do B"


async def test_steer_after_the_final_answer_continues_the_run(make_ws, project):
    from nano_workbuddy.loop import Inbox, Input

    inbox = Inbox()

    def first(msgs):
        inbox.steer.append(Input("one more thing"))
        return reply("first answer")

    model = FakeModel(first, reply("second answer"))
    ws = make_ws(model)
    log = ws.new_session(project)
    assert await ws.submit(log, "hi", inbox=inbox) == "ok"
    assert [text_of(e.data["content"]) for e in log.path() if e.type in (USER, ASSISTANT)] == [
        "hi", "first answer", "one more thing", "second answer"]


async def test_denied_approval_blocks_before_anything_happens(make_ws, project):
    asked = []

    async def deny(check, tool, call):
        asked.append((check.action, check.resource))
        return Result.error("The user denied this call.")

    model = FakeModel(reply(None, call("write", path="x.txt", content="no")), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": "readonly"})
    await ws.submit(log, "go", ask=deny)
    assert asked == [("write", str((project / "x.txt").resolve()))]
    assert not (project / "x.txt").exists()
    assert TOOL_STARTED not in types(log)  # denied calls never reach tool_started: nothing to recover
    assert "denied" in text_of(results(log)[0]["content"])


async def test_reused_call_ids_across_turns_are_not_mistaken_for_interrupted_calls(make_ws, project):
    def same_id(command):
        return {"type": "tool_call", "id": "call_0", "name": "bash", "args": {"command": command}}

    model = FakeModel(reply(None, same_id("echo one > one.txt")), reply("1"),
                      reply(None, same_id("echo two > two.txt")), reply("2"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "first")
    await ws.submit(log, "second")
    assert (project / "two.txt").exists()  # not answered with "outcome unknown"
    assert all(not r["is_error"] for r in results(log))
