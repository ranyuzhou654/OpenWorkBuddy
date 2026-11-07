"""One U2-1 real search trial, isolated from personal data; no automatic retries or reruns."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from nano_workbuddy import artifacts, config, llm, policy, prompt, search, usage
from nano_workbuddy.budgets import BudgetExceeded, Limits
from nano_workbuddy.eval.runner import archive_source, code_version, write_json
from nano_workbuddy.eval.schema import file_hash
from nano_workbuddy.eval.worker import EvalWorkspace
from nano_workbuddy.tools import BUILTIN, Registry

TASK = """为 OpenWorkBuddy 的开发者生成第一期中文 AI 技术研究简报。
用户已确认 Tavily 搜索连接可用，飞书本人连接器已启用。本任务仅研究，不执行办公写入。
研究问题：如何每周生成一份可追溯的 AI 技术周报，并仅在有实质变化时更新？本期聚焦 Agent 可靠性与上下文治理。
资料截止日 2026-10-26。先读取 project-context.md 中的实际项目记录，再使用 Tavily 搜索，
选择 3–5 个官方来源，使用 web_fetch 读取正文后再引用。可以从 Anthropic、LangChain/LangGraph、
OpenTelemetry、MCP 等项目的官方资料找相关做法；仅有搜索摘要不算正文阅读。
至少比较两种可落实的处理方式及其取舍，给出一周内适合本项目实现的 3 项改进及具体验收条件。
区分原文事实、你的推断和建议；本项目已有功能不要当作尚待从零实现。不得用测试通过数代表业务成功率。
发布日期不明则标 unknown；访问日期单列。不把旧资料写成最近一周新闻，不引入截止日之后的资料。
本期是首次建立来源和决策基线，尚无跨期变化结论。设计后续判断实质变化的方法，但不声称已经跑过第二期。
输出 research-brief.md 和 research-brief.docx，使用同一正文、约 1200–1800 中文字，
包含开头结论、来源/日期、方案比较、3 项改进、下期更新规则，关键事实附 [S#]。
工具会自动附带保留快照的文献表，不要手工重复生成文献表。标题和各级标题为纯文字，不加装饰。
预算为整次任务 12 次模型请求、180 秒、最多 3 次 Tavily 搜索。批量执行独立读取，保留时间生成两种产物。
没有飞书、浏览器、shell 或额外问答；不足之处如实写明。"""


class CountedSearch:
    name = "tavily"

    def __init__(self, backend, ledger):
        self.backend, self.ledger, self.calls = backend, ledger, 0

    async def search(self, query, max_results, recency, topic):
        if self.calls >= 3:
            raise search.SearchError("U2 search request limit reached (3)")
        self.calls += 1
        usage.append(self.ledger, {"type": "search.started", "query": query, "request": self.calls})
        try:
            hits = await self.backend.search(query, max_results, recency, topic)
        except Exception as exc:
            usage.append(self.ledger, {"type": "search.failed", "error_type": type(exc).__name__})
            raise
        usage.append(self.ledger, {"type": "search.response", "hits": [dataclasses.asdict(h) for h in hits]})
        return hits


class ResearchWorkspace(EvalWorkspace):
    def search_provider(self):
        return self.backend

    async def toolset(self, spec, root, scheduled=False):
        return Registry([t for t in BUILTIN if t.name in (
            "read", "web_search", "web_fetch", "create_document", "present", "todo")]), {
                "research_trial": "仅使用本任务 project-context.md 和公开网页。工具不能读取其他项目或飞书数据。"}


async def work(trial):
    public = json.loads((trial / "public.json").read_text())
    provider = config.Provider(**public["provider"], api_key="env:NWB_RESEARCH_MODEL_KEY")
    spec = config.ModelSpec(provider=provider, **public["model"])
    model = llm.connect(spec, os.environ.pop("NWB_RESEARCH_MODEL_KEY"))
    model.client = model.client.with_options(max_retries=0)
    calls = 0

    class Bounded:
        def __init__(self, measured):
            self.measured, self.spec = measured, measured.spec

        async def stream(self, *args, **kwargs):
            nonlocal calls
            if calls >= 12:
                raise BudgetExceeded("U2 total model-call limit (12)")
            calls += 1
            async for item in self.measured.stream(*args, **kwargs):
                yield item

    cfg = config.Config(trial / "home", spec.name, {spec.name: spec}, max_turns=12,
                        scratch_dir=trial / "scratch", budget=Limits(model_calls=12, tool_calls=24, wall_seconds=180),
                        rules=tuple(policy.Rule(*r) for r in (
                            ("*", "*", "deny"), ("read", "{root}/*", "allow"),
                            ("read", "{spill}/*", "allow"), ("write", "{root}/*", "allow"),
                            ("web_search", "*", "allow"), ("web_fetch", "https://*", "allow"),
                            ("todo", "*", "allow"))))
    ws = ResearchWorkspace(cfg, connect=lambda _: model)
    ws.backend = CountedSearch(search.Tavily(os.environ.pop("NWB_RESEARCH_SEARCH_KEY")), trial / "search.jsonl")
    ws.arm, ws.trial, ws.bound = "B1", trial, Bounded
    log = ws.new_session(trial / "workspace", initial_prompt=prompt.build(trial / "workspace", cfg.home))
    result = {"session_id": log.id, "status": "error"}
    try:
        result["status"] = await ws.submit(log, public["task"])
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        result.update(model_calls=calls, search_requests=ws.backend.calls)
        write_json(trial / "worker-result.json", result)
        for session in ws._logs.values():
            session.close()
        if ws._knowledge:
            ws._knowledge.db.close()
        ws.index.db.close()
        await model.client.close()


async def run(output):
    cfg = config.load()
    spec = cfg.model()
    if (spec.provider.name, spec.id) != ("opendesign", "deepseek-v4.1-flash"):
        raise ValueError("unexpected provider/model; do not silently change the approved connection")
    model_key, search_key = config.secret(spec.provider.api_key, cfg.home), config.secret(cfg.search.api_key, cfg.home)
    if not model_key or not search_key:
        raise ValueError("model/search key is missing")
    output.mkdir(mode=0o700)
    trial = output / "U2-1"
    root = trial / "workspace"
    root.mkdir(parents=True, mode=0o700)
    repo = Path(__file__).resolve().parents[1]
    inputs = ["docs/design/2026-10-25-resume-implementation-status.md",
              "docs/research/2026-10-26-rd-project-trials-r2/README.md",
              "src/nano_workbuddy/prompts/system.md"]
    context = "\n\n".join(f"## 项目原文 {name}\nSHA256 {file_hash(repo / name)}\n\n"
                             + (repo / name).read_text() for name in inputs)
    (root / "project-context.md").write_text(context)
    provider = {k: v for k, v in dataclasses.asdict(spec.provider).items() if k != "api_key"}
    model = {k: v for k, v in dataclasses.asdict(spec).items() if k != "provider"}
    write_json(trial / "public.json", {"id": "U2-1", "task": TASK, "provider": provider, "model": model})
    version = code_version()
    write_json(output / "freeze.json", {**version, "runner_sha256": file_hash(Path(__file__)),
        "inputs": {name: file_hash(repo / name) for name in inputs},
        "context_sha256": file_hash(root / "project-context.md"), "public_sha256": file_hash(trial / "public.json"),
        "authorization": "User: 继续，我已经验证过搜索了; one U2-1 trial; 12 model requests / 180s / 3 searches"})
    archive_source(version, output / "source.zip")
    shutil.copyfile(__file__, output / "runner.py")
    env = {k: v for k, v in os.environ.items() if k in (
        "PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")}
    env.update(PYTHONPATH=str(repo / "src"), NWB_HOME=str(trial / "home"),
               NWB_RESEARCH_MODEL_KEY=model_key, NWB_RESEARCH_SEARCH_KEY=search_key)
    started = time.monotonic()
    with (trial / "stdout.log").open("wb") as out, (trial / "stderr.log").open("wb") as err:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), "_worker",
                    str(trial), cwd=root, env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), 180)
            process_status = "finished" if proc.returncode == 0 else "worker_error"
        except (TimeoutError, asyncio.CancelledError) as exc:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            process_status = "interrupted" if isinstance(exc, asyncio.CancelledError) else "timeout"
    path = trial / "worker-result.json"
    result = json.loads(path.read_text()) if path.exists() else {"status": process_status}
    events = [e for f in (trial / "home/usage").glob("*.jsonl") for e in usage.events(f)]
    write_json(trial / "calls.json", events)
    begin = [e for e in events if e["type"] == "model.started"]
    end = [e for e in events if e["type"] == "model.response"]
    checks = [{"path": f.name, "sha256": file_hash(f), "checks": artifacts.check(f)[0]}
              for f in sorted(root.glob("research-brief.*"))]
    record = {**result, "id": "U2-1", "process_status": process_status,
              "model_calls": len(begin), "model_responses": len(end),
              "observed_models": sorted({e["model_version"] for e in end}),
              "usage": {k: sum(e["usage"].get(k, 0) for e in end) for k in ("input", "output", "cache_read")},
              "usage_complete": len(begin) == len(end) and all(e.get("usage_complete") for e in end),
              "elapsed_seconds": time.monotonic() - started, "artifact_checks": checks,
              "cost": None, "human_acceptance": "pending", "factual_acceptance": "pending"}
    write_json(trial / "review-card.json", record)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("run", "_worker"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run((work if args.mode == "_worker" else run)(args.output.resolve()))
