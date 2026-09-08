# Focus Room comparison

`explorer.focus_compare.compare_focus(store, request)` runs a byte-identical,
SHA-256 pinned snapshot of Focus Room's `sidecar/eeg_stream.py`. No Focus Room
process, Bluetooth connection, filesystem store or source is opened by this path.
Its only optional recorder imports are absent, and no session key is supplied.

The request accepts `session_id`, `start`, `end`, `channels`, `pipeline`, and
`save` (default true). Select up to 300 seconds. The baseline always receives
available channels among Left-A, Left-B, Right-A and Right-B in original ADC
counts; the candidate pipeline is applied only by Explorer's analysis module.

This compares **signal quality**: flatline, clipping, steps, drift, mains,
provisional eligibility and selected consumer channels. It does not reproduce
Focus Room's separate spectral accepted-window decision or impedance-based worn
gate. Contact explicitly returns unavailable. There is no conversion of a
signal statistic to impedance or assumption that placement determines quality.

The replay groups recorded samples into nominal 50-sample polling intervals,
retaining independent ear arrays and explicit counter/time holes. It inserts no
samples, and does not cross-correlate or align the ears. Original Focus Room live
callback timing and connection state are not reproduced. Presence uses recent
recorded arrivals, so the returned connection-throughput diagnostic is a replay
quantity. Up to 30 seconds before the requested interval warms rolling history;
very old selection state can differ from an uninterrupted run.

Each returned `quality_rows` item preserves the original quality payload plus
the recording timeline time and window start. `flagged_intervals` identifies
assessment positions and their evidence window. `comparisons` reports the
candidate's flagged sample fraction over the same baseline window, alongside
Focus Room reasons and overlapping reviewed manual annotations. These are
inspection targets; detection flags and Focus Room eligibility have different
semantics, so disagreement is not an automatic correctness or contact score.

Only a derived comparison result is saved. Raw samples, annotations and active
production behavior remain intact. Model export separately requires explicit
approval and creates a versioned review package; it does not install anything
into Focus Room. Hardware verification remains outstanding.
