"""Pre-step condition retries; UNKNOWN never repeats input."""
import json
import math
import time
import uuid
from datetime import datetime,timezone


def is_loop(step):
    return step.get('condition_effect',{}).get('action')=='retry_step'


def validate_spec(spec):
    if not isinstance(spec.get('retry_step_id'),str) or not spec['retry_step_id']:
        raise ValueError('Step retry requires a target Step ID')
    if type(spec.get('max_cycles')) is not int or not 1<=spec['max_cycles']<=100:
        raise ValueError('Step retry maximum must be 1..100')
    for key,low,high in [('wait_seconds',.1,60),('timeout_seconds',1,1500)]:
        value=spec.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:
            raise ValueError(f'Step retry {key} outside {low}..{high}')


def before(runner,step,module_id,revision,stop):
    """Return (allowed, diagnostic, repeated records); caller runs original Step."""
    if not is_loop(step):return True,None,[]
    spec=step['condition_effect'];validate_spec(spec)
    started=time.monotonic()
    report={'step_id':step['step_id'],'retry_step_id':spec['retry_step_id'],
            'retries':0,'checks':0,'unknown_attempts':0,'status':'CHECKING','id':uuid.uuid4().hex}
    records=[]
    module=runner.store.get(module_id)
    from .step_control import validate_ranges
    validate_ranges(module['steps'])
    from .workflow_model import resolve_step
    from .step_conditions import evaluate
    effective=runner.display_mapping.apply(runner.task,module_id,resolve_step(module,step))
    target=next(s for s in module['steps'] if s['step_id']==spec['retry_step_id'])

    def failed(reason):
        report.update(status='BLOCKED',reason=reason,elapsed_seconds=round(time.monotonic()-started,3))
        callback=getattr(runner.task,'on_exhaustion',None)
        if callable(callback) and not report.get('cancelled'):
            try:callback({'kind':'recognition','node':getattr(runner,'_evidence_node_id',None) or module_id,
                'event_id':'step_retry:'+report['id'],'module':module_id,'step_id':step['step_id'],
                'attempt_count':report['retries'] or report['unknown_attempts'],
                'attempt_limit':spec['max_cycles'],'reason':reason,'source':'android_manual',
                'recognition':{'step_retry':report},
                'notify_discord':getattr(runner,'canvas_notify_discord',False)})
            except Exception:pass
        return False,report,records

    def boundary():
        from src.rwtd.manual_device_tests import ManualActionInterrupted
        try:
            runner._check_gesture_boundary(stop)
            if runner.task.controller.device_stop.is_set():
                report['cancelled']=True
                return False
            return True
        except ManualActionInterrupted:
            report['cancelled']=True
            return False

    while True:
        if not boundary():return failed('Conditional Step cancelled at operation boundary')
        if runner.store.get(module_id)['revision']!=revision:
            return failed('Conditional Step module changed')
        if time.monotonic()-started>=spec['timeout_seconds']:
            return failed('Conditional Step time limit reached')
        frame=None;device=None
        try:
            backend,device=runner.task.manual_backend()
            frame=runner._recognition_frame(backend)
            runner.task.validate_native_frame(frame)
            result=evaluate(effective['input_conditions'],frame,runner.task)
        except (ValueError,TypeError,OSError,RuntimeError) as exc:
            result={'evaluation':'left_to_right','result':'UNKNOWN','clauses':[],
                    'reason':type(exc).__name__+': '+str(exc)[:200]}
        report.update(result=result['result'],conditions=result,checks=report['checks']+1)
        evidence=getattr(runner.task,'failure_evidence',None)
        if evidence is not None and frame is not None:
            evidence.remember(getattr(runner,'_evidence_node_id',None) or step['step_id'],frame,device=device)
        logger=getattr(runner.task,'log_info',None)
        if callable(logger):logger('RWTD_STEP_RETRY '+json.dumps(dict(module=module_id,
            step=step['step_id'],retry_step=spec['retry_step_id'],retries=report['retries'],
            check=report['checks'],result=result['result']),ensure_ascii=False))
        if not boundary():return failed('Conditional Step cancelled after recognition')
        if time.monotonic()-started>=spec['timeout_seconds']:
            return failed('Conditional Step time limit reached')
        if result['result']==spec.get('when','YES'):
            report.update(status='READY',elapsed_seconds=round(time.monotonic()-started,3))
            return True,report,records
        if result['result']=='UNKNOWN':
            report['unknown_attempts']+=1
            if step.get('unknown_no_retry') or report['unknown_attempts']>=spec['max_cycles']:
                return failed('Conditional Step UNKNOWN retry limit reached')
        else:
            if report['retries']>=spec['max_cycles']:
                return failed('Conditional Step retry limit reached')
            # One explicit saved action only; intervening Steps do not run.
            repeated=runner._execute_canvas_steps([resolve_step(module,target)],module_id,revision,stop,
                loop_settings={'max_cycles':1,'round_wait_seconds':spec['wait_seconds']})
            rows=repeated.get('records',[repeated])
            for row in rows:
                row['step_retry_origin']=step['step_id']
                row['step_retry_id']=report['id']
                if row.get('reports_saved',True):runner.storage.finish(row)
            records.extend(rows)
            report['retries']+=1
            if (not repeated.get('action_completed') or repeated.get('failure_reason')
                    or repeated.get('device_inputs') is None
                    or repeated.get('status')=='SKIPPED' and repeated.get('device_inputs')==0):
                if not boundary():return failed('Conditional Step cancelled during configured retry')
                return failed('Configured retry Step failed; no replay of uncertain input')
        remaining=spec['timeout_seconds']-(time.monotonic()-started)
        if remaining<=0:return failed('Conditional Step time limit reached')
        if stop.wait(min(spec['wait_seconds'],remaining)):
            if not boundary():return failed('Conditional Step cancelled during retry wait')


def blocked_record(step,module,revision,report,records=()):
    return {'report_id':'step_retry_'+uuid.uuid4().hex,'step_id':step['step_id'],
        'module':step['name'],'workflow_module':module,'workflow_revision':revision,
        'parameters':dict(step),'timestamp_utc':datetime.now(timezone.utc).isoformat(),
        'status':'BLOCKED','action_completed':False,'failure_reason':report['reason'],
        'device_inputs':0,'input_attempted':any(r.get('input_attempted') for r in records),'reports_saved':False,
        'step_retry':report,'retryable_target_miss':False,
        'input_conditions':report.get('conditions',{'result':'UNKNOWN','clauses':[]})}
