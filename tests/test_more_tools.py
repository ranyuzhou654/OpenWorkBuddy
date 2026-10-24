"""grep, glob, web_fetch, todo and ask_user."""

import asyncio
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply
from test_api import Server, Stream, finished

from nano_workbuddy import policy
from nano_workbuddy.context import pinned
from nano_workbuddy.tools import BUILTIN, Registry, ToolCtx, execute
from nano_workbuddy.tools.search import glob_regex
from nano_workbuddy.tools.web import html_to_text
from nano_workbuddy.types import TOOL_RESULT, new_id, text_of

TOOLS = Registry(BUILTIN)


def ctx(root, tmp_path, **kw):
    return ToolCtx(root=root, session_id="s", spill_dir=tmp_path / "spill", **kw)


async def run(name, root, tmp_path, c=None, **args):
    r = await execute(TOOLS.get(name), {"type": "tool_call", "id": new_id("call"), "name": name, "args": args},
                      c or ctx(root, tmp_path))
    return r, text_of(r.content)


@pytest.fixture
def tree(project):
    files = {"src/app.py": "import os\nTODO = 1\nprint('Hello')\n", "src/util/helpers.py": "def todo():\n    pass\n",
             "README.md": "# Demo\ntodo list\n", ".hidden/secret.py": "TODO hidden\n",
             "node_modules/x/index.js": "TODO\n", "data.bin": "TODO"}
    for rel, content in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(content)
    (project / "data.bin").write_bytes(b"TODO\0\1\2")
    os.utime(project / "README.md", (time.time() + 10, time.time() + 10))  # the newest file
    return project


@pytest.fixture(params=["python", "rg"])
def engine(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    elif not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    return request.param


def test_glob_regex():
    assert glob_regex("**/*.py").match("a/b/c.py") and glob_regex("**/*.py").match("c.py")
    assert not glob_regex("*.py").match("a/c.py") and glob_regex("src/**/test_?.py").match("src/x/y/test_1.py")


async def test_glob_newest_first_and_skips_hidden(tree, tmp_path):
    r, out = await run("glob", tree, tmp_path, pattern="**/*")
    assert out.splitlines()[0] == "README.md"
    assert "src/util/helpers.py" in out and ".hidden" not in out and "node_modules" not in out
    _, out = await run("glob", tree, tmp_path, pattern="**/*.py", path="src")
    assert sorted(out.splitlines()) == ["src/app.py", "src/util/helpers.py"]


async def test_grep_modes(tree, tmp_path, engine):
    _, files = await run("grep", tree, tmp_path, pattern="todo", ignore_case=True)
    assert sorted(files.splitlines()) == ["README.md", "src/app.py", "src/util/helpers.py"]  # no hidden, binary
    _, content = await run("grep", tree, tmp_path, pattern="^TODO", mode="content")
    assert content == "src/app.py:2:TODO = 1"
    _, count = await run("grep", tree, tmp_path, pattern="o", mode="count", glob="*.py")
    assert sorted(count.splitlines()) == ["src/app.py:2", "src/util/helpers.py:1"]  # matching lines per file
    _, limited = await run("grep", tree, tmp_path, pattern=".", limit=1)
    assert limited.splitlines()[1].startswith("[") and "more not shown" in limited
    _, none = await run("grep", tree, tmp_path, pattern="nothing-here")
    assert none.startswith("No matches")


async def test_grep_is_a_read_for_permissions(tree, tmp_path, home):
    places = {"root": str(tree), "home": str(home), "spill": str(tmp_path)}
    rules = list(policy.PRESETS["workspace"].rules)
    args = TOOLS.get("grep").args.model_validate({"pattern": "x", "path": "~/.ssh"})
    action, resource = TOOLS.get("grep").permission_scope(args, ctx(tree, tmp_path))
    assert (action, policy.check(action, resource, rules, places).effect) == ("read", "deny")


async def test_search_does_not_follow_symlinks(project, tmp_path, engine):
    """A link inside the project may point at a file the rules deny; neither engine follows it (F04)."""
    secret_dir = tmp_path / "elsewhere"
    secret_dir.mkdir()
    (secret_dir / "fake-secret.txt").write_text("SYNTHETIC_MARKER\n")
    (project / "ordinary.txt").symlink_to(secret_dir / "fake-secret.txt")
    (project / "linked").symlink_to(secret_dir, target_is_directory=True)
    (project / "real.txt").write_text("SYNTHETIC_MARKER here\n")
    _, out = await run("grep", project, tmp_path, pattern="SYNTHETIC_MARKER")
    assert out.splitlines() == ["real.txt"]
    _, names = await run("glob", project, tmp_path, pattern="**/*.txt")
    assert names.splitlines() == ["real.txt"]


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            pages = {
                "/page": ("text/html; charset=utf-8", "<html><head><title>Demo  Page</title><style>x{}</style></head>"
                          "<body><nav><a href='/home'>Home</a></nav><h1>Title</h1><p>Hello <b>world</b>.</p>"
                          "<ul><li>one</li><li>two</li></ul><pre>def f():\n    return 1</pre>"
                          "<script>evil()</script></body></html>"),
                "/data.json": ("application/json", '{"a": 1}'),
                "/file.pdf": ("application/pdf", "%PDF-1.4 binary"),
            }
            if self.path == "/moved":
                return self._redirect("/page")
            if self.path == "/away":
                return self._redirect("https://elsewhere.example/x")
            if self.path not in pages:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not here")
                return
            kind, body = pages[self.path]
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body.encode())

        def _redirect(self, to):
            self.send_response(302)
            self.send_header("Location", to)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def test_web_fetch(site, project, tmp_path):
    c = ctx(project, tmp_path)
    r, out = await run("web_fetch", project, tmp_path, c, url=f"{site}/moved")  # same-site redirect is followed
    assert out.startswith(f"[S1] {site}/page (200, text/html)")  # recorded as a source read in full
    [rec] = r.meta["sources"]
    assert (rec["n"], rec["kind"], rec["title"], rec["url"]) == (1, "page", "Demo Page", f"{site}/page")
    assert "# Demo Page" in out and "# Title" in out and "Hello world." in out and "- one\n- two" in out
    assert f"[Home]({site}/home)" in out and "    return 1" in out and "evil" not in out and "x{}" not in out
    _, out = await run("web_fetch", project, tmp_path, c, url=f"{site}/data.json")
    assert out.endswith('{"a": 1}') and out.startswith("[S2]")
    _, out = await run("web_fetch", project, tmp_path, c, url=f"{site}/page#top")
    assert out.startswith("[S1]")  # the same page keeps its number
    _, out = await run("web_fetch", project, tmp_path, url=f"{site}/away")
    assert "redirects to another site: https://elsewhere.example/x" in out
    r, out = await run("web_fetch", project, tmp_path, url=f"{site}/missing")
    assert r.is_error and "HTTP 404" in out
    _, out = await run("web_fetch", project, tmp_path, url=f"{site}/file.pdf")
    saved = out.split("saved to ")[1].split(". ")[0]
    assert Path(saved).read_bytes() == b"%PDF-1.4 binary" and oct(os.stat(saved).st_mode)[-3:] == "600"
    r, out = await run("web_fetch", project, tmp_path, url="ftp://x")
    assert r.is_error


def test_web_fetch_permission_is_per_site():
    check = policy.check("web_fetch", "https://docs.python.org/3/library/re.html",
                         list(policy.PRESETS["workspace"].rules), {"root": "/p", "home": "/h", "spill": "/s"})
    assert check.effect == "ask"
    always = check.suggestion()
    assert (always.action, always.resource) == ("web_fetch", "https://docs.python.org/*")
    assert always.matches("web_fetch", "https://docs.python.org/3/", {})
    assert not always.matches("web_fetch", "https://docs.python.org.evil.com/", {})


def test_html_to_text_keeps_code_indentation():
    assert html_to_text("<pre>a\n  b</pre><p>x</p>", "http://h/") == "```\na\n  b\n```\n\nx"


async def test_todo_is_pinned_and_validated(make_ws, project):
    items = [{"content": "read code", "status": "completed"}, {"content": "fix bug", "status": "in_progress"},
             {"content": "run tests"}]
    model = FakeModel(reply(None, call("todo", todos=items)),
                      reply(None, call("todo", todos=[{"content": "a", "status": "in_progress"}] * 2)), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "go")
    good, bad = [e.data for e in log.path() if e.type == TOOL_RESULT]
    assert text_of(good["content"]) == "Todo list (1/3 done):\n[x] read code\n[>] fix bug\n[ ] run tests"
    assert good["meta"]["todos"][2] == {"content": "run tests", "status": "pending"}
    assert pinned(log.path())["todo"] == text_of(good["content"])
    assert bad["is_error"] and "only one item" in text_of(bad["content"])


async def test_ask_user_through_the_api(home, project):
    from nano_workbuddy.config import load
    from nano_workbuddy.runtime import Workspace

    q = call("ask_user", question="Which database?", options=["SQLite", "Postgres"])
    model = FakeModel(reply(None, q), lambda msgs: reply("using " + text_of(msgs[-1]["content"])))
    ws = Workspace(load(home), connect=model.connect)
    async with Server(ws) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "set up storage"})
        asked = (await stream.until(lambda e: e["type"] == "question.requested"))["question"]
        assert asked["question"] == "Which database?" and asked["options"] == ["SQLite", "Postgres"]
        assert asked["call_id"] == q["id"]
        snap = (await s.client.get(f"/api/sessions/{sid}")).json()
        assert [x["id"] for x in snap["live"]["questions"]] == [asked["id"]]
        assert (await s.client.post(f"/api/questions/{asked['id']}", json={"answer": " "})).status_code == 400
        assert (await s.client.post(f"/api/questions/{asked['id']}", json={"answer": "SQLite"})).json() == {"ok": True}
        await stream.until(finished)
        stream.close()
        assert (await s.client.post(f"/api/questions/{asked['id']}", json={"answer": "x"})).status_code == 404
    log = ws.open_session(sid)
    assert text_of(log.path()[-1].data["content"]) == "using The user answered: SQLite"


async def test_ask_user_without_a_ui(make_ws, project):
    model = FakeModel(reply(None, call("ask_user", question="?")), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await asyncio.wait_for(ws.submit(log, "go"), 5)
    result = next(e.data for e in log.path() if e.type == TOOL_RESULT)
    assert result["is_error"] and "cannot ask questions" in text_of(result["content"])


async def test_grep_long_lines_are_previewed(project, tmp_path, engine):
    (project / "min.js").write_text("var a=1;" * 100 + "NEEDLE" + "x" * 500 + "\n")
    (project / "short.txt").write_text("NEEDLE here\n")
    _, out = await run("grep", project, tmp_path, pattern="NEEDLE", mode="content")
    long, short = out.splitlines()
    assert long.startswith("min.js:1:var a=1;") and len(long) < 400  # cut, not dropped
    assert short == "short.txt:1:NEEDLE here"


async def test_rg_honours_gitignore(project, tmp_path):
    if not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    (project / ".gitignore").write_text("build/\n")
    (project / ".git").mkdir()  # ripgrep reads .gitignore only inside a repository
    (project / "build").mkdir()
    (project / "build" / "out.js").write_text("NEEDLE\n")
    (project / "src.py").write_text("NEEDLE\n")
    _, out = await run("grep", project, tmp_path, pattern="NEEDLE")
    assert out == "src.py"
