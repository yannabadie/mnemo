# Mnemo — Friction Log (Amazon Developer Hackathon 2026)

## Entry 1 — MCP Python SDK breaking rename
- **Task attempted**: import FastMCP from the official docs example (`pip install mcp`, latest).
- **Steps taken**: fresh venv, `pip install mcp`, copied the server example from the docs.
- **Expected**: example runs.
- **What actually happened**: `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` — SDK 2.x renamed FastMCP to MCPServer, with a migration-guide pointer in the error message.
- **Severity**: medium (cost ~40 min mid-hackathon).
- **Workaround used**: migrated to `from mcp.server.mcpserver import MCPServer` per the 2.x migration guide.
- **Actionable suggestion**: keep a one-cycle deprecation alias (`FastMCP = MCPServer`) so tutorials and LLM-generated code keep working during the transition.

## Entry 2 — Conversational recall vs strict FTS AND-matching
- **Task attempted**: recall from a natural spoken query ("what about my lease?").
- **Expected**: the lease memory matches.
- **What actually happened**: strict AND over all terms (including stopwords) returned zero results.
- **Severity**: medium.
- **Workaround used**: AND-first query with OR fallback in the store layer; stopword-free queries unaffected.
- **Actionable suggestion**: document a stopword/fallback strategy for recall-style MCP tools; voice queries never look like keywords.

## Entry 3 — DNS-rebinding host checks vs reverse tunnels
- **Task attempted**: expose the self-hosted server through a Cloudflare quick tunnel for judges.
- **Expected**: handshake passes through the tunnel.
- **What actually happened**: `Invalid Host header` — the SDK's DNS-rebinding protection rejects non-configured hosts (a good default, but the escape hatch is hard to find).
- **Severity**: low-medium.
- **Workaround used**: `TransportSecuritySettings(enable_dns_rebinding_protection=False)` behind an explicit `--public` flag.
- **Actionable suggestion**: document the tunnel scenario — every self-hosted demo behind a tunnel hits this.

## Entry 4 — Devpost requirements unreachable for non-browser clients
- **Task attempted**: fetch the hackathon page (tracks, requirements, dates) via plain HTTP for planning.
- **Expected**: HTML.
- **What actually happened**: 403 for non-browser clients; the page also gates registration behind a Cloudflare human check.
- **Severity**: low.
- **Workaround used**: full browser session.
- **Actionable suggestion**: server-render a machine-readable mirror of key dates and submission requirements.
