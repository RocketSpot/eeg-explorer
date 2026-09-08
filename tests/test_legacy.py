import gzip
import json
import tempfile
import unittest
from pathlib import Path

from explorer.legacy import import_legacy
from explorer.store import Store


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root/'library')
    def tearDown(self):self.temp.cleanup()
    def messages(self,simulation=False):
        return [{'type':'eeg/config-v1','units':'adc_counts_unverified_sdk_units','simulation':simulation,'channelLabels':['Left-A','Left-B'],'sdkReportedSampleRateHz':250},
                {'type':'eeg/raw-v1','simulation':simulation,'channelLabels':['Left-A','Left-B'],'samples':[[0,8388607],[-8388608,1]],'monotonicReceiveTimestamp':0,'continuity':{'deviceCounters':{'left':{'n':2,'firstAbsIdx':10,'lastAbsIdx':11}},'deviceHoles':{'left':[]}}},
                {'type':'eeg/quality-v1','channels':{'Left-A':{'reasons':['clipping']}}}]
    def write(self,messages,name='eeg.raw.ndjson'):
        p=self.root/name
        with (gzip.open(p,'wt') if name.endswith('.gz') else p.open('w')) as f:
            for message in messages:f.write(json.dumps(message)+'\n')
        return p
    def test_gzip_simulation_provenance_and_original_messages_survive(self):
        messages=self.messages(True);result=import_legacy(self.store,self.write(messages,'eeg.raw.ndjson.gz'))
        self.assertEqual(result['source'],'simulation');self.assertEqual(result['units'],'ADC counts');self.assertIsNone(result['first_sample_wall_ns'])
        self.assertFalse(result['acquisition']['absolute_wall_time_available']);self.assertTrue(result['acquisition']['simulation'])
        self.assertEqual(result['sample_count'],4)
        self.assertTrue(all(row['received_wall_ns'] is None for row in self.store.samples(result['id'])))
        self.assertEqual([r['device_index'] for r in self.store.samples(result['id'],channels=['Left-A'])],[10,11])
        events=self.store.events(result['id']);self.assertEqual(sum(e['kind']=='legacy_message' for e in events),2)
        with self.store.db(result['id']) as db:self.assertEqual(json.loads(db.execute('SELECT data FROM batches').fetchone()[0])['legacy_original'],messages[1])
    def test_truncated_import_stays_partial_and_does_not_queue_sync(self):
        queued=[];self.store.changed=queued.append;p=self.write(self.messages())
        with p.open('a') as f:f.write('{"type":')
        with self.assertRaises(json.JSONDecodeError):import_legacy(self.store,p)
        sessions=self.store.list_sessions();self.assertEqual(len(sessions),1);self.assertEqual(sessions[0]['status'],'import_incomplete');self.assertEqual(sessions[0]['sample_count'],4);self.assertEqual(queued,[])
    def test_channel_mismatch_rejected_before_silent_truncation(self):
        messages=self.messages();messages[1]['samples'].append([42])
        with self.assertRaisesRegex(ValueError,'mismatched'):import_legacy(self.store,self.write(messages))
        self.assertEqual(self.store.list_sessions(),[])
    def test_relative_clock_reset_rejected_without_false_complete(self):
        messages=self.messages();messages.append({**messages[1],'monotonicReceiveTimestamp':-1})
        with self.assertRaisesRegex(ValueError,'reset'):import_legacy(self.store,self.write(messages))
        self.assertEqual(self.store.list_sessions()[0]['status'],'import_incomplete')
    def test_conflicting_simulation_metadata_rejected(self):
        messages=self.messages(False);messages[1]['simulation']=True
        with self.assertRaisesRegex(ValueError,'disagree'):import_legacy(self.store,self.write(messages))


if __name__=='__main__':unittest.main()
