"""Conditional Step control, confined to one module invocation."""
import json
import uuid
from datetime import datetime,timezone


def validate(step):
    if 'condition_effect' not in step:return
    effect=step['condition_effect']
    if (not step.get('input_conditions') or not isinstance(effect,dict)
            or effect.get('when','YES') not in {'YES','NO'}
            or effect.get('action') not in {'skip_step','skip_remaining','skip_remaining_now','skip_range','retry_step'}):
        raise ValueError('Step condition effect requires conditions, YES/NO and a supported action')
    keys={'action','when'}|({'start_step_id','end_step_id'} if effect['action']=='skip_range' else set())
    if effect['action']=='retry_step':
        keys|={'retry_step_id','max_cycles','wait_seconds','timeout_seconds'}
        from .step_loop import validate_spec
        validate_spec(effect)
    if effect['action'] in {'skip_range','skip_remaining'}:
        keys|={'skip_self_on_mismatch','skip_self_on_match'}
    if set(effect)-keys:raise ValueError('Unknown condition effect field')
    if 'skip_self_on_mismatch' in effect and type(effect['skip_self_on_mismatch']) is not bool:
        raise ValueError('Skip self on mismatch must be boolean')
    if 'skip_self_on_match' in effect and type(effect['skip_self_on_match']) is not bool:
        raise ValueError('Skip self on match must be boolean')
    if 'skip_self_on_match' in effect and 'skip_self_on_mismatch' in effect:
        raise ValueError('Choose the current match rule or the legacy mismatch rule, not both')
    if effect['action']=='skip_range':
        for key in ('start_step_id','end_step_id'):
            if not isinstance(effect.get(key),str) or not effect[key]:raise ValueError('Condition range requires Step IDs')


def validate_ranges(steps):
    ids=[s['step_id'] for s in steps]
    for index,step in enumerate(steps):
        validate(step);effect=step.get('condition_effect',{})
        if effect.get('action')=='retry_step':
            try:target=ids.index(effect['retry_step_id'])
            except ValueError as exc:raise ValueError('Retry references a missing Step') from exc
            if target>=index:raise ValueError('Retry Step must be earlier than its controlling Step')
            target_effect=steps[target].get('condition_effect',{}).get('action')
            if target_effect not in {None,'skip_step'}:
                raise ValueError('Retry target must not control another range or retry')
        if effect.get('action')!='skip_range':continue
        try:start,end=ids.index(effect['start_step_id']),ids.index(effect['end_step_id'])
        except ValueError as exc:raise ValueError('Condition range references a missing Step') from exc
        if not index<start<=end:raise ValueError('Condition range must be ordered and after its controlling Step')


def decision(step,report):
    effect=step.get('condition_effect')
    if effect and effect.get('action')=='retry_step':return None
    if effect is None and report['result']=='NO':effect={'action':'skip_step','when':'NO'}
    mismatch=None
    no_gate=False
    # The explicit new field is the semantics version marker, even when false.
    # Preserve old files until a human applies/saves them in the new editor.
    if (effect and 'skip_self_on_match' in effect
            and effect.get('action') in {'skip_range','skip_remaining'}
            and report['result']=='NO' and effect.get('when','YES')!='NO'):
        no_gate=True
        effect={'action':'skip_step','when':'NO'}
    if (effect and effect.get('skip_self_on_mismatch') is True
            and effect.get('action') in {'skip_range','skip_remaining'}
            and report['result'] in {'YES','NO'} and report['result']!=effect.get('when','YES')):
        mismatch=effect.get('when','YES')
        effect={'action':'skip_step','when':report['result']}
    if not effect or report['result']!=effect.get('when','YES'):return None
    rows=[]
    for index,row in enumerate(report['clauses'][:30]):
        text=f"{index+1}:{row.get('type')} {row.get('feature',row.get('text',''))}={row['result']}"
        if row.get('threshold') is not None:text+=' threshold='+str(row['threshold'])
        text+=' ROI='+str(row.get('effective_roi',row.get('roi','full')))
        if row.get('type')=='color':text+=' RGB='+str(row.get('rgb'))+' components='+str(len(row.get('components',[])))
        tokens=row.get('tokens',row.get('detections',[]))
        text+=' '+json.dumps(tokens[:5],ensure_ascii=False,default=str)[:350]
        if row.get('reason'):text+=' reason='+str(row['reason'])[:160]
        rows.append(text)
    reason=('組合結果 '+report['result']+'；'+'；'.join(rows))[:3000]
    if mismatch is not None:reason='預期觸發 '+mismatch+'；未符合觸發結果，跳過本 Step；'+reason
    if no_gate:reason='執行前條件 NO，跳過本 Step；未命中範圍跳過觸發，不跳過後續；'+reason
    if effect.get('skip_self_on_match') is True:
        reason='符合觸發結果，跳過本 Step 並套用後續跳過；'+reason
    return dict(effect,reason=reason,result=report['result'])


def applies_to_tail(record):
    """A deliberate self-skip may activate its range without sending input."""
    control=record.get('condition_control')
    if not control or control['action']=='skip_step' or record.get('failure_reason'):
        return False
    return bool(record.get('action_completed') or
                record.get('condition_skipped') and
                (control['action']=='skip_remaining_now' or control.get('skip_self_on_match') is True))


def skipped_ids(control,steps,current_id):
    ids=[s['step_id'] for s in steps]
    if control['action'] in {'skip_remaining','skip_remaining_now'}:return set(ids[ids.index(current_id)+1:])
    if control['action']=='skip_range':
        return set(ids[ids.index(control['start_step_id']):ids.index(control['end_step_id'])+1])
    return set()


def skipped_record(step,module,revision,origin):
    return {'report_id':'condition_skip_'+uuid.uuid4().hex,'step_id':step['step_id'],
        'module':step.get('name'),'timestamp_utc':datetime.now(timezone.utc).isoformat(),
        'workflow_module':module,'workflow_revision':revision,'parameters':dict(step),
        'status':'SKIPPED','condition_skipped':True,'action_completed':False,
        'input_attempted':False,'input_command_returned':False,'device_inputs':0,
        'failure_reason':None,'skip_reason':origin['reason'],
        'condition_control_source':origin.get('source_step_id'),'reports_saved':False}


def log(task,module,step_id,control,affected=None):
    payload=dict(module=module,step=step_id,action=control['action'],
                 result=control['result'],reason=control['reason'])
    if affected is not None:payload['skipped_step_ids']=list(affected)
    message='RWTD_STEP_CONDITION '+json.dumps(payload,ensure_ascii=False)
    logger=getattr(task,'log_info',None)
    if callable(logger):logger(message)
    event=getattr(task,'dashboard_event',None)
    if callable(event):event('STEP_CONDITION',message)
