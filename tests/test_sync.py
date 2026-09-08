import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from explorer.store import Store
from explorer.sync import GitBackend, SyncConflict, SyncManager, make_bundle, sha256, verify_bundle


class FakePrivateRemote:
    """Filesystem-only fake remote; never invokes GitHub or reads authentication."""
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True)
        self.private = True; self.failures = 0; self.corrupt = False
        self.uploads = 0; self.downloads = 0
    def validate_repository(self, repository):
        if not self.private: raise ValueError('Repository must be private')
        return {'private': True}
    def upload(self, repository, relative, folder):
        self.uploads += 1
        if self.failures:
            self.failures -= 1; raise ValueError('Simulated interrupted upload')
        dest = self.root/relative
        if dest.exists():
            if (dest/'manifest.json').read_bytes() != (Path(folder)/'manifest.json').read_bytes():
                raise SyncConflict('Conflicting immutable session revision')
        else:
            shutil.copytree(folder, dest)
    def download(self, repository, relative, destination):
        self.downloads += 1
        shutil.copytree(self.root/relative, destination)
        if self.corrupt:
            path = next(Path(destination).glob('*.part*'))
            content = bytearray(path.read_bytes()); content[0] ^= 1; path.write_bytes(content)


class LocalGitBackend(GitBackend):
    """Real git transport against a temporary bare repository; no external network."""
    def __init__(self, root, remote): super().__init__(root); self.remote = str(remote)
    def validate_repository(self, repository): return {'private': True, 'permissions': {'push': True}}
    def _url(self, repository): return self.remote


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'library')
        self.remote = FakePrivateRemote(self.root/'fake-private-remote')
        self.manager = SyncManager(self.store, backend=self.remote, start_worker=False, retry_seconds=.001)
        self.addCleanup(self.manager.close)
        self.session = self.store.create({'source': 'simulated', 'sample_rate': 128, 'channels': ['Left CH1'], 'participant': 'synthetic-person'})
        self.sid = self.session['id']
        self.store.ingest(self.sid, {'channels': {'Left CH1': [1., 2., 3., 4.]}, 'sample_rate': 128, 'received_monotonic_ns': 1000000000, 'received_wall_ns': 1000000000})

    def enable(self): self.manager.configure({'repository': 'research/private-eeg', 'enabled': True})
    def job(self): return self.manager.status()['jobs'][0]

    def test_unconfigured_is_local_only_and_recording_does_not_export(self):
        self.manager.enqueue(self.sid)
        self.assertFalse(self.manager._process_one())
        self.assertEqual(self.remote.uploads, 0)
        self.enable()
        self.assertFalse(self.manager._process_one())
        self.assertEqual(self.job()['state'], 'waiting_for_recording')
        self.assertFalse((self.store.root/'exports').exists())

    def test_private_repository_validation(self):
        self.remote.private = False
        with self.assertRaisesRegex(ValueError, 'private'): self.enable()
        self.assertFalse(self.manager.status()['enabled'])
        with self.assertRaises(ValueError): self.manager.configure({'repository': 'https://token@github.com/x/y', 'enabled': True})

    def test_production_backend_requires_private_push_permissions(self):
        backend = GitBackend(self.root/'production-backend')
        with patch.object(backend, '_run', return_value=b'{"private":false,"permissions":{"push":true}}'):
            with self.assertRaisesRegex(ValueError, 'private'):
                backend.validate_repository('fixture/private')
        with patch.object(backend, '_run', return_value=b'{"private":true,"permissions":{"push":false}}'):
            with self.assertRaisesRegex(ValueError, 'writable'):
                backend.validate_repository('fixture/private')
        with patch.object(backend, '_run', return_value=b'{"private":true,"permissions":{"push":true},"archived":false}'):
            self.assertTrue(backend.validate_repository('fixture/private')['private'])

    def test_queue_retry_remote_hash_verification_and_restore(self):
        self.store.stop(self.sid); self.enable(); self.remote.failures = 1
        self.assertTrue(self.manager._process_one()); self.assertEqual(self.job()['state'], 'failed')
        self.manager.retry(); self.remote.corrupt = True
        self.manager._process_one(); self.assertEqual(self.job()['state'], 'failed')
        self.assertIn('SHA-256', self.job()['error'])
        self.manager.retry(); self.remote.corrupt = False
        self.manager._process_one(); self.assertEqual(self.job()['state'], 'verified')
        self.assertGreaterEqual(self.remote.downloads, 2)
        restored = self.manager.restore(self.sid, self.store.session(self.sid)['revision'])
        self.assertTrue(restored['verified'])
        self.assertEqual(sha256(restored['path']), restored['sha256'])
        other = Store(self.root/'recovered-library')
        recovered = other.import_zip(restored['path'])
        self.assertEqual(recovered['sample_count'], 4)
        self.assertEqual(other.samples(self.sid), self.store.samples(self.sid))

    def test_annotation_revision_gets_new_immutable_remote_directory(self):
        self.store.stop(self.sid); self.enable(); self.manager._process_one()
        original = self.job()['archive_sha256']
        self.store.annotate(self.sid, {'start': 0, 'end': .02, 'label': 'Synthetic', 'reviewed': True})
        self.manager.enqueue(self.sid); self.manager._process_one()
        jobs = self.manager.status()['jobs']
        self.assertEqual(len([j for j in jobs if j['state'] == 'verified']), 2)
        self.assertNotEqual(self.job()['archive_sha256'], original)
        self.assertEqual(len(list((self.remote.root/'sessions'/self.sid).iterdir())), 2)

    def test_conflicting_revision_is_not_overwritten_and_does_not_auto_retry(self):
        self.store.stop(self.sid); self.enable(); self.manager._process_one()
        revision = self.store.session(self.sid)['revision']
        manifest = self.remote.root/'sessions'/self.sid/f'r{revision:010d}'/'manifest.json'
        data = json.loads(manifest.read_text()); data['archive_sha256'] = 'a'*64; manifest.write_text(json.dumps(data))
        with self.manager._db() as db: db.execute("UPDATE jobs SET state='queued'")
        self.manager._process_one()
        self.assertEqual(self.job()['state'], 'failed'); self.assertEqual(self.job()['next_attempt'], 0)
        self.assertEqual(json.loads(manifest.read_text())['archive_sha256'], 'a'*64)

    def test_queue_resumes_after_interrupted_process(self):
        self.store.stop(self.sid); self.enable()
        with self.manager._db() as db: db.execute("UPDATE jobs SET state='uploading'")
        second = SyncManager(self.store, backend=self.remote, start_worker=False)
        self.addCleanup(second.close)
        self.assertEqual(second.status()['jobs'][0]['state'], 'queued')
        second._process_one(); self.assertEqual(second.status()['jobs'][0]['state'], 'verified')

    def test_chunk_assembly_and_manifest_path_validation(self):
        archive = self.root/'data.zip'; archive.write_bytes(bytes(range(256))*4)
        folder = self.root/'parts'
        expected = make_bundle(archive, folder, self.sid, 1, part_bytes=100)
        self.assertGreater(len(expected['parts']), 1)
        restored = self.root/'reassembled.zip'
        verify_bundle(folder, expected, restored)
        self.assertEqual(restored.read_bytes(), archive.read_bytes())
        data = json.loads((folder/'manifest.json').read_text()); data['parts'][0]['name'] = '../secret'
        (folder/'manifest.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'unsafe'): verify_bundle(folder)

    @unittest.skipUnless(shutil.which('git'), 'git executable required')
    def test_real_git_local_bare_remote_roundtrip(self):
        remote = self.root/'bare.git'
        subprocess.run(['git', 'init', '--bare', '--quiet', str(remote)], check=True, capture_output=True)
        backend = LocalGitBackend(self.root/'git-work', remote)
        manager = SyncManager(self.store, backend=backend, start_worker=False)
        self.addCleanup(manager.close)
        self.store.stop(self.sid)
        manager.configure({'repository': 'fixture/private', 'enabled': True})
        manager._process_one()
        self.assertEqual(manager.status()['jobs'][0]['state'], 'verified', manager.status())
        restored = manager.restore(self.sid, 1)
        self.assertEqual(sha256(restored['path']), restored['sha256'])


if __name__ == '__main__': unittest.main()
