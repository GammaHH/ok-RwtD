"""Preset-word OCR classification; no arbitrary code, regex or answer generation."""
import math
import re
import unicodedata

from src.rwtd.android_core.workflow_model import validate_roi
from src.rwtd.android_core.module_store import identifier


def words_for(node):
    return [word for case in node['ocr_cases'] for word in case['dictionary']] if 'ocr_cases' in node else node.get('dictionary', [])


def output_type(node):
    if node.get('ocr_mode','dict')!='raw':
        return 'text'
    rule=node.get('ocr_conversion',{})
    if not isinstance(rule,dict):
        return 'text'  # The OCR validator reports the malformed rule.
    return rule.get('output_type','number') if rule else node.get('ocr_output_type','text')


def normalize_conditions(nodes):
    """Match condition literals to their producer; legacy string numbers migrate."""
    producers={n.get('output_key'):n for n in nodes if isinstance(n,dict) and n.get('kind')=='ocr' and isinstance(n.get('output_key'),str)}
    for node in nodes:
        if not isinstance(node,dict):
            continue
        producer=producers.get(node.get('variable')) if isinstance(node.get('variable'),str) else None
        if node.get('kind')!='condition' or not producer:
            continue
        node.pop('field',None)
        kind=output_type(producer);value=node.get('value')
        if kind in {'number','time'}:
            if isinstance(value,str):
                if kind=='time' and not re.fullmatch(r'\s*\d+(?:\.\d+)?\s*',value):
                    from src.rwtd.android_core.ocr_time import parse_time
                    value=parse_time(value,producer.get('ocr_conversion',{}).get('time_format','auto'))
                else:
                    try: value=float(value.strip())
                    except ValueError: value=None
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError('OCR condition value must match '+kind+' output; time values are seconds or a formatted duration')
            if node.get('op') not in {'eq','gte','lte'}:
                raise ValueError('Numeric/time OCR conditions support eq/gte/lte')
            node['value']=value
        else:
            if type(value) in (int,float) and math.isfinite(value):
                value=str(int(value)) if float(value).is_integer() else str(value)
            if not isinstance(value,str) or not value or node.get('op') not in {'eq','contains'}:
                raise ValueError('Text OCR conditions support eq/contains and a text value')
            node['value']=value


def validate_conversion(rule):
    if not isinstance(rule, dict) or set(rule)-{'replacements','units','scale','offset','output_type','trim','time_format'}:
        raise ValueError('OCR conversion supports output_type, trim, replacements, units, scale and offset')
    if rule.get('output_type','number') not in {'text','number','english','chinese','time'} or type(rule.get('trim',True)) is not bool:
        raise ValueError('Invalid OCR JSON output_type/trim')
    if rule.get('time_format','auto') not in {'auto','ms','hms','hm','units'}:
        raise ValueError('Invalid OCR time_format')
    if 'time_format' in rule and rule.get('output_type')!='time':
        raise ValueError('time_format requires output_type time')
    if rule.get('output_type')=='time' and any(k in rule for k in ('units','scale','offset')):
        raise ValueError('Time output uses seconds; units/scale/offset are not applicable')
    for key in ('replacements','units'):
        entries=rule.get(key,{})
        if not isinstance(entries,dict) or len(entries)>30:
            raise ValueError('OCR conversion requires bounded maps')
        for text,value in entries.items():
            if not isinstance(text,str) or not text or len(text)>40:
                raise ValueError('Invalid OCR conversion text')
            if key=='replacements':
                if not isinstance(value,str) or len(value)>80:
                    raise ValueError('Invalid OCR replacement')
            elif type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                raise ValueError('OCR unit multiplier must be positive and finite')
    for key in ('scale','offset'):
        value=rule.get(key,1 if key=='scale' else 0)
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError('OCR conversion scale/offset must be finite')


def numeric_text(node, text):
    """Only explicitly configured aliases are normalized; no global S/5 guess."""
    text = unicodedata.normalize('NFKC', text)
    for old, new in node.get('ocr_conversion', {}).get('replacements', {}).items():
        text = text.replace(old, new)
    return text


def read_value(node, texts):
    text=' '.join(texts)
    rule=node.get('ocr_conversion',{})
    trim=rule.get('trim',True) if rule else node.get('ocr_trim',True)
    if trim:
        text=text.strip()
    output=rule.get('output_type','number') if rule else node.get('ocr_output_type','text')
    if output=='time':
        from src.rwtd.android_core.ocr_time import parse_time
        for old,new in rule.get('replacements',{}).items():
            text=text.replace(old,new)
        return parse_time(text,rule.get('time_format','auto'))
    if output=='number':
        rule=node.get('ocr_conversion',{})
        text=numeric_text(node,text)
        units=rule.get('units',{})
        pattern=r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
        matches=list(re.finditer(pattern,text))
        if len(matches)!=1:
            return None
        hit=matches[0]
        value=float(hit.group().replace(',',''))
        suffix=text[hit.end():].strip()
        if suffix and rule:
            if suffix not in units:
                return None
            value*=units[suffix]
        value=value*rule.get('scale',1)+rule.get('offset',0)
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() and abs(value)<=9007199254740991 else value
    if output=='english':
        text=re.sub(r'[^A-Za-z\s]','',text)
    elif output=='chinese':
        text=re.sub(r'[^\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef\s]','',text)
    if trim:
        text=text.strip()
    return text if text.strip() else None


def ocr_ports(node):
    return ['CASE_'+case['id'] for case in node['ocr_cases']]+['NO'] if 'ocr_cases' in node else ['NEXT']


def variable_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or not value[0].isalpha() or not all(c.isalnum() or c in '_-' for c in value):
        raise ValueError('Variable name requires letters/numbers/underscore/hyphen')
    return value


def validate_ocr_node(node):
    variable_name(node.get('output_key'))
    validate_roi(node.get('roi'))
    mode=node.get('ocr_mode','dict')
    if mode not in {'dict','raw'} or node.get('ocr_output_type','text') not in {'text','number','english','chinese','time'} or type(node.get('ocr_trim',True)) is not bool:
        raise ValueError('Invalid OCR output mode/type/trim')
    validate_conversion(node.get('ocr_conversion',{}))
    if mode=='raw' and any(key in node for key in ('ocr_cases','ocr_retry')):
        raise ValueError('Pure OCR does not use DICT cases or case retries')
    if node.get('ocr_region', 'fixed') not in {'fixed', 'coco'}:
        raise ValueError('Invalid OCR region source')
    if node.get('ocr_region') == 'coco':
        identifier(node.get('ocr_feature'))
        score = node.get('ocr_coco_threshold', .8)
        if type(score) not in (int, float) or not math.isfinite(score) or not .5 <= score <= 1:
            raise ValueError('Invalid OCR COCO confidence')
    cases = node.get('ocr_cases')
    if 'ocr_cases' in node:
        if not isinstance(cases,list) or not 1 <= len(cases) <= 20:
            raise ValueError('OCR requires 1..20 cases')
        ids = set()
        for case in cases:
            if not isinstance(case,dict) or set(case) != {'id','name','dictionary'}:
                raise ValueError('Invalid OCR case fields')
            identifier(case['id'])
            if case['id'] in ids or not isinstance(case['name'],str) or not case['name'].strip() or len(case['name']) > 80:
                raise ValueError('Invalid/duplicate OCR case')
            ids.add(case['id'])
            if not isinstance(case['dictionary'],list) or not case['dictionary']:
                raise ValueError('Each OCR case needs preset words')
    words = words_for(node)
    if mode=='dict' and (not isinstance(words, list) or not 1 <= len(words) <= 100
            or any(not isinstance(word, str) or not word or len(word) > 100
                   or any(c.isspace() for c in word) for word in words)
            or len(set(words)) != len(words)):
        raise ValueError('OCR DICT requires unique space-separated preset words')
    if node.get('match', 'contains') not in {'contains', 'exact'}:
        raise ValueError('Invalid OCR DICT matching method')
    confidence = node.get('threshold', .8)
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not .5 <= confidence <= 1:
        raise ValueError('Invalid OCR confidence')
    retry = node.get('ocr_retry')
    if 'ocr_retry' in node:
        if not cases or not isinstance(retry,dict) or set(retry)-{'max_attempts','interval_seconds','precondition'}:
            raise ValueError('OCR retry requires case mode')
        attempts, interval = retry.get('max_attempts',1), retry.get('interval_seconds',1)
        if type(attempts) is not int or not 1 <= attempts <= 100 or type(interval) not in (int,float) or not math.isfinite(interval) or not .1 <= interval <= 300:
            raise ValueError('OCR retries require 1..100 attempts and 0.1..300 seconds')
        if attempts > 1 and not retry.get('precondition'):
            raise ValueError('Repeated OCR requires a COCO precondition')
        if retry.get('precondition'):
            identifier(retry['precondition'])


def reading_regions(node, detections):
    """Resolve native OCR boxes; never fall back to full-frame on a missed feature."""
    validate_ocr_node(node)
    if node.get('ocr_region', 'fixed') == 'fixed':
        return [list(node['roi'])]
    regions = []
    for hit in detections:
        if (hit.get('feature') != node['ocr_feature'] or not hit.get('matched')
                or hit.get('confidence', 0) < node.get('ocr_coco_threshold', .8)):
            continue
        box = hit.get('bbox')
        validate_roi(box)
        x, y, w, h = box
        rx, ry, rw, rh = node['roi']
        left, top, right, bottom = max(x, rx), max(y, ry), min(x+w, rx+rw), min(y+h, ry+rh)
        if right > left and bottom > top:
            region = [left, top, right-left, bottom-top]
            if region not in regions:
                regions.append(region)
    return sorted(regions, key=lambda r: (r[1], r[0]))


def classify(node,sample):
    from .ocr_records import attach
    result=_classify(node,sample)
    raw=sample if isinstance(sample,dict) else {}
    regions=raw.get('regions')
    roi=regions[0] if isinstance(regions,list) and len(regions)==1 else raw.get('roi',node.get('roi'))
    attach(result,raw.get('tokens'),roi=roi,configured_roi=node.get('roi'),
        threshold=node.get('threshold',.8),route='canvas_ocr',node=node['id'],frame_id=raw.get('frame_id'))
    result['ocr_record']['stage']='parsed'
    if isinstance(regions,list):result['ocr_record']['regions']=regions[:100]
    return result


def _classify(node, sample):
    """Return preset words only; preserve absence versus invalid/conflicting evidence."""
    validate_ocr_node(node)
    result = {'value': None, 'verified': False, 'node': node['id'], 'reason': 'OCR UNKNOWN', 'state':'invalid'}
    if not isinstance(sample, dict) or sample.get('verified') is not True or not sample.get('frame_id'):
        return result
    result['frame_id'] = sample['frame_id']
    result['source'] = sample.get('source', 'SIMULATION')
    tokens = sample.get('tokens')
    if not isinstance(tokens, list):
        return result
    result['state'] = 'no_match'
    if node.get('ocr_mode','dict')=='raw':
        texts=[]
        for token in tokens:
            if not isinstance(token,dict) or not isinstance(token.get('text'),str) or type(token.get('confidence')) not in (int,float) or not math.isfinite(token['confidence']) or not 0<=token['confidence']<=1:
                result['state']='invalid'
                return result
            if token['confidence']>=node.get('threshold',.8):
                texts.append(token['text'])
        value=read_value(node,texts)
        if value is not None:
            result.update(value=value,verified=True,state='matched',reason='OCR read',raw_text=' '.join(texts))
        else:
            result['reason']='OCR output empty, ambiguous or conversion rule did not match'
        return result
    matched = set()
    for token in tokens:
        if not isinstance(token, dict):
            result['state'] = 'invalid'
            return result
        score, text = token.get('confidence'), token.get('text')
        if (type(score) not in (int, float) or not math.isfinite(score)
                or not 0 <= score <= 1 or not isinstance(text, str)):
            result['state'] = 'invalid'
            return result
        if score < node.get('threshold', .8):
            continue
        text = text.strip()
        for word in words_for(node):
            matches = text == word if node.get('match', 'contains') == 'exact' else word in text
            if matches:
                matched.add(word)
    matching_cases = [case for case in node.get('ocr_cases',[]) if matched.intersection(case['dictionary'])]
    if len(matched) == 1 or len(matching_cases) == 1:
        word = next(word for word in words_for(node) if word in matched)
        result.update(value=word, verified=True, reason='DICT matched',state='matched')
        if 'ocr_cases' in node:
            case = next(case for case in node['ocr_cases'] if result['value'] in case['dictionary'])
            result.update(case_id=case['id'],case_name=case['name'],port='CASE_'+case['id'])
    elif matched:
        result.update(reason='Conflicting DICT matches',state='conflict')
    return result


def decide(node, sample, attempt):
    result = classify(node,sample)
    if 'ocr_cases' not in node:
        return result, 'NEXT' if result['verified'] else 'UNKNOWN'
    retry = node.get('ocr_retry',{})
    if result['state'] == 'invalid':
        return result,'UNKNOWN'
    if retry.get('precondition'):
        pre = sample.get('precondition')
        if not isinstance(pre,dict) or pre.get('verified') is not True or type(pre.get('detected')) is not bool or pre.get('frame_id') != sample.get('frame_id'):
            for key in ('case_id','case_name','port'): result.pop(key,None)
            result.update(value=None,verified=False,state='invalid',reason='OCR precondition UNKNOWN')
            return result,'UNKNOWN'
        if pre['detected'] is False:
            for key in ('case_id','case_name','port'): result.pop(key,None)
            result.update(value=None,verified=False,state='precondition_lost',reason='COCO 前置條件不成立 → NO')
            return result,'NO'
    if result['verified']:
        return result,result['port']
    if result['state'] == 'conflict':
        return result,'UNKNOWN'
    if attempt >= retry.get('max_attempts',1):
        result['reason'] = 'OCR 次數用完，未命中 → NO'
        return result,'NO'
    result['reason'] = 'OCR 未命中；等待重試'
    return result,'RETRY'


def simulation_sample(node, observations, graph):
    import copy
    sample = copy.deepcopy(observations.get('ocr',{}).get(node['id']))
    pre_id = node.get('ocr_retry',{}).get('precondition')
    if pre_id and isinstance(sample,dict):
        from src.rwtd.android_core.canvas_triggers import TriggerTaskScheduler
        pre = next(n for n in graph['nodes'] if n['id'] == pre_id)
        helper = TriggerTaskScheduler();helper.observations=observations
        result = helper.simulation_result({'node':pre})
        result['frame_id'] = sample.get('frame_id')
        sample['precondition'] = result
    return sample
