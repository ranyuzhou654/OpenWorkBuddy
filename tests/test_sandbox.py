"""The sandbox for real on macOS (Seatbelt); bubblewrap argument building everywhere."""

import socket
import subprocess
import sys
import threading

import pytest

from nano_workbuddy import sandbox
from nano_workbuddy.sandbox import SandboxPolicy, SandboxUnavailable, wrap
from nano_workbuddy.tools import ToolCtx
from nano_workbuddy.tools.builtin import BashArgs, bash, resolve
from nano_workbuddy.types import text_of

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")


@pytest.fixture
def box(tmp_path):
    root, tmp, secret, outside = (tmp_path / n for n in ("root", "tmp", "secret", "outside"))
    for d in (root, tmp, secret, outside, root / ".git", secret / "spill"):
        d.mkdir()
    (secret / "key").write_text("s3cret")
    (secret / "spill" / "out.txt").write_text("spilled")
    policy = SandboxPolicy(writable=(root, tmp), protected=(root / ".git",), deny_read=(secret,),
                           allow_read=(secret / "spill",), network=False)
    return root, outside, secret, policy


def sh(command, policy, cwd):
    p = subprocess.run(wrap(["/bin/bash", "-c", command], policy), cwd=cwd, capture_output=True, text=True,
                       timeout=30)
    return p.returncode, p.stdout + p.stderr


@darwin
def test_writes_are_confined_to_the_workspace(box):
    root, outside, _, policy = box
    assert sh("echo ok > inside.txt && cat inside.txt", policy, root) == (0, "ok\n")
    code, out = sh(f"echo x > {outside}/x.txt", policy, root)
    assert code != 0 and "Operation not permitted" in out and not (outside / "x.txt").exists()
    code, _ = sh("echo x > .git/config", policy, root)
    assert code != 0 and not (root / ".git" / "config").exists()


@darwin
def test_secrets_are_unreadable_except_the_allowed_spill(box):
    root, _, secret, policy = box
    code, out = sh(f"cat {secret}/key", policy, root)
    assert code != 0 and "s3cret" not in out
    assert sh(f"cat {secret}/spill/out.txt", policy, root) == (0, "spilled")


@darwin
def test_network_is_blocked_unless_allowed(box):
    root, _, _, policy = box
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    probe = f"python3 -c 'import socket; socket.create_connection((\"127.0.0.1\", {port}), timeout=3)'"
    assert sh(probe, policy, root)[0] != 0
    open_policy = SandboxPolicy(policy.writable, policy.protected, policy.deny_read, policy.allow_read, True)
    assert sh(probe, open_policy, root)[0] == 0
    server.close()


@darwin
def test_common_tools_still_work(box):
    root, _, _, policy = box
    for command in ("git --version", "python3 -c 'print(1)'", "ls /", "whoami"):
        assert sh(command, policy, root)[0] == 0, command


def test_bwrap_arguments(monkeypatch, box):
    root, _, secret, policy = box
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    args = sandbox.bwrap(["ls"], policy)
    assert args[:3] == ["/usr/bin/bwrap", "--ro-bind", "/"]
    assert ["--bind", str(root), str(root)] == args[args.index("--bind"):args.index("--bind") + 3]
    assert ["--tmpfs", str(secret)] == args[args.index("--tmpfs"):args.index("--tmpfs") + 2]
    assert "--unshare-net" in args and args[-2:] == ["--", "ls"]
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(SandboxUnavailable):
        sandbox.bwrap(["ls"], policy)


async def test_bash_never_falls_back_to_running_unconfined(monkeypatch, box, tmp_path):
    root, _, _, policy = box

    def unavailable(argv, p):
        raise SandboxUnavailable("no sandbox here")

    monkeypatch.setattr("nano_workbuddy.tools.builtin.wrap", unavailable)
    ctx = ToolCtx(root=root, session_id="s", spill_dir=tmp_path / "sp", sandbox=policy, tmp=tmp_path / "t")
    from nano_workbuddy.tools import execute

    r = await execute(bash, {"type": "tool_call", "id": "c", "name": "bash", "args": {"command": "touch ran"}}, ctx)
    assert r.is_error and "no sandbox here" in text_of(r.content) and not (root / "ran").exists()


@darwin
async def test_bash_in_sandbox_hints_at_unsandboxed(box, tmp_path):
    root, outside, _, policy = box
    ctx = ToolCtx(root=root, session_id="s", spill_dir=tmp_path / "sp", sandbox=policy, tmp=tmp_path / "t")
    r = await bash.fn(BashArgs(command=f"touch {outside}/x"), ctx)
    assert "unsandboxed=true" in text_of(r.content)
    assert bash.permission_scope(BashArgs(command="ls", unsandboxed=True), ctx) == ("bash.unsandboxed", "ls")
    assert bash.permission_scope(BashArgs(command="ls"), ctx) == ("bash", "ls")


def test_symlinks_resolve_before_permission_checks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)  # a link inside the project pointing outside
    ctx = ToolCtx(root=root, session_id="s", spill_dir=tmp_path / "sp")
    assert resolve(ctx, "escape/x.txt") == (tmp_path / "x.txt").resolve()
