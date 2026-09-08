"""Transparent, signal-only processing and grouped evaluation. Never mutates source data."""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
import zipfile
from pathlib import Path

import numpy as np

SCHEMA = "eeg-explorer.method/1"
MAX_ANALYSIS_SECONDS = 300
FEATURE_NAMES = ["rms", "std", "variance", "peak_to_peak", "median_absolute_deviation", "flat_fraction", "max_change", "drift_per_second", "delta_fraction", "theta_fraction", "alpha_fraction", "beta_fraction", "line_fraction"]

FEATURE_GROUPS = {
    "amplitude": ["rms", "std", "peak_to_peak", "median_absolute_deviation"],
    "variance": ["variance"], "flatline": ["flat_fraction"],
    "abrupt_changes": ["max_change"], "drift": ["drift_per_second"],
    "spectral_power": ["delta_fraction", "theta_fraction", "alpha_fraction", "beta_fraction"],
    "line_noise": ["line_fraction"],
}


def selected_features(request):
    requested = request.get("features")
    if requested is None:
        return list(FEATURE_NAMES)
    if not isinstance(requested, list) or not requested:
        raise ValueError("Select at least one supported feature group")
    names = []
    for name in requested:
        if name in FEATURE_GROUPS:
            names.extend(FEATURE_GROUPS[name])
        elif name in FEATURE_NAMES:
            names.append(name)
        else:
            raise ValueError(f"Unsupported model feature: {name}. Clipping requires verified ADC rails; cross-ear relationships are descriptive only.")
    return [name for name in FEATURE_NAMES if name in names]


def _number(value, name, low=None, high=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return value


def sample_rate(meta):
    return _number(meta.get("sample_rate"), "recorded nominal sample_rate", 1, 100000)


def _channel_names(meta):
    channels = meta.get("channels", [])
    if isinstance(channels, dict):
        return [str(k) for k in channels]
    return [str(c.get("id", c.get("name", c.get("channel")))) if isinstance(c, dict) else str(c) for c in channels]


def _resolve_channels(meta, requested):
    available = _channel_names(meta)
    selected = [str(c) for c in requested] if requested else available
    if not selected:
        raise ValueError("No recorded signal channels are available")
    if set(selected) - set(available):
        raise ValueError("Selected channels are absent from this recording")
    if len(set(selected)) != len(selected):
        raise ValueError("Channels must be unique")
    return selected


def validate_pipeline(spec, fs):
    """Allow-listed declarative operations; imported code and arbitrary expressions are rejected."""
    spec = spec or {}
    if not isinstance(spec, dict) or set(spec) - {"name", "id", "version", "schema", "mode", "operations", "inputs", "description"}:
        raise ValueError("Pipeline must contain only documented declarative fields; code is not supported")
    mode = spec.get("mode", "causal")
    if mode not in ("causal", "offline"):
        raise ValueError("Pipeline mode must be causal or offline")
    operations = spec.get("operations", [])
    if not isinstance(operations, list) or len(operations) > 12:
        raise ValueError("Pipeline supports up to 12 operations")
    clean = []
    for step in operations:
        if not isinstance(step, dict):
            raise ValueError("Each operation must be an object")
        op = step.get("op")
        allowed = {"bandpass": {"op", "low", "high", "order"}, "highpass": {"op", "hz", "order"}, "lowpass": {"op", "hz", "order"}, "notch": {"op", "hz", "q"}, "demean": {"op"}, "detrend": {"op"}, "detect": {"op", "metric", "threshold"}, "mask_flags": {"op"}}
        if op not in allowed or set(step) - allowed[op]:
            raise ValueError(f"Unsupported operation or parameters: {op}")
        s = dict(step)
        if op in ("demean", "detrend") and mode != "offline":
            raise ValueError(f"{op} uses future samples and requires offline mode")
        if op in ("bandpass", "lowpass", "highpass"):
            s["order"] = int(_number(s.get("order", 4), "filter order", 1, 8))
            if op == "bandpass":
                s["low"] = _number(s.get("low"), "low cutoff", .001, fs / 2 - .001)
                s["high"] = _number(s.get("high"), "high cutoff", .001, fs / 2 - .001)
                if s["low"] >= s["high"]:
                    raise ValueError("Bandpass low cutoff must be below high cutoff")
            else:
                s["hz"] = _number(s.get("hz"), "cutoff", .001, fs / 2 - .001)
        elif op == "notch":
            s["hz"] = _number(s.get("hz"), "notch frequency", .001, fs / 2 - .001)
            s["q"] = _number(s.get("q", 30), "notch Q", 1, 1000)
        elif op == "detect":
            if s.get("metric") not in ("absolute_amplitude", "abrupt_change", "flat_difference"):
                raise ValueError("Detector metric must be absolute_amplitude, abrupt_change, or flat_difference")
            s["threshold"] = _number(s.get("threshold"), "detector threshold", 0)
        clean.append(s)
    inputs = spec.get("inputs", {})
    if not isinstance(inputs, dict) or set(inputs) - {"units", "sample_rate", "channels"}:
        raise ValueError("Pipeline inputs supports only units, sample_rate, channels")
    if inputs.get("sample_rate") is not None and float(inputs["sample_rate"]) != fs:
        raise ValueError("Imported method requires a different sample rate")
    return {"schema": SCHEMA, "name": str(spec.get("name", "Raw signal"))[:100], "version": str(spec.get("version", "1"))[:80], "mode": mode, "operations": clean, "inputs": inputs}


def _scipy():
    try:
        from scipy import signal
        return signal
    except ImportError:
        raise ValueError("Filtering requires scipy. Install this application's requirements first.") from None


def process(values, fs, pipeline):
    """Process one contiguous segment. Filter state resets at every segment/window."""
    x = np.asarray(values, dtype=float)
    if x.ndim != 1 or len(x) < 2 or not np.isfinite(x).all():
        raise ValueError("Processing requires at least two finite, contiguous signal samples")
    spec = validate_pipeline(pipeline, fs)
    y = x.copy()
    flags = np.zeros(len(x), dtype=bool)
    boundary = 0
    steps = []
    for s in spec["operations"]:
        op = s["op"]
        if op in ("bandpass", "lowpass", "highpass", "notch"):
            signal = _scipy()
            if op == "notch":
                b, a = signal.iirnotch(s["hz"], s["q"], fs=fs)
                sos = signal.tf2sos(b, a)
                settling = int(math.ceil(5 * s["q"] / (math.pi * s["hz"]) * fs))
            else:
                cutoff = [s["low"], s["high"]] if op == "bandpass" else s["hz"]
                sos = signal.butter(s["order"], cutoff, btype=op, fs=fs, output="sos")
                slow = s.get("low", s.get("hz"))
                settling = int(math.ceil(3 * fs / slow))
            padlen = 3 * (2 * len(sos) + 1 - min(int((sos[:, 2] == 0).sum()), int((sos[:, 5] == 0).sum())))
            boundary = max(boundary, settling, padlen)
            if spec["mode"] == "offline":
                if len(y) <= padlen:
                    raise ValueError(f"Segment is too short for offline {op}; needs more than {padlen} samples")
                y = signal.sosfiltfilt(sos, y, padlen=padlen)
            else:
                y, _ = signal.sosfilt(sos, y, zi=signal.sosfilt_zi(sos) * y[0])
            steps.append({**s, "sos": sos.tolist(), "boundary_estimate_samples": settling})
        elif op == "demean":
            y -= np.mean(y)
            steps.append(s)
        elif op == "detrend":
            y = _scipy().detrend(y)
            steps.append(s)
        elif op == "detect":
            if s["metric"] == "absolute_amplitude":
                detected = np.abs(y) > s["threshold"]
            elif s["metric"] == "abrupt_change":
                detected = np.r_[False, np.abs(np.diff(y)) > s["threshold"]]
            else:
                detected = np.r_[False, np.abs(np.diff(y)) <= s["threshold"]]
            flags |= detected
            steps.append({**s, "semantics": "signal issue flag; not placement or measured impedance"})
        elif op == "mask_flags":
            # Rejection is represented separately; keeping y finite avoids feeding NaNs into filters.
            if s is not spec["operations"][-1]:
                raise ValueError("mask_flags must be the final pipeline operation")
            steps.append(s)
    mask = flags if any(s["op"] == "mask_flags" for s in spec["operations"]) else np.zeros(len(x), bool)
    return y, flags, mask, {"mode": spec["mode"], "live_compatible": spec["mode"] == "causal", "uses_future_samples": spec["mode"] == "offline", "algorithmic_lookahead_seconds": 0 if spec["mode"] == "causal" else None, "phase_delay_seconds": None, "latency_note": "Causal IIR phase delay is frequency dependent; not characterized as one fixed delay." if spec["mode"] == "causal" else "Offline filters use future samples; unsuitable for live inference.", "boundary_estimate_samples": boundary, "boundary_estimate_seconds": boundary / fs, "boundary_note": "Conservative heuristic, not a guaranteed settling bound; state resets at each gap, selection, and feature window. Offline mode affects both edges.", "steps": steps}


def spectrum(values, fs):
    x = np.asarray(values, dtype=float)
    if len(x) < 2:
        return np.array([]), np.array([])
    x = x - x.mean()
    window = np.hanning(len(x))
    denominator = fs * np.sum(window ** 2)
    if denominator <= 0:
        window = np.ones(len(x)); denominator = fs * len(x)
    powers = abs(np.fft.rfft(x * window)) ** 2 / denominator
    if len(x) % 2:
        powers[1:] *= 2
    else:
        powers[1:-1] *= 2
    return np.fft.rfftfreq(len(x), 1 / fs), powers


def features(values, fs, rails=None):
    x = np.asarray(values, dtype=float)
    if len(x) < 2 or not np.isfinite(x).all():
        raise ValueError("Feature extraction requires finite samples")
    d = np.diff(x)
    freq, power = spectrum(x, fs)
    total = float(np.sum(power))
    frac = lambda lo, hi: float(np.sum(power[(freq >= lo) & (freq < hi)]) / total) if total > 0 else 0.
    time_x = np.arange(len(x)) / fs
    slope = float(np.dot(time_x - time_x.mean(), x - x.mean()) / np.sum((time_x - time_x.mean()) ** 2))
    out = {"rms": float(np.sqrt(np.mean(x*x))), "std": float(np.std(x)), "variance": float(np.var(x)), "peak_to_peak": float(np.ptp(x)), "median_absolute_deviation": float(np.median(abs(x-np.median(x)))), "flat_fraction": float(np.mean(d == 0)), "max_change": float(np.max(abs(d))), "drift_per_second": slope, "delta_fraction": frac(.5, 4), "theta_fraction": frac(4, 8), "alpha_fraction": frac(8, 13), "beta_fraction": frac(13, 30), "line_fraction": frac(49, 51) + frac(59, 61), "clipping_fraction": None}
    if rails is not None and len(rails) == 2:
        out["clipping_fraction"] = float(np.mean((x <= rails[0]) | (x >= rails[1])))
    return out


def _recorded_gaps(store, sid):
    if not hasattr(store, "events"):
        return []
    gaps = []
    for event in store.events(sid, limit=1000000):
        if event.get("kind") != "gap":
            continue
        data = event.get("data") or {}
        start = data.get("start", event.get("t"))
        end = data.get("end", start)
        if start is not None:
            gaps.append({"channel": data.get("channel"), "start": float(start), "end": float(end if end is not None else start), "reason": data.get("reason", "recorded continuity gap"), "unknown_duration": bool((data.get("continuity") or {}).get("uncountable"))})
    return gaps


def _segments(rows, fs, gaps=None):
    current = []
    for row in sorted(rows, key=lambda r: (float(r["t"]), int(r.get("idx", 0)))):
        if not math.isfinite(float(row["value"])) or not math.isfinite(float(row["t"])):
            if current:
                yield current; current = []
            continue
        if current:
            delta = float(row["t"]) - float(current[-1]["t"])
            barrier = any(float(current[-1]["t"]) < g["start"] <= float(row["t"]) + 1e-9 for g in (gaps or []))
            if delta <= 0 or delta > 1.8 / fs or barrier:
                yield current; current = []
        current.append(row)
    if current:
        yield current


def _validate_inputs(meta, pipeline, channels):
    inputs = pipeline.get("inputs", {})
    if inputs.get("units") and inputs["units"] != meta.get("units"):
        raise ValueError("Imported pipeline units do not match recording units")
    if inputs.get("channels") and inputs["channels"] != channels:
        raise ValueError("Imported pipeline channel layout differs from selected recording channels")


def analyze(store, request):
    sid = request.get("session_id")
    meta = store.session(sid)
    fs = sample_rate(meta)
    start = _number(request.get("start", 0), "start", 0)
    end = _number(request.get("end", min(float(meta.get("duration", 30)), start + 30)), "end", start)
    if end <= start or end - start > MAX_ANALYSIS_SECONDS:
        raise ValueError(f"Select a non-empty interval of at most {MAX_ANALYSIS_SECONDS} seconds")
    channels = _resolve_channels(meta, request.get("channels"))
    pipeline = validate_pipeline(request.get("pipeline"), fs)
    _validate_inputs(meta, pipeline, channels)
    if (end-start)*fs*len(channels) > 1_000_000:
        raise ValueError("Selection exceeds one million samples; select fewer channels or a shorter interval")
    rows = store.samples(sid, start=start, end=end, channels=channels)
    recorded_gaps = _recorded_gaps(store, sid)
    result = {"id": str(uuid.uuid4()), "session_id": sid, "revision": meta.get("revision"), "start": start, "end": end, "sample_rate": fs, "units": meta.get("units", "unknown"), "pipeline": pipeline, "channels": [], "metadata": {"signal_only": True, "upstream_processing": meta.get("upstream_processing", "Unknown; inspect acquisition provenance"), "impedance": "Not inferred from signals", "gaps": [], "derived_only": True}}
    for channel in channels:
        channel_gaps = [g for g in recorded_gaps if g["channel"] in (None, channel) and g["end"] >= start and g["start"] <= end]
        segments = list(_segments([r for r in rows if str(r["channel"]) == channel], fs, channel_gaps))
        times, raw, processed, flags, masks, boundaries = [], [], [], [], [], []
        spectral_blocks = []
        for block in segments:
            if len(block) < 2:
                continue
            x = np.array([float(r["value"]) for r in block])
            y, f, m, processing_meta = process(x, fs, pipeline)
            times.extend(float(r["t"]) for r in block)
            raw.extend(x.tolist()); processed.extend(y.tolist()); flags.extend(f.tolist()); masks.extend(m.tolist())
            bounds = processing_meta["boundary_estimate_seconds"]
            boundaries.append({"start": float(block[0]["t"]), "end": min(float(block[-1]["t"]), float(block[0]["t"]) + bounds)})
            if pipeline["mode"] == "offline":
                boundaries.append({"start": max(float(block[0]["t"]), float(block[-1]["t"]) - bounds), "end": float(block[-1]["t"])})
            spectral_blocks.append((x, y))
        if not raw:
            continue
        gaps = [{"channel": channel, "start": float(a[-1]["t"]) + 1/fs, "end": float(b[0]["t"])} for a, b in zip(segments, segments[1:])]
        result["metadata"]["gaps"].extend(gaps + channel_gaps)
        # Spectra use longest contiguous block, never concatenate across gaps.
        sx, sy = max(spectral_blocks, key=lambda pair: len(pair[0]))
        freq, raw_power = spectrum(sx, fs); _, processed_power = spectrum(sy, fs)
        result["channels"].append({"channel": channel, "times": times, "raw": raw, "processed": [None if m else value for value, m in zip(processed, masks)], "removed_component": [r-p for r, p in zip(raw, processed)] if any(s["op"] in ("bandpass", "lowpass", "highpass", "notch", "demean", "detrend") for s in pipeline["operations"]) else None, "flags": flags, "rejected": masks, "boundaries": boundaries, "raw_features": features(sx, fs, meta.get("adc_rails")), "processed_features": features(sy, fs), "spectrum": {"frequencies": freq.tolist(), "raw": raw_power.tolist(), "processed": processed_power.tolist(), "source": "longest contiguous block", "sample_count": len(sx)}, "processing": processing_meta})
    if not result["channels"]:
        raise ValueError("No analyzable samples in selected interval")
    # Channel relationships require actual time alignment; no interpolation across devices/gaps.
    result["relationships"] = []
    for i, a in enumerate(result["channels"]):
        for b in result["channels"][i+1:]:
            by_time = {round(t, 9): v for t, v in zip(b["times"], b["raw"])}
            pairs = [(v, by_time[round(t, 9)]) for t, v in zip(a["times"], a["raw"]) if round(t, 9) in by_time]
            correlation = None
            if len(pairs) > 2:
                xy = np.asarray(pairs)
                if np.std(xy[:, 0]) > 0 and np.std(xy[:, 1]) > 0:
                    correlation = float(np.corrcoef(xy.T)[0, 1])
            result["relationships"].append({"channels": [a["channel"], b["channel"]], "correlation": correlation, "aligned_samples": len(pairs), "timing": "exact recorded time match; no resampling"})
    if request.get("save", True):
        store.save_result(sid, "analysis", result)
    return result


def _annotation_label(a):
    return str(a.get("label") or a.get("label_name") or a.get("label_id") or "")


def _annotation_channels(a, channels):
    scope = a.get("scope", "both")
    if scope in (None, "both", "all"):
        return True
    selected = a.get("channels", [])
    if isinstance(scope, dict):
        selected = scope.get("channels", selected)
        scope = scope.get("ear", scope.get("type", "channels"))
    if scope == "channels" or isinstance(scope, list):
        return set(channels).issubset(set(map(str, selected if scope == "channels" else scope)))
    if scope in ("left", "right"):
        # The pinned Zone adapter publishes explicit Left/Right channel names.
        return all(c.lower().startswith(scope) for c in channels)
    return False


def _collect_windows(store, request):
    session_ids = request.get("session_ids") or ([request["session_id"]] if request.get("session_id") else [])
    if len(set(session_ids)) < 3:
        raise ValueError("At least three distinct recordings are needed for separate train, validation, and test groups")
    if len(session_ids) > 200:
        raise ValueError("Select at most 200 sessions per experiment")
    group_by = request.get("group_by", "session")
    if group_by not in ("session", "participant"):
        raise ValueError("group_by must be session or participant")
    duration = _number(request.get("window_seconds", 2), "window_seconds", .25, 30)
    margin = _number(request.get("transition_margin", .25), "transition_margin", 0, 30)
    labels = set(map(str, request.get("labels", [])))
    feature_names = selected_features(request)
    windows, dataset, excluded = [], {"sessions": [], "group_by": group_by}, {"unreviewed_or_nonmanual": 0, "uncertain": 0, "gaps_or_incomplete": 0, "overlap_or_diagnostic": 0, "scope": 0, "pipeline_boundary_or_rejection": 0}
    fs0 = channels0 = units0 = None
    for sid in sorted(set(session_ids)):
        meta = store.session(sid); fs = sample_rate(meta)
        if meta.get("status") in ("armed", "recording"):
            raise ValueError("Stop recording before using a session for reproducible grouped model evaluation")
        channels = _resolve_channels(meta, request.get("channels"))
        units = meta.get("units", "unknown")
        if fs0 is None:
            fs0, channels0, units0 = fs, channels, units
        if (fs, channels, units) != (fs0, channels0, units0):
            raise ValueError("Training requires matching sample rate, channel layout, and units across sessions; implicit rescaling/resampling is not allowed")
        pipeline = validate_pipeline(request.get("pipeline"), fs)
        _validate_inputs(meta, pipeline, channels)
        group = sid if group_by == "session" else meta.get("participant")
        if not group:
            raise ValueError("Every session needs an explicit pseudonymous participant for participant-level splitting")
        annotations = store.annotations(sid)
        gaps = [g for g in _recorded_gaps(store, sid) if g["channel"] in (None, *channels)]
        accepted_intervals = []
        dataset["sessions"].append({"id": sid, "revision": meta.get("revision"), "participant": meta.get("participant"), "source": meta.get("source"), "group": group})
        for annotation in annotations:
            label = _annotation_label(annotation)
            if labels and label not in labels:
                continue
            if annotation.get("source") != "manual" or not annotation.get("reviewed", False):
                excluded["unreviewed_or_nonmanual"] += 1; continue
            if not label or label.lower() in ("unnamed", "unnamed section", "unlabeled") or annotation.get("needs_review") or annotation.get("uncertain") or annotation.get("uncertain_boundaries"):
                excluded["uncertain"] += 1; continue
            if not _annotation_channels(annotation, channels):
                excluded["scope"] += 1; continue
            uncertainty = _number(annotation.get("boundary_uncertainty", 0), "annotation boundary uncertainty", 0, 3600)
            start = float(annotation["start"]) + margin + uncertainty
            stop = float(annotation["end"]) - margin - uncertainty
            n = int(round(duration * fs))
            actual_duration = n / fs
            cursor = start
            while cursor + actual_duration <= stop + 1e-9:
                end = cursor + actual_duration
                overlaps_accepted = any(a < end and b > cursor for a, b in accepted_intervals)
                crosses_gap = any((cursor < g["start"] < end) or (g["start"] < end and g["end"] > cursor) for g in gaps)
                if overlaps_accepted or crosses_gap:
                    excluded["overlap_or_diagnostic" if overlaps_accepted else "gaps_or_incomplete"] += 1
                    cursor = end; continue
                blockers = [a for a in annotations if a.get("id") != annotation.get("id") and a.get("end") is not None and float(a["start"]) < end and float(a["end"]) > cursor and (a.get("diagnostic") or a.get("attributes", {}).get("diagnostic") or (_annotation_label(a) != label and a.get("source") == "manual" and a.get("reviewed")))]
                if annotation.get("diagnostic") or annotation.get("attributes", {}).get("diagnostic") or blockers:
                    excluded["overlap_or_diagnostic"] += 1; cursor = end; continue
                epsilon = .01 / fs
                rows = store.samples(sid, start=max(0, cursor-epsilon), end=end, channels=channels)
                vector, broken = [], False
                channel_times = []
                for channel in channels:
                    channel_rows = [r for r in rows if str(r["channel"]) == channel and cursor-epsilon <= float(r["t"]) < end - epsilon]
                    segments = list(_segments(channel_rows, fs))
                    if len(segments) != 1 or len(segments[0]) != n or any(r.get("diagnostic") or r.get("is_diagnostic") for r in channel_rows):
                        broken = True; break
                    x = [r["value"] for r in segments[0]]
                    y, flags, masked, pmeta = process(x, fs, pipeline)
                    boundary = int(pmeta["boundary_estimate_samples"])
                    left = boundary; right = len(y) - (boundary if pipeline["mode"] == "offline" else 0)
                    if right - left < max(16, int(.25 * fs)) or masked.any():
                        excluded["pipeline_boundary_or_rejection"] += 1; broken = True; break
                    f = features(y[left:right], fs)
                    vector.extend(f[k] for k in feature_names)
                    channel_times.append([r["t"] for r in segments[0]])
                if broken:
                    excluded["gaps_or_incomplete"] += 1
                else:
                    accepted_intervals.append((cursor, end))
                    windows.append({"session_id": sid, "annotation_id": annotation.get("id"), "group": str(group), "start": cursor, "end": end, "label": label, "features": vector})
                    if len(windows) > 100000:
                        raise ValueError("Experiment exceeds 100,000 windows; select fewer sessions or longer windows")
                cursor = end
    dataset.update({"sample_rate": fs0, "channels": channels0, "units": units0, "requested_window_seconds": duration, "window_seconds": round(duration*fs0)/fs0, "transition_margin": margin, "excluded": excluded, "window_count": len(windows), "pipeline": pipeline, "feature_names": feature_names})
    return windows, dataset


def _split_groups(windows, seed, explicit=None):
    groups = sorted({w["group"] for w in windows})
    if len(groups) < 3:
        raise ValueError("Reviewed intervals must cover at least three independent session/participant groups")
    if explicit:
        if set(explicit) != {"train", "validation", "test"}:
            raise ValueError("Explicit split requires train, validation, test group lists")
        parts = {key: list(map(str, explicit[key])) for key in ("train", "validation", "test")}
        flattened = sum(parts.values(), [])
        if any(not p for p in parts.values()) or len(set(flattened)) != len(flattened) or set(flattened) != set(groups):
            raise ValueError("Split groups must be nonempty, disjoint, and cover every group exactly once")
        return parts
    rng = np.random.default_rng(seed)
    rng.shuffle(groups)
    ntest = max(1, int(round(len(groups) * .2)))
    nval = max(1, int(round(len(groups) * .2)))
    return {"train": groups[ntest+nval:], "validation": groups[ntest:ntest+nval], "test": groups[:ntest]}


def _predict(matrix, model):
    x = (np.asarray(matrix) - np.asarray(model["normalization"]["mean"])) / np.asarray(model["normalization"]["scale"])
    centroids = np.asarray(model["centroids"])
    distance = np.sqrt(np.mean((x[:, None, :] - centroids[None, :, :]) ** 2, axis=2))
    best = distance.argmin(axis=1)
    return [model["classes"][int(i)] if d <= model["unknown_threshold"] else "unknown" for i, d in zip(best, distance.min(axis=1))], distance.min(axis=1).tolist()


def infer_window(model, samples, fs, units, channels, gaps=None):
    """Shared export inference: one complete window, same features and resetting as training.

    Inputs are actual recorded sample rows. Missing/gapped data returns unknown. This
    is intentionally a window API, not a stateful real-time stream implementation.
    """
    fs = _number(fs, "sample_rate", 1, 100000)
    if model.get("schema") != SCHEMA or model.get("algorithm") != "nearest_centroid_standardized_euclidean":
        raise ValueError("Unsupported declarative model schema or algorithm")
    if (fs, units, list(channels)) != (model["sample_rate"], model["units"], model["channels"]):
        raise ValueError("Inference sample rate, units, and channel layout must match the model")
    expected = int(round(model["window_seconds"] * fs))
    vector = []
    feature_names = model.get("feature_names", FEATURE_NAMES)
    if not feature_names or any(name not in FEATURE_NAMES for name in feature_names):
        raise ValueError("Model contains unsupported features")
    for channel in channels:
        channel_rows = [r for r in samples if str(r["channel"]) == channel]
        channel_gaps = [g for g in (gaps or []) if g.get("channel") in (None, channel)]
        segments = list(_segments(channel_rows, fs, channel_gaps))
        if len(segments) != 1 or len(segments[0]) != expected or any(r.get("diagnostic") or r.get("is_diagnostic") for r in channel_rows):
            return {"prediction": "unknown", "reason": "missing, discontinuous, diagnostic, or incomplete window", "distance": None}
        y, _, masked, pmeta = process([r["value"] for r in segments[0]], fs, model["pipeline"])
        boundary = int(pmeta["boundary_estimate_samples"])
        right = len(y) - (boundary if model["pipeline"]["mode"] == "offline" else 0)
        if right-boundary < max(16, int(.25*fs)) or masked.any():
            return {"prediction": "unknown", "reason": "artifact rejection or insufficient settled samples", "distance": None}
        extracted = features(y[boundary:right], fs)
        vector.extend(extracted[k] for k in feature_names)
    expected_features = len(channels)*len(feature_names)
    if len(model["normalization"]["mean"]) != expected_features or len(model["normalization"]["scale"]) != expected_features or any(len(row) != expected_features for row in model["centroids"]):
        raise ValueError("Model feature dimensions are inconsistent")
    if not all(np.isfinite(np.asarray(v, dtype=float)).all() for v in (model["normalization"]["mean"], model["normalization"]["scale"], model["centroids"])) or any(v <= 0 for v in model["normalization"]["scale"]):
        raise ValueError("Model parameters must be finite with positive normalization scales")
    if len(model["classes"]) != len(model["centroids"]) or not model["classes"]:
        raise ValueError("Model class dimensions are inconsistent")
    _number(model["unknown_threshold"], "unknown_threshold", 0)
    predictions, distances = _predict([vector], model)
    return {"prediction": predictions[0], "distance": distances[0], "reason": "distance exceeds validation-selected threshold" if predictions[0] == "unknown" else "nearest training centroid", "window_seconds": model["window_seconds"], "uses_future_samples": model["pipeline"]["mode"] == "offline"}


def _metrics(rows, predictions, classes):
    names = list(classes) + (["unknown"] if "unknown" not in classes else [])
    matrix = [[0 for _ in names] for _ in names]
    for row, pred in zip(rows, predictions):
        matrix[names.index(row["label"])][names.index(pred)] += 1
    per_class = {}
    for label in classes:
        i = names.index(label); tp = matrix[i][i]; support = sum(matrix[i]); predicted = sum(row[i] for row in matrix)
        recall = tp / support if support else None
        precision = tp / predicted if predicted else 0.
        f1 = 2*precision*recall/(precision+recall) if recall is not None and precision+recall else 0.
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
    return {"accuracy": sum(row["label"] == p for row, p in zip(rows, predictions))/len(rows) if rows else None, "unknown_rate": predictions.count("unknown")/len(predictions) if predictions else None, "per_class": per_class, "confusion": {"labels": names, "matrix": matrix, "rows": "reviewed manual label", "columns": "prediction"}, "count": len(rows)}


def train(store, request):
    windows, dataset = _collect_windows(store, request)
    if not windows:
        raise ValueError("No eligible windows: review manual intervals, check scope, length, gaps, and pipeline boundary exclusion")
    if any(w["label"].lower() == "unknown" for w in windows):
        raise ValueError("The name 'unknown' is reserved for abstention. Exclude unknown observations from supervised training.")
    classes = sorted({w["label"] for w in windows})
    if len(classes) < 2:
        raise ValueError("Select at least two reviewed condition labels")
    split = _split_groups(windows, int(request.get("seed", 42)), request.get("split"))
    subsets = {key: [w for w in windows if w["group"] in groups] for key, groups in split.items()}
    for key, rows in subsets.items():
        missing = set(classes) - {r["label"] for r in rows}
        if missing:
            raise ValueError(f"{key} split lacks reviewed windows for {', '.join(sorted(missing))}. Collect each condition in independent groups or supply explicit group splits.")
    x = np.array([r["features"] for r in subsets["train"]])
    mean = x.mean(axis=0); scale = x.std(axis=0); scale[scale < 1e-12] = 1
    z = (x-mean)/scale
    centroids = [z[[r["label"] == c for r in subsets["train"]]].mean(axis=0).tolist() for c in classes]
    model_id = str(uuid.uuid4())
    reference_source = Path(__file__).read_text()
    import scipy
    runtime_versions = {"numpy": np.__version__, "scipy": scipy.__version__}
    model = {"schema": SCHEMA, "id": model_id, "version": "1.0.0", "algorithm": "nearest_centroid_standardized_euclidean", "task": str(request.get("task", "placement")), "classes": classes, "features": [f"{c}:{name}" for c in dataset["channels"] for name in dataset["feature_names"]], "feature_names": dataset["feature_names"], "normalization": {"mean": mean.tolist(), "scale": scale.tolist(), "fitted_on": "training groups only"}, "centroids": centroids, "unknown_threshold": 1e100, "sample_rate": dataset["sample_rate"], "channels": dataset["channels"], "units": dataset["units"], "window_seconds": dataset["window_seconds"], "pipeline": dataset["pipeline"], "missing_data_behavior": "unknown; no interpolation; require complete contiguous window for every selected signal channel", "feature_boundary_behavior": "reset per window; exclude estimated filter settling boundaries", "signal_only": True, "reference_sha256": hashlib.sha256(reference_source.encode()).hexdigest(), "runtime_versions": runtime_versions}
    validation_x = [r["features"] for r in subsets["validation"]]
    _, distances = _predict(validation_x, model)
    # Select threshold from validation only. Test labels never select parameters.
    thresholds = sorted(set(float(v) for v in np.quantile(distances, [0, .25, .5, .75, .9, 1])))
    candidates = []
    for threshold in thresholds:
        model["unknown_threshold"] = threshold + 1e-12
        pred, _ = _predict(validation_x, model)
        score = np.mean([1. if p == r["label"] else 0. if p == "unknown" else -1. for r, p in zip(subsets["validation"], pred)])
        candidates.append((float(score), -threshold, threshold + 1e-12))
    model["unknown_threshold"] = max(candidates)[2]
    prediction_outputs = []
    result = {"id": model_id, "created": time.time(), "model": model, "reference_source": reference_source, "seed": int(request.get("seed", 42)), "split": split, "dataset": dataset, "threshold_selection": {"source": "validation only", "utility": "correct +1, incorrect -1, abstention 0; ties prefer smaller threshold", "candidates": thresholds}, "errors": [], "review_queue": [], "limitations": ["Exploratory baseline, not validated contact detection or clinical EEG interpretation.", "Grouping prevents recording leakage; small group counts still limit generalization.", "Prediction is a reviewed condition label, not measured impedance or proof of wearing.", "No resampling, auxiliary sensors, or diagnostic intervals enter this signal-only model.", "Event-level detection delay is not estimated from this window-classification experiment.", "Window output availability is at window end plus compute time; IIR phase delay is frequency dependent."]}
    for key in ("validation", "test"):
        rows = subsets[key]
        predictions, distances = _predict([r["features"] for r in rows], model)
        result[key] = _metrics(rows, predictions, classes)
        result[key]["groups"] = len(split[key])
        result[key]["unique_sessions"] = len({r["session_id"] for r in rows})
        for row, pred, distance in zip(rows, predictions, distances):
            output = {k: row[k] for k in ("session_id", "annotation_id", "start", "end", "label")}
            output.update({"prediction": pred, "distance": distance, "split": key, "jump": {"session_id": row["session_id"], "start": row["start"], "end": row["end"]}})
            prediction_outputs.append(output)
            if pred != row["label"]:
                result["errors"].append(output)
            if pred == "unknown" or pred != row["label"]:
                result["review_queue"].append(output)
    result["test"]["detection_delay_seconds"] = None
    result["test"]["window_availability_seconds"] = dataset["window_seconds"]
    result["focus_room_errors"] = [e for e in result["errors"] if e["split"] == "test" and ((any(x in e["label"].lower() for x in ("table", "finger")) and "in ear" in e["prediction"].lower()) or ("in ear" in e["label"].lower() and any(x in e["label"].lower() for x in ("mov", "walk")) and "off" in e["prediction"].lower()))]
    result["feature_artifacts"] = []
    result["prediction_artifacts"] = []
    for session in dataset["sessions"]:
        sid = session["id"]
        session_windows = [window for window in windows if window["session_id"] == sid]
        feature_id = store.save_result(sid, "features", {"model_id": model_id, "source_revision": session["revision"], "feature_names": model["features"], "windows": session_windows})
        result["feature_artifacts"].append({"session_id": sid, "result_id": feature_id, "window_count": len(session_windows)})
        session_predictions = [prediction for prediction in prediction_outputs if prediction["session_id"] == sid]
        if session_predictions:
            prediction_id = store.save_result(sid, "predictions", {"model_id": model_id, "source_revision": session["revision"], "predictions": session_predictions, "source": "model", "reviewed": False})
            result["prediction_artifacts"].append({"session_id": sid, "result_id": prediction_id, "window_count": len(session_predictions)})
    destination = Path(store.root)/"models"; destination.mkdir(parents=True, exist_ok=True)
    path = destination/f"{model_id}.json"
    path.write_text(json.dumps(result, indent=2, allow_nan=False))
    for session in dataset["sessions"]:
        store.save_result(session["id"], "model", result)
    return result


def compare(store, request):
    examples = request.get("examples") or [{k: request[k] for k in ("session_id", "start", "end", "channels") if k in request}]
    if not 1 <= len(examples) <= 8:
        raise ValueError("Choose between one and eight comparison examples")
    items = []
    for example in examples:
        current = analyze(store, {**example, "pipeline": request.get("pipeline"), "save": False})
        reference = analyze(store, {**example, "pipeline": request.get("reference_pipeline"), "save": False})
        differences = []
        for a, b in zip(reference["channels"], current["channels"]):
            differences.append({"channel": a["channel"], "flag_disagreements": sum(x != y for x, y in zip(a["flags"], b["flags"])), "candidate_features": b["processed_features"], "reference_features": a["processed_features"]})
        items.append({"candidate": current, "reference": reference, "differences": differences})
    result = {"id": str(uuid.uuid4()), "examples": items, "reference": "Named declarative pipeline; current Focus Room production classifier is not represented unless explicitly supplied and validated.", "limitations": ["Flags indicate signal issues, not contact state.", "Visual appearance and feature differences alone do not prove an improvement."]}
    for sid in set(example["session_id"] for example in examples):
        store.save_result(sid, "comparison", result)
    return result


def candidate(request):
    description = str(request.get("description", "")).strip()
    if not description or len(description) > 4000:
        raise ValueError("Describe the candidate in 1–4000 characters")
    fs = _number(request.get("sample_rate"), "sample_rate", 1, 100000)
    # This is intentionally a reviewable template generator, not an unconfigured LLM claim.
    op = {"op": "detect", "metric": "abrupt_change", "threshold": float(request.get("threshold", 100))}
    pipeline = {"name": "Review candidate: abrupt changes", "version": "0.1.0", "mode": "causal", "operations": [op], "inputs": {"sample_rate": fs}}
    if "notch" in description.lower():
        hz = 60 if "60" in description else 50
        if hz >= fs/2:
            raise ValueError("Requested line-frequency notch exceeds the recorded Nyquist frequency")
        pipeline["operations"].insert(0, {"op": "notch", "hz": hz, "q": 30})
    pipeline = validate_pipeline(pipeline, fs)
    return {"pipeline": pipeline, "description": description, "builder": "local deterministic candidate template", "requires_review": True, "execution": "declarative only; no generated or imported code is executed", "assumptions": ["Large successive-sample changes may flag interruptions or movement; they do not establish placement.", "Threshold is a placeholder in native stream units and must be reviewed and calibrated on training data.", "Low amplitude alone is never classified as poor contact."], "limitations": ["No external AI service is configured; arbitrary natural-language algorithm synthesis is not implemented.", "Evaluate on selected reviewed held-out sessions before export; no automatic pipeline activation or Focus Room promotion."]}


def export_model(store, request):
    if request.get("approved") is not True:
        raise ValueError("Explicit approval is required to export this candidate for integration review")
    model_id = str(request.get("model_id", ""))
    try:
        uuid.UUID(model_id)
    except ValueError:
        raise ValueError("Select a saved model id") from None
    source = Path(store.root)/"models"/f"{model_id}.json"
    if not source.exists() and hasattr(store, "results"):
        # Portable session archives carry full model results; rebuild the convenience index lazily.
        for session in store.list_sessions():
            for saved in store.results(session["id"]):
                data = saved.get("data", {})
                if saved.get("kind") == "model" and data.get("id") == model_id:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text(json.dumps(data, indent=2, allow_nan=False))
                    break
            if source.exists():
                break
    if not source.exists():
        raise ValueError("Saved model was not found")
    result = json.loads(source.read_text())
    model = result["model"]
    package = {"schema": "eeg-explorer.integration/1", "id": model_id, "version": model["version"], "approved_for_export": True, "production_promotion": "not performed; explicit integration review and rollback plan required", "model": model, "evaluation": {key: result[key] for key in ("test", "validation", "dataset", "split", "limitations")}, "runtime": "explorer.analysis.infer_window; exact reference implementation included", "latency": {"window_seconds": model["window_seconds"], "future_samples": model["pipeline"]["mode"] == "offline", "phase_delay_seconds": None}, "missing_data_behavior": model["missing_data_behavior"]}
    output = Path(store.root)/"exports"; output.mkdir(parents=True, exist_ok=True)
    target = output/f"method-{model_id}-v{model['version']}.zip"
    # Package contains reviewed declarative weights and auditable implementation, never auto-loads code.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package.json", json.dumps(package, indent=2, allow_nan=False))
        archive.writestr("reference/analysis.py", result["reference_source"])
        archive.writestr("README.txt", "Versioned experimental model. Review package.json and reference/analysis.py. NumPy and SciPy are required. No automatic Focus Room installation. Offline pipelines cannot perform live inference. Missing/gapped data must return unknown. Use normalization and thresholds exactly as stored. The reference implementation is source for review, not an automatically executable plugin.\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"path": str(target), "sha256": digest, "version": model["version"], "model_id": model_id, "promoted": False}
