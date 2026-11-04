"""Ordered chain ownership and bounded memory revisits; no device APIs."""
import copy
import math


def validate_chains(graph):
    from src.rwtd.android_core.canvas_triggers import reachable
    starts = {n['id']: n for n in graph['nodes'] if n['kind'] == 'start' and n.get('task_type') != 'TriggerTask'}
    orders = set()
    for node in graph['nodes']:
        data = node.get('chain')
        if data is None:
            continue
        if node['id'] not in starts or not isinstance(data, dict) or set(data) - {
                'order', 'type', 'failure_limit', 'run_limit', 'next', 'memory_retries', 'continue_condition'}:
            raise ValueError('Chain settings belong to an operation start')
        for key, default, maximum in [('order', 1, 100), ('failure_limit', 3, 1000),
                                       ('run_limit', 1, 1000), ('memory_retries', 3, 100)]:
            value = data.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid chain '+key)
        if data.get('order', 1) in orders:
            raise ValueError('Chain order must be unique')
        orders.add(data.get('order', 1))
        if data.get('type', 'ordinary') not in {'ordinary', 'memory'}:
            raise ValueError('Invalid chain type')
        if 'next' in data and data['next'] is not None and not isinstance(data['next'], str):
            raise ValueError('Chain handoff must be an ID or null')
        if data.get('next') and data['next'] not in starts:
            raise ValueError('Chain handoff must name an operation start')
        if data.get('next') and not starts[data['next']].get('chain'):
            raise ValueError('Chain handoff target must have chain settings')
        if data.get('next') == node['id']:
            raise ValueError('Use run_limit to repeat a chain, not a self handoff')
        rule = data.get('continue_condition')
        if rule is not None:
            from src.rwtd.android_core.canvas_scheduler import FIELDS
            if (not isinstance(rule, dict) or set(rule) - {'field', 'op', 'value', 'roi', 'threshold'}
                    or not {'field', 'op', 'value'}.issubset(rule)
                    or rule['field'] not in FIELDS or rule['op'] not in {'gte', 'lte', 'eq'}
                    or type(rule['value']) not in (int, float, bool)
                    or isinstance(rule['value'], float) and not math.isfinite(rule['value'])):
                raise ValueError('Invalid chain continuation condition')
            if 'roi' in rule:
                from src.rwtd.android_core.workflow_model import validate_roi
                validate_roi(rule['roi'])
            if (type(rule.get('threshold', .8)) not in (int, float)
                    or not .5 <= rule.get('threshold', .8) <= 1):
                raise ValueError('Invalid chain OCR threshold')
    # Explicit backward handoffs would defeat failure recovery. Memory returns
    # are handled separately with a finite retry budget.
    configured = {key: n for key, n in starts.items() if n.get('chain')}
    for node in configured.values():
        target = configured.get(node['chain'].get('next'))
        if target and target['chain'].get('order',1) <= node['chain'].get('order',1):
            raise ValueError('Chain handoffs must advance order; memory revisits are automatic')
    owners = {}
    kinds = {node['id']:node['kind'] for node in graph['nodes']}
    for start in configured.values():
        ids = reachable(graph, start['id'], stop_at_starts=True)
        ends = [node_id for node_id in ids if kinds[node_id] == 'end']
        if len(ends) != 1:
            raise ValueError('編號 Chain 必須一個開始對一個結束: ' + start['id'])
        for node_id in ids:
            previous = owners.setdefault(node_id, start['id'])
            if previous != start['id']:
                raise ValueError('編號 Chain 不可共用節點: ' + node_id + ' (' + previous + ', ' + start['id'] + ')')


def definitions(graph):
    from src.rwtd.android_core.canvas_triggers import reachable
    starts = [n for n in graph['nodes'] if n['kind'] == 'start' and n.get('task_type') != 'TriggerTask' and n.get('chain')]
    if not starts:
        entry = next(n for n in graph['nodes'] if n['id'] == graph['entry'])
        if entry['kind'] == 'start' and entry.get('task_type') != 'TriggerTask':
            starts = [entry]
    output = []
    for start in sorted(starts, key=lambda n: n.get('chain', {}).get('order', 1)):
        ids = reachable(graph, start['id'])
        data = copy.deepcopy(start.get('chain', {}))
        # Start repeats count completed chains; legacy run_limit remains readable.
        data['run_limit'] = start.get('repeat_runs', data.get('run_limit', 1))
        data.update(id=start['id'], name=start.get('name', start['id']))
        projected = copy.deepcopy(graph)
        projected.update(entry=start['id'], nodes=[n for n in projected['nodes'] if n['id'] in ids],
                         edges=[e for e in projected['edges'] if e['source'] in ids])
        projected.setdefault('ui', {'positions':{}, 'viewport':{'x':0,'y':0,'zoom':1}})
        projected['ui']['positions'] = {k: v for k, v in projected['ui'].get('positions', {}).items() if k in ids}
        # PROGRESS has a live OCR producer. Other legacy simulated fields still
        # have no Farm observer, so retain their previous projection behavior.
        if projected.get('settings', {}).get('exit_condition', {}).get('field') != 'progress_km':
            projected.setdefault('settings', {}).pop('exit_condition', None)
        # The owner sees linked starts, while observer registration stays global.
        for node in projected['nodes']:
            node.pop('chain', None)
            node.pop('task_type', None)
            node.pop('trigger_interval', None)
            node.pop('preempt_operations', None)
            if node['id'] == start['id']:
                node.pop('repeat_runs', None)  # Farm owns the outer chain counter.
        output.append(dict(data, graph=projected))
    for i, item in enumerate(output):
        item.setdefault('next', output[i+1]['id'] if i+1 < len(output) else None)
    return output


def unique_chain_owner(graph, node_id):
    """Resolve an ordinary target only when exactly one operation chain owns it."""
    from src.rwtd.android_core.canvas_triggers import reachable
    target = next((node for node in graph['nodes'] if node['id'] == node_id), None)
    if not target or target['kind'] in {'start', 'end', 'guard'}:
        return None
    owners = [start for start in graph['nodes']
              if start['kind'] == 'start' and start.get('task_type') != 'TriggerTask' and start.get('chain')
              and node_id in reachable(graph, start['id'], stop_at_starts=True)]
    return owners[0]['id'] if len(owners) == 1 else None


def continuation_definition(graph, target):
    """Resolve an existing entry from its graph ID, without registration."""
    from src.rwtd.android_core.canvas_triggers import reachable
    start = next(n for n in graph['nodes'] if n['id'] == target and n['kind'] == 'start')
    ids = reachable(graph, target)
    projected = copy.deepcopy(graph)
    projected.update(entry=target, nodes=[n for n in projected['nodes'] if n['id'] in ids],
                     edges=[e for e in projected['edges'] if e['source'] in ids])
    projected['ui'] = {'positions':{}, 'viewport':{'x':0, 'y':0, 'zoom':1}}
    if projected.get('settings', {}).get('exit_condition', {}).get('field') != 'progress_km':
        projected.setdefault('settings', {}).pop('exit_condition', None)
    for node in projected['nodes']:
        for field in ('chain','handoff_chain','task_type','trigger_interval','preempt_operations'):
            node.pop(field,None)
        if node['id'] == target:
            node.pop('repeat_runs',None)
    data = copy.deepcopy(start.get('chain',{}))
    data.update(id=target, name=start.get('name',target), graph=projected,
                run_limit=start.get('repeat_runs',data.get('run_limit',1)))
    return data


class ChainSchedule:
    def __init__(self, graph, rounds=None):
        validate_chains(graph)
        if rounds is not None and (type(rounds) is not int or not 1 <= rounds <= 100000):
            raise ValueError('Farm rounds must be null or a positive integer')
        self.chains = definitions(graph)
        if not self.chains:
            raise ValueError('Farm needs an operation start')
        self.by_id = {n['id']: n for n in self.chains}
        self.round_limit, self.rounds = rounds, 0
        self.current = self.chains[0]['id']
        self.failures, self.runs, self.revisits, self.memory = {}, {}, {}, {}
        # Lifetime statistics never participate in the per-round limits.
        self.total_failures, self.total_runs, self.consecutive_failures = {}, {}, {}
        self.memory_variables = {}
        self.return_to, self.retry_memory = None, []
        self.completed_handoff = None
        self.events = []
        self.handoff_events = []
        self.graph = copy.deepcopy(graph)

    def follow_next(self, target, *, trigger=None, target_node=None, edge=None):
        """Graph continuation does not earn success or consume retry budgets."""
        if target not in self.by_id:
            self.by_id[target] = continuation_definition(self.graph, target)
        event = {'source':self.current,'target':target,'applied':True,
                 'reason':'TRIGGER_CROSS_CHAIN' if trigger and self.current != target
                          else 'TRIGGER_ROUTE' if trigger else 'NEXT'}
        if trigger:
            event.update(trigger=trigger,target_node=target_node,edge=edge,
                         source_completed=False,source_failure_counted=False)
            source=self.by_id[self.current]
            # Only the configured immediate successor can earn a memory return.
            # A Trigger jump to a later Chain cannot leave a reusable return ticket.
            if (self.current!=target and source.get('type')=='memory'
                    and source.get('next') == target
                    and self.runs.get(self.current,0)<source.get('run_limit',1)
                    and self.failures.get(self.current,0)<source.get('failure_limit',3)
                    and self.revisits.get(self.current,0)<source.get('memory_retries',3)
                    and self.current not in self.retry_memory):
                self.retry_memory.append(self.current)
                event['memory_return_pending']=True
        self.handoff_events.append(event)
        self.handoff_events = self.handoff_events[-100:]
        self.current = target
        self.return_to = None
        self.completed_handoff = None

    @property
    def active(self):
        return self.current is not None

    def fail(self, reason, force_exit=False):
        key = self.current
        self.completed_handoff = None
        self.failures[key] = self.failures.get(key, 0)+1
        self.total_failures[key] = self.total_failures.get(key,0)+1
        self.consecutive_failures[key] = self.consecutive_failures.get(key,0)+1
        item = self.by_id[key]
        exhausted = self.failures[key] >= item.get('failure_limit', 3)
        if not force_exit and not exhausted and (item.get('type') != 'memory' or not item.get('next')):
            return False
        self.events.append({'chain': key, 'name': item['name'], 'reason': reason,
                            'failed': True, 'exhausted':exhausted})
        self.events = self.events[-100:]
        if (not exhausted and item.get('type') == 'memory'
                and self.revisits.get(key, 0) < item.get('memory_retries', 3)
                and self.runs.get(key, 0) < item.get('run_limit', 1)):
            if key not in self.retry_memory:
                self.retry_memory.append(key)
        return True

    def success(self):
        key = self.current
        self.runs[key] = self.runs.get(key, 0)+1
        self.total_runs[key] = self.total_runs.get(key,0)+1
        self.consecutive_failures[key] = 0
        completed = self.runs[key] >= self.by_id[key].get('run_limit', 1)
        self.completed_handoff = key if completed else None
        return completed

    def handoff(self, allow_memory_return=True):
        key = self.current
        completed = self.completed_handoff == key
        self.completed_handoff = None
        if self.return_to is not None:
            target, self.return_to = self.return_to, None
        else:
            target = self.by_id[key].get('next')
            candidates = [n for n in self.retry_memory if n != key
                          and self.by_id[n].get('next') == key
                          and self.failures.get(n,0)<self.by_id[n].get('failure_limit',3)
                          and self.runs.get(n,0)<self.by_id[n].get('run_limit',1)
                          and self.revisits.get(n,0)<self.by_id[n].get('memory_retries',3)] if allow_memory_return and completed else []
            if candidates:
                retry = candidates[0]
                self.retry_memory.remove(retry)
                self.revisits[retry] = self.revisits.get(retry, 0)+1
                self.return_to = target or '__ROUND_END__'
                target = retry
        # An unsuccessful/skipped successor cannot defer its return to C3/C4.
        self.retry_memory = [n for n in self.retry_memory if self.by_id[n].get('next') != key]
        if target == '__ROUND_END__':
            target = None
        if target is None:
            self.rounds += 1
            if self.round_limit is not None and self.rounds >= self.round_limit:
                self.current = None
                return
            self.failures.clear(); self.runs.clear(); self.revisits.clear(); self.retry_memory.clear()
            self.consecutive_failures.clear()
            # Task memory survives a round; only per-round counters reset here.
            # Fresh recognition/target validation still precedes actual input.
            target = self.chains[0]['id']
        self.current = target

    def stop(self):
        self.current = None
