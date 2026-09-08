import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from explorer.acquisition import Hardware, VENDOR, select_gatt_candidates


def packet(sequence, a=17, b=-23, extended=False):
    raw = bytes([0xA0, sequence & 255]) + int(a).to_bytes(3, "big", signed=True) + int(b).to_bytes(3, "big", signed=True)
    return raw + (b"\x01\x02" if extended else b"") + b"\xC0"


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.batches, self.events = [], []
        self.hardware = Hardware(self.batches.append, self.events.append)

    def tearDown(self):
        self.hardware.close()

    def flush(self):
        self.hardware._queue.join()

    def test_pinned_decoder_hash(self):
        manifest = json.loads((VENDOR / "PROVENANCE.json").read_text())
        for name, metadata in manifest["files"].items():
            self.assertEqual(hashlib.sha256((VENDOR / name).read_bytes()).hexdigest(), metadata["sha256"])

    def test_real_decoder_extremes_packets_and_counter_wrap(self):
        c = self.hardware._create_connection()
        try:
            raw = b"".join(packet(n, -8388608 if n == 0 else 8388607, -23) for n in range(2560))
            for offset in range(0, len(raw), 90):
                c._on_dev1_data(None, raw[offset:offset + 90])
            self.flush()
            self.assertEqual(sum(len(b["channels"]["Left-A"]) for b in self.batches), 2560)
            self.assertEqual(self.batches[0]["channels"]["Left-A"][0], -8388608)
            self.assertEqual(self.batches[0]["channels"]["Left-A"][1], 8388607)
            self.assertEqual(self.batches[0]["units"], "ADC counts")
            self.assertFalse(any(b["continuity"]["dev1"]["holes"] for b in self.batches))
            self.assertFalse(any(e["type"] == "auxiliary_leadoff" for e in self.events))
            restored = b"".join(bytes.fromhex(e["details"]["hex"]) for e in self.events if e["type"] == "packet")
            self.assertEqual(restored, raw)
        finally:
            c.shutdown()

    def test_packet_split_duplicate_replay_and_missing_at_wrap(self):
        c = self.hardware._create_connection()
        try:
            payload = packet(253) + packet(254)
            c._on_dev1_data(None, payload[:5])
            c._on_dev1_data(None, payload[5:])
            c._on_dev1_data(None, packet(0) + packet(0) + packet(254) + packet(1))
            self.flush()
            self.assertEqual(sum(len(b["channels"]["Left-A"]) for b in self.batches), 4)
            continuation = self.batches[-1]["continuity"]["dev1"]
            self.assertEqual(continuation["holes"][0]["nMissing"], 1)
            self.assertEqual(continuation["dupes"], 1)
            self.assertEqual(continuation["replays"], 1)
        finally:
            c.shutdown()

    def test_right_ear_retains_identity_and_auxiliary_bits(self):
        c = self.hardware._create_connection()
        try:
            c._on_dev2_data(None, packet(5, 500, -800, True) + packet(6, 600, -900, True))
            self.flush()
            self.assertEqual(self.batches[0]["channels"], {"Right-A": [500., 600.], "Right-B": [-800., -900.]})
            self.assertEqual(self.batches[0]["continuity"]["dev2"]["n"], 2)
            self.assertTrue(any(e["type"] == "auxiliary_leadoff" for e in self.events))
        finally:
            c.shutdown()

    def test_reconnect_epoch_remains_explicit(self):
        c = self.hardware._create_connection()
        try:
            c._on_dev1_data(None, packet(10) + packet(11))
            c._on_dev1_data(None, packet(12)[:5])
            c._reset_device_epoch(1)
            c._on_dev1_data(None, packet(60) + packet(61))
            self.flush()
            holes = self.batches[-1]["continuity"]["dev1"]["holes"]
            self.assertTrue(any(h["kind"] == "reconnect" and h["uncountable"] for h in holes))
            self.assertEqual(sum(len(batch["channels"]["Left-A"]) for batch in self.batches), 4)
            self.assertTrue(any(event["type"] == "incomplete_frame_at_reconnect" for event in self.events))
        finally:
            c.shutdown()

    def test_long_wall_gap_is_not_aliased_missing_count(self):
        c = self.hardware._create_connection()
        try:
            c._on_dev1_data(None, packet(10))
            c._seq_state(1)["last_pkt_perf"] -= 2
            c._on_dev1_data(None, packet(11))
            self.flush()
            hole = self.batches[-1]["continuity"]["dev1"]["holes"][0]
            self.assertIsNone(hole["nMissing"])
            self.assertTrue(hole["uncountable"])
        finally:
            c.shutdown()

    def test_no_hardware_scan_on_initialization_or_simulation(self):
        with patch("explorer.acquisition._sdk_module", side_effect=AssertionError("No radio import expected")):
            self.hardware.start_simulation({"seed": 7, "gap_at_seconds": .1, "gap_duration_seconds": .2, "gap_ear": "left"})
            time.sleep(.46)
            self.hardware.disconnect()
        self.flush()
        self.assertTrue(self.batches)
        self.assertTrue(all(b["simulation"] and b["source"] == "simulation" for b in self.batches))
        self.assertTrue(any("Left-A" not in b["channels"] and "Right-A" in b["channels"] for b in self.batches))
        self.assertTrue(any(b.get("continuity", {}).get("dev1", {}).get("holes") for b in self.batches))
        self.assertFalse(self.hardware.status()["streaming"])

    def test_simulator_deterministic_and_timestamps_exactly_repeatable_relative(self):
        self.hardware.start_simulation({"seed": 3})
        time.sleep(.23)
        self.hardware.disconnect()
        self.flush()
        first = self.batches[0]
        self.batches.clear()
        self.hardware.start_simulation({"seed": 3})
        time.sleep(.13)
        self.hardware.disconnect()
        self.flush()
        second = self.batches[0]
        self.assertEqual(first["channels"], second["channels"])
        self.assertEqual(second["received_monotonic_ns"] - second["first_sample_monotonic_ns"], 24 * 4_000_000)

    def test_callback_error_visible_without_stopping_acquisition(self):
        self.hardware.on_batch = lambda _: (_ for _ in ()).throw(OSError("disk write failed"))
        self.hardware.start_simulation()
        time.sleep(.13)
        self.flush()
        self.assertIn("disk write failed", self.hardware.status()["delivery_error"])
        self.assertTrue(self.hardware.status()["streaming"])

    def test_device_owner_conflict_precedes_radio_access(self):
        with patch("explorer.acquisition._focus_room_conflict", return_value="Focus Room owns the connection"), patch("explorer.acquisition._sdk_module", side_effect=AssertionError("No radio expected")):
            with self.assertRaisesRegex(RuntimeError, "Focus Room"):
                self.hardware.connect({"left": "selected-device"})

    def test_unknown_generic_and_dfu_services_are_not_command_targets(self):
        def service(uuid):
            return SimpleNamespace(uuid=uuid, characteristics=[SimpleNamespace(uuid=uuid + "tx", properties=["notify"]), SimpleNamespace(uuid=uuid + "rx", properties=["write-without-response"])])
        result = select_gatt_candidates([service("unrelated-service"), service("00001530-1212-efde-1523-785feabcd123"), service("00000000-2fda-1234-1234-123456789012")])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["service"].startswith("00000000-2fda-"))

    def test_disarm_before_start_and_never_confirms_hardware_excitation(self):
        commands = []
        class Connection:
            def set_device_profiles(self, **kwargs): pass
            async def connect_device1(self, address): return True
            async def connect_device2(self, address): return True
            def is_connected(self): return True
            async def stop_streaming(self): commands.append("s"); return True
            async def write_eeg_rx_text(self, side, command): commands.append(command); return True
            async def start_streaming(self): commands.append("v/d/b"); return True
            async def disconnect_device1(self): pass
            async def disconnect_device2(self): pass
            def shutdown(self): pass
        async def probe(address, ear):
            return [{"service": "known", "tx": "notify", "rx": "write"}], {}
        with patch("explorer.acquisition._focus_room_conflict", return_value=None), patch.object(self.hardware, "_create_connection", return_value=Connection()), patch.object(self.hardware, "_probe", side_effect=probe), patch.object(self.hardware, "_acquire_device_lock"):
            state = self.hardware.connect({"left": "selected-device"})
        self.assertEqual(commands[:3], ["s", "lead0", "v/d/b"])
        self.assertFalse(state["excitation_verified_disabled"])
        self.assertIn("unverified", state["passive_mode"])


if __name__ == "__main__":
    unittest.main()
