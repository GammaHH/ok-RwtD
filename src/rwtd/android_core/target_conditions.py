"""Composed COCO/OCR targets on one frame; no device input or persistence."""
import copy
import math
from .workflow_model import validate_roi,validate_candidates,TargetNotFound
from .step_conditions import combine,ConditionUnknown


def validate(step,allowed,*,preview=False):
    if 'target_conditions' not in step:return
    group=step['target_conditions']
    if (not isinstance(group,dict) or set(group)!={'evaluation','position_policy','position_source','clauses'}
            or group['evaluation']!='left_to_right' or group['position_policy'] not in {'same','all','source'}
            or not isinstance(group['clauses'],list) or not 1<=len(group['clauses'])<=30
            or type(group['position_source']) is not int or not 0<=group['position_source']<len(group['clauses'])):
        raise ValueError('Target conditions require rows, position policy and source index')
    if (step.get('name') not in {'TapTest','LongPressTest','SwipeCalibration'}
            or any(step.get(k) for k in ('features','feature','color_mode','ocr_target'))
            or step.get('recognize_before',True) is not True or step.get('allow_unconfirmed_target') is True):
        raise ValueError('Composed targets require exclusive recognized input')
    for row in group['clauses']:
        if not isinstance(row,dict) or row.get('type') not in {'coco','ocr','color','coco_color','coco_or_color','padding_coco'}:
            raise ValueError('Target row requires COCO, OCR or color')
        keys={'type','join','threshold','roi','roi_tag'}
        if row['type'] in {'coco','coco_color','coco_or_color','padding_coco'}:keys|={'features','feature_priority'}
        if row['type']=='padding_coco':
            from .padding_coco import FIELDS,validate as validate_padding
            keys|=FIELDS
            validate_padding(row,allowed)
        if row['type']=='ocr':keys|={'text','match'}
        if row['type'] in {'color','coco_color','coco_or_color'}:keys|={'color_rgb','color_tolerance','color_min_pixels'}
        if set(row)-keys or row.get('join','AND') not in {'AND','OR'}:
            raise ValueError('Unknown target row field or join')
        threshold=row.get('threshold',step.get('threshold',.9))
        if type(threshold) not in (int,float) or not math.isfinite(threshold) or not .5<=threshold<=1:
            raise ValueError('Target row threshold requires 0.5..1')
        if 'roi' in row:validate_roi(row['roi'])
        if 'roi_tag' in row:
            from .workflow_model import TAG_ID
            if not isinstance(row['roi_tag'],str) or not TAG_ID.fullmatch(row['roi_tag']) or 'roi' in row:
                raise ValueError('Target row TAG/ROI conflict')
        if row['type'] in {'coco','coco_color','coco_or_color','padding_coco'}:
            if not validate_candidates(row,allowed):raise ValueError('Target row requires COCO features')
        if row['type'] in {'color','coco_color','coco_or_color'}:
            from .color_target import validate_color
            validate_color(dict(row,color_mode='only'))
        if row['type']=='ocr' and (not isinstance(row.get('text'),str) or not row['text'].strip() or len(row['text'])>200
              or row.get('match','exact') not in {'exact','contains'}):
            raise ValueError('Target OCR row requires text and exact/contains')
    priority=[row for row in group['clauses'] if row.get('check_kind')=='region']
    if priority and not preview:
        from .target_order import POSITION_POLICIES
        if (len(priority)!=1 or group['position_policy']!='same'
                or any(row['type']!='padding_coco' or row.get('selection_mode','paired')!='paired'
                       or row.get('join','AND')!='AND' for row in group['clauses'])
                or not any(row.get('check_kind','coco')=='coco' and row.get('match','disappeared')=='disappeared'
                           for row in group['clauses'])
                or step.get('target_policy') not in POSITION_POLICIES):
            raise ValueError('Region priority requires same-position AND pairing, a COCO absence check and top/bottom selection')


def same(a,b):
    x,y,w,h=a['bbox'];u,v,s,t=b['bbox']
    return ((x<=u+s/2<x+w and y<=v+t/2<y+h)
            or (u<=x+w/2<u+s and v<=y+h/2<v+t))


def positions(rows,policy,source):
    if policy=='source':hits=copy.deepcopy(rows[source])
    elif policy=='all':hits=[copy.deepcopy(hit) for row in rows for hit in row]
    else:
        active=[row for row in rows if row]
        hits=copy.deepcopy(active[0]) if active else []
        for row in active[1:]:
            overlap=[]
            for a in hits:
                for b in row:
                    if ('region_rank' in a or 'region_rank' in b) and a['bbox']!=b['bbox']:
                        continue
                    if not same(a,b):continue
                    x,y,w,h=a['bbox'];u,v,s,t=b['bbox']
                    left,top=max(x,u),max(y,v);right,bottom=min(x+w,u+s),min(y+h,v+t)
                    if right>left and bottom>top:
                        merged=dict(a,bbox=[left,top,right-left,bottom-top],
                                    confidence=min(a['confidence'],b['confidence']))
                        if 'region_rank' in b:merged['region_rank']=b['region_rank']
                        overlap.append(merged)
            hits=overlap
    result=[]
    for hit in hits:
        if not any(same(hit,old) for old in result):result.append(hit)
    if len(result)>100:raise ConditionUnknown('Too many composed target positions')
    return sorted(result,key=lambda hit:(hit['bbox'][1],hit['bbox'][0]))


def evaluate(task,frame,step):
    group=step['target_conditions'];rows=[];row_hits=[];value=None
    for index,row in enumerate(group['clauses']):
        threshold=row.get('threshold',step.get('threshold',.9))
        roi=row.get('roi',step.get('coco_roi') or [0,0,1080,2400])
        entry=copy.deepcopy(row);entry['configured_roi']=list(roi)
        hits=[];state=None
        try:
            if row['type']=='padding_coco':
                from .padding_coco import evaluate as evaluate_padding
                hits,state,evidence=evaluate_padding(task,frame,row)
                entry.update(evidence)
            elif row['type']=='ocr':
                from .ocr_target import locate
                hits,tokens=locate(task,frame,{'ocr_target':{'text':row['text'],'match':row.get('match','exact')},
                    'threshold':threshold,'coco_roi':roi},multiple=True)
                entry['detections']=tokens;state=bool(hits)
            elif row['type']=='color':
                from .color_target import components
                pixels=components(frame,dict(row,color_mode='only',coco_roi=roi))
                hits=[dict(hit,feature='__color__',matched=True,confidence=1,evidence_kind='color_predicate') for hit in pixels]
                entry['detections']=hits;state=bool(hits)
            else:
                from .canvas_entry_guard import coco_allowed
                try:
                    tokens=task.detect_manual_features(frame,row['features'],threshold,coco_roi=roi)
                    states=[coco_allowed(tokens,{'feature':feature,'threshold':threshold,'match':'detected'})
                            for feature in row['features']]
                except Exception as exc:
                    if row['type']!='coco_or_color':raise
                    tokens=[];states=[None]
                    entry['coco_reason']=type(exc).__name__+': '+str(exc)[:200]
                state=True if True in states else None if None in states else False
                effective=getattr(task,'recognition_roi',None)
                search=effective(roi,'coco') if callable(effective) else roi
                rx,ry,rw,rh=search
                for hit in tokens:
                    if hit.get('feature') not in row['features'] or hit.get('matched') is not True:continue
                    score=hit.get('confidence')
                    if type(score) not in (float,int) or not math.isfinite(score) or score<threshold:continue
                    x,y,w,h=validate_roi(hit['bbox'])
                    if x>=rx and y>=ry and x+w<=rx+rw and y+h<=ry+rh:hits.append(copy.deepcopy(hit))
                entry['detections']=tokens
                if row['type'] in {'coco_color','coco_or_color'}:
                    from .color_target import either_targets,components
                    color_step=dict(row,coco_roi=roi,color_mode='coco' if row['type']=='coco_color' else 'either',threshold=threshold)
                    if row['type']=='coco_color':
                        colored=[];entry['color']={'targets':[]}
                        for hit in hits:
                            found=components(frame,color_step,hit['bbox'])
                            entry['color']['targets'].append({'bbox':hit['bbox'],'color_components':len(found)})
                            if found:colored.append(hit)
                        hits=colored
                    else:
                        entry['color']={}
                        try:hits=either_targets(frame,color_step,tokens,multiple=True,evidence=entry['color'])
                        except TargetNotFound:hits=[]
                    state=True if hits else None if None in states else False
                if state is True and not hits:state=False
                priority=row.get('feature_priority')
                if priority:hits.sort(key=lambda hit:(priority.index(hit['feature']) if hit['feature'] in priority else len(priority),-hit['confidence']))
        except Exception as exc:
            state=None;entry['reason']=type(exc).__name__+': '+str(exc)[:200]
        entry['result']='UNKNOWN' if state is None else 'YES' if state else 'NO'
        rows.append(entry);row_hits.append(hits if state is True else [])
        value=state if index==0 else combine(value,state,row.get('join','AND'))
    report={'evaluation':'left_to_right','result':'UNKNOWN' if value is None else 'YES' if value else 'NO',
            'clauses':rows,'position_policy':group['position_policy'],'position_source':group['position_source']}
    hits=positions(row_hits,group['position_policy'],group['position_source']) if value is True else []
    report.update(position_count=len(hits),target_policy=step.get('target_policy','single'))
    from .target_order import POSITION_POLICIES,choose
    if hits and all('region_rank' in hit for hit in hits):
        rank=min(hit['region_rank'] for hit in hits)
        hits=[hit for hit in hits if hit['region_rank']==rank]
        report['selected_region_rank']=rank
    elif not hits and all(row['result']!='UNKNOWN' for row in rows):
        fallback=next((row['fallback_anchors'] for row in rows if row.get('fallback_anchors')),None)
        if fallback and step.get('target_policy') in POSITION_POLICIES:
            hits=fallback
            report.update(result='YES',position_count=len(hits),fallback_used=True)
    if step.get('target_policy') in POSITION_POLICIES:hits=choose(hits,step['target_policy'])
    priority=group['clauses'][group['position_source']].get('feature_priority')
    if hits and group['position_policy']=='source' and step.get('target_policy','single')=='single' and priority:
        hits=[min(hits,key=lambda hit:(priority.index(hit['feature']) if hit['feature'] in priority else len(priority),-hit['confidence']))]
    return hits,report
