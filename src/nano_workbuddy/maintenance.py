"""Backup, restore and running at login (M3): `nwb backup`, `nwb restore`, `nwb service`.

A backup is one .tar.gz of ~/.nwb with what cannot be rebuilt: configuration, the SQLite index (copied with
SQLite's own backup API, so a running server does not leave it half-written), session logs, the content
store (delivered versions, uploads), saved tool outputs, memory and skills. Left out: caches that rebuild
themselves (parsed documents, the knowledge index), logs and the login token, and, unless asked for, the two
things that are secrets: API keys (secrets.json) and the browser profile with its logged-in cookies.

`nwb service install` makes macOS start `nwb serve` at login (a LaunchAgent) so scheduled tasks run without
a terminal open. A machine that is asleep or off still runs nothing; the scheduler catches up on wake.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ALWAYS = ("config.toml", "sessions", "files/blobs", "spill", "USER.md", "skills")
SECRET = ("secrets.json",)
BROWSER = ("browser/profile",)
LABEL = "ai.nano-workbuddy.serve"


def backup(home: Path, dest: Path, with_secrets: bool = False, with_browser: bool = False) -> dict:
    parts = [*ALWAYS, *(SECRET if with_secrets else ()), *(BROWSER if with_browser else ())]
    manifest = {"format": 1, "created_at": time.time(), "home": str(home), "included": [], "secrets": with_secrets,
                "browser_profile": with_browser}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(dest, "w:gz") as tar:
        if (home / "index.db").exists():  # a consistent copy even while the server writes to it
            copy = Path(tmp) / "index.db"
            src, out = sqlite3.connect(home / "index.db"), sqlite3.connect(copy)
            with out:
                src.backup(out)
            src.close()
            out.close()
            tar.add(copy, "index.db")
            manifest["included"].append("index.db")
        for part in parts:
            if (home / part).exists():
                tar.add(home / part, part, filter=_skip_junk)
                manifest["included"].append(part)
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
    os.chmod(dest, 0o600)  # sessions hold whatever was discussed: owner-only, like the home itself
    return manifest


def _skip_junk(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    return None if name in (".DS_Store",) or name.endswith((".tmp", "-wal", "-shm")) else info


def restore(archive: Path, home: Path, force: bool = False) -> dict:
    """Unpack a backup into home. An existing home is moved aside (home.before-restore-<time>) only with force."""
    with tarfile.open(archive, "r:gz") as tar:
        try:
            manifest = json.load(tar.extractfile("manifest.json"))
        except KeyError:
            raise ValueError(f"{archive} is not a nano-workbuddy backup (no manifest.json)") from None
        if (home / "config.toml").exists():
            if not force:
                raise FileExistsError(f"{home} already has data; use --force to move it aside and restore")
            home.rename(home.with_name(f"{home.name}.before-restore-{int(time.time())}"))
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        tar.extractall(home, filter="data")  # "data": no absolute paths, no .., no device files
    (home / "manifest.json").unlink(missing_ok=True)
    return manifest


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
