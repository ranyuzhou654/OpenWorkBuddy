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
from typing import Any, Literal
from urllib.parse import urlsplit

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


def _deny_secrets(memory: Effect) -> tuple[Rule, ...]:
    """Credentials are off limits for every tool. Inside {home}, this session's spill files and the
    skills are readable again, and USER.md is writable through the memory tool (D18)."""
    denies = tuple(r for s in SECRETS for r in (Rule("*", s, "deny"), Rule("*", f"{s}/*", "deny")))
    return (*denies, Rule("read", "{spill}/*", "allow"), Rule("read", "{home}/skills/*", "allow"),
            Rule("memory", "{home}/USER.md", memory))


# Harness tools that touch only the conversation (tool catalog, skills, the plan, a question) never need approval.
HARNESS = (Rule("tool_search", "*", "allow"), Rule("skill", "*", "allow"), Rule("todo", "*", "allow"),
           Rule("ask_user", "*", "allow"))

# Reading through connectors (M2): Feishu read commands, and the browser tools that only look at the page.
BROWSER_READ = ("snapshot", "find", "take_screenshot", "console_messages", "network_requests", "network_request",
                "wait_for", "tabs", "navigate_back", "resize", "emulate_media", "close", "hover")
CONNECTOR_READS = (Rule("feishu", "*", "allow"),
                   *(Rule(f"mcp__browser__browser_{t}", "*", "allow") for t in BROWSER_READ))

# Approved per call and never "always": writes to the user's Feishu send or publish as the user.
ONCE_ONLY = ("feishu.write",)

# Appended after every other rule (last match wins), so no preset, config rule or session approval loosens
# them: Feishu writes always ask, and lark-cli through bash (which would bypass the connector) asks too.
FIXED = (Rule("feishu.write", "*", "ask"), Rule("bash", "*lark-cli*", "ask"),
         Rule("bash.unsandboxed", "*lark-cli*", "ask"))


def _ask_protected() -> tuple[Rule, ...]:
    return tuple(Rule(a, f"{{root}}/{d}/*", "ask") for a in ("write", "edit") for d in PROTECTED)


SANDBOX_TEXT = ("Shell commands run in a sandbox: {writable}; everything else is read-only; network access is "
                "blocked; credential directories (~/.ssh, ~/.aws, ...) are unreadable. If a command genuinely "
                "needs the network or must write elsewhere, call bash with unsandboxed=true and the user will "
                "be asked to approve it.")

PRESETS: dict[str, Preset] = {
    "readonly": Preset(
        "只读", "read-only",
        (Rule("*", "*", "ask"), Rule("read", "*", "allow"), *HARNESS, *CONNECTOR_READS, *_deny_secrets("ask")),
        "Permission preset: read-only. You may read files freely; every write, edit or command needs the "
        "user's approval. " + SANDBOX_TEXT.format(writable="only $TMPDIR is writable")),
    "workspace": Preset(
        "工作区", "workspace",
        (Rule("*", "*", "ask"), Rule("read", "*", "allow"), Rule("write", "{root}/*", "allow"),
         Rule("edit", "{root}/*", "allow"), Rule("bash", "*", "allow"), Rule("memory", "*", "allow"),
         Rule("web_search", "*", "allow"), *HARNESS, *CONNECTOR_READS,
         *_ask_protected(), *_deny_secrets("allow")),
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
    preview: Any = None  # what the approval card shows besides the call (a connector's dry run)

    @property
    def once_only(self) -> bool:
        return self.action in ONCE_ONLY

    def suggestion(self) -> Rule:
        """What "always allow" adds: the containing directory for paths, the exact command otherwise.
        Resources are escaped so a command like `rm *` is never turned into a wildcard."""
        if self.resource.startswith("/"):
            return Rule(self.action, glob.escape(str(Path(self.resource).parent)) + "/*", "allow")
        if self.resource.startswith(("http://", "https://")):  # a site: scheme://host/*
            u = urlsplit(self.resource)
            return Rule(self.action, glob.escape(f"{u.scheme}://{u.netloc}") + "/*", "allow")
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
                         deny_read=_secret_paths(home), allow_read=(spill, home / "skills"), network=False)


def server_sandbox(writable: tuple[Path, ...], network: bool, home: Path) -> SandboxPolicy:
    """For stdio MCP servers: they serve every session, so no project is writable by default."""
    return SandboxPolicy(writable=writable, protected=(), deny_read=_secret_paths(home), allow_read=(),
                         network=network)
