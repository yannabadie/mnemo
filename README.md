# Mnemo — versioned memory for a simulated Alexa+ experience

Mnemo is a self-hosted MCP server and a browser simulator for the Alexa+ track of the Amazon Developer Hackathon. It records facts with **declared sources**, keeps previous versions after a correction, and refuses stale writes. The simulator is a deterministic MCP client; it does not connect to Amazon Alexa or use an LLM.

The design comes from a separate multi-agent memory system. This standalone project has its own SQLite store; a demonstration does not establish the correctness, safety, or completeness of that other system.

[Watch the one-minute walkthrough](https://youtu.be/iNJlMGLmAwU): choose the lease, correct twenty-four months to eighteen, and inspect both versions. The video uses actual UI stills with English captions and synthetic narration. The application below lets you exercise the same interaction.

## Try it locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python server.py --demo
```

Open http://127.0.0.1:8799/ and choose the synthetic demonstration. No model key or paid inference is needed. Package installation needs network access the first time.

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

A reference is a declaration from the caller. Requiring it does **not** verify a contract, an email, the caller's identity, or the truth of the fact. The browser's user/date marker is not an authenticated documentary receipt.

## Correcting the intended memory

The browser identifies the memory before writing a correction. An ambiguous request needs an explicit choice rather than silently selecting the first search result. A stale version is an observable conflict; re-reading is not permission to overwrite a different fact or to claim that the user confirmed it.

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
ui/index.html   deterministic simulated Alexa+ client and visible MCP protocol panel
server.py       official Python MCP SDK, Streamable HTTP, storage-mode and origin controls
store.py        SQLite + FTS5, versioned corrections, validation, migration and transactions
verify.py       read-only local consistency verifier
tests/          targeted storage, migration, interface and transport regressions
```

The client negotiates MCP, keeps its session and sends the negotiated protocol version on subsequent requests. This is an MCP demonstration, not a claim of a fully deployed Amazon Alexa integration.

## Tests

```bash
pytest -q
```

The original suite and the added regression cases cover the problems found during independent review: wrong-topic recall and correction, invalid sources and empty content, index failure rollback, legacy migration, inconsistent history, real inventory, safe text display, isolated public sessions and transport rules. Test receipts identify exactly which checks were executed on each delivered version; a passing unit suite alone does not establish a successful public jury journey.

## Scope and next steps

This is a hackathon prototype with a simulated assistant. Public demonstrations use synthetic data. Production accounts, household identity, consent scopes, authenticated document provenance and independent integrity anchoring require additional design and validation. These capabilities are not claimed by the demo.

The submission must match the code and the actual recorded video. Eligibility for any additional mini challenge is determined by its rules and the organizers; an MIT license alone does not establish eligibility.

Alongside Mnemo, [mcp-session-check](https://github.com/yannabadie/mcp-session-check) is a separate MIT utility for MCP HTTP discovery and session-lifecycle checks. It has no Mnemo dependency and invokes no application tool. Its public evidence separates simulated transport tests from a real local discovery/closure run.

## License and author

MIT — see [LICENSE](LICENSE).

Yann Abadie. Built during the Amazon Developer Hackathon, October 2026.
