"""Skills (discovery, loading, pinning across compaction) and the memory tool."""

from pathlib import Path

import pytest
from conftest import FakeModel, call, reply
from test_compaction import LONG, SMALL

from nano_workbuddy import settings, skills
from nano_workbuddy.config import load
from nano_workbuddy.context import budget, is_prunable, request_tokens
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import COMPACTION, NOTICE, TOOL_RESULT, new_id, text_of

BODY = "# PDF forms\n\nUse scripts/fill.py to fill a form.\n" + "Step detail. " * 120


def make_skill(base: Path, name: str, description: str = "Fill PDF forms", body: str = BODY, **files: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}")
    for rel, content in files.items():
        (d / rel.replace("__", "/")).parent.mkdir(parents=True, exist_ok=True)
        (d / rel.replace("__", "/")).write_text(content)
    return d


def load_skill(name):
    return {"type": "tool_call", "id": new_id("call"), "name": "skill", "args": {"name": name}}


def notices(log, kind):
    return [e.data["text"] for e in log.path() if e.type == NOTICE and e.data["kind"] == kind]


def test_frontmatter_forms():
    meta, body = skills.split('---\nname: "x"\ndescription: >\n  Folded\n  text.\nmetadata:\n  a: b\n---\nBody')
    assert meta == {"name": "x", "description": "Folded text.", "metadata": "a: b"} and body == "Body"
    assert skills.split("no frontmatter") == ({}, "no frontmatter")


def test_discovery_precedence_and_requirements(tmp_path, home, project):
    make_skill(home / "skills", "pdf", "user pdf")
    make_skill(project / ".agents" / "skills", "pdf", "project pdf")
    make_skill(home / "skills", "nodesc", description="")
    extra = make_skill(tmp_path / "claude-skills", "review", "Review code").parent
    found = skills.discover(project, home, (str(extra),))
    assert found["pdf"].description == "project pdf" and found["pdf"].source == "project"
    assert "nodesc" not in found and found["review"].source == "user"


async def test_skill_is_listed_loaded_and_switched_off(home, project, make_ws):
    make_skill(home / "skills", "pdf", **{"scripts__fill.py": "print(1)", "references__forms.md": "x"})
    model = FakeModel(reply(None, load_skill("pdf")), reply("ok"), reply("bye"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "fill the form")

    assert "- pdf: Fill PDF forms" in notices(log, "skills")[0]
    assert "skill" in [t["name"] for t in model.requests[0]["tools"]]
    result = next(e.data for e in log.path() if e.type == TOOL_RESULT)
    body = text_of(result["content"])
    assert body.startswith('<skill name="pdf" dir=') and "scripts/fill.py" in body and "references/forms.md" in body
    assert result["meta"] == {"pin": "skill:pdf"} and not result["is_error"]

    ws.cfg = settings.set_skill_enabled(home, "pdf", False)
    await ws.submit(log, "again")
    assert "skill" not in [t["name"] for t in model.requests[-1]["tools"]]
    assert notices(log, "skills")[-1] == "No skills are enabled."
    assert load(home).skills_disabled == ("pdf",)


async def test_loaded_skill_survives_compaction(home, project, make_ws):
    make_skill(home / "skills", "pdf")
    turns = [reply(None, load_skill("pdf")), reply("loaded")] + [reply(f"answer {i}") for i in range(6)]
    # The fixed prompt, tool schemas, one 2k-token user message and pinned skill must all fit.
    # The previous 6k budget only passed because restored notices were sent beyond the limit.
    model = FakeModel(*turns, window=SMALL + 1500)
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "use pdf")
    loaded = next(e for e in log.path() if e.type == TOOL_RESULT)
    assert not is_prunable(loaded)  # pinned results are never replaced by a pointer
    for i in range(6):
        await ws.submit(log, f"question {i} {LONG}")

    assert [e for e in log.path() if e.type == COMPACTION]
    sent = [text_of(m["content"]) for m in model.requests[-1]["messages"]]
    copies = [t for t in sent if '<skill name="pdf"' in t]
    assert len(copies) == 1 and copies[0].startswith("<system-notice>")  # put back once, as a notice
    assert notices(log, "skill:pdf")[-1] == text_of(loaded.data["content"])
    assert all(request_tokens(r["system"], r["messages"], r["tools"]) <= budget(model.spec)
               for r in model.requests)


async def test_slash_command_loads_the_skill(home, project, make_ws):
    make_skill(home / "skills", "pdf")
    model = FakeModel(reply("ok"), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    await ws.submit(log, "/pdf fill invoice.pdf")
    sent = [text_of(m["content"]) for m in model.requests[0]["messages"]]
    skill_at = next(i for i, t in enumerate(sent) if '<skill name="pdf"' in t)
    assert sent.index("/pdf fill invoice.pdf") == skill_at + 1  # the skill comes right before the request
    model.script += [reply("ok")]
    await ws.submit(log, "/unknown thing")  # not a skill: just text
    await ws.submit(log, "/pdf again")  # already in view: not added twice
    assert len(notices(log, "skill:pdf")) == 1


async def test_memory_tool_edits_user_and_project_files(home, project, make_ws):
    model = FakeModel(
        reply(None, call("memory", action="add", file="user", text="- Prefers concise answers in Chinese")),
        reply(None, call("memory", action="add", file="project", text="- Run tests with `uv run pytest`")),
        reply(None, call("memory", action="replace", file="user", old_text="concise", text="short")),
        reply(None, call("memory", action="remove", file="project", old_text="- Run tests with `uv run pytest`")),
        reply(None, call("memory", action="remove", file="user", old_text="nothing like this")),
        reply("saved"))
    ws = make_ws(model)
    log = ws.new_session(project)
    assert await ws.submit(log, "remember") == "ok"  # workspace preset: no approval needed
    results = [e.data for e in log.path() if e.type == TOOL_RESULT]
    assert [r["is_error"] for r in results] == [False, False, False, False, True]
    assert (home / "USER.md").read_text() == "- Prefers short answers in Chinese\n"
    assert (project / "AGENTS.md").read_text() == "\n"
    assert "next model request" in text_of(results[0]["content"])
    assert "Prefers short answers" in model.requests[-1]["system"]
    assert "Prefers concise answers" not in model.requests[-1]["system"]

    fresh = ws.new_session(project)  # the next session's frozen prompt includes the memory
    assert "Prefers short answers" in fresh.path()[0].data["text"]


async def test_memory_needs_approval_in_the_readonly_preset(home, project, make_ws):
    model = FakeModel(reply(None, call("memory", action="add", file="user", text="x")), reply("ok"))
    ws = make_ws(model)
    log = ws.new_session(project)
    ws.index.update_settings(log.id, {"preset": "readonly"})
    await ws.submit(log, "remember")
    assert "needs the user's approval" in text_of(next(e for e in log.path() if e.type == TOOL_RESULT).data["content"])
    assert not (home / "USER.md").exists()


async def test_skills_and_memory_api(home, project):
    from test_api import Server

    make_skill(home / "skills", "pdf")
    make_skill(project / ".agents" / "skills", "deploy", "Deploy this project")
    ws = Workspace(load(home))
    async with Server(ws) as s:
        sid = await s.session(project)
        view = (await s.client.get("/api/settings")).json()
        assert {(k["name"], k["source"]) for k in view["skills"]} == {("pdf", "user"), ("deploy", "project")}
        view = (await s.client.patch("/api/settings/skills/pdf", json={"enabled": False})).json()
        assert next(k for k in view["skills"] if k["name"] == "pdf")["enabled"] is False
        pid = ws.index.session(sid)["project_id"]
        assert [k["name"] for k in (await s.client.get("/api/skills", params={"project_id": pid})).json()] == ["deploy"]

        files = (await s.client.get("/api/memory")).json()
        assert [f["key"] for f in files][0] == "user" and files[1]["path"] == str(project / "AGENTS.md")
        await s.client.put("/api/memory/user", json={"text": "- likes tea\n"})
        assert (home / "USER.md").read_text() == "- likes tea\n"
        assert (await s.client.put("/api/memory/nope", json={"text": ""})).status_code == 404


@pytest.mark.parametrize("name", ["read", "bash"])
def test_skill_files_are_readable_inside_home(home, project, name):
    from nano_workbuddy import policy

    rules = list(policy.PRESETS["workspace"].rules)
    places = {"root": str(project), "home": str(home), "spill": str(home / "spill" / "s")}
    assert policy.check("read", f"{home}/skills/pdf/SKILL.md", rules, places).effect == "allow"
    assert policy.check("read", f"{home}/secrets.json", rules, places).effect == "deny"
    sandbox = policy.sandbox_policy(policy.PRESETS["workspace"], project, home / "tmp", home, home / "spill")
    assert home / "skills" in sandbox.allow_read


async def test_tools_are_tracked_per_session(home, project, tmp_path, make_ws):
    """A run in one project must not change another session's tool set or context estimate (F11)."""
    other = tmp_path / "other"
    other.mkdir()
    make_skill(project / ".agents" / "skills", "pdf")
    ws = make_ws(FakeModel(reply("a"), reply("b")))
    with_skill, without = ws.new_session(project), ws.new_session(other)
    await ws.submit(without, "hi")
    before = ws.context_usage(without)["tokens"]
    await ws.submit(with_skill, "hi")
    assert ws.tools_of(with_skill).get("skill") and not ws.tools_of(without).get("skill")
    assert ws.context_usage(without)["tokens"] == before
