"""Pair long A/B ROI strips by Y; return eligible A targets on one frame."""
import copy
import math
import operator
import re
import unicodedata
from types import SimpleNamespace
from .workflow_model import TAG_ID,validate_roi,select_targets,TargetNotFound
from .canvas_entry_guard import coco_allowed
from .step_conditions import ConditionUnknown


FIELDS={'a_tag','b_tag','a_roi','b_roi','check_feature','check_threshold','padding_y','match',
        'offset_x','offset_y','check_width','check_height','selection_mode','selection_index',
        'check_kind','check_text','check_text_match','check_op','check_value',
        'region_priority','region_fallback'}
COMPARISONS={'eq':operator.eq,'ne':operator.ne,'gt':operator.gt,'gte':operator.ge,
             'lt':operator.lt,'lte':operator.le}


def validate(row,allowed):
    if row.get('selection_mode','paired') not in {'paired','index'}:
        raise ValueError('Unknown PADDING selection mode')
    if 'selection_index' in row and (type(row['selection_index']) is not int or not 1<=row['selection_index']<=100):
        raise ValueError('PADDING selection index requires integer 1..100')
    if row.get('selection_mode')=='index' and 'selection_index' not in row:
        raise ValueError('PADDING indexed selection requires an index')
    for key in ('offset_x','offset_y'):
        if key in row and (type(row[key]) is not int or not -2400<=row[key]<=2400):
            raise ValueError('PADDING offset requires signed integer -2400..2400')
    for key,limit in [('check_width',1080),('check_height',2400)]:
        if key in row and (type(row[key]) is not int or not 1<=row[key]<=limit):
            raise ValueError('PADDING check width/height must be positive reference pixels')
    if 'padding_y' in row and any(k in row for k in ('offset_x','offset_y','check_width','check_height')):
        raise ValueError('Legacy padding cannot be combined with offset/size')
    for tag,roi in [('a_tag','a_roi'),('b_tag','b_roi')]:
        if (tag in row)==(roi in row):raise ValueError('PADDING COCO requires exactly one A/B TAG or resolved ROI')
        if tag in row and (not isinstance(row[tag],str) or not TAG_ID.fullmatch(row[tag])):
            raise ValueError('PADDING COCO requires module-local A/B TAGs')
        if roi in row:validate_roi(row[roi])
    score=row.get('check_threshold',.8)
    if (type(score) not in (int,float) or not .5<=score<=1
            or type(row.get('padding_y',10)) is not int or not 0<=row.get('padding_y',10)<=1200
            or row.get('match','disappeared') not in {'detected','disappeared'}
            or 'roi' in row or 'roi_tag' in row):
        raise ValueError('Invalid PADDING COCO check Feature, threshold, Y padding or match')
    kind=row.get('check_kind','coco')
    fields={'coco':{'check_feature','match'},'ocr':{'check_text','check_text_match'},
            'number':{'check_op','check_value'},'percent':{'check_op','check_value'},
            'region':{'region_priority','region_fallback'}}
    if (not isinstance(kind,str) or kind not in fields
            or (set(row) & set().union(*fields.values())) - fields[kind]):
        raise ValueError('PADDING B fields must match the selected recognition type')
    if kind=='coco' and (not isinstance(row.get('check_feature'),str) or row['check_feature'] not in allowed):
        raise ValueError('Invalid PADDING B Feature')
    if kind=='ocr' and (not isinstance(row.get('check_text'),str) or not row['check_text'].strip()
                        or len(row['check_text'])>200 or row.get('check_text_match','exact') not in {'exact','contains'}):
        raise ValueError('PADDING B OCR requires text and an exact/contains match')
    if kind in {'number','percent'}:
        value=row.get('check_value')
        lower,upper=(0,100) if kind=='percent' else (-1e15,1e15)
        if (not isinstance(row.get('check_op'),str) or row['check_op'] not in COMPARISONS
                or type(value) not in (int,float)
                or not lower<=value<=upper):
            raise ValueError(f'PADDING B requires a comparison and a finite value within {lower}..{upper}')
    if kind=='region':
        names=row.get('region_priority')
        if (not isinstance(names,list) or not 1<=len(names)<=30
                or any(not isinstance(name,str) or not name.strip() or len(name)>200 for name in names)
                or len({name.strip() for name in names})!=len(names)
                or type(row.get('region_fallback',False)) is not bool
                or row.get('selection_mode','paired')!='paired'):
            raise ValueError('PADDING region priority requires unique names and A/B pairing')


def resolve(row,module):
    for tag,roi in [('a_tag','a_roi'),('b_tag','b_roi')]:
        name=row.pop(tag,None)
        if name is not None:
            if name not in module.get('tags',{}):raise ValueError('Missing PADDING COCO TAG: '+name)
            row[roi]=list(module['tags'][name]['roi'])


def read_b_ocr(task,frame,row,band):
    from ok import Box
    from .ocr_records import box_tokens
    from .canvas_entry_guard import ocr_allowed

    threshold=row.get('check_threshold',.8)
    try:
        boxes=task.ocr(box=Box(*band),frame=frame,threshold=threshold)
    except Exception as exc:
        return {'detected':None,'ocr_tokens':[],'reason':'B OCR failed: '+type(exc).__name__}
    if boxes is None:
        return {'detected':None,'ocr_tokens':[],'reason':'B OCR unavailable'}
    tokens=box_tokens(boxes)
    x,y,w,h=band
    if any('bbox' not in token for token in tokens):
        return {'detected':None,'ocr_tokens':tokens,'reason':'B OCR coordinates unavailable'}
    inside=[]
    for token in tokens:
        try:
            tx,ty,tw,th=validate_roi(token['bbox'])
        except ValueError:
            return {'detected':None,'ocr_tokens':tokens,'reason':'Invalid B OCR coordinates'}
        if x<=tx and y<=ty and tx+tw<=x+w and ty+th<=y+h:
            inside.append(token)
    if row['check_kind']=='ocr':
        detected=ocr_allowed(inside,{'text':row['check_text'].strip(),
            'threshold':threshold,'match':row.get('check_text_match','exact')})
        return {'detected':detected,'ocr_tokens':inside}
    if row['check_kind']=='region':
        if ocr_allowed(inside,{'text':'','threshold':threshold,'match':'exact'}) is None:
            return {'detected':None,'ocr_tokens':inside,'reason':'Invalid B OCR result'}
        names={token['text'].strip() for token in inside if token['confidence']>=threshold}
        ranks=[rank for rank,name in enumerate(row['region_priority']) if name.strip() in names]
        if len(ranks)>1:
            return {'detected':None,'ocr_tokens':inside,'reason':'More than one region name matched'}
        return {'detected':bool(ranks),'ocr_tokens':inside,
                'region_rank':ranks[0] if ranks else None}

    # Require one complete numeric token. Never extract a substring or default to zero.
    percent=row['check_kind']=='percent'
    pattern=r'([0-9]+(?:\.[0-9]+)?)\s*%' if percent else r'([+-]?[0-9]+(?:\.[0-9]+)?)'
    lower,upper=(0,100) if percent else (-1e15,1e15)
    numbers=[]
    for token in inside:
        score=token['confidence']
        value=token['text']
        if not isinstance(value,str) or score is None or not threshold<=score<=1:
            continue
        value=unicodedata.normalize('NFKC',value).strip()
        match=re.fullmatch(pattern,value)
        if match:
            number=float(match[1])
            if lower<=number<=upper:numbers.append(number)
    if len(numbers)!=1:
        return {'detected':None,'ocr_tokens':inside,'reason':'B OCR needs exactly one valid number'}
    number=numbers[0]
    return {'detected':COMPARISONS[row['check_op']](number,row['check_value']),
            'ocr_tokens':inside,'value':number}


def evaluate(task,frame,row):
    a,b=row['a_roi'],row['b_roi'];threshold=row.get('threshold',.8)
    matches=task.detect_manual_features(frame,row['features'],threshold,coco_roi=a)
    states=[coco_allowed(matches,{'feature':f,'threshold':threshold}) for f in row['features']]
    if None in states:raise ConditionUnknown('PADDING COCO anchor recognition unavailable')
    try:
        anchors=select_targets({'features':row['features'],'coco_roi':a,'threshold':threshold},matches)
    except TargetNotFound:anchors=[]
    evidence={'detections':matches,'a_roi':a,'b_roi':b,'candidates':[]}
    if not anchors:return [],False,evidence
    if len(anchors)>100:raise ConditionUnknown('PADDING COCO has more than 100 anchors')
    if row.get('selection_mode')=='index':
        requested=row['selection_index']
        # A-only positional override. B/NO_GEM is deliberately not recognized,
        # so B UNKNOWN or a matching NO_GEM cannot cancel the chosen A.
        anchors=sorted(anchors,key=lambda hit:(hit['bbox'][1],hit['bbox'][0],hit.get('feature','')))
        index=requested-1 if requested<=len(anchors) else 0
        chosen=copy.deepcopy(anchors[index])
        evidence.update(selection_mode='index',requested_index=requested,selected_index=index+1,
                        available_count=len(anchors),bypassed_b=True,
                        reason='requested_index' if requested<=len(anchors) else 'index_out_of_range_use_first',
                        candidates=[{'anchor_bbox':a['bbox'],'anchor_feature':a.get('feature'),
                                     'b_band':None,'detected':None,'matches':[],
                                     'accepted':i==index,'reason':'B bypassed by indexed selection'}
                                    for i,a in enumerate(anchors)])
        return [chosen],True,evidence
    kind=row.get('check_kind','coco')
    if kind=='region' and row.get('region_fallback'):
        evidence['fallback_anchors']=copy.deepcopy(anchors)
    scored=[]
    if kind=='coco':
        check={'feature':row['check_feature'],'threshold':row.get('check_threshold',.8),'match':'detected'}
        checks=task.detect_manual_features(frame,[check['feature']],check['threshold'],coco_roi=b)
        evidence['check_detections']=checks
        if coco_allowed(checks,check) is None:raise ConditionUnknown('PADDING COCO B recognition unavailable')
        for hit in checks:
            if hit.get('feature')!=check['feature'] or not hit.get('matched') or hit.get('confidence',0)<check['threshold']:continue
            validate_roi(hit.get('bbox'));scored.append(hit)
    modern=any(k in row for k in ('offset_x','offset_y','check_width','check_height'))
    retained=[];pad=0 if modern else row.get('padding_y',10)
    for anchor in anchors:
        x,y,w,h=anchor['bbox']
        left=max(b[0],b[0]+row.get('offset_x',0))
        right=min(b[0]+b[2],b[0]+row.get('offset_x',0)+row.get('check_width',b[2]))
        raw_top=y+b[1]-a[1]+row.get('offset_y',0)
        top=max(b[1],raw_top-pad)
        bottom=min(b[1]+b[3],raw_top+row.get('check_height',h)+pad)
        if bottom<=top or right<=left:raise ConditionUnknown('PADDING COCO mapped check ROI outside B TAG')
        band=[left,top,right-left,bottom-top]
        paired=[hit for hit in scored if left<=hit['bbox'][0]+hit['bbox'][2]/2<right
                and top<=hit['bbox'][1]+hit['bbox'][3]/2<bottom]
        observation={'detected':bool(paired)} if kind=='coco' else read_b_ocr(task,frame,row,band)
        detected=observation['detected']
        accepted=(not detected if row.get('match','disappeared')=='disappeared' else detected) if kind=='coco' else detected is True
        evidence['candidates'].append({'anchor_bbox':anchor['bbox'],'anchor_feature':anchor.get('feature'),'b_band':band,
            'accepted':accepted,'matches':paired,
            'reason':'B condition matched' if detected else 'B condition unavailable' if detected is None else 'B condition not matched',
            **observation})
        if accepted:
            hit=copy.deepcopy(anchor)
            if kind=='region':hit['region_rank']=observation['region_rank']
            retained.append(hit)
    if any(candidate['detected'] is None for candidate in evidence['candidates']):
        evidence['reason']='At least one PADDING B condition is UNKNOWN'
        for candidate in evidence['candidates']:
            candidate['accepted']=False
        return [],None,evidence
    return retained,bool(retained),evidence


def preview(payload):
    """Pure pairing of displayed snapshot detections; no device/queue/file access."""
    if not isinstance(payload,dict) or set(payload)!={'row','matches','frame_id'}:
        raise ValueError('Invalid padding preview payload')
    row=payload['row'];matches=payload['matches'];frame_id=payload['frame_id']
    if not isinstance(frame_id,str) or not 1<=len(frame_id)<=200:
        raise ValueError('Preview requires a displayed frame id')
    if (not isinstance(row,dict) or row.get('type')!='padding_coco'
            or not isinstance(matches,list) or len(matches)>500):
        raise ValueError('Invalid padding preview row or detections')
    from .target_conditions import validate as validate_targets
    allowed=set(row.get('features',[]))|{row.get('check_feature')}
    validate_targets({'name':'TapTest','target_conditions':{'evaluation':'left_to_right',
        'position_policy':'source','position_source':0,'clauses':[row]}},allowed,preview=True)
    for hit in matches:
        if not isinstance(hit,dict) or not isinstance(hit.get('feature'),str):
            raise ValueError('Invalid preview detection')
        if hit.get('bbox') is not None:validate_roi(hit['bbox'])
        score=hit.get('confidence')
        if score is not None and (type(score) not in (int,float) or not math.isfinite(score)):
            raise ValueError('Invalid preview confidence')
    def detect(frame,features,threshold,coco_roi=None):
        result=[];rx,ry,rw,rh=coco_roi
        for original in matches:
            if original['feature'] not in features:continue
            hit=copy.deepcopy(original)
            # Re-scope existing detections, never claim a new template match.
            for k in ('search_bbox','configured_search_bbox'):hit.pop(k,None)
            if hit.get('matched') is True:
                box=hit.get('bbox')
                if box is None:raise ValueError('Matched preview target has no bbox')
                x,y,w,h=box
                hit['matched']=bool(rx<=x and ry<=y and x+w<=rx+rw and y+h<=ry+rh
                                    and hit.get('confidence',0)>=threshold)
            result.append(hit)
        return result
    try:
        hits,state,evidence=evaluate(SimpleNamespace(detect_manual_features=detect),None,row)
        return dict(evidence,result='UNKNOWN' if state is None else 'YES' if state else 'NO',frame_id=frame_id,
                    retained=hits,selected=hits[:1],device_inputs=0,preview_only=True)
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return {'result':'UNKNOWN','reason':str(exc)[:300],'frame_id':frame_id,
                'candidates':[],'retained':[],'selected':[],'device_inputs':0,'preview_only':True}


def preview_image(payload,recognize):
    """Re-recognize the editor's exact PNG, independent of live preview status."""
    import base64,struct
    import cv2
    import numpy as np
    if not isinstance(payload,dict) or set(payload)!={'row','image_png','frame_id'}:
        raise ValueError('Image pairing preview requires row, image_png and frame_id')
    row=payload['row'];frame_id=payload['frame_id'];encoded=payload['image_png']
    if not isinstance(frame_id,str) or not 1<=len(frame_id)<=200 or not isinstance(encoded,str) or len(encoded)>8*1024*1024:
        raise ValueError('Invalid pairing preview image or id')
    from .target_conditions import validate as validate_targets
    if not isinstance(row,dict) or row.get('type')!='padding_coco':raise ValueError('Invalid padding row')
    validate_targets({'name':'TapTest','target_conditions':{'evaluation':'left_to_right',
        'position_policy':'source','position_source':0,'clauses':[row]}},
        set(row.get('features',[]))|{row.get('check_feature')},preview=True)
    raw=base64.b64decode(encoded,validate=True)
    if len(raw)<24 or raw[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('Preview requires PNG pixels')
    width,height=struct.unpack('>II',raw[16:24])
    if width<1 or height<1 or width*height>12000000:raise ValueError('Preview PNG dimensions exceed limit')
    frame=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
    if frame is None or frame.shape[:2]!=(height,width):raise ValueError('Cannot decode preview PNG')
    normalized=None
    if (width,height)!=(1080,2400):
        from .screen_geometry import ScreenGeometry
        frame=ScreenGeometry(width,height).normalize(frame)
        ok,png=cv2.imencode('.png',frame)
        if not ok:raise ValueError('Cannot encode normalized preview')
        normalized=base64.b64encode(png.tobytes()).decode('ascii')
    try:
        hits,state,evidence=recognize(frame,row)
        return dict(evidence,result='UNKNOWN' if state is None else 'YES' if state else 'NO',retained=hits,selected=hits[:1],
            frame_id=frame_id,device_inputs=0,preview_only=True,normalized_image_png=normalized)
    except (ValueError,TypeError,KeyError) as exc:
        return {'result':'UNKNOWN','reason':str(exc)[:500],'frame_id':frame_id,'candidates':[],
            'retained':[],'selected':[],'device_inputs':0,'preview_only':True,
            'normalized_image_png':normalized}
