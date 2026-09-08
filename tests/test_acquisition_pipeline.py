"""Binary SDK-to-disk integration without radio access or physiological claims."""
import tempfile
import threading
import time
import unittest

from explorer.acquisition import Hardware
from explorer.controller import Controller
from explorer.store import Store
from tests.test_acquisition import packet
from tests.test_controller import FakeSync


class DeferredHardware:
    def __init__(self, batch, event):
        self.batch, self.event = batch, event
        self.pending = []
    def status(self): return {"source": "simulation", "streaming": True}
    def flush(self):
        for kind, value in self.pending:
            (self.batch if kind == "batch" else self.event)(value)
        self.pending = []
    def close(self): self.flush()


def batch(receipt, values=(1, 2, 3)):
    return {"channels": {"Left-A": list(values)}, "sample_rate": 250, "received_monotonic_ns": receipt,
            "received_wall_ns": 1780000000000000000 + receipt, "units": "ADC counts"}


class PipelineTests(unittest.TestCase):
    def test_recording_routes_by_receipt_not_delayed_writer_time(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, DeferredHardware, FakeSync)
            try:
                sid = controller.action({"action": "record_start"})["id"]
                armed = controller.recording["armed_monotonic_ns"]
                controller.hardware.batch(batch(armed - 1, [99]))
                controller.hardware.batch(batch(time.monotonic_ns(), [1]))
                controller.flush()
                self.assertEqual(controller.store.session(sid)["sample_count"], 1)
                receipt = time.monotonic_ns()
                controller.hardware.pending = [("batch", batch(receipt, [2, 3])),
                                               ("event", {"type": "packet", "monotonic_ns": receipt, "details": {"received_monotonic_ns": receipt, "hex": "a0"}}),
                                               ("batch", batch(receipt + 10000000000, [999]))]
                controller.action({"action": "record_stop"})
                self.assertEqual([r["value"] for r in controller.store.samples(sid)], [1, 2, 3])
                self.assertEqual(controller.store.session(sid)["status"], "complete")
                packets = [e for e in controller.store.events(sid,include_packets=True) if e["kind"] == "packet"]
                self.assertEqual(len(packets), 1)
                self.assertGreaterEqual(packets[0]["t"], 0)
                self.assertFalse(any(e['kind']=='packet' for e in controller.store.events(sid)))
            finally:
                controller.close()

    def test_parallel_binary_ears_preserve_raw_packets_and_samples_at_stream_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(directory, Hardware, FakeSync)
            connection = controller.hardware._create_connection()
            controller.hardware._conn = connection
            try:
                sid = controller.action({"action": "record_start"})["id"]
                before = time.monotonic()
                def send(device, extended):
                    for first in range(0, 5000, 10):
                        raw = b"".join(packet(i, i - 2500, 2500 - i, extended) for i in range(first, first + 10))
                        (connection._on_dev1_data if device == 1 else connection._on_dev2_data)(None, raw)
                left = threading.Thread(target=send, args=(1, False))
                right = threading.Thread(target=send, args=(2, True))
                left.start(); right.start(); left.join(); right.join()
                controller.action({"action": "record_stop"})
                elapsed = time.monotonic() - before
                self.assertEqual(controller.store.session(sid)["sample_count"], 20000)
                with controller.store.db(sid) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM events WHERE kind="packet"').fetchone()[0], 1000)
                    self.assertEqual(db.execute('SELECT COUNT(DISTINCT channel) FROM samples').fetchone()[0], 4)
                    self.assertEqual(db.execute('SELECT MIN(value), MAX(value) FROM samples').fetchone()[:], (-2500, 2500))
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM samples WHERE device_index IS NULL').fetchone()[0], 0)
                stats = connection.get_stats()
                self.assertEqual(stats["dev1"]["notifications"], 500)
                self.assertEqual(stats["dev2"]["notifications"], 500)
                self.assertEqual(stats["dev1"]["session"]["lostSeq"], 0)
                self.assertEqual(stats["dev2"]["session"]["received"], 5000)
                self.assertLess(elapsed, 20, "Saving a 20 second raw stream must keep pace in this local software check")
                self.assertEqual(controller.hardware.status()["delivery_queue"], 0)
                self.assertFalse(controller.errors)
            finally:
                controller.close()

    def test_initial_holes_do_not_create_empty_beginning_and_internal_unknown_gap_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.create({"source": "simulation"})["id"]
            first = batch(10000000000, [1, 2])
            first["continuity"] = {"dev1": {"firstAbsIdx": 100, "holes": [{"pos": 0, "nMissing": 50}]}}
            store.ingest(sid, first)
            self.assertAlmostEqual(store.samples(sid)[0]["t"], 0)
            second = batch(12008000000, [3, 4])
            second["continuity"] = {"dev1": {"firstAbsIdx": 102, "holes": [{"pos": 1, "nMissing": None, "uncountable": True, "wallGapSec": 2}]}}
            store.ingest(sid, second)
            rows = store.samples(sid)
            self.assertGreaterEqual(rows[-1]["t"] - rows[-2]["t"], 1.999)
            self.assertEqual([r["device_index"] for r in rows], [100, 101, 102, 103])

    def test_nonrecovering_store_does_not_close_an_active_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = Store(directory)
            sid = writer.create({})["id"]
            writer.ingest(sid, batch(10000000000))
            reader = Store(directory, recover=False)
            self.assertEqual(reader.session(sid)["status"], "recording")
            self.assertFalse(any(e["kind"] == "recovered" for e in reader.events(sid)))


if __name__ == "__main__":
    unittest.main()
