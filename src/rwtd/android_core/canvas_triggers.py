"""JSON TriggerTask definitions and cooperative read-only polling; no device APIs."""
import copy
import math
import time
import uuid
import threading
from functools import wraps


def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return locked


def round_frame(cache, backend, device, payload, tasks, validate):
    """Cache native pixels only inside one unchanged observer round/device."""
    if not payload.get('task_id'):
        frame = backend.get_frame()
        validate(frame)
        return frame, uuid.uuid4().hex
    key = (payload['run_id'],payload['task_id'],payload['generation'],payload['round_id'])
    for old in list(cache):
        task = tasks.get(old[1])
        if (old[0] != payload['run_id'] or not task or task['status'] != 'RUNNING'
                or task['cursor'] is None or old[2:] != (task['generation'],task['rounds'])):
            cache.pop(old,None)
    cached = cache.get(key)
    if cached and cached[2] == device and cached[3] is backend and payload.get('fresh_frame') is not True:
        return cached[0], cached[1]
    frame = backend.get_frame()
    validate(frame)
    frame_id = uuid.uuid4().hex
    cache[key] = (frame,frame_id,copy.deepcopy(device),backend)
    for old in list(cache)[:-3]:
        cache.pop(old,None)
    return frame,frame_id


def reachable(graph, entry, stop_at_starts=False):
    found, pending = set(), [entry]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        if stop_at_starts and current != entry and any(n['id'] == current and n['kind'] == 'start' for n in graph['nodes']):
            continue
        found.add(current)
        pending.extend(e['target'] for e in graph['edges'] if e['source'] == current)
    return found


def definitions(graph):
    """The start node is the canonical stored TriggerTask, never executable Python."""
    output = []
    for node in graph['nodes']:
        if node['kind'] == 'start' and node.get('task_type') == 'TriggerTask':
            ids = reachable(graph, node['id'], stop_at_starts=True)
            output.append({'task_type':'TriggerTask', 'id':node['id'],
                           'name':node.get('name',node['id']), 'enabled':node.get('enabled',False),
                           'trigger_interval':node.get('trigger_interval',3),
                           'preempt_operations':node.get('preempt_operations',False),
                           'revision':graph.get('revision'),
                           'entry':node['id'],
                           'nodes':[copy.deepcopy(n) for n in graph['nodes'] if n['id'] in ids],
                           'edges':[copy.deepcopy(e) for e in graph['edges'] if e['source'] in ids]})
    return output


def main_graph(graph):
    entry = next(n for n in graph['nodes'] if n['id'] == graph['entry'])
    if entry.get('task_type') == 'TriggerTask':
        return None
    if not definitions(graph):
        return copy.deepcopy(graph)  # Preserve old validation and Guard semantics.
    ids = reachable(graph, graph['entry'])
    # Existing always-on guards still belong to the main Simulation runtime.
    for node in graph['nodes']:
        if node['kind'] == 'guard':
            ids |= reachable(graph,node['id'])
    result = copy.deepcopy(graph)
    result['nodes'] = [n for n in result['nodes'] if n['id'] in ids]
    for node in result['nodes']:
        node.pop('chain', None)
        node.pop('handoff_chain', None)
    result['edges'] = [e for e in result['edges'] if e['source'] in ids]
    result['ui'] = {'positions':{k:v for k,v in graph.get('ui',{}).get('positions',{}).items() if k in ids},
                    'viewport':copy.deepcopy(graph.get('ui',{}).get('viewport',{'x':0,'y':0,'zoom':1}))}
    return result


def validate_triggers(graph):
    allowed = {'start','ocr','coco','ocr_decision','condition','notify','module','action','wait','manual','end'}
    # A new START owns new scratch variables, even when two entries share a tail.
    for start in graph['nodes']:
        if start['kind'] != 'start' or start.get('task_type') == 'TriggerTask':
            continue
        ids = reachable(graph, start['id'], stop_at_starts=True)
        local = [n for n in graph['nodes'] if n['id'] in ids]
        producers = {n['output_key'] for n in local if n['kind'] == 'ocr'}
        for node in local:
            from src.rwtd.android_core.canvas_entry_guard import validate as validate_entry_guard
            validate_entry_guard(node, producers)
            if node.get('variable') and node['variable'] not in producers:
                raise ValueError('NEXT start boundary cannot borrow OCR variables: ' + node['id'])
            if node.get('ocr_retry',{}).get('precondition') not in ids and node.get('ocr_retry',{}).get('precondition'):
                raise ValueError('NEXT start boundary cannot borrow OCR preconditions: ' + node['id'])
    main = main_graph(graph)
    if main:
        main_ids = {node['id'] for node in main['nodes']}
        if any(node.get('ocr_retry',{}).get('precondition') not in main_ids
               for node in main['nodes'] if node.get('ocr_retry',{}).get('precondition')):
            raise ValueError('Main flow cannot borrow an OCR precondition from another task')
    for task in definitions(graph):
        # Runtime transfers ownership before entering a numbered Chain target.
        # Its downstream conditions use that Chain's inputs, not Trigger locals.
        from src.rwtd.android_core.canvas_chains import unique_chain_owner
        local_ids, pending = set(), [task['entry']]
        nodes = {n['id']:n for n in graph['nodes']}
        while pending:
            current = pending.pop()
            if current in local_ids:continue
            if current != task['entry'] and (nodes[current]['kind']=='start'
                    or unique_chain_owner(graph,current)):
                continue
            local_ids.add(current)
            pending.extend(e['target'] for e in graph['edges'] if e['source']==current)
        local_nodes = [n for n in task['nodes'] if n['id'] in local_ids]
        if any(n['kind'] not in allowed for n in local_nodes):
            raise ValueError('TriggerTask observes, not Android inputs: '+task['id'])
        producers = {n['output_key'] for n in local_nodes if n['kind'] == 'ocr'}
        for node in local_nodes:
            from src.rwtd.android_core.canvas_entry_guard import validate as validate_entry_guard
            validate_entry_guard(node,producers)
            pre = node.get('ocr_retry',{}).get('precondition')
            if pre and pre not in local_ids:
                raise ValueError('OCR cannot borrow a precondition from another task')
            if node.get('variable') and node['variable'] not in producers:
                raise ValueError('TriggerTask cannot borrow variables from another task')
            if node['kind'] == 'condition' and not node.get('variable'):
                raise ValueError('Trigger '+task['id']+' condition '+node['id']+' requires a task-local OCR variable')
        if not task['edges']:
            raise ValueError('TriggerTask requires a connected observation flow')


class TriggerTaskScheduler:
    """Independent clocks/state, one pending effect; the caller owns the task thread."""
    def __init__(self, clock=time.monotonic):
        from .time_guard import TimeGuards
        self.time_guards=TimeGuards()
        self.lock = threading.RLock()
        self.clock = clock
        self.tasks, self.queue = {}, []
        self.handoff_requests = []
        self.graph, self.source, self.notify_discord = None, 'simulation', False
        self.observations = {}
        self.modules = {}
        self.run_id = None
        self.next_index = 0
        self.generation = 0
        self.queue_inputs = False
        self.results = []
        self.exhaustion_callback = None
        from .solver_recovery import SolverRecovery
        self.recovery=SolverRecovery()
        self.parent_key=None

    @property
    @synchronized
    def active(self):
        return any(t['status'] not in {'IDLE','STOPPED','ERROR'} for t in self.tasks.values())

    @synchronized
    def start(self, graph, observations, source='simulation', notify_discord=False, modules=None, queue_inputs=False):
        if self.active:
            raise ValueError('Stop TriggerTasks before replacing definitions')
        from src.rwtd.android_core.canvas_scheduler import validate_canvas
        graph = validate_canvas(graph,modules or {})
        if source not in {'simulation','android_readonly','android_manual'} or type(notify_discord) is not bool or (source == 'simulation' and notify_discord):
            raise ValueError('Invalid TriggerTask source/notification permission')
        self._validate_observations(observations)
        from .solver_recovery import SolverRecovery
        self.recovery=SolverRecovery()
        self.parent_key=None
        self.generation += 1
        self.run_id = uuid.uuid4().hex
        self.graph = copy.deepcopy(graph)
        self.source, self.notify_discord = source, notify_discord
        self.observations = copy.deepcopy(observations)
        self.modules = copy.deepcopy(modules or {})
        if type(queue_inputs) is not bool or (queue_inputs and source != 'android_manual'):
            raise ValueError('Queued inputs require explicit manual Android activation')
        self.queue_inputs = queue_inputs
        self.results = []
        self.queue, self.tasks = [], {}
        self.handoff_requests.clear()
        now = self.clock()
        for data in definitions(graph):
            if data['enabled']:
                self.tasks[data['id']] = {'definition':data,'status':'RUNNING','reason':'等待輪詢',
                    'next_due':float('inf') if data['trigger_interval']==0 else now,'cursor':None,'current_node':None,'variables':{},
                    'pending':None,'rounds':0,'steps':0,'wait_until':0,
                    'notifications':[],'trace':[],'generation':self.generation,'ocr_attempts':{},
                    'unmet_attempts':{},'condition_probe':None,'fixed_counts':{}}

    @staticmethod
    def _validate_observations(values):
        from src.rwtd.android_core.canvas_scheduler import FIELDS
        if (not isinstance(values,dict) or set(values) - (FIELDS | {'features','ocr'})
                or any(key in values and not isinstance(values[key],dict) for key in ('features','ocr'))):
            raise ValueError('Unknown TriggerTask observation fields')

    @synchronized
    def observe(self, values):
        if self.source != 'simulation':
            raise ValueError('Real TriggerTasks cannot accept simulated observations')
        self._validate_observations(values)
        self.observations = copy.deepcopy(values)

    def _fail_round(self, task, reason, detail=None):
        """Fail this invocation without disabling the configured observer."""
        task['error_count'] = task.get('error_count', 0) + 1
        task['last_error'] = {'node':task.get('cursor'), 'reason':reason,
                              'detail':copy.deepcopy(detail or {}), 'at':self.clock()}
        task['status'] = 'RUNNING'
        task['route_active'] = False
        task_id = task['definition']['id']
        self.queue = [q for q in self.queue if q['task_id'] != task_id]
        self.handoff_requests = [link for link in self.handoff_requests if link['trigger'] != task_id]
        self._finish_round(task, reason+'；本次結束，保留監看等待下次觸發')
        # Positive-interval observers must not spin on malformed runtime data.
        if task['definition']['trigger_interval'] > 0:
            task['next_due'] = max(task['next_due'], self.clock()+1)

    def _finish_round(self, task, reason, completed=False):
        if task['definition']['trigger_interval']==0 and not completed and not task.get('route_active'):
            self.recovery.reject(task['definition']['id'])
        task['waiting_request'] = False
        task['fresh_frame'] = False
        task.pop('failure_cleanup', None)
        task.pop('start_completion_pending',None)
        from .flow_key import has_execution
        if completed and has_execution(task.get('flow_key')):
            if any(q['task_id']==task['definition']['id'] for q in self.queue):
                task['start_completion_pending']=True
            else:
                self.time_guards.complete(next(n for n in task['definition']['nodes'] if n['kind']=='start'))
        task.update(cursor=None,pending=None,reason=reason,ocr_attempts={},unmet_attempts={},condition_probe=None,fixed_counts={},next_due=self.clock()+task['definition']['trigger_interval'])
        task.update(entry_guard_ready=set(),entry_guard_attempts={})
        if task['definition']['trigger_interval'] == 0:
            task['next_due'] = float('inf')

    @synchronized
    def take_continuations(self):
        requests,self.handoff_requests=self.handoff_requests,[]
        return requests

    @synchronized
    def urgent_handoff_pending(self):
        return any(link.get('preempt_operations') and
                   (task:=self.tasks.get(link['trigger'])) and
                   task['status']=='RUNNING' and task.get('route_active')
                   for link in self.handoff_requests)

    def _cancel_waiting_triggers(self, winner):
        """Discard unstarted Trigger work when an urgent Trigger takes ownership."""
        cancelled=[q for q in self.queue if q['task_id']!=winner and
                   q['status']!='RUNNING' and not q.get('solver')]
        self.queue=[q for q in self.queue if q not in cancelled]
        links=[link for link in self.handoff_requests if link['trigger']!=winner and
               self.tasks.get(link['trigger'],{}).get('definition',{}).get('trigger_interval')!=0]
        self.handoff_requests=[link for link in self.handoff_requests if link not in links]
        for request in cancelled:
            self.results.append(dict(copy.deepcopy(request),status='CANCELLED',
                                     result={'status':'CANCELLED','action_completed':False,
                                             'cancel_reason':'插隊 Trigger 取消等待中的操作'}))
        for task_id in {q['task_id'] for q in cancelled} | {link['trigger'] for link in links}:
            task=self.tasks.get(task_id)
            if task and task['status']=='RUNNING':
                task['route_active']=False
                self._finish_round(task,'插隊 Trigger 取消等待中的操作；不計成功或失敗')
        self.results=self.results[-20:]

    @synchronized
    def cancel_waiting_routes(self, winner, routes):
        """Drop lower-priority NEXT links already taken by the owner."""
        kept=[]
        for link in routes:
            task=self.tasks.get(link['trigger'])
            if (link['trigger']==winner or not task or
                    task['definition']['trigger_interval']==0):
                kept.append(link)
            elif task['status']=='RUNNING' and task.get('route_active'):
                task['route_active']=False
                self._finish_round(task,'插隊 Trigger 取消等待中的交接；不計成功或失敗')
        return kept

    @synchronized
    def failure_event(self, problem=None):
        """Zero interval means one check per failed chain exit, not a hot loop."""
        candidates=[key for key,task in self.tasks.items()
                    if task['status']=='RUNNING' and task['definition']['trigger_interval']==0]
        guarded=[key for key in candidates if any(n['id']==key and n.get('entry_guard')
                 for n in self.tasks[key]['definition']['nodes'])]
        event=self.recovery.begin(problem,candidates,guarded)
        for task in self.tasks.values():
            if task['status']=='RUNNING' and task['definition']['trigger_interval']==0:
                self.queue=[q for q in self.queue if q['task_id']!=task['definition']['id']]
                task.update(cursor=None,pending=None,waiting_request=False,route_active=False,recovery_event=event)
                task['next_due'] = self.clock()

    @property
    @synchronized
    def failure_checks_pending(self):
        return any(t['status']=='RUNNING' and t['definition']['trigger_interval']==0
                   and (self.recovery.probe_eligible(t['definition']['id'])
                        or self.recovery.eligible(t['definition']['id']))
                   and (t['cursor'] is not None or t['pending'] or t['next_due'] != float('inf'))
                   for t in self.tasks.values())

    def _next(self, task, port, allow_fixed=True, mark_completed=True):
        node=next(n for n in task['definition']['nodes'] if n['id']==task['cursor'])
        task.setdefault('entry_guard_ready',set()).discard(node['id'])
        task.setdefault('entry_guard_attempts',{}).pop(node['id'],None)
        if allow_fixed and node['kind'] not in {'action','module','start'} and self._repeat_node(task,node):
            return
        if mark_completed and node['kind'] not in {'module','action','start'}:self.time_guards.complete(node)
        task['unmet_attempts'].pop(task['cursor'],None)
        task['ocr_attempts'].pop(task['cursor'],None)
        edge = next(e for e in task['definition']['edges'] if e['source'] == task['cursor'] and e['port'] == port)
        target = next(n for n in self.graph['nodes'] if n['id'] == edge['target'])
        from src.rwtd.android_core.canvas_chains import unique_chain_owner
        owner = unique_chain_owner(self.graph, target['id'])
        if owner and task.get('failure_cleanup'):
            self._finish_round(task,'前置辨識失敗；通知收尾不交接操作 Chain')
            return
        if owner:
            urgent=bool(task['definition'].get('preempt_operations') and
                        task['definition']['trigger_interval']>0)
            if urgent:
                self._cancel_waiting_triggers(task['definition']['id'])
            self.handoff_requests.append({'trigger':task['definition']['id'],
                                          'chain':owner,'target':target['id'],
                                          'flow_key':copy.deepcopy(task.get('flow_key')),
                                          'edge':edge['id'],
                                          'preempt_operations':urgent})
            # Ownership transfer is not END. Freeze this KEY until the owner
            # actually returns completion; do not consume a daily Guard here.
            task['route_active']=True
            self._finish_round(task, 'Trigger NEXT → ' + owner + ' / ' + target['id'])
            task['active_edge'] = edge['id']
            return
        task['entry_pending']=True
        task['cursor'], task['active_edge'] = edge['target'], edge['id']
        settings = self.graph.get('settings', {})
        if self.source == 'android_manual' or 'node_handoff_seconds' in settings:
            task['wait_until'] = max(task['wait_until'], self.clock() +
                                     settings.get('node_handoff_seconds', 1))

    @synchronized
    def control(self, task_id, operation, passed=None):
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError('Unknown TriggerTask')
        if operation in {'stop','pause'}:
            self.queue = [q for q in self.queue if q['task_id'] != task_id]
            self.handoff_requests=[r for r in self.handoff_requests if r['trigger']!=task_id]
            task['generation'] += 1
            task['status'] = 'STOPPED' if operation == 'stop' else 'PAUSED'
            self._finish_round(task,'使用者'+operation)
            task['variables'] = {}
        elif operation == 'resume' and task['status'] == 'PAUSED':
            task.update(status='RUNNING',next_due=float('inf') if task['definition']['trigger_interval']==0 else self.clock(),reason='恢復監看')
        elif operation == 'verify' and task['status'] == 'WAITING_MANUAL' and type(passed) is bool:
            task['status'] = 'RUNNING' if passed else 'STOPPED'
            self._finish_round(task,'人工處理完成' if passed else '人工停止')
        else:
            raise ValueError('TriggerTask control unavailable')

    @synchronized
    def stop(self):
        self.handoff_requests.clear()
        self.generation += 1
        for task in self.tasks.values():
            task['generation'] += 1
            task['status'] = 'STOPPED'
            self._finish_round(task,'使用者停止監看')
            task['variables'] = {}
        self.queue.clear()

    def _repeat_node(self,task,node):
        from src.rwtd.android_core.canvas_repeat import repeat_node
        if not repeat_node(node,task['fixed_counts']):
            return False
        task.setdefault('entry_guard_ready',set()).discard(node['id'])
        task['unmet_attempts'].pop(node['id'],None)
        task['ocr_attempts'].pop(node['id'],None)
        if node['kind']=='condition':
            task['condition_probe']=copy.deepcopy(next(p for p in task['definition']['nodes']
                if p.get('kind')=='ocr' and p.get('output_key')==node['variable']))
        task['wait_until']=self.clock()+self.graph.get('settings',{}).get('round_wait_seconds',1)
        task['reason']=f"固定執行 · {task['fixed_counts'][node['id']]}/{node['repeat_runs']} 已完成"
        return True

    def _retry_unmet(self, task, node, *, unknown=False):
        attempt=task['unmet_attempts'].get(node['id'],0)+1
        task['unmet_attempts'][node['id']]=attempt
        if unknown and node.get('unknown_no_retry') is True:
            task['reason']='UNKNOWN 不重試；本輪結束'
            return False
        maximum=self.graph.get('settings',{}).get('max_cycles',10)
        if maximum is not None and attempt>=maximum:
            self._exhaustion(task,node,attempt,maximum,'辨識嘗試次數已達上限')
            return False
        task['wait_until']=self.clock()+self.graph.get('settings',{}).get('round_wait_seconds',1)
        task['reason']='條件未成立；更新資料後重試 · '+str(attempt)+'/'+str(maximum or '不限')
        return True

    def _exhaustion(self,task,node,attempt,maximum,reason,kind='recognition'):
        if not callable(self.exhaustion_callback) or self.source=='simulation':return
        event={'kind':kind,'node':node['id'],'node_kind':node['kind'],'task_id':task['definition']['id'],
               'trigger_interval':task['definition']['trigger_interval'],
               'event_id':f"{self.run_id}:{task['definition']['id']}:{task['rounds']}:{node['id']}:{task['steps']}:{kind}:{attempt}",
               'attempt_count':attempt,'attempt_limit':maximum,'reason':reason,
               'source':self.source,'notify_discord':self.notify_discord}
        if kind=='node_limit':
            event.update(source_node=next((e['source'] for e in task['definition']['edges'] if e['id']==task.get('active_edge')),None),
                         target_node=node['id'],executed_count=attempt,execution_limit=maximum)
        try:self.exhaustion_callback(event)
        except Exception:pass

    @synchronized
    def poll(self, busy=False, task_id=None):
        now = self.clock()
        tasks = ([self.tasks[task_id]] if task_id in self.tasks else []) if task_id is not None else list(self.tasks.values())
        if not tasks:
            return None
        for offset in range(len(tasks)):
            index = (self.next_index+offset) % len(tasks)
            task = tasks[index]
            if task['status'] != 'RUNNING':
                continue
            if task.get('route_active'):
                continue
            solver=task['definition']['trigger_interval']==0
            probing=solver and self.recovery.probe_eligible(task['definition']['id'])
            if solver and not probing and not self.recovery.eligible(task['definition']['id']):
                continue
            if task.get('waiting_request'):
                continue
            if task['pending']:
                if not busy:
                    self.next_index = (index+1) % len(tasks)
                    return self._effect(task)
                continue
            if task['cursor'] is None:
                if any(q['task_id'] == task['definition']['id'] for q in self.queue):
                    continue  # Finish the queued branch before observing the same trigger again.
                if now < task['next_due']:
                    continue
                if busy:
                    task.update(next_due=now+task['definition']['trigger_interval'],reason='裝置忙碌；跳過本輪')
                    continue
                if task_id is None and sum(t['status'] == 'RUNNING' and t['cursor'] is not None for t in tasks) >= 3:
                    task.update(next_due=now+task['definition']['trigger_interval'],reason='輪詢空檔不足；跳過本輪')
                    continue
                task.update(cursor=task['definition']['entry'],variables={},steps=0,wait_until=0,
                            entry_guard_ready=set(),entry_guard_attempts={},chain_completed_runs=0,
                            entry_visits={},entry_pending=True, start_action_skipped=False)
                task['rounds'] += 1
                from .flow_key import start_key
                task['flow_key']=start_key(task['definition']['entry'],
                    purpose='solver' if task['definition']['trigger_interval']==0 else 'trigger',
                    parent=(self.parent_key or {}).get('id'),event=task.get('recovery_event'))
                if task['definition']['trigger_interval']==0 and self.source!='simulation':
                    task['pending']={'id':task['definition']['entry'],'kind':'flow_frame','phase':'baseline'}
                    return self._effect(task)
            if busy:
                continue
            if now < task['wait_until']:
                continue
            if task['condition_probe']:
                task['pending']=copy.deepcopy(task['condition_probe'])
                return self._effect(task)
            if solver and not probing and not self.recovery.eligible(task['definition']['id']):
                continue
            self.next_index = (index+1) % len(tasks)
            try:
                node = next(n for n in task['definition']['nodes'] if n['id'] == task['cursor'])
                task['current_node'] = node['id']
                if task.get('failure_cleanup') and node['kind'] not in {'notify','end'}:
                    # Failed observations cannot authorize input. Follow only
                    # explicit NEXT links toward notifications, never YES/NO.
                    task['steps'] += 1
                    if task['steps'] > 500:
                        self._finish_round(task,'失敗通知收尾超過 500 個轉移')
                        return None
                    task['trace'].append({'node':node['id'],'kind':node['kind'],
                        'status':'SKIPPED','reason':'前置辨識失敗；僅接續通知'})
                    task['trace'] = task['trace'][-50:]
                    if any(e['source']==node['id'] and e['port']=='NEXT' for e in task['definition']['edges']):
                        self._next(task,'NEXT',allow_fixed=False,mark_completed=False)
                    else:
                        self._finish_round(task,'前置辨識失敗；沒有可供通知接續的 NEXT')
                    return None
                # Input nodes check their explicit entry Guard in the owner executor,
                # per actual execution round, rather than on the observation snapshot.
                if node.get('entry_guard') and node['kind'] not in {'module','action'} and node['id'] not in task.setdefault('entry_guard_ready',set()):
                    if self.time_guards.blocked(node):
                        self._skip_node(task,node,'時間 Guard 未到期')
                        return None
                    from src.rwtd.android_core.canvas_entry_guard import probe,evaluate,needs_frame
                    if not needs_frame(node['entry_guard']):
                        self._complete_entry_guard(task,node,evaluate(node['entry_guard'],task['variables'],lambda c:None,lambda c:self.time_guards.allowed(node,c)))
                        return None
                    task['pending']=probe(node,self.graph['settings'],task.get('entry_guard_attempts',{}).get(node['id'],0)+1)
                    return self._effect(task)
                if probing:
                    # A successful START Guard waits until every Solver Guard
                    # has been checked before any candidate can advance.
                    continue
                if task.get('entry_pending',True):
                    visits=task.setdefault('entry_visits',{})
                    executed=visits.get(node['id'],0)
                    maximum=self.graph['settings'].get('max_cycles',10)
                    if maximum is not None and executed>=maximum:
                        self._exhaustion(task,node,executed,maximum,
                            f'節點 {node["id"]} 實際執行 {executed}/{maximum}；再次執行被拒絕','node_limit')
                        self._finish_round(task,'節點 '+node['id']+' 實際執行次數上限已達')
                        return None
                    visits[node['id']]=executed+1
                    task['entry_pending']=False
                task['steps'] += 1
                if task['steps'] > 500:
                    raise ValueError('TriggerTask round exceeded 500 transitions')
                task['trace'].append({'node':node['id'],'kind':node['kind']})
                task['trace'] = task['trace'][-50:]
                if node['kind'] in {'wait','condition','manual'}:
                    from .flow_key import participate
                    participate(task.get('flow_key'),node['id'],{'node_executed':True},kind=node['kind'])
                if node['kind'] in {'ocr','coco','ocr_decision','notify'}:
                    if node['kind'] == 'ocr':
                        task['variables'][node['output_key']] = {'verified':False,'value':None,'reason':'Await OCR'}
                    if node['kind'] == 'notify' and task.get('failure_cleanup'):
                        pending = copy.deepcopy(node)
                        failure = task['failure_cleanup']
                        text = 'UNKNOWN：'+failure['node']+' '+failure['reason']
                        pending['message'] = pending['message'].replace('{value}',text)
                        if '{value}' not in node['message']:
                            pending['message'] += '\n'+text
                        pending.pop('variable',None)
                        task['pending'] = pending
                        return self._effect(task)
                    if node['kind'] == 'notify' and node.get('variable') and task['variables'].get(node['variable'],{}).get('verified') is not True:
                        self._block_node(task,node,'通知變數 UNKNOWN')
                        return None
                    task['pending'] = copy.deepcopy(node)
                    return self._effect(task)
                if node['kind'] == 'condition':
                    from src.rwtd.android_core.canvas_scheduler import condition
                    result = condition(node,{},task['variables'])
                    if result is None and self._retry_unmet(task,node,unknown=bool(node.get('variable'))):
                        task['condition_probe']=copy.deepcopy(next(p for p in task['definition']['nodes']
                            if p.get('kind')=='ocr' and p.get('output_key')==node['variable']))
                    elif result is None:
                        self._finish_round(task,'UNKNOWN；等待下一輪')
                    else:
                        self._next(task,'YES' if result else 'NO',allow_fixed=result is True,
                                   mark_completed=result is True)
                elif node['kind'] in {'module','action'}:
                    if self.time_guards.blocked(node):
                        task['start_action_skipped'] = True
                        self._next(task,'NEXT', mark_completed=False)
                    else:
                        self._queue(task,node)
                        task['waiting_request'] = True
                elif node['kind'] == 'end':
                    if not self._repeat_node(task,node):
                        start = next(n for n in task['definition']['nodes'] if n['id']==task['definition']['entry'])
                        task['chain_completed_runs'] = task.get('chain_completed_runs',0)+1
                        if task['chain_completed_runs'] < start.get('repeat_runs',1):
                            task.update(cursor=start['id'],variables={},steps=0,pending=None,
                                        fixed_counts={},unmet_attempts={},ocr_attempts={},condition_probe=None,
                                        entry_guard_ready=set(),entry_guard_attempts={},entry_visits={},entry_pending=True,
                                        wait_until=now+self.graph['settings'].get('round_wait_seconds',1),
                                        reason='整條監看流程固定重複')
                            task['rounds'] += 1
                        else:
                            if task['definition']['trigger_interval']==0:
                                if self.source=='simulation':
                                    from .flow_key import finish_key
                                    comparison=self.recovery.finish(task['definition']['id'],{'valid':False})
                                    task['completion']=finish_key(task['flow_key'],node['id'],frame=comparison)
                                    self._finish_round(task,'Solver 到 END；模擬未驗證畫面',completed=True)
                                    return None
                                task['pending']={'id':node['id'],'kind':'flow_frame','phase':'end'}
                                return self._effect(task)
                            from .flow_key import finish_key
                            task['completion']=finish_key(task['flow_key'],node['id'])
                            self._finish_round(task,'本輪完成；等待下一輪',completed=True)
                elif node['kind'] == 'manual':
                    task.update(status='WAITING_MANUAL',reason='等待人工處理')
                else:
                    self._next(task,'NEXT')
                    if node['kind'] == 'wait':
                        task['wait_until'] = now+node['seconds']
            except (ValueError,KeyError,StopIteration,TypeError) as exc:
                self._fail_round(task, '監看執行例外：'+str(exc)[:500],
                                 {'error_type':type(exc).__name__})
            return None  # At most one transition/effect for all observers per tick.
        return None

    def _effect(self, task):
        return {'task_id':task['definition']['id'],'generation':task['generation'],
                'run_id':self.run_id,'round_id':task['rounds'],
                'node':copy.deepcopy(task['pending']),'source':self.source,
                'recovery_event':task.get('recovery_event'),
                'flow_key':copy.deepcopy(task.get('flow_key')),
                'ocr_attempt':task['ocr_attempts'].get(task['pending']['id'],0)+1,
                'fresh_frame':bool(task.get('fresh_frame') or task['pending'].get('_entry_guard_for') or task['condition_probe'] or task['unmet_attempts'].get(task['pending']['id'])
                                   or task['fixed_counts'].get(task['pending']['id'])),
                'notify_discord':self.notify_discord,'revision':self.graph['revision'],
                'variables':copy.deepcopy(task['variables'])}

    @synchronized
    def complete_continuation(self, key, end, frame=None):
        """Return a transferred START KEY only after its downstream END."""
        from .flow_key import finish_key
        task=next((t for t in self.tasks.values() if (t.get('flow_key') or {}).get('id')==key.get('id')),None)
        if task is None or task['status']!='RUNNING' or not task.get('route_active'):return None
        comparison=None
        if key.get('purpose')=='solver':
            # A guarded queue reservation may have released its rank before
            # NEXT transferred the KEY. Downstream input is still this Solver's
            # actual attempt and must count on the next recurrence.
            if key.get('operation_attempted'):
                self.recovery.select(task['definition']['id'])
            comparison=self.recovery.finish(task['definition']['id'],frame or {'valid':False})
        task['completion']=finish_key(key,end,frame=comparison)
        task['route_active']=False
        from .flow_key import has_execution
        if has_execution(key):
            self.time_guards.complete(next(n for n in task['definition']['nodes'] if n['id']==key['start']))
        task['next_due']=(float('inf') if task['definition']['trigger_interval']==0
                          else self.clock()+task['definition']['trigger_interval'])
        return task['completion']

    @synchronized
    def abandon_continuation(self, key, reason):
        for task in self.tasks.values():
            if task.get('route_active') and (task.get('flow_key') or {}).get('id')==key.get('id'):
                task['route_active']=False
                self._finish_round(task,reason)
                return

    def _complete_entry_guard(self,task,node,allowed,skip_now=False):
        if allowed is True:
            task.setdefault('entry_guard_ready',set()).add(node['id'])
            task.setdefault('entry_guard_attempts',{}).pop(node['id'],None)
            if task['definition']['trigger_interval']==0 and node['id']==task['definition']['entry']:
                self.recovery.guard_decision(task['definition']['id'],True)
            return
        counts=task.setdefault('entry_guard_attempts',{})
        counts[node['id']]=counts.get(node['id'],0)+1
        from .canvas_entry_guard import retry_limit
        maximum=retry_limit(node,self.graph['settings'])
        if allowed is None and not skip_now and (maximum is None or counts[node['id']]<maximum):
            task['wait_until']=self.clock()+self.graph['settings'].get('round_wait_seconds',1)
            task['reason']='附加 Guard UNKNOWN；等待重試'
            return
        if allowed is None and not skip_now and not node.get('unknown_no_retry'):
            self._exhaustion(task,node,counts[node['id']],maximum,'Guard UNKNOWN 重試耗盡')
        if task['definition']['trigger_interval']==0 and node['id']==task['definition']['entry']:
            self.recovery.guard_decision(task['definition']['id'],False)
        self._skip_node(task,node,'附加 Guard '+('未成立' if allowed is False else
            'UNKNOWN 不重試' if node.get('unknown_no_retry') is True else 'UNKNOWN 次數用完'))

    def _skip_node(self,task,node,reason):
        """Use the node library's skip route, independently of why it was denied."""
        from src.rwtd.android_core.canvas_node_flow import skip_port
        port=skip_port(node)
        task['fixed_counts'].pop(node['id'],None)
        if port is None:
            self._finish_round(task,reason+'；節點未執行，本輪結束')
        else:
            self._next(task,port,allow_fixed=False,mark_completed=False)
            task['reason']=reason+'；跳過 '+node['id']

    def _failed_read_notifications(self, task, node, sample):
        """Continue a failed read only as a no-input notification tail."""
        reason = str(sample.get('reason') or 'OCR 無有效結果')[:300]
        task['variables'][node['output_key']] = dict(sample,verified=False,value=None)
        task['failure_cleanup'] = {'node':node['id'],'reason':reason}
        if task['trace']:
            task['trace'][-1].update(status='SKIPPED',reason=reason)
        task['pending'] = None
        task['fixed_counts'].pop(node['id'],None)
        if any(e['source']==node['id'] and e['port']=='NEXT' for e in task['definition']['edges']):
            self._next(task,'NEXT',allow_fixed=False,mark_completed=False)
            task['reason']=reason+'；跳過失敗辨識及後續操作，接續通知'
        else:
            self._finish_round(task,reason+'；沒有 NEXT，結束本輪')

    def _block_node(self,task,node,reason):
        counts=task.setdefault('block_counts',{})
        counts[node['id']]=counts.get(node['id'],0)+1
        self._exhaustion(task,node,counts[node['id']],None,reason,'blocked')
        if any(e['source']==node['id'] and e['port']=='NEXT' for e in task['definition']['edges']):
            task['pending']=None
            task['waiting_request']=False
            self._next(task,'NEXT',allow_fixed=False,mark_completed=False)
            task['reason']=reason+'；BLOCK 已記錄，沿 NEXT 繼續'
        else:
            self._fail_round(task, reason+'；缺少 NEXT，未猜測出口')

    def _queue(self, task, node):
        key = (task['definition']['id'],node['id'],node['module'])
        if any((q['task_id'],q['node'],q['module']) == key for q in self.queue):
            task['reason'] = '同一處理模組已在待辦佇列'
            return
        solver=task['definition']['trigger_interval']==0
        if solver and not self.recovery.select(task['definition']['id']):
            return
        request={'task_id':key[0],'node':key[1],'module':key[2],
                           'solver':solver, 'flow_key':copy.deepcopy(task.get('flow_key')),
                           'recovery_event':task.get('recovery_event'),
                           'preempt_operations':bool(task['definition'].get('preempt_operations')
                                                     and task['definition']['trigger_interval']>0),
                           'id':uuid.uuid4().hex, 'kind':node['kind'], 'definition':copy.deepcopy(node),
                           'status':'QUEUED' if self.queue_inputs or self.source == 'simulation' else 'WAITING_DEVICE',
                           'run_id':self.run_id, 'generation':task['generation'],
                           'revision':self.graph['revision'],
                           'module_revision':self.modules.get(node['module'],{}).get('revision'),
                           'variables':copy.deepcopy(task['variables'])}
        if request['preempt_operations']:
            self._cancel_waiting_triggers(request['task_id'])
        if len(self.queue) >= 100:
            raise ValueError('TriggerTask queue is full')
        # The newest urgent match replaces other unstarted Trigger work.
        # Never move it ahead of an in-flight owner before a safe stop.
        if request['preempt_operations']:
            index=next((i for i,q in enumerate(self.queue)
                        if q['status']!='RUNNING' and not q.get('preempt_operations')),len(self.queue))
            self.queue.insert(index,request)
        else:
            index=next((i for i,q in enumerate(self.queue)
                        if q['status']!='RUNNING' and q.get('solver')),len(self.queue)) if not solver else len(self.queue)
            self.queue.insert(index,request)
        task['reason'] = '已加入操作佇列；等待執行端空閒'

    def simulate_guarded_request(self,request):
        """Cooperative fake macro rounds; no hidden capture or actual input."""
        now=self.clock()
        if self.time_guards.blocked(request['definition']):
            return {'status':'SKIPPED','guard_skipped':True,'action_completed':True,'device_inputs':0}
        if now<request.get('guard_due',0):
            return None
        from src.rwtd.android_core.canvas_entry_guard import probe,result_allowed
        sample=self.simulation_result({'node':probe(request['definition']),'variables':request['variables']})
        allowed=result_allowed(sample)
        if allowed is False:
            return {'status':'SKIPPED','guard_skipped':True,'action_completed':True,'device_inputs':0}
        if allowed is None:
            request['guard_attempts']=request.get('guard_attempts',0)+1
            from .canvas_entry_guard import retry_limit
            maximum=retry_limit(request['definition'],self.graph['settings'])
            if maximum is None or request['guard_attempts']<maximum:
                request['guard_due']=now+self.graph['settings'].get('round_wait_seconds',1)
                return None
            return {'status':'SKIPPED','guard_skipped':True,'action_completed':True,'device_inputs':0}
        request.pop('guard_attempts',None)
        request['completed_rounds']=request.get('completed_rounds',0)+1
        if request['completed_rounds']<request['definition'].get('repeat_runs',1):
            request['guard_due']=now+self.graph['settings'].get('round_wait_seconds',1)
            return None
        return {'status':'SIMULATED','action_completed':True,'device_inputs':0}

    @synchronized
    def preemption_pending(self):
        return self.urgent_handoff_pending() or any(q.get('preempt_operations') and q['status']=='QUEUED'
                   and q['run_id']==self.run_id
                   and (task:=self.tasks.get(q['task_id'])) and task['status']=='RUNNING'
                   and q['generation']==task['generation'] for q in self.queue)

    @synchronized
    def claim(self, allow_solver=True):
        """Only the owner executor claims a frozen request; observers never inject input."""
        if any(q['status']=='RUNNING' for q in self.queue):
            return None
        urgent=next((q for q in self.queue if q.get('preempt_operations') and q['status']=='QUEUED'),None)
        if urgent is not None:
            self.queue.remove(urgent)
            self.queue.insert(0,urgent)
        if not self.queue or self.queue[0]['status'] != 'QUEUED':
            return None
        request = self.queue[0]
        if request.get('solver') and not allow_solver:
            return None
        task = self.tasks.get(request['task_id'])
        if (not task or task['status'] != 'RUNNING' or request['run_id'] != self.run_id
                or request['generation'] != task['generation']):
            self.queue.pop(0)
            return None
        request['status'] = 'RUNNING'
        return copy.deepcopy(request)

    @synchronized
    def finish_request(self, request, result):
        current = next((q for q in self.queue if q['id'] == request['id']), None)
        if not current:
            return
        if result.get('status') in {'SIMULATED','SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'} and result.get('action_completed') is not False:
            self.time_guards.complete(current['definition'])
        self.queue.remove(current)
        self.results.append(dict(copy.deepcopy(current), status=result.get('status','FAILED'), result=copy.deepcopy(result)))
        self.results = self.results[-20:]
        task = self.tasks.get(request['task_id'])
        if task:
            from .flow_key import participate,has_execution
            participate(task.get('flow_key'),request['node'],result,kind=request.get('kind'))
            if request.get('solver'):
                if not (task.get('flow_key') or {}).get('operation_attempted'):
                    self.recovery.unused(request['task_id'])
            # The suspended owner's key remains unchanged. Contributions are
            # recorded against it rather than replacing its END behavior.
            if self.parent_key is not None:
                participate(self.parent_key,request['node'],result,actor=(task.get('flow_key') or {}).get('id'),kind=request.get('kind'))
            selected=(self.recovery.event or {}).get('selected')
            owner=self.tasks.get(selected)
            if owner is not None and owner is not task:
                participate(owner.get('flow_key'),request['node'],result,
                            actor=(task.get('flow_key') or {}).get('id'),kind=request.get('kind'))
            task['waiting_request'] = False
            if result.get('guard_skipped'):
                task['start_action_skipped'] = True
        if task and task.get('start_completion_pending'):
            successful=(result.get('status') in {'SIMULATED','SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'}
                        and result.get('action_completed') is not False and not result.get('guard_skipped'))
            if not successful:
                task.pop('start_completion_pending',None)
            elif not any(q['task_id']==request['task_id'] for q in self.queue) and has_execution(task.get('flow_key')):
                self.time_guards.complete(next(n for n in task['definition']['nodes'] if n['kind']=='start'))
                task.pop('start_completion_pending',None)
        if task and result.get('preempted'):
            task['start_action_skipped'] = True
            task.pop('start_completion_pending',None)
            task['fixed_counts'].pop(request['node'],None)
            self._next(task,'NEXT',allow_fixed=False,mark_completed=False)
            task['reason']='插隊取消目前節點；剩餘動作不再執行'
            return
        if task and result.get('recognition_retry_exhausted') is True:
            self.queue=[q for q in self.queue if q['task_id']!=request['task_id']]
            self._finish_round(task,'未找到操作目標；本輪次數用完，等待下一輪')
            return
        if task and (result.get('status') not in {'SKIPPED','SIMULATED','SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'}
                     or result.get('action_completed') is False):
            node=next(n for n in task['definition']['nodes'] if n['id']==request['node'])
            if result.get('status') in {'BLOCK','BLOCKED'}:
                self._block_node(task,node,result.get('failure_reason') or '佇列操作 BLOCKED')
            else:
                self._fail_round(task,'佇列操作失敗',
                                 {key:copy.deepcopy(result.get(key)) for key in
                                  ('status','action_completed','failure_reason','error_type','error_message',
                                   'device_inputs','input_attempted')})
            return
        if task and task['cursor'] == request['node']:
            task['fresh_frame'] = True
            self._next(task, 'NEXT', allow_fixed=False, mark_completed=not result.get('guard_skipped'))

    @synchronized
    def remove_request(self, task_id, node_id):
        if any(q['task_id'] == task_id and q['node'] == node_id and q['status'] == 'RUNNING' for q in self.queue):
            raise ValueError('Stop the monitor to cancel an executing request')
        previous = len(self.queue)
        self.queue = [q for q in self.queue if (q['task_id'],q['node']) != (task_id,node_id)]
        if len(self.queue) == previous:
            raise ValueError('Unknown queued TriggerTask request')
        self._finish_round(self.tasks[task_id], '操作佇列已移除；等待下一輪')

    @synchronized
    def complete(self, effect, result):
        task = self.tasks.get(effect['task_id'])
        if (not task or task['status'] != 'RUNNING' or effect['generation'] != task['generation']
                or effect.get('run_id') != self.run_id or effect.get('round_id') != task['rounds']
                or task['pending'] != effect['node']):
            return  # Stop/Pause cannot consume a late capture or notification.
        node = task['pending']
        task['pending'] = None
        if node['kind']=='flow_frame':
            if node.get('phase')=='baseline':
                if not result.get('valid'):
                    self._finish_round(task,'Solver 操作前畫面無效；不宣稱解決')
                return
            from .flow_key import finish_key
            comparison=self.recovery.finish(task['definition']['id'],result)
            task['completion']=finish_key(task['flow_key'],node['id'],frame=comparison)
            self._finish_round(task,'Solver 到 END；'+('畫面已變更' if comparison['changed'] else '尚未確認解決'),completed=True)
            return
        if not node.get('_entry_guard_for'):
            from .flow_key import participate
            record=dict(result or {})
            if record.get('verified') is True or record.get('status') in {'SIMULATED','local_only','native_submitted','delivered'}:
                record['node_executed']=True
            participate(task.get('flow_key'),node['id'],record,kind=node['kind'])
        if node['kind'] in {'ocr','coco','ocr_decision','entry_guard'}:
            task['fresh_frame'] = False
        if node.get('_entry_guard_for'):
            from src.rwtd.android_core.canvas_entry_guard import result_allowed
            target=next(n for n in task['definition']['nodes'] if n['id']==node['_entry_guard_for'])
            self._complete_entry_guard(task,target,result_allowed(result))
            return
        if task['condition_probe']==node:
            from src.rwtd.android_core.canvas_ocr import classify
            task['variables'][node['output_key']]=classify(node,result)
            task['condition_probe']=None
            return
        if node['kind'] == 'ocr':
            from src.rwtd.android_core.canvas_ocr import decide
            attempt = task['ocr_attempts'].get(node['id'],0)+1
            task['ocr_attempts'][node['id']] = attempt
            sample, route = decide(node,result,attempt)
            task['variables'][node['output_key']] = sample
            task['reason'] = sample['reason']+' · OCR '+str(attempt)
            if route == 'UNKNOWN' and 'ocr_cases' not in node:
                if self._retry_unmet(task,node,unknown=sample.get('state')!='no_match'):
                    return
                self._failed_read_notifications(task,node,sample)
                return
            if (route == 'UNKNOWN' and node.get('unknown_no_retry') is True
                    and (node.get('ocr_mode')=='raw' or sample.get('state')!='no_match')):
                self._finish_round(task,sample['reason']+'；UNKNOWN 不重試，本輪結束')
                return
            if sample.get('state')=='no_match' and 'ocr_retry' not in node:
                if self._retry_unmet(task,node):
                    return
                if route=='UNKNOWN':
                    self._finish_round(task,'OCR 次數用完；等待下一轮')
                    return
            if route == 'RETRY':
                task['wait_until'] = self.clock()+node['ocr_retry'].get('interval_seconds',1)
                return
            if route == 'UNKNOWN':
                self._finish_round(task,sample['reason']+'；等待下一輪')
                return
            if (route=='NO' and sample.get('state')=='no_match' and 'ocr_retry' in node
                    and attempt>=node['ocr_retry'].get('max_attempts',1)):
                self._exhaustion(task,node,attempt,node['ocr_retry'].get('max_attempts',1),sample['reason'],'ocr')
            self._next(task,route,allow_fixed=route!='NO',mark_completed=route!='NO')
        elif node['kind'] == 'ocr_decision':
            from .canvas_ocr_decision import evaluate
            decision, reading = evaluate(node, result)
            task['trace'][-1]['ocr_decision'] = reading
            if decision is None:
                if not self._retry_unmet(task, node, unknown=True):
                    self._finish_round(task, 'OCR 決策 UNKNOWN；本輪結束')
            else:
                self._next(task, 'YES' if decision else 'NO', allow_fixed=decision, mark_completed=decision)
        elif node['kind'] == 'coco':
            if not isinstance(result,dict) or result.get('verified') is not True or not result.get('frame_id') or type(result.get('detected')) is not bool:
                if self._retry_unmet(task,node,unknown=True):
                    task['reason']='COCO UNKNOWN；重新擷取 · '+str(task['unmet_attempts'][node['id']])+'/'+str(self.graph['settings'].get('max_cycles') or '不限')
                    return
                self._finish_round(task,'COCO UNKNOWN；不重試，本輪結束'
                    if node.get('unknown_no_retry') is True else 'COCO UNKNOWN；重試次數用完，等待下一輪')
                return
            self._next(task,'YES' if result['detected'] else 'NO',allow_fixed=result['detected'],
                       mark_completed=result['detected'])
        else:
            record = copy.deepcopy(result or {'status':'FAILED'})
            if record.get('status') == 'SIMULATED':
                sample = task['variables'].get(node.get('variable'),{})
                record['description'] = node['message'].replace('{value}',str(sample['value']) if sample.get('value') is not None else '')
            task['notifications'].append(record)
            task['notifications'] = task['notifications'][-20:]
            if record.get('status') not in {'SIMULATED','local_only','native_submitted','delivered','duplicate_suppressed','cooldown_suppressed'}:
                if record.get('status') in {'BLOCK','BLOCKED'}:
                    self._block_node(task,node,'通知 BLOCK：'+record['status'])
                else:
                    self._fail_round(task,'通知失敗',record)
                return
            self._next(task,'NEXT')

    def simulation_result(self, effect):
        if effect['node']['kind']=='flow_frame':
            # Fake runs must supply an explicit valid signature to claim a
            # visual recovery; an action callback alone is insufficient.
            return {'valid':False,'frame_id':None}
        if effect['node']['kind']=='entry_guard':
            from src.rwtd.android_core.canvas_entry_guard import evaluate,ocr_allowed
            def observe(clause):
                from .shared_ocr import PRESETS
                if clause.get('source')=='shared' or clause.get('mode')=='ocr_cooldown':
                    key=clause['variable'];value=self.observations.get('shared_ocr',{}).get(key)
                    sample=value if isinstance(value,dict) else {'value':value,'verified':type(value) in (int,float)}
                    if clause.get('mode')=='ocr_cooldown':
                        return self.time_guards.prepare(effect['node'],clause,sample)
                    from .canvas_entry_guard import variable_result
                    return variable_result(clause,{key:sample})
                if clause['kind']=='ocr':
                    key=clause['text']+'@'+','.join(str(v) for v in clause['roi'])
                    sample=self.observations.get('guard_ocr',{}).get(key)
                    return ocr_allowed(sample.get('tokens'),clause) if isinstance(sample,dict) and sample.get('verified') is True else None
                sample=self.simulation_result({'node':dict(clause,kind='coco')})
                return sample.get('detected') if sample.get('verified') is True else None
            allowed=evaluate(effect['node']['entry_guard'],effect.get('variables',{}),observe,lambda c:self.time_guards.allowed(effect['node'],c))
            return {'verified':allowed is not None,'detected':allowed,'frame_id':'simulation-entry-guard'}
        node = effect['node']
        if node['kind'] == 'ocr_decision':
            from .canvas_ocr_decision import probe
            node = probe(node)
        if node['kind'] == 'ocr':
            from src.rwtd.android_core.canvas_ocr import simulation_sample
            return simulation_sample(node,self.observations,self.graph)
        if node['kind'] == 'coco':
            key = node['feature']+('@'+','.join(str(v) for v in node['roi']) if node.get('roi') else '')
            sample = self.observations.get('features',{}).get(key)
            if not isinstance(sample,dict) or sample.get('verified') is not True or type(sample.get('detected')) is not bool:
                return {'verified':False}
            score = sample.get('confidence')
            if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 1:
                return {'verified':False}
            detected = sample['detected'] and score >= node.get('threshold',.8)
            return {'verified':True,'frame_id':'simulation-trigger','detected':detected if node.get('match','detected') == 'detected' else not detected}
        return {'status':'SIMULATED','delivery_verified':False}

    @synchronized
    def snapshot(self):
        return {'active':self.active,'source':self.source,'device_inputs':0,
                'solver_recovery':copy.deepcopy(self.recovery.event),
                'queue_inputs':self.queue_inputs, 'results':copy.deepcopy(self.results),
                'tasks':[{'id':t['definition']['id'],'name':t['definition']['name'],
                          'task_type':'TriggerTask','trigger_interval':t['definition']['trigger_interval'],
                          'preempt_operations':t['definition'].get('preempt_operations',False),
                          'revision':t['definition']['revision'],
                          **{k:copy.deepcopy(t.get(k)) for k in ('status','reason','current_node','rounds','variables','notifications','active_edge','ocr_attempts','fixed_counts','completion','observation_busy','observation_thread','observation_seconds','observation_discarded','last_error','error_count','last_guard_diagnostic')}}
                         for t in self.tasks.values()], 'queue':copy.deepcopy(self.queue)}
