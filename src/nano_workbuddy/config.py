"""~/.nwb/config.toml is the single source of truth for connections and models (D17).

A provider ("connection") is an endpoint plus one API key; two keys for the same service are two
connections. Keys are never written to config.toml, only references:
  "env:NAME"      an environment variable
  "keyring:NAME"  the system keychain
  "secret:NAME"   ~/.nwb/secrets.json, a 0600 file managed from the web settings page
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hooks import Hook
from .hooks import parse as parse_hooks
from .policy import Rule

DEFAULT_CONFIG = """\
default_model = "deepseek-flash"

[providers.deepseek]
protocol = "openai-chat"
base_url = "https://api.deepseek.com"
api_key = "env:DEEPSEEK_API_KEY"

[models.deepseek-flash]
provider = "deepseek"
id = "deepseek-flash"
context_window = 1_000_000
max_output = 32_000

[models.deepseek-v4-pro]
provider = "deepseek"
id = "deepseek-v4-pro"
context_window = 1_000_000
max_output = 32_000
"""

PROTOCOLS = ("openai-chat", "anthropic", "openai-responses")
EXPOSURES = ("auto", "direct", "deferred")


@dataclass(frozen=True)
class Provider:
    name: str
    protocol: str
    base_url: str
    api_key: str  # reference, resolve with secret()


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: Provider
    id: str
    context_window: int
    max_output: int
    params: dict[str, Any] = field(default_factory=dict)  # extra request body, e.g. thinking switches


@dataclass(frozen=True)
class McpServer:
    """[mcp.NAME]: a stdio command or a Streamable HTTP url (D14). Values in env and headers may be
    key references (env:/keyring:/secret:), resolved when the server starts."""

    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    exposure: str = "auto"  # auto: deferred when all MCP schemas would take more than 5% of the window
    sandbox: bool = True  # stdio only: run inside the OS sandbox
    network: bool = True
    writable: tuple[str, ...] = ()  # extra writable paths inside the sandbox, e.g. "~/.npm"
    timeout: int = 300  # seconds per tool call
    description: str = ""  # one line shown to the model while the server's tools are deferred


@dataclass(frozen=True)
class Config:
    home: Path
    default_model: str
    models: dict[str, ModelSpec]
    max_turns: int = 50
    rules: tuple[Rule, ...] = ()  # [[rules]] in config.toml, applied after the preset's own rules
    providers: dict[str, Provider] = field(default_factory=dict)
    mcp: dict[str, McpServer] = field(default_factory=dict)
    skills_disabled: tuple[str, ...] = ()  # [skills] disabled = [...]
    skill_dirs: tuple[str, ...] = ()  # [skills] dirs = [...]: more places to look for skills
    hooks: tuple[Hook, ...] = ()  # [[hooks.EVENT]] tables, Claude Code's events
    scratch_dir: Path = field(default_factory=lambda: default_scratch())  # where chats without a project work

    def model(self, name: str | None = None) -> ModelSpec:
        name = name or self.default_model
        if name not in self.models:
            raise KeyError(f"unknown model {name!r}; configured: {', '.join(self.models)}")
        return self.models[name]


def default_scratch() -> Path:
    """Visible, so the files a chat writes are easy to find; not inside ~/.nwb, which the sandbox hides."""
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "nano-workbuddy"


def home_dir() -> Path:
    return Path(os.environ.get("NWB_HOME", "~/.nwb")).expanduser()


def load(home: Path | None = None) -> Config:
    home = home or home_dir()
    file = home / "config.toml"
    if not file.exists():
        home.mkdir(parents=True, exist_ok=True)
        file.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return parse(home, tomllib.loads(file.read_text(encoding="utf-8")))


def parse(home: Path, raw: dict[str, Any]) -> Config:
    """Build and validate a Config; raises ValueError naming what is wrong."""
    providers = {n: Provider(n, p["protocol"], p["base_url"], p["api_key"])
                 for n, p in raw.get("providers", {}).items()}
    for p in providers.values():
        if p.protocol not in PROTOCOLS:
            raise ValueError(f"provider {p.name}: protocol must be one of {', '.join(PROTOCOLS)}")
    models = {}
    for n, m in raw.get("models", {}).items():
        if m["provider"] not in providers:
            raise ValueError(f"model {n}: unknown provider {m['provider']!r}")
        models[n] = ModelSpec(n, providers[m["provider"]], m["id"], int(m["context_window"]), int(m["max_output"]),
                              dict(m.get("params", {})))
    if raw.get("default_model") not in models:
        raise ValueError(f"default_model {raw.get('default_model')!r} is not a configured model")
    rules = tuple(Rule(r["action"], r["resource"], r["effect"]) for r in raw.get("rules", []))
    mcp = {n: _mcp(n, m) for n, m in raw.get("mcp", {}).items()}
    return Config(home, raw["default_model"], models, raw.get("max_turns", 50), rules, providers, mcp,
                  tuple(raw.get("skills", {}).get("disabled", ())), tuple(raw.get("skills", {}).get("dirs", ())),
                  parse_hooks(raw.get("hooks", {})),
                  Path(raw["scratch_dir"]).expanduser() if "scratch_dir" in raw else default_scratch())


def _mcp(name: str, m: dict[str, Any]) -> McpServer:
    if bool(m.get("command")) == bool(m.get("url")):
        raise ValueError(f"mcp {name}: set exactly one of command (stdio) or url (Streamable HTTP)")
    if m.get("exposure", "auto") not in EXPOSURES:
        raise ValueError(f"mcp {name}: exposure must be one of {', '.join(EXPOSURES)}")
    return McpServer(name, m.get("command", ""), tuple(m.get("args", ())), dict(m.get("env", {})), m.get("url", ""),
                     dict(m.get("headers", {})), m.get("enabled", True), m.get("exposure", "auto"),
                     m.get("sandbox", True), m.get("network", True), tuple(m.get("writable", ())),
                     int(m.get("timeout", 300)), m.get("description", ""))


class SecretStore:
    """~/.nwb/secrets.json: name -> key, readable only by the owner (0600), replaced atomically."""

    def __init__(self, home: Path):
        self.file = home / "secrets.json"

    def _read(self) -> dict[str, str]:
        return json.loads(self.file.read_text()) if self.file.exists() else {}

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def names(self) -> list[str]:
        return list(self._read())

    def set(self, name: str, value: str) -> None:
        self._write({**self._read(), name: value})

    def delete(self, name: str) -> None:
        data = self._read()
        if data.pop(name, None) is not None:
            self._write(data)

    def _write(self, data: dict[str, str]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.file.parent, prefix=".secrets-")  # created 0600
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2)
        os.replace(tmp, self.file)


def secret(ref: str, home: Path | None = None) -> str:
    kind, _, name = ref.partition(":")
    if kind == "env":
        if value := os.environ.get(name):
            return value
        raise LookupError(f"environment variable {name} is not set")
    if kind == "keyring":
        import keyring

        if value := keyring.get_password("nano-workbuddy", name):
            return value
        raise LookupError(f"no keyring entry nano-workbuddy/{name}")
    if kind == "secret":
        if value := SecretStore(home or home_dir()).get(name):
            return value
        raise LookupError(f"no key saved for {name!r}; add it on the model settings page")
    raise ValueError(f"secret reference must be env:NAME, keyring:NAME or secret:NAME, got {ref!r}")


def resolve(value: str, home: Path) -> str:
    """A value that is exactly a key reference is replaced by the key; anything else is literal."""
    kind, sep, name = value.partition(":")
    return secret(value, home) if sep and kind in ("env", "keyring", "secret") and name else value


def key_status(ref: str, home: Path) -> dict[str, Any]:
    """What the settings page may show about a key: where it comes from, whether it exists, last 4 chars."""
    kind, _, name = ref.partition(":")
    try:
        value = secret(ref, home)
    except (LookupError, ValueError):
        return {"source": kind, "name": name, "set": False}
    return {"source": kind, "name": name, "set": True, "hint": f"…{value[-4:]}" if len(value) > 8 else "已设置"}
