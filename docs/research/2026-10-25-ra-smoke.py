"""Optional local integration check: CLI offline dry-run and isolated about:blank screenshot.

Run: .venv/bin/python docs/research/2026-10-25-ra-smoke.py --browser
Never sends a Feishu message or navigates to an external site. npx may fetch the pinned MCP package.
"""

import argparse
import asyncio
import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from nano_workbuddy.connectors import browser
from nano_workbuddy.connectors.feishu import Feishu
from nano_workbuddy.tools.mcp import Server
from nano_workbuddy.tools.registry import AnyArgs, ToolCtx


def contains(value, expected):
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(contains(v, expected) for v in value.values())
    if isinstance(value, list):
        return any(contains(v, expected) for v in value)
    if isinstance(value, str):
        try:
            return contains(json.loads(value), expected)
        except ValueError:
            pass
    return False


async def main(args):
    evidence = {"at": datetime.now(UTC).isoformat(), "model_requests": 0, "real_feishu_writes": 0,
                "external_sites": 0}
    with tempfile.TemporaryDirectory(prefix="nwb-ra-smoke-") as folder:
        home = Path(folder).resolve()
        cli = Feishu(args.cli)
        body = "@literal\n--dry-run=false\nCONTROLLED_STDIN_ONLY"
        code, out = await cli.run(["im", "+messages-send", "--chat-id=oc_synthetic", "--text=-",
                                   "--as=user", "--dry-run"], home, timeout=30, input=body.encode())
        data = json.loads(out[out.index("{"):])
        exact = contains(data.get("data", {}).get("api", []), body)
        evidence["cli"] = {"version": await cli.version(), "exit": code, "ok": data.get("ok"),
                           "dry_run": data.get("dry_run"), "stdin_exact": exact}
        assert code == 0 and data.get("ok") is True and data.get("dry_run") is True and exact
        if args.browser:
            project = home / "project"
            project.mkdir()
            server = Server(browser.spec(home, headless=True), home)
            try:
                await server.start()
                assert not server.error, server.error
                tools = {t.name: t for t in browser.wrap(server.as_tools(), browser.Lease(), set())}
                ctx = ToolCtx(project, "isolated-smoke", home / "files/isolated-smoke")
                nav = await tools["mcp__browser__browser_navigate"].fn(AnyArgs(url="about:blank"), ctx)
                shot = await tools["mcp__browser__browser_take_screenshot"].fn(AnyArgs(filename="blank.png"), ctx)
                assert not nav.is_error and not shot.is_error
                png = (ctx.spill_dir / "browser-output/blank.png").read_bytes()
                assert png.startswith(b"\x89PNG\r\n\x1a\n")
                evidence["browser"] = {"package": browser.PACKAGE, "advertised_tools": len(server.tools),
                                       "exposed_tools": len(tools), "navigation": "about:blank",
                                       "screenshot_bytes": len(png),
                                       "screenshot_sha256": hashlib.sha256(png).hexdigest(),
                                       "isolated_profile": True, "output_in_session": True}
            finally:
                await server.stop()
    output = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--cli", default=shutil.which("lark-cli") or "lark-cli")
    parser.add_argument("--output")
    asyncio.run(main(parser.parse_args()))
