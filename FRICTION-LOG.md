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

## Entry 4 — No reference Alexa+ client to test an MCP server against
- **Task attempted**: point a real Alexa+ client at our self-hosted MCP server to validate the integration end-to-end.
- **Steps taken**: searched the track resources for a client/simulator; found SDK docs and sample code but no downloadable Alexa+ test client.
- **Expected**: a harness or emulator to observe the handshake from the assistant's side.
- **What actually happened**: no such client is publicly available during the hackathon, so we wrote our own simulated Alexa+ experience that is itself a real MCP Streamable HTTP client — which turned out to be the demo's best feature (the protocol panel shows live JSON-RPC).
- **Severity**: medium.
- **Workaround used**: the simulator (`ui/`), plus raw `curl`/SDK handshake transcripts.
- **Actionable suggestion**: ship even a minimal "Alexa+ MCP client sandbox" for developers — testing server behavior against a genuine client is currently the biggest gap in the Alexa+ developer experience.
