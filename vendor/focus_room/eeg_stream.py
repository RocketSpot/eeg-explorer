"""Phase 2A / 2A.1 — raw EEG batch transport + honest signal quality (one source of truth).

Consumes raw per-channel ADC-count batches (real: SDK ``on_raw_data``; sim: a labelled
synthetic generator) and produces three versioned messages: ``eeg/config-v1`` (once per
stream), ``eeg/raw-v1`` (per batch) and ``eeg/quality-v1`` (throttled).

DISCIPLINE
  * Raw ADC counts are the IMMUTABLE source. No overwriting / filtering / interpolation /
    channel averaging here. Display filtering happens causally in the browser.
  * No microvolts. Amplitudes are reported in ADC COUNTS, never labelled µV (calibration
    unverified — see docs/eeg-hardware-confirmations.md).
  * CONTINUITY (2026-09-06, plan task 4). The firmware packet carries a ONE-BYTE sequence
    counter; connection.py admits every sample through it and read_data hands each device's
    chunk over with its own continuity block (holes with their true size, or marked
    UNCOUNTABLE when the wall gap says the counter wrapped; duplicates; replays). When a
    source supplies that block, ingest(..., continuity=...) reports
    deviceContinuityAvailable=true, continuityMethod 'device_sequence_counter', the holes per
    side, and advances the local index by the REAL missing count. Without it (an older or
    other source) the previous callback-timing inference remains, labelled as such. Either
    way a gap is never closed: no interpolation, no padding, the index steps over it.
  * There is still NO per-sample timestamp. The physical ADC sample rate is UNVERIFIED:
    callback-arrival timing measures INGEST THROUGHPUT, not the hardware sampling frequency
    (deviceMeasuredSampleRateHz=null).
  * All thresholds are PROVISIONAL (unvalidated on real Zone recordings). Until validated,
    real mode must NOT show the final "clear" state (clearStateEnabled=false); only
    deterministic simulation, whose injected signal is known clean, may reach 'clear'.

Quality is computed on RAW ADC COUNTS over an accumulated ROLLING window (not one ~50-sample
callback), updated on a shorter hop. Pure stdlib so it runs in any sidecar and is unit-testable.
"""

import math
import os
import statistics
import time
from collections import deque

# --- ADC geometry (24-bit signed) — only used for clip/flatline in COUNTS, never µV ---
ADC_BITS = 24
ADC_MAX = 2 ** (ADC_BITS - 1) - 1        # +8388607
ADC_MIN = -(2 ** (ADC_BITS - 1))         # -8388608
CLIP_MARGIN_COUNTS = 256

# --- quality window / hop (item 6) -------------------------------------------------
# A single ~50-sample (0.2 s) callback is far too short to judge drift or 50/60 Hz
# line contamination, so every metric is computed over an accumulated ROLLING window
# and refreshed on a shorter hop. 6 s chosen: long enough for a few cycles of the
# slowest drift we care about and >=300 cycles of 50/60 Hz for a stable line probe,
# short enough that a reseat is reflected within ~a window. PROVISIONAL.
QUALITY_WINDOW_SEC = 6.0
QUALITY_HOP_SEC = 0.33                    # emit/refresh cadence (~3 Hz)

# --- provisional thresholds (relative/robust; no absolute µV) ----------------------
FLATLINE_MAD_COUNTS = 12.0                # robust spread below this ⇒ flatline/dead contact
CLIP_FRACTION_BAD = 0.02                  # >2% of window at a rail ⇒ clipping
STEP_MAD_MULT = 12.0                      # a jump > this×(robust deriv) ⇒ electrode step
DRIFT_RANGE_MULT = 6.0                    # slow-mean range > this×(robust amp) ⇒ heavy drift
LINE_RATIO_BAD = 0.5                      # 50/60 Hz magnitude / broadband RMS above this ⇒ line-contaminated
USABLE_MIN_FRACTION = 0.6                 # recent clean fraction a channel needs to be "usable"
CLEAR_SUSTAIN_SEC = 2.0                   # both ears usable this long ⇒ 'clear' (sim only until validated)
RATE_MISMATCH_TOL = 0.15                  # |ingest−expected|/expected above this ⇒ mismatch flag
GAP_TOLERANCE_SAMPLES = 8                 # inferred-missing below this is ignored as jitter
# hysteresis (item 7): consecutive assessments a condition must hold before it acts
SWITCH_HOLD = 3                           # ~1 s at the 0.33 s hop
# eligibility (Phase 2A.2 correction 1): a session is (provisionally) reveal-eligible only
# once analysis-eligibility has held across ENOUGH of the recording — both a minimum number
# of assessments and a minimum clean fraction of them. PROVISIONAL — the orchestrator makes
# the FINAL reveal decision (it knows the event pre/post window + any staff override). This
# layer reports only the signal-derived truth. revealEligible is deliberately a HIGHER bar
# than analysisEligible: the instant analysis first passes, the reveal is not yet eligible.
REVEAL_COVERAGE_MIN = 0.8
REVEAL_MIN_ASSESSMENTS = 10               # ~a few seconds of sustained eligibility (at the 0.33 s hop)

CONFIG_SCHEMA_VERSION = 1
RAW_SCHEMA_VERSION = 1
QUALITY_SCHEMA_VERSION = 1


def _mad(xs):
    if len(xs) < 2:
        return 0.0
    med = statistics.median(xs)
    return statistics.median([abs(x - med) for x in xs])


def _goertzel_mag(samples, fs, freq):
    """Single-bin Goertzel magnitude at ``freq`` — a cheap line-noise probe (no full FFT)."""
    n = len(samples)
    if n < 8 or fs <= 0:
        return 0.0
    k = int(0.5 + (n * freq) / fs)
    w = (2.0 * math.pi / n) * k
    coeff = 2.0 * math.cos(w)
    s_prev = s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return math.sqrt(max(0.0, power)) / n


class _ChannelQuality:
    """Rolling per-channel quality from RAW ADC counts (never display-filtered). Robust stats.

    Window: ~QUALITY_WINDOW_SEC of raw samples. flatline/clip/step/robust-amplitude/drift/
    line-noise are all computed over this accumulated window; usablePct is the fraction of
    recent windows that were fully clean."""

    def __init__(self, fs):
        self.fs = fs
        self.buf = deque(maxlen=int(QUALITY_WINDOW_SEC * fs) + 16)
        self._clean_hist = deque(maxlen=64)

    def add(self, samples):
        for x in samples:
            self.buf.append(float(x))

    def assess(self, present):
        if not present:
            self._clean_hist.append(False)
            return {"present": False, "eligible": False, "reasons": ["absent"], "inputDomain": "raw_adc_counts"}
        xs = list(self.buf)
        if len(xs) < max(16, int(0.25 * self.fs)):
            self._clean_hist.append(False)
            return {"present": True, "eligible": False, "reasons": ["filling"], "inputDomain": "raw_adc_counts"}

        med = statistics.median(xs)
        amp_counts = 1.4826 * _mad(xs)
        flatline = amp_counts < FLATLINE_MAD_COUNTS
        clip_frac = sum(1 for x in xs
                        if x >= ADC_MAX - CLIP_MARGIN_COUNTS or x <= ADC_MIN + CLIP_MARGIN_COUNTS) / len(xs)
        clipping = clip_frac > CLIP_FRACTION_BAD

        diffs = [abs(xs[i] - xs[i - 1]) for i in range(1, len(xs))]
        deriv_mad = _mad(diffs) if diffs else 0.0
        step = bool(diffs) and max(diffs) > STEP_MAD_MULT * max(deriv_mad, 1.0)

        w = max(2, int(0.5 * self.fs))
        means = [sum(xs[i:i + w]) / w for i in range(0, len(xs) - w, w)]
        drift = bool(means) and (max(means) - min(means)) > DRIFT_RANGE_MULT * max(amp_counts, 1.0)

        rms = math.sqrt(sum((x - med) ** 2 for x in xs) / len(xs)) or 1e-9
        line_ratio = max(_goertzel_mag(xs, self.fs, 50.0), _goertzel_mag(xs, self.fs, 60.0)) / rms
        line = line_ratio > LINE_RATIO_BAD

        reasons = []
        if flatline: reasons.append("flatline")
        if clipping: reasons.append("clipping")
        if step: reasons.append("electrode_step")
        if drift: reasons.append("drift")
        if line: reasons.append("line_noise")

        clean = not reasons
        self._clean_hist.append(clean)
        usable_pct = (sum(1 for c in self._clean_hist if c) / len(self._clean_hist)) if self._clean_hist else 0.0
        eligible = clean and usable_pct >= USABLE_MIN_FRACTION

        return {
            "present": True, "eligible": bool(eligible),
            "inputDomain": "raw_adc_counts",       # quality is NOT computed on display-filtered data
            "windowSec": QUALITY_WINDOW_SEC, "windowSamples": len(xs),
            "flatline": bool(flatline), "clipping": bool(clipping), "clipFraction": round(clip_frac, 4),
            "electrodeStep": bool(step), "drift": bool(drift),
            "lineNoise": bool(line), "lineRatio": round(line_ratio, 3),
            "robustAmplitudeCounts": round(amp_counts, 1),   # COUNTS, never µV
            "usablePct": round(usable_pct, 3), "reasons": reasons,
        }


class EegStream:
    """Per-session raw transport + quality. One instance per streaming session (real or sim)."""

    def __init__(self, tx, log, simulation, expected_rate_hz=250, session_key=None, raw_dir=None):
        self.tx = tx
        self.log = log
        self.simulation = bool(simulation)
        self.expected_rate = expected_rate_hz
        # the reading segment on this stream (begin_reading / end_reading), for
        # the per-session raw recorder's phase tags
        self._phase = "preflight"
        self._reading_t0_ms = None
        # Real mode must NOT reach the final "clear" state until thresholds are validated on
        # real recordings. Only deterministic simulation (known-clean) may — or an explicit
        # override once validation lands.
        self.clear_state_enabled = self.simulation or os.environ.get("FOCUSROOM_CLEAR_STATE") == "1"
        self.seq = 0
        self.local_index = 0                 # LOCAL continuity index (no device counter exists)
        self.total_samples = 0
        self.callback_count = 0
        self._t0 = None
        self._last_recv = None
        self._cadence_hz = None              # EMA of callbacks/sec
        self._mean_batch = None              # EMA of samples/callback
        self._sdk_rate = None                # raw.sample_rate reported by the SDK (unverified)
        self._q = {}
        self._selected = {"left": None, "right": None}
        self._sel_reason = {"left": "none", "right": "none"}
        self._sel_switch_at = {"left": None, "right": None}   # local index of last switch
        self._ineligible_run = {}            # per label: consecutive ineligible assessments
        self._eligible_run = {}
        self._clear_since = None
        self._config_sent = False
        self._last_quality_emit = 0.0
        # continuity bookkeeping for the connection report: what the device
        # counter said (when a source supplies it) and what timing inferred
        self._device_continuity = None       # None until the first ingest says
        self._holes = {"left": [], "right": []}      # [{sec, samples, uncountable, kind}] (bounded)
        self._holes_dropped = 0
        self._timing_gaps = []               # [est_missing] inferred gaps (bounded)
        self._timing_gaps_dropped = 0
        self._missing_by_side = {"left": 0, "right": 0}
        self._dupes_by_side = {"left": 0, "right": 0}
        self._replays_by_side = {"left": 0, "right": 0}
        # eligibility coverage history (correction 1): recent analysis-eligible verdicts,
        # so 'reveal-eligible' can require sustained usable data, not one clean window.
        self._analysis_hist = deque(maxlen=64)
        # local validation recorder (correction / section 4): OFF unless FOCUSROOM_VALIDATION=1.
        # Records raw ADC-count batches + config + quality + metadata + staff annotations to
        # LOCAL engineering files only. Never uploads; never changes production retention.
        self._recorder = None
        try:
            from validation_recorder import enabled as _valid_enabled, ValidationRecorder
            if _valid_enabled():
                self._recorder = ValidationRecorder("sim" if self.simulation else "real", self.simulation, log)
        except Exception as e:  # a recorder problem must never break streaming
            self.log(f"validation recorder unavailable: {e}")
            self._recorder = None
        # THE PER-SESSION RAW RECORDER (plan 2026-09-06 task 7, decision 6): ON by
        # default, real and sim, whenever the app opened this stream for a guest
        # session (a session key came with start_session). It announces itself
        # over the wire as eeg/raw-capture-v1 at open and at close, and says
        # 'disabled' with the reason when it cannot record, so the archive never
        # has to guess whether raw exists. A manual test stream (no key) is not
        # recorded here.
        self._session_rec = None
        self.raw_capture = None      # the last eeg/raw-capture-v1 payload sent
        if session_key:
            self._open_session_recorder(session_key, raw_dir)

    def _open_session_recorder(self, session_key, raw_dir):
        try:
            import session_recorder as _sr
        except Exception as e:  # noqa: BLE001
            self._send_capture({"type": "eeg/raw-capture-v1", "status": "failed", "sessionKey": str(session_key),
                                "why": f"recorder module unavailable: {e}", "simulation": self.simulation})
            return
        if not _sr.enabled():
            self._send_capture({"type": _sr.CAPTURE_TYPE, "status": "disabled", "sessionKey": str(session_key),
                                "why": "switched off by FOCUSROOM_RAW_RECORD=0", "simulation": self.simulation})
            return
        if not (raw_dir or os.environ.get("FOCUSROOM_RAW_DIR")):
            self._send_capture({"type": _sr.CAPTURE_TYPE, "status": "disabled", "sessionKey": str(session_key),
                                "why": "no raw directory (FOCUSROOM_RAW_DIR unset)", "simulation": self.simulation})
            return
        try:
            self._session_rec = _sr.SessionRawRecorder(session_key, self.simulation, self.log, base_dir=raw_dir)
            if self._phase != "preflight":
                self._session_rec.set_phase(self._phase, self._reading_t0_ms)
            self._send_capture(self._session_rec.info("open"))
        except Exception as e:  # noqa: BLE001 — never let the recorder break streaming
            self.log(f"raw recorder unavailable: {e}")
            self._session_rec = None
            self._send_capture({"type": _sr.CAPTURE_TYPE, "status": "failed", "sessionKey": str(session_key),
                                "why": str(e), "simulation": self.simulation})

    def _send_capture(self, payload):
        self.raw_capture = payload
        try:
            self.tx.send_raw(dict(payload))
        except Exception as e:  # noqa: BLE001
            self.log(f"raw capture message failed: {e}")

    # the reading segment (begin_reading -> 'reading', end_reading -> 'post-reading')
    def set_phase(self, phase, reading_t0_ms=None):
        self._phase = str(phase)
        if reading_t0_ms is not None:
            self._reading_t0_ms = reading_t0_ms
        if self._session_rec:
            try:
                self._session_rec.set_phase(self._phase, self._reading_t0_ms)
            except Exception as e:  # noqa: BLE001
                self.log(f"raw recorder phase error: {e}")

    # emit a message to the transport AND the recorders (the per-session raw
    # recorder by default; the validation recorder when FOCUSROOM_VALIDATION=1).
    def _emit(self, msg):
        self.tx.send_raw(msg)
        if self._session_rec:
            try:
                self._session_rec.record(msg)
            except Exception as e:  # noqa: BLE001
                self.log(f"raw record error: {e}")
        if self._recorder:
            try:
                self._recorder.record(msg)
            except Exception as e:
                self.log(f"validation record error: {e}")

    # staff event annotation for the captures (blink/swallow/L-out/… — NOT a classifier).
    def annotate(self, kind, t=None, note=None):
        if self._session_rec:
            try:
                self._session_rec.annotate(kind, t, note)
            except Exception as e:  # noqa: BLE001
                self.log(f"raw annotate error: {e}")
        if self._recorder:
            try:
                self._recorder.annotate(kind, t, note)
            except Exception as e:
                self.log(f"validation annotate error: {e}")

    # close the captures cleanly (stop_session / disconnect / shutdown); the
    # per-session recorder gzips its file and reports 'closed' over the wire.
    def close(self, reason="stop"):
        if self._session_rec:
            try:
                out = self._session_rec.close(reason)
                if out:
                    self._send_capture(out)
            except Exception as e:  # noqa: BLE001
                self.log(f"raw recorder close error: {e}")
            self._session_rec = None
        if self._recorder:
            try:
                self._recorder.close(reason)
            except Exception as e:
                self.log(f"validation close error: {e}")
            self._recorder = None

    # ---- config (emit once per stream) ----
    def emit_config(self, labels, sdk_rate=None, device_continuity=False):
        if self._config_sent:
            return
        self._config_sent = True
        self._sdk_rate = sdk_rate
        device_continuity = bool(device_continuity)
        self._emit({
            "type": "eeg/config-v1", "schemaVersion": CONFIG_SCHEMA_VERSION,
            "physicalElectrodeCount": 8, "physicalElectrodesPerEar": 4,
            "sensingElectrodesPerEar": 2, "referenceElectrodesPerEar": 2,
            "transmittedEegChannelCount": 4, "channelsPerEar": 2,
            "channelLabels": ["Left-A", "Left-B", "Right-A", "Right-B"],
            "channelMappingStatus": "provisional",
            # sample-rate: hardware value UNVERIFIED; SDK-reported is a config claim, not a
            # measurement; device-level measurement is unavailable (no counter/timestamps).
            "expectedHardwareSampleRateHz": self.expected_rate,
            "sdkReportedSampleRateHz": sdk_rate,
            "deviceMeasuredSampleRateHz": None,
            "deviceSampleRateMeasurementAvailable": False,
            "sampleRateTimingMethod": "callback_arrival_throughput_only",
            "sampleRateConfidence": "unverified",
            "units": "adc_counts_unverified_sdk_units", "calibrationStatus": "unverified",
            "qualityThresholdStatus": "provisional", "clearStateEnabled": self.clear_state_enabled,
            "deviceContinuityAvailable": device_continuity,
            "continuityMethod": ("device_sequence_counter" if device_continuity
                                 else "callback_timing_inference"),
            "engineeringNote": (
                ("Device-level sample continuity comes from the firmware's one-byte packet "
                 "sequence counter (holes sized by the counter, or marked uncountable past "
                 "a 1 s wall gap). There is no per-sample timestamp; the physical ADC sample "
                 "rate is UNVERIFIED (callback-arrival timing measures ingest throughput only).")
                if device_continuity else
                ("Device-level sample continuity is UNVERIFIED on this source (no sequence "
                 "counter was supplied); gaps are inferred from callback timing. The physical "
                 "ADC sample rate is UNVERIFIED (callback-arrival timing measures ingest "
                 "throughput only).")),
            "simulation": self.simulation,
        })

    # ---- per-batch ingest ----
    def ingest(self, channels, labels, now_monotonic=None, sdk_rate=None, continuity=None):
        """`continuity` (optional): {"left": chunk, "right": chunk} where chunk is the
        per-device block connection.read_data produced for THIS batch ({n, firstAbsIdx,
        lastAbsIdx, holes:[{pos, nMissing, uncountable, wallGapSec, kind}], dupes, replays,
        refusals, available}); None for a side that sent nothing. Channels may differ in
        length (each device is read on its own); a channel's samples are carried at its
        own length, never truncated to the shortest."""
        if now_monotonic is None:
            now_monotonic = time.monotonic()
        if self._t0 is None:
            self._t0 = now_monotonic
        if self._device_continuity is None:
            # the first batch says what this stream can know; a source that
            # supplies the counter's verdict does so from its first chunk
            self._device_continuity = continuity is not None
        self.emit_config(labels, sdk_rate if sdk_rate is not None else self._sdk_rate,
                         device_continuity=self._device_continuity)

        lens = [len(c) for c in channels]
        n = max(lens, default=0)
        if n == 0:
            return
        for lab in ["Left-A", "Left-B", "Right-A", "Right-B"]:
            self._q.setdefault(lab, _ChannelQuality(self.expected_rate))
            self._ineligible_run.setdefault(lab, 0)
            self._eligible_run.setdefault(lab, 0)

        present = {lab: (lab in labels) for lab in ["Left-A", "Left-B", "Right-A", "Right-B"]}
        for lab, col in zip(labels, channels):
            self._q[lab].add(col)

        # ---- continuity: the DEVICE COUNTER when supplied, else timing inference ----
        sdk_gap = False
        est_missing = 0
        out_of_order = False
        if self._last_recv is not None:
            dt = now_monotonic - self._last_recv
            if dt < 0:
                out_of_order = True
            else:
                rate = self._cadence_and_throughput_rate()
                expected_samples = dt * rate
                missing = expected_samples - n
                if missing > GAP_TOLERANCE_SAMPLES:
                    sdk_gap = True
                    est_missing = int(round(missing))
                    if len(self._timing_gaps) < 5000:
                        self._timing_gaps.append(est_missing)
                    else:
                        self._timing_gaps_dropped += 1
            # cadence + mean-batch EMAs
            inst_cad = 1.0 / max(1e-6, now_monotonic - self._last_recv)
            self._cadence_hz = inst_cad if self._cadence_hz is None else 0.85 * self._cadence_hz + 0.15 * inst_cad
        holes_msg = None
        device_missing = 0
        counters = None
        if continuity is not None:
            holes_msg = {}
            counters = {}
            for side in ("left", "right"):
                c = continuity.get(side) if isinstance(continuity, dict) else None
                if not c:
                    continue
                side_missing = 0
                out_h = []
                for h in c.get("holes") or []:
                    nm = h.get("nMissing")
                    unc = bool(h.get("uncountable"))
                    sec = (float(nm) / self.expected_rate) if (nm is not None and not unc) \
                        else (float(h["wallGapSec"]) if h.get("wallGapSec") is not None else None)
                    rec = {"pos": h.get("pos", 0), "nMissing": nm, "uncountable": unc,
                           "wallGapSec": h.get("wallGapSec"), "kind": h.get("kind") or "lost_seq"}
                    out_h.append(rec)
                    if nm is not None and not unc:
                        side_missing += int(nm)
                    if len(self._holes[side]) < 5000:
                        self._holes[side].append({"sec": sec, "samples": nm, "uncountable": unc,
                                                  "kind": rec["kind"], "atSec": round(now_monotonic - self._t0, 3)})
                    else:
                        self._holes_dropped += 1
                self._missing_by_side[side] += side_missing
                self._dupes_by_side[side] += int(c.get("dupes") or 0)
                self._replays_by_side[side] += int(c.get("replays") or 0)
                device_missing = max(device_missing, side_missing)
                holes_msg[side] = out_h
                counters[side] = {"firstAbsIdx": c.get("firstAbsIdx"), "lastAbsIdx": c.get("lastAbsIdx"),
                                  "n": c.get("n"), "dupes": c.get("dupes"), "replays": c.get("replays"),
                                  "refusals": c.get("refusals")}
            # the index steps over the counted loss (the larger side): a hole is
            # never closed, and an uncountable one is carried by its wall gap
            self.local_index += device_missing
        elif self._last_recv is not None and not out_of_order:
            self.local_index += max(0, est_missing)   # advance across the gap, never close it
        self._mean_batch = float(n) if self._mean_batch is None else 0.9 * self._mean_batch + 0.1 * n
        self._last_recv = now_monotonic
        self.callback_count += 1
        self.total_samples += n

        first_index = self.local_index
        self.local_index += n
        last_index = self.local_index - 1
        self.seq += 1

        by_label = {lab: col for lab, col in zip(labels, channels)}
        samples = [[round(v, 1) for v in by_label.get(lab, [])] if present[lab] else None
                   for lab in ["Left-A", "Left-B", "Right-A", "Right-B"]]

        dev_avail = continuity is not None
        self._emit({
            "type": "eeg/raw-v1", "schemaVersion": RAW_SCHEMA_VERSION,
            "sequenceNumber": self.seq, "firstSampleIndex": first_index,
            "lastSampleIndex": last_index, "sampleCount": n,
            "sampleCounts": lens,                # per channel, in channelLabels order (may differ)
            "expectedHardwareSampleRateHz": self.expected_rate,
            "sdkReportedSampleRateHz": self._sdk_rate,
            "ingestThroughputSamplesPerSecond": (round(self._throughput(), 1) or None),
            "sourceTimestamp": None,             # no per-sample device time exists
            "monotonicReceiveTimestamp": round(now_monotonic - self._t0, 6),
            "channelLabels": ["Left-A", "Left-B", "Right-A", "Right-B"],
            "samples": samples,                  # ADC counts; null channel = absent ear
            "continuity": {
                "deviceContinuityAvailable": dev_avail,
                # the firmware counter, unwrapped per device (first index of this chunk)
                "deviceSampleCounter": ({k: v["firstAbsIdx"] for k, v in counters.items()}
                                        if counters else None),
                "devicePacketSequence": None,    # the raw byte is not surfaced, its unwrap is
                "deviceHoles": holes_msg,        # per side: [{pos, nMissing, uncountable, wallGapSec, kind}]
                "deviceCounters": counters,      # per side: dupes / replays / refusals in this chunk
                "sdkCallbackGapEstimate": sdk_gap,
                "estimatedMissingSamples": device_missing if dev_avail else est_missing,
                "timingEstimatedMissingSamples": est_missing,   # the inference, kept for comparison
                "browserTransportSequence": self.seq,   # the browser re-checks dup/reorder
                "browserTransportDuplicateDetected": False,
                "browserTransportOutOfOrderDetected": out_of_order,
                "continuityMethod": "device_sequence_counter" if dev_avail else "callback_timing_inference",
                "continuityConfidence": "high" if dev_avail else "low",
            },
            "simulation": self.simulation,
        })

        if (now_monotonic - self._last_quality_emit) >= QUALITY_HOP_SEC:
            self._last_quality_emit = now_monotonic
            self._emit(self._quality_message(present, now_monotonic))

    # ---- the connection report's continuity block ----
    def continuity_summary(self):
        """What this stream can say about gaps, for eeg/connection-report-v1:
        the device counter's holes when the source supplied them, else the
        timing inference. Durations in seconds (an uncountable hole counts its
        measured wall gap). Nulls, never zeros, before the first batch."""
        fs = float(self.expected_rate)
        if self._device_continuity is None:
            return {"source": None, "count": None, "longestSec": None, "totalSec": None,
                    "missingSamples": None, "uncountable": None, "bySide": None}
        if self._device_continuity:
            # A per-device EPOCH BREAK (kind 'reconnect': the bud's counter
            # restarted on a (re)connect) is not a gap: it carries no
            # seconds, the outage ledger owns the outage's measured duration,
            # and counting it here made a clean session with one connect per
            # side report 'gaps 2 (longest None)'. They are counted apart
            # (epochBreaks) and never turn longestSec/totalSec into None.
            by_side = {}
            secs = []
            unc = 0
            count = 0
            breaks = 0
            for side in ("left", "right"):
                hs = self._holes[side]
                gaps = [h for h in hs if h.get("kind") != "reconnect"]
                side_breaks = len(hs) - len(gaps)
                s_secs = [h["sec"] for h in gaps if h["sec"] is not None]
                by_side[side] = {
                    "count": len(gaps),
                    "longestSec": round(max(s_secs), 3) if s_secs else (0.0 if not gaps else None),
                    "totalSec": round(sum(s_secs), 3) if s_secs else (0.0 if not gaps else None),
                    "missingSamples": self._missing_by_side[side],
                    "uncountable": sum(1 for h in gaps if h["uncountable"]),
                    "epochBreaks": side_breaks,
                    "dupes": self._dupes_by_side[side], "replays": self._replays_by_side[side],
                    "holes": [dict(h) for h in hs[:200]],
                }
                secs.extend(s_secs)
                unc += by_side[side]["uncountable"]
                count += len(gaps)
                breaks += side_breaks
            return {
                "source": "device_counter", "count": count,
                # the max / sum over the SIZED holes; None only when every hole
                # is unsized (an uncountable hole still carries its wall gap)
                "longestSec": round(max(secs), 3) if secs else (0.0 if count == 0 else None),
                "totalSec": round(sum(secs), 3) if secs else (0.0 if count == 0 else None),
                "missingSamples": max(self._missing_by_side.values()),
                "uncountable": unc, "epochBreaks": breaks, "bySide": by_side,
                "truncated": self._holes_dropped,
            }
        gaps = self._timing_gaps
        return {
            "source": "callback_timing", "count": len(gaps),
            "longestSec": round(max(gaps) / fs, 3) if gaps else 0.0,
            "totalSec": round(sum(gaps) / fs, 3) if gaps else 0.0,
            "missingSamples": int(sum(gaps)), "uncountable": None, "bySide": None,
            "truncated": self._timing_gaps_dropped,
        }

    # ---- throughput / cadence ----
    def _throughput(self):
        if self._t0 is None or self._last_recv is None:
            return 0.0
        el = (self._last_recv or self._t0) - self._t0
        # a sub-0.1 s window is not a measurable throughput — reporting total/tiny gives a
        # nonsense rate (millions/s on the first emit). Omit it until the window is real.
        if el < 0.1:
            return 0.0
        return self.total_samples / el

    def _cadence_and_throughput_rate(self):
        # for the gap estimate use the measured per-channel ingest throughput when we have
        # enough history, else fall back to the expected hardware rate (documented).
        thr = self._throughput()
        return thr if self.callback_count > 10 and thr > 0 else self.expected_rate

    # ---- channel selection with hysteresis (item 7) ----
    def _select(self, ear, a, b, chans):
        cur = self._selected[ear]
        ca, cb = chans[a], chans[b]

        # track eligibility runs for the hysteresis
        for lab, q in ((a, ca), (b, cb)):
            if q["eligible"]:
                self._eligible_run[lab] += 1; self._ineligible_run[lab] = 0
            else:
                self._ineligible_run[lab] += 1; self._eligible_run[lab] = 0

        # keep the current pick unless it has been INELIGIBLE for SWITCH_HOLD assessments —
        # so a single noisy window can't flap the display between A and B.
        if cur and self._ineligible_run.get(cur, 99) < SWITCH_HOLD and chans[cur]["eligible"] is not False:
            if chans[cur]["eligible"]:
                self._sel_reason[ear] = "held (eligible)"
                return cur, False
        # need a replacement: pick a channel that has been eligible for SWITCH_HOLD assessments
        cand = None
        if self._eligible_run.get(a, 0) >= SWITCH_HOLD:
            cand = a
        elif self._eligible_run.get(b, 0) >= SWITCH_HOLD:
            cand = b
        elif ca["eligible"]:
            cand = a
        elif cb["eligible"]:
            cand = b
        switched = cand is not None and cand != cur
        if cand is None:
            self._sel_reason[ear] = "no usable channel"
            self._selected[ear] = None
            return None, cur is not None
        if switched:
            self._sel_switch_at[ear] = self.local_index
            self._sel_reason[ear] = ("initial pick" if cur is None else "switched: " + cur + " became unusable")
        self._selected[ear] = cand
        return cand, switched

    # ---- eligibility state machine (correction 1) ----------------------------
    # SEPARATES "packets are arriving" from "the signal can be analysed". Receiving
    # raw callbacks alone is NOT sufficient for the room to advance automatically:
    #   transportReady  — callbacks arriving + samples ingested (no sustained stall)
    #   displayEligible — >=1 channel present, so the consumer scope can draw honestly
    #   analysisEligible — one grossly-usable channel on EACH ear (not flatline, not
    #                      clipping), delivery sufficiently continuous, recent usable
    #                      fraction over the provisional minimum
    #   revealEligible  — analysis-eligible for a sufficient fraction of the recording
    #                     (signal-derived only; the orchestrator adds the event pre/post
    #                     coverage + staff-override gate before any guest claim)
    # All thresholds PROVISIONAL. staffOverride is applied by the app layer, not here.
    @staticmethod
    def _gross_fail(ch):
        # the two gross failures the spec calls out explicitly for analysis eligibility
        return ch is None or not ch.get("present") or ch.get("flatline") or ch.get("clipping")

    def _eligibility(self, chans, left, right, rate_mismatch):
        left_ch = chans.get(left) if left else None
        right_ch = chans.get(right) if right else None
        # a selected channel is "usable" only if present, quality-eligible, and free of
        # a gross failure (flatline / clipping). _select already only returns an
        # eligible channel or None, but re-check here so the rule stands on its own.
        left_usable = bool(left is not None and left_ch and left_ch.get("eligible")
                           and not self._gross_fail(left_ch))
        right_usable = bool(right is not None and right_ch and right_ch.get("eligible")
                            and not self._gross_fail(right_ch))

        transport_ready = bool(self.callback_count > 0 and self.total_samples > 0)
        display_eligible = any(chans[l].get("present") for l in chans)
        continuity_ok = not rate_mismatch                      # provisional delivery gate
        analysis_eligible = bool(left_usable and right_usable and continuity_ok)

        self._analysis_hist.append(analysis_eligible)
        n_assess = len(self._analysis_hist)
        coverage = (sum(1 for a in self._analysis_hist if a) / n_assess) if n_assess else 0.0
        reveal_eligible = bool(analysis_eligible
                               and n_assess >= REVEAL_MIN_ASSESSMENTS
                               and coverage >= REVEAL_COVERAGE_MIN)

        filling = any("filling" in (chans[l].get("reasons") or [])
                      for l in chans if chans[l].get("present"))

        reasons = []
        if not transport_ready:
            reasons.append("no_transport")
        elif filling and not analysis_eligible:
            reasons.append("filling_first_window")
        if transport_ready and not left_usable:
            reasons.append("left_ear_not_usable")
        if transport_ready and not right_usable:
            reasons.append("right_ear_not_usable")
        if transport_ready and not continuity_ok:
            reasons.append("delivery_discontinuous")
        if analysis_eligible and not reveal_eligible:
            if n_assess < REVEAL_MIN_ASSESSMENTS:
                reasons.append("insufficient_recording_for_reveal(%d/%d)" % (n_assess, REVEAL_MIN_ASSESSMENTS))
            elif coverage < REVEAL_COVERAGE_MIN:
                reasons.append("insufficient_usable_coverage_for_reveal(%.2f)" % coverage)

        if not transport_ready:
            estatus = "checking"
        elif analysis_eligible:
            estatus = "provisional-pass"
        elif filling:
            estatus = "checking"
        elif left_usable or right_usable:
            estatus = "limited"
        else:
            estatus = "failed"

        return {
            "transportReady": transport_ready,
            "displayEligible": display_eligible,
            "analysisEligible": analysis_eligible,
            "revealEligible": reveal_eligible,
            "eligibilityStatus": estatus,
            "qualityThresholdStatus": "provisional",
            "staffOverride": False,          # signal layer; the app overlays a real override
            "reasons": reasons,
            "ears": {
                "left": {"selected": left, "usable": left_usable},
                "right": {"selected": right, "usable": right_usable},
            },
            "usableCoverageFraction": round(coverage, 3),
            "note": ("Signal-derived eligibility. Thresholds PROVISIONAL (unvalidated on "
                     "real Zone recordings). Staff override + event pre/post coverage are "
                     "applied by the app layer, not here."),
        }

    def _quality_message(self, present, now_monotonic):
        chans = {lab: self._q[lab].assess(present[lab])
                 for lab in ["Left-A", "Left-B", "Right-A", "Right-B"]}
        left, sw_l = self._select("left", "Left-A", "Left-B", chans)
        right, sw_r = self._select("right", "Right-A", "Right-B", chans)

        left_ok, right_ok = left is not None, right is not None
        reasons = []
        thr = self._throughput()
        rate_mismatch = (self.callback_count > 10 and thr > 0
                         and abs(thr - self.expected_rate) / self.expected_rate > RATE_MISMATCH_TOL)
        if rate_mismatch:
            reasons.append("ingest_throughput_mismatch(~%.0f/s vs expected %d Hz)" % (thr, self.expected_rate))

        # status. Real mode is CAPPED below 'clear' until thresholds are validated
        # (clearStateEnabled=false → the highest real-mode state is 'received').
        if left_ok and right_ok:
            if self._clear_since is None:
                self._clear_since = now_monotonic
            sustained = (now_monotonic - self._clear_since) >= CLEAR_SUSTAIN_SEC
            if self.clear_state_enabled:
                status = "clear" if (sustained and not rate_mismatch) else "checking"
                if not sustained:
                    reasons.append("stabilising")
            else:
                status = "received"                 # both ears delivering; final "clear" gated off
                reasons.append("clear_state_disabled_pending_threshold_validation")
        else:
            self._clear_since = None
            if left_ok or right_ok:
                status = "limited"; reasons.append("one_ear_unusable")
            elif any(chans[l]["present"] for l in chans):
                status = "poor"; reasons.append("no_usable_channel")
            else:
                status = "receiving"; reasons.append("no_samples")

        def conf(lab):
            if lab is None:
                return "none"
            up = self._q[lab]._clean_hist if lab in self._q else []
            frac = (sum(1 for c in up if c) / len(up)) if up else 0.0
            return "high" if frac > 0.85 else "medium" if frac > 0.6 else "low"

        eligibility = self._eligibility(chans, left, right, rate_mismatch)

        return {
            "type": "eeg/quality-v1", "schemaVersion": QUALITY_SCHEMA_VERSION,
            "qualityThresholdStatus": "provisional", "clearStateEnabled": self.clear_state_enabled,
            "eligibility": eligibility,
            "connectionQuality": {
                "callbackCadenceHz": round(self._cadence_hz, 2) if self._cadence_hz else None,
                "meanSamplesPerCallback": round(self._mean_batch, 1) if self._mean_batch else None,
                "ingestThroughputSamplesPerSecond": (round(thr, 1) or None),
                "throughputMeasurementWindowSeconds": round((self._last_recv - self._t0), 1) if self._t0 else 0,
                "expectedHardwareSampleRateHz": self.expected_rate,
                "sdkReportedSampleRateHz": self._sdk_rate,
                "deviceMeasuredSampleRateHz": None, "deviceSampleRateMeasurementAvailable": False,
                "sampleRateConfidence": "unverified",
            },
            "packetContinuity": {
                "deviceContinuityAvailable": bool(self._device_continuity),
                "continuityMethod": ("device_sequence_counter" if self._device_continuity
                                     else "callback_timing_inference"),
                "continuityConfidence": "high" if self._device_continuity else "low",
                "localIndex": self.local_index, "sequenceNumber": self.seq,
                "deviceMissingSamples": (dict(self._missing_by_side) if self._device_continuity else None),
            },
            "channels": chans,
            "selectedConsumerChannels": {
                "left": left, "right": right, "switched": {"left": sw_l, "right": sw_r},
                "reason": {"left": self._sel_reason["left"], "right": self._sel_reason["right"]},
                "confidence": {"left": conf(left), "right": conf(right)},
                "switchAtIndex": {"left": self._sel_switch_at["left"], "right": self._sel_switch_at["right"]},
            },
            "overallStatus": status, "reasons": reasons, "simulation": self.simulation,
        }
