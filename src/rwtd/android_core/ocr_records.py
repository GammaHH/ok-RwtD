"""One OCR evidence policy for every selected engine and all RwtD consumers.

Records describe the public OCR return value, not hidden model tensors. Never
lower engine thresholds or change return objects to collect extra evidence.
"""
import copy
import json
import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime,timezone

MAX_TOKENS=128
MAX_TEXT=512
MAX_RAW=16384
MAX_RECORDS=32
_init_lock=threading.Lock()


def box_tokens(boxes):
    result=[]
    for box in boxes:
        score=getattr(box,'confidence',None)
        try:score=float(score) if score is not None else None
        except (TypeError,ValueError):score=None
        token={'text':getattr(box,'name',None),'confidence':score}
        if all(hasattr(box,k) for k in ('x','y','width','height')):
            token['bbox']=[box.x,box.y,box.width,box.height]
        result.append(token)
    return result


def _safe(value):
    if value is None or type(value) in (bool,int):return value
    if isinstance(value,str):return value[:MAX_TEXT]
    if type(value) is float:return value if math.isfinite(value) else None
    if isinstance(value,(list,tuple)):return [_safe(v) for v in value[:8]]
    return str(value)[:MAX_TEXT]


def describe(tokens,*,roi=None,configured_roi=None,threshold=None,route=None,
             engine=None,result=None,value=None,reason=None,frame_id=None,node=None):
    valid=isinstance(tokens,(list,tuple))
    source=tokens if valid else []
    records=[]
    for token in source[:MAX_TOKENS]:
        if isinstance(token,dict):records.append({k:_safe(token.get(k)) for k in ('text','confidence','bbox') if k in token})
        else:records.append({'text':None,'confidence':None,'invalid_token':True})
    raw=' '.join(token['text'] for token in records if isinstance(token.get('text'),str))
    truncated=(len(source)>MAX_TOKENS or len(raw)>MAX_RAW
               or any(isinstance(t,dict) and isinstance(t.get('text'),str) and len(t['text'])>MAX_TEXT for t in source[:MAX_TOKENS]))
    return {'schema_version':1,'raw_text':raw[:MAX_RAW],'tokens':records,
        'token_count':len(source),'tokens_truncated':truncated,
        'configured_roi':copy.deepcopy(configured_roi),'roi':copy.deepcopy(roi),
        'threshold':_safe(threshold),'route':route,'engine':engine,
        'state':'invalid' if not valid else 'empty' if not source else 'returned',
        'result':_safe(result),'value':_safe(value),'reason':_safe(reason),
        'frame_id':frame_id,'node':node}


def attach(result,tokens,**context):
    """Add diagnostics only; preserve the caller's parsing and routing fields."""
    status=result.get('result',result.get('state'))
    record=describe(tokens,result=status,value=result.get('value',result.get('number')),
        reason=result.get('reason'),**context)
    result['ocr_record']=record
    result['raw_text']=record['raw_text']
    return result


def publish(task,record,frame=None):
    """Bounded memory + same-frame failure evidence; verbose text is DEBUG only."""
    try:
        item=copy.deepcopy(record)
        item.setdefault('record_id',uuid.uuid4().hex)
        item.setdefault('captured_utc',datetime.now(timezone.utc).isoformat())
        if not hasattr(task,'_ocr_records_lock'):
            with _init_lock:
                if not hasattr(task,'_ocr_records_lock'):
                    task._ocr_records_lock=threading.RLock();task._ocr_records=deque(maxlen=MAX_RECORDS)
        with task._ocr_records_lock:task._ocr_records.append(item)
        evidence=getattr(task,'failure_evidence',None)
        if evidence is not None and frame is not None:evidence.record_ocr(frame,item)
        if getattr(task,'config',{}).get('Detailed Logging',False):
            logger=getattr(task,'log_debug',None)
            if callable(logger):logger('RWTD_OCR '+json.dumps(item,ensure_ascii=False,allow_nan=False))
    except Exception:pass


def publish_result(task,tokens,frame,**context):
    record=describe(tokens,**context);record['stage']='parsed'
    publish(task,record,frame)
    return record


def capture(task,invoke,args,kwargs,*,configured_roi=None,context=None):
    """Exactly one engine call; diagnostics never replace its result/exception."""
    started=time.monotonic();returned=None;error=None
    try:
        returned=invoke(*args,**kwargs)
        return returned
    except Exception as exc:
        error=exc
        raise
    finally:
        try:
            box=kwargs.get('box');roi=[box.x,box.y,box.width,box.height] if box is not None else None
            alias=kwargs.get('lib','default');engine=alias
            try:
                config=getattr(getattr(task,'executor',None),'config',{})
                engine=(config.get('ocr') or {}).get(alias,{}).get('lib') or alias
            except (AttributeError,TypeError):pass
            raw=box_tokens(returned) if isinstance(returned,(list,tuple)) else None
            record=describe(raw,roi=roi,configured_roi=configured_roi,
                threshold=kwargs.get('threshold') or getattr(task,'ocr_default_threshold',None),
                route=(context or {}).get('route','ocr'),engine=engine,
                node=(context or {}).get('node'),frame_id=(context or {}).get('frame_id'),
                reason=(type(error).__name__+': '+str(error)) if error else None)
            record.update(record_id=uuid.uuid4().hex,engine_alias=alias,
                captured_utc=datetime.now(timezone.utc).isoformat(),elapsed_seconds=round(time.monotonic()-started,6))
            record['stage']='engine'
            publish(task,record,kwargs.get('frame'))
        except Exception:pass
