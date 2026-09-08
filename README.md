# Zone EEG Explorer

A separate desktop application for receiving Zone earbud EEG, keeping the original available counts, and collecting labeled experiments. Focus Room source and production behavior are not modified.

## Open on this Mac

Double-click **EEG Explorer.app** on the Desktop. The source and independent Python/Electron installation are in `~/eeg-explorer`.

1. Open **Device connection**, scan, and select each earbud for the correct ear. Close any application that owns the earbuds first. Bluetooth connection is initiated only by your action.
2. Verify the source badge and arriving samples. The hardware source is labeled separately from **SIMULATION**.
3. Set a pseudonymous participant and session title; start recording. The timeline begins with the first actual received sample after arming.
4. Choose a label with **1–9**. Press **Space** to begin/end a section. Select another favorite during a section to switch at the same boundary. Unnamed sections are supported. Recording continues throughout.
5. **M** adds a marker, **U** marks the previous seconds, **P** pauses only the viewer, **Esc** cancels a pending/current section, and **Cmd/Ctrl+Z** undoes annotations. **?** opens editable shortcuts. Shortcuts are inactive while typing and ignore held-key repetition.
6. Stop recording, review labels and their boundaries, then mark appropriate annotations reviewed. Browse/reopen sessions through **Session library**. Use **Export** for a complete portable ZIP.
7. Assemble processing operations or compare intervals. Training requires reviewed human labels from at least three independent session or participant groups. Reviewing a scheduled protocol section requires explicit confirmation that the action was performed; its protocol origin and earlier version remain in history. A simulation result does not validate contact detection on a person.

Real connection deliberately does not run Focus Room's fit, battery or quality gates before recording. It requests the documented passive acquisition command sequence, but cannot verify disabled excitation electrically or read back the analog configuration. No noise score is called impedance and no arbitrary counts-to-microvolts conversion is used.

## Install on another machine

Python 3.12+, Node.js/npm, and the platform's Bluetooth support are required. From the checkout run:

```sh
sh tools/setup.sh
npm start
```

On macOS, `./.venv/bin/python tools/install-mac-launcher.py` adds the Desktop launcher. Windows users can create `.venv` with `python -m venv .venv`, install `requirements.txt` with `.venv\Scripts\python -m pip install -r requirements.txt`, then run `npm install` and `npm start`. Real BLE behavior has not yet been validated on Windows in Explorer.

Run without Electron using `.venv/bin/python -m explorer.server`; open `http://127.0.0.1:8766`. The server binds only to loopback and validates Host/Origin. `--simulate` starts explicitly synthetic signals; never use it as evidence of radio validation. `--data-dir /absolute/path` selects an isolated development library. `EEG_EXPLORER_PYTHON` can select a different interpreter for the desktop launcher.

## Durable local data

The normal macOS library is `~/Library/Application Support/Zone EEG Explorer`, outside this checkout and temporary build folders. Windows uses `%LOCALAPPDATA%\Zone EEG Explorer`; Linux uses `$XDG_DATA_HOME/zone-eeg-explorer` or `~/.local/share/zone-eeg-explorer`. `EEG_EXPLORER_DATA_DIR` overrides it. Keep the entire library when moving or updating the app.

Each session has an independently indexed SQLite database in `sessions/<id>/session.sqlite`. Acquisition commits incrementally; SQLite WAL recovery retains committed samples after interruption. A recovered session is marked as such. Raw sample values are immutable through the application API. Annotation changes keep their previous versions. Exports use SQLite online snapshots, SHA-256 checksums and ZIP, and include source samples, batch provenance, original notification events where available, gap ledger, labels, history, device metadata, and saved derived work. Import refuses an existing session identity rather than silently overwriting a different revision.

## Research tools and limits

- Live and reopened waveforms with fixed/automatic scale, channel controls, pan/zoom, sample inspection and timelines. Large views use min/max envelopes; exports and analysis keep original available values.
- Human placement, activity and observed issue attributes are separate from protocol instructions and algorithm flags. Unlabeled data has no inferred class.
- Guided experiments with durations, transitions, manual advancement, repetitions, seeded randomized/counterbalanced ordering and unreviewed protocol annotations.
- Named declarative pipelines with sample-rate validation, raw/processed/removed components, spectral and feature comparison, flagging and masking. Offline operations are marked. Signal filtering is not evidence of better contact detection.
- Transparent signal-only nearest-centroid classification with train-only normalization, independent grouped train/validation/test splits, confusion and per-class metrics, unknown output, error windows and reproducible versioned settings.
- Pinned Focus Room **signal-quality** comparison; this does not reproduce hardware impedance measurements or claim to compare an unavailable passive production contact classifier.
- A local **candidate template builder**, with explicit review and approval. An external AI service and arbitrary Python/model code execution are not implemented. Imported algorithms use the constrained declarative interface; unsupported code is rejected. This avoids pretending an unrestricted subprocess is a secure plugin sandbox.
- Versioned approved model export is a review package. Explorer does not install it into Focus Room; integration and production promotion remain explicit engineering work with a rollback path.

See [data interface](docs/DATA.md), [hardware provenance](docs/HARDWARE.md), [analysis and plugin interface](docs/ANALYSIS.md), [private synchronization](docs/SYNC.md), [Focus Room comparison](docs/FOCUS_COMPARISON.md), and [verification evidence](docs/VERIFICATION.md).

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
npm test
```

Tests use synthetic inputs and temporary local storage. They do not open Bluetooth devices or upload guest data. The application requires no network connection for acquisition, labeling or local analysis.
