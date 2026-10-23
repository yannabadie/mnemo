"""Read a versioned workshop brief and derive a bounded, read-only plan.

The caller identifies a stored brief, never supplies replacement planning inputs.
Plans are snapshots of that exact version, not bookings or verified source facts.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from store import MemoryStore, VersionConflict
from plan_receipts import PlanReceiptRegistry


BRIEF_SCHEMA = "workshop-brief-v1"
FIELDS = frozenset({"schema", "title", "participants", "capacity", "event_date",
                    "timezone", "start", "end", "session_minutes", "transition_minutes"})
LIMITS = [
    "Declared sources are retained; this plan does not authenticate them or the facts.",
    "This is a snapshot of the named brief version. Re-read it and regenerate after any change.",
    "Read-only calculation: no attendees are selected, no room is booked, and no calendar or message is sent.",
    "Official export requires export_workshop(plan_id), which checks the current brief on the server.",
    "Plan receipts are transient within this MCP session, bounded, and lost after session/server shutdown or eviction.",
    "Already downloaded copies remain snapshots; they cannot be revoked by a later change.",
]


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Workshop brief contains a duplicate JSON field: " + key)
        result[key] = value
    return result


def _integer(data: dict, name: str, minimum: int, maximum: int) -> int:
    value = data[name]
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Workshop brief {name} must be an integer from {minimum} to {maximum}")
    return value


def _text(data: dict, name: str, maximum: int) -> str:
    value = data[name]
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Workshop brief {name} must be non-empty text of at most {maximum} characters")
    return value.strip()


def _clock_minutes(value: str, name: str) -> int:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError(f"Workshop brief {name} must use a 24-hour HH:MM time")
    return int(value[:2]) * 60 + int(value[3:])


def _local_instant(day: date, clock: str, zone: ZoneInfo) -> datetime:
    """Refuse ambiguous/missing wall times rather than guess a DST fold."""
    naive = datetime.combine(day, time.fromisoformat(clock))
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        try:
            roundtrip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        except (OverflowError, ValueError) as error:
            raise ValueError("Workshop date/time is outside the supported timezone range") from error
        if roundtrip == naive:
            candidates.append(candidate)
    if not candidates or len({item.utcoffset() for item in candidates}) != 1:
        raise ValueError("Workshop window contains an ambiguous or nonexistent local time; choose another time")
    return candidates[0]


def _load_brief(content: str) -> dict:
    try:
        data = json.loads(content, object_pairs_hook=_unique_pairs)
    except json.JSONDecodeError as error:
        raise ValueError("Stored workshop brief must contain valid JSON for workshop-brief-v1") from error
    if not isinstance(data, dict):
        raise ValueError("Stored workshop brief must be a JSON object")
    missing, extra = FIELDS - data.keys(), data.keys() - FIELDS
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing fields: " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown fields: " + ", ".join(sorted(extra)))
        raise ValueError("Workshop brief " + "; ".join(detail))
    if data["schema"] != BRIEF_SCHEMA:
        raise ValueError("Workshop brief schema must be workshop-brief-v1")
    data = dict(data)
    data["title"] = _text(data, "title", 160)
    for name in ("participants", "capacity"):
        _integer(data, name, 1, 500)
    _integer(data, "session_minutes", 1, 480)
    _integer(data, "transition_minutes", 0, 120)
    for name in ("event_date", "timezone", "start", "end"):
        data[name] = _text(data, name, 100)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data["event_date"]):
        raise ValueError("Workshop brief event_date must use YYYY-MM-DD")
    try:
        day = date.fromisoformat(data["event_date"])
    except ValueError as error:
        raise ValueError("Workshop brief event_date must be a real calendar date") from error
    start = _clock_minutes(data["start"], "start")
    end = _clock_minutes(data["end"], "end")
    if end <= start:
        raise ValueError("Workshop end must be after start on the same calendar day")
    try:
        zone = ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Workshop timezone must be a known IANA name, such as Europe/Paris or UTC") from error
    first = _local_instant(day, data["start"], zone)
    last = _local_instant(day, data["end"], zone)
    if first.utcoffset() != last.utcoffset():
        raise ValueError("Workshop window crosses a clock change; choose a window with one UTC offset")
    return data


def _option(data: dict, strategy: str) -> dict:
    participants, capacity = data["participants"], data["capacity"]
    groups = (participants + capacity - 1) // capacity if strategy == "split" else 1
    planned = participants if strategy == "split" else min(participants, capacity)
    required = groups * data["session_minutes"] + (groups - 1) * data["transition_minutes"]
    available = _clock_minutes(data["end"], "end") - _clock_minutes(data["start"], "start")
    return {
        "strategy": strategy,
        "label": "Split into successive groups" if strategy == "split" else "Limit attendance to one group",
        "feasible": required <= available,
        "required_sessions": groups, "required_minutes": required, "available_minutes": available,
        "planned_participants": planned, "waitlisted_participants": participants - planned,
        "reasons": [] if required <= available else [
            f"{required} minutes are required, but the declared window has only {available} minutes."
        ],
    }


def _clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _export(plan: dict) -> str:
    brief, data = plan["brief"], plan["inputs"]
    lines = ["WORKSHOP PLAN", data["title"], "",
             f"Server plan receipt: {plan['plan_id']}",
             f"Current brief checked at: {plan['export_checked_at']}",
             f"Date: {data['event_date']} | Timezone: {data['timezone']}",
             f"Window: {data['start']}–{data['end']} | Declared capacity: {data['capacity']}",
             f"Brief: {brief['memory_id']} | Version: {brief['version']}",
             f"Local entry hash: {brief['entry_hash']}",
             "Declared sources (not independently authenticated):"]
    lines.extend("- " + source for source in brief["source_refs"])
    lines.extend(["", "Decision: " + plan["strategy"],
                  f"Expected participants: {data['participants']}",
                  f"Scheduled participants: {plan['scheduled_participants']}",
                  f"Waiting list: {plan['waitlisted_participants']} (individual attendees are not selected)",
                  "", "SESSIONS"])
    lines.extend(f"Group {row['group']}: {row['start']}–{row['end']} | {row['participants']} participants"
                 for row in plan["sessions"])
    lines.extend([f"Transition between groups: {data['transition_minutes']} minutes", "", "CHECKLIST"])
    lines.extend("[ ] " + item for item in plan["checklist"])
    lines.extend(["", "SCOPE"] + LIMITS)
    return "\n".join(lines) + "\n"


def _calculate_workshop(store: MemoryStore, memory_id: str, expected_version: int,
                        strategy: str | None = None) -> dict:
    """Derive a plan only from the current stored brief at the expected version."""
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("expected_version must be a positive integer")
    if strategy is not None and strategy not in ("split", "limit"):
        raise ValueError("Workshop strategy must be absent, split, or limit")
    history = store.history(memory_id)
    current = [row for row in history["chain"] if row["status"] == "current"]
    if len(current) != 1:
        raise ValueError("The workshop brief has no unique current version; inspect its history")
    row = current[0]
    if row["version"] != expected_version:
        raise VersionConflict(f"stale workshop brief: {memory_id} is at version {row['version']}, "
                              f"you expected {expected_version}; re-read it and prepare a new plan")
    data = _load_brief(row["content"])
    options = [_option(data, choice) for choice in ("split", "limit")]
    plan = {
        "status": "blocked",
        "brief": {"memory_id": memory_id, "version": row["version"], "title": history["title"],
                  "source_refs": list(row["source_refs"]), "entry_hash": row["entry_hash"]},
        "inputs": data, "strategy": strategy, "sessions": [], "scheduled_participants": 0,
        "waitlisted_participants": 0, "options": options, "reasons": [], "checklist": [],
        "plan_id": None, "limits": list(LIMITS),
    }
    if strategy is None and data["participants"] > data["capacity"]:
        if any(option["feasible"] for option in options):
            plan["status"] = "needs_decision"
            plan["reasons"] = [f"{data['participants']} participants exceed the declared capacity of "
                               f"{data['capacity']}. Choose split or limit explicitly; nobody has been removed."]
        else:
            plan["reasons"] = list(dict.fromkeys(reason for option in options for reason in option["reasons"]))
        return plan
    chosen = _option(data, strategy or "single")
    if not chosen["feasible"]:
        plan["reasons"] = chosen["reasons"]
        return plan
    plan["status"], plan["strategy"] = "ready", strategy or "single"
    plan["scheduled_participants"] = chosen["planned_participants"]
    plan["waitlisted_participants"] = chosen["waitlisted_participants"]
    start = _clock_minutes(data["start"], "start")
    remaining = chosen["planned_participants"]
    for index in range(chosen["required_sessions"]):
        group_size = min(remaining, data["capacity"])
        end = start + data["session_minutes"]
        plan["sessions"].append({"group": index + 1, "participants": group_size,
                                 "start": _clock(start), "end": _clock(end)})
        remaining -= group_size
        start = end + data["transition_minutes"]
    plan["checklist"] = [
        f"Confirm the declared room capacity of {data['capacity']} places.",
        f"Assign {plan['scheduled_participants']} participants to the listed group(s).",
        "Confirm the date and local times with participants; this tool sends no messages.",
    ]
    if plan["waitlisted_participants"]:
        plan["checklist"].append(f"Choose and inform the {plan['waitlisted_participants']} people on the waiting list; "
                                  "their identities are not selected by this tool.")
    return plan


def prepare_workshop(store: MemoryStore, memory_id: str, expected_version: int,
                     strategy: str | None = None, *, receipts: PlanReceiptRegistry) -> dict:
    """Prepare a structured preview and a transient ready-plan receipt.

    No export is generated here. The receipt is meaningful only in its owning
    MCP connection; it is not a durable permission to act on this version.
    """
    with store._lock:
        plan = _calculate_workshop(store, memory_id, expected_version, strategy)
        if plan["status"] == "ready":
            plan["plan_id"] = receipts.register(plan)
        return plan


def export_workshop(store: MemoryStore, plan_id: str, *, receipts: PlanReceiptRegistry) -> dict:
    """Check the stored version/hash, then generate the official export.

    The MemoryStore lock spans the check and rendering, so a normal concurrent
    correct() cannot change the brief between them. The returned file remains
    a snapshot of this instant and cannot be revoked after downloading.
    """
    with store._lock:
        receipt = receipts.get(plan_id)
        history = store.history(receipt.memory_id)
        current = [row for row in history["chain"] if row["status"] == "current"]
        if len(current) != 1:
            raise ValueError("The workshop brief has no unique current version; inspect its history")
        row = current[0]
        if row["version"] != receipt.version or row["entry_hash"] != receipt.entry_hash:
            raise VersionConflict(
                f"stale plan: brief {receipt.memory_id} changed after version {receipt.version}; "
                "this plan was not exported or recalculated; re-read the brief and prepare a new plan"
            )
        integrity = store.verify(receipt.memory_id)
        if not integrity["ok"]:
            raise ValueError("Local memory receipt consistency failed; inspect the history before exporting")
        plan = json.loads(receipt.snapshot_json)
        checked_at = datetime.now(timezone.utc).isoformat()
        plan["plan_id"] = receipt.plan_id
        plan["export_checked_at"] = checked_at
        return {
            "status": "exported", "plan_id": receipt.plan_id,
            "brief": plan["brief"], "checked_at": checked_at,
            "filename": f"mnemo-workshop-{plan['inputs']['event_date']}-v{receipt.version}.txt",
            "content_type": "text/plain;charset=utf-8", "export_text": _export(plan),
            "notice": "Current when checked on the server; downloaded copies are non-revocable snapshots.",
        }
