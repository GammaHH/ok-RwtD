"""Bounded passive Farm telemetry and read-only Android process checks."""
import copy
import re
import threading
import time
import uuid
import weakref
from contextlib import contextmanager


GAME_PACKAGE = 'net.room6.horizon'


def selected_android(manager):
    """Return the selected Android address and its existing ADB connection."""
    preferred = manager.get_preferred_device() or {}
    if preferred.get('device') not in {'adb', 'emulator'}:
        return None, None
    address = preferred.get('address')
    # DeviceManager.device may connect and shell_device may disconnect on error.
    # Only use the already selected connection and its read-only shell transport.
    device = getattr(manager, '_device', None)
    return address, device if address and getattr(device, 'serial', None) == address else None


def game_process_status(manager):
    """Return (device address, alive, foreground package). Unknown fields are None."""
    address, device = selected_android(manager)
    if device is None:
        return address, None, None
    command = (f'pidof {GAME_PACKAGE} >/dev/null; echo RWTD_PID_STATUS=$?; '
               "dumpsys activity activities | grep -E '^[[:space:]]*(topResumedActivity|mResumedActivity|ResumedActivity)[[:space:]:=]'")
    try:
        output = device.shell(command, timeout=3)
    except Exception:
        return address, None, None
    if selected_android(manager) != (address, device) or not isinstance(output, str):
        return address, None, None
    markers = re.findall(r'^RWTD_PID_STATUS=(\d+)\s*$', output, re.M)
    alive = {'0': True, '1': False}.get(markers[0]) if len(markers) == 1 else None
    foreground = None
    for field in ('topResumedActivity', 'ResumedActivity', 'mResumedActivity'):
        packages = set(re.findall(
            rf'^\s*{field}\s*[:=]\s*ActivityRecord\{{[^\r\n]*?\bu\d+\s+([\w.]+)/',
            output, re.M))
        if packages:
            if len(packages) == 1:
                foreground = next(iter(packages))
            break
    if alive is False and foreground == GAME_PACKAGE:
        alive = None  # The process may have started between the two reads.
    return address, alive, foreground


def restore_game_foreground(manager, address, cancelled, wait, clock=time.monotonic):
    """Only called by the FARM owner, after outstanding operations return."""
    if cancelled() or not address:
        return False
    selected, device = selected_android(manager)
    if selected != address or device is None:
        return False
    current, alive, foreground = game_process_status(manager)
    if current != address or alive is None or (alive and foreground is None) or cancelled():
        return False
    if alive and foreground == GAME_PACKAGE:
        return True
    if selected_android(manager) != (address, device):
        return False
    device.shell(f'monkey -p {GAME_PACKAGE} -c android.intent.category.LAUNCHER 1', timeout=5)
    deadline = clock() + 30
    while not cancelled() and clock() < deadline and selected_android(manager) == (address, device):
        current, alive, foreground = game_process_status(manager)
        if current == address and alive is True and foreground == GAME_PACKAGE:
            return not cancelled()
        wait(1)
    return False


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

    def request_game_recovery(self, task, token, address):
        with self.lock:
            state = self.sessions.get(task)
            if state and state['session'] == token and not state.get('game_recovering'):
                state.setdefault('game_recovery', address)

    def take_game_recovery(self, task):
        with self.lock:
            state = self.sessions.get(task)
            if state and 'game_recovery' in state:
                state['game_recovering'] = True
                return state.pop('game_recovery')

    def has_game_recovery(self, task):
        with self.lock:
            return bool(self.sessions.get(task, {}).get('game_recovery'))

    def finish_game_recovery(self, task):
        with self.lock:
            state = self.sessions.get(task)
            if state:
                state.pop('game_recovering', None)

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
        self.process_incidents={}

    def check_game_process(self, sample, address, alive, foreground, now):
        session = sample['session']
        previous_address, previous_kind, since, reported = self.process_incidents.get(
            session, (None, None, None, False))
        if address != previous_address or (alive is True and foreground == GAME_PACKAGE):
            previous_kind, since, reported = None, None, False
        if alive is False:
            kind = 'GAME_PROCESS_MISSING'
        elif alive is True and foreground:
            kind = 'GAME_NOT_FOREGROUND' if foreground != GAME_PACKAGE else None
        else:
            kind = None
        if kind is None:
            self.process_incidents[session] = (address, previous_kind, None, reported)
            return None
        if since is None or kind != previous_kind:
            self.process_incidents[session] = (address, kind, now, reported)
            return None
        if reported:
            return None
        self.process_incidents[session] = (address, kind, since, True)
        return dict(kind=kind, key=GAME_PACKAGE, foreground_package=foreground,
                    elapsed_seconds=round(now-since, 3))

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
