# Mnemo — Friction Log (Amazon Developer Hackathon 2026)

Updated 23 October 2026 to separate the prototype's workarounds from the implementation. These are project observations, not a survey of every SDK tutorial or available Alexa+ client.

## Entry 1 — Matching examples to the installed SDK
- **Task attempted**: build the server from an example targeting a different SDK API.
- **Steps taken**: the prototype tried the FastMCP import; the installed environment uses the MCPServer API.
- **Expected**: an obvious path from the example to a running server.
- **What actually happened**: the API mismatch required adapting the imports and lifecycle handling.
- **Severity**: medium; no independently measured time saving is claimed.
- **Resolution in this version**: use `from mcp.server.mcpserver import MCPServer` and pin the tested SDK to `mcp==2.3.0`.
- **Actionable suggestion**: show the applicable SDK version next to runnable examples and link the matching migration instructions.

## Entry 2 — Conversational recall vs strict FTS AND-matching
- **Task attempted**: recall from a natural spoken query ("what about my lease?").
- **Expected**: the lease memory matches.
- **What actually happened**: strict AND over all terms (including stopwords) returned zero results.
- **Severity**: medium.
- **Prototype workaround**: an OR fallback increased matches, but the later review reproduced an unrelated match and wrong-subject correction.
- **Resolution in this version**: remove common conversational stopwords, require all remaining meaningful terms, and require an explicit memory/version selection before a conversational correction. An absent passport must not recall or modify the lease.
- **Actionable suggestion**: memory-tool examples should include unrelated-subject negative cases, not only a successful recall.

## Entry 3 — DNS-rebinding host checks vs reverse tunnels
- **Task attempted**: expose the self-hosted server through a Cloudflare quick tunnel for judges.
- **Expected**: handshake passes through the tunnel.
- **What actually happened**: `Invalid Host header` — the SDK's DNS-rebinding protection rejects a host that has not been configured.
- **Severity**: low-medium.
- **Prototype workaround, no longer used**: disabling DNS-rebinding protection.
- **Resolution in this version**: public mode requires the exact HTTPS origin via `--public-origin`; host and origin checks remain enabled. Public sessions use isolated temporary stores.
- **Actionable suggestion**: provide a reverse-proxy/tunnel example that configures the allowed host and origin while keeping protection enabled.

## Entry 4 — No reference Alexa+ client to test an MCP server against
- **Task attempted**: point a real Alexa+ client at our self-hosted MCP server to validate the integration end-to-end.
- **Steps taken**: searched the track resources for a client/simulator; found SDK docs and sample code but no downloadable Alexa+ test client.
- **Expected**: a harness or emulator to observe the handshake from the assistant's side.
- **What actually happened**: the resource review did not identify a usable reference client for this project. That does not establish that no client exists. I built a deterministic simulated Alexa+ experience connected to the real MCP Streamable HTTP server; its protocol panel shows the actual exchanges.
- **Severity**: medium.
- **Resolution in this version**: the simulator (`ui/`), real SDK/transport checks and real-browser checks. This establishes the submitted simulation, not a genuine Amazon Alexa device integration.
- **Actionable suggestion**: provide a discoverable reference client or documented conformance harness so developers can test the lifecycle against the intended platform.


## Entry 5 — A memory demo did not explain a useful task

- **Observed on 23 October**: the initial screen foregrounded raw MCP messages and an empty chat. French commands were treated as lexical searches; a question could miss a stored fact. Connection failures could leave controls or status misleading. A successful scripted lease correction did not establish usable free-form interaction.
- **Product change**: a structured workshop brief now drives a real read-only `prepare_workshop` tool. A room-capacity change invalidates the previous plan, requires a split/limit choice and produces a version-bound running order and text export. An impossible window is refused.
- **Interface change**: French/English guided actions, current and obsolete plans, visible decisions and direct history by ID replace the implicit command grammar. MCP details remain inspectable in a secondary panel. Loading, timeout, expiry and write-versus-refresh outcomes are separate.
- **Validation**: isolated storage/planner and real MCP transport tests accompany browser checks. The room-change example is synthetic; it does not establish customer demand, unrestricted language understanding or an actual Alexa device integration.

## Entry 6 — A client freshness read still leaves a read-to-export race

- **Task attempted**: export a prepared plan after the client has read and matched its current memory version and hash.
- **Observed on 23 October**: another connection can correct the durable brief after that successful read. A local client rendering the cached preview then still uses the old capacity. This is a controlled comparison on this application's actual MCP server, not an Alexa device or a competitor benchmark.
- **Resolution in version 1.2.0**: preparation returns a preview and opaque receipt only. `export_workshop` checks the receipt against the current version and hash, verifies local history consistency and generates the file under the correction lock. Stale receipts are refused without silently replanning. Valid unchanged and newly prepared plans still export.
- **Remaining scope**: the connection-local registry is bounded and temporary. A client can copy a preview and downloaded files cannot be revoked. This is a control on this server's official export, not a policy imposed on unrelated tools.
- **Actionable suggestion**: assistant-tool examples should distinguish preparation from execution, bind execution to the facts used, and test a correction between a successful read and the final action.
