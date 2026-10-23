# Mnemo — review the decision when the facts change

A smaller room makes yesterday's workshop plan unusable. Mnemo ties a prepared decision to the facts that produced it. Its server refuses an official export when those facts have changed, even if a client skips its own freshness checks. The workshop example makes that boundary visible: read the current brief, decide how to adapt, then receive a new running order with its version and declared sources.

Try the example with **12 participants, a room reduced from 12 places to 6, and a 10:00–12:00 window**. Keep everyone in two successive groups, or explicitly choose one group with a waiting list. Two 45-minute sessions need a 15-minute transition: the split plan is 10:00–10:45 and 11:00–11:45. A shorter window must refuse that plan rather than invent extra time.

The browser offers this task in French or English. It is a simulated Alexa+ client connected to a real, self-hosted MCP server. Planning is a deterministic backend tool, not a canned screen or an LLM response. No Amazon device connection is claimed.

The design comes from a separate multi-agent memory system. This standalone project has its own SQLite store; a demonstration does not establish the correctness, safety, or completeness of that other system.

The [current 87-second walkthrough](https://youtu.be/xwvZ3CgVz7Y) shows the real version 1.2 interface, the room change, the explicit adaptation choice and the current plan. Its stale-export segment reproduces the exact response from a separately received live local HTTP check, labelled as such. English captions and locally generated Kokoro narration accompany actual browser capture.

The [earlier one-minute walkthrough](https://youtu.be/iNJlMGLmAwU) preserves the previous memory interface: choose a lease, correct it and inspect both versions. It predates the workshop planner and current design; it remains historical evidence.

## Try it locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python server.py --demo
```

Open http://127.0.0.1:8799/ and choose **Prepare a workshop**. Change the room, make the adaptation decision and download the resulting text plan. You can also edit the brief, inspect its earlier versions and exercise the general memory tools. No model key or paid inference is needed. Package installation needs network access the first time.

### What a change does

1. The example stores a synthetic brief with declared source references. `prepare_workshop` reads that exact memory ID and expected version.
2. Changing the room creates a new brief version. The old plan is labelled obsolete and cannot be downloaded as the current result. No adaptation is chosen silently.
3. A chosen feasible strategy produces sessions, participant totals and a checklist. `prepare_workshop` returns a structured preview and an opaque, connection-local `plan_id`; it does not render an export.
4. Download calls `export_workshop(plan_id)`. The server checks the unique current brief version, entry hash and local history consistency under the same store lock used by normal corrections, then generates the file. An old receipt fails with `VersionConflict`; no automatic recalculation or fallback file is delivered.

The exported file is a snapshot, current at the server check instant. An already downloaded copy cannot be revoked; a client can also copy a preview independently. Receipts expire with their MCP connection or server, or after bounded LRU eviction (500 receipts and 8 MiB per connection). Durable facts retain their existing local persistence; receipts do not. The tool does not book a room, choose individual attendees, send invitations or write to a calendar. The examples demonstrate a task; user demand and time savings have not been measured.

## What this adds, and what it does not

Versioned memory, source metadata and scheduling are established ideas. The contribution tested here is an application-level action boundary: a prepared plan is not permission to export it indefinitely. A local comparison first reads the correct version and hash, then changes the brief from a second connection before export. A client rendering its cached preview still produces the old plan; the official MCP export refuses that same old receipt. Unchanged and freshly replanned controls export successfully. This compares two policies on this application, not Alexa+, Mem0 or Zep, and does not establish worldwide novelty or a customer benefit in the field.

### Storage modes

- **Local durable mode:** `python server.py --db mnemo.db` binds to loopback by default. The local store persists after reconnecting and restarting. It is a trusted local development service, not a multi-user authentication system. The browser must make its storage mode clear and must not overwrite a durable store with the sample scenario.
- **Isolated demonstration:** `python server.py --demo` uses a separate temporary store for each MCP session. A new demo begins empty; this temporary mode is for synthetic examples.
- **Public demonstration:** `python server.py --public --public-origin https://YOUR-EXACT-DEMO-HOST` requires the exact HTTPS origin. Host and Origin validation remain enabled. Public sessions are isolated temporary demonstrations, not storage for personal data. Start the tunnel separately and keep the configured origin aligned with the actual host.

The quick tunnel used during submission is temporary. The repository, video and local commands are the reproducible fallback. No future uptime is promised.

## Tools

| Tool | Result |
|---|---|
| `remember(title, content, source_refs)` | Record a non-empty fact and non-empty declared references; return its ID and first version. |
| `recall(query, limit, include_superseded)` | Search current facts using meaningful terms; an unrelated topic must not be substituted just because a common word matches. |
| `list_memories(limit, offset, include_superseded)` | Enumerate real pages of memories without using a hard-coded list of search words. |
| `correct(memory_id, expected_version, content, source_refs)` | Correct the explicitly identified memory at the expected version, preserving its history. |
| `history(memory_id)` | Read versions, declared references, status and timestamps. |
| `stats()` | Read counters for the current store. |
| `prepare_workshop(memory_id, expected_version, strategy)` | Read a structured workshop brief from the current session; return a plan, a required decision or a constraint refusal. No write occurs. |
| `export_workshop(plan_id)` | Check this connection's receipt against the current stored brief, then generate the official text file. Refuse stale, unknown or evicted receipts. |

`strategy` is `null`, `split` or `limit`. A stale version is refused. A capacity conflict without a choice returns `needs_decision` with no export receipt. An infeasible choice returns `blocked`; only `ready` includes sessions and a `plan_id`. Results carry the brief ID, version, entry hash and declared references. Official export text is returned only by `export_workshop` after its server check. The server and browser client are version `1.2.0`; the SDK pin is unchanged.

The brief's content is JSON with the following schema; it is stored through `remember` and revised through `correct`:

```json
{
  "schema": "workshop-brief-v1",
  "title": "Planning workshop",
  "participants": 12,
  "capacity": 6,
  "event_date": "2026-11-06",
  "timezone": "Europe/Paris",
  "start": "10:00",
  "end": "12:00",
  "session_minutes": 45,
  "transition_minutes": 15
}
```

The planner validates fields, bounds, the calendar date, IANA time zone and local clock validity. It rejects ambiguous or nonexistent local times and windows crossing a clock change. It reads current memory by ID, without trying to infer structured constraints from an arbitrary sentence.

A reference is a declaration from the caller. Requiring it does **not** verify a contract, an email, the caller's identity, or the truth of the fact. The browser's user/date marker is not an authenticated documentary receipt.

## Correcting the intended memory

The browser identifies the memory before writing a correction. The guided editor retains the selected ID and expected version; a stale version is an observable conflict. Re-reading is not permission to overwrite a different fact or to claim that the user confirmed it. General recall is explicitly a keyword search, not unrestricted language understanding.

The memory panel uses the listing tool. Recalled content is displayed as text, including text that looks like HTML. Microphone or speech features, when available, use the browser and its permissions; they are optional and do not imply a connection to an Amazon device.

## Local receipt consistency

```bash
python verify.py --db mnemo.db
```

Mnemo checks local receipt consistency: contiguous versions, previous-hash links, content and declared sources, immutable memory metadata, version statuses and the stored head commitment. It detects inconsistent edits and detectable truncation against those stored values.

It is **not an external trust anchor**: an operator who rewrites the database and all hashes can create another consistent history. A locally consistent chain does not prove that the recorded statement is true or that its source is authentic. An unknown memory must not be reported as a successfully checked history.

The verifier reads the database without migrating it. Legacy migrations establish a new integrity baseline; they do not authenticate past facts or their sources. Preserve a backup before upgrading a valuable store. Schema compatibility and failure rollback are tested with temporary fixtures, not the public demo's data.

## Architecture

```text
ui/index.html   French/English workshop journey, memory editor and optional MCP details
server.py       official Python MCP SDK, Streamable HTTP, storage-mode and origin controls
planner.py      constraint validation and read-only, version-bound workshop planning
store.py        SQLite + FTS5, versioned corrections, validation, migration and transactions
verify.py       read-only local consistency verifier
tests/          targeted storage, migration, interface and transport regressions
```

The client negotiates MCP, keeps its session and sends the negotiated protocol version on subsequent requests. This is an MCP demonstration, not a claim of a fully deployed Amazon Alexa integration.

## Tests

```bash
pytest -q
node tests/test_ui.mjs
```

The storage and transport regressions preserve wrong-topic refusal, ID/version corrections, rollback, history consistency, inventory, text rendering and session isolation. Workshop tests cover changed constraints, explicit split/limit decisions, infeasible windows, stale versions, malformed briefs and real MCP calls on isolated temporary stores. The JavaScript checks exercise the real planner and memory store, including version checks at download and connection failures. Test receipts identify the exact version and checks; browser acceptance is recorded separately.

## Scope and next steps

This is a hackathon prototype with a simulated assistant. Public demonstrations use synthetic data. Production accounts, household identity, consent scopes, authenticated document provenance and independent integrity anchoring require additional design and validation. These capabilities are not claimed by the demo.

The submission must match the code and the actual recorded video. Eligibility for any additional mini challenge is determined by its rules and the organizers; an MIT license alone does not establish eligibility.

Alongside Mnemo, [mcp-session-check](https://github.com/yannabadie/mcp-session-check) is a separate MIT utility for MCP HTTP discovery and session-lifecycle checks. It has no Mnemo dependency and invokes no application tool. Its public evidence separates simulated transport tests from a real local discovery/closure run.

## License and author

MIT — see [LICENSE](LICENSE).

Yann Abadie. Built during the Amazon Developer Hackathon, October 2026.
