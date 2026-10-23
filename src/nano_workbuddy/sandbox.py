"""OS-level isolation for commands (D13).

File tools are checked in-process by policy.py, but a shell command can do anything its process
can; only the kernel can confine it. `wrap()` turns an argv into one that runs under
macOS Seatbelt (sandbox-exec) or Linux bubblewrap. There is no silent fallback: if no backend
exists, wrap() raises and the command does not run.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class SandboxUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SandboxPolicy:
    writable: tuple[Path, ...]  # empty means read-only (except /dev/null and friends)
    protected: tuple[Path, ...]  # never writable, even inside a writable root (.git, .nwb)
    deny_read: tuple[Path, ...]  # credentials and other private data
    allow_read: tuple[Path, ...]  # exceptions inside deny_read (this session's spill files)
    network: bool


# Deny by default, then allow what ordinary command-line tools need. Later rules win, so the
# per-policy rules appended after this base can carve exceptions out of it.
SEATBELT_BASE = """\
(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow file-read*)
(allow file-read* file-write* file-ioctl
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper"))
(allow pseudo-tty)
(allow ipc-posix-sem)
(allow user-preference-read)
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.bsd.dirhelper"))
"""

SEATBELT_NETWORK = """\
(allow network-outbound network-inbound network-bind)
(allow system-socket)
(allow mach-lookup
  (global-name "com.apple.dnssd.service")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
  (global-name "com.apple.networkd")
  (global-name "com.apple.trustd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.ocspd"))
"""


def wrap(argv: list[str], policy: SandboxPolicy) -> list[str]:
    if sys.platform == "darwin":
        return seatbelt(argv, policy)
    if sys.platform.startswith("linux"):
        return bwrap(argv, policy)
    raise SandboxUnavailable(f"no sandbox backend for {sys.platform}")


def seatbelt(argv: list[str], p: SandboxPolicy) -> list[str]:
    if not Path(SANDBOX_EXEC).exists():
        raise SandboxUnavailable(f"{SANDBOX_EXEC} not found")
    lines, params = [SEATBELT_BASE], []

    def rule(action: str, key: str, paths: tuple[Path, ...]) -> None:
        if not paths:
            return
        names = []
        for i, path in enumerate(paths):  # paths travel as -D parameters: no quoting problems
            params.extend(["-D", f"{key}{i}={path.resolve()}"])
            names.append(f'(subpath (param "{key}{i}"))')
        lines.append(f"({action} {' '.join(names)})")

    rule("deny file-read*", "R", p.deny_read)
    rule("allow file-read*", "A", p.allow_read)
    rule("allow file-write*", "W", p.writable)
    rule("deny file-write*", "P", p.protected)
    if p.network:
        lines.append(SEATBELT_NETWORK)
    return [SANDBOX_EXEC, "-p", "\n".join(lines), *params, "--", *argv]


def bwrap(argv: list[str], p: SandboxPolicy) -> list[str]:
    if not (exe := shutil.which("bwrap")):
        raise SandboxUnavailable("bubblewrap (bwrap) is not installed")
    args = [exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent", "--unshare-pid"]
    for path in p.writable:
        args += ["--bind", str(path), str(path)]
    for path in p.protected:
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for path in p.deny_read:
        if path.is_dir():
            args += ["--tmpfs", str(path)]
        elif path.exists():
            args += ["--ro-bind", "/dev/null", str(path)]
    for path in p.allow_read:
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    if not p.network:
        args.append("--unshare-net")
    return [*args, "--", *argv]
