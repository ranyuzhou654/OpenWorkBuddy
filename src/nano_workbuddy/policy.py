"""Who may do what (D12).

Every tool call is reduced to (action, resource) - e.g. ("write", "/abs/path") or ("bash", "git status")
- and checked against an ordered rule list where the LAST matching rule wins:

    preset defaults  <  [[rules]] in config.toml  <  rules the user approved in this session

A preset also decides how shell commands are sandboxed (sandbox.py). Rules guard what the agent asks
to do; the sandbox guards what a running command can actually reach.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from .sandbox import SandboxPolicy

type Effect = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class Rule:
    action: str  # tool name, "bash.unsandboxed", or a glob such as "*"
    resource: str  # glob; "~" and {root} {home} {spill} expand, "*" also crosses "/"
    effect: Effect

    def matches(self, action: str, resource: str, places: dict[str, str]) -> bool:
        pattern = self.resource
        for name, value in places.items():
            pattern = pattern.replace(f"{{{name}}}", value)
        return fnmatchcase(action, self.action) and fnmatchcase(resource, os.path.expanduser(pattern))


@dataclass(frozen=True)
class Preset:
    label: str
    sandbox: Literal["read-only", "workspace", "off"]
    rules: tuple[Rule, ...]
    notice: str  # what the model is told about its permissions


SECRETS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.kube", "~/.docker", "~/.config/gh", "~/.netrc",
           "~/Library/Keychains", "{home}")
PROTECTED = (".git", ".nwb")  # inside the project, but changes need approval (hooks, agent config)


def _deny_secrets() -> tuple[Rule, ...]:
    """Credentials are off limits for every tool. This session's spill files sit inside {home},
    so reading them is re-allowed after the deny."""
    denies = tuple(r for s in SECRETS for r in (Rule("*", s, "deny"), Rule("*", f"{s}/*", "deny")))
    return (*denies, Rule("read", "{spill}/*", "allow"))


# Harness tools that only read the harness's own data (tool catalog, skill files) never need approval.
HARNESS = (Rule("tool_search", "*", "allow"),)


def _ask_protected() -> tuple[Rule, ...]:
    return tuple(Rule(a, f"{{root}}/{d}/*", "ask") for a in ("write", "edit") for d in PROTECTED)


SANDBOX_TEXT = ("Shell commands run in a sandbox: {writable}; everything else is read-only; network access is "
                "blocked; credential directories (~/.ssh, ~/.aws, ...) are unreadable. If a command genuinely "
                "needs the network or must write elsewhere, call bash with unsandboxed=true and the user will "
                "be asked to approve it.")

PRESETS: dict[str, Preset] = {
    "readonly": Preset(
        "只读", "read-only",
        (Rule("*", "*", "ask"), Rule("read", "*", "allow"), *HARNESS, *_deny_secrets()),
        "Permission preset: read-only. You may read files freely; every write, edit or command needs the "
        "user's approval. " + SANDBOX_TEXT.format(writable="only $TMPDIR is writable")),
    "workspace": Preset(
        "工作区", "workspace",
        (Rule("*", "*", "ask"), Rule("read", "*", "allow"), Rule("write", "{root}/*", "allow"),
         Rule("edit", "{root}/*", "allow"), Rule("bash", "*", "allow"), *HARNESS, *_ask_protected(),
         *_deny_secrets()),
        "Permission preset: workspace. Reading, and editing files inside the project ({root}), need no approval "
        "(except .git and .nwb). Changes outside the project need the user's approval. "
        + SANDBOX_TEXT.format(writable="the project directory and $TMPDIR are writable")),
    "trust": Preset(
        "完全信任", "off",
        (Rule("*", "*", "allow"),),
        "Permission preset: full trust. Tools run with the user's full permissions, without a sandbox and "
        "without asking. Take extra care with destructive or irreversible actions."),
}


@dataclass(frozen=True)
class Check:
    action: str
    resource: str
    effect: Effect
    rule: Rule | None  # the rule that decided; None means the built-in default (ask)

    def suggestion(self) -> Rule:
        """What "always allow" adds: the containing directory for paths, the exact command otherwise.
        Resources are escaped so a command like `rm *` is never turned into a wildcard."""
        if self.resource.startswith("/"):
            return Rule(self.action, glob.escape(str(Path(self.resource).parent)) + "/*", "allow")
        return Rule(self.action, glob.escape(self.resource), "allow")


def check(action: str, resource: str, rules: list[Rule], places: dict[str, str]) -> Check:
    """places: values for {root}, {home}, {spill} in rule resources."""
    for rule in reversed(rules):
        if rule.matches(action, resource, places):
            return Check(action, resource, rule.effect, rule)
    return Check(action, resource, "ask", None)


def _secret_paths(home: Path) -> tuple[Path, ...]:
    return tuple(Path(os.path.expanduser(s.replace("{home}", str(home)))) for s in SECRETS)


def sandbox_policy(preset: Preset, root: Path, tmp: Path, home: Path, spill: Path) -> SandboxPolicy | None:
    if preset.sandbox == "off":
        return None
    writable = (root, tmp) if preset.sandbox == "workspace" else (tmp,)
    return SandboxPolicy(writable=writable, protected=tuple(root / d for d in PROTECTED),
                         deny_read=_secret_paths(home), allow_read=(spill,), network=False)


def server_sandbox(writable: tuple[Path, ...], network: bool, home: Path) -> SandboxPolicy:
    """For stdio MCP servers: they serve every session, so no project is writable by default."""
    return SandboxPolicy(writable=writable, protected=(), deny_read=_secret_paths(home), allow_read=(),
                         network=network)
