"""Real HTTP/SSE + built React UI, deterministic local model and disposable data. No provider keys."""
from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles

from nano_workbuddy import skills
from nano_workbuddy.api import COOKIE, create_app
from nano_workbuddy.config import load
from nano_workbuddy.llm import Delta, Response
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.types import text, text_of

TOKEN = "e2e-local-only"


def call(name, **args):
    return {"type": "tool_call", "id": f"test-{name}", "name": name, "args": args}


class LocalModel:
    def __init__(self, spec):
        self.spec = spec

    async def stream(self, system, messages, tools, tool_choice=None):
        latest = next(m for m in reversed(messages) if m["role"] == "user"
                      and not text_of(m["content"]).startswith("<system-notice>"))
        body = text_of(latest["content"])
        if messages[-1]["role"] == "tool" and "E2E交付后失败" in body:
            raise TimeoutError("synthetic post-delivery timeout")
        if messages[-1]["role"] == "tool":
            content = [text("本地操作完成")]
        elif any(t["name"] == "task_result" for t in tools):
            content = [call("create_document", path="task-report.md", content="# 合成任务报告\nRESULT_MARKER"),
                       call("task_result", outcome="changed", summary="已生成合成任务报告",
                            evidence=["task-report.md"])]
        elif "E2E审批" in body:
            content = [call("write", path="approved.txt", content="APPROVED_ONLY")]
        elif "E2E下载" in body:
            content = [call("create_document", path="report.md", content="# E2E report\n\nDOWNLOAD_MARKER")]
        elif "E2E交付后失败" in body:
            content = [call("create_document", path="delivered.md", content="# Kept report\nKEPT_BEFORE_TIMEOUT")]
        elif "E2E断线" in body:
            yield Delta("text", "重连测试已开始")
            await asyncio.sleep(2)
            content = [text("重连后持久结果 RECOVERY_MARKER")]
        elif "ATTACHMENT_MARKER" in body:
            content = [text("模型读到附件 ATTACHMENT_MARKER")]
        else:
            content = [text("本地模拟回复")]
        yield Response(content, "tool_use" if any(b["type"] == "tool_call" for b in content) else "end",
                       {"input": 20, "output": 10}, {"usage_complete": True})


def main():
    with tempfile.TemporaryDirectory(prefix="nwb-browser-e2e-") as raw:
        root = Path(raw)
        project = root / "project"
        project.mkdir()
        cfg = dataclasses.replace(load(root / "home"), scratch_dir=root / "scratch")
        skills.SHARED = root / "no-shared-skills"
        ws = Workspace(cfg, connect=LocalModel)
        pid = ws.index.project_for(project, trusted=True)["id"]

        async def notify(*args):
            pass

        app = create_app(ws, TOKEN, notify=notify)
        app.state.scheduler.tick_seconds = 0.1  # deterministic collection, without waiting the product's 30s tick

        @app.get("/__test__/ready")
        async def ready():
            return {"ok": True}

        @app.post("/__test__/disconnect/{sid}")
        async def disconnect(sid: str, request: Request):
            if request.cookies.get(COOKIE) != TOKEN:
                raise HTTPException(401)
            for queue in list(app.state.runs.hub(ws.open_session(sid)).subs):
                queue.put_nowait(None)
            return {"ok": True}

        @app.get("/__test__/project")
        async def project_info(request: Request):
            if request.cookies.get(COOKIE) != TOKEN:
                raise HTTPException(401)
            return {"project_id": pid}

        app.mount("/", StaticFiles(directory=Path(__file__).parents[2] / "src/nano_workbuddy/static", html=True))
        uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")


if __name__ == "__main__":
    main()
