"""Restart-safe activity deadlines and outstanding runs for the native FARM."""
import json
import math

from .module_store import atomic_json, safe_path
from .resolution_calibration import file_lock
from .user_data import definition_root


class ActivityScheduleState:
    def __init__(self, root):
        self.path = safe_path(definition_root(root), 'assets/rwtd/runtime/activity_schedule.json')
        self.records = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), dict):
            raise ValueError('活動排程狀態格式無效')
        return data['entries']

    @staticmethod
    def _identity(entry):
        identity = [entry['canvas_id'], entry['interval_seconds'], entry['initial_runs']]
        # Keep legacy identities stable when the optional countdown is absent.
        if entry.get('first_delay_seconds') is not None:
            identity.append(entry['first_delay_seconds'])
        return identity

    def restore(self, entry):
        row = self.records.get(entry['id'])
        if row is None:
            return False
        if not isinstance(row, dict):
            raise ValueError('活動排程紀錄格式無效')
        if row.get('settings') != self._identity(entry):
            return False
        due = row.get('next_due')
        initial, deferred, manual = (row.get(key) for key in
                                     ('initial_runs_left', 'deferred_runs', 'manual_runs'))
        if (type(due) not in (int, float) or not math.isfinite(due)
                or any(type(value) is not int or value < 0 for value in (initial, deferred, manual))
                or initial > entry['initial_runs'] or manual > 100
                or (deferred and initial > deferred)):
            raise ValueError('活動排程計時或待辦紀錄無效')
        entry.update(next_due=due, initial_runs_left=initial, initial_completed=initial == 0,
                     deferred_runs=deferred, manual_runs=manual)
        return True

    def save(self, entry, *, starting=False):
        initial = entry['initial_runs_left']
        deferred = entry.get('deferred_runs', 0)
        if starting:
            # Entering consumes the current run. Retain only unstarted runs so
            # a restart never automatically repeats a possibly sent operation.
            initial = max(0, initial - 1)
            deferred = entry['batch_runs'] - entry['repetition']
        key = entry['id']
        row = dict(settings=self._identity(entry), next_due=entry['next_due'],
                   initial_runs_left=initial, deferred_runs=deferred,
                   manual_runs=entry.get('manual_runs', 0))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix('.lock')):
            records = self._load()
            if records.get(key) != self.records.get(key):
                raise ValueError('活動排程已由另一個 FARM 更新，請重新啟動')
            records[key] = row
            atomic_json(self.path, dict(version=1, entries=records))
        self.records[key] = row
