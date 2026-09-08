from __future__ import annotations
import json, math, queue, random, threading, time
from collections import defaultdict,deque
from .store import Store,uid

class Controller:
    def __init__(self,root,hardware_class=None,sync_class=None):
        self.store=Store(root);self.lock=threading.RLock();self.recording={'armed':False,'session_id':None};self.recording_windows=[];self.active_session=None
        self.section=None;self.pending=None;self.selected_label=None;self.scope='both';self.live=defaultdict(lambda:deque(maxlen=5000));self.live_origin=None
        self.errors=deque(maxlen=20);self.recent_events=deque(maxlen=100);self.q=queue.Queue();self.running=True;self.protocol=None
        if hardware_class is None:
            from .acquisition import Hardware
            hardware_class=Hardware
        self.hardware=hardware_class(self.on_batch,self.on_event)
        if sync_class is None:
            from .sync import SyncManager
            sync_class=SyncManager
        self.sync=sync_class(self.store);self.store.changed=self.sync.enqueue
        self.worker=threading.Thread(target=self._writer,daemon=True);self.worker.start()
        self.timer=threading.Thread(target=self._timer,daemon=True);self.timer.start()
    def on_batch(self,batch):self.q.put(('batch',batch))
    def on_event(self,event):self.q.put(('event',event))
    def flush(self):self.q.join()
    def flush_acquisition(self):
        if hasattr(self.hardware,'flush'):self.hardware.flush()
        self.flush()
    def _receipt_ns(self,kind,item):
        return int(item.get('received_monotonic_ns') or (item.get('details') or {}).get('received_monotonic_ns') or item.get('monotonic_ns') or time.monotonic_ns())
    def _recording_for_receipt(self,receipt):
        for window in reversed(self.recording_windows):
            if receipt>=window['armed_monotonic_ns'] and (window.get('stopped_monotonic_ns') is None or receipt<=window['stopped_monotonic_ns']):
                return window['session_id']
        return None
    def _writer(self):
        while self.running or not self.q.empty():
            try:kind,item=self.q.get(timeout=.1)
            except queue.Empty:continue
            try:
                with self.lock:
                    receipt=self._receipt_ns(kind,item)
                    sid=self._recording_for_receipt(receipt)
                    if kind=='batch':
                        if sid:self.store.ingest(sid,item)
                        fs=item['sample_rate'];mono=item.get('received_monotonic_ns',time.monotonic_ns())/1e9
                        if self.live_origin is None:self.live_origin=mono-max([len(v) for v in item['channels'].values()] or [0])/fs
                        for ch,values in item['channels'].items():
                            for i,v in enumerate(values):
                                t=mono-(len(values)-i-1)/fs-self.live_origin
                                self.live[ch].append({'channel':ch,'t':t,'value':v,'idx':int(t*fs)})
                    else:
                        if item.get('type')!='packet':self.recent_events.append(item)
                        if sid:
                            meta=self.store.session(sid);origin=meta.get('first_sample_monotonic_ns')
                            event_t=max(0,(receipt-origin)/1e9) if origin is not None else None
                            self.store.event(sid,item.get('type','hardware'),item,event_t)
            except Exception as e:
                self.errors.append('Acquisition write failed: '+str(e));self.recording['error']=str(e)
                # Never claim continued successful recording after a write failure.
                self.recording['armed']=False
                if self.recording_windows and self.recording_windows[-1].get('stopped_monotonic_ns') is None:
                    self.recording_windows[-1]['stopped_monotonic_ns']=time.monotonic_ns()
            finally:self.q.task_done()
    def boundary(self):
        sid=self.recording.get('session_id')
        return self.store.session(sid)['duration'] if sid else 0
    def state(self):
        with self.lock:
            m=self.store.session(self.recording['session_id']) if self.recording.get('session_id') else None
            if m and self.recording['armed']:
                rows=self.store.samples(m['id'],max(0,m['duration']-20),limit=10000)
                live=defaultdict(list)
                for r in rows:live[r['channel']].append(r)
            else:live={k:list(v)[-2500:] for k,v in self.live.items()}
            return {'hardware':self.hardware.status(),'recording':dict(self.recording),'active_session':m,'session':m,'live':{'channels':dict(live)},'section':self.section,'countdown':({**self.pending,'remaining':max(0,self.pending['deadline']-time.monotonic())} if self.pending else None),'selected_label':self.selected_label,'labels':self.store.labels(),'settings':self.store.settings(),'sync':self.sync.status(),'errors':list(self.errors),'events':list(self.recent_events),'protocol':self.protocol,'queue_depth':self.q.qsize(),'library_path':str(self.store.root),'latest_t':m['duration'] if m else 0}
    def _annotation(self,start,end,label_id=None,source='manual',scope=None,**extra):
        label=next((l for l in self.store.labels() if l['id']==label_id),{})
        return {'start':start,'end':end,'label_id':label_id,'label':label.get('name','Unnamed section'),'color':label.get('color','#94a3b8'),'placement':label.get('placement',''),'activity':label.get('activity',''),'issue':label.get('issue',''),'source':source,'reviewed':False,'needs_review':True,'scope':scope or self.scope,**extra}
    def _close_section(self,cancel=False):
        if self.section:
            a=self.section;self.section=None
            if not cancel and self.boundary()>a['start']:
                return self.store.annotate(self.recording['session_id'],self._annotation(a['start'],self.boundary(),a.get('label_id'),a.get('source','manual'),a.get('scope'),notes=a.get('notes','')))
        self.pending=None
    def _begin_section(self,label_id,delay=0,source='manual'):
        if not self.recording['armed']:raise ValueError('Start recording before marking a live section')
        if self.boundary()<=0:raise ValueError('Waiting for the first actual sample')
        if delay>0:
            self.pending={'deadline':time.monotonic()+delay,'label_id':label_id,'source':source};self.store.event(self.recording['session_id'],'countdown_started',{'seconds':delay},self.boundary());return
        label=next((l for l in self.store.labels() if l['id']==label_id),{})
        self.section={'start':self.boundary(),'label_id':label_id,'label':label.get('name','Unnamed section'),'source':source,'scope':self.scope}
    def _timer(self):
        while self.running:
            with self.lock:
                if self.pending and time.monotonic()>=self.pending['deadline']:
                    p=self.pending;self.pending=None
                    try:self._begin_section(p['label_id'],source=p['source']);self.store.event(self.recording['session_id'],'countdown_finished',{},self.boundary())
                    except Exception as e:self.errors.append(str(e))
                if self.protocol and self.protocol.get('running'):
                    self.protocol['remaining']=max(0,self.protocol['deadline']-time.monotonic())
                    if self.protocol['remaining']==0 and not self.protocol.get('awaiting_advance'):
                        # A scheduled instruction cannot establish that the action was performed.
                        self._protocol_close();self.protocol['awaiting_advance']=True
            time.sleep(.05)
    def _protocol_close(self):
        p=self.protocol
        if p and p.get('section_start') is not None:
            margin=p.get('transition_seconds',0);start=p['section_start']+margin;end=self.boundary()-margin
            if end>start:
                step=p['sequence'][p['index']];self.store.annotate(self.recording['session_id'],self._annotation(start,end,step.get('label_id'),'protocol',notes=step.get('action',step.get('name','Scheduled instruction'))))
            p['section_start']=None
    def _protocol_action(self,d):
        command=d.get('command')
        if command=='save':return self.store.settings({'protocol':d})
        if command=='stop':
            self._protocol_close()
            if self.protocol:self.protocol['running']=False
            return self.protocol
        if not self.recording['armed']:raise ValueError('Start recording before running an experiment')
        if command=='start':
            steps=d.get('steps',[])
            if not steps or len(steps)>200:raise ValueError('Add 1–200 experiment steps')
            seq=[];seed=int(d.get('seed',0));rng=random.Random(seed)
            for i in range(max(1,min(100,int(d.get('repetitions',1))))):
                block=[dict(x) for x in steps]
                if d.get('order')=='randomized':rng.shuffle(block)
                elif d.get('order')=='counterbalanced':block=block[i%len(block):]+block[:i%len(block)]
                seq.extend(block)
            for step in seq:
                step['duration']=float(step.get('duration',step.get('seconds',30)))
                if not 0<step['duration']<=86400:raise ValueError('Step duration must be positive')
            self.protocol={'running':True,'sequence':seq,'index':-1,'transition_seconds':max(0,float(d.get('transition_seconds',1))),'seed':seed}
        p=self.protocol
        if not p or not p.get('running'):raise ValueError('No experiment is running')
        self._protocol_close();p['index']+=1
        if p['index']>=len(p['sequence']):p['running']=False;return p
        step=p['sequence'][p['index']];p.update(current=step,next=p['sequence'][p['index']+1] if p['index']+1<len(p['sequence']) else None,deadline=time.monotonic()+step['duration'],remaining=step['duration'],awaiting_advance=False,section_start=self.boundary())
        self.store.event(self.recording['session_id'],'protocol_instruction',{'step':step,'index':p['index'],'performed_confirmed':False,'seed':p['seed']},self.boundary())
        return p
    def action(self,d):
        action=d.get('action');action_monotonic_ns=time.monotonic_ns();action_wall_ns=time.time_ns()
        if action=='record_start':
            # Bind received samples to an explicit host-clock interval. A queued
            # pre-arm notification cannot become part of a new recording merely
            # because its disk writer ran after the user pressed Record.
            with self.lock:
                if self.recording['armed'] or self.recording.get('stopping'):raise ValueError('Already recording or finishing a recording')
                armed=time.monotonic_ns();hw=self.hardware.status();source=hw.get('source',hw.get('mode','unknown'))
                m=self.store.create({'title':d.get('title') or 'Untitled experiment','participant':d.get('participant',''),'source':source,'acquisition':hw})
                window={'session_id':m['id'],'armed_monotonic_ns':armed,'stopped_monotonic_ns':None}
                self.recording_windows.append(window);self.recording={'armed':True,**window};self.active_session=m['id']
                self.store.event(m['id'],'recording_armed',{'server_monotonic_ns':armed,'server_wall_ns':time.time_ns(),'client_wall_ms':d.get('client_wall_ms')})
                return m
        if action=='record_stop':
            with self.lock:
                if not self.recording['armed']:raise ValueError('Recording is not active')
                sid=self.recording['session_id'];stopped=time.monotonic_ns()
                self.recording_windows[-1]['stopped_monotonic_ns']=stopped
                self.recording.update(armed=False,stopping=True,stopped_monotonic_ns=stopped)
                if self.protocol:self.protocol['running']=False
            self.flush_acquisition()
            with self.lock:
                self._close_section();self._protocol_close()
                meta=self.store.session(sid);origin=meta.get('first_sample_monotonic_ns')
                self.store.event(sid,'recording_stop_requested',{'server_monotonic_ns':stopped,'server_wall_ns':time.time_ns(),'client_wall_ms':d.get('client_wall_ms')},max(0,(stopped-origin)/1e9) if origin else None)
                result=self.store.stop(sid);self.recording['stopping']=False
                return result
        if action in ('scan','connect','disconnect','simulate'):
            if action in ('connect','simulate') and (self.recording['armed'] or self.recording.get('stopping')):raise ValueError('Stop recording before changing the data source')
            if action=='scan':return self.hardware.scan()
            if action=='connect':return self.hardware.connect(d.get('request',d))
            if action=='simulate':
                self.live.clear();self.live_origin=None;return self.hardware.start_simulation(d)
            return self.hardware.disconnect()
        if action in ('analyze','train','compare','candidate','export_model'):
            from . import analysis
            return getattr(analysis,action)(d.get('request',d)) if action=='candidate' else getattr(analysis,action)(self.store,d.get('request',d))
        if action=='sync_config':return self.sync.configure(d.get('config',d))
        if action=='sync_retry':return self.sync.retry()
        if action=='sync_restore':return self.sync.restore(d['session_id'],int(d['revision']))
        if action=='focus_compare':
            from .focus_compare import compare_focus
            return compare_focus(self.store,d.get('request',d))
        if action=='import':
            if str(d['path']).lower().endswith('.zip'):return self.store.import_zip(d['path'])
            from .legacy import import_legacy
            return import_legacy(self.store,d['path'])
        # Let already-received acquisition events reach disk before assigning annotation boundaries.
        self.flush_acquisition()
        with self.lock:
            sid=d.get('session_id') or self.recording.get('session_id') or self.active_session
            if sid and action in ('section_toggle','select_label','cancel_section','mark_previous','marker'):
                meta=self.store.session(sid);origin=meta.get('first_sample_monotonic_ns')
                event_t=max(0,(action_monotonic_ns-origin)/1e9) if origin is not None else None
                self.store.event(sid,'annotation_action_received',{'action':action,'server_monotonic_ns':action_monotonic_ns,'server_wall_ns':action_wall_ns,'client_wall_ms':d.get('client_wall_ms'),'sample_boundary':self.boundary(),'timing':'host action receipt; sample boundary is the latest committed received sample'},event_t)
            if action=='open':self.active_session=d['session_id'];return self.store.session(self.active_session)
            if action=='labels':return self.store.labels(d['labels'])
            if action=='settings':return self.store.settings(d['settings'])
            if action=='select_label':
                self.selected_label=d.get('label_id');self.scope=d.get('scope',self.scope)
                if self.section:self._close_section();self._begin_section(self.selected_label)
                return {'selected_label':self.selected_label}
            if action=='section_toggle':
                self.store.event(sid,'annotation_keypress',{'action':action,'client_wall_ms':d.get('client_wall_ms'),'server_monotonic_ns':action_monotonic_ns,'server_wall_ns':action_wall_ns},event_t)
                self.scope=d.get('scope',self.scope)
                if self.section or self.pending:return self._close_section()
                self._begin_section(d.get('label_id',self.selected_label),float(d.get('countdown',self.store.settings().get('countdown',0))));return self.section
            if action=='cancel_section':
                self._close_section(cancel=True);return {'cancelled':True}
            if action=='annotation':return self.store.annotate(sid,d['annotation'])
            if action=='annotation_delete':return self.store.delete_annotation(sid,d['id'])
            if action=='undo':return self.store.undo(sid)
            if action=='mark_previous':
                end=self.boundary();seconds=float(d.get('seconds',self.store.settings()['previous_seconds']))
                return self.store.annotate(sid,self._annotation(max(0,end-seconds),end,self.selected_label))
            if action=='marker':
                self.store.event(sid,'marker',{'label':d.get('label','Event marker'),'source':'manual','server_monotonic_ns':action_monotonic_ns,'server_wall_ns':action_wall_ns,'client_wall_ms':d.get('client_wall_ms')},float(d.get('t',self.boundary())));return {'saved':True}
            if action=='annotation_split':
                a=next(a for a in self.store.annotations(sid) if a['id']==d['id']);t=float(d['t'])
                if not a['start']<t<a['end']:raise ValueError('Split point must be inside the section')
                b={**a,'id':uid(),'start':t};a['end']=t;return self.store.edit_annotations(sid,[a,b],[])
            if action=='annotation_merge':
                rows=sorted([a for a in self.store.annotations(sid) if a['id'] in d['ids']],key=lambda a:a['start'])
                if len(rows)<2:raise ValueError('Select at least two sections to merge')
                if any(a.get('label_id')!=rows[0].get('label_id') or a.get('scope')!=rows[0].get('scope') or a['source']!=rows[0]['source'] for a in rows):raise ValueError('Merged sections must have the same label, scope and source')
                if any(b['start']>a['end']+1e-6 for a,b in zip(rows,rows[1:])):raise ValueError('Sections must touch or overlap; unlabeled data will not be filled silently')
                rows[0]['end']=max(a['end'] for a in rows);return self.store.edit_annotations(sid,[rows[0]],[a['id'] for a in rows[1:]])
            if action=='protocol':return self._protocol_action(d)
            raise ValueError('Unknown action: '+str(action))
    def close(self):
        self.hardware.close();self.flush()
        with self.lock:
            if self.recording['armed']:self._close_section();self.recording['armed']=False;self.store.stop(self.recording['session_id'])
        self.running=False;self.worker.join(timeout=5);self.sync.close()
