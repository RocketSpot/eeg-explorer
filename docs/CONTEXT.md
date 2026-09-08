# Context carried forward

Read relevant Codex tasks and the local Claude project transcript before implementing:

- **Explore The Focus Room** (`01a081ef-3461-7740-88eb-3a6b513c6235`): architecture, raw recording path, contact versus sample availability and acquisition-window rejection.
- **Fix focus room chat access** (`01a081fe-905f-77c0-b322-ce6af96b51de`): recent session observations and the distinction between samples arriving and analysis accepting them.
- **Continue imported Claude session** (`01a0588d-e756-75d1-9f3a-2dd9d8741702`): imported development context and project location.
- **Learn The Focus Room app** (`01a082ea-86c8-7272-923c-ffd08f925d15`): concurrent completion of recent Claude work. Explorer does not modify that checkout.
- Claude transcript: `~/.claude/projects/-/0ff9047a-ed0d-4bc9-868c-e094cad02c9b.jsonl` and the relevant local project logs inspected by the acquisition audit.

The current source is `~/focus-room`. The earlier familiarization report was an older revision; the pinned hardware snapshot records source version 1.0.20 and exact working-tree file hashes in `vendor/PROVENANCE.json`. A working-tree snapshot is explicitly identified rather than claimed to match only its parent Git commit.

Consequential findings: raw samples must be captured independently of fit and analysis gates; receive throughput is not a device sample clock; an older apparent counter-wrap loss was caused by short/long packet misframing and must not be hidden by a wrap-skip compensation; lead-off command sequencing must be serialized; the analog excitation state is not verified by successful command delivery; missing data must remain visible. Explorer retains those distinctions, copies the proven decoder, and adds a documented notification-fragment envelope without changing the pinned parser.

The source tree contains neither copied chat transcripts nor guest recordings. Only implementation-relevant conclusions and source provenance are retained.
