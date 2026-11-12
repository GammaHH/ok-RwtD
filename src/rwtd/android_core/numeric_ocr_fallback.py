"""Recognition-only fallback for an empty, small fixed number/time OCR region.

Uses the selected engine and existing frame under the caller's OCR lock. Never
captures, changes engine thresholds, or bypasses the number/time parser.
"""
import json
import math
import re
import unicodedata


def eligible(node):
    from .canvas_ocr import output_type
    return (isinstance(node,dict) and node.get('ocr_mode','dict')=='raw'
            and output_type(node) in {'number','time'} and node.get('ocr_region','fixed')=='fixed'
            and not node.get('_progress_for'))


def recognize(task, frame, roi, node, threshold, context, box_factory):
    """Return one accepted Box or []; invalid/unsupported reads stay empty."""
    from .ocr_records import describe,publish
    from .canvas_ocr import read_value,output_type,numeric_text
    is_time=isinstance(node,dict) and output_type(node)=='time'
    tokens=[];value=None;reason='unsupported_engine';accepted=False
    try:
        alias='default'
        config=task.executor.config.get('ocr',{})
        selected=config.get(alias,config)
        if not isinstance(selected,dict) or selected.get('lib')!='onnxocr':return []
        if frame is None or not isinstance(roi,(list,tuple)) or len(roi)!=4:
            reason='missing_frame_or_roi';return []
        x,y,w,h=roi
        if (any(type(v) is not int for v in roi) or x<0 or y<0
                or not 0<w<=512 or not 0<h<=256 or w*h>65536
                or x+w>frame.shape[1] or y+h>frame.shape[0]):
            reason='roi_not_small_or_out_of_bounds';return []
        if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0<threshold<=1:
            reason='invalid_threshold';return []
        engine=task.executor.ocr_lib(alias)
        reader=getattr(engine,'text_recognizer',None)
        if not callable(reader):return []
        raw=reader([frame[y:y+h,x:x+w].copy()])
        if not isinstance(raw,(list,tuple)) or len(raw)!=1:
            reason='ambiguous_result';return []
        pair=raw[0]
        if not isinstance(pair,(list,tuple)) or len(pair)!=2 or not isinstance(pair[0],str):
            reason='invalid_result';return []
        text,confidence=pair[0],float(pair[1])
        tokens=[{'text':text,'confidence':confidence,'bbox':list(roi)}]
        if not math.isfinite(confidence) or not threshold<=confidence<=1:
            reason='below_threshold';return []
        normalized=unicodedata.normalize('NFKC',text).strip()
        # Countdown fallback uses the existing strict time parser. Expedition
        # requires all three fields; never accept a truncated MM:SS crop as HMS.
        if is_time:
            if node.get('output_key')=='ExpeditionTime':
                from .ocr_time import parse_time,normalize_hms
                value=parse_time(normalized,'hms') if normalize_hms(normalized) else None
            else:
                value=read_value(node,[text])
        elif not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?',numeric_text(node,normalized)):
            reason='not_a_complete_number';return []
        else:
            value=read_value(node,[text])
        if type(value) not in (int,float) or not math.isfinite(value):
            reason='time_conversion_rejected' if is_time else 'numeric_conversion_rejected';return []
        result=box_factory(x,y,w,h,confidence,text)
        accepted=True;reason='recognized_without_text_detection'
        return [result]
    except Exception as exc:
        # Optional fallback must not replace the original empty-read semantics.
        reason='fallback_error:'+type(exc).__name__
        return []
    finally:
        try:
            record=describe(tokens,roi=roi,configured_roi=roi,threshold=threshold,
                            route='fixed_time_fallback' if is_time else 'fixed_number_fallback',engine='onnxocr_recognition_only',
                            result='accepted' if accepted else 'rejected',value=value,reason=reason,
                            node=(context or {}).get('node'),frame_id=(context or {}).get('frame_id'))
            record['stage']='fallback'
            publish(task,record,frame)
            log=getattr(task,'log_info',None)
            if callable(log):
                log(('RWTD_OCR_TIME_FALLBACK ' if is_time else 'RWTD_OCR_NUMBER_FALLBACK ')+json.dumps({
                    'node':record['node'],'roi':roi,'threshold':threshold,'result':record['result'],
                    'tokens':record['tokens'],'value':record['value'],'reason':reason},ensure_ascii=False,allow_nan=False))
        except Exception:pass
