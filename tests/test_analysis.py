import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from explorer import analysis
from explorer.store import Store


class MemoryStore:
    """Synthetic fixtures only; no user recordings or device access."""
    def __init__(self, root, count=6):
        self.root = Path(root)
        self.meta = {}; self.rows = {}; self.labels = {}; self.saved = []; self.gap_events = {}
        for i in range(count):
            sid = f"session-{i}"
            fs = 128
            t = np.arange(8*fs)/fs
            amplitude = np.where(t < 4, 2., 20.) * (1+i*.01)
            x = amplitude * np.sin(2*np.pi*np.where(t < 4, 10., 22.)*t)
            self.meta[sid] = {"id": sid, "sample_rate": fs, "channels": ["Left CH1"], "units": "counts", "revision": 1, "duration": 8., "participant": f"p{i//2}", "source": "simulated"}
            self.rows[sid] = [{"channel": "Left CH1", "idx": j, "device_index": j, "t": float(tm), "value": float(v)} for j, (tm, v) in enumerate(zip(t, x))]
            self.labels[sid] = [{"id": f"a{i}", "start": 0., "end": 4., "label": "In ear, still", "source": "manual", "reviewed": True, "needs_review": False, "scope": "both"}, {"id": f"b{i}", "start": 4., "end": 8., "label": "Flat on table", "source": "manual", "reviewed": True, "needs_review": False, "scope": "both"}]

    def session(self, sid): return self.meta[sid]
    def list_sessions(self): return list(self.meta.values())
    def annotations(self, sid): return self.labels[sid]
    def samples(self, sid, start=0, end=None, channels=None, limit=None):
        return [r for r in self.rows[sid] if r['t'] >= start and (end is None or r['t'] <= end) and (not channels or r['channel'] in channels)]
    def save_result(self, sid, kind, result): self.saved.append((sid, kind, result)); return "saved"
    def events(self, sid, limit=1000): return self.gap_events.get(sid, [])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(self.temp.name)
        self.request = {"session_ids": list(self.store.meta), "window_seconds": 2, "transition_margin": 0, "split": {"train": ["session-0", "session-1"], "validation": ["session-2", "session-3"], "test": ["session-4", "session-5"]}}

    def test_sample_rate_and_declarative_validation(self):
        for fs in (None, 0, float("nan")):
            with self.assertRaises(ValueError): analysis.sample_rate({"sample_rate": fs})
        with self.assertRaises(ValueError): analysis.validate_pipeline({"operations": [{"op": "bandpass", "low": 1, "high": 64}]}, 128)
        with self.assertRaises(ValueError): analysis.validate_pipeline({"operations": [{"op": "demean"}]}, 128)
        with self.assertRaises(ValueError): analysis.validate_pipeline({"code": "open('/etc/passwd')"}, 128)
        with self.assertRaises(ValueError): analysis.validate_pipeline({"operations": [{"op": "eval", "expression": "1+1"}]}, 128)

    def test_causal_filter_has_no_future_dependence(self):
        rng = np.random.default_rng(10); x = rng.normal(size=2048)
        pipeline = {"mode": "causal", "operations": [{"op": "lowpass", "hz": 20, "order": 4}]}
        full, _, _, meta = analysis.process(x, 128, pipeline)
        prefix, _, _, _ = analysis.process(x[:800], 128, pipeline)
        np.testing.assert_allclose(full[:800], prefix, atol=1e-12)
        self.assertEqual(meta['algorithmic_lookahead_seconds'], 0)
        self.assertIsNone(meta['phase_delay_seconds'])
        offline, _, _, om = analysis.process(x, 128, {**pipeline, "mode": "offline"})
        self.assertTrue(om['uses_future_samples']); self.assertFalse(np.allclose(full, offline))

    def test_spectrum_and_flatline(self):
        x = np.sin(2*np.pi*10*np.arange(256)/128)
        features = analysis.features(x, 128)
        self.assertGreater(features['alpha_fraction'], .99)
        self.assertAlmostEqual(features['rms'], 2**-.5)
        self.assertIsNone(features['clipping_fraction'])
        self.assertEqual(analysis.features(np.ones(256), 128)['flat_fraction'], 1.)

    def test_gaps_and_zero_width_unknown_gaps_reset_filter(self):
        sid = 'session-0'
        self.store.rows[sid] = [r for r in self.store.rows[sid] if not 2 <= r['t'] < 3]
        self.store.gap_events[sid] = [{"kind": "gap", "t": 5, "data": {"channel": "Left CH1", "start": 5, "end": 5, "continuity": {"uncountable": True}}}]
        result = analysis.analyze(self.store, {"session_id": sid, "start": 0, "end": 8, "pipeline": {"operations": [{"op": "lowpass", "hz": 20}]}})
        self.assertFalse(any(2 <= t < 3 for t in result['channels'][0]['times']))
        self.assertGreaterEqual(len(result['metadata']['gaps']), 2)
        starts = [b['start'] for b in result['channels'][0]['boundaries']]
        self.assertIn(5., starts)
        windows, _ = analysis._collect_windows(self.store, {**self.request, "window_seconds": 2})
        self.assertFalse(any(w['session_id'] == sid and w['start'] < 5 < w['end'] for w in windows))

    def test_detection_is_separate_from_rejection_and_originals(self):
        x = np.array([0., 0, 500, 0, 0])
        original = x.copy()
        y, flags, rejected, _ = analysis.process(x, 128, {"operations": [{"op": "detect", "metric": "abrupt_change", "threshold": 100}]})
        np.testing.assert_array_equal(original, x); np.testing.assert_array_equal(y, x)
        self.assertEqual(flags.tolist(), [False, False, True, True, False]); self.assertFalse(rejected.any())
        _, _, rejected, _ = analysis.process(x, 128, {"operations": [{"op": "detect", "metric": "abrupt_change", "threshold": 100}, {"op": "mask_flags"}]})
        self.assertTrue(rejected.any())

    def test_grouped_training_normalization_uses_only_training_groups(self):
        result = analysis.train(self.store, self.request)
        windows, _ = analysis._collect_windows(self.store, self.request)
        train_x = [w['features'] for w in windows if w['group'] in self.request['split']['train']]
        np.testing.assert_allclose(result['model']['normalization']['mean'], np.mean(train_x, axis=0))
        self.assertEqual(result['split'], self.request['split'])
        self.assertEqual(result['test']['unique_sessions'], 2)
        self.assertTrue(result['feature_artifacts'])
        self.assertTrue(result['prediction_artifacts'])
        self.assertEqual(result['test']['confusion']['rows'], 'reviewed manual label')
        self.assertIn('unknown_rate', result['test'])
        self.assertIsNone(result['test']['detection_delay_seconds'])
        for error in result['errors']:
            self.assertEqual(error['jump']['session_id'], error['session_id'])

    def test_participant_splits_are_disjoint(self):
        result = analysis.train(self.store, {"session_ids": list(self.store.meta), "group_by": "participant", "window_seconds": 2, "transition_margin": 0})
        all_groups = sum(result['split'].values(), [])
        self.assertEqual(len(all_groups), len(set(all_groups)))
        self.assertEqual(result['test']['unique_sessions'], 2)

    def test_nonmanual_unreviewed_diagnostic_and_overlapping_intervals_excluded(self):
        sid = 'session-0'
        self.store.labels[sid].extend([{**self.store.labels[sid][0], 'id': 'model', 'label': 'Prediction', 'source': 'model'}, {**self.store.labels[sid][0], 'id': 'protocol', 'source': 'protocol'}, {**self.store.labels[sid][0], 'id': 'unreviewed', 'reviewed': False}, {**self.store.labels[sid][0], 'id': 'overlap'}])
        windows, data = analysis._collect_windows(self.store, self.request)
        self.assertEqual(len([w for w in windows if w['session_id'] == sid]), 4)
        self.assertGreaterEqual(data['excluded']['unreviewed_or_nonmanual'], 3)
        self.assertNotIn('Prediction', {w['label'] for w in windows})
        self.store.labels[sid].append({'id': 'diag', 'start': 0., 'end': 2., 'source': 'protocol', 'label': 'Diagnostic', 'diagnostic': True})
        windows, _ = analysis._collect_windows(self.store, self.request)
        self.assertFalse(any(w['session_id'] == sid and w['start'] == 0 for w in windows))

    def test_numeric_boundary_uncertainty_excludes_transition_samples(self):
        self.store.labels['session-0'][0]['boundary_uncertainty'] = .75
        windows, _ = analysis._collect_windows(self.store, {**self.request, "window_seconds": 1})
        for window in windows:
            if window['session_id'] == 'session-0' and window['annotation_id'] == 'a0':
                self.assertGreaterEqual(window['start'], .75)
                self.assertLessEqual(window['end'], 3.25)

    def test_insufficient_groups_and_unknown_groundtruth_fail_honestly(self):
        with self.assertRaisesRegex(ValueError, 'three'):
            analysis.train(self.store, {'session_ids': ['session-0', 'session-1']})
        self.store.labels['session-0'][0]['label'] = 'Unknown'
        with self.assertRaisesRegex(ValueError, 'reserved'):
            analysis.train(self.store, self.request)

    def test_export_is_reviewed_versioned_and_reproducible_reference(self):
        result = analysis.train(self.store, self.request)
        with self.assertRaisesRegex(ValueError, 'approval'):
            analysis.export_model(self.store, {'model_id': result['id']})
        export = analysis.export_model(self.store, {'model_id': result['id'], 'approved': True})
        self.assertEqual(hashlib.sha256(Path(export['path']).read_bytes()).hexdigest(), export['sha256'])
        with zipfile.ZipFile(export['path']) as archive:
            package = json.loads(archive.read('package.json'))
            self.assertEqual(package['model'], result['model'])
            self.assertIn('reference/analysis.py', archive.namelist())
            self.assertEqual(hashlib.sha256(archive.read('reference/analysis.py')).hexdigest(), result['model']['reference_sha256'])
            self.assertIn('not performed', package['production_promotion'])

    def test_shared_inference_matches_evaluation_and_abstains_at_gaps(self):
        result = analysis.train(self.store, self.request)
        samples = [r for r in self.store.samples("session-4", 0, 2) if r["t"] < 2]
        output = analysis.infer_window(result["model"], samples, 128, "counts", ["Left CH1"])
        matching = [e for e in result["errors"] if e["session_id"] == "session-4" and e["start"] == 0]
        expected = matching[0]["prediction"] if matching else "In ear, still"
        self.assertEqual(output["prediction"], expected)
        gap_output = analysis.infer_window(result["model"], samples, 128, "counts", ["Left CH1"], [{"channel": "Left CH1", "start": 1, "end": 1}])
        self.assertEqual(gap_output["prediction"], "unknown")
        self.assertIn("discontinuous", gap_output["reason"])

    def test_configured_features_are_used_by_training_and_shared_inference(self):
        result = analysis.train(self.store, {**self.request, "features": ["variance", "spectral_power"]})
        self.assertEqual(result["model"]["feature_names"], ["variance", "delta_fraction", "theta_fraction", "alpha_fraction", "beta_fraction"])
        self.assertEqual(len(result["model"]["normalization"]["mean"]), 5)
        samples = [r for r in self.store.samples("session-4", 0, 2) if r["t"] < 2]
        output = analysis.infer_window(result["model"], samples, 128, "counts", ["Left CH1"])
        self.assertIn(output["prediction"], result["model"]["classes"] + ["unknown"])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            analysis.train(self.store, {**self.request, "features": ["clipping"]})

    def test_real_store_timeline_and_restored_model_export(self):
        store = Store(Path(self.temp.name)/'real-library')
        ids = []
        fs = 250
        times = np.arange(8*fs)/fs
        for i in range(3):
            session = store.create({"source": "simulated", "sample_rate": fs, "channels": ["Left CH1"], "participant": f"fixture-{i}"})
            sid = session['id']; ids.append(sid)
            values = np.where(times < 4, 2., 20.) * np.sin(2*np.pi*np.where(times < 4, 10., 22.)*times)
            store.ingest(sid, {"sample_rate": fs, "channels": {"Left CH1": values.tolist()}, "received_monotonic_ns": 100000000000, "received_wall_ns": 100000000000})
            store.stop(sid)
            for start, end, label in ((0, 4, 'In ear, still'), (4, 8, 'Flat on table')):
                store.annotate(sid, {"start": start, "end": end, "label": label, "source": "manual", "reviewed": True, "needs_review": False})
        result = analysis.train(store, {"session_ids": ids, "transition_margin": 0, "window_seconds": 2, "features": ["variance", "spectral_power"]})
        self.assertEqual(result['dataset']['window_count'], 12)
        archive = store.export_session(ids[0])
        restored = Store(Path(self.temp.name)/'restored-library')
        restored.import_zip(archive)
        exported = analysis.export_model(restored, {"model_id": result['id'], "approved": True})
        self.assertTrue(Path(exported['path']).is_file())
        with zipfile.ZipFile(exported['path']) as package:
            self.assertEqual(json.loads(package.read('package.json'))['model']['reference_sha256'], result['model']['reference_sha256'])

    def test_candidate_requires_review_and_does_not_claim_hosted_ai(self):
        output = analysis.candidate({'description': 'Detect brief interruptions without treating low amplitude as bad contact', 'sample_rate': 128})
        self.assertTrue(output['requires_review'])
        self.assertIn('deterministic', output['builder'])
        self.assertEqual(output['pipeline']['operations'][0]['metric'], 'abrupt_change')
        self.assertEqual(self.store.saved, [])


if __name__ == '__main__': unittest.main()
