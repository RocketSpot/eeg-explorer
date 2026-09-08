# Zone hardware acquisition

EEG Explorer launches independently. It snapshots the working Focus Room BLE
decoder instead of importing or editing that application's active checkout.
`vendor/PROVENANCE.json` pins SHA-256 hashes, source paths, commit, source version
and capture time. This is explicitly a snapshot of an evolving working tree;
the commit alone does not identify its contents. Updating the snapshot is an
explicit development action, never an automatic software update.

## Recovered context

The source was `/Users/neurotech/focus-room`, version 1.0.20. The relevant Claude
session is `/Users/neurotech/.claude/projects/-/0ff9047a-ed0d-4bc9-868c-e094cad02c9b.jsonl`.
The Codex tasks `Explore The Focus Room`, `Fix focus room chat access`, and
`Continue imported Claude session` supplied historical observations and the
hardware map. The September 8 real fit attempt recorded 72,833 samples despite
rejecting every one of 140 analysis windows. That observation motivated a raw
capture path independent of contact and analysis eligibility.

The latest Claude work identified a decoder bug: a nine-byte packet followed by
a sequence byte of 0xC0 could be mistaken for an eleven-byte packet. This caused
phantom approximately 0.4% loss and bogus auxiliary lead-off readings. The pinned
decoder contains the framing fix. Earlier documentation's assumed firmware
wrap skip is superseded: genuine missing packets at the counter wrap remain
missing. Explorer tests a 2,560-packet wrap stream, a real missing packet,
duplicates, replay refusal, extended frames, split notifications and reconnects.

## Available connection and stream

Real connection is BLE through the bundled `DualBLEConnection` and installed
Bleak runtime. No USB, serial or shared hardware stream is claimed. Scanning is
explicit; names containing `zone` come from the existing SDK discovery method.
The user assigns each discovered device to an ear. GATT services and characteristic
properties are inspected on the actual device. Candidate selection uses the
existing Zone family and Nordic UART policy, excludes firmware update services,
and records the selected triplet and candidates. Explorer additionally refuses
generic unknown service families rather than sending EEG commands to them.
Different firmware exposing only an unknown service must be integrated explicitly.

Each ear sends two signed 24-bit ADC values. Nine-byte packets have a header,
sequence counter, two three-byte values and trailer. Eleven-byte packets add two
auxiliary lead-off fields. The current labels are Left-A, Left-B, Right-A,
Right-B: eight physical electrodes produce four differential channels. Exact
electrode and reference wiring remain unverified. SDK nominal rate is 250 Hz;
the ADC register and independent calibrated clock rate are not verified.

Raw BLE notifications are retained as hex `packet` events *before* decoder
admission, including duplicate, replayed, incomplete and malformed inputs. Each
notification may contain multiple packets or a packet fragment. Accepted values
are drained on their own ear's notification thread and delivered immediately.
This avoids filtering disconnected ears out of a later buffer read. All values
are recorded: zero, saturation, off-ear and noisy signals have no capture gate.
Decoded duplicates are not inserted twice; the original received bytes remain.
A small Explorer framing envelope retains packet fragments across notification
boundaries, which the original SDK parser alone did not retain. It uses the same
nine/eleven-byte framing rules and calls the unchanged SDK to decode and admit
each complete frame. Skipped framing bytes and incomplete closing fragments are
diagnosed; original notifications still contain their bytes. This is the only
addition to framing, and is tested separately from the upstream wrap fix.

The SDK supplies each ear's independent unwrapped counter indices, holes,
duplicate/replay/refusal counts, and reconnect epochs. Missing count is explicitly
unknown for wall gaps beyond the one-byte counter's reliable range. Samples
from opposite ears are never paired by queue position. No precise cross-ear
clock synchronization or per-sample device timestamp is claimed.

The adapter adds host wall and monotonic receipt nanoseconds and estimates each
notification's sample positions using the nominal rate. `first_sample_*_ns` is
an estimate of the first decoded sample, not hardware time. BLE batching,
scheduling and separate ear clocks limit annotation accuracy. Preserve both
receipt clocks, per-ear indices, and continuity when aligning labels or exporting.

## Passive recording and physical uncertainty

Explorer never invokes the impedance test or sends `lead`. It requests the
sequence documented and previously exercised by Focus Room and bud-check:

1. `s` to stop streaming, then 0.4 seconds settle.
2. `lead0` to request lead-off excitation disabled, then 0.4 seconds settle.
3. The SDK's `v`, 50 ms, `d`, 50 ms, `b` to start EEG streaming.

Every command completion/failure is logged separately. This means **passive mode
requested**, not proven zero excitation current. There is no supported register
readback or physical measurement here verifying that the current is disabled;
`excitation_verified_disabled` stays false. A failed write stays explicit in
configuration. Incoming values during transitions are preserved, with the current
passive-mode state attached to each batch, instead of discarded as settling time.

Firmware filters, bias, reference, Vref and gain are unresolved. The existing
impedance code's 4.5 V, gain 24 and 24 nA assumptions are not EEG calibration.
Explorer saves and labels **ADC counts**, never an arbitrary microvolt scale.
The stream is host-unfiltered; upstream hardware/firmware processing is unknown.
Auxiliary lead-off bitfields, when present, are logged separately and excluded
from the primary signal-only model. They are not an impedance measurement.

## Ownership, reconnects and failure isolation

Explorer checks the usual Focus Room port for a real-hardware process and refuses
connection while it runs. A local advisory ownership lock prevents a second
Explorer process from taking the same hardware. The OS or earbuds may reject
other connection conflicts; this is not a shared BLE architecture. Stop the
other owner before connecting. Do not run both applications against the same pair.

Reconnect attempts use the selected device address and cached, inspected GATT
triplet with 1, 3, 8, 15 then 30 second delays. Only the lost ear receives restart
commands, preserving the healthy ear. The SDK records a reconnect epoch as an
explicit gap. No interpolation or backfilling occurs. Initial failed ears retry
if their partner connected. An entirely failed initial connection returns an
error instead of starting an unrequested search loop.

Acquisition uses a separate asyncio worker plus the SDK's per-ear workers. Batch
and diagnostic callbacks run on a dedicated delivery thread; callers must return
promptly. The delivery queue never silently drops items. Its length and callback
error are visible in status. A slow or failed persistence callback must be treated
as a recording failure by the app; streaming alone does not prove saved data.
On close, callbacks drain before the delivery thread exits, with bounded teardown.

## Python integration

`Hardware(on_batch, on_event)` provides `scan()`, `connect(request)`,
`disconnect()`, `status()`, `start_simulation(request=None)`, and `close()`.
Connect accepts `left_address` and/or `right_address` (aliases `left`, `right`).
It is a blocking server operation scheduled onto acquisition's asyncio thread.

A batch contains `channels` (a mapping to arrays; absent ears stay absent),
`sample_rate`, `units`, `source`, `simulation`, `received_wall_ns`,
`received_monotonic_ns`, estimated `first_sample_wall_ns` and
`first_sample_monotonic_ns`, and `continuity` indexed by `dev1` or `dev2`.
Events contain `type`, host `time` in seconds, `time_ns`, `monotonic_ns` and
`details`. The recording service decides whether a recording is armed; labels
never invoke acquisition or affect its sample stream.

Simulation needs no BLE dependency, is explicit and deterministic by `seed`,
and marks every batch/configuration as synthetic. Optional `gap_at_seconds`,
`gap_duration_seconds`, and `gap_ear` (`left`, `right`, `both`) model missing
intervals without inventing replacement values. Synthetic continuity is labeled
`simulated_gap`. It is useful for software checks, not physiological validation.

## Verification boundary

Automated tests exercise the actual vendored parser with fabricated binary
notifications, mocked command order, reconnect epochs, fixed-seed simulation,
gaps, clock metadata, ownership refusal and callback errors. No real scan,
connection, command or wearer recording was performed while building Explorer.
Real device and firmware compatibility, physical excitation, complete ear
reconnect behavior and signal interpretation require a user-run hardware check.
