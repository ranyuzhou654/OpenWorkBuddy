"""A small stdio MCP server for tests: echo, add, a file writer, and an image."""

import base64
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

server = MCPServer("demo", instructions="Demo tools for tests.")


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def touch(path: str) -> str:
    """Create an empty file at path."""
    open(path, "w").close()
    return f"created {path}"


@server.tool()
def whoami() -> str:
    """Report the environment the server runs in."""
    return f"cwd={os.getcwd()} secret={os.environ.get('NWB_TEST_SECRET', '-')}"


@server.tool()
def pixel() -> Image:
    """Return a 1x1 PNG."""
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9aw"
                           "AAAABJRU5ErkJggg==")
    return Image(data=png, format="png")


if __name__ == "__main__":
    for extra in sys.argv[1:]:  # extra tools for exposure tests: big_0 ... big_N
        n = int(extra)
        for i in range(n):
            server.tool(name=f"big_{i}", description=f"Bulk tool number {i}. " + "padding " * 200)(lambda x: x)
    server.run("stdio")
