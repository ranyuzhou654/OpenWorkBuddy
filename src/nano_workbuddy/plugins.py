"""Optional capabilities. A plugin contributes tools/services; it never owns the agent loop.

Installed packages may publish a `nano_workbuddy.plugins` entry point returning Plugin(config).
Only explicitly enabled names are imported. No plugin installer, event bus or dependency framework.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from importlib.metadata import entry_points
from importlib.resources import files
from typing import Any

from .config import Config, McpServer, secret
from .tools.registry import Tool

log = logging.getLogger(__name__)


@dataclass
class Plugin:
    name: str
    description: str
    tools: tuple[Tool, ...] = ()
    services: dict[str, Any] = field(default_factory=dict)
    notices: dict[str, str] = field(default_factory=dict)
    mcp: dict[str, McpServer] = field(default_factory=dict)
    wrap_mcp: Callable[[str, list[Tool], set[str]], list[Tool]] | None = None
    cleanup: Callable[[str], None] | None = None


def search_provider(cfg: Config):
    from . import search

    if not cfg.search.api_key:
        return None
    try:
        return search.provider(cfg.search.provider, secret(cfg.search.api_key, cfg.home))
    except LookupError:
        return None


def feishu_client(cfg: Config, state: dict):
    from .connectors.feishu import Feishu

    if not cfg.feishu.enabled:
        return None
    current = state.get("feishu")
    if current is None or current.cli != Feishu(cfg.feishu.cli).cli:
        current = state["feishu"] = Feishu(cfg.feishu.cli)
    return current


def builtin(name: str, cfg: Config, state: dict) -> Plugin:
    if name == "documents":
        from .tools.docs import create_document, export_document, present

        instructions = files("nano_workbuddy").joinpath("prompts/documents.md").read_text()
        create = replace(create_document, description=create_document.description + "\n\n" + instructions)
        return Plugin(name, "Create and export local documents", (create, export_document, present))
    if name == "web":
        from .tools.web import web_fetch, web_search

        provider = search_provider(cfg)
        instructions = files("nano_workbuddy").joinpath("prompts/web.md").read_text()
        fetch = replace(web_fetch, description=web_fetch.description + "\n\n" + instructions)
        return Plugin(name, "Web pages and search", (fetch, *([web_search] if provider else [])),
                      services={"search": provider})
    if name == "feishu":
        from .connectors.feishu import feishu

        client = feishu_client(cfg, state)
        if client is None:
            return Plugin(name, "Feishu (not configured)")
        # Ordinary conversations must not make identity/network requests on account of an installed plugin.
        if not client.available:
            return Plugin(name, "Feishu", notices={name: "Feishu is enabled but lark-cli was not found."})
        return Plugin(name, "Feishu", (feishu,), services={"feishu": client},
                      notices={name: "Feishu tools are available when requested. Check identity and scope "
                                    "when accessing a resource; connection availability is not authorization. "
                                    "Never run lark-cli through bash. Writes require approval of the exact payload."})
    if name == "browser":
        from .connectors import browser

        if not cfg.browser.enabled:
            return Plugin(name, "Browser (not configured)")
        lease = state.setdefault("browser", browser.Lease())
        return Plugin(name, "Browser", mcp={browser.NAME: browser.spec(cfg.home.resolve(), cfg.browser.headless)},
                      notices={name: browser.NOTICE},
                      wrap_mcp=lambda server, tools, running: browser.wrap(tools, lease, running)
                      if server == browser.NAME else tools, cleanup=lease.release)
    raise KeyError(name)


BUILTINS = {"documents": "Local documents", "web": "Web pages and search", "feishu": "Feishu", "browser": "Browser"}


def catalog() -> dict[str, str]:
    return BUILTINS | {ep.name: f"Installed plugin ({ep.value})" for ep in entry_points(group="nano_workbuddy.plugins")}


def load(cfg: Config, state: dict, injected: Iterable[Plugin] = ()) -> list[Plugin]:
    external = {ep.name: ep for ep in entry_points(group="nano_workbuddy.plugins")}
    out = []
    for name in dict.fromkeys(cfg.plugins_enabled):
        try:
            if name in BUILTINS:
                out.append(builtin(name, cfg, state))
            elif name in external:
                plugin = external[name].load()(cfg)
                if not isinstance(plugin, Plugin) or plugin.name != name:
                    raise ValueError(f"plugin {name}: factory must return Plugin with the registered name")
                out.append(plugin)
            else:
                raise ValueError(f"plugin {name!r} is not installed")
        except Exception as exc:
            log.warning("Plugin %s could not load: %s", name, type(exc).__name__)
            out.append(Plugin(name, "Unavailable plugin", notices={f"plugin:{name}":
                f"Plugin {name!r} is unavailable ({type(exc).__name__}); its tools are not loaded."}))
    return [*out, *injected]
