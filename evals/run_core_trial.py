"""One bounded core integration trial, with no business plugins or user data.

Two turns share a session, with Workspace reconstruction and changed memory, project
rules and Skill between turns. MCP uses the real SDK and the local test server.
Use --fake for an offline runner check; otherwise the selected model is called.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import sys
import time
from pathlib import Path

from nano_workbuddy import config, llm, policy, skills, usage
from nano_workbuddy.budgets import BudgetExceeded, Limits
from nano_workbuddy.eval.runner import code_version
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import ASSISTANT, TOOL_RESULT, new_id, text_of

REPO = Path(__file__).resolve().parents[1]
TURNS = (
    "使用 memory 工具将用户偏好 preferred_locale 改为 zh-CN（原值 en-US），然后按 core-note Skill "
    "处理 input.json，将结果保存到 result.json。完成后简短回复。",
    "继续按当前 core-note Skill 重新处理已更新的 input.json，更新 result.json。"
    "采用当前记忆和项目规则，完成后简短回复。",
)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def setup_phase(root, home, phase):
    write_json(root / "input.json", {"a": 17, "b": 25} if phase == 1 else {"a": 31, "b": 16})
    (home / "USER.md").write_text(f"preferred_locale={'en-US' if phase == 1 else 'ja-JP'}\n")
    (root / "AGENTS.md").write_text(f"project_rule=RULE_V{phase}\n")
    folder = home / "skills" / "core-note"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\nname: core-note\ndescription: 处理 input.json，生成个人助手核心试用结果。\n---\n"
        "先读取 input.json，使用 mcp__demo__add 计算 a+b。将纯 JSON 写入 result.json，且只包含四个字段："
        f"total（MCP 返回的整数）；tag（固定为 SKILL_V{phase}）；locale（当前用户记忆 preferred_locale）；"
        "project_rule（当前项目规则 project_rule）。已有结果须先读取再修改。不要把历史旧值当作当前值。\n")


async def close(ws):
    await ws.mcp.close()
    for log in ws._logs.values():
        log.close()
    if ws._knowledge:
        ws._knowledge.db.close()
    ws.index.db.close()


def fake_model():
    # The deterministic model exercises the same Workspace, dispatcher and real MCP server.
    sys.path.insert(0, str(REPO / "tests"))
    from conftest import FakeModel, call, reply

    return FakeModel(
        reply(None, call("memory", action="replace", file="user", old_text="en-US", text="zh-CN")),
        reply(None, {"type": "tool_call", "id": new_id("call"), "name": "skill",
                     "args": {"name": "core-note"}}),
        reply(None, call("read", path="input.json"), call("mcp__demo__add", a=17, b=25)),
        reply(None, call("write", path="result.json", content=json.dumps(
            {"total": 42, "tag": "SKILL_V1", "locale": "zh-CN", "project_rule": "RULE_V1"}))),
        reply("done"),
        reply(None, call("read", path="input.json"), call("read", path="result.json"),
              call("mcp__demo__add", a=31, b=16)),
        reply(None, call("write", path="result.json", content=json.dumps(
            {"total": 47, "tag": "SKILL_V2", "locale": "ja-JP", "project_rule": "RULE_V2"}))),
        reply("done"),
    )


async def work(output, source_home, model_name, fake=False):
    output.mkdir(parents=True, exist_ok=False)
    root, home = output / "workspace", output / "home"
    root.mkdir()
    home.mkdir()
    # Do not discover the user's shared skills, hooks, MCP, plugins or stored history.
    skills.SHARED = output / "empty-shared-skills"
    source = config.load(source_home)
    original = source.model(model_name)
    spec = dataclasses.replace(original, provider=dataclasses.replace(original.provider, api_key="env:UNSET_CORE_KEY"))
    model = fake_model() if fake else llm.connect(spec, config.secret(original.provider.api_key, source.home))
    if not fake:
        model.client = model.client.with_options(max_retries=0, timeout=60)
    cfg = config.Config(home, spec.name, {spec.name: spec}, max_turns=12, plugins_enabled=(),
        scratch_dir=output / "scratch", budget=Limits(model_calls=12, tool_calls=24, wall_seconds=180),
        rules=tuple(policy.Rule(a, r, "allow") for a, r in (
            ("read", "{root}"), ("read", "{root}/*"), ("write", "{root}/*"), ("edit", "{root}/*"),
            ("skill", "core-note"), ("memory", str(home / "USER.md")), ("mcp__demo__add", ""))),
        mcp={"demo": config.McpServer("demo", sys.executable,
             (str(REPO / "tests/fixtures/mcp_server.py"),), sandbox=False, exposure="direct",
             cwd=str(root), network=False)})
    # Deny every other capability, including bash, outside reads and MCP file writes.
    cfg = dataclasses.replace(cfg, rules=(policy.Rule("*", "*", "deny"), *cfg.rules))
    calls, requests = 0, []
    started = time.monotonic()

    class Bounded:
        client = getattr(model, "client", None)

        def __init__(self, requested):
            self.spec = requested

        async def stream(self, system, messages, tools, tool_choice=None):
            body = "\n".join(text_of(m["content"]) for m in messages)
            requests.append({"call": calls, "system": system, "messages": body,
                             "tools": [t["name"] for t in tools]})
            async for item in model.stream(system, messages, tools, tool_choice):
                yield item

    class CoreWorkspace(Workspace):
        def make_run(self, *args, **kwargs):
            run = super().make_run(*args, **kwargs)
            before = run.model.before_call

            def reserve():
                nonlocal calls
                if calls >= 12:
                    raise BudgetExceeded("total core trial model_calls")
                if before:
                    before()
                calls += 1

            run.model.before_call = reserve
            return run

    def open_ws():
        return CoreWorkspace(cfg, connect=Bounded if not fake else lambda requested: connect_fake(requested))

    def connect_fake(requested):
        model.connect(requested)
        return Bounded(requested)

    result = {"mode": "deterministic" if fake else "real-model", "provider": spec.provider.name,
              "requested_model": spec.id, "budget": {"model_calls_total": 12, "wall_seconds_total": 180,
              "sdk_retries": 0}, "source": code_version(), "business_plugins": [], "phases": [],
              "checks": [], "cost": None, "restart_kind": "Workspace reconstruction, not process crash"}
    result["mcp_fixture_sha256"] = hashlib.sha256((REPO / "tests/fixtures/mcp_server.py").read_bytes()).hexdigest()
    ws, log, first_raw = None, None, b""

    def check(name, ok):
        result["checks"].append({"name": name, "ok": bool(ok)})

    try:
        async with asyncio.timeout(180):
            for phase, turn in enumerate(TURNS, 1):
                setup_phase(root, home, phase)
                ws = open_ws()
                log = ws.new_session(root) if phase == 1 else ws.open_session(result["session_id"])
                result["session_id"] = log.id
                if phase == 2:
                    check("raw_history_preserved_after_restart", log.file.read_bytes() == first_raw)
                start_call = len(requests)
                before_ids = set(log.entries)
                status = await ws.submit(log, turn)
                result["phases"].append({"phase": phase, "status": status, "model_calls": len(requests) - start_call})
                generated = json.loads((root / "result.json").read_text()) if (root / "result.json").exists() else None
                write_json(output / f"phase-{phase}-result.json", generated)
                expected = {"total": 42 if phase == 1 else 47, "tag": f"SKILL_V{phase}",
                            "locale": "zh-CN" if phase == 1 else "ja-JP", "project_rule": f"RULE_V{phase}"}
                check(f"phase_{phase}_runtime_ok", status == "ok")
                check(f"phase_{phase}_actual_file_values", generated == expected)
                new = [e for e in log.path() if e.id not in before_ids]
                result["phases"][-1]["tool_errors"] = sum(e.type == TOOL_RESULT and e.data["is_error"] for e in new)
                mcp_ids = {c["id"] for e in new if e.type == ASSISTANT for c in e.tool_calls
                           if c["name"] == "mcp__demo__add"}
                check(f"phase_{phase}_real_mcp_result", any(e.type == TOOL_RESULT and
                      e.data["call_id"] in mcp_ids and not e.data["is_error"] and
                      text_of(e.data["content"]).strip() == str(expected["total"]) for e in new))
                if phase == 1:
                    check("memory_tool_persisted", (home / "USER.md").read_text().strip() == "preferred_locale=zh-CN")
                    check("memory_update_visible_in_same_turn", "preferred_locale=zh-CN" in requests[-1]["system"]
                          and "preferred_locale=en-US" not in requests[-1]["system"])
                    check("skill_loaded_on_demand", any(e.type == TOOL_RESULT and
                          e.data.get("meta", {}).get("pin") == "skill:core-note" for e in new))
                    first_raw = log.file.read_bytes()
                else:
                    projected = requests[start_call]
                    check("current_memory_and_rules_projected", "preferred_locale=ja-JP" in projected["system"]
                          and "project_rule=RULE_V2" in projected["system"]
                          and "preferred_locale=en-US" not in projected["system"]
                          and "project_rule=RULE_V1" not in projected["system"])
                    check("current_skill_projected", "固定为 SKILL_V2" in projected["messages"] and
                          "固定为 SKILL_V1" not in projected["messages"])
                    check("raw_history_append_only", log.file.read_bytes().startswith(first_raw))
                await close(ws)
                ws = None
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        check("trial_completed", False)
    finally:
        if ws:
            await close(ws)
        if not fake:
            await model.client.close()
        result.update(model_calls=calls, elapsed_seconds=time.monotonic() - started)
        events = usage.events(home / "usage" / f"{result.get('session_id', 'none')}.jsonl")
        responses = [e for e in events if e["type"] == "model.response"]
        result.update(model_responses=len(responses), usage=usage.totals(
            home / "usage" / f"{result.get('session_id', 'none')}.jsonl"),
            observed_models=sorted({e["model_version"] for e in responses}),
            usage_complete=bool(responses) and all(e["usage_complete"] for e in responses)
            and len(responses) == calls)
        check("no_business_tools_declared", all(not set(r["tools"]) & {
              "feishu", "web_search", "web_fetch", "create_document", "export_document"} for r in requests))
        result["passed"] = len(result["phases"]) == 2 and all(c["ok"] for c in result["checks"])
        result["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        write_json(output / "requests.json", requests)
        write_json(output / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "source"}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--fake", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(work(args.output.resolve(), args.home, args.model, args.fake)))
