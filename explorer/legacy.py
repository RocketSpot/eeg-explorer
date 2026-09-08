"""Explicit legacy raw NDJSON import. Never imports personal Focus Room guest records."""
import gzip,hashlib,json,math,time
from pathlib import Path

def import_legacy(store,path):
    path=Path(path).expanduser().resolve()
    if not path.is_file() or not (path.name.endswith('.ndjson') or path.name.endswith('.ndjson.gz')):raise ValueError('Import a portable Explorer ZIP or Focus Room eeg.raw*.ndjson[.gz] file')
    op=gzip.open if path.suffix=='.gz' else open;config={};sid=None;offset=None;last=None;count=0;hasher=hashlib.sha256();pending=[];simulation=None;wall_available=True
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):hasher.update(chunk)
    try:
        with op(path,'rt') as f:
            for line in f:
                if not line.strip():continue
                data=json.loads(line)
                if not isinstance(data,dict):raise ValueError('Legacy messages must be objects')
                kind=data.get('type','')
                if kind=='eeg/config-v1':config=data
                if kind!='eeg/raw-v1':
                    if kind in ('eeg/config-v1','eeg/quality-v1','eeg/raw-capture-v1','eeg/annotation-v1'):
                        if sid:store.event(sid,'legacy_message',{'original':data})
                        else:pending.append(data)
                    continue
                rate=data.get('sdkReportedSampleRateHz') or config.get('sdkReportedSampleRateHz') or data.get('expectedHardwareSampleRateHz')
                if not rate:raise ValueError('Legacy data has no sampling configuration; refusing to invent one')
                labels=data.get('channelLabels') or config.get('channelLabels')
                samples=data.get('samples')
                if not isinstance(labels,list) or not labels or not isinstance(samples,list) or len(labels)!=len(samples) or len(set(labels))!=len(labels):raise ValueError('Unsupported or mismatched legacy raw channel layout')
                synthetic=data.get('simulation',config.get('simulation'))
                if synthetic not in (True,False,None):raise ValueError('Legacy simulation metadata must be a boolean')
                if data.get('simulation') is not None and config.get('simulation') is not None and data['simulation']!=config['simulation']:raise ValueError('Legacy raw and configuration disagree about simulation provenance')
                if simulation is not None and synthetic is not None and synthetic!=simulation:raise ValueError('Legacy data changes simulation provenance; import each stream separately')
                if simulation is None:simulation=synthetic
                source_units=config.get('units')
                units='ADC counts' if source_units in ('ADC counts','counts','adc_counts','raw_adc_counts','adc_counts_unverified_sdk_units') else (source_units or 'unknown legacy units')
                if sid is None:
                    meta=store.create({'title':'Imported simulated recording' if simulation else 'Imported raw Zone recording','source':'simulation' if simulation else 'legacy_import','sample_rate':rate,'units':units,'acquisition':{'legacy_config':config,'source_units':source_units,'simulation':simulation,'source_kind':'legacy_import','input_sha256':hasher.hexdigest(),'warning':'Raw packet bytes, exact hardware sample times, absolute wall timestamps, participant identity and human annotations may be absent. Focus Room raw-v1 rounds values to one decimal. No missing values reconstructed.'}});sid=meta['id']
                    for message in pending:store.event(sid,'legacy_message',{'original':message})
                    pending=[]
                relative=data.get('monotonicReceiveTimestamp')
                if relative is None or not math.isfinite(float(relative)):raise ValueError('Legacy recording lacks finite receive timestamps')
                if offset is None:offset=float(relative)
                if last is not None and float(relative)<last:raise ValueError('Legacy receive time reset: import each stream segment separately')
                last=float(relative);cont=data.get('continuity',{});counts=cont.get('deviceCounters') or {};holes=cont.get('deviceHoles') or {}
                continuity={}
                for dev,ear in [('dev1','left'),('dev2','right')]:
                    c=counts.get(ear,counts.get(dev,{}));continuity[dev]={**c,'holes':holes.get(ear,holes.get(dev,[]))}
                wall_ms=data.get('wallMs');known_wall=wall_ms is not None and math.isfinite(float(wall_ms));wall_available=wall_available and known_wall
                store.ingest(sid,{'channels':{name:values for name,values in zip(labels,samples) if values is not None},'sample_rate':float(rate),'received_monotonic_ns':int((float(relative)-offset+1000)*1e9),'received_wall_ns':int(float(wall_ms)*1e6) if known_wall else None,'received_wall_time_available':known_wall,'legacy_monotonic_origin':'synthetic 1000 second anchor preserving source-relative receive times','continuity':continuity,'legacy_original':data,'simulation':simulation,'units':units});count+=1
        if not sid:raise ValueError('No supported eeg/raw-v1 samples were found')
        with store.db(sid) as db:
            meta=store._meta(db);meta['acquisition']['absolute_wall_time_available']=wall_available
            meta['timing']='Legacy relative receive timeline retained with a synthetic monotonic origin. Device counters and gaps preserved where present. Absolute wall time is unavailable unless explicitly recorded; unavailable received_wall_ns fields are null.'
            store._setmeta(db,meta)
        store.event(sid,'legacy_import',{'input_sha256':hasher.hexdigest(),'batches':count,'metadata_warning':'Source samples and each original raw-v1 message retained; no inferred human ground truth.'})
        return store.stop(sid)
    except Exception as e:
        if sid:
            store.event(sid,'import_incomplete',{'reason':str(e),'batches_preserved':count})
            # Keep committed samples available, but do not publish a failed import
            # as a complete experiment or automatically queue it for sync.
            with store.db(sid) as db:
                meta=store._meta(db);meta.update(status='import_incomplete',import_error=str(e),revision=meta['revision']+1)
                if not wall_available:meta['first_sample_wall_ns']=None
                store._setmeta(db,meta)
            store.clocks.pop(sid,None)
        raise
