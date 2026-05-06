"""Entry point for the codebase-cowalk MCP server.

Invoked as `codebase-cowalk-mcp` (declared in pyproject.toml's [project.scripts]).
The plugin's .mcp.json runs this via `uvx --from ${CLAUDE_PLUGIN_ROOT} codebase-cowalk-mcp`.
"""

from __future__ import annotations


def main() -> None:
    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
