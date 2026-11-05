"""Read-only timing queries, executed by the native statistics TASK."""
import copy
import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from .android_core.round_timing_store import RoundTimingStore, full_version, version_parts
from .round_statistics import activity_catalog, main


class RoundStatisticsEditor:
    def __init__(self, root):
        self.root = Path(root)
        self.store = RoundTimingStore(self.root/'assets/rwtd/runtime/farm_round_timings.sqlite3')
        self.jobs = queue.Queue(maxsize=4)
        self.closing = threading.Event()
        self.lock = threading.RLock()
        self.state = {'results': {}, 'report': None, 'markdown': '', 'csv': ''}
        self.refresh()

    def _log(self, *args):
        pass

    def refresh(self):
        activities = activity_catalog(self.root, self.store)
        registry = self.root/'assets/rwtd/scheduler/canvases.json'
        entries = json.loads(registry.read_text(encoding='utf-8-sig')).get('entries', {}) if registry.exists() else {}
        names = [dict(canvas=item.get('name',''),stage=None,scope='farm',canvas_id=key) for key,item in entries.items()]
        from .android_core.retreatable_km import stage_from_name
        for item in names:
            item['stage'] = stage_from_name(item['canvas'])
        for scope in ('farm','activity'):
            names.extend(self.store.rows(None, scope=scope))
        versions = {}
        for item in names:
            version = full_version(item['canvas'])
            if not version:
                continue
            keys = ['farm:'+str(item['stage'])] if item['scope']=='farm' else []
            keys += ['activity:'+item['canvas_id']] if item.get('canvas_id') else []
            for key in keys:
                versions.setdefault(key,set()).add(version)
        with self.lock:
            self.state.update(activities=activities, versions={key:sorted(value,key=version_parts) for key,value in versions.items()})

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def image(self, which):
        return None

    def enqueue(self, request_id, command, payload):
        if command not in {'refresh','generate','close'} or not isinstance(payload,dict) or not isinstance(request_id,str) or not 1<=len(request_id)<=100:
            raise ValueError('Invalid statistics command')
        with self.lock:
            if request_id in self.state['results']:
                return self.state['results'][request_id]
            self.jobs.put_nowait((request_id,command,payload))
            self.state['results'][request_id]={'status':'QUEUED'}
        return {'status':'QUEUED','request_id':request_id}

    def process_next(self):
        try:
            request_id, command, payload = self.jobs.get_nowait()
        except queue.Empty:
            return
        try:
            self.command(command,payload)
            result={'status':'SUCCESS'}
        except Exception as exc:
            result={'status':'FAILED','reason':str(exc)[:500]}
        with self.lock:
            self.state['results'][request_id]=result
            while len(self.state['results'])>50:
                self.state['results'].pop(next(iter(self.state['results'])))

    def command(self, command, payload):
        if command=='close':
            self.closing.set()
            return
        if command=='refresh':
            self.refresh()
            return
        scope=payload.get('scope','farm')
        stage=payload.get('stage',1)
        count=payload.get('rounds',100)
        if scope not in {'farm','activity'} or type(stage) is not int or not 1<=stage<=5 or type(count) is not int or not 1<=count<=100000:
            raise ValueError('請選關卡／活動，筆數須為 1～100000')
        activity=payload.get('activity','')
        if scope=='activity' and activity not in {item['id'] for item in activity_catalog(self.root,self.store)}:
            raise ValueError('請選擇有效的活動 Canvas')
        version=str(payload.get('version','')).strip()
        args=['--database',str(self.store.path),'--scope',scope,'--stage',str(stage),'--rounds',str(count)]
        if scope=='activity':
            args+=['--activity-canvas',activity]
        if version:
            parts=version.replace('～','~').replace('至','~').split('~')
            if len(parts)>2:
                raise ValueError('版本格式：1.6、1.6.2 或 1.5~1.7')
            for part in parts:
                version_parts(part.strip())
            args+=['--canvas-version',parts[0].strip()] if len(parts)==1 else ['--from-version',parts[0].strip(),'--to-version',parts[1].strip()]
        output=self.root/'docs/rwtd/analysis'/('farm_rounds_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        output.mkdir(parents=True,exist_ok=False)
        main(args+['--output-dir',str(output)],quiet=True)
        with self.lock:
            self.state.update(report=json.loads((output/'report.json').read_text(encoding='utf-8')),
                markdown=(output/'report.md').read_text(encoding='utf-8'),
                csv=(output/'rounds.csv').read_text(encoding='utf-8-sig'),output=str(output))
