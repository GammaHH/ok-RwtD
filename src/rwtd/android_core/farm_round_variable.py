"""Farm-owned round interval gates; no OCR, wall clock or cross-run persistence."""
import threading


def is_round_guard(clause):
    return clause.get('kind')=='variable' and clause.get('source')=='shared' and clause.get('variable')=='FarmRound'


def validate(clause):
    if (set(clause)!={'kind','source','variable','op','value'} or not is_round_guard(clause)
            or clause.get('op')!='gte' or type(clause.get('value')) is not int
            or not 1<=clause['value']<=1000000):
        raise ValueError('Farm Round requires an integer interval 1..1000000 (gte)')


class FarmRounds:
    def __init__(self):
        self.lock=threading.RLock()
        self.completed=0
        self.last_success={}
        self.pending=set()

    def advance(self, completed):
        if type(completed) is not int or completed<0:
            raise ValueError('Invalid completed Farm round count')
        with self.lock:
            self.completed=max(self.completed,completed)

    def allowed(self, key, interval):
        with self.lock:
            due=self.completed-self.last_success.get(key,0)>=interval
            if due:self.pending.add(key)
            else:self.pending.discard(key)
            return due

    def complete(self, key):
        with self.lock:
            if key in self.pending:
                self.last_success[key]=self.completed
                self.pending.discard(key)
