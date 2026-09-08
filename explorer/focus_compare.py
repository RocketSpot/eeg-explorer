"""Replay the pinned Focus Room quality code without changing either raw data or Focus Room."""
from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left, bisect_right
import hashlib
import json
import math
from pathlib import Path
import time

from vendor.focus_room.eeg_stream import EegStream, QUALITY_WINDOW_SEC, QUALITY_HOP_SEC
from .acquisition import CHANNELS, VENDOR


def _finite(value, name):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class _Capture:
    def __init__(self):
        self.t = 0
        self.quality = []
        self.config = None

    def send_raw(self, value):
        if value["type"] == "eeg/quality-v1":
            self.quality.append({"t": self.t, "window_start": max(0, self.t - QUALITY_WINDOW_SEC), "quality": value})
        elif value["type"] == "eeg/config-v1":
            self.config = value


def _continuity(samples, previous, fs):
    if not samples:
        return None
    holes = []
    last = previous
    for pos, row in enumerate(samples):
        index = row.get("device_index")
        if last:
            before_index = last.get("device_index")
            delta_t = float(row["t"]) - float(last["t"])
            if index is not None and before_index is not None:
                if index <= before_index:
                    holes.append({"pos": pos, "nMissing": None, "uncountable": True, "wallGapSec": max(0, delta_t - 1/fs), "kind": "reconnect"})
                elif index - before_index > 1:
                    holes.append({"pos": pos, "nMissing": index - before_index - 1, "uncountable": False, "wallGapSec": None, "kind": "lost_seq"})
                elif delta_t > max(1, 3/fs):
                    holes.append({"pos": pos, "nMissing": None, "uncountable": True, "wallGapSec": delta_t - 1/fs, "kind": "recorded_time_gap"})
            elif delta_t > 1.5/fs:
                holes.append({"pos": pos, "nMissing": None, "uncountable": True, "wallGapSec": delta_t - 1/fs, "kind": "recorded_time_gap"})
        last = row
    return {"n": len(samples), "firstAbsIdx": samples[0].get("device_index"), "lastAbsIdx": samples[-1].get("device_index"), "holes": holes, "dupes": 0, "replays": 0, "refusals": 0, "available": all(s.get("device_index") is not None for s in samples)}


def compare_focus(store, request):
    """Compare current pinned EegStream quality and an Explorer pipeline.

    request: session_id, start, end, channels, pipeline, save (default True).
    Results label signal-quality decisions, never infer physical contact.
    """
    from .analysis import analyze

    sid = request.get("session_id")
    meta = store.session(sid)
    fs = _finite(meta.get("sample_rate") or 0, "sample_rate")
    if fs <= 120:
        raise ValueError("Focus Room's 50/60 Hz quality baseline requires sample rate above 120 Hz")
    if meta.get("units") not in ("counts", "ADC counts", "raw_adc_counts", "adc_counts"):
        raise ValueError("The pinned Focus Room baseline requires unscaled ADC counts")
    start = _finite(request.get("start", 0), "start")
    end = _finite(request.get("end", min(meta.get("duration", 30), start + 30)), "end")
    if start < 0 or end <= start or end - start > 300:
        raise ValueError("Select a nonempty interval of at most 300 seconds")
    selected = request.get("channels") or meta.get("channels") or list(CHANNELS)
    if isinstance(selected, str):
        selected = [selected]
    if any(channel not in CHANNELS for channel in selected):
        raise ValueError("Focus Room comparison requires the documented Left-A/B and Right-A/B channel labels")
    # The rolling six-second window and 64 quality assessments need history.
    # Replay at most 30 seconds before selection, keeping large libraries bounded.
    replay_start = max(0, start - 30)
    rows = store.samples(sid, start=replay_start, end=end, channels=list(CHANNELS))
    if not rows:
        raise ValueError("No recorded samples in the selected interval")
    if len(rows) > 1_000_000:
        raise ValueError("Too many samples for one comparison; reduce the selected interval")
    source_file = VENDOR / "focus_room/eeg_stream.py"
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    expected = json.loads((VENDOR / "PROVENANCE.json").read_text())["files"]["focus_room/eeg_stream.py"]["sha256"]
    if digest != expected:
        raise RuntimeError("Pinned Focus Room baseline failed its integrity check")
    capture, diagnostics = _Capture(), []
    # No session key is given, so the original per-session recorder is disabled.
    # The optional standalone validation recorder is not installed or imported.
    stream = EegStream(capture, diagnostics.append, simulation=meta.get("source") == "simulation", expected_rate_hz=fs)
    stream.clear_state_enabled = bool(meta.get("source") == "simulation")
    bins = defaultdict(lambda: defaultdict(list))
    poll = 50/fs
    for row in rows:
        bins[int(math.floor((float(row["t"]) - replay_start) / poll + 1e-7))][row["channel"]].append(row)
    previous = {"left": None, "right": None}
    last_present = {}
    for bucket in sorted(bins):
        by_channel = bins[bucket]
        for samples in by_channel.values():
            samples.sort(key=lambda sample: sample["t"])
        t = min(end, replay_start + (bucket + 1) * poll)
        capture.t = t
        continuity = {}
        for side, labels in (("left", CHANNELS[:2]), ("right", CHANNELS[2:])):
            samples = by_channel.get(labels[0]) or by_channel.get(labels[1]) or []
            if samples:
                continuity[side] = _continuity(samples, previous[side], fs)
                previous[side] = samples[-1]
                last_present[side] = t
        # Empty columns preserve the independent ears without inserting samples.
        # Presence is inferred from receipt in the last second, never physiological contact.
        labels = [channel for channel in CHANNELS if channel in meta.get("channels", CHANNELS) and t - last_present.get("left" if channel.startswith("Left") else "right", -100) <= 1]
        if not labels:
            continue
        stream.ingest([[float(r["value"]) for r in by_channel.get(label, [])] for label in labels], labels,
                      now_monotonic=1000 + t, sdk_rate=fs, continuity=continuity or None)
    stream.close("explorer offline comparison")
    quality = [row for row in capture.quality if start <= row["t"] <= end]
    candidate = analyze(store, {**request, "session_id": sid, "start": start, "end": end, "channels": selected, "save": False})
    candidate_channels = {channel["channel"]: channel for channel in candidate["channels"]}
    for cc in candidate_channels.values():
        prefix = [0]
        for flag in cc["flags"]:
            prefix.append(prefix[-1] + int(bool(flag)))
        cc["_flag_prefix"] = prefix
    comparisons, intervals = [], []
    reviewed = [a for a in store.annotations(sid) if a.get("reviewed") and a.get("source") == "manual"]
    for row in quality:
        t, window_start = row["t"], max(start, row["window_start"])
        for channel in selected:
            qc = row["quality"]["channels"].get(channel, {})
            cc = candidate_channels.get(channel, {})
            first = bisect_left(cc.get("times", []), window_start)
            last = bisect_right(cc.get("times", []), t)
            count = last - first
            prefix = cc.get("_flag_prefix", [0])
            flagged = prefix[last] - prefix[first]
            fraction = flagged / count if count else None
            comparisons.append({"t": t, "window_start": window_start, "channel": channel,
                                "focus_eligible": qc.get("eligible"), "focus_reasons": qc.get("reasons", []),
                                "candidate_flagged_samples": flagged, "candidate_samples": count, "candidate_flagged_fraction": fraction,
                                "different_flags": (bool(qc.get("reasons")) != bool(flagged)) if count else None,
                                "reviewed_context": [{"id": a["id"], "label": a.get("label"), "issue": a.get("issue"), "placement": a.get("placement"), "activity": a.get("activity")} for a in reviewed if a["start"] < t and a["end"] > window_start]})
            if qc.get("reasons"):
                intervals.append({"channel": channel, "start": max(start, t - QUALITY_HOP_SEC), "end": t,
                                  "assessed_window_start": row["window_start"], "reasons": qc["reasons"], "kind": "focus_quality_assessment"})
    for cc in candidate_channels.values():
        cc.pop("_flag_prefix", None)
    result = {"schema": "eeg-explorer.focus-comparison/1", "session_id": sid, "revision": meta.get("revision"),
              "created_at": time.time(), "start": start, "end": end, "channels": selected,
              "baseline": {"name": "Focus Room EegStream signal quality", "sha256": digest, "module": "vendor/focus_room/eeg_stream.py",
                           "provisional": True, "quality_window_seconds": QUALITY_WINDOW_SEC, "replay_start": replay_start, "config": capture.config},
              "quality_rows": quality, "flagged_intervals": intervals, "candidate": candidate,
              "comparisons": comparisons, "contact": {"available": False, "reason": "Focus Room worn/contact logic requires supported impedance measurement and state. Signal-only replay cannot reproduce a measured contact verdict."},
              "continuity": stream.continuity_summary(), "diagnostics": diagnostics,
              "limitations": ["Pinned original signal-quality code, not Focus Room's impedance or spectral acceptance gate.",
                              "Replay uses independent-ear recorded samples grouped into nominal 50-sample polling intervals; actual live callback timing and link state are not reproduced.",
                              "No interpolation or cross-ear sample pairing. Presence is inferred from recorded arrivals, so live connection metrics are not hardware validation.",
                              "Up to 30 seconds of preselection history is replayed; older hysteresis may differ for a mid-session selection.",
                              "Quality reasons and candidate detection flags have different semantics. Different flags are inspection targets, not correctness scores.",
                              "Manual placement labels do not establish signal quality or measured impedance; contact accuracy is not inferred.",
                              "No production code, pipeline or labels were changed."]}
    if request.get("save", True):
        store.save_result(sid, "focus_comparison", result)
    return result
