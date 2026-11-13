"""Cooperative Farm owner: Trigger priority, chain handoff, scoped cache lifetime."""
import copy
import json
import math
import uuid
from datetime import datetime, timezone
from src.rwtd.android_core.canvas_chains import ChainSchedule
from src.rwtd.android_core.canvas_scheduler import CanvasRuntime, condition, validate_canvas
from src.rwtd.android_core.canvas_triggers import TriggerTaskScheduler


def failure_log_message(event):
    """Keep the failing node and chain budget visible in both log sinks."""
    fields = {key: event.get(key) for key in
              ('chain', 'name', 'node', 'node_name', 'node_kind',
               'reason', 'failure_count', 'failure_limit', 'failure_kind',
               'elapsed_seconds', 'timeout_minutes')}
    return 'RWTD_CHAIN_FAILURE ' + json.dumps(fields, ensure_ascii=False)


class FarmRuntime:
    def __init__(self, graph, modules, effect, queued, failure_notice, clock,
                 rounds=None, source='simulation', notifications=False, observe=None,
                 external_watchers=False, keep_watchers=False, invalidate_observers=None, time_guards=None,
                 exhaustion_callback=None, round_complete=None, round_context=None, farm_rounds=None):
        from .time_guard import TimeGuards
        self.time_guards=time_guards or TimeGuards()
        from .farm_round_variable import FarmRounds
        self.owns_round_counter=farm_rounds is None
        self.farm_rounds=farm_rounds if farm_rounds is not None else FarmRounds()
        self.time_guards.farm_rounds=self.farm_rounds
        self.exhaustion_callback=exhaustion_callback
        from .retreatable_km import resolve_graph
        self.graph = resolve_graph(validate_canvas(graph, modules))
        self.schedule = ChainSchedule(self.graph, rounds)
        self.modules, self.effect, self.queued = copy.deepcopy(modules), effect, queued
        self.failure_notice, self.clock, self.observe = failure_notice, clock, observe
        self.source, self.notifications = source, notifications
        self.external_watchers, self.keep_watchers = external_watchers, keep_watchers
        self.invalidate_observers = invalidate_observers
        self.round_complete = round_complete
        from .notification_timing import NotificationTiming
        self.notification_timing = NotificationTiming()
        self.round_context = copy.deepcopy(round_context or {})
        self.round_runtime_id = uuid.uuid4().hex
        self.round_started_at = self.clock()
        self.round_started_utc = datetime.now(timezone.utc)
        self.watchers = TriggerTaskScheduler(clock)
        self.watchers.exhaustion_callback=self.exhaustion_callback
        self.watchers.time_guards=self.time_guards
        self.watchers.start(self.graph, {}, source, notifications, self.modules,
                            queue_inputs=source=='android_manual')
        self.runtime = None
        self.handoff_pending = False
        self.handoff_failed = False
        self.pending_notice = None
        self.next_due = clock()
        self.reason, self.status = 'Farm 已啟動', 'RUNNING'
        self.next_visits = {}
        self.next_routes = []
        self.route_active = False
        self.activity = None
        self.activities = []
        self.activity_queue = []
        self.activity_resume_round = False
        self.activity_round_failed_exit = False
        self.failed_chain_exits = 0
        self.activity_child = None
        self.activity_history = []
        self.activity_paused_at = None
        self.activity_ready = None
        self.activity_switch = None
        self.activity_state = None
        self.activity_clock = clock
        self.chain_reading = None
        self._enter()

    @property
    def activity_active(self):
        return self.activity_child is not None

    @property
    def activity_pending(self):
        return self.activity_resume_round

    @property
    def active_runtime(self):
        """The sole owner of device effects; the suspended parent stays intact."""
        return self.activity_child or self

    def configure_activity(self, graph, interval_seconds, *, name='', activity_ready=None,
                           activity_switch=None, first_due=None):
        entries = [] if graph is None else [{'id':'activity', 'graph':graph, 'name':name,
                     'interval_seconds':interval_seconds, 'first_due':first_due}]
        self.configure_activities(entries, activity_ready=activity_ready, activity_switch=activity_switch)

    def configure_activities(self, entries, *, activity_ready=None, activity_switch=None,
                             activity_state=None, activity_clock=None):
        """Independent intervals; collect due entries only after a normal Farm round."""
        if self.activity_pending:
            raise ValueError('活動執行中不可替換活動設定')
        if not isinstance(entries, list):
            raise ValueError('活動設定必須是清單')
        prepared, ids = [], set()
        schedule_clock = activity_clock if activity_clock is not None else self.clock
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('活動設定無效')
            key = entry.get('id')
            interval = entry.get('interval_seconds')
            due = entry.get('first_due')
            initial_runs = entry.get('initial_runs',1)
            first_delay = entry.get('first_delay_seconds')
            max_pending_runs = entry.get('max_pending_runs', 0)
            if not isinstance(key, str) or not key or key in ids:
                raise ValueError('活動 ID 必須非空且不可重複')
            if (type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0):
                raise ValueError('活動間隔必須是大於零的秒數')
            if due is not None and (type(due) not in (int, float) or not math.isfinite(due)):
                raise ValueError('活動到期時間無效')
            if type(initial_runs) is not int or not 0 <= initial_runs <= 1000:
                raise ValueError('首次活動次數必須是 0 至 1000 的整數')
            if first_delay is not None and (type(first_delay) is not int or not 0 <= first_delay <= 365*86400):
                raise ValueError('首次倒數必須是 0 至 365 天的整數秒數')
            if type(max_pending_runs) is not int or not 0 <= max_pending_runs <= 1000:
                raise ValueError('活動最多累積場數必須是 0 至 1000 的整數')
            from .retreatable_km import with_stage, resolve_graph
            graph = resolve_graph(validate_canvas(with_stage(entry.get('graph'),
                self.graph.get('settings',{}).get('game_stage'), inherit=True), self.modules))
            ChainSchedule(graph, 1)
            from .time_guard import TimeGuards
            prepared.append({'id':key, 'graph':graph, 'name':str(entry.get('name',key)),
                             'canvas_id':entry.get('canvas_id',key),
                             'interval_seconds':interval,
                             'max_pending_runs':max_pending_runs,
                             'first_delay_seconds':first_delay,
                             'next_due':(schedule_clock()+(first_delay if first_delay is not None else
                                          interval if initial_runs==0 else 0)) if due is None else due,
                             'status':'WAITING', 'time_guards':entry.get('time_guards') or TimeGuards(),
                             'initial_runs':initial_runs, 'initial_completed':initial_runs==0,
                             'initial_runs_left':initial_runs,
                             'repetition':0, 'batch_runs':0})
            ids.add(key)
        if activity_state is not None:
            for entry in prepared:
                if not activity_state.restore(entry):
                    activity_state.save(entry)
        self.activity_state, self.activity_clock = activity_state, schedule_clock
        self.activities = prepared
        self.activity = prepared[0] if prepared else None
        self.activity_ready, self.activity_switch = activity_ready, activity_switch

    def activity_snapshot(self, *, include_runtime=True):
        now = self.activity_clock()
        def public(entry):
            result = {key:copy.deepcopy(value) for key,value in entry.items() if key not in {'graph','time_guards'}}
            if entry:
                slots = self._activity_due_slots(entry, now)
                initial = entry['initial_runs_left'] if (slots or entry.get('first_delay_seconds') is not None) and not entry.get('deferred_runs') else 0
                result['scheduled_pending_runs'] = self._scheduled_activity_runs(entry, slots, initial)
            return result
        data = public(self.activity or {})
        data.update(enabled=bool(self.activities), active=self.activity_active, pending=self.activity_pending,
                    entries=[public(entry) for entry in self.activities],
                    queue=[entry['id'] for entry in self.activity_queue],
                    history=copy.deepcopy(self.activity_history))
        if include_runtime and self.activity_child:
            data['runtime'] = self.activity_child.snapshot()
        return data

    def queue_activity(self, activity_id, runs=1):
        """Explicit remote request still uses the existing safe-round gate."""
        if type(runs) is not int or not 1<=runs<=100:raise ValueError('活動次數必須為 1～100')
        entry=next((row for row in self.activities if row['id']==activity_id),None)
        if entry is None:raise ValueError('找不到已啟用的活動 ID；請先使用 /activity list')
        if self.activity_pending:raise ValueError('活動批次交接／執行中，請稍後排入')
        if entry.get('manual_runs',0)+runs>100:raise ValueError('活動待辦次數超過 100')
        manual_runs = entry.get('manual_runs', 0) + runs
        if self.activity_state is not None:
            self.activity_state.save(dict(entry, manual_runs=manual_runs))
        entry['manual_runs'] = manual_runs
        entry['status']='WAITING_BOUNDARY'

    def _activity_event(self, status, **fields):
        self.activity_history.append(dict(status=status, at=self.clock(),
                                          id=self.activity['id'], name=self.activity['name'], **fields))
        self.activity_history = self.activity_history[-50:]

    @staticmethod
    def _activity_due_slots(entry, now):
        overdue = now - entry['next_due']
        return 0 if overdue < 0 else 1 + math.floor(overdue / entry['interval_seconds'])

    @staticmethod
    def _scheduled_activity_runs(entry, slots, initial_runs):
        periodic = slots - int(bool(initial_runs) and entry.get('first_delay_seconds') is None)
        limit = entry['max_pending_runs']
        return initial_runs + (min(periodic, limit) if limit else periodic)

    def _activity_start(self, repeat=False):
        if not repeat:
            self.activity = self.activity_queue.pop(0)
            deferred_runs = self.activity.pop('deferred_runs', 0)
            manual_runs = self.activity.pop('manual_runs', 0)
            slots = self._activity_due_slots(self.activity, self.activity_clock())
            initial_runs = self.activity['initial_runs_left'] if not deferred_runs else 0
            # Legacy schedules carry the initial batch in their first slot.
            # A custom countdown separates immediately available initial runs
            # from periodic recovery; running that batch must not advance its deadline.
            consumed_slots = max(1, slots) if initial_runs and self.activity.get('first_delay_seconds') is None else slots
            scheduled_runs = self._scheduled_activity_runs(self.activity, consumed_slots, initial_runs)
            self.activity['next_due'] += consumed_slots * self.activity['interval_seconds']
            self.activity['batch_runs'] = manual_runs + deferred_runs + scheduled_runs
            self.activity['repetition'] = 1
        child = FarmRuntime(self.activity['graph'], self.modules, self.effect, self.queued,
                            self.failure_notice, self.clock, rounds=1, source=self.source,
                            notifications=self.notifications, observe=self.observe,
                            external_watchers=self.external_watchers, keep_watchers=False,
                            invalidate_observers=self.invalidate_observers,
                            round_complete=self.round_complete,
                            farm_rounds=self.farm_rounds,
                            round_context=dict(self.round_context, scope='activity',
                                canvas_id=self.activity['canvas_id'], canvas=self.activity['name'],
                                activity_id=self.activity['id'], repetition=self.activity['repetition']),
                            time_guards=self.activity.get('time_guards'),
                            exhaustion_callback=self.exhaustion_callback)
        if self.activity_state is not None:
            self.activity_state.save(self.activity, starting=True)
        self.activity_paused_at = self.clock()
        self.activity_child = child
        self.activity['status'] = 'RUNNING'
        self._activity_event('STARTED', round=self.schedule.rounds,
                             repetition=self.activity['repetition'], batch_runs=self.activity['batch_runs'])
        if self.activity_switch:
            self.activity_switch(child)

    def _resume_after_activities(self, deferred=False):
        self.notification_timing.reset()
        self.activity_resume_round = False
        # Inserted activities belong to their own summaries, not the next
        # normal Farm round's elapsed time.
        self.round_started_at = self.clock()
        self.round_started_utc = datetime.now(timezone.utc)
        if self.schedule.active:
            self._enter()
            self.reason = ('其餘活動等待安全交接；FARM 繼續下一輪' if deferred else
                           '活動批次完成；FARM 繼續下一輪')
        else:
            if not self.keep_watchers:
                self.watchers.stop()
            self.status = 'STOPPED'
            self.reason = ('指定 Farm 輪數完成；未執行活動已延後' if deferred else
                           '指定 Farm 輪數與到期活動完成')
        if self.activity_switch:
            self.activity_switch(self)

    def _activity_tick(self, seconds):
        """Return True when activity ownership consumes this owner tick."""
        if self.activity_child is None:
            if not self.activity_pending:
                return False
            # A due activity holds the boundary while external observers drain.
            # Never start another parent node while their worker is retiring.
            try:
                self.activity = self.activity_queue[0]
                if self.activity_ready and not self.activity_ready():
                    for entry in self.activity_queue:
                        entry['status'] = 'WAITING_BOUNDARY'
                    return True
                # All old observations refer to the pre-activity game screen.
                # The integration callback has retired workers before this point.
                with self.watchers.lock:
                    self.watchers.queue.clear()
                    self.watchers.handoff_requests.clear()
                self.next_routes.clear()
                self._activity_start()
            except Exception as exc:
                self.status, self.reason = 'BLOCKED', '活動交接失敗：'+type(exc).__name__
                self.activity['status'] = 'FAILED'
                self._activity_event('FAILED', reason=self.reason)
                return True
        child = self.activity_child
        try:
            child.tick(seconds)
        except Exception as exc:
            self.status, self.reason = 'BLOCKED', '活動執行失敗：'+type(exc).__name__
            self.activity['status'] = 'FAILED'
            self._activity_event('FAILED', reason=self.reason)
            child.watchers.stop()
            return True
        if self.status != 'RUNNING':
            return True  # Parent/user cancellation during an effect wins.
        had_failed_exit = child.failed_chain_exits > 0
        # A Chain's exhausted budget is a normal handoff input, not an outer
        # activity stop. Let its own scheduler run successors, solvers/returns
        # and the configured settlement before deciding the activity outcome.
        if child.status not in {'RUNNING','STOPPED'}:
            self.status, self.reason = 'BLOCKED', '活動失敗；保留 FARM 暫停位置，等待處理'
            self.activity['status'] = 'FAILED'
            self._activity_event('FAILED', reason=child.reason)
            child.watchers.stop()
            return True
        if child.status == 'RUNNING':
            return True
        # The Canvas owns success, failure handoffs, skips and round completion.
        # Only a schedule stopped before completing its requested round is an
        # interrupted activity. Never impose an extra outer END/success gate.
        if child.schedule.active or child.schedule.rounds < 1:
            self.status, self.reason = 'BLOCKED', '活動排程提前中斷；FARM 保持暫停'
            self.activity['status'] = 'FAILED'
            self._activity_event('FAILED', reason=child.reason)
            return True
        elapsed = self.clock()-self.activity_paused_at
        self.next_due += elapsed
        self.chain_started_at += elapsed
        with self.watchers.lock:
            for task in self.watchers.tasks.values():
                for key in ('next_due','wait_until'):
                    if key in task:
                        task[key] += elapsed
            for request in self.watchers.queue:
                if 'guard_due' in request:
                    request['guard_due'] += elapsed
        self._activity_event('COMPLETED_WITH_FAILURES' if had_failed_exit else
                             'COMPLETED' if child.runtime.outcome == 'SUCCESS' else 'SKIPPED',
                             duration_seconds=elapsed, repetition=self.activity['repetition'],
                             batch_runs=self.activity['batch_runs'],
                             chain_failures=dict(child.schedule.total_failures),
                             chain_runs=dict(child.schedule.total_runs))
        self.activity_child = None
        self.activity_paused_at = None
        try:
            if not self.activity['initial_completed']:
                self.activity['initial_runs_left'] -= 1
                self.activity['initial_completed'] = self.activity['initial_runs_left'] == 0
            safe_next_activity = not had_failed_exit and child.runtime.outcome == 'SUCCESS'
            if not safe_next_activity:
                self.activity['deferred_runs'] = self.activity['batch_runs'] - self.activity['repetition']
                # Internal Canvas routing has finished; this is solely the next
                # cross-Canvas insertion gate. Keep due jobs/repeats pending for
                # the next safe main Farm round instead of discarding or running
                # them directly after an unsafe predecessor.
                self.activity['status'] = 'WAITING'
                for entry in self.activity_queue:
                    entry['status'] = 'WAITING'
                self.activity_queue.clear()
                self._activity_event('DEFERRED_UNSAFE_BOUNDARY',
                    initial_runs_left=self.activity['initial_runs_left'])
                self._resume_after_activities(deferred=True)
                return True
            if self.activity['repetition'] < self.activity['batch_runs']:
                self.activity['repetition'] += 1
                self._activity_start(repeat=True)
                return True
            self.activity['status'] = 'WAITING'
            if self.activity_queue:
                self._activity_start()
            else:
                self._resume_after_activities()
        except Exception as exc:
            self.status, self.reason = 'BLOCKED', 'FARM 恢復交接失敗：'+type(exc).__name__
            self.activity['status'] = 'FAILED'
        return True

    def _enter(self, start_node=None):
        self.chain_reading = None
        handoff = self.runtime is not None
        item = self.schedule.by_id[self.schedule.current]
        runtime = CanvasRuntime()
        runtime.time_guards=self.time_guards
        runtime.exhaustion_callback=self.exhaustion_callback
        runtime.start(item['graph'], {}, self.modules, self.source, self.notifications, unattended=True)
        runtime.entry_visits[item['id']]=self.next_visits.get(item['id'],0)
        runtime.owner_boundaries = True
        # Reuse explicit node retry policy; BLOCKED/UNKNOWN is bounded by the
        # chain failure budget without restarting already completed operations.
        runtime.exclusion_regions = copy.deepcopy(self.schedule.memory)
        if item.get('type')=='memory':
            runtime.variables = copy.deepcopy(self.schedule.memory_variables.get(item['id'],{}))
        if start_node is not None:
            if not any(node['id'] == start_node and node['kind'] not in {'start','guard'}
                       for node in runtime.graph['nodes']):
                raise ValueError('Trigger NEXT target is not in the selected Chain')
            runtime.cursor = runtime.current_node = start_node
            runtime.reason = 'Trigger NEXT 接手節點 ' + start_node
        self.runtime = runtime
        # The Farm owner measures wall time, including observer work. Its child
        # Canvas must not maintain a second, tick-based timeout.
        runtime.owner_chain_timeout = True
        self.chain_started_at = self.clock()
        self.watchers.parent_key=runtime.flow_key
        self.next_due = self.clock() + (self.graph['settings'].get('node_handoff_seconds', 1) if handoff else 0)

    def stop(self):
        self.notification_timing.reset()
        if self.activity_child:
            self.activity_child.stop()
            if self.activity:
                self.activity['status'] = 'STOPPED'
        for entry in self.activity_queue:
            entry['status'] = 'STOPPED'
        self.activity_queue.clear()
        self.activity_resume_round = False
        self.status, self.reason = 'STOPPED', '使用者停止 Farm'
        self.schedule.stop()
        self.watchers.stop()
        self.next_routes.clear()
        self.route_active = False
        if self.runtime:
            self.runtime.control('stop')
            self.runtime.variables.clear()
            self.runtime.observations.clear()
            self.runtime.trace.clear()

    def restart_from_first_chain(self):
        """Abandon the interrupted round and re-enter C1 with fresh observations."""
        if self.activity_child is not None:
            self.activity_child.stop()
            if self.activity_state is not None:
                self.activity_state.restore(self.activity)
            else:
                entry = self.activity
                entry['deferred_runs'] = entry['batch_runs'] - entry['repetition']
                entry['initial_runs_left'] = max(0, entry['initial_runs_left'] - 1)
                entry['initial_completed'] = entry['initial_runs_left'] == 0
        self.activity_child = None
        self.activity_paused_at = None
        self.activity_queue.clear()
        self.activity_resume_round = False
        self.activity_round_failed_exit = False
        for entry in self.activities:
            entry['status'] = 'WAITING'
        self.runtime.control('stop')
        previous = self.schedule
        self.schedule = ChainSchedule(self.graph, previous.round_limit)
        self.schedule.rounds = previous.rounds
        self.schedule.total_failures = previous.total_failures
        self.schedule.total_runs = previous.total_runs
        self.schedule.events = previous.events
        self.schedule.handoff_events = previous.handoff_events
        self.watchers.stop()
        self.watchers.start(self.graph, {}, self.source, self.notifications, self.modules,
                            queue_inputs=self.source=='android_manual')
        self.next_visits.clear()
        self.next_routes.clear()
        self.handoff_pending = self.handoff_failed = self.route_active = False
        self.pending_notice = None
        self.notification_timing.reset()
        self.round_started_at = self.clock()
        self.round_started_utc = datetime.now(timezone.utc)
        self.status, self.reason = 'RUNNING', '遊戲已回到前景；從 C1 重新開始'
        self._enter()

    def _leave(self, failed=False):
        if failed:
            self.notification_timing.failure()
            self.activity_round_failed_exit = True
            self.failed_chain_exits += 1
        if self.runtime.flow_key and self.runtime.outcome!='SUCCESS':
            self.watchers.abandon_continuation(self.runtime.flow_key,'交接未到 END；不產生完成結果')
        if self.invalidate_observers:
            # Clear prior observation rounds BEFORE scheduling zero-interval
            # failure checks. Never cancel that new check during the handoff.
            self.invalidate_observers()
        item = self.schedule.by_id[self.schedule.current]
        if item.get('type') == 'memory':
            self.schedule.memory.update(copy.deepcopy({k:v for k,v in self.runtime.exclusion_regions.items() if not v.get('target_node')}))
            self.schedule.memory_variables[item['id']] = copy.deepcopy({k:v for k,v in self.runtime.variables.items()
                if isinstance(v,dict) and v.get('verified') is True})
        self.runtime.control('stop')
        # Keep counters in ChainSchedule, and frozen queued variables in the
        # queue; release only this chain's recognition/flow scratch data.
        self.runtime.variables.clear()
        self.runtime.observations.clear()
        self.runtime.action_plan = self.runtime.action_result = None
        self.runtime.trace.clear()
        self.handoff_failed = failed
        self.route_active = False
        if failed and (self.schedule.events[-1]['exhausted']
                       or self.schedule.events[-1].get('failure_kind') == 'chain_timeout'):
            event = self.schedule.events[-1]
            self.pending_notice = copy.deepcopy(event)
            if callable(self.exhaustion_callback) and self.source!='simulation':
                # Preserve the failure frame before solver observations can evict it.
                try:
                    self.exhaustion_callback(dict(event,kind='chain',source=self.source,
                        event_id=f"{self.runtime.evidence_run_id}:chain:{event['chain']}:{event.get('failure_count')}",
                        notify_discord=self.notifications))
                    self.pending_notice['evidence_dispatched']=True
                except Exception:pass
        if failed:
            # Capture exhausted evidence before waking solvers. A memory exit
            # with budget remaining also offers the zero-interval solvers a round.
            event=self.schedule.events[-1]
            self.watchers.failure_event(str((self.schedule.current,event.get('node'),event.get('node_kind'))))
        self.handoff_pending = True

    def _failed_attempt(self, reason, force_exit=False):
        if self.schedule.fail(reason, force_exit=force_exit):
            # Capture before _leave stops the runtime and clears its trace.
            node_id = self.runtime.current_node or self.runtime.cursor
            node = next((n for n in self.runtime.graph['nodes'] if n['id'] == node_id), {})
            item = self.schedule.by_id[self.schedule.current]
            self.schedule.events[-1].update(
                node=node_id, node_name=node.get('name', ''), node_kind=node.get('kind'),
                failure_count=self.schedule.failures[self.schedule.current],
                failure_limit=item.get('failure_limit', 3),
                chain_reading=copy.deepcopy(self.chain_reading))
            if force_exit:
                self.schedule.events[-1].update(failure_kind='chain_timeout',
                    elapsed_seconds=max(0, self.clock()-self.chain_started_at),
                    timeout_minutes=self.graph['settings'].get('chain_timeout_minutes',25))
                self.runtime.outcome = 'FAILED'
                self.runtime.chain_timed_out = True
            self._leave(True)
            if force_exit:
                self.runtime.reason = reason
                self.reason = reason
            return
        self.runtime.status, self.runtime.outcome = 'RUNNING', None
        self.runtime.pending_effect = None
        self.runtime.condition_probe = None
        self.runtime.unmet_attempts.clear()
        self.runtime.exit_unmet_attempts.clear()
        self.runtime.progress_reading = None
        self.runtime.exit_progress_reading = None
        self.runtime.ocr_attempts.clear()
        self.runtime.entry_guard_attempts.clear()
        self.runtime.due = self.runtime.now
        self.next_due = self.clock()+self.graph['settings'].get('round_wait_seconds',1)

    def _follow_next(self, target, routed=False, link=None):
        previous_started_at = self.chain_started_at
        # Ownership transfer is not execution. The target runtime first checks
        # its Guard, then applies its own actual-entry count and failure label.
        self.next_routes = [route for route in self.next_routes
                            if route.get('chain',route['target']) != target]
        item = self.schedule.by_id[self.schedule.current]
        if item.get('type') == 'memory' and not self.handoff_pending:
            self.schedule.memory.update(copy.deepcopy({k:v for k,v in self.runtime.exclusion_regions.items() if not v.get('target_node')}))
            self.schedule.memory_variables[item['id']] = copy.deepcopy({k:v for k,v in self.runtime.variables.items()
                if isinstance(v,dict) and v.get('verified') is True})
        self.runtime.control('stop')
        self.runtime.variables.clear()
        self.schedule.follow_next(target,
                                  trigger=link.get('trigger') if link else None,
                                  target_node=link.get('target') if link else None,
                                  edge=link.get('edge') if link else None)
        if routed:
            self.route_active = True
        self.handoff_pending = self.handoff_failed = False
        # The stored chain failure notification remains due even if a solver
        # has chosen an explicit NEXT entry.
        if self.pending_notice:
            try:
                self.failure_notice(self.pending_notice)
            except Exception:
                self.reason = '失敗通知提交失敗；仍繼續 NEXT'
            self.pending_notice = None
        if self.status != 'RUNNING':
            return
        self._enter(start_node=link.get('target') if link else None)
        if item['id'] == target:
            # Routing to another node in the same Chain is not a fresh entry.
            self.chain_started_at = previous_started_at
        if link and link.get('flow_key'):
            self.runtime.flow_key=copy.deepcopy(link['flow_key'])
            self.watchers.parent_key=self.runtime.flow_key
        if link:
            self.reason=('Trigger 達成跨鍊交接' if item['id'] != target else 'Trigger 接手流程') + \
                        f"：{item['id']} → {target} / {link['target']}；來源未完成，不計成功或失敗"

    def _observers(self):
        effect = self.watchers.poll(busy=False)
        if effect:
            try:
                result = self.watchers.simulation_result(effect) if self.source=='simulation' else self.effect(effect)
            except Exception as exc:
                result = {'verified':False,'status':'FAILED','error_type':type(exc).__name__}
            if self.status != 'RUNNING':
                return True
            self.watchers.complete(effect, result)
        if self.watchers.urgent_handoff_pending():
            return False  # Resolve the urgent NEXT before claiming another Trigger.
        # Finish one observation branch before executing its queued macro.
        request = self.watchers.claim(allow_solver=self.handoff_pending or self.runtime.status=='STOPPED')
        if request:
            try:
                result = self.queued(request)
            except Exception as exc:
                result = {'status':'FAILED','action_completed':False,'error_type':type(exc).__name__,
                          'error_message':str(exc)[:500]}
            if self.status != 'RUNNING':
                return True
            self.watchers.finish_request(request, result)
            return True
        return bool(any(not q.get('solver') for q in self.watchers.queue)
                    or self.handoff_pending and self.watchers.failure_checks_pending
                    or any(t['cursor'] is not None and t['status']=='RUNNING'
                           and t['definition']['trigger_interval']>0 for t in self.watchers.tasks.values()))

    def check_chain_timeout(self):
        if self.status != 'RUNNING' or self.activity_pending or self.activity_active:
            return False
        # Check before claiming more observer work so a busy Trigger cannot
        # postpone expiry indefinitely. In-flight input completes cooperatively.
        if (not self.handoff_pending and not self.runtime.pending_effect
                and self.runtime.status in {'RUNNING','BLOCKED','WAITING_MANUAL'}
                and self.clock()-self.chain_started_at >=
                    self.graph['settings'].get('chain_timeout_minutes',25)*60):
            self._failed_attempt('CHAIN 執行時長達上限；失敗退出交接', force_exit=True)
            return True
        return False

    def tick(self, seconds=.2):
        if self.status != 'RUNNING':
            return
        if self._activity_tick(seconds):
            return
        if self.check_chain_timeout():
            return
        # Observers and all matched Trigger macros precede operation chains.
        observer_busy = not self.external_watchers and self._observers()
        # NEXT from a Trigger applies at the current operation boundary,
        # independently of whether a whole CHAIN has finished.
        continuations = self.watchers.take_continuations()
        urgent=next((link for link in continuations if link.get('preempt_operations')),None)
        if urgent:
            self.next_routes=self.watchers.cancel_waiting_routes(urgent['trigger'],self.next_routes)
        for link in continuations:
            target = link.get('chain',link['target'])
            if (target == self.schedule.current and not link.get('chain')
                    and not self.handoff_pending and self.runtime.status != 'STOPPED'):
                continue
            if not any(item.get('chain',item['target'])==target and item['target']==link['target']
                       for item in self.next_routes):
                self.next_routes.append(copy.deepcopy(link))
        self.next_routes.sort(key=lambda link:not link.get('preempt_operations'))
        if self.next_routes and not self.route_active and not self.runtime.pending_effect:
            route = self.next_routes.pop(0)
            self._follow_next(route.get('chain',route['target']), routed=True,
                              link=route if route.get('chain') else None)
            return
        if observer_busy:
            return
        if self.handoff_pending:
            if self.external_watchers and (self.watchers.failure_checks_pending or self.watchers.queue):
                return
            if self.pending_notice:
                try:
                    self.failure_notice(self.pending_notice)
                except Exception:
                    self.reason = '失敗通知提交失敗；仍繼續交接'
                self.pending_notice = None
            if self.status != 'RUNNING':
                return
            previous_round = self.schedule.rounds
            # Activity insertion requires the Canvas's last Chain to reach END
            # and no failed Chain exit anywhere in this round. A memory return
            # that later succeeds does not erase the earlier unsafe exit.
            normal_round = (not self.activity_round_failed_exit and
                            self.runtime.outcome == 'SUCCESS' and not self.handoff_failed)
            self.schedule.handoff(allow_memory_return=not self.handoff_failed)
            self.handoff_pending = False
            self.next_visits = {}
            if self.schedule.rounds != previous_round:
                if self.owns_round_counter:
                    self.farm_rounds.advance(self.schedule.rounds)
                if self.round_complete:
                    elapsed = max(0, self.clock()-self.round_started_at)
                    ended = datetime.now(timezone.utc)
                    self.round_complete(dict(self.round_context,
                        runtime_id=self.round_runtime_id, round=self.schedule.rounds,
                        revision=self.graph.get('revision'), seconds=round(elapsed, 6),
                        start_utc=self.round_started_utc.isoformat(), end_utc=ended.isoformat(),
                        outcome='SUCCESS' if normal_round else 'COMPLETED_WITH_FAILURES',
                        details={'total_failures':dict(self.schedule.total_failures),
                                 'total_runs':dict(self.schedule.total_runs)}))
                self.round_started_at = self.clock()
                self.round_started_utc = datetime.now(timezone.utc)
                self.notification_timing.reset()
                self.activity_round_failed_exit = False
                if getattr(self,'remote_stop_after_round',False):
                    self.stop()
                    self.reason='Discord 指定主 FARM 本輪完成後停止；未插入活動'
                    return
                due_activities = [entry for entry in self.activities
                                  if entry.get('deferred_runs') or entry.get('manual_runs',0)>0
                                  or (entry.get('first_delay_seconds') is not None and entry['initial_runs_left'] > 0)
                                  or self.activity_clock() >= entry['next_due']]
                if normal_round:
                    self.activity_queue = due_activities
                    if self.activity_queue:
                        self.activity_resume_round = True
                        for entry in self.activity_queue:
                            entry['status'] = 'WAITING_BOUNDARY'
                        return
                elif due_activities:
                    self._activity_event('INSERTION_DEFERRED', round=self.schedule.rounds,
                        reason='previous_canvas_failed_exit_or_no_terminal_end',
                        pending_activity_ids=[entry['id'] for entry in due_activities])
            if not self.schedule.active:
                if self.keep_watchers:
                    self.status = 'STOPPED'
                else:
                    self.stop()
                self.reason = '指定 Farm 輪數完成'
                return
            self._enter()
        if self.clock() < self.next_due:
            return
        item = self.schedule.by_id[self.schedule.current]
        rule = item.get('continue_condition')
        if rule:
            self.chain_reading = None
            try:
                values = self.observe(rule) if self.observe else self.runtime.observations
            except Exception:
                values = {}
            if self.status != 'RUNNING':
                return
            allowed = condition(rule, values or {})
            if allowed is None:
                self._failed_attempt('Chain 繼續條件 UNKNOWN')
                return
            if not allowed:
                self._leave()
                return
        self.runtime.tick(seconds)
        entry=self.runtime.graph['entry']
        self.next_visits[entry]=self.runtime.entry_visits.get(entry,0)
        if self.runtime.status=='WAITING_MANUAL' and self.runtime.waiting_kind=='action_execute':
            self.runtime.control('execute_once', {'explicit_activation':True})
        node = copy.deepcopy(self.runtime.pending_effect)
        if node:
            context = {'node':node, 'source':self.source, 'notify_discord':self.notifications,
                       'revision':self.graph['revision'], 'variables':copy.deepcopy(self.runtime.variables),
                       'ocr_attempt':self.runtime.ocr_attempts.get(node['id'],0)+1}
            try:
                result = self.effect(context)
            except Exception as exc:
                result = {'status':'FAILED','action_completed':False,'error_type':type(exc).__name__}
            if self.status != 'RUNNING':
                return
            self.runtime.complete_effect(node, result)
        if self.runtime.status=='STOPPED' and self.runtime.outcome=='CONTINUE':
            self._follow_next(self.runtime.continuation_target)
        elif self.runtime.status=='STOPPED' and self.runtime.outcome=='SKIPPED':
            # A closed entry does not earn a completed run or a failed attempt.
            self._leave()
        elif self.runtime.status=='BLOCKED' or self.runtime.status=='STOPPED' and self.runtime.outcome!='SUCCESS':
            self._failed_attempt(self.runtime.reason)
        elif self.runtime.status=='STOPPED':
            key=self.runtime.flow_key
            if key and key.get('purpose') in {'trigger','solver'}:
                frame=None
                if key['purpose']=='solver' and self.source!='simulation':
                    try:
                        frame=self.effect({'node':{'id':self.runtime.current_node,'kind':'flow_frame','phase':'end'},
                                           'source':self.source,'recovery_event':key.get('event')})
                    except Exception:
                        frame={'valid':False}
                returned=self.watchers.complete_continuation(key,self.runtime.current_node,frame)
                if returned:self.runtime.completion=returned
            if self.schedule.success():
                self._leave()
            else:
                if item.get('type')=='memory':
                    self.schedule.memory.update(copy.deepcopy({k:v for k,v in self.runtime.exclusion_regions.items() if not v.get('target_node')}))
                    self.schedule.memory_variables[item['id']] = copy.deepcopy({k:v for k,v in self.runtime.variables.items()
                        if isinstance(v,dict) and v.get('verified') is True})
                self._enter()

    def snapshot(self):
        return {'status':self.status, 'reason':self.reason, 'chain':self.schedule.current,
                'rounds':self.schedule.rounds, 'failures':dict(self.schedule.failures),
                'consecutive_failures':dict(self.schedule.consecutive_failures),
                'total_failures':dict(self.schedule.total_failures),'total_runs':dict(self.schedule.total_runs),
                'runs':dict(self.schedule.runs), 'memory':copy.deepcopy(self.schedule.memory),
                'memory_variables':copy.deepcopy(self.schedule.memory_variables),
                'handoff_events':copy.deepcopy(self.schedule.handoff_events),
                'next_routes':copy.deepcopy(self.next_routes), 'route_active':self.route_active,
                'handoff_pending':self.handoff_pending, 'triggers':self.watchers.snapshot(),
                'failure_events':copy.deepcopy(self.schedule.events),
                'chain_elapsed_seconds':max(0,self.clock()-self.chain_started_at),
                'chain_timeout_minutes':self.graph['settings'].get('chain_timeout_minutes',25),
                'activity':self.activity_snapshot()}
