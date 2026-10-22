"""MCP transport integration checks; all databases are temporary."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import sys

from starlette.testclient import TestClient

CANDIDATE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("MNEMO_TEST_REPO", str(CANDIDATE)))
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("mnemo_transport_candidate", CANDIDATE / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
IDS = itertools.count(1)


def rpc(client, method, params=None, session=None, version="2025-11-25", notification=False, **extra):
    headers = {"Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = version
    headers.update(extra.pop("headers", {}))
    body = {"jsonrpc": "2.0", "method": method}
    if not notification:
        body.update(id=next(IDS), params=params or {})
    return client.post("/mcp", json=body, headers=headers, **extra)


def initialize(client):
    response = rpc(client, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "isolated-transport-test", "version": "1"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["result"]["protocolVersion"] == "2025-11-25"
    session = response.headers["Mcp-Session-Id"]
    notified = rpc(client, "notifications/initialized", session=session, notification=True)
    assert notified.status_code == 202, notified.text
    return session


def tool_response(client, session, name, arguments):
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def tool(client, session, name, arguments):
    result = tool_response(client, session, name, arguments)
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_public_sessions_are_isolated_and_deleted_sessions_refused(tmp_path):
    untouched = tmp_path / "existing.sqlite"
    app = server.build_app(str(untouched), public=True, public_origins=["https://jury.example.test"])
    with TestClient(app, base_url="https://jury.example.test") as client:
        first, second = initialize(client), initialize(client)
        assert first != second
        names = [item["name"] for item in rpc(client, "tools/list", session=first).json()["result"]["tools"]]
        assert "list_memories" in names
        lease = tool(client, first, "remember", {
            "title": "Apartment lease", "content": "my lease is 24 months",
            "source_refs": ["synthetic:test"],
        })
        assert tool(client, second, "recall", {"query": "lease"})["results"] == []
        assert tool_response(client, second, "history", {"memory_id": lease["memory_id"]})["isError"]
        assert tool_response(client, second, "correct", {
            "memory_id": lease["memory_id"], "expected_version": 1,
            "content": "unrelated replacement", "source_refs": ["synthetic:test"],
        })["isError"]
        assert tool(client, first, "recall", {"query": "What about my passport?"})["count"] == 0
        assert tool(client, first, "history", {"memory_id": lease["memory_id"]})["chain"][0]["content"] == "my lease is 24 months"
        third = initialize(client)  # browser refresh/new connection is a new public demo
        assert tool(client, third, "stats", {})["memories"] == 0
        response = client.delete("/mcp", headers={
            "Mcp-Session-Id": first, "MCP-Protocol-Version": "2025-11-25",
        })
        assert response.status_code == 200
        assert rpc(client, "tools/list", session=first).status_code == 404
        assert tool(client, second, "stats", {})["memories"] == 0
    assert not untouched.exists()


def test_host_origin_and_protocol_validation_are_enabled(tmp_path):
    app = server.build_app(str(tmp_path / "never.sqlite"), public=True,
                           public_origins=["https://jury.example.test"])
    with TestClient(app, base_url="https://jury.example.test") as client:
        assert client.get("/", headers={"Host": "evil.example.test"}).status_code == 421
        assert client.get("/session-info", headers={"Origin": "https://evil.example.test"}).status_code == 403
        assert rpc(client, "initialize", headers={"Origin": "https://evil.example.test"}).status_code == 403
        session = initialize(client)
        assert rpc(client, "tools/list", session=session, version="invalid").status_code == 400
        assert rpc(client, "tools/list").status_code == 400
        assert rpc(client, "tools/list", session="invented-session").status_code == 404
        assert client.get("/session-info").json()["storage"] == "temporary_demo"
        assert client.get("/").status_code == 200


def test_public_mode_refuses_missing_or_wildcard_origin(tmp_path):
    import pytest
    for origins in [[], ["https://*.trycloudflare.com"], ["http://demo.test"],
                    ["https://user@demo.test"], ["https://demo.test/path"]]:
        with pytest.raises(ValueError):
            server.build_app(str(tmp_path / "untouched.sqlite"), public=True, public_origins=origins)
    assert not (tmp_path / "untouched.sqlite").exists()


def test_demo_database_is_closed_when_session_is_deleted(tmp_path):
    import time
    app = server.build_app(str(tmp_path / "untouched.sqlite"), demo=True)
    directory = Path(app.state.mnemo._mnemo_session_stores._directory.name)
    with TestClient(app, base_url="http://127.0.0.1:8799") as client:
        session = initialize(client)
        tool(client, session, "stats", {})
        assert list(directory.glob("*.sqlite"))
        assert client.delete("/mcp", headers={
            "Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-11-25",
        }).status_code == 200
        for _ in range(20):
            if not list(directory.glob("*.sqlite")):
                break
            time.sleep(0.01)
        assert not list(directory.glob("*.sqlite"))
    assert not directory.exists()


def test_local_durable_mode_preserves_memories_on_reconnect_and_restart(tmp_path):
    path = tmp_path / "owner.sqlite"
    app = server.build_app(str(path))
    with TestClient(app, base_url="http://127.0.0.1:8799") as client:
        assert client.get("/session-info").json()["memories_survive_reconnect"] is True
        first = initialize(client)
        record = tool(client, first, "remember", {
            "title": "Synthetic durable fact", "content": "the gate code is 1234",
            "source_refs": ["synthetic:test"],
        })
        client.delete("/mcp", headers={
            "Mcp-Session-Id": first, "MCP-Protocol-Version": "2025-11-25",
        })
        second = initialize(client)
        assert tool(client, second, "recall", {"query": "gate code"})["results"][0]["memory_id"] == record["memory_id"]
    with TestClient(server.build_app(str(path)), base_url="http://localhost:8799") as client:
        assert tool(client, initialize(client), "stats", {})["memories"] == 1
        assert client.get("/", headers={"Host": "untrusted.example.test"}).status_code == 421


def test_local_demo_does_not_open_durable_path_and_inventory_pages_are_real(tmp_path):
    path = tmp_path / "must-not-open.sqlite"
    app = server.build_app(str(path), demo=True)
    with TestClient(app, base_url="http://127.0.0.1:8799") as client:
        session = initialize(client)
        for index in range(23):
            tool(client, session, "remember", {
                "title": "Synthetic entry " + str(index), "content": "虹 " + str(index),
                "source_refs": ["synthetic:test"],
            })
        page1 = tool(client, session, "list_memories", {"limit": 20, "offset": 0})
        page2 = tool(client, session, "list_memories", {"limit": 20, "offset": page1["next_offset"]})
        assert len(page1["results"]) == 20 and len(page2["results"]) == 3
        assert page1["total_memories"] == page2["total_memories"] == 23
        assert {r["memory_id"] for r in page1["results"]}.isdisjoint(r["memory_id"] for r in page2["results"])
        assert page2["has_more"] is False and page2["next_offset"] is None
        assert tool_response(client, session, "remember", {
            "title": "Invalid", "content": "synthetic", "source_refs": ["  "],
        })["isError"]
        assert tool(client, initialize(client), "stats", {})["memories"] == 0
    assert not path.exists()
