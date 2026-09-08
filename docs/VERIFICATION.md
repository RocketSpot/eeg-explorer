# Verification record — September 8, 2026

**Verified in software; real Zone radio and physiology remain unverified.**

## Automated checks

The final Python run passed **63 tests** across acquisition, binary packet integration, local API, recording/annotations, legacy and portable imports, processing/evaluation, Focus Room quality comparison and synchronization. JavaScript shortcut tests pass (**7 tests**). Python syntax, Electron entry-point syntax and UI JavaScript syntax checks pass.

Coverage includes:

- Signed 24-bit counts, 9/11-byte packets, frame splits across notifications, one-byte counter wrap, actual missing packets, duplicates and replay admission.
- Recording arming/stopping by receipt time despite delayed queues, no artificial empty start, independent ears, known and unknown gaps, reconnect epochs and notification preservation.
- One integrated fake binary stream preserved **20,000 channel samples and 1,000 original BLE-format notifications** through the real parser, Controller and SQLite. This does not use a radio.
- Annotation switching at the same boundary, countdown/key timing, cancellation, previous-window marking, history, atomic split/merge undo, and keeping recording independent.
- Crash recovery, non-recovering read access, exact portable save/reopen/import, corrupt archive detection, atomic failed-copy retry and legacy unknown timestamps/incomplete-import status.
- Causal processing without future dependence, reset at gaps, native units, validated configuration, separate detection/masking, group separation, train-only normalization, held-out errors/unknowns, inference parity, complete derived artifacts and restored-model export.
- Local-only unconfigured sync, privacy/writability checks, chunk integrity, durable retries/restart, conflicts, fresh downloaded-byte verification and restore. A **temporary local bare Git repository** was used for actual push/clone roundtrip; no GitHub dataset upload occurred.
- Browser API Host/Origin checks and path traversal rejection.

## Interactive simulated workflow

The browser test ran against an isolated `/tmp/eeg-explorer-verification-20260908` library. It:

1. Displayed explicitly simulated samples.
2. Started a recording titled “Simulated workflow verification” with participant `TEST-P001`.
3. Used keys **1**, **Space**, **2**, **P**, **Space** to create two conditions and pause the viewer. The saved sample count continued increasing while the viewer was paused.
4. Stopped after **25,400 channel samples**, edited an annotation and saved a human review action. These labels describe a UI exercise, not measured human placement.
5. Applied an offline bandpass and displayed real computed features and spectra.
6. Stopped and restarted the server, reopened the recording from the library, and verified waveform data and annotations persisted.
7. Exported the complete session and verified every ZIP member SHA-256. The retained verification export hash is `bb26008341890614455f30abece235677fcc7e56ba9184ed9b99d3407c462013`.

The local screenshot and ZIP are in `artifacts/verification/` (excluded from source Git). Browser page load, navigation, key controls, simulation badge, saved library and processing were inspected. No browser errors or framework error overlay were reported. No performance claim is made for arbitrarily long studies.

## Desktop check

`~/Desktop/EEG Explorer.app` launched the independent Electron/Python application successfully. The actual native window was inspected. Its default library is `~/Library/Application Support/Zone EEG Explorer`; it was left disconnected with no real acquisition or automatic upload started.

## Outstanding acceptance work and scope limits

- Validate actual scan/connect/reconnect and sustained recordings on the user's Zone earbuds. Confirm actual firmware, sample configuration, electrodes, gain/reference and analog excitation status with hardware evidence; counts and nominal timing remain explicitly uncalibrated until then.
- Configure an authorized private GitHub repository, then run and verify a real end-to-end upload/recovery. Local fake-Git testing is not proof of the user's repository credentials or destination.
- The candidate builder is a local declarative template generator. External AI synthesis and arbitrary executable plugin/model sandboxing are **not implemented**; unsupported code is rejected.
- Focus Room comparison reuses its pinned signal-quality stream logic. It is **not** an impedance contact classifier comparison or the complete production spectral pipeline. Exported models require explicit integration/promotion work and a production rollback path.
- Optional spectrograms, a general model-plugin runtime, automatic conflict reconciliation and a broad human dataset are not supplied by this release. Spectra, features, grouped baseline evaluation and error review are available.
