"""Node-local daily and completion cooldown gates; wall time survives Farm rounds."""
import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime
from .module_store import atomic_json, safe_path

_state_lock=threading.RLock()


def validate(clause):
    if clause.get('mode') == 'daily':
        if set(clause) != {'kind','mode','at'} or not isinstance(clause.get('at'),str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clause['at']):
            raise ValueError('Daily Guard requires HH:MM (local computer time)')
    elif clause.get('mode') == 'cooldown':
        seconds=clause.get('seconds')
        if set(clause) != {'kind','mode','seconds'} or type(seconds) not in (int,float) or not 0 < seconds <= 31536000:
            raise ValueError('Cooldown Guard requires 0 < seconds <= 31536000')
    elif clause.get('mode') == 'ocr_cooldown':
        if set(clause)!={'kind','mode','variable'} or clause['variable']!='ExpeditionTime':
            raise ValueError('OCR cooldown requires Expedition Time')
    else:
        raise ValueError('Unknown time Guard mode')


class TimeGuards:
    def __init__(self,root=None,clock=time.time,namespace='default'):
        from .user_data import definition_root
        self.root,self.clock=definition_root(root) if root else None,clock
        from .module_store import identifier
        self.namespace=identifier(namespace)
        self.lock=threading.RLock()
        self.records={}
        self.pending={}
        self.farm_rounds=None
        self._read()

    def _read(self):
        """Merge persisted completions so independent Canvas instances retain each other."""
        if self.root:
            path=safe_path(self.root,'assets/rwtd/scheduler/time_guards.json')
            if path.exists():
                data=json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(data,dict) or data.get('schema_version')!=1 or not isinstance(data.get('completed'),dict):
                    raise ValueError('Invalid time Guard state')
                if len(data['completed'])>10000 or any(not isinstance(k,str) or len(k)>240 or type(v) not in (int,float) or not math.isfinite(v) or v<0 for k,v in data['completed'].items()):
                    raise ValueError('Invalid time Guard completion timestamps')
                for key,value in data['completed'].items():
                    # Older state had no Canvas namespace; keep it attached to default.
                    if key.count(':')==1:key='default:'+key
                    self.records[key]=max(value,self.records.get(key,0))

    def key(self,node_id,clause):
        return self.namespace+':'+node_id+':'+hashlib.sha256(json.dumps(clause,sort_keys=True).encode()).hexdigest()[:20]

    def allowed(self,node_id,clause):
        from .farm_round_variable import is_round_guard,validate as validate_round
        if is_round_guard(clause):
            validate_round(clause)
            return (self.farm_rounds.allowed(self.key(node_id,clause),clause['value'])
                    if self.farm_rounds is not None else None)
        validate(clause)
        with self.lock:
            now=self.clock();last=self.records.get(self.key(node_id,clause))
            if clause['mode']=='ocr_cooldown':
                return now>=last if last is not None else None
            if clause['mode']=='cooldown':
                return last is None or now-last>=clause['seconds']
            local=datetime.fromtimestamp(now).astimezone()
            return local.strftime('%H:%M')>=clause['at'] and (last is None or
                datetime.fromtimestamp(last).astimezone().date()!=local.date())

    def prepare(self,node_id,clause,sample):
        """Freeze a fresh OCR duration; reading alone never starts cooldown."""
        from .shared_ocr import numeric
        validate(clause)
        key=self.key(node_id,clause)
        with self.lock:
            self.pending.pop(key,None)
            if (clause['mode']=='ocr_cooldown' and sample.get('verified') is True
                    and sample.get('expedition_completed') is True):
                # No duration exists in the completed UI; do not invent zero or
                # reuse an earlier pending countdown. Existing cooldown still gates.
                return self.allowed(node_id,clause) is not False
            if sample.get('verified') is not True or not numeric(sample.get('value')):return None
            if self.allowed(node_id,clause) is False:return False
            self.pending[key]=sample['value']
            if len(self.pending)>10000:self.pending.pop(next(iter(self.pending)))
            return True

    def complete(self,node):
        guard=node.get('entry_guard',{})
        conditions=guard.get('conditions',[guard])
        from .farm_round_variable import is_round_guard
        if self.farm_rounds is not None:
            for clause in conditions:
                if is_round_guard(clause):
                    self.farm_rounds.complete(self.key(node['id'],clause))
        clauses=[c for c in conditions if c.get('kind')=='time']
        if not clauses:return
        with self.lock, _state_lock:
            self._read()
            for clause in clauses:
                key=self.key(node['id'],clause)
                if clause['mode']=='ocr_cooldown':
                    duration=self.pending.pop(key,None)
                    if duration is not None:self.records[key]=self.clock()+duration
                else:self.records[key]=self.clock()
            self.records=dict(sorted(self.records.items(),key=lambda pair:pair[1],reverse=True)[:10000])
            if self.root:atomic_json(safe_path(self.root,'assets/rwtd/scheduler/time_guards.json'),{'schema_version':1,'completed':self.records})

    def blocked(self,node):
        guard=node.get('entry_guard',{});conditions=guard.get('conditions',[guard])
        from .farm_round_variable import is_round_guard
        values=[self.allowed(node['id'],c) for c in conditions if c.get('kind')=='time' or is_round_guard(c)]
        return bool(values) and (any(v is False for v in values) if guard.get('operator','and')=='and'
            else len(values)==len(conditions) and all(v is False for v in values))
