"""Task-scoped output policy; never changes capture, dispatch or queues."""
import logging
import threading
import time
import json
from functools import wraps
from datetime import datetime, timezone


def timing_start(task):
    """No clock reads outside explicitly enabled diagnostic modes."""
    config=getattr(task,'config',{})
    return time.monotonic() if (config.get('Detailed Logging',False)
                               or config.get('Timing Logging',False)) else None


def timing_mark(task,phase,started=None,**details):
    """Diagnostic only: never affect dispatch if a logger fails."""
    from .watchdog import mark
    mark(task,phase,**details)
    config=getattr(task,'config',{})
    detailed=config.get('Detailed Logging',False)
    if not (detailed or config.get('Timing Logging',False)):
        return
    try:
        now=time.monotonic()
        payload=dict(details,phase=phase,utc=datetime.now(timezone.utc).isoformat(),
                     monotonic_seconds=round(now,6),thread=threading.current_thread().name)
        if started is not None:
            payload['elapsed_seconds']=round(now-started,6)
        # Keep the shared OK logger at INFO for timing-only sessions. Raising
        # its level to DEBUG would also enable shell scripts and every poll.
        logger=task.log_debug if detailed else task.log_info
        logger('[RWTD_FLOW_TIMING] '+json.dumps(payload,ensure_ascii=False,sort_keys=True))
    except Exception:
        pass


class RuntimeMetrics:
    def __init__(self):
        self.lock=threading.RLock()
        self.started=time.monotonic()
        self.counts={'adb_shell_calls':0,'screencap_calls':0,'observer_polls':0}

    def increment(self,key):
        with self.lock:self.counts[key]+=1

    def snapshot(self):
        with self.lock:return dict(self.counts,elapsed_seconds=round(time.monotonic()-self.started,3))


class LogSession:
    def __init__(self,detailed=False,device_manager=None):
        self.detailed=detailed
        self.logger=logging.getLogger('ok')
        self.manager=device_manager
        self.metrics=RuntimeMetrics()
        self.last_report=0

    def __enter__(self):
        self.previous=self.logger.level
        self.logger.setLevel(logging.DEBUG if self.detailed else logging.INFO)
        self.wrapped=None
        if self.manager is not None and callable(getattr(self.manager,'shell_device',None)):
            self.original=self.manager.shell_device
            self.instance_override=self.manager.__dict__.get('shell_device')
            @wraps(self.original)
            def counted(device,*args,**kwargs):
                if device is not None:
                    self.metrics.increment('adb_shell_calls')
                    if args and isinstance(args[0],str) and 'screencap' in args[0]:
                        self.metrics.increment('screencap_calls')
                return self.original(device,*args,**kwargs)
            self.wrapped=counted
            self.manager.shell_device=counted
        self.logger.info('RWTD_LOG_POLICY level=%s handlers=%s propagate=%s root_handlers=%s',
                         'DEBUG' if self.detailed else 'INFO',
                         [type(h).__name__ for h in self.logger.handlers],self.logger.propagate,
                         [type(h).__name__ for h in logging.getLogger().handlers])
        return self

    def __exit__(self,*args):
        try:
            self.report(force=True)
        finally:
            if self.wrapped and self.manager.shell_device is self.wrapped:
                if self.instance_override is None:del self.manager.shell_device
                else:self.manager.shell_device=self.instance_override
            self.logger.setLevel(self.previous)

    def report(self,force=False):
        now=time.monotonic()
        if force or now-self.last_report>=60:
            self.last_report=now
            try:self.logger.info('RWTD_ACTIVITY %s',json.dumps(self.metrics.snapshot(),sort_keys=True))
            except Exception:pass  # Telemetry never changes task execution.


def recognition(task,controller,event,message,key,state,**details):
    if not hasattr(task,'_recognition_log_lock'):
        task._recognition_log_lock=threading.RLock()
        task._recognition_log_states={}
    with task._recognition_log_lock:
        changed=task._recognition_log_states.get(key,object())!=state
        task._recognition_log_states[key]=state
        while len(task._recognition_log_states)>256:
            task._recognition_log_states.pop(next(iter(task._recognition_log_states)))
    level='INFO' if changed else 'DEBUG'
    logger=getattr(task,'log_info' if changed else 'log_debug',None)
    if callable(logger):
        try:logger(message)
        except Exception:pass
    sink=getattr(controller,'_log',None)
    if callable(sink):
        try:sink(event,message,level=level,**details)
        except Exception:pass


def trigger_event(controller,event,message,**details):
    try:controller._log(event,message,level='INFO',**details)
    except Exception:pass
    service=getattr(getattr(controller,'manual_handler',None),'__self__',None)
    task=getattr(service,'task',None)
    logger=getattr(task,'log_info',None)
    if callable(logger):
        try:logger(event+' '+message+' '+str(details))
        except Exception:pass
