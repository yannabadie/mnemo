"""Regression tests for the concrete data-loss and false-confidence audit cases.

Every database, including legacy/corrupt fixtures, belongs to TemporaryDirectory.
These tests do not start the application, its transport or an external service.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import ChainBroken, MemoryStore, VersionConflict


class StoreIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mnemo-integrity-test-")
        self.root = Path(self.tmp.name)
        self.stores = []
        self.s = self.open_store("current.db")

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def open_store(self, filename):
        store = MemoryStore(self.root / filename)
        self.stores.append(store)
        return store

    def two_versions(self, store=None):
        store = store or self.s
        mid = store.remember("Lease", "24 months ending March 2028", ["lease.pdf"])['memory_id']
        store.correct(mid, 1, "18 months ending September 2027", ["email:correction"])
        return mid

    def legacy(self, filename, hashed=False):
        path = self.root / filename
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE memories(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,current_version INTEGER NOT NULL)")
        hash_columns = ",prev_hash TEXT NOT NULL,entry_hash TEXT NOT NULL" if hashed else ""
        db.execute("CREATE TABLE versions(memory_id TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,source_refs TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL" + hash_columns + ",PRIMARY KEY(memory_id,version))")
        db.execute("CREATE TABLE fts_map(fts_rowid INTEGER PRIMARY KEY,memory_id TEXT NOT NULL)")
        db.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(title,content,content='',tokenize='unicode61')")
        first, last = "2026-10-22T10:00:00+00:00", "2026-10-22T11:00:00+00:00"
        db.execute("INSERT INTO memories VALUES (?,?,?,?,?)", ("mem_legacy", "Lease", first, last, 2))
        prev = "0" * 64
        for version, content, status, stamp in [(1, "24 months ending March 2028", "superseded", first), (2, "18 months ending September 2027", "current", last)]:
            refs = json.dumps(["lease.pdf" if version == 1 else "email:fix"])
            row = ("mem_legacy", version, content, refs, status, stamp)
            if hashed:
                digest = hashlib.sha256("|".join([prev, "mem_legacy", str(version), content, refs, stamp]).encode()).hexdigest()
                row += (prev, digest)
                prev = digest
            db.execute("INSERT INTO versions VALUES (" + ",".join("?" for _ in row) + ")", row)
        cursor = db.execute("INSERT INTO memory_fts(title,content) VALUES (?,?)", ("lease", "18 months ending september 2027"))
        db.execute("INSERT INTO fts_map VALUES (?,?)", (cursor.lastrowid, "mem_legacy"))
        db.commit()
        db.close()
        return path

    def run_verifier(self, path, *extra):
        return subprocess.run([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "verify.py"), "--db", str(path), *extra],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_absent_subject_does_not_match_shared_stopwords(self):
        self.s.remember("Lease", "my lease is 24 months", ["user:test"])
        self.s.remember("Dentist", "my dentist is Dr Ferrand", ["user:test"])
        self.assertEqual(self.s.recall("What about my passport?")["count"], 0)
        self.assertEqual(self.s.recall("What about my lease?")["count"], 1)
        self.assertEqual(self.s.recall("Who is my dentist?")["count"], 1)
        self.assertEqual(self.s.recall("my the about")["count"], 0)

    def test_all_meaningful_terms_are_required(self):
        self.s.remember("Lease", "Apartment contract with Marignier", ["lease.pdf"])
        self.assertEqual(self.s.recall("lease passport")["count"], 0)
        self.assertEqual(self.s.recall("lease Marignier")["count"], 1)

    def test_references_must_each_be_nonempty_strings(self):
        mid = self.s.remember("Lease", "24 months", ["lease.pdf"])["memory_id"]
        for refs in [None, [], [""], ["  "], [None], [1], "lease.pdf"]:
            with self.subTest(refs=refs):
                with self.assertRaises(ValueError):
                    self.s.remember("Other", "fact", refs)
                with self.assertRaises(ValueError):
                    self.s.correct(mid, 1, "18 months", refs)
        self.assertEqual(self.s.stats()["versions"], 1)

    def test_blank_correction_and_invalid_expected_version_are_refused(self):
        mid = self.s.remember("Lease", "24 months", ["lease.pdf"])["memory_id"]
        for content in ["", " \n ", None, 3]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.s.correct(mid, 1, content, ["email:fix"])
        for version in [True, 0, -1, "1"]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.s.correct(mid, version, "18 months", ["email:fix"])
        self.assertEqual(self.s.stats()["versions"], 1)

    def test_index_failure_rolls_back_correction_and_preserves_other_memory(self):
        mid = self.s.remember("Lease", "24 months ending March 2028", ["lease.pdf"])["memory_id"]
        self.s.remember("Gate", "gate code is 1234", ["user:test"])
        with patch.object(self.s, "_index", side_effect=RuntimeError("injected index failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.s.correct(mid, 1, "18 months ending September 2027", ["email:fix"])
        reopened = self.open_store("current.db")
        self.assertEqual(len(reopened.history(mid)["chain"]), 1)
        self.assertEqual(reopened.recall("March 2028")["count"], 1)
        self.assertEqual(reopened.recall("September 2027")["count"], 0)
        self.assertEqual(reopened.recall("gate code")["count"], 1)
        self.assertTrue(reopened.verify()["ok"])

    def test_remember_index_failure_leaves_no_partial_memory(self):
        with patch.object(self.s, "_index", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.s.remember("Lease", "24 months", ["lease.pdf"])
        self.assertEqual(self.s.stats(), {"memories": 0, "versions": 0, "superseded": 0})

    def test_two_connections_serialize_version_checks(self):
        mid = self.s.remember("Lease", "24 months", ["lease.pdf"])["memory_id"]
        other = self.open_store("current.db")
        barrier, results = threading.Barrier(2), []
        def writer(store, content):
            barrier.wait(timeout=3)
            try:
                store.correct(mid, 1, content, ["user:fix"])
                results.append("corrected")
            except VersionConflict:
                results.append("conflict")
            except Exception as exc:
                results.append(type(exc).__name__)
        threads = [threading.Thread(target=writer, args=(self.s, "18 months")), threading.Thread(target=writer, args=(other, "12 months"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ["corrected", "conflict"])
        self.assertEqual(len(self.s.history(mid)["chain"]), 2)
        self.assertTrue(self.s.verify()["ok"])

    def test_v1_and_v2_migration_preserve_history_and_are_idempotent(self):
        for hashed in [False, True]:
            with self.subTest(hashed=hashed):
                filename = "legacy-hashed.db" if hashed else "legacy-plain.db"
                path = self.legacy(filename, hashed)
                original_hashes = []
                if hashed:
                    with sqlite3.connect(path) as db:
                        original_hashes = db.execute("SELECT prev_hash,entry_hash FROM versions ORDER BY version").fetchall()
                store = self.open_store(filename)
                chain = store.history("mem_legacy")["chain"]
                self.assertEqual([(r["version"], r["content"], r["status"], r["source_refs"]) for r in chain],
                    [(1, "24 months ending March 2028", "superseded", ["lease.pdf"]), (2, "18 months ending September 2027", "current", ["email:fix"])])
                self.assertTrue(store.verify()["ok"])
                if hashed:
                    self.assertEqual([(r["legacy_receipt"]["prev_hash"], r["legacy_receipt"]["entry_hash"]) for r in chain], original_hashes)
                baseline = store.db.execute("SELECT value FROM schema_meta WHERE key='legacy_baseline'").fetchone()[0]
                store.correct("mem_legacy", 2, "12 months ending March 2027", ["email:fix2"])
                store.remember("Gate", "gate code is 1234", ["user:test"])
                hashes = [r["entry_hash"] for r in store.history("mem_legacy")["chain"]]
                reopened = self.open_store(filename)
                self.assertTrue(reopened.verify()["ok"])
                self.assertEqual(hashes, [r["entry_hash"] for r in reopened.history("mem_legacy")["chain"]])
                self.assertEqual(reopened.db.execute("SELECT value FROM schema_meta WHERE key='legacy_baseline'").fetchone()[0], baseline)
                self.assertEqual(reopened.recall("March 2027")["count"], 1)
                self.assertEqual(reopened.list_memories()["total_memories"], 2)

    def test_migration_does_not_launder_a_detectable_v2_mismatch(self):
        path = self.legacy("damaged-v2.db", True)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE versions SET content='forged' WHERE version=2")
        with self.assertRaises(ChainBroken):
            MemoryStore(path)
        with sqlite3.connect(path) as db:
            self.assertNotIn("head_hash", [r[1] for r in db.execute("PRAGMA table_info(memories)")])
            self.assertEqual(db.execute("SELECT content FROM versions WHERE version=2").fetchone()[0], "forged")

    def test_migration_failure_rolls_back_schema_and_hashes(self):
        path = self.legacy("migration-failure.db")
        with patch.object(MemoryStore, "_index", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                MemoryStore(path)
        with sqlite3.connect(path) as db:
            self.assertNotIn("head_hash", [r[1] for r in db.execute("PRAGMA table_info(memories)")])
            self.assertNotIn("entry_hash", [r[1] for r in db.execute("PRAGMA table_info(versions)")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 2)
        repaired = self.open_store("migration-failure.db")
        self.assertTrue(repaired.verify()["ok"])

    def test_verify_rejects_unknown_memory_but_accepts_a_known_empty_store(self):
        self.assertEqual(self.s.verify()["checked"], 0)
        self.assertTrue(self.s.verify()["ok"])
        missing = self.s.verify("mem_doesnotexist")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["note"], "memory not found")

    def test_verify_checks_links_and_metadata(self):
        mutations = [
            ("UPDATE versions SET prev_hash=? WHERE memory_id=? AND version=2", ("f"*64,)),
            ("UPDATE memories SET title=? WHERE id=?", ("forged title",)),
            ("UPDATE memories SET current_version=? WHERE id=?", (1,)),
            ("UPDATE memories SET head_hash=? WHERE id=?", ("0"*64,)),
            ("UPDATE memories SET updated_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00",)),
            ("UPDATE versions SET status=? WHERE memory_id=? AND version=1", ("current",)),
            ("UPDATE versions SET source_refs=? WHERE memory_id=? AND version=2", ('["forged"]',)),
            ("UPDATE versions SET content=? WHERE memory_id=? AND version=2", ("forged",)),
        ]
        for i, (sql, args) in enumerate(mutations):
            with self.subTest(sql=sql):
                store = self.open_store("tamper-" + str(i) + ".db")
                mid = self.two_versions(store)
                with store.db: store.db.execute(sql, (*args, mid))
                self.assertFalse(store.verify(mid)["ok"])

    def test_verify_detects_truncation_and_sequence_gaps(self):
        for reset_pointer in [False, True]:
            with self.subTest(reset_pointer=reset_pointer):
                store = self.open_store("truncate-" + str(reset_pointer) + ".db")
                mid = self.two_versions(store)
                with store.db:
                    store.db.execute("DELETE FROM versions WHERE memory_id=? AND version=2", (mid,))
                    if reset_pointer:
                        store.db.execute("UPDATE memories SET current_version=1,updated_at=created_at WHERE id=?", (mid,))
                        store.db.execute("UPDATE versions SET status='current' WHERE memory_id=?", (mid,))
                self.assertFalse(store.verify(mid)["ok"])
        mid = self.two_versions()
        with self.s.db: self.s.db.execute("DELETE FROM versions WHERE memory_id=? AND version=1", (mid,))
        self.assertFalse(self.s.verify(mid)["ok"])

    def test_correction_refuses_to_append_to_a_broken_chain(self):
        mid = self.two_versions()
        with self.s.db: self.s.db.execute("UPDATE versions SET content='forged' WHERE memory_id=? AND version=1", (mid,))
        with self.assertRaises(ChainBroken):
            self.s.correct(mid, 2, "12 months", ["email:fix"])
        self.assertEqual(self.s.stats()["versions"], 2)

    def test_inventory_pages_include_topics_without_fixed_keywords(self):
        mids = {self.s.remember(title, content, ["user:test"])["memory_id"] for title, content in
            [("Gate", "code is 1234"), ("Parcel", "delivered tomorrow"), ("Window", "repair on Friday")]}
        page = self.s.list_memories(limit=2)
        self.assertEqual(page["total_memories"], 3)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 2)
        last = self.s.list_memories(limit=2, offset=page["next_offset"])
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])
        self.assertEqual({r["memory_id"] for r in page["results"] + last["results"]}, mids)
        first = self.s.list_memories(limit=1)["results"][0]
        self.s.correct(first["memory_id"], 1, "updated fact", ["user:fix"])
        audited = self.s.list_memories(limit=1, include_superseded=True)
        self.assertEqual(audited["count"], 2)
        self.assertEqual(audited["next_offset"], 1)
        self.assertEqual({r["status"] for r in audited["results"]}, {"current", "superseded"})

    def test_inventory_rejects_invalid_pagination(self):
        for args in [{"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.s.list_memories(**args)

    def test_cli_is_read_only_and_unknown_memory_is_not_ok(self):
        path = self.root / "cli.db"
        store = MemoryStore(path)
        store.remember("Lease", "24 months", ["lease.pdf"])
        store.close()
        before = path.read_bytes()
        valid = self.run_verifier(path)
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        self.assertIn("No external trust anchor", valid.stdout)
        missing = self.run_verifier(path, "--memory", "mem_doesnotexist")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("memory not found", missing.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_cli_does_not_migrate_legacy_storage(self):
        path = self.legacy("legacy-cli.db")
        result = self.run_verifier(path)
        self.assertNotEqual(result.returncode, 0)
        with sqlite3.connect(path) as db:
            self.assertNotIn("head_hash", [r[1] for r in db.execute("PRAGMA table_info(memories)")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 2)

    def test_cli_handles_invalid_sqlite_without_a_traceback(self):
        path = self.root / "invalid.db"
        path.write_bytes(b"not a SQLite database")
        result = self.run_verifier(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
