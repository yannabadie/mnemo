"""A bounded local comparison against a competent versioned deterministic tool.

The reference below is independent of Mnemo's planner and export implementation.
It uses the same declared constraints, explicit choices, and an atomic revision
precondition at export. This is not an Alexa/Mem0/LLM benchmark or a user study.
Run: python comparisons/deterministic_workshop.py --output /path/to/results.json
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
BRIEF = {
    "schema": "workshop-brief-v1", "title": "Recurring workshop comparison",
    "participants": 12, "capacity": 12, "event_date": "2026-11-06",
    "timezone": "Europe/Paris", "start": "10:00", "end": "12:00",
    "session_minutes": 45, "transition_minutes": 15,
}
SOURCES = ["synthetic:organizer initial brief"]
NEW_SOURCES = SOURCES + ["synthetic:organizer corrected room capacity"]


class RevisionChanged(Exception):
    pass


def minutes(clock):
    if not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise ValueError("Expected a 24-hour clock")
    hours, mins = map(int, clock.split(":"))
    return hours * 60 + mins


def clock(value):
    return f"{value // 60:02d}:{value % 60:02d}"


class VersionedReference:
    """Small deterministic alternative with a real version/hash export guard.

    It has no MCP transport or UI. Current revision and preview receipts live in
    memory. The comparison concerns the stated same-day workshop fixtures only.
    """

    def __init__(self, brief, sources):
        self._lock = threading.RLock()
        self._plans = {}
        self.brief = deepcopy(brief)
        self.sources = list(sources)
        self.version = 1
        self.memory_id = "reference-workshop"

    def read_current(self):
        with self._lock:
            data = {"brief": self.brief, "sources": self.sources, "version": self.version}
            digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            return {"memory_id": self.memory_id, "version": self.version, "entry_hash": digest,
                    "source_refs": list(self.sources)}

    def correct(self, brief, sources, expected_version):
        with self._lock:
            if expected_version != self.version:
                raise RevisionChanged("Current revision differs from the selected revision")
            self.brief, self.sources = deepcopy(brief), list(sources)
            self.version += 1

    @staticmethod
    def validate(data):
        if set(data) != set(BRIEF) or data["schema"] != "workshop-brief-v1":
            raise ValueError("Malformed structured brief")
        for field, low, high in [
            ("participants", 1, 500), ("capacity", 1, 500),
            ("session_minutes", 1, 480), ("transition_minutes", 0, 120),
        ]:
            if type(data[field]) is not int or not low <= data[field] <= high:
                raise ValueError("Invalid numeric constraint: " + field)
        if not isinstance(data["title"], str) or not data["title"].strip():
            raise ValueError("Missing workshop title")
        day = date.fromisoformat(data["event_date"])
        start, end = minutes(data["start"]), minutes(data["end"])
        if end <= start:
            raise ValueError("The window must end after it starts")
        zone = ZoneInfo(data["timezone"])
        offsets = []
        for value in (data["start"], data["end"]):
            hours, mins = map(int, value.split(":"))
            naive = datetime(day.year, day.month, day.day, hours, mins)
            instants = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
            valid = [item for item in instants
                     if item.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive]
            if not valid or len({item.utcoffset() for item in valid}) != 1:
                raise ValueError("Ambiguous or missing local time")
            offsets.append(valid[0].utcoffset())
        if offsets[0] != offsets[1]:
            raise ValueError("Clock change within the workshop window")
        return start, end

    def prepare(self, strategy=None):
        with self._lock:
            data = deepcopy(self.brief)
            start, end = self.validate(data)
            if strategy not in (None, "split", "limit"):
                raise ValueError("An explicit supported strategy is required")
            options = []
            for choice in ("split", "limit"):
                groups = ((data["participants"] - 1) // data["capacity"] + 1) if choice == "split" else 1
                scheduled = data["participants"] if choice == "split" else min(data["participants"], data["capacity"])
                needed = groups * data["session_minutes"] + max(0, groups - 1) * data["transition_minutes"]
                options.append({
                    "strategy": choice, "feasible": needed <= end - start,
                    "required_sessions": groups, "required_minutes": needed,
                    "available_minutes": end - start, "planned_participants": scheduled,
                    "waitlisted_participants": data["participants"] - scheduled,
                })
            result = {"status": "blocked", "strategy": strategy, "options": options,
                      "sessions": [], "scheduled_participants": 0, "waitlisted_participants": 0,
                      "brief": self.read_current(), "inputs": data, "plan_id": None}
            if strategy is None and data["participants"] > data["capacity"]:
                result["status"] = "needs_decision" if any(row["feasible"] for row in options) else "blocked"
                return result
            choice = next(row for row in options if row["strategy"] == (strategy or "limit"))
            if not choice["feasible"]:
                return result
            result.update(status="ready", strategy=strategy or "single",
                          scheduled_participants=choice["planned_participants"],
                          waitlisted_participants=choice["waitlisted_participants"])
            remaining, cursor = choice["planned_participants"], start
            for group in range(1, choice["required_sessions"] + 1):
                count = min(remaining, data["capacity"])
                result["sessions"].append({"group": group, "participants": count,
                                           "start": clock(cursor), "end": clock(cursor + data["session_minutes"])})
                remaining -= count
                cursor += data["session_minutes"] + data["transition_minutes"]
            result["plan_id"] = "reference_" + secrets.token_hex(16)
            self._plans[result["plan_id"]] = json.dumps(result)
            return result

    def export(self, plan):
        with self._lock:
            saved = self._plans.get(plan["plan_id"])
            if saved is None:
                raise ValueError("No ready receipt")
            saved = json.loads(saved)
            current = self.read_current()
            if any(current[key] != saved["brief"][key] for key in ("memory_id", "version", "entry_hash")):
                raise RevisionChanged("The prepared revision is no longer current")
            text = json.dumps({
                "title": saved["inputs"]["title"], "date": saved["inputs"]["event_date"],
                "timezone": saved["inputs"]["timezone"], "brief": saved["brief"],
                "strategy": saved["strategy"], "sessions": saved["sessions"],
                "scheduled_participants": saved["scheduled_participants"],
                "waitlisted_participants": saved["waitlisted_participants"],
                "notice": "Current when checked; downloaded copies cannot be revoked.",
            }, ensure_ascii=False, indent=2)
            return {"status": "exported", "brief": saved["brief"], "text": text,
                    "sessions": saved["sessions"], "scheduled_participants": saved["scheduled_participants"],
                    "waitlisted_participants": saved["waitlisted_participants"]}

    def close(self):
        pass


class MnemoAdapter:
    def __init__(self, brief, sources):
        sys.path.insert(0, str(ROOT))
        from store import MemoryStore
        from plan_receipts import PlanReceiptRegistry
        self.directory = tempfile.TemporaryDirectory(prefix="mnemo-fair-comparison-")
        self.store = MemoryStore(Path(self.directory.name) / "synthetic.sqlite")
        self.receipts = PlanReceiptRegistry()
        self.memory_id = self.store.remember(brief["title"], json.dumps(brief), sources)["memory_id"]

    def read_current(self):
        row = self.store.history(self.memory_id)["chain"][-1]
        return {"memory_id": self.memory_id, "version": row["version"],
                "entry_hash": row["entry_hash"], "source_refs": row["source_refs"]}

    def correct(self, brief, sources, expected_version):
        self.store.correct(self.memory_id, expected_version, json.dumps(brief), sources)

    def prepare(self, strategy=None):
        from planner import prepare_workshop
        current = self.read_current()
        return prepare_workshop(self.store, self.memory_id, current["version"], strategy, receipts=self.receipts)

    def export(self, plan):
        from planner import export_workshop
        result = export_workshop(self.store, plan["plan_id"], receipts=self.receipts)
        text = result["export_text"]
        sessions = [
            {"group": int(group), "start": start, "end": end, "participants": int(people)}
            for group, start, end, people in re.findall(
                r"^Group (\d+): (\d{2}:\d{2})–(\d{2}:\d{2}) \| (\d+) participants$", text, re.M)
        ]
        return {"status": result["status"], "brief": result["brief"], "text": text,
                "sessions": sessions,
                "scheduled_participants": int(re.search(r"^Scheduled participants: (\d+)$", text, re.M)[1]),
                "waitlisted_participants": int(re.search(r"^Waiting list: (\d+)", text, re.M)[1])}

    def close(self):
        self.store.close()
        self.directory.cleanup()


def observable(plan):
    fields = ("strategy", "feasible", "required_sessions", "required_minutes",
              "available_minutes", "planned_participants", "waitlisted_participants")
    return {"status": plan["status"], "strategy": plan["strategy"],
            "options": [{key: row[key] for key in fields} for row in plan["options"]],
            "sessions": plan["sessions"], "scheduled_participants": plan["scheduled_participants"],
            "waitlisted_participants": plan["waitlisted_participants"]}


def export_observable(result):
    return {key: result[key] for key in ("status", "sessions", "scheduled_participants", "waitlisted_participants")} | {
        "version": result["brief"]["version"], "source_refs": result["brief"]["source_refs"],
    }


def rejected_export(tool, plan):
    try:
        tool.export(plan)
    except Exception as error:
        if type(error).__name__ in ("RevisionChanged", "VersionConflict"):
            return {"status": "rejected_stale", "error_type": type(error).__name__, "message": str(error)}
        raise
    raise AssertionError("An old plan was exported after a known version change")


def check_expected(name, record):
    """Check the declared fixture outcome, not just agreement between programs."""
    assert record["initial_control"]["status"] == "exported"
    if name == "unchanged":
        assert record["export"]["version"] == 1
        assert record["export"]["scheduled_participants"] == 12
        return
    assert record["old_receipt"]["status"] == "rejected_stale"
    if name == "change_after_successful_fresh_read":
        assert record["fresh_read_matched"] is True
        assert record["before_choice"]["status"] == "needs_decision"
        return
    if name == "impossible_window":
        assert record["current"]["status"] == "blocked"
        assert not record["current"]["sessions"]
        assert not any(option["feasible"] for option in record["current"]["options"])
        assert record["export"]["status"] == "not_available_no_ready_plan"
        return
    assert record["before_choice"]["status"] == "needs_decision"
    assert record["export"]["version"] == 2
    assert record["export"]["source_refs"] == NEW_SOURCES
    if name == "capacity_reduced_split":
        assert record["current"]["strategy"] == "split"
        assert record["export"]["scheduled_participants"] == 12
        assert record["export"]["waitlisted_participants"] == 0
        assert record["current"]["options"][0]["required_minutes"] == 105
        assert record["export"]["sessions"] == [
            {"group": 1, "participants": 6, "start": "10:00", "end": "10:45"},
            {"group": 2, "participants": 6, "start": "11:00", "end": "11:45"},
        ]
    else:
        assert record["current"]["strategy"] == "limit"
        assert record["export"]["scheduled_participants"] == 6
        assert record["export"]["waitlisted_participants"] == 6
        assert record["current"]["options"][1]["required_minutes"] == 45


def run_case(factory, name, changes=None, strategy=None, race=False):
    tool = factory(BRIEF, SOURCES)
    try:
        first = tool.prepare()
        initial = tool.export(first)
        assert initial["status"] == "exported"
        record = {"initial_control": export_observable(initial)}
        if changes is None:
            record["current"] = observable(first)
            record["export"] = export_observable(initial)
            assert record["export"]["sessions"] == [{"group": 1, "participants": 12, "start": "10:00", "end": "10:45"}]
            return record
        if race:
            current = tool.read_current()
            record["fresh_read_matched"] = all(current[key] == first["brief"][key]
                                               for key in ("memory_id", "version", "entry_hash"))
            assert record["fresh_read_matched"]
        changed = {**BRIEF, **changes}
        tool.correct(changed, NEW_SOURCES, 1)
        record["old_receipt"] = rejected_export(tool, first)
        current = tool.prepare()
        record["before_choice"] = observable(current)
        if race:
            return record
        if current["status"] == "needs_decision":
            assert strategy in ("split", "limit")
            current = tool.prepare(strategy)
        record["current"] = observable(current)
        if current["status"] == "ready":
            record["export"] = export_observable(tool.export(current))
            assert record["export"]["sessions"] == current["sessions"]
        else:
            record["export"] = {"status": "not_available_no_ready_plan"}
            assert current["status"] == "blocked" and current["plan_id"] is None
        return record
    finally:
        tool.close()


def compare():
    cases = [
        ("unchanged", None, None, False, []),
        ("capacity_reduced_split", {"capacity": 6}, "split", False,
         ["Record the capacity change", "Explicitly choose split"]),
        ("capacity_reduced_limit", {"capacity": 6}, "limit", False,
         ["Record the capacity change", "Explicitly choose limit", "Choose the six waiting individuals outside the tool"]),
        ("impossible_window", {"capacity": 6, "end": "10:30"}, None, False,
         ["Change the constraints; neither option fits"]),
        ("change_after_successful_fresh_read", {"capacity": 6}, None, True,
         ["Re-read the changed brief and make a new explicit choice"]),
    ]
    rows = []
    for name, changes, strategy, race, interventions in cases:
        reference = run_case(VersionedReference, name, changes, strategy, race)
        mnemo = run_case(MnemoAdapter, name, changes, strategy, race)
        check_expected(name, reference)
        check_expected(name, mnemo)
        # Error classes/messages identify their implementations; the decision is compared.
        def normalized(record):
            value = deepcopy(record)
            if "old_receipt" in value:
                value["old_receipt"] = {"status": value["old_receipt"]["status"]}
            return value
        equal = normalized(reference) == normalized(mnemo)
        assert equal, (name, reference, mnemo)
        rows.append({"case": name, "changes": changes, "explicit_test_choice": strategy,
                     "reference": reference, "mnemo": mnemo, "functional_results_equal": equal,
                     "required_intervention_for_both": interventions,
                     "intervention_measurement": "Contract-level actions listed; choices were supplied by this script, not observed users."})
    return {
        "schema": "mnemo-competent-deterministic-comparison/v1",
        "scope": "Five local synthetic conditions; real Mnemo MemoryStore/planner/export functions versus an independently implemented deterministic versioned reference. No browser, native Alexa, third-party service or LLM execution.",
        "fixture": BRIEF, "initial_sources": SOURCES, "changed_sources": NEW_SOURCES,
        "preconditions": "Same structured facts, limits, choices, date/window and sources. Both tools reject an export whose current version/hash differs. A successful fresh read before an intervening correction does not bypass either export guard.",
        "cases": rows,
        "summary": {"conditions": len(rows), "functional_ties": len(rows),
                    "unexpected_successes_on_blocked_or_stale": 0, "unjustified_rejections_on_valid_controls": 0,
                    "measured_functional_superiority": False,
                    "finding": "The competent deterministic reference matches Mnemo on these constraints and version checks. The observed difference is Mnemo's MCP/UI packaging and explanation; no user benefit or time saving was measured."},
        "not_measured": ["Human completion time, usability or error rate", "LLM/Alexa/Mem0/Zep performance", "Real organizer demand", "MCP transport or availability", "Generalization beyond these fixtures"],
        "runtime": "Python standard library; synthetic temporary SQLite for Mnemo; no model/provider/network calls.",
        "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("planner.py", "plan_receipts.py", "comparisons/deterministic_workshop.py")},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        print(json.dumps({"output": str(args.output), "summary": result["summary"]}, ensure_ascii=False))
    else:
        sys.stdout.write(encoded)
