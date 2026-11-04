"""Bounded passive Farm telemetry. Never captures, recognizes or dispatches input."""
import copy
import threading
import time
import uuid
import weakref
from contextlib import contextmanager


class WatchRegistry:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = weakref.WeakKeyDictionary()

    @contextmanager
    def session(self, task):
        now = self.clock()
        with self.lock:
            self.sessions[task] = dict(session=uuid.uuid4().hex, heartbeat=now,
                progress=now, thread_id=threading.get_ident(), state={}, busy={})
        try:
            yield
        finally:
            with self.lock:
                self.sessions.pop(task, None)

    def mark(self, task, phase, **details):
        with self.lock:
            state = self.sessions.get(task)
            if state is None:
                return
            now = self.clock()
            observer = phase.startswith('OBSERVE')
            if not observer:
                state['heartbeat'] = now
            key = ('observer:' + str(details.get('trigger')) if observer else
                   'macro:' + str(details.get('segment')))
            if phase in {'OBSERVE_START','RWTD_MACRO_SEND'}:
                theoretical = details.get('theoretical_seconds', 0)
                state['busy'][key] = dict(start=now, phase=phase,
                    thread_id=threading.get_ident(), node=details.get('node'),
                    theoretical_seconds=theoretical if type(theoretical) in (int,float) else 0)
                while len(state['busy']) > 128:
                    state['busy'].pop(next(iter(state['busy'])))
            elif phase in {'OBSERVE_DONE','OBSERVE_END','OBSERVER_STOP',
                           'RWTD_MACRO_DONE','RWTD_MACRO_STOP','RWTD_MACRO_UNKNOWN'}:
                state['busy'].pop(key, None)
            if phase in {'RWTD_MACRO_DONE','NODE_EFFECT_RETURN','TASK_READY'}:
                state['progress'] = now
            if phase == 'WATCHDOG_STATE':
                identity = tuple(details.get(k) for k in ('canvas','chain','node','round','visits'))
                if identity != state.get('identity'):
                    state['identity'],state['progress'] = identity,now
                    state['wait_until']=now
                state['wait_until']=max(state.get('wait_until',now),
                                        now+max(0,details.get('wait_seconds',0)))
                state['state'] = dict(details)

    def snapshots(self):
        with self.lock:
            return [(task,copy.deepcopy(state)) for task,state in self.sessions.items()]

    def is_current(self,task,token):
        with self.lock:
            return self.sessions.get(task,{}).get('session')==token

    def rebase(self, task):
        """Intentional executor pause does not consume watchdog budgets."""
        with self.lock:
            state=self.sessions.get(task)
            if state:
                state['heartbeat']=state['progress']=self.clock()
                for busy in state['busy'].values():
                    busy['start']=self.clock()


class WatchPolicy:
    def __init__(self):
        self.reported=set()

    def evaluate(self, sample, now, heartbeat=120, progress=300, observer=60):
        alerts=[]
        busy=list(sample['busy'].items())
        macro_budget=max([heartbeat]+[max(0,b['theoretical_seconds'])*4+heartbeat
                         for key,b in busy if key.startswith('macro:')])
        if now-sample['heartbeat'] > macro_budget:
            alerts.append(('EXECUTOR_STALLED','executor',now-sample['heartbeat']))
        if (not any(key.startswith('macro:') for key,_ in busy)
                and now-max(sample['progress'],sample.get('wait_until',0)) > progress):
            alerts.append(('FLOW_STALLED','flow',now-sample['progress']))
        for key,work in busy:
            if key.startswith('observer:') and now-work['start'] > observer:
                alerts.append(('OBSERVER_STALLED',key,now-work['start']))
        current={(sample['session'],kind,key) for kind,key,_ in alerts}
        # A recovered incident can be reported if it occurs again later.
        self.reported={key for key in self.reported
                       if key[0]!=sample['session'] or key in current}
        fresh=[]
        for kind,key,elapsed in alerts:
            identity=(sample['session'],kind,key)
            if identity not in self.reported:
                self.reported.add(identity)
                fresh.append(dict(kind=kind,key=key,elapsed_seconds=round(elapsed,3)))
        while len(self.reported)>256:
            self.reported.pop()
        return fresh


registry = WatchRegistry()


def mark(task, phase, **details):
    try:
        registry.mark(task, phase, **details)
    except Exception:
        pass  # Monitoring must never change operation ordering or delivery.
