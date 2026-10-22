# Mnemo — Long-term memory for Alexa+ (MCP)

**Self-hosted MCP server (Streamable HTTP, spec 2025-11-25) that gives Alexa+ a
memory you can trust: every fact keeps its provenance, every correction keeps
its history, and stale corrections are refused instead of silently overwriting.**

> *Memory with receipts.*

Built for the **Alexa+ track** of the Amazon Developer Hackathon
(Build, Ship, Shape — Oct 2026). Also entering the **Open Source mini challenge**
(this repo is a new MIT project created during the hackathon window).

---

## Why

Alexa+ and brand experiences built on it forget everything that matters:
which version of a fact is current, where a fact came from, and who changed it.
When memory IS retained, it is usually an opaque blob that gets silently
overwritten — the user can't audit what the assistant "knows" about them.

Mnemo fixes the three things a memory must guarantee before anyone trusts it:

1. **Provenance** — a memory without a source is a rumor. `remember` refuses to
   record anything without `source_refs`, and every recalled fact answers
   *"how do you know that?"*.
2. **History, not overwriting** — corrections create a new version and mark the
   old one `superseded`. The full chain stays queryable forever (`history`).
3. **Optimistic concurrency** — `correct` requires the version you last read.
   If another skill corrected the memory since, the write is refused with a
   `VersionConflict` instead of clobbering. The client re-reads and retries.
   Nothing is lost, nobody lies to the user.

## The four tools

| Tool | What it does |
|---|---|
| `remember(title, content, source_refs)` | Record a fact with receipts. Returns `memory_id`, version 1. |
| `recall(query, limit, include_superseded)` | Accent-insensitive full-text recall of **current** knowledge; optional audit mode includes superseded versions. |
| `correct(memory_id, expected_version, content, source_refs)` | Versioned correction. Old version becomes `superseded`. Stale writes refused. |
| `history(memory_id)` | The complete version chain with sources and timestamps. |
| `stats()` | Counters (memories, versions, superseded). |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py            # MCP at http://127.0.0.1:8799/mcp + simulator at /
```

Open http://127.0.0.1:8799/ — the **simulated Alexa+ experience** is a real MCP
client: it performs the `initialize` handshake over Streamable HTTP, lists the
tools, and shows every JSON-RPC frame live in the protocol panel while you chat.
Press **▶ Demo** for the scripted scenario (record → recall → correct → history),
or type your own:

```
Remember that my apartment lease with Marignier Properties is 24 months
What about my lease?
Actually, the lease is 18 months — I misread the contract
History of the lease
```

The deterministic mini-agent needs no LLM and no API keys, so the demo runs
anywhere. Any real MCP client works too:

```bash
# initialize — the server negotiates protocol 2025-11-25
curl -s -D headers.txt http://127.0.0.1:8799/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-11-25","capabilities":{},
        "clientInfo":{"name":"curl","version":"0"}}}'
SID=$(grep -i mcp-session-id headers.txt | awk '{print $2}' | tr -d '\r')
# ...then tools/list and tools/call with header "Mcp-Session-Id: $SID"
```

(Also verified with the MCP SDK's own client transport; see `tests/`.)

## Architecture

```
ui/index.html   simulated Alexa+ chat — a real MCP Streamable HTTP client (vanilla JS)
server.py       MCPServer (official Python SDK, 2.x) · tools · static UI mount
store.py        SQLite (WAL) + FTS5 · versioned facts · provenance · OCC
tests/          unit tests: provenance required, version chains, stale-write refusal,
                accent-insensitive recall, FTS injection neutralized
```

- **Zero dependencies beyond the MCP SDK** (starlette/uvicorn come with it).
  Storage is plain SQLite — trivially self-hostable, no services to run.
- The full-text index folds accents, so French and English recall match either
  way; strict-AND matching falls back to OR for conversational queries
  ("what about my lease").
- The store layer has no network surface at all; the MCP layer adds no storage
  logic. FTS MATCH expressions are built from a `[a-z0-9]` whitelist only and
  are passed as bound parameters.

## Use cases on Alexa+

- **Personal knowledge**: "what did I decide about the lease?", "who is my
  dentist?" — answers carry their source and date.
- **Shared household memory**: two skills correcting the same fact can't
  clobber each other; the second writer is told to re-read.
- **Brand experiences**: a brand skill can publish curated facts (opening
  hours, policies) with provenance, and correct them publicly, keeping the
  audit trail customers can inspect.
- **Compliance posture**: memories are inspectable, correctable and never
  silently rewritten — the substrate a privacy story can be built on.

## License

MIT — see [LICENSE](LICENSE).

## Author

Yann Abadie — built during the Amazon Developer Hackathon (Oct 2026).
The design is distilled from a production multi-agent memory system
(195+ versioned records in daily use), rewritten standalone for this hackathon.
