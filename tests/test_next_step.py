"""next_step is the heart of the loop and of crash recovery; cover every tail shape."""

import pytest

from nano_workbuddy.loop import TRUNCATED, CallModel, Done, FailCalls, RunTools, next_step
from nano_workbuddy.types import Entry


def e(type, **data):
    return Entry(f"e{id(data)}", None, type, data)


def tc(i):
    return {"type": "tool_call", "id": f"c{i}", "name": "read", "args": {}}


def assistant(*calls, stop=None):
    return e("assistant", content=list(calls), stop=stop or ("tool_use" if calls else "end"))


def result(i):
    return e("tool_result", call_id=f"c{i}", content=[], is_error=False)


def started(i):
    return e("tool_started", call_id=f"c{i}", name="read")


SYS = e("system_prompt", text="")
USER = e("user", content=[])

CASES = [
    ("empty log", [], Done()),
    ("only system prompt", [SYS], Done()),
    ("user message waits for the model", [SYS, USER], CallModel()),
    ("final answer ends the run", [SYS, USER, assistant()], Done()),
    ("aborted text answer ends the run", [SYS, USER, assistant(stop="aborted")], Done()),
    ("tool calls need running", [SYS, USER, assistant(tc(1), tc(2))], RunTools([tc(1), tc(2)])),
    ("only unanswered calls run", [SYS, USER, assistant(tc(1), tc(2)), result(1)], RunTools([tc(2)])),
    ("started but unanswered still runs (recover decides)", [SYS, USER, assistant(tc(1)), started(1)],
     RunTools([tc(1)])),
    ("all answered goes back to the model", [SYS, USER, assistant(tc(1)), started(1), result(1)], CallModel()),
    ("truncated calls are failed, never run", [SYS, USER, assistant(tc(1), stop="length")],
     FailCalls([tc(1)], TRUNCATED)),
    ("truncated but answered goes back to the model", [SYS, USER, assistant(tc(1), stop="length"), result(1)],
     CallModel()),
    ("state entries are transparent", [SYS, USER, e("state", todo=[])], CallModel()),
    ("steer after results goes to the model", [SYS, USER, assistant(tc(1)), result(1), USER], CallModel()),
]


@pytest.mark.parametrize(("path", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_next_step(path, expected):
    assert next_step(path) == expected
