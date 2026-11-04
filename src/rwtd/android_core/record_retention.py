"""Bound generated records only. No startup scan, recursive deletion or links."""
import json
import re
import shutil
import threading
import time
from pathlib import Path

from .module_store import safe_path, linked

DEFAULTS={
    'backups': {'max_records':100,'max_bytes':64*1024**2,'max_days':90},
    'notifications': {'max_records':2000,'max_bytes':64*1024**2,'max_days':30},
    'exceptions': {'max_records':200,'max_bytes':512*1024**2,'max_days':30},
    'sessions': {'max_records':1000,'max_bytes':2*1024**3,'max_days':30},
    'history': {'max_records':2000,'max_bytes':128*1024**2,'max_days':90},
    'cleanup_audit': {'max_records':100,'max_bytes':32*1024**2,'max_days':30},
    'logs': {'max_records':7,'max_bytes':224*1024**2,'max_days':1},
    'dedupe': {'max_records':2000,'max_bytes':1024**2,'max_days':90},
}
# Configuration may reduce these ceilings, but cannot enlarge managed pools.
HARD_LIMITS={category:dict(rule) for category,rule in DEFAULTS.items()}
MINIMUM_FREE_BYTES=2*1024**3
_lock=threading.RLock()
_last={}
STAMP=r'\d{8}T\d{6}_\d{6}Z_[0-9a-f]{8}'


def limits(root,category):
    path=safe_path(root,'assets/rwtd/scheduler/retention.json')
    rules=DEFAULTS
    if path.exists():
        config=json.loads(path.read_text(encoding='utf-8-sig'))
        if set(config)!={'schema_version','limits'} or config['schema_version']!=1 or set(config['limits'])!=set(DEFAULTS):
            raise ValueError('Invalid retention configuration')
        rules=config['limits']
    rule=rules[category]
    if set(rule)!={'max_records','max_bytes','max_days'} or any(type(rule[k]) is not int or rule[k]<1 for k in rule):
        raise ValueError('Retention limits must be positive integers')
    return {key:min(value,HARD_LIMITS[category][key]) for key,value in rule.items()}


def disk_space_reason(root,minimum_free_bytes=MINIMUM_FREE_BYTES):
    """Read only: return a stop reason below the reserve, otherwise None.

    This checks the volume containing root; it never scans or deletes records.
    A caller may increase the reserve, but cannot reduce it below 2 GiB.
    """
    if type(minimum_free_bytes) is not int or minimum_free_bytes<1:
        return '磁碟保留空間設定無效；停止 Farm，未清理任何資料'
    reserve=max(MINIMUM_FREE_BYTES,minimum_free_bytes)
    try:
        free=shutil.disk_usage(Path(root).absolute()).free
    except OSError:
        return '無法確認磁碟剩餘空間；停止 Farm，未清理任何資料'
    if free<reserve:
        return ('磁碟剩餘空間不足：'+str(free)+' bytes；需保留至少 '+str(reserve)+
                ' bytes。停止 Farm，未清理任何資料')
    return None


def _allowed(category,relative):
    if category=='backups':
        return relative in {'assets/rwtd/scheduler/backups','assets/rwtd/modules/user/backups','assets/rwtd/modules/system/backups'}
    return relative=={'notifications':'docs/rwtd/notifications','exceptions':'docs/rwtd/exceptions',
        'sessions':'docs/rwtd/analysis/module_tests','history':'docs/rwtd/analysis/module_tests/history',
        'cleanup_audit':'docs/rwtd/analysis/module_tests/cleanup_audit','logs':'logs'}.get(category)


def _candidate(path,category,protected,protected_runs):
    if path.name in protected or linked(path):
        return None
    paths=[path]
    if category in {'sessions','exceptions'}:
        prefix='session_' if category=='sessions' else 'exception_'
        if not re.fullmatch(prefix+STAMP,path.name) or not path.is_dir():
            return None
        paths=list(path.iterdir())
        allowed={'report.json','execution.json','before.png','after.png'} if category=='sessions' else {'report.json','analyzed_frame.png'}
        if not paths or 'report.json' not in {p.name for p in paths} or any(p.name not in allowed or linked(p) or not p.is_file() for p in paths):
            return None
        record=json.loads((path/'report.json').read_text(encoding='utf-8-sig'))
        if category=='sessions':
            if (record.get('managed_by')!='android_workflow_evidence_v1' or record.get('storage_schema')!=1
                    or record.get('profile_id')!='rwtd' or record.get('completed') is not True
                    or path.name!='session_'+record.get('report_id','') or record.get('run_id') in protected_runs):
                return None
        elif record.get('retention_complete') is not True:
            return None
    else:
        if category=='logs':
            if not path.is_file() or not re.fullmatch(
                    r'(?:ok-script\.log\.(?:\d+|\d{4}-\d{2}-\d{2}(?:_\d+)?)|ok-script\.\d{4}-\d{2}-\d{2}\.log)',
                    path.name):
                return None
            return {'path':path,'paths':[path],'signature':((path.name,path.stat().st_size,path.stat().st_mtime_ns),),
                    'bytes':path.stat().st_size,'mtime':path.stat().st_mtime}
        if not path.is_file() or path.suffix!='.json':
            return None
        record=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(record,dict):
            return None
        if category=='notifications':
            if not re.fullmatch('notification_'+STAMP+r'\.json',path.name) or record.get('status') in {None,'pending'}:
                return None
        elif category=='history':
            if (not re.fullmatch(STAMP+r'\.json',path.name) or record.get('managed_by')!='android_workflow_evidence_v1'
                    or record.get('completed') is not True or record.get('run_id') in protected_runs):
                return None
        elif category=='cleanup_audit':
            if record.get('phase')!='COMPLETED' or not record.get('plan_id'):
                return None
        elif category=='backups':
            if not re.fullmatch(r'(?:decision_)?[0-9a-f]{32}\.json|(?:deleted_)?[A-Za-z][A-Za-z0-9_-]{0,79}_[0-9a-f]{32}\.json',path.name):
                return None
            if record.get('schema_version') not in {1,2}:
                return None
    signature=tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(paths))
    return {'path':path,'paths':paths,'signature':signature,'bytes':sum(p.stat().st_size for p in paths),
            'mtime':max(p.stat().st_mtime for p in paths)}


def enforce(root,category,relative,*,protected=(),protected_runs=(),force=False,now=None):
    """After-write maintenance; best effort and throttled to once/minute/category.

    Limits cover recognized completed records. Active/foreign/invalid records
    are deliberately excluded. Capacity can exceed limits during active writes.
    """
    try:
        if not _allowed(category,relative):
            raise ValueError('Unmanaged retention directory')
        folder=safe_path(root,relative)
        clock=time.time() if now is None else now
        key=(str(Path(root).absolute()),category,relative)
        with _lock:
            if not force and 0 <= clock-_last.get(key,0)<60:
                return {'removed':0,'throttled':True}
            rule=limits(root,category)
            if not folder.exists():
                return {'removed':0}
            _last[key]=clock
            candidates=[]
            for path in folder.iterdir():
                try:
                    item=_candidate(path,category,set(protected),set(protected_runs))
                    if item: candidates.append(item)
                except (OSError,ValueError,TypeError,AttributeError):
                    continue
            candidates.sort(key=lambda i:i['mtime'])
            count=len(candidates);size=sum(i['bytes'] for i in candidates);removed=0
            for item in candidates:
                if count<=rule['max_records'] and size<=rule['max_bytes'] and clock-item['mtime']<=rule['max_days']*86400:
                    continue
                path=item['path']
                # Revalidate containment, completion and file fingerprints before
                # each removal; never walk nested folders or shell out to delete.
                safe_path(root,str(path.relative_to(Path(root).absolute())))
                fresh=_candidate(path,category,set(protected),set(protected_runs))
                if not fresh or fresh['signature']!=item['signature']:
                    continue
                for file in fresh['paths']:
                    safe_path(root,str(file.relative_to(Path(root).absolute()))).unlink()
                if path.is_dir(): path.rmdir()
                count-=1;size-=item['bytes'];removed+=1
            return {'removed':removed,'retained':count,'bytes':size}
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:
        # Retention failures cannot turn a delivered input into a retry.
        return {'removed':0,'error':type(exc).__name__}


def bounded_stamps(root, stamps, now):
    rule=limits(root,'dedupe')
    entries=sorted(((key,value) for key,value in stamps.items() if now-value<=rule['max_days']*86400),key=lambda item:item[1],reverse=True)
    result={};size=2
    for key,value in entries[:rule['max_records']]:
        cost=len(json.dumps({key:value}).encode())+1
        if size+cost>rule['max_bytes']:break
        result[key]=value;size+=cost
    return result


class BoundedCache(dict):
    """Recent report cache; persisted evidence remains independently reloadable."""
    def __init__(self, values=(), maximum=2000):
        super().__init__();self.maximum=maximum;self.update(values)

    def __setitem__(self,key,value):
        if key in self:super().__delitem__(key)
        super().__setitem__(key,value)
        while len(self)>self.maximum:super().__delitem__(next(iter(self)))

    def update(self,values=(),**kwargs):
        for key,value in dict(values,**kwargs).items():self[key]=value


def install_log_limits(root):
    """Adapt the existing async OK sink at task startup; no framework file edits."""
    import logging.handlers
    try:
        import ok.util.logger as logger
        old=getattr(logger,'_file_handler',None)
        listener=getattr(logger,'_file_listener',None)
        if old is None or listener is None or getattr(old,'_rwtd_bounded',False):
            return
        if Path(old.baseFilename).absolute()!=safe_path(root,'logs/ok-script.log'):
            return
        rule=limits(root,'logs')
        class BoundedLog(logging.handlers.RotatingFileHandler):
            _rwtd_bounded=True
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                try:
                    stat=Path(self.baseFilename).stat()
                    self._opened_at=min(time.time(),stat.st_ctime,stat.st_mtime)
                except OSError:
                    self._opened_at=time.time()

            def shouldRollover(self,record):
                return time.time()-self._opened_at>=86400 or super().shouldRollover(record)

            def doRollover(self):
                super().doRollover()
                self._opened_at=time.time()
                enforce(root,'logs','logs',force=True)
        new=BoundedLog(old.baseFilename,maxBytes=max(1,rule['max_bytes']//rule['max_records']),
            backupCount=rule['max_records'],encoding='utf-8',delay=True)
        new.setFormatter(old.formatter);new.setLevel(old.level)
        for filter in old.filters:new.addFilter(filter)
        listener.stop()  # Drain once before swapping; input execution has not started.
        listener.handlers=tuple(new if handler is old else handler for handler in listener.handlers)
        logger._file_handler=new
        old.close()
        listener.start()
    except (ImportError,OSError,ValueError,AttributeError,TypeError,KeyError):
        return
