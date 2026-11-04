"""One independent read-only loop per Trigger; inputs stay with the owner."""
import copy
import threading
import time


class TriggerObservers:
    READ_KINDS = {'coco', 'ocr', 'ocr_decision', 'entry_guard', 'flow_frame'}

    def __init__(self, controller, observe, cleanup=None):
        self.controller, self.observe, self.cleanup = controller, observe, cleanup
        self.slots = {}
        self.closed = False

    def sync(self):
        """Called under the controller lock. Never queue a second job/thread."""
        with self.controller.lock, self.controller.trigger_scheduler.lock:
            self._sync_locked()

    def _sync_locked(self):
        scheduler = self.controller.trigger_scheduler
        enabled = (not self.closed and not self.controller.closed
                   and not getattr(self.controller, 'activity_inline_observers', False)
                   and scheduler.source != 'simulation' and scheduler.active)
        for key, slot in list(self.slots.items()):
            if not enabled or slot['scheduler'] is not scheduler or key not in scheduler.tasks:
                slot['retire'].set()
                if slot.get('cancel'):
                    slot['cancel'].set()
            if not slot['thread'].is_alive():
                self.slots.pop(key)
        if not enabled:
            return
        for key in scheduler.tasks:
            if key in self.slots:
                continue  # A retired but unfinished capture must finish first.
            slot = {'scheduler':scheduler, 'retire':threading.Event(), 'cancel':None}
            thread = threading.Thread(target=self._run, args=(key,slot),
                                      name='RwtDObserve-'+key, daemon=True)
            slot['thread'] = thread
            self.slots[key] = slot
            thread.start()

    def invalidate(self):
        """Cancel publication, never interrupt a touch or a native capture."""
        for slot in self.slots.values():
            if slot.get('cancel'):
                slot['cancel'].set()

    def close(self):
        self.closed = True
        self.invalidate()
        for slot in self.slots.values():
            slot['retire'].set()

    def _eligible(self, slot):
        c = self.controller
        return (not self.closed and not c.closed and not slot['retire'].is_set()
                and not getattr(c, 'activity_inline_observers', False)
                and c.trigger_scheduler is slot['scheduler']
                and not c.device_stop.is_set() and not c.trigger_cancel.is_set()
                and not c.capture_suppressed.is_set()
                and c.mode in {'Android Preview','Manual Device Test'})

    def unavailable_reason(self):
        """Report a latched observation shutdown; never silently clear cancellation."""
        c = self.controller
        if self.closed or c.closed:
            return 'Observer manager closed'
        if c.device_stop.is_set():
            return 'Android device stop latched'
        if c.trigger_cancel.is_set():
            return 'Trigger cancellation latched'
        if c.capture_suppressed.is_set():
            return 'Android capture suppressed'
        if c.mode not in {'Android Preview','Manual Device Test'}:
            return 'Android observation mode unavailable'
        return None

    def _run(self, key, slot):
        c, scheduler = self.controller, slot['scheduler']
        from .log_policy import timing_start,timing_mark
        log_task=getattr(getattr(c.manual_handler,'__self__',None),'task',None)
        try:
            while not slot['retire'].is_set() and not self.closed:
                effect = None
                poll_started=timing_start(log_task)
                with c.lock, scheduler.lock:
                    task = scheduler.tasks.get(key)
                    if task and self._eligible(slot):
                        before_cursor=task['cursor']
                        before_status=task['status']
                        # Notification remains pending for the owner thread.
                        queued_before={q['id'] for q in scheduler.queue}
                        if not task.get('pending') or task['pending']['kind'] in self.READ_KINDS:
                            effect = scheduler.poll(task_id=key)
                        from .log_policy import trigger_event
                        for request in scheduler.queue:
                            if request['id'] not in queued_before:
                                trigger_event(c,'TRIGGER_QUEUED','Trigger 成功排隊',
                                              trigger=key,node=request['node'],request_id=request['id'])
                        if effect and effect['node']['kind'] not in self.READ_KINDS:
                            effect = None
                        if effect:
                            cancel = threading.Event()
                            slot['cancel'] = cancel
                            task['observation_busy'] = True
                            started = time.monotonic()
                        task['observation_thread'] = threading.current_thread().name
                        if (before_cursor!=task['cursor'] or before_status!=task['status'] or effect):
                            timing_mark(log_task,'OBSERVER_TRANSITION',poll_started,
                                        trigger=key,node=before_cursor,next_node=task['cursor'],
                                        status=task['status'],round=task['rounds'])
                if effect:
                    metrics=getattr(c,'runtime_metrics',None)
                    if metrics is not None:metrics.increment('observer_polls')
                    try:
                        observe_started=timing_start(log_task)
                        timing_mark(log_task,'OBSERVE_START',trigger=key,node=effect['node']['id'],
                                    round=effect['round_id'])
                        result = self.observe(copy.deepcopy(effect), cancel)
                        timing_mark(log_task,'OBSERVE_DONE',observe_started,
                                    trigger=key,node=effect['node']['id'],round=effect['round_id'])
                    except Exception as exc:
                        from .log_policy import trigger_event
                        trigger_event(c,'OBSERVER_ERROR','觀測辨識異常',trigger=key,
                                      error=type(exc).__name__,reason=str(exc)[:160])
                        result = {'verified':False,'status':'FAILED',
                                  'error_type':type(exc).__name__,'reason':str(exc)[:200]}
                    finally:
                        from .watchdog import mark as watchdog_mark
                        watchdog_mark(log_task,'OBSERVE_END',trigger=key)
                    with c.lock, scheduler.lock:
                        task = scheduler.tasks.get(key)
                        if task:
                            task['observation_busy'] = False
                            task['observation_seconds'] = round(time.monotonic()-started,3)
                        if (self._eligible(slot) and not cancel.is_set() and task
                                and task['status']=='RUNNING'
                                and task['generation']==effect['generation']
                                and task['rounds']==effect['round_id']
                                and task['pending']==effect['node']):
                            guards=(scheduler.recovery.event or {}).get('guards',{})
                            guard_before=guards.get(key)
                            diagnostic=None
                            if guard_before=='PENDING' and effect['node'].get('_entry_guard_for'):
                                from .solver_guard_diagnostics import describe
                                target=next(n for n in task['definition']['nodes']
                                            if n['id']==effect['node']['_entry_guard_for'])
                                diagnostic=describe(target,result)
                                task['last_guard_diagnostic']=diagnostic
                                from .log_policy import trigger_event
                                trigger_event(c,'SOLVER_GUARD_DETAIL','Solver Guard 同幀條件結果',
                                              trigger=key,recovery_event=task.get('recovery_event'),
                                              diagnostic=diagnostic)
                            scheduler.complete(effect,result)
                            guard_after=(scheduler.recovery.event or {}).get('guards',{}).get(key)
                            if guard_before=='PENDING' and guard_after in {'MATCHED','REJECTED'}:
                                from .log_policy import trigger_event
                                trigger_event(c,'SOLVER_GUARD','Solver 開始 Guard 已判定',
                                              trigger=key,decision=guard_after,
                                              recovery_event=task.get('recovery_event'),
                                              guard_result=(diagnostic or {}).get('result'),
                                              rejection_reason=('CONDITION_NO' if diagnostic and diagnostic['result']=='NO'
                                                  else 'UNKNOWN_NO_RETRY' if diagnostic and diagnostic['result']=='UNKNOWN' and target.get('unknown_no_retry')
                                                  else 'UNKNOWN_RETRY_EXHAUSTED' if diagnostic and diagnostic['result']=='UNKNOWN'
                                                  else None),diagnostic=diagnostic)
                        elif task:
                            task['observation_discarded'] = task.get('observation_discarded',0)+1
                        slot['cancel'] = None
                    c.wake.set()
                slot['retire'].wait(.05)
        except Exception as exc:
            with c.lock, scheduler.lock:
                task = scheduler.tasks.get(key)
                if task and self._eligible(slot):
                    from .log_policy import trigger_event
                    trigger_event(c,'OBSERVER_ERROR','觀測工作異常',trigger=key,
                                  error=type(exc).__name__,reason=str(exc)[:160])
                    task['observation_busy'] = False
                    scheduler._fail_round(task,'觀測工作異常：'+type(exc).__name__+': '+str(exc)[:160],
                                          {'error_type':type(exc).__name__,'error_message':str(exc)[:500]})
                    # sync() replaces this exited worker; the next observation
                    # still obeys the interval or a new Solver failure event.
                    c.wake.set()
        finally:
            from .watchdog import mark as watchdog_mark
            watchdog_mark(log_task,'OBSERVER_STOP',trigger=key)
            if self.cleanup:
                self.cleanup()
