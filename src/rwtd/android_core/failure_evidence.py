"""Threshold-only evidence from already analyzed native frames; never captures."""
import copy
import threading
from collections import OrderedDict
from src.rwtd.exception_monitor import ExceptionMonitor


def canvas_failure_context(*, owner=None, runtime=None, scheduler=None, store=None,
                           canvas_name=None):
    """Resolve identity from the executing owner, never its suspended parent."""
    graph = (getattr(owner, 'graph', None) or getattr(runtime, 'graph', None)
             or getattr(scheduler, 'graph', None) or {})
    context = getattr(owner, 'round_context', None) or {}
    identity = {'canvas_id': context.get('canvas_id'),
                'canvas_name': context.get('canvas'),
                'canvas_revision': graph.get('revision'),
                'chain': getattr(getattr(owner, 'schedule', None), 'current', None)}
    # Dashboard tests have no Farm owner; resolve the selected saved Canvas.
    if owner is None and store is not None:
        identity['canvas_id'] = store.active_id()
        row = next((row for row in store.catalog()
                    if row['id'] == identity['canvas_id']), {})
        identity['canvas_name'] = row.get('name') or canvas_name
    return graph, identity


def apply_node_notification_policy(event, graph):
    """Mute only this node's failure delivery; retain local evidence and counters."""
    if event.get('kind') == 'chain' and event.get('failure_kind') == 'chain_timeout':
        return event
    node_id = FailureEvidence.node_key(event)
    node = next((n for n in (graph or {}).get('nodes', [])
                 if n.get('id') == node_id), {})
    if node.get('suppress_failure_notification') is True:
        event['notify_discord'] = False
        event['notification_suppressed_reason'] = 'node_failure_notification_disabled'
    return event


def notification_allowed(event):
    if event.get('task_id') is not None or 'trigger_interval' in event:
        interval=event.get('trigger_interval')
        return type(interval) in (int,float) and interval==0
    return True


def screenshot_allowed(event):
    return event['kind'] not in {'blocked','exclusion'} and notification_allowed(event)


class FailureEvidence:
    def __init__(self, root, *, notifier=None, frame_limit=3, event_limit=256):
        self.monitor=ExceptionMonitor(root,notifier=notifier)
        self.frame_limit=frame_limit
        self.event_limit=event_limit
        self.frames=OrderedDict()
        self.events=OrderedDict()
        self.lock=threading.RLock()

    @staticmethod
    def node_key(node):
        return str(node.get('id') or node.get('node') or 'unknown') if isinstance(node,dict) else str(node)

    def remember(self,node,frame,*,device=None):
        """Copy the actual recognition frame in memory without image/file output."""
        key=self.node_key(node)
        with self.lock:
            self.monitor.accept_frame(key,frame,device=device)
            self.monitor.states[key].update(recognition_source_id=id(frame),recognition_regions=[])
            self.frames.pop(key,None)
            self.frames[key]=True
            while len(self.frames)>self.frame_limit:
                old,_=self.frames.popitem(last=False)
                self.monitor.states.pop(old,None)

    def annotate(self,node,frame,items):
        """Only attach metadata to the exact pixels most recently remembered."""
        key=self.node_key(node)
        with self.lock:
            state=self.monitor.states.get(key,{})
            if state.get('recognition_source_id')!=id(frame):return False
            state['recognition_regions']=copy.deepcopy(items[:80])
            return True

    def record_ocr(self,frame,record):
        """Keep only same-frame OCR evidence in the existing bounded node cache."""
        with self.lock:
            for key,state in self.monitor.states.items():
                if state.get('recognition_source_id')!=id(frame):continue
                item=copy.deepcopy(record)
                if item.get('node') is None:item['node']=key
                values=state.setdefault('ocr_records',[]);values.append(item)
                del values[:-16]

    def read_frame(self,node):
        with self.lock:
            frame=self.monitor.states.get(self.node_key(node),{}).get('frame')
            return frame.copy() if frame is not None else None

    def read_ocr(self, node, frame):
        """Copy exact-frame metadata only; never borrow another capture's tokens."""
        with self.lock:
            state = self.monitor.states.get(self.node_key(node), {})
            if state.get('recognition_source_id') != id(frame):
                return []
            return copy.deepcopy(state.get('ocr_records', []))

    def forget(self, node):
        """A failed fresh capture must not leave an older node frame as evidence."""
        key = self.node_key(node)
        with self.lock:
            self.frames.pop(key, None)
            self.monitor.states.pop(key, None)

    def on_exhaustion(self,event):
        """Only recognized threshold event kinds can produce disk/network evidence."""
        if not isinstance(event,dict) or event.get('kind') not in {'recognition','ocr','chain','node_limit','blocked','exclusion'}:
            return None
        if event.get('source')=='simulation':return None
        event_id=event.get('event_id')
        if not isinstance(event_id,str) or not event_id:return None
        key=self.node_key(event)
        with self.lock:
            if event_id in self.events:return copy.deepcopy(self.events[event_id])
            # Reserve before reporting: a write/notification failure cannot replay evidence.
            self.events[event_id]={'status':'REPORTING'}
            while len(self.events)>self.event_limit:self.events.popitem(last=False)
            # Never substitute another node's image when this node has no frame.
            self.monitor.latest_frame=None
            try:
                categories={'chain':'CHAIN_FAILURE_LIMIT','node_limit':'NODE_EXECUTION_LIMIT',
                            'blocked':'NODE_BLOCKED','exclusion':'INPUT_EXCLUSION_SKIPPED'}
                timeout = event['kind'] == 'chain' and event.get('failure_kind') == 'chain_timeout'
                category = 'CHAIN_TIMEOUT' if timeout else categories.get(event['kind'],'RECOGNITION_RETRY_EXHAUSTED')
                record=self.monitor.report(key,category,
                    str(event.get('reason') or 'Recognition attempts exhausted'),
                    notify=event.get('notify_discord') is True and notification_allowed(event),
                    count_failure=False,error_identifier='chain_timeout' if timeout else event['kind']+'_limit',
                    save_screenshot=screenshot_allowed(event),
                    error_details={k:v for k,v in event.items() if k not in {'frame','result'}},
                    device=self.monitor.states.get(key,{}).get('device'))
                summary={'status':'SAVED','report_path':record['report_path'],
                         'screenshot':record['screenshot'],'notification':record['notification']}
            except Exception as exc:
                summary={'status':'FAILED','reason':type(exc).__name__}
            self.events[event_id]=summary
            # Missing-frame event states do not create an unbounded node history.
            for missing in list(self.monitor.states):
                if missing not in self.frames:self.monitor.states.pop(missing,None)
            return copy.deepcopy(summary)
