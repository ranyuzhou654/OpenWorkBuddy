"""Developer CLI: a thin REPL over Workspace, for exercising the loop before the web UI exists."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from .log import SessionLog
from .loop import CANCELLED
from .policy import PRESETS, Check
from .runtime import Ask, Workspace
from .tools import Result, Tool
from .types import ASSISTANT, USER, Entry, ToolCall, text_of

DIM, RED, CYAN, YELLOW, BOLD, RESET = "\033[2m", "\033[31m", "\033[36m", "\033[33m", "\033[1m", "\033[0m"

HELP = """\
/new                 start a new session in this project
/sessions            list sessions of this project
/open ID             switch to a session
/tree                show the conversation tree (* = current path)
/checkout ID         move the current position to an entry
/edit ID             go back to before user message ID; your next input becomes a new branch
/fork ID             copy the path up to ID into a new session
/model [NAME]        show or set the model for the next run
/preset [NAME]       show or set the permission preset: readonly, workspace, trust
/compact [FOCUS]     summarise older turns now (optionally saying what to keep)
/resume              continue a run interrupted by a crash
/quit"""


class Printer:
    """Renders events: durable ones come from the session log, ephemeral ones from the run."""

    def __init__(self) -> None:
        self.mode: str | None = None
        self.calls: dict[str, dict[str, Any]] = {}
        self.shown: set[str] = set()

    def watch(self, log: SessionLog) -> SessionLog:
        if self not in log.listeners:
            log.listeners.append(self)
        return log

    def _end_stream(self) -> None:
        if self.mode:
            print(RESET)
        self.mode = None

    def _call_header(self, call_id: str) -> None:
        if call_id in self.shown or call_id not in self.calls:
            return
        self.shown.add(call_id)
        c = self.calls[call_id]
        args = json.dumps(c["args"], ensure_ascii=False)
        print(f"{CYAN}⏺ {c['name']}{RESET}({args[:120]}{'…' if len(args) > 120 else ''})")

    def __call__(self, ev: dict[str, Any]) -> None:
        match ev["type"]:
            case "assistant.delta":
                if ev["kind"] != self.mode:
                    self._end_stream()
                    self.mode = ev["kind"]
                    print(DIM if self.mode == "thinking" else "", end="")
                print(ev["text"], end="", flush=True)
            case "entry":
                e = ev["entry"]
                if e["type"] == "assistant":
                    self._end_stream()
                    self.calls = {b["id"]: b for b in e["data"]["content"] if b["type"] == "tool_call"}
                    self.shown = set()  # ids are per turn; providers may reuse them
                elif e["type"] == "tool_started":
                    self._call_header(e["data"]["call_id"])
                elif e["type"] == "compaction":
                    self._end_stream()
                    print(f"{DIM}[context compacted: about {e['data']['tokens_before']} tokens summarised]{RESET}")
                elif e["type"] == "prune":
                    print(f"{DIM}[{e['data']['count']} old tool outputs cleared from context]{RESET}")
                elif e["type"] == "tool_result":
                    d = e["data"]
                    self._call_header(d["call_id"])  # calls blocked before starting still get a header
                    lines = text_of(d["content"]).splitlines() or [""]
                    more = f" … (+{len(lines) - 3} lines)" if len(lines) > 3 else ""
                    color = RED if d["is_error"] else DIM
                    print(color + "\n".join(f"  ⎿ {line[:160]}" for line in lines[:3]) + more + RESET)
            case "run.started" if ev.get("kind") == "compaction":
                print(f"{DIM}[compacting…]{RESET}")
            case "context.overflow":
                print(f"{DIM}[the model said the context is too long; compacting and retrying]{RESET}")
            case "run.resumed":
                print(f"{DIM}[resuming interrupted run {ev['run_id']}]{RESET}")
            case "run.finished":
                self._end_stream()
                u = ev["usage"]
                note = "" if ev["status"] == "ok" else f"{RED}[{ev['status']}]{RESET} "
                print(f"{note}{DIM}[tokens in {u.get('input', 0)} (cached {u.get('cache_read', 0)}), "
                      f"out {u.get('output', 0)}]{RESET}")


def terminal_ask(ws: Workspace, log: SessionLog) -> Ask:
    """Approvals in the terminal: y = once, a = always (this session), n [feedback] = deny."""

    async def ask(check: Check, tool: Tool, call: ToolCall) -> Result | None:
        PRINTER._end_stream()
        always = check.suggestion()
        print(f"{YELLOW}? {check.action}: {check.resource[:400]}{RESET}\n"
              f"  y = allow once · a = always allow {always.action} {always.resource[:80]} · n [reason] = deny")
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        loop.remove_signal_handler(signal.SIGINT)  # so Ctrl-C interrupts input() instead of waiting for Enter
        try:
            answer = input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            task.cancel()  # stop the run at its next await
            return Result.error(CANCELLED)
        finally:
            loop.add_signal_handler(signal.SIGINT, task.cancel)
        if answer.lower() in ("y", "yes"):
            return None
        if answer.lower() in ("a", "always"):
            ws.index.add_rule(log.id, always)
            return None
        feedback = answer[1:].strip() if answer[:1].lower() == "n" else answer
        return Result.error("The user denied this call." + (f" Their feedback: {feedback}" if feedback else ""))

    return ask


async def cancellable(coro: Coroutine[Any, Any, Any]) -> Any:
    """Ctrl-C cancels the run (killing its subprocesses) instead of the program."""
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        return "cancelled"  # the run.finished event already reported it
    finally:
        loop.remove_signal_handler(signal.SIGINT)


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        return asyncio.run(cancellable(coro))
    except Exception as e:  # shown to the user; the run is already marked as error
        print(f"{RED}error: {type(e).__name__}: {e}{RESET}")
        return "error"


def label(e: Entry) -> str:
    if e.type == USER:
        return f"{BOLD}you{RESET}: {text_of(e.data['content']).strip().splitlines()[0][:70]}"
    parts = [text_of(e.data["content"]).strip().split("\n")[0][:60]]
    if tools := [c["name"] for c in e.tool_calls]:
        parts.append("→ " + ", ".join(tools))
    return "ai: " + " ".join(p for p in parts if p)


def print_tree(log: SessionLog) -> None:
    on_path = {e.id for e in log.path()}
    kids: dict[str | None, list[Entry]] = {}
    for e in sorted(log.entries.values(), key=lambda e: e.seq):
        kids.setdefault(e.parent_id, []).append(e)
    stack = [(e, 0) for e in reversed(kids.get(None, []))]
    while stack:
        e, depth = stack.pop()
        children = kids.get(e.id, [])
        if e.type in (USER, ASSISTANT):
            mark = "*" if e.id in on_path else " "
            print(f"{'  ' * depth}{mark} {DIM}{e.id}{RESET} {label(e)}")
        step = 1 if len(children) > 1 else 0
        stack.extend((c, depth + step) for c in reversed(children))


def command(ws: Workspace, log: SessionLog, line: str) -> SessionLog | None:
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    match cmd:
        case "/quit" | "/exit":
            return None
        case "/new":
            log = PRINTER.watch(ws.new_session(ws.root_of(log), ws.index.session(log.id)["model_ref"]))
            print(f"new session {log.id}")
        case "/sessions":
            for s in ws.index.sessions(log.header["project_id"]):
                print(f"{'*' if s['id'] == log.id else ' '} {s['id']}  {s['title'] or '(empty)'}")
        case "/open":
            log = PRINTER.watch(ws.open_session(arg))
        case "/tree":
            print_tree(log)
        case "/checkout":
            log.checkout(log.resolve(arg))
        case "/edit":
            e = log.entries[log.resolve(arg)]
            if e.type != USER:
                print("only user messages can be edited")
            else:
                log.checkout(e.parent_id)
                print(f"{DIM}was: {text_of(e.data['content'])}{RESET}\ntype the new message:")
        case "/fork":
            log = PRINTER.watch(ws.fork(log, log.resolve(arg)))
            print(f"forked into {log.id}")
        case "/model":
            if arg:
                ws.set_model(log, arg)
            print(f"model: {ws.index.session(log.id)['model_ref']}  (configured: {', '.join(ws.cfg.models)})")
        case "/preset":
            if arg:
                if arg not in PRESETS:
                    print(f"presets: {', '.join(PRESETS)}")
                else:
                    ws.index.update_settings(log.id, {"preset": arg})
            print(f"preset: {ws.preset(log)}  (applies from the next run)")
        case "/compact":
            run(ws.compact(log, arg or None, PRINTER))
        case "/resume":
            run(ws.resume(log, PRINTER, ask=terminal_ask(ws, log)))
        case _:
            print(HELP)
    return log


def repl(ws: Workspace, log: SessionLog) -> None:
    print(f"{DIM}nano-workbuddy · session {log.id} · {ws.root_of(log)} · preset {ws.preset(log)} · "
          f"/help for commands{RESET}")
    while True:
        try:
            line = input(f"{BOLD}›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith("/"):
            try:
                nxt = command(ws, log, line)
            except KeyError as e:
                print(f"{RED}{e}{RESET}")
                continue
            if nxt is None:
                return
            log = nxt
            continue
        run(ws.submit(log, line, emit=PRINTER, ask=terminal_ask(ws, log)))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        return serve_main(argv[1:])
    p = argparse.ArgumentParser(prog="nwb", description="nano-workbuddy developer CLI (`nwb serve` starts the web UI)")
    p.add_argument("prompt", nargs="?", help="run one prompt and exit")
    p.add_argument("-C", "--project", type=Path, default=Path("."), help="project directory (default: cwd)")
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest session")
    p.add_argument("-s", "--session", help="open a session by id")
    p.add_argument("-m", "--model", help="model name from config.toml")
    p.add_argument("-p", "--preset", choices=list(PRESETS), help="permission preset for this session")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    ws = Workspace.open()
    root = args.project.resolve()
    if args.session:
        log = ws.open_session(args.session)
    elif args.cont and (recent := ws.index.sessions(ws.index.project_for(root)["id"])):
        log = ws.open_session(recent[0]["id"])
    else:
        log = ws.new_session(root, args.model)
    PRINTER.watch(log)
    if args.model:
        ws.set_model(log, args.model)
    if args.preset:
        ws.index.update_settings(log.id, {"preset": args.preset})
    if ws.index.unfinished_runs(log.id):  # D6: pick up where a crash left off
        run(ws.resume(log, PRINTER, ask=terminal_ask(ws, log)))
    if args.prompt:
        status = run(ws.submit(log, args.prompt, emit=PRINTER, ask=terminal_ask(ws, log)))
        sys.exit(0 if status in ("ok", "limit") else 1)
    repl(ws, log)


def serve_main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nwb serve", description="start the local web workbench")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("nano_workbuddy").setLevel(logging.INFO)
    from .server import serve

    serve(args.host, args.port, open_browser=not args.no_open)


PRINTER = Printer()

if __name__ == "__main__":
    main()
