"""OCR localization on the current recognition frame; never injects input."""
import math
from .step_conditions import ConditionUnknown
from .workflow_model import validate_roi


def validate(step):
    if 'ocr_target' not in step:return
    target=step['ocr_target']
    if (not isinstance(target,dict) or set(target)-{'text','match'}
            or not isinstance(target.get('text'),str) or not target['text'].strip()
            or len(target['text'])>200 or target.get('match','exact') not in {'exact','contains'}):
        raise ValueError('OCR target requires text and exact/contains matching')
    if (step.get('name') not in {'TapTest','LongPressTest','SwipeCalibration'}
            or step.get('features') or step.get('feature') or step.get('color_mode')
            or step.get('target_policy','single') not in {'single','unique_only','first_top_to_bottom','first_bottom_to_top','all_top_to_bottom'}
            or step.get('name')=='SwipeCalibration' and step.get('target_policy')=='all_top_to_bottom'
            or step.get('recognize_before',True) is not True
            or step.get('allow_unconfirmed_target') is True):
        raise ValueError('OCR target requires exclusive recognized single-target input')


def select(step, boxes, roi, *, multiple=False):
    """Require one distinct qualified box. Invalid/ambiguous output is UNKNOWN."""
    target=step['ocr_target'];expected=target['text'].strip()
    rx,ry,rw,rh=validate_roi(roi)
    hits=[];tokens=[]
    if boxes is None:raise ConditionUnknown('OCR returned no valid result')
    for box in boxes:
        try:
            text=box.name;score=float(box.confidence)
            if not isinstance(text,str) or not math.isfinite(score) or not 0<=score<=1:
                raise ValueError('Invalid OCR text/confidence')
            bbox=[box.x,box.y,box.width,box.height]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in bbox):
                raise ValueError('Invalid OCR coordinates')
            bbox=[int(round(v)) for v in bbox];validate_roi(bbox)
        except (AttributeError,ValueError,TypeError) as exc:
            raise ConditionUnknown('OCR target output invalid: '+str(exc)) from exc
        hit={'feature':'__ocr__','text':text,'confidence':score,'bbox':bbox,'matched':False}
        matched=expected==text.strip() if target.get('match','exact')=='exact' else expected in text
        x,y,w,h=bbox
        hit['matched']=bool(matched and score>=step.get('threshold',.9)
                            and x>=rx and y>=ry and x+w<=rx+rw and y+h<=ry+rh)
        tokens.append(hit)
        if hit['matched'] and not any(old['bbox']==bbox for old in hits):hits.append(hit)
    from .target_order import POSITION_POLICIES,choose
    if not multiple and step.get('target_policy') in POSITION_POLICIES:
        hits=choose(hits,step['target_policy'])
    if len(hits)>1 and not multiple:raise ConditionUnknown('Multiple OCR targets matched; narrow ROI')
    return (hits if multiple else hits[0] if hits else None),tokens


def locate(task,frame,step,*,multiple=False):
    from ok import Box
    roi=step.get('coco_roi') or [0,0,1080,2400]
    effective=getattr(task,'recognition_roi',None)
    search=effective(roi,'ocr') if callable(effective) else roi
    try:
        boxes=task.ocr(box=Box(*roi),frame=frame,threshold=step.get('threshold',.9))
    except Exception as exc:
        raise ConditionUnknown('OCR target recognition failed: '+type(exc).__name__) from exc
    from .ocr_records import box_tokens,publish_result
    raw=box_tokens(boxes) if boxes is not None else None
    try:
        selected,tokens=select(step,boxes,search,multiple=multiple)
    except Exception as exc:
        publish_result(task,raw,frame,roi=search,configured_roi=roi,threshold=step.get('threshold',.9),
            route='step_target',result='UNKNOWN',reason=str(exc),node=step.get('step_id'))
        raise
    publish_result(task,raw,frame,roi=search,configured_roi=roi,threshold=step.get('threshold',.9),
        route='step_target',result='YES' if selected else 'NO',value=step['ocr_target']['text'] if selected else None,
        reason=None if selected else 'OCR target text not matched',node=step.get('step_id'))
    return selected,tokens
