"""Task-local shared OCR presets; countdown consistency uses capture timestamps."""
import copy
import json
import math
import re
import unicodedata
import threading
from pathlib import Path

PRESETS={
    'ProgressKM':{'label':'Progress KM','type':'number','roi':[330,370,156,55],'threshold':.7},
    'RetreatableKM':{'label':'Retreatable KM','type':'number','roi':[330,370,156,55],'threshold':.7},
    'ExpeditionTime':{'label':'Expedition Time','type':'time','feature':'expedition_time','threshold':.7},
}


def numeric(value):
    return type(value) in (int,float) and math.isfinite(value) and 0<=value<=31536000


def countdown_sample(node,raw):
    from .ocr_records import attach
    sample=_countdown_sample(node,raw)
    attach(sample,raw.get('tokens'),roi=node['roi'],threshold=node['threshold'],
        route='shared_ocr_countdown',node=node['id'],frame_id=raw.get('frame_id'))
    sample['ocr_record']['stage']='parsed'
    return sample


def _countdown_sample(node,raw):
    """Select a unique complete HH:MM:SS token, never repair guessed digits."""
    from .canvas_ocr import classify
    from .ocr_time import parse_time,normalize_hms
    sample=classify(node,raw)
    tokens=raw.get('tokens',[])
    if not isinstance(tokens,list):tokens=[]
    if sample.get('verified') is True or sample.get('state')!='no_match':return sample
    candidates={};accepted_count=0
    for token in tokens:
        # classify already rejected malformed token dictionaries/confidences.
        if token['confidence']<node.get('threshold',.7):continue
        accepted_count+=1
        text=unicodedata.normalize('NFKC',token['text']).strip()
        if normalize_hms(text) is None:continue
        value=parse_time(text,'hms')
        if value is not None:candidates.setdefault(value,[]).append(token['text'])
    sample['time_candidates']=[{'seconds':value,'texts':texts} for value,texts in candidates.items()]
    if len(candidates)==1:
        value=next(iter(candidates))
        sample.update(value=value,verified=True,state='matched',matched_time_text=candidates[value][0],
                      reason='Unique complete countdown token; unrelated OCR text ignored')
    elif len(candidates)>1:
        sample.update(state='conflict',reason='Multiple different countdown values in OCR ROI')
    else:
        sample['reason']=('OCR returned no tokens' if not tokens else
                          'All OCR tokens below countdown confidence threshold' if not accepted_count else
                          'No valid complete HH:MM:SS token in OCR output')
    return sample


def expedition_cooldown_sample(sample,recognize,clause=None):
    """Missing countdown is ready only when this capture confirms expedition_done.

    This result is Guard-local: never publish completion as an OCR numeric value.
    An unconfirmed but readable countdown must still pass consistency sampling.
    """
    if (sample.get('verified') is True or numeric(sample.get('observed_seconds'))
            or sample.get('state')=='conflict' or sample.get('time_candidates')):
        return sample
    from .canvas_entry_guard import coco_allowed
    check=clause or {'kind':'coco','feature':'expedition_done','threshold':.8,'match':'detected'}
    result=copy.deepcopy(sample)
    try:
        matches=recognize(check)
        completed=coco_allowed(matches,check)
        result['completion_evidence']={'clause':copy.deepcopy(check),'matches':matches,
                                       'detected':completed}
        if completed is True:
            result.update(verified=True,value=None,expedition_completed=True,
                          reason='expedition_done confirmed on the same frame; no countdown cooldown armed')
    except Exception as exc:
        result['completion_evidence']={'detected':None,'reason':type(exc).__name__+': '+str(exc)[:180]}
    return result


class SharedOcr:
    def __init__(self,root):
        self.root=Path(root);self.lock=threading.RLock()
        self.latest={};self.candidates={};self.frames={}

    def node(self,key):
        spec=PRESETS[key];roi=spec.get('roi')
        if key=='ExpeditionTime':
            from .feature_settings import load
            roi=load(self.root)['features'].get(spec['feature'],{}).get('roi')
        if roi is None:
            data=json.loads((self.root/'assets/coco_annotations.json').read_text(encoding='utf-8-sig'))
            ids={c['id'] for c in data['categories'] if c['name']==spec['feature']}
            images={im['id']:im for im in data['images']}
            boxes={tuple(round(v*s) for v,s in zip(a['bbox'],
                       [1080/images[a['image_id']]['width'],2400/images[a['image_id']]['height']]*2))
                   for a in data['annotations'] if a['category_id'] in ids}
            if len(boxes)!=1:raise ValueError('Shared OCR preset requires one unambiguous annotated ROI: '+key)
            roi=list(next(iter(boxes)))
        return {'id':'Shared'+key,'kind':'ocr','output_key':key,'roi':list(roi),
                'threshold':spec['threshold'],'ocr_mode':'raw','ocr_output_type':spec['type'],
                **({'_progress_for':'shared'} if key in {'ProgressKM','RetreatableKM'} else {})}

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.latest)

    def accept(self,key,sample,captured_at,*,series='shared',attempt=1,limit=1):
        """Reject a jump once; a consistent following capture confirms its new baseline."""
        value=sample.get('value');result=copy.deepcopy(sample)
        result['captured_at']=captured_at
        with self.lock:
            candidate_key=(series,key)
            if attempt==1:self.candidates.pop(candidate_key,None)
            if key=='ExpeditionTime' and sample.get('verified') is True and numeric(value):
                previous=self.candidates.get(candidate_key)
                if not previous and limit!=1:
                    result.update(verified=False,value=None,state='invalid',observed_seconds=value,
                                  reason='Await another consistent countdown capture')
                if previous:
                    old_time,old_value=previous
                    delta=captured_at-old_time
                    if delta<0 or abs(value-old_value)>delta+10:
                        result.update(verified=False,value=None,state='invalid',
                            reason='Countdown jump exceeds capture time difference + 10 seconds',
                            observed_seconds=value,previous_seconds=old_value,
                            capture_delta_seconds=delta,tolerance_seconds=max(0,delta)+10)
                self.candidates[candidate_key]=(captured_at,value)
                if len(self.candidates)>10000:self.candidates.pop(next(iter(self.candidates)))
            elif key=='ExpeditionTime':
                result.update(verified=False,value=None)
            if 'ocr_record' in result:
                result['ocr_record']['validation']={'verified':result.get('verified'),
                    'value':result.get('value'),'reason':result.get('reason')}
            self.latest[key]=result
            return copy.deepcopy(result)

    def read(self,key,frame_id,captured_at,read_tokens,*,series='shared',attempt=1,limit=1):
        from .canvas_ocr import classify
        cache_key=(series,key)
        with self.lock:
            cached=self.frames.get(cache_key)
            if cached and cached[0]==(frame_id,attempt):return copy.deepcopy(cached[1])
        # Do not serialize unrelated Observer OCR calls behind this cache lock.
        try:
            node=self.node(key)
            raw={'verified':True,'frame_id':frame_id,'source':'REAL_ANDROID','tokens':read_tokens(node)}
            sample=countdown_sample(node,raw) if key=='ExpeditionTime' else classify(node,raw)
            sample.update(tokens=raw['tokens'],roi=node['roi'])
        except Exception as exc:
            sample={'verified':False,'value':None,'reason':type(exc).__name__+': '+str(exc)[:180]}
        with self.lock:
            result=self.accept(key,sample,captured_at,series=series,attempt=attempt,limit=limit)
            self.frames[cache_key]=((frame_id,attempt),result)
            if len(self.frames)>10000:self.frames.pop(next(iter(self.frames)))
            return copy.deepcopy(result)
