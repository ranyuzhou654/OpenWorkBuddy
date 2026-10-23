"""~/.nwb/config.toml is the single source of truth for providers and models (D17).

Secrets are never stored in the file, only references: "env:NAME" or "keyring:NAME".
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
class Config:
    home: Path
    default_model: str
    models: dict[str, ModelSpec]
    max_turns: int = 50

    def model(self, name: str | None = None) -> ModelSpec:
        name = name or self.default_model
        if name not in self.models:
            raise KeyError(f"unknown model {name!r}; configured: {', '.join(self.models)}")
        return self.models[name]


def home_dir() -> Path:
    return Path(os.environ.get("NWB_HOME", "~/.nwb")).expanduser()


def load(home: Path | None = None) -> Config:
    home = home or home_dir()
    file = home / "config.toml"
    if not file.exists():
        home.mkdir(parents=True, exist_ok=True)
        file.write_text(DEFAULT_CONFIG, encoding="utf-8")
    raw = tomllib.loads(file.read_text(encoding="utf-8"))
    providers = {n: Provider(n, p["protocol"], p["base_url"], p["api_key"])
                 for n, p in raw.get("providers", {}).items()}
    models = {n: ModelSpec(n, providers[m["provider"]], m["id"], m["context_window"], m["max_output"],
                           m.get("params", {}))
              for n, m in raw.get("models", {}).items()}
    return Config(home, raw["default_model"], models, raw.get("max_turns", 50))


def secret(ref: str) -> str:
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
    raise ValueError(f"secret reference must be env:NAME or keyring:NAME, got {ref!r}")
