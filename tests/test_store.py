import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from store import MemoryStore, VersionConflict, NotFound  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.s = MemoryStore(self.path)

    def tearDown(self):
        os.remove(self.path)

    def test_remember_requires_provenance(self):
        with self.assertRaises(ValueError):
            self.s.remember("t", "c", [])

    def test_recall_matches_across_accents(self):
        self.s.remember("Bail appartement", "Signé 24 mois chez Marignier.", ["user:test"])
        r = self.s.recall("bail appartement")
        self.assertEqual(r["count"], 1)

    def test_correction_bumps_version_and_keeps_chain(self):
        m = self.s.remember("Lease", "24 months.", ["user:test"])
        c = self.s.correct(m["memory_id"], 1, "18 months.", ["user:fix"])
        self.assertEqual(c["version"], 2)
        h = self.s.history(m["memory_id"])
        self.assertEqual([(v["version"], v["status"]) for v in h["chain"]],
                         [(1, "superseded"), (2, "current")])

    def test_stale_correction_is_refused(self):
        m = self.s.remember("Lease", "24 months.", ["user:test"])
        self.s.correct(m["memory_id"], 1, "18 months.", ["user:fix"])
        with self.assertRaises(VersionConflict):
            self.s.correct(m["memory_id"], 1, "12 months.", ["user:fix2"])

    def test_recall_returns_current_version_content(self):
        m = self.s.remember("Lease", "24 months ending March 2028.", ["user:test"])
        self.s.correct(m["memory_id"], 1, "18 months ending September 2027.", ["user:fix"])
        r = self.s.recall("lease months")
        self.assertIn("18 months", r["results"][0]["content"])
        self.assertNotIn("March 2028", r["results"][0]["content"])

    def test_audit_mode_lists_superseded(self):
        m = self.s.remember("Lease", "24 months.", ["user:test"])
        self.s.correct(m["memory_id"], 1, "18 months.", ["user:fix"])
        r = self.s.recall("lease", include_superseded=True)
        statuses = sorted(x["status"] for x in r["results"])
        self.assertEqual(statuses, ["current", "superseded"])

    def test_history_unknown_id(self):
        with self.assertRaises(NotFound):
            self.s.history("mem_0000dead")

    def test_fts_injection_is_neutralized(self):
        self.s.remember("Lease", "24 months.", ["user:test"])
        r = self.s.recall('lease" OR 1=1 --')
        self.assertLessEqual(r["count"], 1)


if __name__ == "__main__":
    unittest.main()
