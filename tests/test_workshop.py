"""Workshop planning against real, isolated stores and the real MCP app."""
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planner import prepare_workshop, export_workshop
from plan_receipts import PlanReceiptRegistry
from store import MemoryStore, NotFound, VersionConflict


BRIEF = {
    "schema": "workshop-brief-v1", "title": "Synthetic workshop",
    "participants": 12, "capacity": 12,
    "event_date": "2026-10-31", "timezone": "Europe/Paris",
    "start": "10:00", "end": "12:00",
    "session_minutes": 45, "transition_minutes": 15,
}


class WorkshopStoreCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mnemo-workshop-test-")
        self.store = MemoryStore(Path(self.directory.name) / "isolated.sqlite")
        self.receipts = PlanReceiptRegistry()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def prepare(self, memory_id, expected_version, strategy=None):
        return prepare_workshop(self.store, memory_id, expected_version, strategy, receipts=self.receipts)

    def export(self, plan):
        return export_workshop(self.store, plan["plan_id"], receipts=self.receipts)

    def remember(self, **changes):
        data = {**BRIEF, **changes}
        row = self.store.remember("Stored workshop brief", json.dumps(data), ["synthetic:brief-v1"])
        return row["memory_id"]

    def correct(self, memory_id, version, **changes):
        return self.store.correct(memory_id, version, json.dumps({**BRIEF, **changes}),
                                  [f"synthetic:brief-v{version + 1}"])


class WorkshopPlanningTests(WorkshopStoreCase):
    def test_initial_plan_uses_stored_inputs_and_does_not_write(self):
        memory_id = self.remember()
        before = self.store.stats()
        plan = self.prepare(memory_id, 1)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["strategy"], "single")
        self.assertEqual(plan["inputs"], BRIEF)
        self.assertEqual(plan["sessions"], [{"group": 1, "participants": 12,
                                             "start": "10:00", "end": "10:45"}])
        self.assertEqual(plan["scheduled_participants"], 12)
        self.assertEqual(plan["waitlisted_participants"], 0)
        self.assertEqual(self.store.stats(), before)
        self.assertEqual(plan["brief"]["memory_id"], memory_id)
        self.assertEqual(plan["brief"]["version"], 1)
        self.assertEqual(plan["brief"]["source_refs"], ["synthetic:brief-v1"])
        self.assertEqual(plan["brief"]["entry_hash"], self.store.history(memory_id)["chain"][0]["entry_hash"])
        self.assertIn(memory_id, self.export(plan)["export_text"])
        self.assertIn("Version: 1", self.export(plan)["export_text"])
        self.assertIn("synthetic:brief-v1", self.export(plan)["export_text"])
        self.assertIn("not independently authenticated", self.export(plan)["export_text"])

    def test_capacity_change_requires_a_new_explicit_decision(self):
        memory_id = self.remember()
        old_plan = self.prepare(memory_id, 1)
        self.correct(memory_id, 1, capacity=6)
        with self.assertRaisesRegex(VersionConflict, "stale workshop brief"):
            self.prepare(memory_id, old_plan["brief"]["version"])
        plan = self.prepare(memory_id, 2)
        self.assertEqual(plan["status"], "needs_decision")
        self.assertEqual(plan["sessions"], [])
        self.assertIsNone(plan["plan_id"])
        self.assertNotIn("export_text", plan)
        self.assertEqual(plan["brief"]["source_refs"], ["synthetic:brief-v2"])
        self.assertEqual({option["strategy"] for option in plan["options"]}, {"split", "limit"})
        self.assertTrue(all(option["feasible"] for option in plan["options"]))
        self.assertEqual(len(self.store.history(memory_id)["chain"]), 2)

    def test_split_keeps_all_participants_and_respects_transition_and_window(self):
        memory_id = self.remember(capacity=6)
        plan = self.prepare(memory_id, 1, "split")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["sessions"], [
            {"group": 1, "participants": 6, "start": "10:00", "end": "10:45"},
            {"group": 2, "participants": 6, "start": "11:00", "end": "11:45"},
        ])
        self.assertEqual(plan["scheduled_participants"], 12)
        self.assertEqual(plan["waitlisted_participants"], 0)
        self.assertEqual(plan["options"][0]["required_minutes"], 105)
        self.assertIn("Group 2: 11:00–11:45 | 6 participants", self.export(plan)["export_text"])

    def test_limit_reports_waiting_list_without_selecting_identities(self):
        memory_id = self.remember(capacity=6)
        plan = self.prepare(memory_id, 1, "limit")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(len(plan["sessions"]), 1)
        self.assertEqual(plan["scheduled_participants"], 6)
        self.assertEqual(plan["waitlisted_participants"], 6)
        self.assertIn("Waiting list: 6 (individual attendees are not selected)", self.export(plan)["export_text"])
        self.assertIn("Choose and inform the 6 people", self.export(plan)["export_text"])
        self.assertEqual(self.store.stats(), {"memories": 1, "versions": 1, "superseded": 0})

    def test_tight_window_blocks_split_without_silently_limiting_attendance(self):
        memory_id = self.remember(capacity=6, end="11:44")
        plan = self.prepare(memory_id, 1, "split")
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["sessions"], [])
        self.assertIsNone(plan["plan_id"])
        self.assertNotIn("export_text", plan)
        self.assertIn("105 minutes", plan["reasons"][0])
        self.assertIn("104 minutes", plan["reasons"][0])
        self.assertTrue(next(option for option in plan["options"] if option["strategy"] == "limit")["feasible"])

    def test_exact_fit_does_not_require_a_transition_after_the_last_group(self):
        memory_id = self.remember(capacity=6, end="11:45")
        plan = self.prepare(memory_id, 1, "split")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["sessions"][-1]["end"], "11:45")

    def test_insufficient_window_for_one_session_is_blocked(self):
        memory_id = self.remember(end="10:30")
        for strategy in (None, "split", "limit"):
            with self.subTest(strategy=strategy):
                plan = self.prepare(memory_id, 1, strategy)
                self.assertEqual(plan["status"], "blocked")
                self.assertIsNone(plan["plan_id"])
                self.assertNotIn("export_text", plan)
                self.assertEqual(plan["sessions"], [])

    def test_non_multiple_capacity_has_a_smaller_final_group(self):
        memory_id = self.remember(capacity=5, end="13:00")
        plan = self.prepare(memory_id, 1, "split")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual([row["participants"] for row in plan["sessions"]], [5, 5, 2])
        self.assertEqual(plan["sessions"][-1]["end"], "12:45")

    def test_further_change_cannot_reuse_a_previously_feasible_split(self):
        memory_id = self.remember(capacity=6)
        self.assertEqual(self.prepare(memory_id, 1, "split")["status"], "ready")
        self.correct(memory_id, 1, capacity=4)
        with self.assertRaises(VersionConflict):
            self.prepare(memory_id, 1, "split")
        plan = self.prepare(memory_id, 2, "split")
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("165 minutes", plan["reasons"][0])
        self.assertIsNone(plan["plan_id"])
        self.assertNotIn("export_text", plan)
        self.assertEqual(self.prepare(memory_id, 2, "limit")["waitlisted_participants"], 8)

    def test_required_fields_types_bounds_dates_and_clocks_are_strict(self):
        invalid = [
            {"capacity": 0}, {"capacity": True}, {"capacity": 6.0}, {"capacity": "6"},
            {"participants": 0}, {"participants": 501}, {"participants": False},
            {"session_minutes": 0}, {"session_minutes": 481}, {"transition_minutes": -1},
            {"transition_minutes": 121}, {"transition_minutes": True},
            {"title": " "}, {"schema": "workshop-brief-v2"},
            {"event_date": "2026-02-30"}, {"event_date": "20261031"},
            {"timezone": "Not/AZone"}, {"start": "10:00:00"}, {"end": "24:00"},
            {"start": "12:00"}, {"start": "13:00"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                memory_id = self.remember(**changes)
                with self.assertRaises(ValueError):
                    self.prepare(memory_id, 1)

    def test_json_shape_missing_extra_and_duplicate_fields_are_refused(self):
        malformed = ["not json", "[]", "null", json.dumps({"schema": "workshop-brief-v1"}),
                     json.dumps({**BRIEF, "invented_extra": 1}),
                     json.dumps(BRIEF)[:-1] + ', "capacity": 1}']
        for content in malformed:
            with self.subTest(content=content):
                row = self.store.remember("Malformed synthetic brief", content, ["synthetic:test"])
                with self.assertRaises(ValueError):
                    self.prepare(row["memory_id"], 1)

    def test_clock_changes_are_refused_rather_than_guessed(self):
        for changes in [
            {"event_date": "2026-03-29", "start": "02:30", "end": "04:00"},
            {"event_date": "2026-10-25", "start": "02:30", "end": "04:00"},
            {"event_date": "2026-10-25", "start": "01:30", "end": "04:00"},
        ]:
            with self.subTest(changes=changes):
                memory_id = self.remember(**changes)
                with self.assertRaisesRegex(ValueError, "local time|clock change"):
                    self.prepare(memory_id, 1)

    def test_invalid_expected_version_strategy_and_unknown_memory_are_actionable(self):
        memory_id = self.remember()
        for version in (0, -1, True, 1.0, "1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "expected_version"):
                    self.prepare(memory_id, version)
        with self.assertRaisesRegex(ValueError, "strategy"):
            self.prepare(memory_id, 1, "automatically-pick")
        with self.assertRaises(NotFound):
            self.prepare("mem_unknown", 1)


class WorkshopExportGateTests(WorkshopStoreCase):
    def test_repeated_unchanged_preparation_is_stable_and_export_does_not_change_facts(self):
        memory_id = self.remember()
        first = self.prepare(memory_id, 1)
        again = self.prepare(memory_id, 1)
        self.assertEqual(first["plan_id"], again["plan_id"])
        self.assertEqual(len(self.receipts), 1)
        self.assertNotIn("export_text", first)
        before = self.store.stats()
        exported = self.export(first)
        self.assertEqual(exported["status"], "exported")
        self.assertEqual(exported["brief"]["version"], 1)
        self.assertIn(first["plan_id"], exported["export_text"])
        self.assertIn(exported["checked_at"], exported["export_text"])
        self.assertEqual(self.export(first)["plan_id"], first["plan_id"])
        self.assertEqual(self.store.stats(), before)

    def test_stale_export_is_refused_before_rendering_even_for_blocked_or_malformed_new_context(self):
        for content in (json.dumps({**BRIEF, "capacity": 4}), json.dumps({**BRIEF, "capacity": 0}), "not json"):
            with self.subTest(content=content):
                memory_id = self.remember()
                old_plan = self.prepare(memory_id, 1)
                self.store.correct(memory_id, 1, content, ["synthetic:new-context"])
                with patch("planner._export", side_effect=AssertionError("must not render before the guard")):
                    with self.assertRaisesRegex(VersionConflict, "stale plan"):
                        self.export(old_plan)

    def test_snapshot_cannot_be_changed_by_mutating_a_returned_preview(self):
        memory_id = self.remember()
        plan = self.prepare(memory_id, 1)
        plan["inputs"]["capacity"] = 999
        plan["sessions"][0]["participants"] = 999
        plan["brief"]["source_refs"].append("client-invented-source")
        exported = self.export(plan)
        self.assertIn("Declared capacity: 12", exported["export_text"])
        self.assertIn("Group 1: 10:00–10:45 | 12 participants", exported["export_text"])
        self.assertNotIn("client-invented-source", exported["export_text"])

    def test_hash_change_without_version_change_refuses_export(self):
        memory_id = self.remember()
        plan = self.prepare(memory_id, 1)
        self.store.db.execute("UPDATE versions SET entry_hash=? WHERE memory_id=?", ("0" * 64, memory_id))
        with self.assertRaisesRegex(VersionConflict, "stale plan"):
            self.export(plan)

    def test_invalid_unknown_nonready_and_other_registry_ids_have_no_export(self):
        memory_id = self.remember(capacity=6)
        decision = self.prepare(memory_id, 1)
        self.assertIsNone(decision["plan_id"])
        for value in (None, "", 3, "plan_" + "a" * 31, "plan_" + "A" * 32, "<img onerror=bad()>"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    export_workshop(self.store, value, receipts=self.receipts)
        with self.assertRaises(NotFound):
            export_workshop(self.store, "plan_" + "0" * 32, receipts=self.receipts)
        ready = self.prepare(memory_id, 1, "split")
        with self.assertRaises(NotFound):
            export_workshop(self.store, ready["plan_id"], receipts=PlanReceiptRegistry())
        self.receipts.clear()
        with self.assertRaises(NotFound):
            self.export(ready)

    def test_registry_is_bounded_at_500_without_evicting_facts(self):
        plans = []
        for index in range(501):
            memory_id = self.remember(title="Synthetic workshop " + str(index))
            plans.append(self.prepare(memory_id, 1))
        self.assertEqual(len(self.receipts), 500)
        self.assertLessEqual(self.receipts.total_bytes, self.receipts.max_bytes)
        with self.assertRaises(NotFound):
            self.export(plans[0])
        self.assertEqual(self.export(plans[-1])["brief"]["version"], 1)
        self.assertEqual(self.store.stats()["memories"], 501)
        stable = self.prepare(plans[-1]["brief"]["memory_id"], 1)
        self.assertEqual(stable["plan_id"], plans[-1]["plan_id"])
        self.assertEqual(len(self.receipts), 500)

    def test_registry_byte_budget_is_bounded_as_well_as_entry_count(self):
        memory_id = self.remember()
        plan = self.prepare(memory_id, 1)
        size = self.receipts.total_bytes
        small = PlanReceiptRegistry(max_bytes=size + 100)
        first = small.register(plan)
        second_memory = self.remember(title="Second synthetic workshop")
        second_plan = self.prepare(second_memory, 1)
        second = small.register(second_plan)
        self.assertLessEqual(small.total_bytes, size + 100)
        self.assertEqual(len(small), 1)
        with self.assertRaises(NotFound):
            small.get(first)
        self.assertEqual(small.get(second).memory_id, second_memory)

    def test_check_and_render_hold_the_same_store_lock_against_normal_correction(self):
        import planner
        memory_id = self.remember()
        plan = self.prepare(memory_id, 1)
        rendering = threading.Event()
        attempting = threading.Event()
        corrected = threading.Event()
        errors = []
        original_render = planner._export

        def correction():
            try:
                self.assertTrue(rendering.wait(2))
                attempting.set()
                self.correct(memory_id, 1, capacity=6)
                corrected.set()
            except Exception as error:
                errors.append(error)

        def render(snapshot):
            rendering.set()
            self.assertTrue(attempting.wait(2))
            self.assertFalse(corrected.wait(.02))
            return original_render(snapshot)

        worker = threading.Thread(target=correction)
        worker.start()
        try:
            with patch("planner._export", side_effect=render):
                exported = self.export(plan)
        finally:
            rendering.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(corrected.is_set())
        self.assertEqual(exported["brief"]["version"], 1)
        with self.assertRaises(VersionConflict):
            self.export(plan)


class WorkshopMCPTests(unittest.TestCase):
    def test_real_mcp_tool_reads_own_session_version_and_preserves_six_existing_tools(self):
        from starlette.testclient import TestClient
        from server import build_app

        with tempfile.TemporaryDirectory(prefix="mnemo-workshop-mcp-") as directory:
            untouched = Path(directory) / "must-not-open.sqlite"
            app = build_app(str(untouched), demo=True)
            ids = itertools.count(1)
            with TestClient(app, base_url="http://127.0.0.1:8799") as client:
                def rpc(method, params=None, session=None, notification=False):
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session:
                        headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"})
                    body = {"jsonrpc": "2.0", "method": method}
                    if not notification:
                        body.update(id=next(ids), params=params or {})
                    response = client.post("/mcp", json=body, headers=headers)
                    self.assertIn(response.status_code, (200, 202), response.text)
                    return response

                def connect():
                    response = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                   "clientInfo": {"name": "synthetic-workshop-test", "version": "1"}})
                    session = response.headers["Mcp-Session-Id"]
                    rpc("notifications/initialized", session=session, notification=True)
                    return session

                def call(session, name, arguments, error=False):
                    result = rpc("tools/call", {"name": name, "arguments": arguments}, session).json()["result"]
                    self.assertEqual(bool(result.get("isError")), error, result)
                    return result if error else result.get("structuredContent") or json.loads(result["content"][0]["text"])

                first, second = connect(), connect()
                names = {row["name"] for row in rpc("tools/list", session=first).json()["result"]["tools"]}
                self.assertEqual(names, {"remember", "recall", "list_memories", "correct", "history", "stats", "prepare_workshop", "export_workshop"})
                row = call(first, "remember", {"title": "Synthetic workshop brief", "content": json.dumps(BRIEF),
                                                "source_refs": ["synthetic:mcp-brief-v1"]})
                arguments = {"memory_id": row["memory_id"], "expected_version": 1}
                initial = call(first, "prepare_workshop", arguments)
                self.assertEqual(initial["status"], "ready")
                self.assertNotIn("export_text", initial)
                self.assertTrue(initial["plan_id"].startswith("plan_"))
                first_export = call(first, "export_workshop", {"plan_id": initial["plan_id"]})
                self.assertIn("Version: 1", first_export["export_text"])
                cross_export = call(second, "export_workshop", {"plan_id": initial["plan_id"]}, error=True)
                self.assertIn("NotFound", cross_export["content"][0]["text"])
                self.assertEqual(initial["inputs"], BRIEF)
                self.assertEqual(initial["brief"]["source_refs"], ["synthetic:mcp-brief-v1"])
                isolated_error = call(second, "prepare_workshop", arguments, error=True)
                self.assertIn("NotFound", isolated_error["content"][0]["text"])
                call(first, "correct", {**arguments, "content": json.dumps({**BRIEF, "capacity": 6}),
                                         "source_refs": ["synthetic:mcp-brief-v2"]})
                old_export = call(first, "export_workshop", {"plan_id": initial["plan_id"]}, error=True)
                self.assertIn("stale plan", old_export["content"][0]["text"])
                stale = call(first, "prepare_workshop", arguments, error=True)
                self.assertIn("stale workshop brief", stale["content"][0]["text"])
                new_arguments = {**arguments, "expected_version": 2}
                decision = call(first, "prepare_workshop", new_arguments)
                self.assertEqual(decision["status"], "needs_decision")
                self.assertIsNone(decision["plan_id"])
                self.assertNotIn("export_text", decision)
                split = call(first, "prepare_workshop", {**new_arguments, "strategy": "split"})
                self.assertEqual(split["sessions"][-1]["end"], "11:45")
                self.assertEqual(split["brief"]["version"], 2)
                self.assertEqual(split["brief"]["source_refs"], ["synthetic:mcp-brief-v2"])
                final_export = call(first, "export_workshop", {"plan_id": split["plan_id"]})
                self.assertIn("Version: 2", final_export["export_text"])
                self.assertIn("11:00–11:45", final_export["export_text"])
                self.assertIn("synthetic:mcp-brief-v2", final_export["export_text"])
                self.assertEqual(call(first, "stats", {}), {"memories": 1, "versions": 2, "superseded": 1})
                self.assertEqual(call(second, "stats", {}), {"memories": 0, "versions": 0, "superseded": 0})
            self.assertFalse(untouched.exists())

    def test_durable_facts_are_shared_but_plan_receipts_are_connection_local_and_transient(self):
        from starlette.testclient import TestClient
        from server import build_app

        with tempfile.TemporaryDirectory(prefix="mnemo-durable-export-test-") as directory:
            database = Path(directory) / "synthetic-durable.sqlite"
            app = build_app(str(database))
            ids = itertools.count(1)
            with TestClient(app, base_url="http://127.0.0.1:8799") as client:
                def rpc(method, params=None, session=None, notification=False):
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session:
                        headers.update({"Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"})
                    body = {"jsonrpc": "2.0", "method": method}
                    if not notification:
                        body.update(id=next(ids), params=params or {})
                    response = client.post("/mcp", json=body, headers=headers)
                    self.assertIn(response.status_code, (200, 202), response.text)
                    return response

                def connect():
                    response = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                   "clientInfo": {"name": "durable-export-test", "version": "1"}})
                    self.assertEqual(response.json()["result"]["serverInfo"]["version"], "1.2.0")
                    session = response.headers["Mcp-Session-Id"]
                    rpc("notifications/initialized", session=session, notification=True)
                    return session

                def call(session, name, args, error=False):
                    result = rpc("tools/call", {"name": name, "arguments": args}, session).json()["result"]
                    self.assertEqual(bool(result.get("isError")), error, result)
                    return result if error else result.get("structuredContent") or json.loads(result["content"][0]["text"])

                first, second = connect(), connect()
                row = call(first, "remember", {"title": "Synthetic durable brief", "content": json.dumps(BRIEF),
                                                "source_refs": ["synthetic:durable-v1"]})
                args = {"memory_id": row["memory_id"], "expected_version": 1}
                first_plan = call(first, "prepare_workshop", args)
                self.assertEqual(call(second, "stats", {})["memories"], 1)
                inaccessible = call(second, "export_workshop", {"plan_id": first_plan["plan_id"]}, error=True)
                self.assertIn("NotFound", inaccessible["content"][0]["text"])
                second_plan = call(second, "prepare_workshop", args)
                self.assertNotEqual(first_plan["plan_id"], second_plan["plan_id"])
                self.assertEqual(call(second, "export_workshop", {"plan_id": second_plan["plan_id"]})["status"], "exported")
                response = client.delete("/mcp", headers={"Mcp-Session-Id": first, "MCP-Protocol-Version": "2025-11-25"})
                self.assertEqual(response.status_code, 200)
                third = connect()
                self.assertEqual(call(third, "stats", {})["memories"], 1)
                expired = call(third, "export_workshop", {"plan_id": first_plan["plan_id"]}, error=True)
                self.assertIn("NotFound", expired["content"][0]["text"])
                third_plan = call(third, "prepare_workshop", args)
                # A client did read current facts. Another connection changes them
                # between that read and the official export; the server still refuses.
                read = call(third, "history", {"memory_id": row["memory_id"]})
                self.assertEqual(read["chain"][-1]["version"], 1)
                call(second, "correct", {**args, "content": json.dumps({**BRIEF, "capacity": 6}),
                                          "source_refs": ["synthetic:durable-v2"]})
                rejected = call(third, "export_workshop", {"plan_id": third_plan["plan_id"]}, error=True)
                self.assertIn("stale plan", rejected["content"][0]["text"])
                self.assertEqual(call(third, "stats", {}), {"memories": 1, "versions": 2, "superseded": 1})
            reopened = MemoryStore(database)
            try:
                self.assertEqual(reopened.stats(), {"memories": 1, "versions": 2, "superseded": 1})
                with self.assertRaises(NotFound):
                    export_workshop(reopened, third_plan["plan_id"], receipts=PlanReceiptRegistry())
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
