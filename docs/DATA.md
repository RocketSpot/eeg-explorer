# Data and local API interface (schema 1)

A session identity is 32 lowercase hexadecimal UUID characters. Metadata contains source, nominal sample rate and calibration limitations, discovered channels, software/firmware information when available, acquisition provenance, pseudonymous participant, anonymous per-library computer identity, first sample estimates and independent host clock measurements. Never infer a value for absent firmware/calibration information.

SQLite tables:

| Table | Purpose |
|---|---|
| `meta` | JSON session metadata, current revision, recording/recovery state |
| `samples` | Original count value per channel; channel ordinal `idx`, estimated sample time `t`, original available unwrapped `device_index`, receipt wall/monotonic nanoseconds and batch identity |
| `batches` | Acquisition settings, continuity counters and holes, original import messages, source provenance |
| `events` | Connection and recording events, original notification bytes where exposed, keyboard/countdown markers, explicit gaps and diagnostics |
| `annotations` | Current human, protocol or model interval observations |
| `history` | Before/after annotation edits and undo history; split/merge is one operation |
| `results` | Saved analyses, model results, comparisons and versioned settings |

The sample timeline starts at the earliest actual sample in the first eligible received batch, using a receive-time estimate. Each independent ear's nominal sampling period and counter continuity determine subsequent sample positions. Reconnects and uncountable losses start a new receive-time estimate and remain gap events. A channel's `idx` counts available samples; it does not pretend missing samples were observed. `device_index` includes known sequence holes when available. Counter wrapping and duplicate/replay admission come from the pinned SDK. Original notifications retain evidence of rejected/undecodable input.

Annotations use half-open intervals `[start,end)` and preserve per-channel sample-index bounds along with sample timeline times. Keyboard boundaries use received acquisition evidence, never a delayed plot's playback cursor. Host/device acquisition latency is unknown. Cross-ear hardware synchronization is not established. Estimated times must not be used as a claim of precise physiological event latency.

Annotation fields include id, start/end/duration, label/id/color, scope (`left`, `right`, `both` or selected channel list), placement, activity, issue, notes, source (`manual`, `protocol`, `model`), reviewed, needs_review, and uncertain boundary information. Model training requires explicit reviewed human intervals and excludes gaps, incompatible scopes and transitions.

The loopback JSON API uses `GET /api/state`, `/api/sessions`, `/api/session?id=…`, `/api/samples?id=…&start=…&end=…&limit=…`, `/api/labels`, `/api/settings`, and `/api/export?id=…`. `POST /api/action` receives `{action,...}`. Mutations are serialized with acquisition as required. See `explorer/controller.py` for the version-0.1 action contract. Only same-origin JSON requests are accepted from browsers. There is no LAN control endpoint.

Portable ZIP files include `session.sqlite`, `metadata.json`, `annotations.json`, `labels.json`, `README.txt`, and `checksums.json`. Every data member must match its SHA-256 before import. A conflicting existing identity is never overwritten. Focus Room `eeg.raw*.ndjson[.gz]` imports preserve each raw message and make missing metadata explicit. Import separate stream segments separately. Summary-only guest session JSON cannot restore raw EEG and is intentionally not treated as raw data.

No SQL/API operation exposed by Explorer deletes source samples. Keep a full library backup as well as verified synchronized archives. Local deletion or corruption outside the application cannot be prevented by the data schema.
