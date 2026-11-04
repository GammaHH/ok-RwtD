"""Optional node-local entry conditions; never sends device input."""
import copy
import math


def validate(node, producers):
    if 'entry_guard_retry' in node:
        if ('entry_guard' not in node or type(node['entry_guard_retry']) is not int
                or not 1 <= node['entry_guard_retry'] <= 1000):
            raise ValueError('Attached Guard retry must be 1..1000 and requires a Guard')
    if 'entry_guard' not in node:
        return
    guard = node['entry_guard']
    if node['kind'] == 'guard' or not isinstance(guard, dict):
        raise ValueError('Invalid attached Guard')
    if 'conditions' in guard:
        if (set(guard)!={'operator','conditions'} or guard['operator'] not in {'and','or'}
                or not isinstance(guard['conditions'],list) or not 1<=len(guard['conditions'])<=10):
            raise ValueError('Attached Guard requires AND/OR and 1..10 conditions')
        for clause in guard['conditions']:
            if not isinstance(clause,dict) or 'conditions' in clause:
                raise ValueError('Nested Guard groups are not supported')
            validate({'kind':node['kind'],'entry_guard':clause},producers)
        return
    if guard.get('kind') == 'time':
        from .time_guard import validate as validate_time
        validate_time(guard)
    elif guard.get('kind') == 'coco':
        from src.rwtd.android_core.module_store import identifier
        from src.rwtd.android_core.canvas_scheduler import number
        if set(guard) - {'kind','feature','threshold','match','roi'}:
            raise ValueError('Invalid attached COCO Guard fields')
        identifier(guard.get('feature'))
        if not number(guard.get('threshold',.8),.5,1) or guard.get('match','detected') not in {'detected','disappeared'}:
            raise ValueError('Invalid attached COCO Guard')
        if 'roi' in guard:
            from src.rwtd.android_core.workflow_model import validate_roi
            validate_roi(guard['roi'])
    elif guard.get('kind') == 'variable':
        from .farm_round_variable import is_round_guard,validate as validate_round
        if is_round_guard(guard):
            validate_round(guard)
            return
        from .shared_ocr import PRESETS
        value=guard.get('value');numeric=type(value) in (int,float) and math.isfinite(value)
        shared=guard.get('source')=='shared'
        if (set(guard) != {'kind','variable','op','value'}|({'source'} if shared else set())
                or guard['variable'] not in (set(PRESETS) if shared else set(producers))
                or (guard['op'] not in {'eq','gte','lte'} if numeric else guard['op'] not in {'eq','contains'})
                or not (numeric or isinstance(value,str) and value.strip() and len(value)<=100)
                or shared and not numeric):
            raise ValueError('Attached Guard requires a local/shared OCR variable and typed comparison')
    elif guard.get('kind') == 'ocr':
        from src.rwtd.android_core.workflow_model import validate_roi
        if (set(guard) != {'kind','text','threshold','match','roi'}
                or not isinstance(guard['text'],str) or not guard['text'].strip()
                or len(guard['text'])>100 or any(c.isspace() for c in guard['text'])
                or type(guard['threshold']) not in (int,float)
                or not math.isfinite(guard['threshold']) or not .5<=guard['threshold']<=1
                or guard['match'] not in {'contains','exact'}):
            raise ValueError('Attached OCR Guard requires text, confidence and matching method')
        validate_roi(guard['roi'])
    else:
        raise ValueError('Attached Guard requires COCO, OCR, OCR variable or time')


def probe(node,settings=None,attempt=1):
    return {'id':node['id'],'kind':'entry_guard','entry_guard':copy.deepcopy(node['entry_guard']),
            '_entry_guard_for':node['id'],'_guard_attempt_limit':retry_limit(node,settings or {}),
            '_guard_attempt':attempt,
            **({'_expedition_completion_clause':{
                'kind':'coco','feature':'expedition_done','match':'detected',
                'threshold':node.get('threshold',.8),
                **({'roi':copy.deepcopy(node['roi'])} if 'roi' in node else {})}}
               if node.get('kind')=='coco' and node.get('feature')=='expedition_done' else {})}


def retry_limit(node, settings):
    """A local Guard budget overrides the node/global execution budget."""
    if node.get('unknown_no_retry') is True:
        return 1
    return node.get('entry_guard_retry', settings.get('max_cycles', 10))


def clauses(guard):
    return guard.get('conditions',[guard])


def needs_frame(guard):
    from .shared_ocr import PRESETS
    from .farm_round_variable import is_round_guard
    return any(c['kind'] in {'coco','ocr'} or c.get('mode')=='ocr_cooldown'
               or c.get('source')=='shared' and not is_round_guard(c) for c in clauses(guard))


def evaluate(guard,variables,observe,time_check=None,trace=None):
    """Three-valued logic: UNKNOWN does not satisfy absence; safe short circuit."""
    results=[];operator=guard.get('operator','and')
    for index,clause in enumerate(clauses(guard)):
        from .shared_ocr import PRESETS
        from .farm_round_variable import is_round_guard
        if is_round_guard(clause):
            value=time_check(clause) if time_check else None
            results.append(value)
            if trace is not None:trace.append({'index':index,'result':value})
            if operator=='and' and value is False:return False
            if operator=='or' and value is True:return True
            continue
        value=(observe(clause) if clause.get('mode')=='ocr_cooldown' or clause.get('source')=='shared'
               else (time_check(clause) if time_check else None) if clause['kind']=='time'
               else variable_result(clause,variables) if clause['kind']=='variable' else observe(clause))
        results.append(value)
        if trace is not None:trace.append({'index':index,'result':value})
        if operator=='and' and value is False:
            return False
        if operator=='or' and value is True:
            return True
    if any(value is None for value in results):
        return None
    return all(results) if operator=='and' else any(results)


def variable_result(guard, variables):
    from .canvas_scheduler import condition
    return condition(guard,{},variables)


def ocr_allowed(tokens, clause):
    """A valid OCR response without a matching token is false; unavailable OCR is UNKNOWN."""
    if not isinstance(tokens,list):
        return None
    for token in tokens:
        if (not isinstance(token,dict) or not isinstance(token.get('text'),str)
                or type(token.get('confidence')) not in (int,float)
                or not math.isfinite(token['confidence']) or not 0<=token['confidence']<=1):
            return None
    return any(token['confidence']>=clause['threshold'] and
               (token['text'].strip()==clause['text'] if clause['match']=='exact'
                else clause['text'] in token['text'].strip()) for token in tokens)


from .canvas_node_flow import skip_port  # Compatibility for existing callers.


def result_allowed(result):
    if not isinstance(result,dict) or result.get('verified') is not True or type(result.get('detected')) is not bool:
        return None
    return result['detected']


def coco_allowed(matches, clause):
    """An unavailable template/matcher is UNKNOWN, never proof of absence."""
    rows = [row for row in matches if row.get('feature') == clause['feature']]
    threshold = clause.get('threshold', .8)
    valid = [row for row in rows if row.get('status') in {None, 'SNAPSHOT_ONLY_NOT_EXECUTION_TARGET'}
             and type(row.get('matched')) is bool
             and (row.get('confidence') is None and row['matched'] is False
                  or type(row.get('confidence')) in (int, float)
                  and math.isfinite(row['confidence']) and 0 <= row['confidence'] <= 1)]
    detected = any(row['matched'] and row['confidence'] >= threshold for row in valid)
    if not detected and (not rows or len(valid) != len(rows)):
        return None
    return detected if clause.get('match', 'detected') == 'detected' else not detected
