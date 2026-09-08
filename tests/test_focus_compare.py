import math
import tempfile
import unittest

from explorer.focus_compare import compare_focus
from explorer.store import Store


class FocusComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def session(self):
        sid = self.store.create({"source": "zone_ble", "units": "ADC counts"})["id"]
        for chunk in range(50):
            i = chunk * 50
            channels = {"Left-A": [0] * 50, "Left-B": [0] * 50,
                        "Right-A": [round(5000 * math.sin(2 * math.pi * 10 * (i + j) / 250)) for j in range(50)],
                        "Right-B": [round(5000 * math.sin(2 * math.pi * 12 * (i + j) / 250)) for j in range(50)]}
            continuity = {dev: {"n": 50, "firstAbsIdx": i, "lastAbsIdx": i + 49, "holes": [], "available": True} for dev in ("dev1", "dev2")}
            self.store.ingest(sid, {"channels": channels, "sample_rate": 250, "received_wall_ns": 1780000000000000000 + (i + 49) * 4000000, "received_monotonic_ns": 1000000000000 + (i + 49) * 4000000, "units": "ADC counts", "continuity": continuity})
        self.store.stop(sid)
        return sid

    def test_pinned_quality_flatline_and_contact_unavailable(self):
        sid = self.session()
        before = self.store.session(sid)
        result = compare_focus(self.store, {"session_id": sid, "start": 1, "end": 9, "save": False})
        self.assertTrue(result["quality_rows"])
        self.assertTrue(any("flatline" in q["quality"]["channels"]["Left-A"]["reasons"] for q in result["quality_rows"]))
        self.assertFalse(result["contact"]["available"])
        self.assertEqual(self.store.session(sid)["revision"], before["revision"])
        self.assertTrue(result["comparisons"])
        self.assertTrue(all(not q["quality"]["clearStateEnabled"] for q in result["quality_rows"]))
        self.assertEqual(result["baseline"]["quality_window_seconds"], 6)

    def test_saves_result_only_and_requires_raw_counts(self):
        sid = self.session()
        samples = self.store.session(sid)["sample_count"]
        result = compare_focus(self.store, {"session_id": sid, "start": 0, "end": 3})
        self.assertEqual(self.store.session(sid)["sample_count"], samples)
        self.assertEqual(len(self.store.results(sid)), 1)
        self.assertEqual(self.store.results(sid)[0]["kind"], "focus_comparison")
        with self.store.db(sid) as db:
            meta = self.store._meta(db)
            meta["units"] = "microvolts"
            self.store._setmeta(db, meta)
        with self.assertRaisesRegex(ValueError, "unscaled ADC counts"):
            compare_focus(self.store, {"session_id": sid})


if __name__ == "__main__":
    unittest.main()
