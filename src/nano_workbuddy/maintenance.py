"""Backup, restore and running at login (M3): `nwb backup`, `nwb restore`, `nwb service`.

A backup is one .tar.gz of ~/.nwb with what cannot be rebuilt: configuration, the SQLite index (copied with
SQLite's own backup API while the server and agent writers are stopped), session logs, the content
store (delivered versions, uploads), saved tool outputs, memory and skills. Left out: caches that rebuild
themselves (parsed documents, the knowledge index), logs and the login token, and, unless asked for, the two
things that are secrets: API keys (secrets.json) and the browser profile with its logged-in cookies.

`nwb service install` makes macOS start `nwb serve` at login (a LaunchAgent) so scheduled tasks run without
a terminal open. A machine that is asleep or off still runs nothing; the scheduler catches up on wake.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ALWAYS = ("config.toml", "sessions", "files/blobs", "spill", "usage", "USER.md", "skills", "restore.json")
SECRET = ("secrets.json",)
BROWSER = ("browser/profile",)
LABEL = "ai.nano-workbuddy.serve"


@contextlib.contextmanager
def home_guard(home: Path, *, exclusive: bool = False):
    """Coordinate offline maintenance with the server and agent writers. The lock survives home renames."""
    home = home.expanduser().resolve()
    home.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(home.parent / f".{home.name}.maintenance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("home is in use: stop the server and agent writers before backup/restore") from None
        yield
    finally:
        os.close(fd)


def _sha(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@contextlib.contextmanager
def _offline(home: Path):
    with home_guard(home, exclusive=True), contextlib.ExitStack() as stack:
        for p in sorted((home / "sessions").glob("*/*.jsonl")):
            f = stack.enter_context(p.open("rb"))
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("a session writer is active; stop it before backup/restore") from None
        yield


def backup(home: Path, dest: Path, with_secrets: bool = False, with_browser: bool = False) -> dict:
    home, dest = home.expanduser().resolve(), dest.expanduser().resolve()
    if dest.is_relative_to(home):
        raise ValueError("store the backup outside the application home")
    with _offline(home):
        return _backup(home, dest, with_secrets, with_browser)


def _backup(home: Path, dest: Path, with_secrets: bool, with_browser: bool) -> dict:
    if not (home / "config.toml").is_file():
        raise FileNotFoundError(f"{home} has no application configuration to back up")
    parts = [*ALWAYS, *(SECRET if with_secrets else ()), *(BROWSER if with_browser else ())]
    manifest = {"format": 2, "created_at": time.time(), "home": str(home), "included": [], "secrets": with_secrets,
                "browser_profile": with_browser, "consistency": "offline", "files": {}, "projects": [],
                "project_worktrees_included": False}
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, pending = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp)
            _stage_backup(home, staged, parts, manifest)
            with tarfile.open(pending, "w:gz") as tar:
                for p in sorted(staged.rglob("*")):
                    if p.is_file():
                        name = p.relative_to(staged).as_posix()
                        manifest["files"][name] = {"bytes": p.stat().st_size, "sha256": _sha(p)}
                        tar.add(p, name, recursive=False)
                data = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mtime = len(data), int(time.time())
                tar.addfile(info, io.BytesIO(data))
            with open(pending, "rb") as f:
                os.fsync(f.fileno())
            os.replace(pending, dest)
    finally:
        Path(pending).unlink(missing_ok=True)
    return manifest


def _stage_backup(home: Path, staged: Path, parts: list[str], manifest: dict) -> None:
    if (home / "index.db").exists():
        src, out = sqlite3.connect(home / "index.db"), sqlite3.connect(staged / "index.db")
        try:
            src.backup(out)
            manifest["projects"] = [{"id": r[0], "root_path": r[1]} for r in src.execute(
                "select id,root_path from projects")]
        finally:
            src.close()
            out.close()
        manifest["included"].append("index.db")
    else:
        from .index import Index

        Index(staged / "index.db").db.close()
        manifest["included"].append("index.db")
    for part in parts:
        source = home / part
        if source.is_symlink() or not source.exists():
            continue
        dest = staged / part
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, ignore=lambda folder, names: [n for n in names if
                Path(folder, n).is_symlink() or n == ".DS_Store" or n.endswith((".tmp", "-wal", "-shm"))])
        else:
            shutil.copy2(source, dest)
        manifest["included"].append(part)


def restore(archive: Path, home: Path, force: bool = False) -> dict:
    """Unpack a backup into home. An existing home is moved aside (home.before-restore-<time>) only with force."""
    home = home.expanduser().resolve()
    with _offline(home), tempfile.TemporaryDirectory(dir=home.parent, prefix=".nwb-restore-") as tmp:
        _recover_restore(home, retry=True)
        staged = Path(tmp) / "home"
        staged.mkdir(mode=0o700)
        manifest = _extract_verified(archive, staged)
        _validate_home(staged)
        from .relocation import relocate

        manifest["relocation"] = relocate(staged, Path(manifest["home"]), home)
        _validate_home(staged)
        (staged / "restore.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if home.exists() and any(home.iterdir()) and not force:
            raise FileExistsError(f"{home} already has data; use --force to move it aside and restore")
        moved = None
        if home.exists():
            moved = home.with_name(f"{home.name}.before-restore-{time.time_ns()}")
        marker = _restore_marker(home)
        from .tools.builtin import atomic_write

        atomic_write(marker, json.dumps({"previous": str(moved) if moved else None, "staged": str(staged)}))
        marker.chmod(0o600)
        if moved:
            home.rename(moved)
        try:
            os.replace(staged, home)
        except BaseException:
            if moved:
                moved.rename(home)
            marker.unlink(missing_ok=True)
            raise
        marker.unlink(missing_ok=True)
        return manifest


def _restore_marker(home: Path) -> Path:
    return home.parent / f".{home.name}.restore-pending.json"


def recover_restore(home: Path) -> None:
    home = home.expanduser().resolve()
    if _restore_marker(home).exists():
        with home_guard(home, exclusive=True):
            _recover_restore(home)


def _recover_restore(home: Path, retry: bool = False) -> None:
    marker = _restore_marker(home)
    if not marker.exists():
        return
    pending = json.loads(marker.read_text())
    previous = Path(pending["previous"]) if pending["previous"] else None
    if not home.exists():
        if previous and previous.exists():
            previous.rename(home)
        elif not retry:
            raise RuntimeError("an interrupted restore has no previous home; retry nwb restore before starting")
    marker.unlink()


def _extract_verified(archive: Path, staged: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        seen = set()
        for item in members:
            if Path(item.name).is_absolute() or ".." in Path(item.name).parts:
                raise tarfile.FilterError("backup member escapes the target directory")
            tarfile.data_filter(item, str(staged))
            if item.issym() or item.islnk() or not (item.isfile() or item.isdir()):
                raise tarfile.FilterError("backup contains a link or special file")
            if item.name in seen:
                raise ValueError("backup contains duplicate member names")
            seen.add(item.name)
        try:
            manifest = json.load(tar.extractfile("manifest.json"))
        except KeyError:
            raise ValueError(f"{archive} is not a nano-workbuddy backup (no manifest.json)") from None
        if manifest.get("format") not in (1, 2) or not Path(manifest.get("home", "")).is_absolute():
            raise ValueError("unsupported or incomplete backup manifest")
        tar.extractall(staged, filter="data")
    if manifest["format"] == 2:
        files = {p.relative_to(staged).as_posix(): p for p in staged.rglob("*")
                 if p.is_file() and p != staged / "manifest.json"}
        if files.keys() != manifest.get("files", {}).keys():
            raise ValueError("backup file inventory does not match manifest")
        for name, p in files.items():
            expected = manifest["files"][name]
            if p.stat().st_size != expected["bytes"] or _sha(p) != expected["sha256"]:
                raise ValueError(f"backup integrity check failed: {name}")
    manifest["integrity_verified"] = manifest["format"] == 2
    (staged / "manifest.json").unlink()
    return manifest


def _validate_home(home: Path) -> None:
    from .config import load
    from .log import SessionLog

    if not (home / "config.toml").is_file() or not (home / "index.db").is_file():
        raise ValueError("backup lacks configuration or index")
    load(home)
    db = sqlite3.connect(home / "index.db")
    try:
        if db.execute("pragma quick_check").fetchone()[0] != "ok":
            raise ValueError("backup database failed quick_check")
    finally:
        db.close()
    for p in (home / "sessions").glob("*/*.jsonl"):
        SessionLog.open(p)


# --- macOS LaunchAgent ---


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def service_plist(home: Path, port: int = 8765) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/opt/homebrew/bin")}  # npx, lark-cli, rg
    if os.environ.get("NWB_HOME"):
        env["NWB_HOME"] = os.environ["NWB_HOME"]
    (home / "logs").mkdir(parents=True, exist_ok=True)
    return {"Label": LABEL, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ProgramArguments": [sys.executable, "-c", "from nano_workbuddy.cli import main; main()",
                                 "serve", "--no-open", "--port", str(port)],
            "EnvironmentVariables": env, "ProcessType": "Background",
            "StandardOutPath": str(home / "logs" / "serve.log"), "StandardErrorPath": str(home / "logs" / "serve.log")}


def service(action: str, home: Path, port: int = 8765) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("starting at login is set up for macOS only; on Linux use a systemd user unit")
    domain, path = f"gui/{os.getuid()}", plist_path()
    if action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(service_plist(home, port)))
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or "launchctl bootstrap failed")
        return f"installed {path}; nwb serve now starts at login (log: {home / 'logs' / 'serve.log'})"
    if action == "uninstall":
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.unlink(missing_ok=True)
        return "removed: nwb serve no longer starts at login"
    if action == "status":
        r = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True, text=True)
        return "not installed" if r.returncode else next(
            (ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("state")), "installed")
    raise ValueError("action must be install, uninstall or status")
