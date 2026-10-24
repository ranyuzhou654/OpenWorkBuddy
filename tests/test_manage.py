"""Managing sessions and projects: rename, archive, delete, trust, chats without a project, live context."""

from conftest import FakeModel, call, reply
from test_api import Server, Stream, finished
from test_skills_memory import make_skill

from nano_workbuddy import api
from nano_workbuddy.config import load
from nano_workbuddy.runtime import Workspace, tmp_dir


def workspace(home, *script, scratch=None):
    if scratch:
        home.mkdir(parents=True, exist_ok=True)
        load(home)  # writes the default config
        (home / "config.toml").write_text(f'scratch_dir = "{scratch}"\n' + (home / "config.toml").read_text())
    return Workspace(load(home), connect=FakeModel(*script).connect)


async def test_rename_archive_and_delete_a_session(home, project):
    ws = workspace(home, reply(None, call("bash", command="echo hi")), reply("done"))
    async with Server(ws) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "first message"})
        await stream.until(finished)
        stream.close()
        assert (await s.client.patch(f"/api/sessions/{sid}", json={"title": " My task "})).json()["title"] == "My task"
        ws.index.sync_session(ws.open_session(sid))  # a later run keeps the user's name
        assert ws.index.session(sid)["title"] == "My task"
        assert (await s.client.patch(f"/api/sessions/{sid}", json={"title": ""})).json()["title"] == "first message"
        assert (await s.client.patch(f"/api/sessions/{sid}", json={"archived": True})).json()["archived_at"]
        assert (await s.client.patch(f"/api/sessions/{sid}", json={"archived": False})).json()["archived_at"] is None

        f = ws.open_session(sid).file
        (home / "spill" / sid).mkdir(parents=True)
        assert (await s.client.delete(f"/api/sessions/{sid}")).json() == {"ok": True}
        assert not f.exists() and not (home / "spill" / sid).exists() and not tmp_dir(sid).exists()
        assert ws.index.session(sid) is None and not ws.index.unfinished_runs(sid)
        assert (await s.client.get(f"/api/sessions/{sid}")).status_code == 404
    assert ws.index.reindex(ws.sessions_dir) == 0  # nothing comes back from disk


async def test_remove_a_project_keeps_its_folder(home, project):
    (project / "keep.txt").write_text("mine")
    ws = workspace(home)
    async with Server(ws) as s:
        sid = await s.session(project)
        pid = ws.index.session(sid)["project_id"]
        renamed = (await s.client.patch(f"/api/projects/{pid}", json={"name": "Demo"})).json()
        assert renamed["name"] == "Demo" and renamed["trusted"] is True
        assert (await s.client.delete(f"/api/projects/{pid}")).json() == {"ok": True}
        assert (await s.client.get("/api/projects")).json() == []
        assert ws.index.session(sid) is None and not (ws.sessions_dir / pid).exists()
    assert (project / "keep.txt").read_text() == "mine"


async def test_untrusted_projects_start_read_only_without_their_skills(home, project):
    make_skill(project / ".agents" / "skills", "deploy", "Deploy this project")
    ws = workspace(home)
    async with Server(ws) as s:
        p = (await s.client.post("/api/projects", json={"path": str(project)})).json()
        assert p["trusted"] is False
        session = (await s.client.post("/api/sessions", json={"project_id": p["id"]})).json()
        assert session["settings"]["preset"] == "readonly"
        assert (await s.client.get("/api/skills", params={"project_id": p["id"]})).json() == []
        assert not ws.skills(project)
        again = (await s.client.post("/api/projects", json={"path": str(project), "trusted": True})).json()
        assert again["trusted"] is False  # adding an existing project never changes its trust
        await s.client.patch(f"/api/projects/{p['id']}", json={"trusted": True})
        assert [k["name"] for k in (await s.client.get("/api/skills", params={"project_id": p["id"]})).json()] \
            == ["deploy"]
        session = (await s.client.post("/api/sessions", json={"project_id": p["id"]})).json()
        assert session["settings"]["preset"] == "workspace"


async def test_chat_without_a_project(home, tmp_path):
    scratch = tmp_path / "chats"
    ws = workspace(home, reply("hello"), scratch=scratch)
    async with Server(ws) as s:
        session = (await s.client.post("/api/sessions", json={})).json()
        projects = (await s.client.get("/api/projects")).json()
        assert [(p["root_path"], p["scratch"], p["trusted"]) for p in projects] == [(str(scratch.resolve()), True,
                                                                                     True)]
        assert session["project_id"] == projects[0]["id"] and session["settings"]["preset"] == "workspace"
        assert (await s.client.post("/api/sessions", json={})).json()["project_id"] == projects[0]["id"]


async def test_pick_folder(home, project, monkeypatch):
    ws = workspace(home)

    async def chosen():
        return str(project) + "/"

    async def cancelled():
        return None

    async def missing():
        raise FileNotFoundError("no folder dialog")

    async with Server(ws) as s:
        for picker, expect in ((chosen, 200), (cancelled, 200), (missing, 501)):
            monkeypatch.setattr(api, "choose_folder", picker)
            r = await s.client.post("/api/pick-folder")
            assert r.status_code == expect
        monkeypatch.setattr(api, "choose_folder", chosen)
        assert (await s.client.post("/api/pick-folder")).json() == {"path": str(project.resolve())}


async def test_context_usage_is_published_before_each_request(home, project):
    ws = workspace(home, reply(None, call("bash", command="echo hi")), reply("done"))
    async with Server(ws) as s:
        sid = await s.session(project)
        stream = Stream(s.client, sid)
        await stream.ready.wait()
        await s.client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
        await stream.until(finished)
        stream.close()
    sizes = [e["tokens"] for e in stream.events if e["type"] == "context"]
    assert len(sizes) == 2 and sizes[1] > sizes[0]  # the tool call and its result were added
