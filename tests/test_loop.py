"""End-to-end loop behaviour through Workspace, with a scripted model."""

import asyncio

from conftest import HANG, FakeModel, call, reply

from nano_workbuddy.log import SessionLog
from nano_workbuddy.loop import CANCELLED, RECOVERED, TRUNCATED, UNKNOWN
from nano_workbuddy.types import ASSISTANT, TOOL_RESULT, TOOL_STARTED, USER, text_of


def results(log):
    return [e.data for e in log.path() if e.type == TOOL_RESULT]


def types(log):
    return [e.type for e in log.path()]


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
    assert types(log) == ["system_prompt", "user", "assistant", "tool_started", "tool_result", "assistant",
                          "tool_started", "tool_result", "assistant"]
    assert text_of(log.path()[-1].data["content"]) == "done: # hi"
    assert model.requests[0]["system"].startswith("You are nano-workbuddy")
    assert [t["name"] for t in model.requests[0]["tools"]] == ["read", "write", "edit", "bash"]
    assert {"run.started", "assistant.delta", "tool.started", "tool.result", "run.finished"} <= {
        e["type"] for e in events}
    run = ws.index.running_runs(log.id)
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
    assert ws.index.running_runs(log.id) == []
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
    sent = [text_of(m["content"]) for m in model.requests[-1]["messages"]]
    assert sent == ["first, rephrased"]  # the abandoned branch is not in context


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
    assert ws.index.running_runs(log.id) == []


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
    run_id = ws.index.running_runs(log.id)[0]["id"]
    for _ in range(3):
        ws.index.bump_resume(run_id)
    assert await ws.resume(log) == "error"
    assert ws.index.running_runs(log.id) == []


async def test_new_message_closes_dangling_calls(make_ws, project):
    model = FakeModel(reply("fresh"))
    ws = make_ws(model)
    log = crashed_session(ws, project, call("bash", command="true"))
    ws.index.finish_run(ws.index.running_runs(log.id)[0]["id"], "error")  # user declined to resume
    await ws.submit(log, "something else")
    msgs = model.requests[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
