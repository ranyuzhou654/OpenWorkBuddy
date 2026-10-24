"""System notifications (M3): macOS Notification Center through osascript, notify-send on Linux.

Best effort and never fatal: a notification that cannot be shown is logged, the task's result is still in
the task page. Nothing is sent off the machine.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

log = logging.getLogger(__name__)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def notify(title: str, body: str, subtitle: str = "") -> bool:
    body = body.strip().replace("\n", " ")[:240]
    if sys.platform == "darwin":
        script = f"display notification {_quote(body)} with title {_quote(title)}"
        if subtitle:
            script += f" subtitle {_quote(subtitle[:120])}"
        argv = ["osascript", "-e", script]
    elif shutil.which("notify-send"):
        argv = ["notify-send", f"{title} · {subtitle}" if subtitle else title, body]
    else:
        log.info("notification (no notifier available): %s: %s", title, body)
        return False
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, TimeoutError) as e:
        log.warning("notification failed: %s", e)
        return False
    if proc.returncode:
        log.warning("notification failed: %s", err.decode(errors="replace")[:200])
    return proc.returncode == 0
