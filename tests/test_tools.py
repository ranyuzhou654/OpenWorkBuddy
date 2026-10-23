import asyncio
import os

import pytest

from nano_workbuddy.context import spill
from nano_workbuddy.tools import BUILTIN, Registry, Result, ToolCtx, execute
from nano_workbuddy.tools.builtin import bash, edit, write
from nano_workbuddy.types import text_of

TOOLS = Registry(BUILTIN)


@pytest.fixture
def ctx(project, tmp_path):
    return ToolCtx(root=project, session_id="s", spill_dir=tmp_path / "spill")


async def run(ctx, name, **args):
    return await execute(TOOLS.get(name), {"type": "tool_call", "id": "c1", "name": name, "args": args}, ctx)


async def test_read_numbers_lines_and_pages(ctx, project):
    (project / "a.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)))
    r = await run(ctx, "read", path="a.txt", offset=3, limit=2)
    assert not r.is_error
    assert "     3\tline3\n     4\tline4" in text_of(r.content)
    assert "lines 3-4 of 10" in text_of(r.content)


async def test_read_errors_are_results_not_exceptions(ctx):
    assert (await run(ctx, "read", path="missing.txt")).is_error
    assert (await run(ctx, "read", path="a", offset=0)).is_error  # schema violation
    r = await execute(TOOLS.get("read"), {"type": "tool_call", "id": "c", "name": "read", "args": {},
                                          "error": "arguments are not valid JSON"}, ctx)
    assert r.is_error and "not valid JSON" in text_of(r.content)


async def test_read_image(ctx, project):
    (project / "x.png").write_bytes(b"\x89PNG fake")
    r = await run(ctx, "read", path="x.png")
    assert r.content[1]["type"] == "image" and r.content[1]["mime"] == "image/png"


async def test_write_requires_fresh_read_of_existing_file(ctx, project):
    f = project / "a.txt"
    f.write_text("old")
    assert (await run(ctx, "write", path="a.txt", content="new")).is_error
    await run(ctx, "read", path="a.txt")
    os.utime(f, (1, 1))  # changed behind our back
    assert "changed since" in text_of((await run(ctx, "write", path="a.txt", content="new")).content)
    await run(ctx, "read", path="a.txt")
    assert not (await run(ctx, "write", path="a.txt", content="new")).is_error
    assert f.read_text() == "new"


async def test_write_creates_new_files_and_dirs(ctx, project):
    r = await run(ctx, "write", path="d/e/new.txt", content="hi")
    assert not r.is_error and (project / "d/e/new.txt").read_text() == "hi"


async def test_edit_requires_unique_match(ctx, project):
    f = project / "a.py"
    f.write_text("x = 1\nx = 1\ny = 2\n")
    await run(ctx, "read", path="a.py")
    assert "matches 2 places" in text_of((await run(ctx, "edit", path="a.py", old_string="x = 1",
                                                    new_string="x = 3")).content)
    assert "not found" in text_of((await run(ctx, "edit", path="a.py", old_string="z", new_string="w")).content)
    assert not (await run(ctx, "edit", path="a.py", old_string="y = 2", new_string="y = 5")).is_error
    assert not (await run(ctx, "edit", path="a.py", old_string="x = 1", new_string="x = 0",
                          replace_all=True)).is_error
    assert f.read_text() == "x = 0\nx = 0\ny = 5\n"


async def test_recover_verdicts(ctx, project):
    f = project / "a.txt"
    f.write_text("hello world")
    assert write.recover(write.args(path="a.txt", content="hello world"), ctx) == "done"
    assert write.recover(write.args(path="a.txt", content="other"), ctx) == "not_done"
    assert edit.recover(edit.args(path="a.txt", old_string="world", new_string="there"), ctx) == "not_done"
    f.write_text("hello there")
    assert edit.recover(edit.args(path="a.txt", old_string="world", new_string="there"), ctx) == "done"


async def test_bash_reports_exit_code_and_scrubs_env(ctx, monkeypatch):
    monkeypatch.setenv("SECRET_API_KEY", "sk-123")
    r = await run(ctx, "bash", command="echo hi; echo ${SECRET_API_KEY:-none}; exit 3")
    out = text_of(r.content)
    assert "hi\nnone" in out and "[exit code 3]" in out


async def test_bash_timeout_kills(ctx):
    r = await run(ctx, "bash", command="sleep 30", timeout=1)
    assert r.is_error and "timed out" in text_of(r.content)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_bash_cancel_kills_the_whole_process_group(ctx, project):
    task = asyncio.create_task(bash.fn(bash.args(command="sleep 60 & echo $! > pid; wait"), ctx))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (project / "pid").exists() and (project / "pid").read_text().strip():
            break
    pid = int((project / "pid").read_text())
    assert alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not alive(pid)


def test_spill_keeps_head_and_tail(tmp_path):
    big = "a" * 20_000 + "MIDDLE" + "z" * 20_000
    r = spill(Result.ok(big), tmp_path / "sp", "c9")
    inline = text_of(r.content)
    assert inline.startswith("a" * 100) and inline.endswith("z" * 100) and "MIDDLE" not in inline
    saved = tmp_path / "sp" / "c9.txt"
    assert saved.read_text() == big and str(saved) in inline
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    assert spill(Result.ok("small"), tmp_path / "sp", "c10").content == [{"type": "text", "text": "small"}]
