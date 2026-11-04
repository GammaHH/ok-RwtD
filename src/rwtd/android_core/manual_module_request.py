"""One-shot local requests consumed by the native FARM owner at a safe boundary."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from .module_store import atomic_json, safe_path, identifier
from .workflow_model import resolve_step


class ManualModuleRequest:
    def __init__(self, root):
        self.root=root
        self.pending=safe_path(root,'assets/rwtd/manual_requests/pending.json')

    def consume(self, task):
        """True means the boundary was occupied; no concurrent main-node input."""
        if not self.pending.exists():
            return False
        request_id=uuid.uuid4().hex
        claim=None
        waiting=False
        try:
            if self.pending.stat().st_size>4096:
                raise ValueError('Manual module request is too large')
            request=json.loads(self.pending.read_text(encoding='utf-8-sig'))
            if (not isinstance(request,dict) or set(request)!={
                    'schema_version','id','module','module_file_sha256','expires_unix'}
                    or request['schema_version']!=1
                    or not isinstance(request['id'],str)
                    or not re.fullmatch('[0-9a-f]{32}',request['id'])
                    or type(request['expires_unix']) not in (int,float)
                    or not time.time()<request['expires_unix']<=time.time()+86400):
                raise ValueError('Invalid or expired manual module request')
            request_id=request['id']
            identifier(request['module'])
            module=task.manual.store.get(request['module'])
            path=safe_path(self.root,f"assets/rwtd/modules/{module['kind']}/{module['id']}.json")
            if hashlib.sha256(path.read_bytes()).hexdigest()!=request['module_file_sha256']:
                raise ValueError('Requested module changed; request not executed')
            if task.controller.device_stop.is_set() or task.exit_is_set():
                return True
            # Quiesce captures using the same boundary used by activity handoffs.
            if not task._activity_ready():
                waiting=True
                return True
            candidate_claim=safe_path(self.root,f'assets/rwtd/manual_requests/claimed/{request_id}.json')
            receipt=safe_path(self.root,f'assets/rwtd/manual_requests/results/{request_id}.json')
            if candidate_claim.exists() or receipt.exists():
                raise ValueError('Manual module request ID was already consumed')
            candidate_claim.parent.mkdir(parents=True,exist_ok=True)
            self.pending.replace(candidate_claim)
            claim=candidate_claim
            base={'id':request_id,'module':module['id'],'version':module['version'],
                  'revision':module['revision'],'started_utc':datetime.now(timezone.utc).isoformat()}
            # Persist before any input. A process crash never replays this claim.
            atomic_json(receipt,dict(base,status='STARTED'))
            task.log_info('RWTD_MANUAL_MODULE_START '+json.dumps(base,ensure_ascii=False))
            steps=[resolve_step(module,s) for s in module['steps']]
            result=task.manual.execute_canvas_steps(steps,module['id'],module['revision'],
                task.controller.device_stop,loop_settings={'max_cycles':1,'round_wait_seconds':1})
            status='COMPLETED' if result.get('action_completed') else 'BLOCKED'
            summary=dict(base,status=status,result_status=result.get('status'),
                device_inputs=result.get('device_inputs'),reason=result.get('failure_reason'),
                finished_utc=datetime.now(timezone.utc).isoformat())
            atomic_json(receipt,summary)
            task.log_info('RWTD_MANUAL_MODULE_DONE '+json.dumps(summary,ensure_ascii=False))
            if status!='COMPLETED':
                task.controller.device_stop.set()
                task.farm.stop()
            return True
        except Exception as exc:
            # Invalid requests cannot authorize Android input or be retried forever.
            rejected=safe_path(self.root,f'assets/rwtd/manual_requests/rejected/{uuid.uuid4().hex}.json')
            if self.pending.exists():
                rejected.parent.mkdir(parents=True,exist_ok=True)
                self.pending.replace(rejected)
            failure={'id':request_id,'status':'FAILED' if claim else 'REJECTED',
                     'reason':type(exc).__name__+': '+str(exc)[:240],
                     'finished_utc':datetime.now(timezone.utc).isoformat()}
            if claim is not None:
                atomic_json(safe_path(self.root,f'assets/rwtd/manual_requests/results/{request_id}.json'),failure)
                task.controller.device_stop.set()
                task.farm.stop()
            task.log_warning('RWTD_MANUAL_MODULE_FAILED '+json.dumps(failure,ensure_ascii=False))
            return True
        finally:
            if not waiting:
                task.controller.activity_inline_observers=False

