from .builtin import bash, edit, read, write
from .interact import ask_user, todo
from .recall import read_record, recall
from .registry import Registry, Result, Tool, ToolCtx, ToolError, execute, tool
from .search import glob, grep

CORE = [read, write, edit, bash, grep, glob, recall, read_record, todo, ask_user]


def __getattr__(name):
    # Compatibility for integrations selecting individual tools; the runtime imports only CORE.
    if name == "BUILTIN":
        from .docs import create_document, export_document, present
        from .web import web_fetch, web_search

        return [*CORE, web_search, web_fetch, create_document, export_document, present]
    raise AttributeError(name)

__all__ = ["CORE", "BUILTIN", "Registry", "Result", "Tool", "ToolCtx", "ToolError", "execute", "tool"]
