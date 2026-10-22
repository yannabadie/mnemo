"""Mnemo — long-term memory for Alexa+ as a self-hosted MCP server.

Streamable HTTP transport (MCP spec 2025-11-25 and later, as supported by the
official Python SDK). Four tools:

- remember(title, content, source_refs): record a fact WITH provenance
- recall(query, limit, include_superseded): full-text recall of current knowledge
- correct(memory_id, expected_version, content, source_refs): versioned correction
  with optimistic concurrency (stale writes are refused, never silent)
- history(memory_id): the full version chain of a memory, with receipts

Run:  python server.py            (serves MCP at /mcp and the simulator at /)
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from store import MemoryStore, NotFound, VersionConflict

DEFAULT_DB = os.environ.get("MNEMO_DB", str(Path(__file__).parent / "mnemo.db"))


def build_server(db_path: str) -> MCPServer:
    mcp = MCPServer(
        name="Mnemo",
        version="1.0.0",
        description="Long-term memory for Alexa+ with provenance and versioned corrections",
        instructions=(
            "Mnemo gives an assistant long-term memory with receipts: every "
            "memory stores its source references, corrections create new "
            "versions instead of overwriting, and stale corrections are "
            "refused (optimistic concurrency)."),
    )
    store = MemoryStore(db_path)

    @mcp.tool()
    def remember(title: str, content: str, source_refs: list[str]) -> dict:
        """Record a new memory. source_refs is mandatory: a memory without
        provenance is a rumor. Returns the new memory id and version 1."""
        return store.remember(title, content, source_refs)

    @mcp.tool()
    def recall(query: str, limit: int = 5, include_superseded: bool = False) -> dict:
        """Recall memories matching a full-text query (accent-insensitive).
        Returns current knowledge by default; set include_superseded=true to
        also list superseded versions for audit."""
        return store.recall(query, limit, include_superseded)

    @mcp.tool()
    def correct(memory_id: str, expected_version: int, content: str,
                source_refs: list[str] | None = None) -> dict:
        """Correct a memory. The old version is superseded, never deleted.
        Pass the version you last read (expected_version): if someone else
        corrected the memory since, the write is refused with VersionConflict."""
        return store.correct(memory_id, expected_version, content, source_refs)

    @mcp.tool()
    def history(memory_id: str) -> dict:
        """Full version chain of a memory: every version, its status
        (current/superseded), its content and its source receipts."""
        return store.history(memory_id)

    @mcp.tool()
    def stats() -> dict:
        """Counters: memories, versions, superseded versions."""
        return store.stats()

    mcp._mnemo_store = store  # exposed for tests
    return mcp


def build_app(db_path: str, public: bool = False):
    mcp = build_server(db_path)
    kwargs = {"streamable_http_path": "/mcp"}
    if public:
        # exposed through a reverse tunnel: accept the tunnel's host/origin
        from mcp.server.transport_security import TransportSecuritySettings
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(**kwargs)
    ui_dir = Path(__file__).parent / "ui"
    if ui_dir.is_dir():
        app.routes.append(
            Mount("/", app=StaticFiles(directory=str(ui_dir), html=True), name="ui"))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mnemo MCP server (Streamable HTTP)")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--public", action="store_true",
                        help="serve through a reverse tunnel (disables host checks)")
    args = parser.parse_args()

    import uvicorn
    app = build_app(args.db, public=args.public)
    uvicorn.run(app, host=args.host, port=args.port, lifespan="on")


if __name__ == "__main__":
    main()
