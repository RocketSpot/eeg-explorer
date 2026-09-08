import tempfile,unittest,hashlib,zipfile,json
from unittest.mock import patch
from pathlib import Path
from explorer.store import Store
class StoreTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.sid=self.s.create({'source':'simulation'})['id']
 def tearDown(self):self.tmp.cleanup()
 def batch(self,values,mono=10000000000,continuity=None):return {'channels':{'Left-A':values},'sample_rate':250,'received_wall_ns':1700000000000000000+mono,'received_monotonic_ns':mono,'continuity':continuity or {},'units':'counts'}
 def test_first_sample_zero_and_unequal_channels(self):
  b=self.batch([1,2,3]);b['channels']['Right-A']=[8];self.s.ingest(self.sid,b);rows=self.s.samples(self.sid);self.assertAlmostEqual(rows[0]['t'],0,6);self.assertEqual(len(rows),4);self.assertAlmostEqual(rows[-1]['t'],.008,6)
 def test_exact_counts_timing_gap_and_indices(self):
  self.s.ingest(self.sid,self.batch([1,2],continuity={'dev1':{'firstAbsIdx':4,'lastAbsIdx':5}}))
  self.s.ingest(self.sid,self.batch([3,4],mono=10016000000,continuity={'dev1':{'firstAbsIdx':8,'lastAbsIdx':9,'holes':[{'pos':0,'nMissing':2}]}}))
  rows=self.s.samples(self.sid);self.assertEqual([r['device_index'] for r in rows],[4,5,8,9]);self.assertAlmostEqual(rows[2]['t']-rows[1]['t'],.012,6);self.assertEqual(len(self.s.events(self.sid)),1)
 def test_reconnect_unknown_and_no_initial_empty_period(self):
  self.s.ingest(self.sid,self.batch([1,2]));self.s.ingest(self.sid,self.batch([3,4],mono=14000000000,continuity={'dev1':{'firstAbsIdx':0,'holes':[{'pos':0,'nMissing':None,'uncountable':True,'kind':'reconnect'}]}}));rows=self.s.samples(self.sid);self.assertGreater(rows[2]['t']-rows[1]['t'],3.9);self.assertTrue(any(e['kind']=='gap' for e in self.s.events(self.sid)))
 def test_annotations_do_not_change_samples(self):
  self.s.ingest(self.sid,self.batch(list(range(100))));before=self.s.samples(self.sid);a=self.s.annotate(self.sid,{'start':.04,'end':.2,'label':'Test','reviewed':True});self.assertIn('Left-A',a['sample_bounds']);self.s.annotate(self.sid,{**a,'label':'New'});self.s.undo(self.sid);self.assertEqual(self.s.annotations(self.sid)[0]['label'],'Test');self.assertEqual(self.s.samples(self.sid),before);self.assertEqual(self.s.session(self.sid)['status'],'recording')
 def test_recovery_export_round_trip_integrity(self):
  self.s.ingest(self.sid,self.batch([8388607,-8388608,0]));fresh=Store(self.tmp.name);self.assertEqual(fresh.session(self.sid)['status'],'recovered');archive=fresh.export_session(self.sid)
  with tempfile.TemporaryDirectory() as other:
   restored=Store(other);restored.import_zip(archive);self.assertEqual(restored.samples(self.sid),fresh.samples(self.sid));self.assertEqual(restored.session(self.sid)['id'],self.sid)
   with self.assertRaises(ValueError):restored.import_zip(archive)
 def test_decimation_keeps_flat_signals_bounded(self):
  self.s.ingest(self.sid,self.batch([5]*10000));self.assertLessEqual(len(self.s.samples(self.sid,limit=100)),102)
 def test_invalid_annotations(self):
  self.s.ingest(self.sid,self.batch([1,2]));
  with self.assertRaises(ValueError):self.s.annotate(self.sid,{'start':-1,'end':1})
 def test_import_copy_failure_leaves_no_partial_destination_and_retry_works(self):
  self.s.ingest(self.sid,self.batch([1,2,3]));self.s.stop(self.sid);archive=self.s.export_session(self.sid)
  with tempfile.TemporaryDirectory() as other:
   target=Store(other)
   with patch('shutil.copyfileobj',side_effect=OSError('disk full')):
    with self.assertRaisesRegex(OSError,'disk full'):target.import_zip(archive)
   self.assertFalse((target.root/'sessions'/self.sid).exists())
   result=target.import_zip(archive);self.assertEqual(result['sample_count'],3)
   self.assertEqual(target.samples(self.sid),self.s.samples(self.sid))
if __name__=='__main__':unittest.main()
