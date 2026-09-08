"""Independent acquisition. Recording and annotations are owned by the caller.

The only hardware decoder is the pinned Focus Room SDK. No physiology or fit
gate runs on this path. Callbacks run on a delivery thread and must be prompt.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
from pathlib import Path
import queue
import random
import threading
import time
import urllib.request

CHANNELS = ("Left-A", "Left-B", "Right-A", "Right-B")
SAMPLE_RATE = 250
NUS = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
SIG_SUFFIX = "-0000-1000-8000-00805f9b34fb"
ZONE_PREFIXES = ("00000000-2fda-", "efaecafe-")
DFU = {"00001530-1212-efde-1523-785feabcd123", "8ec90001-f315-4f60-9fb8-838830daea50", "fe59"}
VENDOR = Path(__file__).resolve().parents[1] / "vendor"


def provenance():
    return json.loads((VENDOR / "PROVENANCE.json").read_text())


def _sdk_module():
    # Delayed import keeps replay and simulation usable without BLE packages.
    try:
        from vendor.zone_sdk import connection
    except ImportError as exc:
        raise RuntimeError("Real Zone acquisition requires bleak. Run the setup script first.") from exc
    return connection


def select_gatt_candidates(services):
    """Same property/ranking policy as Focus Room, limited to known families.

    A generic writable service may control something unrelated to EEG. Explorer
    does not send stream commands to such a service merely because it can write.
    """
    candidates = []
    for service in services:
        su = str(service.uuid).lower()
        if su in DFU or su.endswith(SIG_SUFFIX):
            continue
        if su != NUS and not su.startswith(ZONE_PREFIXES):
            continue
        chars = list(service.characteristics)
        notifies = [c for c in chars if set(c.properties) & {"notify", "indicate"}]
        writes = [c for c in chars if set(c.properties) & {"write", "write-without-response"}]
        if not notifies or not writes:
            continue
        tx = next((c for c in notifies if not set(c.properties) & {"write", "write-without-response"}), notifies[0])
        pool = [c for c in writes if not set(c.properties) & {"notify", "indicate"}] or writes
        rx = next((c for c in pool if "write-without-response" in c.properties), pool[0])
        triplet = {"service": su, "rx": str(rx.uuid).lower(), "tx": str(tx.uuid).lower()}
        rank = -1 if su == NUS else 0 if triplet["tx"] != triplet["rx"] else 2
        candidates.append((rank, triplet))
    return [trip for _, trip in sorted(candidates, key=lambda pair: (pair[0], pair[1]["service"]))]


def _focus_room_conflict():
    for port in (4321,):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/__info", timeout=0.35) as response:
                info = json.load(response)
            if info.get("simulate") is False:
                return f"Focus Room is running with real EEG on port {port}. Close it before connecting Explorer."
        except Exception:
            pass
    return None


class Hardware:
    def __init__(self, on_batch, on_event):
        self.on_batch, self.on_event = on_batch, on_event
        self._lock = threading.RLock()
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._queue = queue.Queue()
        self._delivery_error = None
        self._conn = None
        self._device_lock = None
        self._desired = {}
        self._profiles = {}
        self._reconnect_tasks = {}
        self._simulation = None
        self._generation = 0
        self._last_sample = {"left": None, "right": None}
        self._state = {"mode": "disconnected", "connected": False, "streaming": False,
                       "source": None, "sample_rate": SAMPLE_RATE, "sample_rate_status": "nominal_unverified",
                       "units": "ADC counts", "channels": list(CHANNELS), "devices": {},
                       "passive_mode": "not_requested", "excitation_verified_disabled": False,
                       "provenance": provenance(), "received_samples": 0,
                       "timing": "per-ear independent counter; host receipt estimates; no device timestamp",
                       "upstream_processing": "unknown hardware/firmware filters, gain, reference and bias",
                       "calibration": "unverified; no microvolt conversion"}
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="explorer-acquisition")
        self._thread.start()
        self._delivery = threading.Thread(target=self._deliver, daemon=True, name="explorer-raw-delivery")
        self._delivery.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout=90):
        if self._closed:
            coro.close()
            raise RuntimeError("Acquisition is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("Acquisition operation timed out; inspect connection diagnostics")

    def _event(self, kind, **details):
        self._queue.put(("event", {"type": kind, "time": time.time(), "time_ns": time.time_ns(),
                                  "monotonic_ns": time.monotonic_ns(), "details": details}))

    def _deliver(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                kind, payload = item
                if kind == "barrier":
                    payload.set()
                    continue
                (self.on_batch if kind == "batch" else self.on_event)(payload)
            except Exception as exc:
                # Acquisition continues; the visible status must disclose failed persistence.
                self._delivery_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def _batch(self, channels, continuity, wall, mono, source, **extra):
        n = max((len(values) for values in channels.values()), default=0)
        if not n:
            return
        batch = {"channels": channels, "sample_rate": SAMPLE_RATE, "received_wall_ns": wall,
                 "received_monotonic_ns": mono, "continuity": continuity, "units": "ADC counts",
                 "source": source, "simulation": source == "simulation", "timing_method": "host_receipt_nominal_rate_estimate",
                 "device_timestamp_available": False, "cross_ear_synchronization": "unverified",
                 "first_sample_wall_ns": wall - int((n - 1) * 1e9 / SAMPLE_RATE),
                 "first_sample_monotonic_ns": mono - int((n - 1) * 1e9 / SAMPLE_RATE), **extra}
        with self._lock:
            self._state["received_samples"] += sum(len(v) for v in channels.values())
            for ear, prefix in (("left", "Left"), ("right", "Right")):
                if any(len(values) for name, values in channels.items() if name.startswith(prefix)):
                    self._last_sample[ear] = mono
        self._queue.put(("batch", batch))

    def status(self):
        with self._lock:
            value = copy.deepcopy(self._state)
            now = time.monotonic_ns()
            value["samples_arriving"] = {ear: bool(value.get("devices", {}).get(ear, {}).get("connected")) and last is not None and now - last < 2_000_000_000 for ear, last in self._last_sample.items()}
            value["sample_age_seconds"] = {ear: (now - last) / 1e9 if last else None for ear, last in self._last_sample.items()}
        value["delivery_queue"] = self._queue.qsize()
        value["delivery_error"] = self._delivery_error
        return value

    def flush(self, timeout=15):
        """Deliver everything received before this barrier, including in-flight decoding."""
        barrier = threading.Event()
        locks = list(getattr(self._conn, "_capture_locks", {}).values())
        for lock in locks:
            lock.acquire()
        try:
            self._queue.put(("barrier", barrier))
        finally:
            for lock in reversed(locks):
                lock.release()
        if not barrier.wait(timeout):
            raise RuntimeError("Acquisition delivery did not flush; raw data may still be pending")

    def scan(self):
        return self._call(self._scan(), 20)

    async def _scan(self):
        devices = await _sdk_module().discover_devices(duration=5)
        self._event("discovery", devices=devices)
        return devices

    def connect(self, request):
        request = dict(request or {})
        if request.get("mode") == "simulation" or request.get("source") == "simulation":
            return self.start_simulation(request)
        return self._call(self._connect(request))

    def _acquire_device_lock(self):
        directory = Path(os.environ.get("EEG_EXPLORER_DATA_DIR", Path.home() / "Library/Application Support/Zone EEG Explorer"))
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / "ble-owner.lock", "a+")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write("0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Another EEG Explorer process owns the Bluetooth connection")
        self._device_lock = handle

    async def _probe(self, address, ear):
        module = _sdk_module()
        client = module.BleakClient(address)
        try:
            await client.connect(timeout=15)
            services = list(client.services)
            candidates = select_gatt_candidates(services)
            if not candidates:
                raise RuntimeError(f"{ear}: no supported Zone or Nordic UART EEG service found")
            # Standard Device Information is read only if the device exposes it.
            info = {}
            fields = {"2a29": "manufacturer", "2a24": "model", "2a25": "serial", "2a26": "firmware", "2a27": "hardware", "2a28": "software"}
            for service in services:
                for char in service.characteristics:
                    uuid = str(char.uuid).lower()
                    field = next((v for k, v in fields.items() if uuid == f"0000{k}{SIG_SUFFIX}"), None)
                    if field and "read" in char.properties:
                        try:
                            info[field] = bytes(await client.read_gatt_char(char)).decode("utf-8", errors="replace").strip("\x00")
                        except Exception:
                            info[field] = None
            self._event("gatt", ear=ear, address=address, candidates=candidates, device_information=info,
                        services=[{"uuid": str(s.uuid), "characteristics": [{"uuid": str(c.uuid), "properties": list(c.properties)} for c in s.characteristics]} for s in services])
            return candidates, info
        finally:
            if client.is_connected:
                await client.disconnect()
            await asyncio.sleep(0.3)

    def _create_connection(self):
        owner = self
        base = _sdk_module().DualBLEConnection

        class CapturedConnection(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._fragments = {1: bytearray(), 2: bytearray()}
                self._capture_locks = {1: threading.RLock(), 2: threading.RLock()}

            def _reset_device_epoch(self, device):
                fragments = getattr(self, "_fragments", {})
                if fragments.get(device):
                    owner._event("incomplete_frame_at_reconnect", ear="left" if device == 1 else "right", count=len(fragments[device]), original_notifications_preserved=True)
                    fragments[device].clear()
                super()._reset_device_epoch(device)

            def _complete_frames(self, data, device):
                # The upstream parser assumes notification-aligned frames. Keep
                # partial framing bytes here; signed decoding and counter admission
                # still run only in the unchanged, pinned SDK implementation.
                pending = self._fragments[device]
                pending.extend(data)
                i, skipped = 0, 0
                while i < len(pending):
                    if pending[i] != 0xA0:
                        i += 1
                        skipped += 1
                        continue
                    available = len(pending) - i
                    if available < 9:
                        break
                    short_first = pending[i + 8] == 0xC0 and (available == 9 or pending[i + 9] == 0xA0)
                    sizes = (9, 11) if short_first else (11, 9)
                    size = next((size for size in sizes if available >= size and pending[i + size - 1] == 0xC0), None)
                    if size is None:
                        if available < 11:
                            break
                        i += 1
                        skipped += 1
                        continue
                    super()._parse_packets(pending[i:i + size], device)
                    i += size
                del pending[:i]
                if skipped:
                    owner._event("framing_bytes_skipped", ear="left" if device == 1 else "right", count=skipped, original_notifications_preserved=True)

            def _capture(self, sender, data, device):
                with self._capture_locks[device]:
                    self._capture_locked(sender, data, device)

            def _capture_locked(self, sender, data, device):
                wall, mono = time.time_ns(), time.monotonic_ns()
                if device == 1:
                    self._dev1_notify_rx += 1
                else:
                    self._dev2_notify_rx += 1
                ear = "left" if device == 1 else "right"
                owner._event("packet", ear=ear, encoding="hex", hex=bytes(data).hex(),
                             received_wall_ns=wall, received_monotonic_ns=mono,
                             packet_kind="original_ble_notification", source="zone_ble")
                # Decode unchanged SDK packets. Drain on the same device worker,
                # avoiding read_data's disconnected-side filtering and any fit gate.
                self._complete_frames(data, device)
                idx = 0 if device == 1 else 2
                n = min(len(self._channel_buffers[idx]), len(self._channel_buffers[idx + 1]))
                channels = {CHANNELS[i]: [self._channel_buffers[i].popleft() for _ in range(n)] for i in (idx, idx + 1)}
                continuity = {"dev1" if device == 1 else "dev2": self._take_continuity(device, n)}
                if not n and any(v.get("dupes") or v.get("replays") or v.get("refusals") for v in continuity.values()):
                    owner._event("packet_admission", ear=ear, continuity=continuity)
                owner._batch(channels, continuity, wall, mono, "zone_ble", ear=ear,
                             passive_mode=owner._state["passive_mode"], excitation_verified_disabled=False)

            def _on_dev1_data(self, sender, data):
                self._capture(sender, data, 1)

            def _on_dev2_data(self, sender, data):
                self._capture(sender, data, 2)

        connection = CapturedConnection(buffer_size=65536)
        connection.set_disconnect_callback(lambda side: self._loop.call_soon_threadsafe(self._lost, side))
        connection.set_stats_callback(self._stats)
        connection.set_leadoff_tap(lambda dev, positive, negative: self._event("auxiliary_leadoff", ear="left" if dev == 1 else "right", positive=positive, negative=negative, excluded_from_signal_only_model=True, mapping="raw firmware bitfields; electrode mapping unverified"))
        return connection

    def _stats(self, stats):
        with self._lock:
            self._state["connection_stats"] = copy.deepcopy(stats)
        self._event("connection_stats", stats=stats)

    async def _connect(self, request):
        await self._disconnect()
        conflict = _focus_room_conflict()
        if conflict:
            raise RuntimeError(conflict)
        desired = {ear: request.get(f"{ear}_address") or request.get(ear) for ear in ("left", "right")}
        desired = {ear: str(addr) for ear, addr in desired.items() if addr}
        if not desired:
            raise ValueError("Scan and select a left and/or right Zone earbud before connecting")
        if len(set(desired.values())) != len(desired):
            raise ValueError("The same device cannot be assigned to both ears")
        self._acquire_device_lock()
        self._desired = desired
        self._state.update(mode="connecting", source="zone_ble", passive_mode="requested_unverified", devices={})
        try:
            self._conn = self._create_connection()
            profiles, extras = {}, {}
            for ear, address in desired.items():
                candidates, info = await self._probe(address, ear)
                profiles[ear] = candidates[0]
                # Focus Room keeps the non-selected Zone sibling notify subscribed
                # to avoid Windows throttling. No command is sent to that sibling.
                extras[ear] = [c["tx"] for c in candidates[1:] if c["service"].startswith(ZONE_PREFIXES) and c["tx"] != c["rx"]][:1]
                self._state["devices"][ear] = {"address": address, "connected": False, "channels": list(CHANNELS[:2] if ear == "left" else CHANNELS[2:]), "gatt": candidates[0], "candidates": candidates, "firmware": info.get("firmware"), **info}
            self._profiles = profiles
            self._conn.set_device_profiles(dev1_profile=profiles.get("left"), dev2_profile=profiles.get("right"), dev1_extra_tx=extras.get("left", []), dev2_extra_tx=extras.get("right", []))
            for ear, address in desired.items():
                ok = await (self._conn.connect_device1(address) if ear == "left" else self._conn.connect_device2(address))
                self._state["devices"][ear]["connected"] = bool(ok)
                self._event("connection", ear=ear, connected=bool(ok))
            if not self._conn.is_connected():
                raise RuntimeError("No selected earbud connected. It may be owned by another application.")
            await self._conn.stop_streaming()
            self._event("hardware_command", command="s", result="write_completed")
            await asyncio.sleep(0.4)
            disarmed = await self._conn.write_eeg_rx_text("both", "lead0")
            self._event("hardware_command", command="lead0", result="write_completed" if disarmed else "write_failed", excitation_verified_disabled=False)
            await asyncio.sleep(0.4)
            self._state["passive_mode"] = "lead0_written_current_unverified" if disarmed else "lead0_write_failed_current_unknown"
            started = await self._conn.start_streaming()
            self._event("hardware_command", command="v/d/b", result="write_completed" if started else "write_failed")
            self._state.update(mode="streaming" if started else "connected", streaming=bool(started), connected=True)
            self._event("acquisition_config", configuration=self.status())
            for ear in desired:
                if not self._state["devices"][ear]["connected"]:
                    self._lost(ear)
            return self.status()
        except Exception as exc:
            self._event("connection_error", message=str(exc))
            await self._disconnect()
            raise

    def _lost(self, side):
        ear = "left" if side in ("left", "dev1", 1) else "right"
        if ear in self._state["devices"]:
            self._state["devices"][ear]["connected"] = False
        self._state["connected"] = any(d.get("connected") for d in self._state["devices"].values())
        if not self._state["connected"]:
            self._state.update(streaming=False, mode="reconnecting" if self._desired else "disconnected")
        self._event("disconnect", ear=ear, gap=True)
        if ear in self._desired and ear not in self._reconnect_tasks:
            self._reconnect_tasks[ear] = asyncio.create_task(self._reconnect(ear, self._generation))

    async def _reconnect(self, ear, generation):
        try:
            attempt = 0
            while generation == self._generation and ear in self._desired:
                await asyncio.sleep((1, 3, 8, 15, 30)[min(attempt, 4)])
                attempt += 1
                self._event("reconnect_attempt", ear=ear, attempt=attempt)
                try:
                    ok = await (self._conn.connect_device1(self._desired[ear]) if ear == "left" else self._conn.connect_device2(self._desired[ear]))
                    if not ok:
                        continue
                    for command, delay in (("s", .4), ("lead0", .4), ("v", .05), ("d", .05), ("b", 0)):
                        written = await self._conn.write_eeg_rx_text(ear, command)
                        self._event("hardware_command", ear=ear, command=command, result="write_completed" if written else "write_failed", reconnect=True)
                        if not written:
                            raise RuntimeError(f"Reconnect command {command} failed")
                        await asyncio.sleep(delay)
                    self._state["devices"][ear]["connected"] = True
                    self._state.update(connected=True, streaming=True, mode="streaming")
                    self._event("reconnected", ear=ear, continuity="new epoch; gap retained", excitation_verified_disabled=False)
                    return
                except Exception as exc:
                    self._event("reconnect_error", ear=ear, message=str(exc))
        finally:
            self._reconnect_tasks.pop(ear, None)

    def start_simulation(self, request=None):
        return self._call(self._start_simulation(dict(request or {})))

    async def _start_simulation(self, request):
        await self._disconnect()
        self._state.update(mode="streaming", source="simulation", connected=True, streaming=True,
                           passive_mode="not_applicable_simulation", received_samples=0,
                           devices={ear: {"name": f"Simulated {ear} earbud", "connected": True, "firmware": "synthetic-v1", "channels": list(CHANNELS[:2] if ear == "left" else CHANNELS[2:])} for ear in ("left", "right")})
        self._event("acquisition_config", configuration=self.status(), simulation=True, settings=request)
        self._simulation = asyncio.create_task(self._simulate(request))
        return self.status()

    async def _simulate(self, request):
        rng = random.Random(int(request.get("seed", 42)))
        n, size = 0, 25
        previous = {"left": None, "right": None}
        gap_at = float(request.get("gap_at_seconds", request.get("gap_at", -1)))
        gap_duration = float(request.get("gap_duration_seconds", request.get("gap_duration", 0)))
        gap_first = round(gap_at * SAMPLE_RATE)
        gap_end = gap_first + round(gap_duration * SAMPLE_RATE)
        gap_ear = request.get("gap_ear", "both")
        origin_wall, origin_mono = time.time_ns(), time.monotonic_ns()
        while True:
            channels, continuity = {}, {}
            for dev, ear in enumerate(("left", "right"), 1):
                missing = gap_at >= 0 and gap_first <= n < gap_end and gap_ear in (ear, "both")
                if missing:
                    continue
                first = n
                holes = []
                if previous[ear] is not None and first > previous[ear] + 1:
                    holes = [{"pos": 0, "nMissing": first - previous[ear] - 1, "uncountable": False, "wallGapSec": (first - previous[ear] - 1) / SAMPLE_RATE, "kind": "simulated_gap"}]
                for channel in CHANNELS[:2] if ear == "left" else CHANNELS[2:]:
                    frequency = 9 + CHANNELS.index(channel)
                    channels[channel] = [round(5000 * math.sin(2 * math.pi * frequency * (n + j) / SAMPLE_RATE) + rng.gauss(0, 600)) for j in range(size)]
                continuity[f"dev{dev}"] = {"n": size, "firstAbsIdx": first, "lastAbsIdx": first + size - 1, "holes": holes, "dupes": 0, "replays": 0, "refusals": 0, "available": True}
                previous[ear] = first + size - 1
            # Batch receipt occurs after its last nominal sample, as on the device.
            n += size
            await asyncio.sleep(max(0, (origin_mono + int(n * 1e9 / SAMPLE_RATE) - time.monotonic_ns()) / 1e9))
            if channels:
                self._batch(channels, continuity, origin_wall + int((n - 1) * 1e9 / SAMPLE_RATE), origin_mono + int((n - 1) * 1e9 / SAMPLE_RATE), "simulation", seed=int(request.get("seed", 42)))

    def disconnect(self):
        return self._call(self._disconnect())

    async def _disconnect(self):
        self._generation += 1
        self._desired = {}
        tasks = list(self._reconnect_tasks.values())
        self._reconnect_tasks.clear()
        if self._simulation:
            tasks.append(self._simulation)
            self._simulation = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        connection, self._conn = self._conn, None
        if connection:
            try:
                await connection.stop_streaming()
                await asyncio.gather(connection.disconnect_device1(), connection.disconnect_device2(), return_exceptions=True)
            finally:
                for dev, fragment in getattr(connection, "_fragments", {}).items():
                    if fragment:
                        self._event("incomplete_frame_at_close", ear="left" if dev == 1 else "right", count=len(fragment), original_notifications_preserved=True)
                connection.shutdown()
        if self._device_lock:
            self._device_lock.close()
            self._device_lock = None
        self._state.update(mode="disconnected", connected=False, streaming=False)
        for device in self._state["devices"].values():
            device["connected"] = False
        self._event("acquisition_stopped", source=self._state.get("source"))
        return self.status()

    def close(self):
        if self._closed:
            return
        self.disconnect()
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._queue.put(None)
        self._delivery.join(timeout=10)
        self._loop.close()
