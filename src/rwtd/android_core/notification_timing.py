"""Explicit notification-node timing; no device, network or storage access."""
from datetime import datetime, timezone
from threading import RLock

ROLES = {'none', 'start', 'end', 'failure'}
SUCCESS_STATUSES = {'local_only', 'native_submitted', 'delivered'}


class NotificationTiming:
    def __init__(self):
        self.lock = RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.started = None
            self.failed = False

    def failure(self):
        with self.lock:
            if self.started is not None:
                self.failed = True

    def observe(self, node, result, now, utc=None):
        if node.get('kind') != 'notify' or result.get('status') not in SUCCESS_STATUSES:
            return None
        role = node.get('round_timing', 'none')
        stamp = utc or datetime.now(timezone.utc).isoformat()
        with self.lock:
            if role == 'start':
                if self.started is None:
                    self.started = (now, stamp, node['id'])
                    self.failed = False
                else:
                    # Repeated start cannot shorten or erase a failed interval.
                    self.failed = True
            elif role == 'failure':
                self.failure()
            elif role == 'end' and self.started is not None:
                started, start_utc, start_node = self.started
                valid = not self.failed
                self.reset()
                if valid:
                    return dict(seconds=round(max(0, now-started), 6),
                                start_utc=start_utc, end_utc=stamp, outcome='SUCCESS',
                                details={'timing_basis':'notification_nodes',
                                         'start_node':start_node, 'end_node':node['id']})
        return None
