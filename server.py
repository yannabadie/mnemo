"""Mnemo's MCP server and isolated, synthetic browser demonstration.

Public/--demo HTTP sessions use separate temporary databases and never open --db.
Local default mode uses --db durably and is restricted to loopback. Session IDs
are capabilities, not authenticated household identities. Public mode is synthetic.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit
from uuid import uuid4
import weakref

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import (
    TransportSecurityMiddleware, TransportSecuritySettings,
)
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from store import MemoryStore, NotFound, VersionConflict

DEFAULT_DB = os.environ.get("MNEMO_DB", str(Path(__file__).parent / "mnemo.db"))


class SessionMemoryStores:
    """Bind stores to server-owned session objects, never a user-supplied ID."""

    def __init__(self):
        self._directory = tempfile.TemporaryDirectory(prefix="mnemo-demo-")
        self._entries = weakref.WeakKeyDictionary()
        self._lock = threading.RLock()

    @staticmethod
    def _dispose(store, path):
        store.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    def get(self, session):
        # The installed SDK exposes a per-request ServerSession proxy. Its
        # connection is stable for the complete handshake/session lifetime.
        connection = getattr(session, "_connection", session)
        with self._lock:
            entry = self._entries.get(connection)
            if entry is None:
                path = Path(self._directory.name) / (uuid4().hex + ".sqlite")
                store = MemoryStore(path)
                finalizer = weakref.finalize(connection, self._dispose, store, path)
                exit_stack = getattr(connection, "exit_stack", None)
                if exit_stack is not None:
                    exit_stack.callback(finalizer)
                entry = (store, path, finalizer)
                self._entries[connection] = entry
            return entry[0]

    def close(self):
        with self._lock:
            for store, path, finalizer in list(self._entries.values()):
                if finalizer.alive:
                    finalizer()
            self._entries.clear()
            self._directory.cleanup()


class OriginHostGuard:
    """Apply the SDK's enabled Host/Origin allowlist to UI and MCP routes."""

    def __init__(self, app, settings):
        self.app = app
        self.security = TransportSecurityMiddleware(settings)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            rejection = await self.security.validate_request(Request(scope))
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        await self.app(scope, receive, send)


def transport_security(public=False, public_origins=()):
    hosts = ["127.0.0.1", "localhost", "[::1]",
             "127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1", "http://localhost", "http://[::1]",
               "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public and not public_origins:
        raise ValueError("--public requires --public-origin with the exact HTTPS origin")
    if public_origins and not public:
        raise ValueError("--public-origin requires --public")
    for value in public_origins:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                "*" in value):
            raise ValueError("public origin must be an exact HTTPS origin without a path")
        port = parsed.port
        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = "[" + hostname + "]"
        authority = hostname + (":" + str(port) if port not in (None, 443) else "")
        hosts.append(authority)
        origins.append("https://" + authority)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_server(db_path: str, isolated_sessions: bool = False) -> MCPServer:
    mcp = MCPServer(
        name="Mnemo",
        version="1.1.0",
        description="Versioned memory with declared sources; local durable storage or isolated demo",
        instructions=(
            "Memories have versioned corrections and declared source references. "
            "A source declaration is not independent evidence of its truth. "
            "Identify the intended memory before correcting; never pick a "
            "search result arbitrarily. "
            + ("This demo is session-isolated, temporary, and intended only for synthetic examples."
               if isolated_sessions else
               "This local durable store preserves memories across reconnects. It has no household authentication.")
        ),
    )
    stores = SessionMemoryStores() if isolated_sessions else None
    persistent = None if isolated_sessions else MemoryStore(db_path)

    def current_store(ctx):
        return stores.get(ctx.session) if stores is not None else persistent

    def invoke(ctx, operation, *args):
        try:
            return getattr(current_store(ctx), operation)(*args)
        except (ValueError, NotFound, VersionConflict) as error:
            raise ToolError(type(error).__name__ + ": " + str(error)) from error

    @mcp.tool()
    def remember(ctx: Context, title: str, content: str, source_refs: list[str]) -> dict:
        """Record a memory with non-empty declared sources."""
        return invoke(ctx, "remember", title, content, source_refs)

    @mcp.tool()
    def recall(ctx: Context, query: str, limit: int = 5,
               include_superseded: bool = False) -> dict:
        """Search meaningful terms; no match must not fall back to another topic."""
        return invoke(ctx, "recall", query, limit, include_superseded)

    @mcp.tool()
    def list_memories(ctx: Context, limit: int = 20, offset: int = 0,
                      include_superseded: bool = False) -> dict:
        """List a stable page of memories without a keyword search."""
        return invoke(ctx, "list_memories", limit, offset, include_superseded)

    @mcp.tool()
    def correct(ctx: Context, memory_id: str, expected_version: int,
                content: str, source_refs: list[str]) -> dict:
        """Correct the explicitly chosen ID/version; stale writes are refused."""
        return invoke(ctx, "correct", memory_id, expected_version, content, source_refs)

    @mcp.tool()
    def history(ctx: Context, memory_id: str) -> dict:
        """Read all versions and their declared sources."""
        return invoke(ctx, "history", memory_id)

    @mcp.tool()
    def stats(ctx: Context) -> dict:
        """Counters for this store only."""
        return invoke(ctx, "stats")

    mcp._mnemo_store = persistent
    mcp._mnemo_session_stores = stores
    return mcp


def build_app(db_path: str, public: bool = False, public_origins=(), demo: bool = False):
    # Validate configuration before allocating stores or touching any database.
    security = transport_security(public, public_origins)
    isolated = public or demo
    mcp = build_server(db_path, isolated_sessions=isolated)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=security,
        max_request_body_size=65536,
        max_sessions=64,
        session_idle_timeout=1800,
    )
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with previous_lifespan(application) as state:
                yield state
        finally:
            if isolated:
                mcp._mnemo_session_stores.close()
            else:
                mcp._mnemo_store.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(OriginHostGuard, settings=security)
    app.state.mnemo = mcp

    async def session_info(request):
        return JSONResponse({
            "storage": "temporary_demo" if isolated else "local_durable",
            "synthetic_only": isolated,
            "session_idle_timeout_seconds": 1800,
            "memories_survive_reconnect": not isolated,
        }, headers={"Cache-Control": "no-store"})

    app.routes.append(Route("/session-info", session_info, methods=["GET"]))
    ui_dir = Path(__file__).parent / "ui"
    if ui_dir.is_dir():
        app.routes.append(
            Mount("/", app=StaticFiles(directory=str(ui_dir), html=True), name="ui"))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mnemo local durable MCP store or isolated synthetic demo")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="durable local SQLite path; never opened by --demo or --public")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--demo", action="store_true",
                        help="use isolated temporary sessions locally, with synthetic examples")
    parser.add_argument("--public", action="store_true",
                        help="allow an explicitly configured reverse-tunnel origin")
    parser.add_argument("--public-origin", action="append", default=[],
                        help="exact public HTTPS origin, e.g. https://demo.example.org")
    args = parser.parse_args()
    if not args.public and args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("local durable/demo mode must bind to a loopback address")
    try:
        app = build_app(args.db, public=args.public,
                        public_origins=args.public_origin, demo=args.demo)
    except ValueError as exc:
        parser.error(str(exc))

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, lifespan="on")


if __name__ == "__main__":
    main()
