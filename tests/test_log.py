import json

import pytest

from nano_workbuddy.log import SessionLog
from nano_workbuddy.types import ASSISTANT, STATE, SYSTEM_PROMPT, USER


def make(tmp_path):
    log = SessionLog.create(tmp_path / "s.jsonl", "s_1", "p_1")
    root = log.append(SYSTEM_PROMPT, {"text": "sys"})
    return log, root


def test_append_builds_a_linear_path(tmp_path):
    log, root = make(tmp_path)
    u = log.append(USER, {"content": []})
    a = log.append(ASSISTANT, {"content": [], "stop": "end"})
    assert [e.id for e in log.path()] == [root.id, u.id, a.id]
    assert a.parent_id == u.id
    assert [root.seq, u.seq, a.seq] == [1, 2, 3]


def test_checkout_branches_and_survives_reopen(tmp_path):
    log, root = make(tmp_path)
    u1 = log.append(USER, {"content": ["first"]})
    log.append(ASSISTANT, {"content": [], "stop": "end"})
    log.checkout(root.id)
    u2 = log.append(USER, {"content": ["second"]})
    assert u2.parent_id == root.id
    assert {e.id for e in log.children(root.id)} == {u1.id, u2.id}

    reopened = SessionLog.open(log.file)
    assert reopened.leaf_id == u2.id
    assert [e.id for e in reopened.path()] == [root.id, u2.id]
    assert reopened.lines == log.lines

    reopened.checkout(u1.id)
    assert SessionLog.open(log.file).leaf_id == u1.id


def test_partial_last_line_is_truncated_on_open(tmp_path):
    log, root = make(tmp_path)
    u = log.append(USER, {"content": []})
    with open(log.file, "a") as f:
        f.write('{"id": "e_half", "parent_id": ')  # crash mid-write
    reopened = SessionLog.open(log.file)
    assert reopened.leaf_id == u.id
    assert log.file.read_text().endswith("\n")
    reopened.append(ASSISTANT, {"content": [], "stop": "end"})
    assert len(SessionLog.open(log.file).entries) == 3


def test_corruption_in_the_middle_raises(tmp_path):
    log, _ = make(tmp_path)
    log.append(USER, {"content": []})
    lines = log.file.read_text().splitlines()
    lines[1] = "{broken"
    log.file.write_text("\n".join(lines) + "\n")
    with pytest.raises(json.JSONDecodeError):
        SessionLog.open(log.file)


def test_state_is_taken_from_the_current_path(tmp_path):
    log, root = make(tmp_path)
    log.append(STATE, {"todo": ["a"]})
    log.checkout(root.id)
    assert log.state() == {}
    log.append(STATE, {"todo": ["b"]})
    assert log.state() == {"todo": ["b"]}


def test_fork_copies_only_the_path(tmp_path):
    log, root = make(tmp_path)
    u1 = log.append(USER, {"content": ["a"]})
    log.checkout(root.id)
    log.append(USER, {"content": ["b"]})
    new = log.fork(u1.id, tmp_path / "f.jsonl", "s_2")
    assert new.header["forked_from"] == "s_1"
    assert [e.id for e in SessionLog.open(new.file).path()] == [root.id, u1.id]


def test_resolve_by_prefix(tmp_path):
    log, root = make(tmp_path)
    assert log.resolve(root.id[:8]) == root.id
    with pytest.raises(KeyError):
        log.resolve("zzz")


def test_index_migrates_a_phase1_database(tmp_path):
    import sqlite3

    from nano_workbuddy.index import Index

    db = sqlite3.connect(tmp_path / "index.db")
    db.executescript("""
      create table sessions(id text primary key, project_id text not null, title text, file_path text not null,
        indexed_bytes integer not null default 0, leaf_id text, model_ref text, updated_at real not null);
      insert into sessions values ('s_1', 'p_1', 't', '/x', 0, null, 'm', 0);""")
    db.close()
    idx = Index(tmp_path / "index.db")
    assert idx.session("s_1")["settings"] == {"mode": "confirm", "allow": []}
    assert idx.db.execute("pragma user_version").fetchone()[0] == 1
    Index(tmp_path / "index.db")  # reopening is a no-op
