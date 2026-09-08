"""Loopback-only API; exact Origin/Host validation prevents cross-site local writes."""
import argparse,json,mimetypes,os,signal,threading,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
from .controller import Controller
from .store import dumps
UI=Path(__file__).resolve().parents[1]/'ui'
def default_root():
    if sys.platform=='darwin':return Path.home()/'Library/Application Support/Zone EEG Explorer'
    if sys.platform=='win32':return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Zone EEG Explorer'
    return Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'zone-eeg-explorer'
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def reply(self,data,status=200):
        payload=dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
    def valid_host(self):return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
    def do_GET(self):
        if not self.valid_host():return self.reply({'error':'Invalid host'},403)
        p=urlparse(self.path);q={k:v[-1] for k,v in parse_qs(p.query).items()};s=self.server.controller.store
        try:
            if p.path=='/api/state':return self.reply(self.server.controller.state())
            if p.path=='/api/sessions':return self.reply(s.list_sessions())
            if p.path=='/api/session':return self.reply({'session':s.session(q['id']),'annotations':s.annotations(q['id']),'events':s.events(q['id']),'history':s.history(q['id']),'results':s.results(q['id'])})
            if p.path=='/api/samples':return self.reply(s.samples(q['id'],float(q.get('start',0)),float(q['end']) if q.get('end') else None,q['channels'].split(',') if q.get('channels') else None,min(50000,max(1,int(q.get('limit',10000))))))
            if p.path=='/api/labels':return self.reply(s.labels())
            if p.path=='/api/settings':return self.reply(s.settings())
            if p.path=='/api/sync':return self.reply(self.server.controller.sync.status())
            if p.path=='/api/export':
                f=s.export_session(q['id']);self.send_response(200);self.send_header('Content-Type','application/zip');self.send_header('Content-Disposition',f'attachment; filename="{f.name}"');self.send_header('Content-Length',str(f.stat().st_size));self.end_headers()
                with f.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1048576),b''):self.wfile.write(chunk)
                return
            path=(UI/('index.html' if p.path=='/' else p.path.lstrip('/'))).resolve()
            if not path.is_relative_to(UI) or not path.is_file():return self.reply({'error':'Not found'},404)
            data=path.read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'");self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:self.reply({'error':str(e)},400)
    def do_POST(self):
        origin=self.headers.get('Origin');expected=f'http://{self.headers.get("Host")}'
        if not self.valid_host() or (origin is not None and origin!=expected) or self.headers.get('Sec-Fetch-Site')=='cross-site':return self.reply({'error':'Cross-origin local access denied'},403)
        if self.headers.get_content_type()!='application/json':return self.reply({'error':'JSON required'},415)
        try:
            n=int(self.headers.get('Content-Length',0))
            if not 0<n<2*1024*1024:raise ValueError('Request too large or empty')
            d=json.loads(self.rfile.read(n))
            if self.path=='/api/import':d['action']='import'
            elif self.path!='/api/action':return self.reply({'error':'Not found'},404)
            self.reply(self.server.controller.action(d))
        except Exception as e:self.reply({'error':str(e)},400)
def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8766);p.add_argument('--data-dir',default=os.environ.get('EEG_EXPLORER_DATA_DIR'));p.add_argument('--simulate',action='store_true');a=p.parse_args()
    root=Path(a.data_dir).expanduser() if a.data_dir else default_root();root.mkdir(parents=True,exist_ok=True)
    # Single acquisition/store owner per library. OS releases this lock on crash.
    lock=(root/'app.lock').open('a+')
    if os.name!='nt':
        import fcntl
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('EEG Explorer already owns this library. Open its window or choose another data directory.')
    c=Controller(root);server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);server.controller=c
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    if a.simulate:c.action({'action':'simulate'})
    print(json.dumps({'ready':True,'port':server.server_port,'url':f'http://127.0.0.1:{server.server_port}','data_dir':str(root)}),flush=True)
    try:server.serve_forever()
    finally:c.close();server.server_close();lock.close()
if __name__=='__main__':main()
