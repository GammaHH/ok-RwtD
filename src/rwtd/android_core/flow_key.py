"""Execution identity belongs to START, never to a shared END node."""
import copy
import uuid


def start_key(start, *, purpose='flow', parent=None, event=None):
    return {'id':uuid.uuid4().hex, 'start':start, 'purpose':purpose,
            'parent':parent, 'event':event, 'participants':[], 'completion':None,
            'executed':False,'operation_attempted':False}


def participate(key, node, result, *, actor=None, kind=None):
    if not key:
        return
    key['participants'].append({'node':node, 'actor':actor or key['id'],
                                'status':result.get('status'),
                                'kind':kind,
                                'action_completed':result.get('action_completed'),
                                'input_attempted':bool(result.get('input_attempted')),
                                'node_executed':bool(result.get('node_executed')),
                                'device_inputs':result.get('device_inputs'),
                                'preempted':bool(result.get('preempted'))})
    item=key['participants'][-1]
    # Solver rank belongs to its own attempted operation, not an inserted
    # Trigger's contribution. Keep this bit beyond the bounded trace window.
    if item['actor']==key['id'] and kind in {'module','action'}:
        count=item['device_inputs']
        if (item['input_attempted'] or type(count) in (int,float) and count>0
                or count is None and item['status']=='INPUT_SENT_UNVERIFIED'
                or item['status']=='SIMULATED' and item['action_completed'] is not False):
            key['operation_attempted']=True
    key['participants']=key['participants'][-200:]
    key['executed']=has_execution(key)


def has_execution(key):
    """Skipped/cancelled input is not a completed START use or cooldown."""
    if not key:return False
    if key.get('executed'):return True
    for item in key['participants']:
        if item.get('preempted'):continue
        if item.get('node_executed'):return True
        if item.get('status') in {'SIMULATED','SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'} and item.get('action_completed') is not False:
            count=item.get('device_inputs')
            if (type(count) in (int,float) and count>0
                    or count is None and item.get('status')=='INPUT_SENT_UNVERIFIED'
                    or item.get('status')=='SIMULATED' and item.get('kind') in {'module','action'}):
                return True
    return False


def finish_key(key, end, *, frame=None):
    """Only the executor which actually reaches END may produce this record."""
    result={'key':key['id'], 'start':key['start'], 'end':end,
            'purpose':key['purpose'], 'parent':key['parent'], 'event':key['event'],
            'status':'SUCCESS', 'reached_end':True,
            'participants':copy.deepcopy(key['participants'])}
    if frame is not None:
        result['frame']=copy.deepcopy(frame)
    key['completion']=result
    return result
