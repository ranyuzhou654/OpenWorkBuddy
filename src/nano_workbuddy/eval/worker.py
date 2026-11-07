"""One clean process per trial; only local, scoped file tools and the configured model.

The parent keeps answers/graders outside this workspace. This capability restriction is
for the eval Agent, not an OS sandbox for executing arbitrary untrusted Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from .. import llm, policy, usage
from ..config import Config, ModelSpec, Provider
from ..runtime import Workspace
from ..tools import BUILTIN, Registry, ToolError
from .schema import Limits

TOOLS = ("read", "write", "edit", "create_document", "present", "todo", "ask_user")


class BudgetExceeded(Exception):
    pass


class EvalWorkspace(Workspace):
    async def toolset(self, spec, root, scheduled=False):
        return self.tools, {"evaluation": "Only the declared local file tools are available. Shell, directory "
                           "enumeration, network tools and connectors are unavailable. Use the supplied input "
                           "file inventory; do not guess tool names or file names."}

    def make_run(self, *args, **kwargs):
        run = super().make_run(*args, **kwargs)
        run.model = self.bound(run.model)
        run.context_governance = self.arm == "B1"
        original = run.gate

        async def gate(tool, call):
            blocked = await original(tool, call)
            if blocked is not None:
                usage.append(self.trial / "events.jsonl", {"type": "scope.blocked", "tool": tool.name,
                                                          "call_id": call["id"], "args": call["args"]})
            return blocked

        run.gate = gate
        return run


async def work(trial: Path) -> None:
    public = json.loads((trial / "public.json").read_text())
    limits = Limits.model_validate(public["limits"])
    provider = Provider(**public["provider"], api_key="env:NWB_EVAL_API_KEY")
    spec = ModelSpec(provider=provider, **public["model"])
    root, home = trial / "workspace", trial / "home"
    home.mkdir()
    cfg = Config(home, spec.name, {spec.name: spec}, max_turns=limits.max_turns,
                 rules=(policy.Rule("*", "*", "deny"), policy.Rule("read", "{root}", "allow"),
                        policy.Rule("read", "{root}/*", "allow"),
                        policy.Rule("read", "{spill}/*", "allow"), policy.Rule("write", "{root}/*", "allow"),
                        policy.Rule("edit", "{root}/*", "allow"), policy.Rule("todo", "*", "allow"),
                        policy.Rule("ask_user", "*", "allow")))
    model = llm.connect(spec, os.environ.pop("NWB_EVAL_API_KEY"))
    # The counter wraps *every* call, including compaction, wrap-up and overflow retry.
    model.client = model.client.with_options(max_retries=0)
    calls = 0

    class Bounded:
        def __init__(self, measured):
            self.measured, self.spec = measured, measured.spec

        async def stream(self, *args, **kwargs):
            nonlocal calls
            if calls >= limits.model_calls:
                raise BudgetExceeded("eval model-call budget exhausted")
            calls += 1
            async for item in self.measured.stream(*args, **kwargs):
                yield item

    ws = EvalWorkspace(cfg, connect=lambda _: model,
                       tools=Registry([t for t in BUILTIN if t.name in TOOLS]))
    ws.arm, ws.trial, ws.bound = public["arm"], trial, Bounded
    # The same frozen system prompt in both arms; physical roots are isolated, but not arm-labelled.
    log = ws.new_session(root, initial_prompt=public["initial_prompt"])

    async def question(args):
        usage.append(trial / "events.jsonl", {"type": "clarification", "question": args})
        if not public["allow_clarification"]:
            raise ToolError("This task must be completed using the supplied materials; record any missing facts.")
        return public["clarification_reply"] or "No additional information is available."

    statuses = []
    result = {"session_id": log.id, "status": "error", "failure_kind": "tool_runtime",
              "system_prompt_sha256": hashlib.sha256(log.path()[0].data["text"].encode()).hexdigest()}
    try:
        for n, turn in enumerate(public["turns"]):
            if n == 0:
                turn = "本任务允许的输入文件（相对工作目录）：\n" + "\n".join(public["inputs"]) + "\n\n" + turn
            status = await ws.submit(log, turn, question=question)
            statuses.append(status)
            if status != "ok":
                break
        result.update(status="ok" if all(s == "ok" for s in statuses) else "error",
                      failure_kind=None if all(s == "ok" for s in statuses) else "model_judgment")
    except Exception as exc:
        result.update(error_type=type(exc).__name__)
        if isinstance(exc, BudgetExceeded):
            result["failure_kind"] = "budget"
    finally:
        result.update(run_statuses=statuses, calls=calls)
        (trial / "worker-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        log.close()
        ws.index.db.close()
        await model.client.close()


if __name__ == "__main__":
    asyncio.run(work(Path(sys.argv[1]).resolve()))
