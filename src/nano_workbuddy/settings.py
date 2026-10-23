"""Edits to config.toml made from the web settings page or `nwb key`.

tomlkit edits the document in place, so comments and formatting the user wrote by hand survive.
Every edit is validated by parsing the result before it is written, the previous file is kept as
config.toml.bak, and the new file replaces the old one atomically.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table

from .config import PROTOCOLS, Config, SecretStore, parse

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SettingsError(ValueError):
    """The requested change is invalid; nothing was written."""


def _check_name(kind: str, name: str) -> None:
    if not NAME.match(name):
        raise SettingsError(f"{kind} name may use letters, digits, '.', '_' and '-' (max 64): {name!r}")


def _section(doc: tomlkit.TOMLDocument, key: str) -> Table:
    if key not in doc:
        doc[key] = tomlkit.table(is_super_table=True)  # renders as [key.name] headers only
    return doc[key]


def edit(home: Path, change: Callable[[tomlkit.TOMLDocument], None]) -> Config:
    file = home / "config.toml"
    doc = tomlkit.parse(file.read_text(encoding="utf-8"))
    change(doc)
    text = tomlkit.dumps(doc)
    try:
        cfg = parse(home, tomllib.loads(text))
    except (ValueError, KeyError, tomllib.TOMLDecodeError) as e:
        raise SettingsError(str(e)) from e
    shutil.copy2(file, file.with_name("config.toml.bak"))
    fd, tmp = tempfile.mkstemp(dir=home, prefix=".config-")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, file)
    return cfg


def put_provider(home: Path, name: str, protocol: str, base_url: str, api_key: str | None = None,
                 key_ref: str | None = None) -> Config:
    """Create or update a connection. api_key (plaintext) is stored in secrets.json as secret:<name>;
    key_ref points at an existing env:/keyring: reference instead; neither keeps the current key."""
    _check_name("connection", name)
    if protocol not in PROTOCOLS:
        raise SettingsError(f"protocol must be one of {', '.join(PROTOCOLS)}")
    if not base_url.startswith(("http://", "https://")):
        raise SettingsError("base URL must start with http:// or https://")

    def change(doc: tomlkit.TOMLDocument) -> None:
        providers = _section(doc, "providers")
        table = providers.get(name)
        new = table is None
        if new:
            table = tomlkit.table()
        table["protocol"] = protocol
        table["base_url"] = base_url.rstrip("/")
        if api_key:
            table["api_key"] = f"secret:{name}"
        elif key_ref:
            table["api_key"] = key_ref
        elif "api_key" not in table:
            table["api_key"] = f"secret:{name}"  # set the key later from the settings page
        if new:
            table.add(tomlkit.nl())
            providers[name] = table

    cfg = edit(home, change)
    if api_key:  # only after the config is known to be valid
        SecretStore(home).set(name, api_key.strip())
    return cfg


def delete_provider(home: Path, name: str, cfg: Config) -> Config:
    if users := [m.name for m in cfg.models.values() if m.provider.name == name]:
        raise SettingsError(f"connection {name} is used by models: {', '.join(users)}; delete or move them first")

    def change(doc: tomlkit.TOMLDocument) -> None:
        if name not in doc.get("providers", {}):
            raise SettingsError(f"no connection {name!r}")
        del doc["providers"][name]

    new = edit(home, change)
    SecretStore(home).delete(name)
    return new


def put_model(home: Path, name: str, provider: str, model_id: str, context_window: int, max_output: int,
              params: dict[str, Any] | None = None) -> Config:
    _check_name("model", name)
    if not model_id.strip():
        raise SettingsError("model id is required")
    if not 1_000 <= context_window <= 100_000_000 or not 1 <= max_output < context_window:
        raise SettingsError("context window must be ≥ 1000 and larger than max output")

    def change(doc: tomlkit.TOMLDocument) -> None:
        models = _section(doc, "models")
        table = models.get(name)
        new = table is None
        if new:
            table = tomlkit.table()
        table["provider"] = provider
        table["id"] = model_id.strip()
        table["context_window"] = context_window
        table["max_output"] = max_output
        if params:
            inline = tomlkit.inline_table()
            inline.update(params)
            table["params"] = inline
        elif "params" in table:
            del table["params"]
        if new:
            table.add(tomlkit.nl())
            models[name] = table

    return edit(home, change)


def delete_model(home: Path, name: str) -> Config:
    def change(doc: tomlkit.TOMLDocument) -> None:
        if doc.get("default_model") == name:
            raise SettingsError(f"{name} is the default model; choose another default first")
        if name not in doc.get("models", {}):
            raise SettingsError(f"no model {name!r}")
        del doc["models"][name]

    return edit(home, change)


def set_default(home: Path, name: str) -> Config:
    def change(doc: tomlkit.TOMLDocument) -> None:
        doc["default_model"] = name

    return edit(home, change)
