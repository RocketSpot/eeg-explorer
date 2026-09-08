import tempfile,time,unittest
from explorer.controller import Controller
class FakeHardware:
 def __init__(self,b,e):self.batch=b;self.event=e
 def status(self):return {'connected':True,'source':'simulation','streaming':True}
 def close(self):pass
class FakeSync:
 def __init__(self,s):self.queued=[]
 def enqueue(self,s):self.queued.append(s)
 def status(self):return {}
 def close(self):pass
class ControllerTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.c=Controller(self.tmp.name,FakeHardware,FakeSync);self.sid=self.c.action({'action':'record_start'})['id'];self.epoch=self.c.recording['armed_monotonic_ns']+400000000;self.feed(100)
 def tearDown(self):self.c.close();self.tmp.cleanup()
 def feed(self,n,mono=10000000000):mono=self.epoch+mono-10000000000;self.c.on_batch({'channels':{'Left-A':list(range(n))},'sample_rate':250,'received_monotonic_ns':mono,'received_wall_ns':1700000000000000000+mono,'units':'counts'});self.c.flush()
 def test_labels_separate_and_switch_same_boundary(self):
  labs=self.c.store.labels();self.c.action({'action':'select_label','label_id':labs[0]['id']});self.c.action({'action':'section_toggle'});self.feed(100,10400000000);self.c.action({'action':'select_label','label_id':labs[1]['id']});self.feed(50,10600000000);self.c.action({'action':'section_toggle'});a=self.c.store.annotations(self.sid);self.assertEqual(a[0]['end'],a[1]['start']);self.assertTrue(self.c.recording['armed']);self.assertEqual(self.c.store.session(self.sid)['sample_count'],250)
 def test_split_merge_undo_whole_operation(self):
  a=self.c.store.annotate(self.sid,{'start':0,'end':.3,'label':'Test'});self.c.action({'action':'annotation_split','id':a['id'],'t':.1});self.assertEqual(len(self.c.store.annotations(self.sid)),2);self.c.action({'action':'undo'});r=self.c.store.annotations(self.sid);self.assertEqual(len(r),1);self.assertEqual(r[0]['end'],.3)
 def test_cancel_no_sample_change_and_previous(self):
  self.c.action({'action':'section_toggle'});self.feed(100,10400000000);self.c.action({'action':'cancel_section'});self.assertEqual(self.c.store.annotations(self.sid),[]);self.c.action({'action':'mark_previous','seconds':.2});self.assertEqual(len(self.c.store.annotations(self.sid)),1);self.assertEqual(self.c.store.session(self.sid)['sample_count'],200)
 def test_countdown_events_without_artificial_samples(self):
  self.c.action({'action':'section_toggle','countdown':.05});time.sleep(.12);self.assertIsNotNone(self.c.section);self.assertEqual(self.c.store.session(self.sid)['sample_count'],100);self.assertIn('countdown_finished',[e['kind'] for e in self.c.store.events(self.sid)])
 def test_protocol_remains_unreviewed(self):
  lab=self.c.store.labels()[0];self.c.action({'action':'protocol','command':'start','steps':[{'label_id':lab['id'],'duration':30}],'transition_seconds':0});self.feed(100,10400000000);self.c.action({'action':'protocol','command':'stop'});a=self.c.store.annotations(self.sid)[0];self.assertEqual(a['source'],'protocol');self.assertFalse(a['reviewed']);self.assertTrue(self.c.recording['armed'])
if __name__=='__main__':unittest.main()
