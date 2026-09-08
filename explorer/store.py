"""Durable append-only acquisition and revisioned annotations. No raw-data delete API."""
from __future__ import annotations
import hashlib, json, math, os, re, sqlite3, threading, time, uuid, zipfile
from pathlib import Path
from collections import defaultdict

VERSION = '0.1.0'
PRESETS = ['In ear, still','In ear, moving','Walking','Talking','Chewing','Adjusting fit','Partially inserted','Left ear only','Right ear only','Off ear, held in hand','Flat on table','Electrodes touching fingertips','Insertion/removal transition','Unknown']
COLORS = ['#5cdbc4','#a59aff','#8cc8ff','#f5c178','#ec96bc','#e3a373']
def uid(): return uuid.uuid4().hex
def dumps(x): return json.dumps(x, allow_nan=False, separators=(',',':'))
def atomic(path, data):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+'.'+uid()+'.tmp')
    with tmp.open('w') as f: f.write(dumps(data)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

class Store:
    def __init__(self, root, recover=True):
        self.root=Path(root).expanduser().resolve(); self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'sessions').mkdir(exist_ok=True); self.lock=threading.RLock(); self.clocks={}; self.changed=None
        self.settings_path=self.root/'settings.json'
        if not self.settings_path.exists(): atomic(self.settings_path, {'previous_seconds':10,'countdown':0,'shortcuts':{},'label_sets':{},'participant':''})
        if not (self.root/'labels.json').exists():
            atomic(self.root/'labels.json',[{'id':uid(),'name':n,'description':'','color':COLORS[i%len(COLORS)],'shortcut':str(i+1) if i<9 else '', 'placement': 'in ear' if i<2 else '', 'activity': 'still' if i==0 else ('moving' if i==1 else ''),'issue':''} for i,n in enumerate(PRESETS)])
        if not (self.root/'computer.json').exists(): atomic(self.root/'computer.json',{'id':uid()})
        for d in ((self.root/'sessions').iterdir() if recover else []):
            if (d/'session.sqlite').exists():
                try:
                    with self.db(d.name) as db:
                        meta=self._meta(db)
                        if meta.get('status') in ('recording','armed'):
                            meta['status']='recovered';meta['recovery']='Recovered after interrupted application; only committed samples preserved.'
                            self._setmeta(db,meta);self._event(db,'recovered',{},meta['duration'])
                except sqlite3.DatabaseError: pass
    def path(self, sid):
        if not re.fullmatch(r'[a-f0-9]{32}',sid or ''): raise ValueError('Invalid session ID')
        p=self.root/'sessions'/sid
        if not (p/'session.sqlite').exists(): raise ValueError('Session does not exist')
        return p
    def db(self,sid):
        db=sqlite3.connect(self.path(sid)/'session.sqlite',timeout=20);db.row_factory=sqlite3.Row
        return db
    def _meta(self,db): return json.loads(db.execute('SELECT value FROM meta WHERE key="session"').fetchone()[0])
    def _setmeta(self,db,m): db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('session',dumps(m)))
    def _event(self,db,kind,data,t=None): db.execute('INSERT INTO events(t,kind,data,wall_ns) VALUES(?,?,?,?)',(t,kind,dumps(data),time.time_ns()))
    def create(self,metadata):
        sid=uid();p=self.root/'sessions'/sid;p.mkdir();db=sqlite3.connect(p/'session.sqlite')
        db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE samples(channel TEXT,idx INTEGER,t REAL,value REAL,device_index INTEGER,received_wall_ns INTEGER,received_monotonic_ns INTEGER,batch_id INTEGER, PRIMARY KEY(channel,idx));
        CREATE INDEX time_idx ON samples(t,channel);
        CREATE TABLE batches(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE events(id INTEGER PRIMARY KEY,t REAL,kind TEXT,data TEXT,wall_ns INTEGER);
        CREATE TABLE annotations(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE history(id INTEGER PRIMARY KEY,before TEXT,after TEXT,wall_ns INTEGER,undone INTEGER DEFAULT 0);
        CREATE TABLE results(id TEXT PRIMARY KEY,kind TEXT,data TEXT,wall_ns INTEGER);
        ''')
        meta={'id':sid,'version':1,'software_version':VERSION,'created_at':time.time(),'status':'armed','title':'Untitled experiment','participant':'','source':'unknown','units':'counts','sample_rate':None,'channels':[],'duration':0,'sample_count':0,'revision':0,'first_sample_wall_ns':None,'first_sample_monotonic_ns':None,'computer_id':json.loads((self.root/'computer.json').read_text())['id'],'timing':'Host receive-time estimates at nominal sample rate; device counters preserved separately. Independent ears are not hardware synchronized; absolute sample acquisition time and transport latency unknown.','upstream_processing':'Not yet established; see acquisition metadata.','label_set':self.labels()}
        for k in ('title','participant','source','acquisition','units','sample_rate','channels'):
            if k in metadata: meta[k]=metadata[k]
        self._setmeta(db,meta);db.commit();db.close();self.clocks[sid]={}
        return meta
    def ingest(self,sid,batch):
        with self.lock, self.db(sid) as db:
            m=self._meta(db)
            if m['status'] not in ('armed','recording'): return None
            channels=batch.get('channels',{});fs=float(batch['sample_rate'])
            if not 0<fs<100000: raise ValueError('Invalid sample rate')
            if m['sample_rate'] and m['sample_rate']!=fs: raise ValueError('Sampling configuration changed; stop and start a new session')
            wall_value=batch.get('received_wall_ns',time.time_ns());wall=None if wall_value is None else int(wall_value);mono=int(batch.get('received_monotonic_ns',time.monotonic_ns()))
            cont=batch.get('continuity') or {}; clocks=self.clocks.setdefault(sid,{})
            if not any(len(v) for v in channels.values()): return None
            # Each independent ear has its own ordinal and continuity map. Counts are never rescaled.
            ends=[];prepared=[]
            for ch,values in channels.items():
                n=len(values)
                if not n: continue
                if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in values): raise ValueError('Non-finite raw sample')
                side='dev1' if ch.startswith('Left') else 'dev2';c=cont.get(side,{}) or {}
                holes={int(h.get('pos',0)):h for h in c.get('holes',[])}
                def gap_seconds(h):
                    if h.get('uncountable'):
                        return max(0,float(h.get('wallGapSec') or 0)-1/fs)
                    return max(0,int(h.get('nMissing') or 0))/fs
                # A pos0 hole precedes this batch's first actual sample. It must
                # never create an artificial blank beginning when recording arms.
                internal_gap=sum(gap_seconds(h) for pos,h in holes.items() if pos>0)
                before_gap=gap_seconds(holes.get(0,{}))
                est_start=mono/1e9-(n-1)/fs-internal_gap
                last=clocks.get(ch);idx=0 if last is None else last['idx']+1
                outage=last is not None and (mono-last['receipt'])/1e9 > max(1.0,3*n/fs)
                reset=bool(last and ((c.get('firstAbsIdx') is not None and last.get('device_index') is not None and c['firstAbsIdx']<=last['device_index']) or holes.get(0,{}).get('uncountable')))
                if last and not(outage or reset): start=last['absolute']+1/fs+before_gap
                else: start=max(est_start,last['absolute']+1/fs+before_gap if last else est_start)
                prepared.append((ch,values,holes,c,start,idx,outage,reset,last));ends.append(start)
            if m['first_sample_monotonic_ns'] is None:
                origin=min(ends);m['first_sample_monotonic_ns']=int(origin*1e9);m['first_sample_wall_ns']=wall-int((mono/1e9-origin)*1e9) if wall is not None else None
            origin=m['first_sample_monotonic_ns']/1e9
            rawmeta={k:v for k,v in batch.items() if k!='channels'};rawmeta['channel_lengths']={k:len(v) for k,v in channels.items()}
            bid=db.execute('INSERT INTO batches(data) VALUES(?)',(dumps(rawmeta),)).lastrowid
            for ch,values,holes,c,start,idx,outage,reset,last in prepared:
                if start<origin:
                    self._event(db,'timing_origin_clamped',{'channel':ch,'estimated_start':start-origin,'reason':'independent ear receipt estimate predates first recorded sample'},0)
                    start=origin
                if outage or reset: self._event(db,'gap',{'channel':ch,'start':max(0,last['absolute']+1/fs-origin),'end':start-origin,'reason':'reconnect_or_receive_outage','missing_samples':None},start-origin)
                absolute=start;dev=c.get('firstAbsIdx');rows=[]
                for i,value in enumerate(values):
                    h=holes.get(i)
                    if h:
                        missing=max(0,int(h.get('nMissing') or 0))
                        extra=max(0,float(h.get('wallGapSec') or 0)-1/fs) if h.get('uncountable') else missing/fs
                        # Boundary gaps were already included in `start`; internal
                        # gaps keep their own interval without padding raw samples.
                        gap_start=(last['absolute']+1/fs-origin) if i==0 and last else absolute-origin
                        if i>0:absolute+=extra
                        if dev is not None and i>0: dev+=missing
                        self._event(db,'gap',{'channel':ch,'start':max(0,gap_start),'end':max(0,absolute-origin),'continuity':h,'missing_samples':None if h.get('uncountable') else missing,'before_recording':i==0 and last is None},max(0,gap_start))
                    t=max(0,absolute-origin)
                    rows.append((ch,idx+i,t,value,dev,wall,mono,bid))
                    if dev is not None: dev+=1
                    absolute+=1/fs
                db.executemany('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)',rows)
                clocks[ch]={'idx':idx+len(values)-1,'absolute':absolute-1/fs,'receipt':mono,'device_index':(dev-1 if dev is not None else None)}
                m['duration']=max(m['duration'],rows[-1][2]+1/fs);m['sample_count']+=len(values)
                if ch not in m['channels']:m['channels'].append(ch)
            m.update(status='recording',sample_rate=fs,units=batch.get('units',m['units']),last_received_wall_ns=wall)
            if batch.get('acquisition'):m['acquisition']=batch['acquisition']
            self._setmeta(db,m)
            return m
    def session(self,sid):
        with self.db(sid) as db:return self._meta(db)
    def list_sessions(self):
        sessions=[]
        for p in (self.root/'sessions').glob('*/session.sqlite'):
            try:sessions.append(self.session(p.parent.name))
            except Exception:continue
        return sorted(sessions,key=lambda x:x['created_at'],reverse=True)
    def stop(self,sid):
        with self.lock,self.db(sid) as db:
            m=self._meta(db);m.update(status='complete',finished_at=time.time(),revision=m['revision']+1);self._setmeta(db,m);self._event(db,'recording_stopped',{},m['duration'])
        self.clocks.pop(sid,None);self.notify(sid);return self.session(sid)
    def notify(self,sid):
        if self.changed:self.changed(sid)
    def event(self,sid,kind,data,t=None):
        with self.lock,self.db(sid) as db:
            self._event(db,kind,data,t)
            if kind=='marker':
                m=self._meta(db);m['revision']+=1;self._setmeta(db,m)
        if kind=='marker':self.notify(sid)
    def events(self,sid,limit=100000,include_packets=False):
        # Original notifications remain in SQLite and portable archives. Their
        # high rate must not evict user markers/gaps from a full-session timeline.
        where='' if include_packets else " WHERE kind!='packet'"
        with self.db(sid) as db:return [dict(r,data=json.loads(r['data'])) for r in db.execute('SELECT * FROM events'+where+' ORDER BY id DESC LIMIT ?',(int(limit),))][::-1]
    def samples(self,sid,start=0,end=None,channels=None,limit=None):
        params=[float(start)];where='t>=?'
        if end is not None:where+=' AND t<=?';params.append(float(end))
        if channels:
            where+=' AND channel IN ('+','.join('?' for _ in channels)+')';params+=list(channels)
        with self.db(sid) as db:
            if limit:
                total=db.execute('SELECT count(*) FROM samples WHERE '+where,params).fetchone()[0]
                stride=max(1,math.ceil(total/int(limit)))
                # Representative min/max envelopes preserve extremes for overview rendering.
                if stride>1:
                    q=f'''WITH s AS (SELECT *,CAST(idx/? AS INTEGER) bin FROM samples WHERE {where}), ranked AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY channel,bin ORDER BY value,idx) low_rank,ROW_NUMBER() OVER(PARTITION BY channel,bin ORDER BY value DESC,idx DESC) high_rank FROM s) SELECT channel,idx,t,value,device_index,received_wall_ns,received_monotonic_ns,batch_id FROM ranked WHERE low_rank=1 OR high_rank=1 ORDER BY t,channel'''
                    return [dict(r) for r in db.execute(q,[stride*2]+params)]
            return [dict(r) for r in db.execute('SELECT * FROM samples WHERE '+where+' ORDER BY t,channel',params)]
    def annotations(self,sid):
        with self.db(sid) as db:return [json.loads(r[0]) for r in db.execute('SELECT data FROM annotations')]
    def annotate(self,sid,a):
        with self.lock,self.db(sid) as db:
            m=self._meta(db);a=dict(a);a.setdefault('id',uid());a.setdefault('source','manual');a.setdefault('reviewed',False);a.setdefault('needs_review',not a['reviewed']);a.setdefault('scope','both');a.setdefault('label','Unnamed section')
            if a['source'] not in ('manual','protocol','model'): raise ValueError('Invalid annotation source')
            a['start']=float(a['start']);a['end']=float(a['end'])
            if not(0<=a['start']<a['end']<=m['duration']+1e-6):raise ValueError('Section boundaries must lie within recorded samples')
            a['duration']=a['end']-a['start'];a['updated_at']=time.time()
            a['sample_bounds']={r['channel']:{'start_index':r['lo'],'end_index_exclusive':r['hi']+1} for r in db.execute('SELECT channel,MIN(idx) lo,MAX(idx) hi FROM samples WHERE t>=? AND t<? GROUP BY channel',(a['start'],a['end']))}
            old=db.execute('SELECT data FROM annotations WHERE id=?',(a['id'],)).fetchone()
            db.execute('INSERT OR REPLACE INTO annotations VALUES(?,?)',(a['id'],dumps(a)))
            db.execute('INSERT INTO history(before,after,wall_ns) VALUES(?,?,?)',(old[0] if old else 'null',dumps(a),time.time_ns()))
            m['revision']+=1;self._setmeta(db,m)
        self.notify(sid);return a
    def delete_annotation(self,sid,aid):
        with self.lock,self.db(sid) as db:
            old=db.execute('SELECT data FROM annotations WHERE id=?',(aid,)).fetchone()
            if not old:raise ValueError('Section not found')
            db.execute('INSERT INTO history(before,after,wall_ns) VALUES(?,?,?)',(old[0],'null',time.time_ns()));db.execute('DELETE FROM annotations WHERE id=?',(aid,))
            m=self._meta(db);m['revision']+=1;self._setmeta(db,m)
        self.notify(sid)
    def edit_annotations(self,sid,upserts,deletes):
        """Atomic split/merge; one undo restores the entire annotation operation."""
        with self.lock,self.db(sid) as db:
            m=self._meta(db);before=[];after=[]
            ids=set(deletes)|{a['id'] for a in upserts}
            for aid in ids:
                row=db.execute('SELECT data FROM annotations WHERE id=?',(aid,)).fetchone()
                if row:before.append(json.loads(row[0]))
            for a in upserts:
                a=dict(a)
                if not 0<=float(a['start'])<float(a['end'])<=m['duration']+1e-6:raise ValueError('Invalid section boundaries')
                a['duration']=a['end']-a['start'];a['updated_at']=time.time()
                a['sample_bounds']={r['channel']:{'start_index':r['lo'],'end_index_exclusive':r['hi']+1} for r in db.execute('SELECT channel,MIN(idx) lo,MAX(idx) hi FROM samples WHERE t>=? AND t<? GROUP BY channel',(a['start'],a['end']))}
                after.append(a)
            for aid in ids:db.execute('DELETE FROM annotations WHERE id=?',(aid,))
            for a in after:db.execute('INSERT INTO annotations VALUES(?,?)',(a['id'],dumps(a)))
            db.execute('INSERT INTO history(before,after,wall_ns) VALUES(?,?,?)',(dumps(before),dumps(after),time.time_ns()));m['revision']+=1;self._setmeta(db,m)
        self.notify(sid);return after
    def undo(self,sid):
        with self.lock,self.db(sid) as db:
            r=db.execute('SELECT * FROM history WHERE undone=0 ORDER BY id DESC LIMIT 1').fetchone()
            if not r:return False
            before=json.loads(r['before']);after=json.loads(r['after'])
            if isinstance(before,list):
                for a in after:db.execute('DELETE FROM annotations WHERE id=?',(a['id'],))
                for a in before:db.execute('INSERT OR REPLACE INTO annotations VALUES(?,?)',(a['id'],dumps(a)))
            elif before:db.execute('INSERT OR REPLACE INTO annotations VALUES(?,?)',(before['id'],dumps(before)))
            elif after:db.execute('DELETE FROM annotations WHERE id=?',(after['id'],))
            db.execute('UPDATE history SET undone=1 WHERE id=?',(r['id'],));m=self._meta(db);m['revision']+=1;self._setmeta(db,m);self._event(db,'annotation_undo',{'history_id':r['id']})
        self.notify(sid);return True
    def history(self,sid):
        with self.db(sid) as db:return [dict(r,before=json.loads(r['before']),after=json.loads(r['after'])) for r in db.execute('SELECT * FROM history ORDER BY id DESC')]
    def save_result(self,sid,kind,result):
        rid=uid()
        with self.lock,self.db(sid) as db:
            db.execute('INSERT INTO results VALUES(?,?,?,?)',(rid,kind,dumps(result),time.time_ns()));m=self._meta(db);m['revision']+=1;self._setmeta(db,m)
        self.notify(sid);return rid
    def results(self,sid):
        with self.db(sid) as db:return [dict(r,data=json.loads(r['data'])) for r in db.execute('SELECT * FROM results ORDER BY wall_ns DESC')]
    def settings(self,value=None):
        if value is not None:atomic(self.settings_path,{**self.settings(),**value})
        return json.loads(self.settings_path.read_text())
    def labels(self,value=None):
        path=self.root/'labels.json'
        if value is not None:
            if not isinstance(value,list):raise ValueError('Labels must be a list')
            for v in value:v.setdefault('id',uid())
            atomic(path,value)
        return json.loads(path.read_text())
    def export_session(self,sid):
        import tempfile,shutil
        self.path(sid)
        # SQLite online backup captures one consistent revision without taking the
        # acquisition lock. Compression and hashing never block another session.
        folder=self.root/'exports';folder.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            snap=Path(td)/'session.sqlite'
            with self.db(sid) as source,sqlite3.connect(snap) as dest:source.backup(dest)
            with sqlite3.connect(snap) as db:
                m=json.loads(db.execute('SELECT value FROM meta WHERE key="session"').fetchone()[0])
                annotations=[json.loads(r[0]) for r in db.execute('SELECT data FROM annotations')]
            if m['status'] in ('armed','recording'):raise ValueError('Stop recording before exporting a complete session')
            out=folder/f'{sid}-r{m["revision"]}.zip'
            if out.exists():return out
            files={'metadata.json':dumps(m).encode(),'annotations.json':dumps(annotations).encode(),'labels.json':dumps(m.get('label_set',[])).encode(),'README.txt':b'EEG Explorer portable archive. SQLite contains original samples, packet events, batch provenance, annotations and history, gaps and derived results. Units and timing limitations are in metadata.json. Import with EEG Explorer; sqlite3 or Python can inspect without the app.'}
            hashes={k:hashlib.sha256(v).hexdigest() for k,v in files.items()};h=hashlib.sha256()
            with snap.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1048576),b''):h.update(chunk)
            hashes['session.sqlite']=h.hexdigest();tmp=out.with_name(out.name+'.'+uid()+'.tmp')
            with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
                info=zipfile.ZipInfo('session.sqlite');info.compress_type=zipfile.ZIP_DEFLATED
                with z.open(info,'w',force_zip64=True) as dest,snap.open('rb') as src:shutil.copyfileobj(src,dest)
                for k,v in files.items():z.writestr(zipfile.ZipInfo(k),v,compress_type=zipfile.ZIP_DEFLATED)
                z.writestr(zipfile.ZipInfo('checksums.json'),dumps(hashes),compress_type=zipfile.ZIP_DEFLATED)
            os.replace(tmp,out)
        return out
    def import_zip(self,path):
        import tempfile,shutil
        with zipfile.ZipFile(Path(path).expanduser()) as z:
            info={i.filename:i for i in z.infolist()}
            required={'checksums.json','metadata.json','session.sqlite','annotations.json','labels.json','README.txt'}
            if set(info)!=required or any(i.file_size>8*1024**3 for i in info.values()):raise ValueError('Unsupported or oversized session archive')
            hashes=json.loads(z.read('checksums.json'))
            for name in required-{'checksums.json'}:
                h=hashlib.sha256()
                with z.open(name) as f:
                    for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
                if h.hexdigest()!=hashes.get(name):raise ValueError('Archive integrity mismatch: '+name)
            meta=json.loads(z.read('metadata.json'));sid=meta['id']
            if not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid imported ID')
            dest=self.root/'sessions'/sid
            if dest.exists():raise ValueError('Session already exists; import into another library to review conflicting revisions. No changes overwritten.')
            with tempfile.TemporaryDirectory(dir=self.root) as td:
                snap=Path(td)/'session.sqlite'
                with z.open('session.sqlite') as src,snap.open('wb') as out:shutil.copyfileobj(src,out)
                with sqlite3.connect(snap) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity check failed')
                    actual=json.loads(db.execute('SELECT value FROM meta WHERE key="session"').fetchone()[0])
                    if actual['id']!=sid or actual.get('revision')!=meta.get('revision'):raise ValueError('Metadata disagrees with database')
                # Publish only the fully copied, validated database. A crash or
                # full disk during extraction cannot leave a partial session at
                # its permanent ID and prevent the user from retrying import.
                staged=Path(td)/'publish';staged.mkdir();os.replace(snap,staged/'session.sqlite')
                with self.lock:
                    if dest.exists():raise ValueError('Session already exists; no imported changes overwritten.')
                    try:os.rename(staged,dest)
                    except FileExistsError:raise ValueError('Session already exists; no imported changes overwritten.') from None
        return self.session(sid)
