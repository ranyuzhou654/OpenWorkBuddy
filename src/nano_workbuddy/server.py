"""`nwb serve`: local token, built web assets, uvicorn. One process, one worker (the run manager is in memory)."""

from __future__ import annotations

import logging
import os
import secrets
import threading
import webbrowser
from importlib.resources import files
from pathlib import Path

import uvicorn

from .api import create_app
from .runtime import Workspace

log = logging.getLogger(__name__)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def load_token(home: Path) -> str:
    """A random token kept in ~/.nwb/token (0600), stable across restarts so bookmarks keep working."""
    f = home / "token"
    if not f.exists():
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(32))
    return f.read_text().strip()


def static_dir() -> Path:
    return Path(str(files("nano_workbuddy") / "static"))


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    ws = Workspace.open()
    token = load_token(ws.cfg.home)
    if host not in LOOPBACK:
        log.warning("listening on %s: anyone who can reach it and has the token can run commands as you", host)
    static = static_dir()
    if not (static / "index.html").exists():
        print("web UI is not built yet: cd web && npm install && npm run build  (the API still works)")
    url = f"http://{host}:{port}/api/login?token={token}"
    print(f"nano-workbuddy is running. Open this link to sign in:\n  {url}", flush=True)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    # SSE connections never end on their own; without a timeout a graceful shutdown would wait for them forever
    # (and the lifespan shutdown that marks runs "interrupted" would never run).
    uvicorn.run(create_app(ws, token, static), host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
