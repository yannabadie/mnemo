# A competent deterministic alternative

This is a small, reproducible local comparison. It deliberately gives the alternative a version/hash precondition at export, rather than a client that keeps using old data.

```bash
python3 -B comparisons/deterministic_workshop.py --output /tmp/mnemo-comparison.json
```

The script uses Python's standard library and the repository's real `MemoryStore`, planner and export gate. Mnemo runs against temporary synthetic SQLite files. The independently implemented reference calculates the same constraints and checks its current revision under a lock before exporting. It does not import Mnemo's planning or export implementation.

The five declared conditions use the same 12-person brief, 10:00–12:00 window, 45-minute sessions and 15-minute transitions:

| Condition | Expected result for both tools |
|---|---|
| No change | One group; a valid version 1 export. |
| Capacity reduced to 6; explicit split | A decision is required first; all 12 people in two groups, occupying 105 minutes. |
| Capacity reduced to 6; explicit limit | A decision is required first; 6 scheduled, 6 waiting, occupying 45 minutes. Individual attendees still need to be chosen. |
| Capacity 6 and only 10:00–10:30 available | Neither option fits; no ready plan or new export. |
| Correct version/hash read, then capacity changes before export | Both reject the old receipt; a new explicit decision is required. |

Each implementation is checked against the declared outcomes, then their observable results are compared. The trace preserves their actual error types/messages. It compares schedules, participant/waitlist counts, choice requirements, version binding and export availability. It does not compare output bytes: the reference exports a structured JSON document, while Mnemo exports its text run sheet.

## Received result

The recorded run in [results-20261023.json](results-20261023.json) has five functional ties. No unexpected success on an impossible/stale path or unjustified rejection of a valid control occurred within these five conditions.

This establishes no functional superiority over that competent deterministic alternative. Mnemo's additional packaging is its MCP interface, version/source inspection and explanation of the choices. Whether that helps a recurring organizer complete the task faster or with fewer mistakes still requires observation; it was not measured here.

The choices in this run were supplied by the script. Listed interventions are required domain decisions, not measured user clicks or time. No LLM, browser, Alexa device, Mem0/Zep service, real organizer, model/provider call or network request is involved. This is not a market benchmark, usability study, or proof of global novelty.
