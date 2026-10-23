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
from .runtime import Workspace
from .types import ASSISTANT, USER, Entry, text_of

DIM, RED, CYAN, BOLD, RESET = "\033[2m", "\033[31m", "\033[36m", "\033[1m", "\033[0m"

HELP = """\
/new                 start a new session in this project
/sessions            list sessions of this project
/open ID             switch to a session
/tree                show the conversation tree (* = current path)
/checkout ID         move the current position to an entry
/edit ID             go back to before user message ID; your next input becomes a new branch
/fork ID             copy the path up to ID into a new session
/model [NAME]        show or set the model for the next run
/resume              continue a run interrupted by a crash
/quit"""


class Printer:
    """Renders run events. Thinking is dimmed; tool results are abbreviated."""

    def __init__(self) -> None:
        self.mode: str | None = None

    def __call__(self, ev: dict[str, Any]) -> None:
        match ev["type"]:
            case "assistant.delta":
                if ev["kind"] != self.mode:
                    if self.mode:
                        print(RESET)
                    self.mode = ev["kind"]
                    print(DIM if self.mode == "thinking" else "", end="")
                print(ev["text"], end="", flush=True)
            case "assistant.message":
                if self.mode:
                    print(RESET)
                self.mode = None
            case "tool.started":
                args = json.dumps(ev["args"], ensure_ascii=False)
                print(f"{CYAN}⏺ {ev['name']}{RESET}({args[:120]}{'…' if len(args) > 120 else ''})")
            case "tool.result":
                d = ev["entry"]["data"]
                lines = text_of(d["content"]).splitlines() or [""]
                color = RED if d["is_error"] else DIM
                more = f" … (+{len(lines) - 3} lines)" if len(lines) > 3 else ""
                print(color + "\n".join(f"  ⎿ {line[:160]}" for line in lines[:3]) + more + RESET)
            case "run.resumed":
                print(f"{DIM}[resuming interrupted run {ev['run_id']}]{RESET}")
            case "run.finished":
                if self.mode:
                    print(RESET)
                    self.mode = None
                u = ev["usage"]
                note = "" if ev["status"] == "ok" else f"{RED}[{ev['status']}]{RESET} "
                print(f"{note}{DIM}[tokens in {u.get('input', 0)} (cached {u.get('cache_read', 0)}), "
                      f"out {u.get('output', 0)}]{RESET}")


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
            log = ws.new_session(ws.root_of(log), ws.index.session(log.id)["model_ref"])
            print(f"new session {log.id}")
        case "/sessions":
            for s in ws.index.sessions(log.header["project_id"]):
                print(f"{'*' if s['id'] == log.id else ' '} {s['id']}  {s['title'] or '(empty)'}")
        case "/open":
            log = ws.open_session(arg)
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
            log = ws.fork(log, log.resolve(arg))
            print(f"forked into {log.id}")
        case "/model":
            if arg:
                ws.set_model(log, arg)
            print(f"model: {ws.index.session(log.id)['model_ref']}  (configured: {', '.join(ws.cfg.models)})")
        case "/resume":
            run(ws.resume(log, Printer()))
        case _:
            print(HELP)
    return log


def repl(ws: Workspace, log: SessionLog) -> None:
    print(f"{DIM}nano-workbuddy · session {log.id} · {ws.root_of(log)} · /help for commands{RESET}")
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
        run(ws.submit(log, line, emit=Printer()))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nwb", description="nano-workbuddy developer CLI")
    p.add_argument("prompt", nargs="?", help="run one prompt and exit")
    p.add_argument("-C", "--project", type=Path, default=Path("."), help="project directory (default: cwd)")
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest session")
    p.add_argument("-s", "--session", help="open a session by id")
    p.add_argument("-m", "--model", help="model name from config.toml")
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
    if args.model:
        ws.set_model(log, args.model)
    if ws.index.running_runs(log.id):  # D6: pick up where a crash left off
        run(ws.resume(log, Printer()))
    if args.prompt:
        sys.exit(0 if run(ws.submit(log, args.prompt, emit=Printer())) in ("ok", "limit") else 1)
    repl(ws, log)


if __name__ == "__main__":
    main()
