"""Durable private GitHub backup queue. Network and compression never run in acquisition callbacks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

PART_BYTES = 40 * 1024 * 1024
SCHEMA = "eeg-explorer.sync/1"


class SyncConflict(ValueError):
    pass


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with tmp.open("w") as f:
        json.dump(value, f, sort_keys=True, indent=2, allow_nan=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def _relative(value):
    if not re.fullmatch(r"sessions/[a-f0-9]{32}/r[0-9]{10}", value):
        raise ValueError("Invalid backup session path")
    return value


class GitBackend:
    """Dedicated local clones; credentials remain in the user's gh credential provider."""
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)

    def _run(self, args, cwd=None, timeout=120):
        # Do not emit command stderr: it can contain credentials from external Git configuration.
        try:
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
            p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError:
            raise ValueError(f"Required command is unavailable: {args[0]}") from None
        except subprocess.TimeoutExpired:
            raise ValueError(f"{args[0]} timed out; local recording is safe and retry is available") from None
        if p.returncode:
            raise ValueError(f"{args[0]} operation failed (exit {p.returncode}); check authentication, connection, and repository permissions")
        return p.stdout

    def validate_repository(self, repository):
        data = json.loads(self._run(["gh", "api", f"repos/{repository}", "--jq", "{private: .private, archived: .archived, permissions: .permissions, default_branch: .default_branch}"]))
        if data.get("private") is not True:
            raise ValueError("EEG synchronization requires an explicitly configured private GitHub repository")
        if data.get("archived") or not data.get("permissions", {}).get("push"):
            raise ValueError("Repository must be writable and unarchived")
        return data

    def _url(self, repository):
        return f"https://github.com/{repository}.git"

    def _git(self, arguments, cwd=None, timeout=120):
        return self._run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *arguments], cwd=cwd, timeout=timeout)

    def _clone(self, repository, destination):
        self._git(["clone", "--quiet", "--no-tags", self._url(repository), str(destination)])

    def upload(self, repository, relative, folder):
        """Optimistic immutable addition. Retry from a fresh clone after unrelated concurrent commits."""
        _relative(relative)
        expected = json.loads((Path(folder)/"manifest.json").read_text())
        self.validate_repository(repository)  # Visibility may change after initial configuration.
        for attempt in range(3):
            with tempfile.TemporaryDirectory(prefix="upload-", dir=self.root) as td:
                checkout = Path(td)/"repo"; self._clone(repository, checkout)
                target = checkout/relative
                for path in (checkout/"sessions", checkout/"sessions"/expected["session_id"], target):
                    if path.is_symlink():
                        raise SyncConflict("Remote backup path contains an unsupported symbolic link")
                if target.exists():
                    actual_path = target/"manifest.json"
                    if actual_path.is_file() and not actual_path.is_symlink() and json.loads(actual_path.read_text()) == expected:
                        return {"already_present": True}
                    raise SyncConflict("Another backup contains different data at this session revision. No remote or local data was overwritten; import into a separate library for review.")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(folder, target)
                self._git(["add", "--", relative], cwd=checkout)
                self._git(["-c", "user.name=EEG Explorer", "-c", "user.email=eeg-explorer@localhost", "commit", "--quiet", "-m", f"Preserve EEG session {expected['session_id']} revision {expected['revision']}"], cwd=checkout)
                try:
                    self._git(["push", "--quiet", "origin", "HEAD"], cwd=checkout)
                    return {"already_present": False}
                except ValueError:
                    if attempt == 2:
                        raise
        raise ValueError("Concurrent pushes exceeded retry limit")

    def download(self, repository, relative, destination):
        """Read from a new remote clone, not upload staging or its object database."""
        _relative(relative)
        self.validate_repository(repository)
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as td:
            checkout = Path(td)/"repo"; self._clone(repository, checkout)
            source = checkout/relative
            if source.is_symlink() or not source.resolve().is_relative_to(checkout.resolve()) or any(p.is_symlink() for p in source.rglob("*")):
                raise ValueError("Remote backup contains unsupported symbolic links")
            if not source.is_dir():
                raise ValueError("Uploaded revision was absent from fresh remote verification")
            shutil.copytree(source, destination, dirs_exist_ok=True)


def make_bundle(archive, destination, session_id, revision, part_bytes=PART_BYTES):
    """Chunk ZIP bytes; manifest authenticates every chunk and the complete archive."""
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=True)
    parts = []
    whole = hashlib.sha256()
    with Path(archive).open("rb") as src:
        index = 0
        while True:
            data = src.read(part_bytes)
            if not data:
                break
            name = f"archive.zip.part{index:06d}"
            (destination/name).write_bytes(data)
            parts.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            whole.update(data); index += 1
    manifest = {"schema": SCHEMA, "session_id": session_id, "revision": revision, "archive": "archive.zip", "archive_sha256": whole.hexdigest(), "archive_size": sum(p["size"] for p in parts), "parts": parts, "storage": "immutable normal Git chunks; each ≤40 MiB; no LFS pointers", "privacy": "private repository required; session participants should be pseudonymous"}
    _atomic(destination/"manifest.json", manifest)
    return manifest


def verify_bundle(folder, expected=None, output=None):
    folder = Path(folder)
    if (folder/"manifest.json").is_symlink():
        raise ValueError("Backup manifest contains an unsafe symbolic link")
    manifest = json.loads((folder/"manifest.json").read_text())
    if manifest.get("schema") != SCHEMA or (expected is not None and manifest != expected):
        raise ValueError("Remote manifest does not match the immutable local revision")
    parts = manifest.get("parts", [])
    if not isinstance(parts, list) or not parts:
        raise ValueError("Backup contains no archive parts")
    whole = hashlib.sha256(); total = 0
    out = Path(output).open("wb") if output is not None else None
    try:
        for i, part in enumerate(parts):
            if part.get("name") != f"archive.zip.part{i:06d}":
                raise ValueError("Backup parts are not sequential or contain an unsafe path")
            path = folder/part["name"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != part["size"]:
                raise ValueError("Backup part missing, unsafe, or wrong size")
            h = hashlib.sha256()
            with path.open("rb") as src:
                for data in iter(lambda: src.read(1024*1024), b""):
                    h.update(data); whole.update(data); total += len(data)
                    if out:
                        out.write(data)
            if h.hexdigest() != part["sha256"]:
                raise ValueError("Downloaded archive part failed SHA-256 verification")
        if total != manifest["archive_size"] or whole.hexdigest() != manifest["archive_sha256"]:
            raise ValueError("Reassembled archive failed SHA-256 verification")
    finally:
        if out:
            out.close()
    return manifest


class SyncManager:
    def __init__(self, store, backend=None, start_worker=True, retry_seconds=15):
        self.store = store
        self.root = Path(store.root)/"sync"; self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/"queue.sqlite"
        self.config_path = self.root/"config.json"
        if not self.config_path.exists():
            _atomic(self.config_path, {"repository": "", "enabled": False})
        self.backend = backend or GitBackend(self.root/"work")
        self.lock = threading.RLock(); self.wake = threading.Event(); self.closed = threading.Event()
        self.retry_seconds = retry_seconds
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session_id TEXT,revision INTEGER,repository TEXT,state TEXT,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,error TEXT,bundle TEXT,archive_sha256 TEXT,created REAL,updated REAL,UNIQUE(session_id,revision,repository))")
            db.execute("UPDATE jobs SET state='queued' WHERE state='uploading'")
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self._loop, name="eeg-github-sync", daemon=True)
            self.worker.start()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def config(self):
        return json.loads(self.config_path.read_text())

    def status(self):
        config = self.config()
        with self._db() as db:
            jobs = [dict(row) for row in db.execute("SELECT id,session_id,revision,repository,state,attempts,next_attempt,error,archive_sha256,created,updated FROM jobs ORDER BY updated DESC LIMIT 100")]
        return {**config, "configured": bool(config.get("repository")), "jobs": jobs, "saved_locally": True, "storage": "Complete immutable revision archives split into ≤40 MiB Git files", "limitations": "Full revisions increase repository history size. For large or long-running studies migrate to an approved large-dataset backend; recording remains local if GitHub rejects a push.", "worker_alive": bool(self.worker and self.worker.is_alive())}

    def configure(self, request):
        repository = str(request.get("repository", "")).strip()
        enabled = bool(request.get("enabled", False))
        if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Configure an existing private repository as owner/repository; URLs and credentials are not accepted")
        if enabled and not repository:
            raise ValueError("Select an existing private repository before enabling synchronization")
        if repository:
            self.backend.validate_repository(repository)
        _atomic(self.config_path, {"repository": repository, "enabled": enabled})
        if enabled:
            for session in self.store.list_sessions():
                self.enqueue(session["id"])
        self.wake.set()
        return self.status()

    def enqueue(self, session_id):
        """Cheap durable enqueue only. No ZIP, Git, subprocess, or network in this method."""
        meta = self.store.session(session_id)
        config = self.config()
        revision = int(meta.get("revision", 0))
        recording = meta.get("status") in ("armed", "recording")
        state = "waiting_for_recording" if recording else ("queued" if config.get("enabled") else "saved_locally")
        repository = config.get("repository", "")
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,session_id,revision,repository,state,created,updated) VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, session_id, revision, repository, state, time.time(), time.time()))
            if not recording:
                db.execute("UPDATE jobs SET state='superseded',updated=? WHERE session_id=? AND repository=? AND state IN ('waiting_for_recording','saved_locally','queued') AND revision<?", (time.time(), session_id, repository, revision))
            db.execute("UPDATE jobs SET state=?,updated=? WHERE session_id=? AND revision=? AND repository=? AND state IN ('saved_locally','waiting_for_recording')", (state, time.time(), session_id, revision, repository))
        self.wake.set()
        return self.status()

    def retry(self):
        with self._db() as db:
            db.execute("UPDATE jobs SET state='queued',next_attempt=0,error=NULL,updated=? WHERE state='failed'", (time.time(),))
        self.wake.set()
        return self.status()

    def _update(self, job_id, **values):
        values["updated"] = time.time()
        with self._db() as db:
            db.execute("UPDATE jobs SET " + ",".join(k+"=?" for k in values) + " WHERE id=?", [*values.values(), job_id])

    def _process_one(self):
        config = self.config()
        if not config.get("enabled") or not config.get("repository"):
            return False
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE repository=? AND (state='queued' OR (state='failed' AND next_attempt>0 AND next_attempt<=?)) ORDER BY created LIMIT 1", (config["repository"], time.time())).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='uploading',updated=? WHERE id=?", (time.time(), row["id"]))
        if not row:
            return False
        job = dict(row)
        try:
            meta = self.store.session(job["session_id"])
            if meta.get("status") in ("armed", "recording"):
                self._update(job["id"], state="waiting_for_recording")
                return True
            if not job.get("bundle"):
                if int(meta.get("revision", 0)) != job["revision"]:
                    self._update(job["id"], state="superseded")
                    self.enqueue(job["session_id"])
                    return True
                archive = self.store.export_session(job["session_id"])
                with zipfile.ZipFile(archive) as z:
                    snapshot_meta = json.loads(z.read("metadata.json"))
                if snapshot_meta["id"] != job["session_id"] or int(snapshot_meta["revision"]) != job["revision"]:
                    self._update(job["id"], state="superseded")
                    self.enqueue(job["session_id"])
                    return True
                bundle = self.root/"bundles"/job["id"]
                manifest = make_bundle(archive, bundle, job["session_id"], job["revision"])
                job["bundle"] = str(bundle)
                self._update(job["id"], bundle=str(bundle), archive_sha256=manifest["archive_sha256"])
            bundle = Path(job["bundle"])
            manifest = verify_bundle(bundle)
            # Recheck configuration immediately before network; disabled queues stay local.
            if self.config() != config:
                self._update(job["id"], state="queued")
                return False
            self._update(job["id"], state="uploading", attempts=job["attempts"]+1, error=None)
            relative = f"sessions/{job['session_id']}/r{job['revision']:010d}"
            self.backend.upload(config["repository"], relative, bundle)
            with tempfile.TemporaryDirectory(prefix="download-", dir=self.root) as td:
                downloaded = Path(td)/"revision"
                self.backend.download(config["repository"], relative, downloaded)
                verify_bundle(downloaded, manifest)
            self._update(job["id"], state="verified", error=None, next_attempt=0)
        except Exception as exc:
            attempts = job["attempts"]+1
            # Our backend messages are sanitized. Unexpected exception text is not logged because
            # arbitrary subprocess/config exceptions could include credentials or private file paths.
            error = str(exc)[:500] if isinstance(exc, (ValueError, SyncConflict)) else f"{type(exc).__name__}: synchronization failed; local data is preserved"
            permanent = isinstance(exc, SyncConflict)
            retry = 0 if permanent or attempts >= 5 else time.time() + min(300, self.retry_seconds * 2**(attempts-1))
            self._update(job["id"], state="failed", attempts=attempts, error=error, next_attempt=retry)
        return True

    def _loop(self):
        while not self.closed.is_set():
            try:
                worked = self._process_one()
            except Exception:
                worked = False
            if not worked:
                self.wake.wait(1)
                self.wake.clear()

    def restore(self, session_id, revision, output=None):
        """Download + verify portable archive. Import separately to preserve conflict review."""
        config = self.config()
        if not config.get("repository"):
            raise ValueError("Configure the private repository before restoring")
        relative = _relative(f"sessions/{session_id}/r{int(revision):010d}")
        destination = Path(output) if output else Path(self.store.root)/"exports"/f"recovered-{session_id}-r{revision}.zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=self.root) as td:
            self.backend.download(config["repository"], relative, Path(td)/"revision")
            temporary_archive = Path(td)/"archive.zip"
            manifest = verify_bundle(Path(td)/"revision", output=temporary_archive)
            if manifest.get("session_id") != session_id or manifest.get("revision") != int(revision):
                raise ValueError("Remote manifest identity mismatch")
            shutil.copy2(temporary_archive, destination)
        return {"path": str(destination), "sha256": manifest["archive_sha256"], "verified": True, "imported": False}

    def close(self):
        self.closed.set(); self.wake.set()
        if self.worker:
            self.worker.join(timeout=2)
