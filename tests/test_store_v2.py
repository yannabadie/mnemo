import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from store import MemoryStore, VersionConflict  # noqa: E402


class StoreV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mnemo-v2-test-")
        self.path = os.path.join(self.tmp.name, "memory.db")
        self.s = MemoryStore(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_correction_is_searchable_immediately(self):
        m = self.s.remember("Lease", "24 months, ending March 2028.",
                            ["lease.pdf:2026-10-12"])
        self.s.correct(m["memory_id"], 1,
                       "18 months, ending September 2027.",
                       ["email:claire:2026-10-20"])
        fresh = self.s.recall("September 2027")
        self.assertEqual(fresh["count"], 1)
        self.assertIn("18 months", fresh["results"][0]["content"])

    def test_stale_wording_no_longer_matches(self):
        m = self.s.remember("Lease", "24 months, ending March 2028.",
                            ["lease.pdf:2026-10-12"])
        self.s.correct(m["memory_id"], 1,
                       "18 months, ending September 2027.",
                       ["email:claire:2026-10-20"])
        stale = self.s.recall("March 2028")
        self.assertEqual(stale["count"], 0)

    def test_correction_requires_provenance(self):
        m = self.s.remember("Lease", "24 months.", ["lease.pdf:2026-10-12"])
        with self.assertRaises(ValueError):
            self.s.correct(m["memory_id"], 1, "18 months.")

    def test_verify_ok_and_detects_tampering(self):
        m = self.s.remember("Lease", "24 months, ending March 2028.",
                            ["lease.pdf:2026-10-12"])
        self.s.correct(m["memory_id"], 1,
                       "18 months, ending September 2027.",
                       ["email:claire:2026-10-20"])
        self.assertTrue(self.s.verify()["ok"])
        with self.s.db:
            self.s.db.execute(
                "UPDATE versions SET content = ? WHERE memory_id = ? AND version = ?",
                ("24 months, ending March 2028.", m["memory_id"], 2))
        result = self.s.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"]["memory_id"], m["memory_id"])
        self.assertEqual(result["broken_at"]["version"], 2)

    def test_history_exposes_entry_hashes(self):
        m = self.s.remember("Lease", "24 months.", ["lease.pdf"])
        h = self.s.history(m["memory_id"])
        self.assertEqual(len(h["chain"]), 1)
        self.assertEqual(len(h["chain"][0]["entry_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
