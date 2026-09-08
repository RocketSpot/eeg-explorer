# Private GitHub synchronization and recovery

Raw acquisition is local-first. Synchronization is disabled until an existing private repository is explicitly configured as `owner/repository`. No repository is created automatically, and no data uploads during setup, tests, or an unconfigured run.

## Configure

Install Git and GitHub CLI, authenticate with `gh auth login`, and ensure the authenticated account can push to the intended private repository. In EEG Explorer's sync panel, enter that repository and enable sync. No access token is entered into EEG Explorer. The app invokes Git with the GitHub CLI credential provider; credentials are never included in archive manifests, saved queue state, or logs. Git's external global/system configuration and repository hooks are disabled for the worker. The private repository still contains the session metadata you deliberately record; use pseudonymous participant IDs and avoid personal details in titles or notes.

Configuration uses `gh api repos/OWNER/REPO` to require `private=true`, writable permissions, and an unarchived repository. Visibility is checked again before each upload and download. Configuration cannot prevent a repository owner from making it public later; manage repository access accordingly.

Once configured, completed and recovered sessions enter the background queue automatically. Later annotation edits and derived analyses create new session revisions. Recording-time changes are marked `waiting_for_recording`; the final completed snapshot supersedes intermediate unsent revisions. `enqueue()` writes only small SQLite queue rows. ZIP creation, Git, and network operations run in a separate daemon worker. Recording and labeling continue offline.

## Complete immutable revision storage

The remote directory is:

```text
sessions/<session-id>/r0000000012/
  manifest.json
  archive.zip.part000000
  archive.zip.part000001
  ...
```

The archive is the complete portable session export: original available samples and provenance, acquisition metadata, gap/diagnostic events, labels and annotations, edit history, and derived results/model evaluations. Export integrity checks are included inside the ZIP. It is split into sequential files of at most 40 MiB, below GitHub's per-file warning size. These are actual archive bytes, not Git LFS pointers. Each part has a SHA-256 and byte length; the manifest also includes the reassembled archive SHA-256 and length, session ID, and revision.

After pushing, the worker creates a **new clone from the remote** and reads every part from that fresh download. It verifies each size and SHA-256, the exact manifest, and the complete archive hash. It declares `verified` only after that succeeds. The upload working copy is not used as proof of remote storage. No local recording is removed after upload or verification.

This implementation uses ordinary Git storage with complete immutable snapshots. Repeated full revisions grow history. Splitting files avoids per-file limits; it does not make Git unlimited or guarantee that a large push will be accepted. GitHub can reject large repositories or pushes. The app keeps the local session and reports a retryable failure. For large studies, provision an approved dataset storage backend before collecting beyond practical Git repository sizes. The current implementation does not claim Git LFS or external object storage support. See [GitHub large-file limits and repository guidance](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).

## Queue, retries, and conflicts

Queue and settings live in `<library>/sync/queue.sqlite` and `<library>/sync/config.json`. Frozen upload bundles remain in `<library>/sync/bundles/`. States are `saved_locally`, `waiting_for_recording`, `queued`, `uploading`, `verified`, `failed`, and `superseded`. The status endpoint includes attempts, next retry time, and a sanitized error. Synchronization is independent of local save success.

A process restart converts interrupted `uploading` jobs back to `queued`. Transient failures retry with exponential backoff up to five attempts; Retry explicitly requeues failed jobs. Retrying uses the same frozen archive bytes. An already matching remote revision is accepted idempotently, then still downloaded and hash-verified. Unrelated concurrent pushes retry from a fresh clone.

If the remote already contains a **different manifest at the same session ID and revision**, the operation stops with a conflict and does not overwrite either copy. This includes two computers editing the same revision independently. Conflicts do not automatically retry. Restore the remote version into a separate local library to inspect annotation history and reconcile deliberately. Editing/merging histories automatically is not implemented.

The worker rejects symbolic links in backup paths and verifies safe sequential part names. Unexpected subprocess errors are summarized without printing arbitrary stderr, which may contain environment-specific credentials or identifiers.

## Recover on another computer

Install EEG Explorer on the authorized computer, authenticate GitHub CLI, and configure the same private repository. Use the sync restore action with the session ID and revision from a `manifest.json`:

```python
from explorer.store import Store
from explorer.sync import SyncManager

store = Store("/absolute/path/to/new-eeg-library")
sync = SyncManager(store)
sync.configure({"repository": "owner/private-eeg", "enabled": False})
restored = sync.restore("32-character-hex-session-id", 12)
store.import_zip(restored["path"])
sync.close()
```

Disabling uploads still permits an explicitly requested restore. Restore downloads all parts, verifies them, reassembles the archive, and returns a local ZIP path. Import validates the archive and SQLite metadata. Existing local session IDs are rejected instead of overwritten; use a separate library for conflict review. The model/evaluation remains in the restored session's results table; exporting a saved model automatically reconstructs a missing global `models` index entry.

## Verified behavior

Tests use synthetic fixtures and temporary fake private remotes. They cover no upload without configuration, private-repository checks, no export during active recording, durable restart, interrupted upload retry, corrupted remote download rejection, matching retry idempotence, immutable annotation revisions, manifest conflicts, chunk assembly, unsafe path rejection, restore/import equality, and a real Git push/clone round trip through a local temporary bare repository. No tests upload to GitHub or access existing guest data. A real GitHub remote and physical hardware have not been used to validate this delivery.
