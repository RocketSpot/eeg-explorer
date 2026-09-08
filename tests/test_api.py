import json,tempfile,threading,unittest,urllib.request,urllib.error
from http.server import ThreadingHTTPServer
from explorer.server import Handler
from explorer.controller import Controller
from test_controller import FakeHardware,FakeSync
class APITests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.c=Controller(self.tmp.name,FakeHardware,FakeSync);self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.controller=self.c;self.url='http://127.0.0.1:'+str(self.server.server_port);self.t=threading.Thread(target=self.server.serve_forever,daemon=True);self.t.start()
 def tearDown(self):self.server.shutdown();self.c.close();self.server.server_close();self.tmp.cleanup()
 def post(self,payload,headers=None):
  data=json.dumps(payload).encode();req=urllib.request.Request(self.url+'/api/action',data=data,headers={'Content-Type':'application/json',**(headers or {})});return json.load(urllib.request.urlopen(req))
 def test_local_record_label_export_reopen(self):
  session=self.post({'action':'record_start','title':'API round trip'});sid=session['id'];self.c.on_batch({'channels':{'Left-A':[1,2,3,4,5]},'sample_rate':250});self.c.flush();self.post({'action':'annotation','session_id':sid,'annotation':{'start':0,'end':.016,'label':'Fingertips','reviewed':True}});self.post({'action':'record_stop'})
  reopened=json.load(urllib.request.urlopen(self.url+'/api/session?id='+sid));self.assertEqual(reopened['session']['sample_count'],5);self.assertEqual(reopened['annotations'][0]['label'],'Fingertips')
  with urllib.request.urlopen(self.url+'/api/export?id='+sid) as response:self.assertEqual(response.read(2),b'PK')
 def test_cross_origin_write_and_dns_rebinding_rejected(self):
  for headers in ({'Origin':'https://attacker.invalid'},{'Host':'attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}):
   with self.assertRaises(urllib.error.HTTPError) as e:self.post({'action':'record_start'},headers)
   self.assertEqual(e.exception.code,403)
 def test_read_traversal_rejected(self):
  with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(self.url+'/../../explorer/store.py')
if __name__=='__main__':unittest.main()
