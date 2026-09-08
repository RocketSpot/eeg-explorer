# Analysis and method interface

EEG Explorer includes an exploratory signal-only baseline. It does not establish contact state, measured impedance, clinical signal usability, or that every physical condition can be distinguished. All analyses are derived work; source SQLite samples remain intact.

## Process a selected interval

`explorer.analysis.analyze(store, request)` accepts:

```json
{
  "session_id": "32-character session ID",
  "start": 5,
  "end": 35,
  "channels": ["Left CH1"],
  "pipeline": {
    "schema": "eeg-explorer.method/1",
    "name": "1–40 Hz offline view",
    "version": "1.0.0",
    "mode": "offline",
    "inputs": {"sample_rate": 250, "units": "counts", "channels": ["Left CH1"]},
    "operations": [{"op": "bandpass", "low": 1, "high": 40, "order": 4}]
  }
}
```

The sample rate, channel names, and units above are interface examples, **not a hardware claim**. Inputs are validated against the selected session; no automatic count-to-microvolt conversion or resampling occurs. A selection is limited to 300 seconds and one million input values to bound work. It returns channel-aligned raw/processed arrays, real timestamps, flags, rejected samples, affected boundary estimates, spectra, amplitude/variance/flatline/change/drift/spectral features, and exact-time channel correlations when available. Independent ears are not hardware synchronized; exact recorded-time coincidence alone does not establish synchronized physiological acquisition.

Spectrum and feature summaries use the longest contiguous selected block, identified in the response. They never concatenate across dropouts. Missing intervals and explicit acquisition gap events (including unknown, zero-duration discontinuities) split processing state. No signal is synthesized across a gap. The spectrum is a demeaned, Hann-windowed single-segment periodogram; it is not a robust Welch estimate. Power fractions describe available sampled bands only. Clipping fraction is shown only when session metadata supplies documented `adc_rails`; otherwise it remains unavailable.

The `processed` trace uses `null` for explicitly rejected samples. `removed_component` is raw minus transformed signal and describes filter subtraction, **not evidence of successful artifact reconstruction**. A threshold flag is a signal issue annotation, not a placement label. The implementation does not reconstruct a signal after artifacts.

## Declarative methods and review

Only JSON operations in this table are accepted. Unknown fields and operations are rejected. There is no `eval`, module import, shell command, or arbitrary Python plugin execution endpoint. This constrained interface avoids exposing acquisition, credentials, or unrelated files to an untrusted method.

| Operation | Fields | Behavior |
| --- | --- | --- |
| `bandpass` | `low`, `high`, `order` 1–8 | Butterworth second-order sections; cutoffs strictly below Nyquist |
| `highpass`, `lowpass` | `hz`, `order` 1–8 | Butterworth second-order sections |
| `notch` | `hz`, `q` | Second-order notch; frequency strictly below Nyquist |
| `demean` | none | Subtract interval mean; offline only |
| `detrend` | none | Subtract linear trend; offline only |
| `detect` | `metric`, `threshold` | Flag samples using `absolute_amplitude`, `abrupt_change`, or `flat_difference` |
| `mask_flags` | none | Reject flagged samples; must be final operation |

Pipeline operations are limited to twelve. The optional `inputs` object specifies units, sample rate, and ordered channel layout. These requirements must match the recording. Threshold units follow the metric: amplitude/counts, adjacent-sample change/counts, or adjacent-sample flat difference/counts. A very small adjacent difference is not independently interpreted as bad contact.

`causal` filtering uses SciPy `sosfilt` initialized from the segment's first value. It needs no future samples, but IIR phase delay is frequency dependent and is not falsely reported as zero. A live integrator must preserve filter state between streaming blocks; this analysis API deliberately resets at each selection, gap, and feature window. `offline` uses `sosfiltfilt`, including future samples, and rejects segments shorter than its padding requirement. Offline processing affects both boundaries. Settling regions are conservative heuristics (three periods of the low cutoff; five notch decay constants); they are not guaranteed transient bounds. State resets and boundary metadata accompany every result.

Design references: [SciPy Butterworth filter documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.butter.html), [SciPy forward-backward SOS filtering](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.sosfiltfilt.html).

`candidate({description, sample_rate, threshold?})` produces a local deterministic, reviewable interruption-detector template. A requested notch is included when valid. The threshold is a placeholder requiring review in native units. No external AI provider is configured, and general natural-language algorithm synthesis is not implemented. Generated candidates do not activate themselves, change labels, or enter Focus Room. The UI allows reviewing/editing the JSON and explicitly running it on selected data.

## Train and evaluate

`train(store, request)` accepts `session_ids`, `features` (supported feature groups or exact names; omitted means all), optional `channels` and condition `labels`, `group_by` (`session` or `participant`), `window_seconds` (default 2), `transition_margin` (default .25 seconds), `task` (a descriptive task name), `seed` (default 42), and an optional pipeline. Separate tasks should be trained from explicitly selected reviewed labels rather than interpreting placement as signal quality.

Eligible observations are **manual, reviewed, named, not uncertain, and not marked needs-review**. Protocol labels remain excluded even when marked reviewed until a deliberate human annotation creates a manual observation. Unnamed observations and label `Unknown` are not ground truth for an in-ear/off-ear classifier. Explicit diagnostic flags on samples or annotations and overlapping conflicting manual labels are excluded. Numeric `boundary_uncertainty` expands the excluded margin at both annotation edges, in addition to the experiment transition margin. Unknown-duration data gaps exclude windows that span the boundary. Duplicate/overlapping accepted windows are not counted twice. Ear/channel scope must cover all selected channels; the pinned acquisition adapter supplies explicit Left/Right channel names.

Windows are non-overlapping and complete on each channel. The selected sessions must share sample rate, ordered channels, and units. No auxiliary optical, hardware lead-off, or impedance values enter the feature vector. Features are amplitude RMS, standard deviation, variance, peak-to-peak, median absolute deviation, exact flatline fraction, largest adjacent change, linear drift, delta/theta/alpha/beta power fractions, and 50/60 Hz line-band fraction. The configurable groups are `amplitude`, `variance`, `flatline`, `abrupt_changes`, `drift`, `spectral_power`, and `line_noise`. Feature selection is an explicit experiment setting; no test-set feature selection runs. These are per-channel features; cross-channel correlation is offered as descriptive analysis but is not part of this baseline classifier.

Filters run within each window; estimated settling samples are excluded from feature extraction. If too little remains, that window is rejected. For low-frequency filters, use longer windows. Masking any artifact sample excludes the model window rather than fabricating replacement values. Window output becomes available only at the end of the window plus processing time.

Only completed or recovered recordings can enter training. At least three independent groups are required. With participant separation, every recording needs a pseudonymous participant ID. By default a seeded, deterministic group permutation assigns approximately 60% training, 20% validation, and 20% test, with at least one group in each. For small datasets, a split can lack a class; the operation fails with an explanation instead of silently choosing a favorable split. An explicit group split may be supplied:

```json
{"split":{"train":["participant-a","participant-b"],"validation":["participant-c"],"test":["participant-d"]}}
```

Every included group must occur exactly once, and every split must contain every selected class. Standardization mean and scale and class centroids are fitted on training windows only. This nearest-centroid classifier uses standardized Euclidean distance. A distance rejection threshold is selected from validation-distance quantiles using utility `correct +1, incorrect -1, unknown 0`; ties favor the smaller threshold. Test labels never set normalization, centroids, or rejection thresholds. The final test is one held-out evaluation for this run; repeatedly choosing methods based on it would invalidate its role as a final test.

Results include per-class precision/recall/F1/support, a confusion matrix with explicit row/column semantics, unknown rate, accuracy, group and session counts, excluded observations, reviewed-label errors with exact waveform jump coordinates, and a review queue of errors/abstentions. The queue never changes labels. High-risk Focus Room confusions (table/fingertip → in-ear and in-ear movement → off-ear) are listed separately. Event-level detection delay remains `null` because a window classifier evaluation does not establish transition-detection delay. The window-availability interval is reported separately.

Models and evaluations preserve the exact reference source code and its SHA-256, NumPy/SciPy versions, source session revisions, preprocessing and thresholds, random seed-derived split assignments, task, features, and version. Each selected session receives its actual extracted feature-window table and held-out predictions as separate derived artifacts, plus the model result, so they are preserved in complete-session exports and Git backup. A copy is also saved under `models/<id>.json` in the durable library. A restored session's saved model result can be inspected in SQLite; exporting a saved model reconstructs the separate model index from session results when needed.

## Compare and export

`compare(store, {examples, pipeline, reference_pipeline})` processes the same selections through both named declarative pipelines and returns raw/processed signals, feature comparisons, and exact flag-disagreement counts. It does not claim to run Focus Room production decisions. The separate pinned Focus Room comparison adapter, when offered by the application, states its own source revision and supported decisions.

`export_model(store, {model_id, approved:true})` creates a versioned ZIP containing `package.json`, the auditable reference `analysis.py`, and an integration README. It preserves centroids, train-only normalization, thresholds, channel layout, sample rate, units, windows, pipeline, gap behavior, split provenance, and evaluation. The ZIP's SHA-256 is returned. The included public `infer_window(model, samples, fs, units, channels, gaps=None)` helper runs the exact same preprocessing, feature extraction, normalization, and rejection rule as training; incomplete or discontinuous windows return unknown. It is a window API, not a persistent streaming filter. Approval is for export/review; no package is installed into Focus Room. Exported Python source is provided for human integration review and is never loaded as a plugin. Maintain a production rollback path during any subsequent separately authorized integration.

## Verification boundary

Automated tests cover spectral features on known synthetic signals; causal prefix invariance; cutoff validation; gap/state resets; detection vs rejection; manual-label eligibility; overlap/diagnostic exclusion; session and participant group isolation; train-only normalization; abstention metrics; export integrity; and declarative-only candidates. These establish software behavior with synthetic data. They do not validate contact classification on Zone hardware or biological ground truth.
