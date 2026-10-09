"""Bundle entry point: run the stdio MCP server from the bundle root."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ninfs_mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
