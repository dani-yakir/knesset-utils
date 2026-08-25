from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from knesset_utils.server.tools import register_tools

DEFAULT_DB_PATH = Path("data/knesset_mirror.sqlite")


def build_server(db_path: Path = DEFAULT_DB_PATH) -> MCPServer:
    mcp = MCPServer("knesset-utils")
    register_tools(mcp, db_path)
    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
