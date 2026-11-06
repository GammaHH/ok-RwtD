"""Cooperative cancellation of one node; never cancels a finger-down gesture."""
import time


class NodeStop:
    def __init__(self, stop, controller, request=None):
        self.stop, self.controller, self.request = stop, controller, request
        self.preempted = False

    def is_set(self):
        if self.stop.is_set() or self.controller.device_stop.is_set():
            return True
        if not self.preempted:
            remote=getattr(self.controller,'remote_preemption_pending',None)
            if callable(remote) and not (self.request or {}).get('preempt_operations'):
                self.preempted=bool(remote())
            scheduler = getattr(self.controller,'trigger_scheduler',None)
            # An interrupting Trigger completes its node before another peer
            # can interrupt it. Ordinary queued Trigger operations may yield.
            if not self.preempted and scheduler is not None and not (self.request or {}).get('preempt_operations'):
                self.preempted = scheduler.preemption_pending()
        return self.preempted

    def set(self):
        # An uncertain input must still stop the owning execution globally;
        # preemption must never hide a transport/capture failure.
        self.stop.set()

    def wait(self, seconds=None):
        scheduler=getattr(self.controller,'trigger_scheduler',None)
        tasks=getattr(scheduler,'tasks',None)
        enabled=(tasks is None or any(t['definition'].get('preempt_operations')
                                     and t['definition'].get('trigger_interval',3)>0
                                     for t in tasks.values()))
        remote=callable(getattr(self.controller,'remote_preemption_pending',None))
        if ((scheduler is None or not enabled) and not remote) or (self.request or {}).get('preempt_operations'):
            return self.stop.wait(seconds)
        deadline = None if seconds is None else time.monotonic()+seconds
        while not self.is_set():
            remaining = None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.stop.wait(.05 if remaining is None else min(.05, remaining))
        return True
