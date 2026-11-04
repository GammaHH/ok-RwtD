"""Local by default; live Dashboard submits to the configured OK notification API."""
import hashlib
import json
import math
import os
import time
import uuid

from src.rwtd.discord_notifications import DiscordNotifier, storage_path, write_json


def notify(root, node, context, device, enabled=False, clock=time.time, sender=None, native_sender=None):
    sample = context.get('variables', {}).get(node.get('variable'))
    if node.get('variable') and (not sample or sample.get('verified') is not True):
        raise ValueError('Notification variable UNKNOWN')
    text = node['message'].replace('{value}', str(sample['value']) if sample else '')
    key = 'canvas:'+str(context['revision'])+':'+node['id']+':'+str(device.get('device_identifier'))
    cooldown = node.get('cooldown_seconds',120)
    if enabled and native_sender is None:
        service = sender or DiscordNotifier(root, cooldown_seconds=cooldown, duplicate_seconds=max(900,cooldown))
        return service.send(text, dedupe_key=key)
    folder = storage_path(root, 'docs/rwtd/notifications')
    state_dir = storage_path(root, 'docs/rwtd/notifications/.state')
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir/'canvas_local.lock'
    try:
        fd = os.open(lock_path, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    except FileExistsError:
        return {'status':'notification_service_busy','delivery_verified':False}
    try:
        os.close(fd)
        path = storage_path(root, 'docs/rwtd/notifications/.state/canvas_local.json')
        ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(ledger,dict) or any(type(v) not in (int,float) or not math.isfinite(v) for v in ledger.values()):
            raise ValueError('Invalid local notification ledger')
        # A local preview must not suppress the first enabled native notification.
        ledger_key = key+':native' if enabled else key
        fingerprint = hashlib.sha256(ledger_key.encode()).hexdigest()
        now = clock()
        if now-ledger.get(fingerprint, -1e20) < cooldown:
            return {'status':'duplicate_suppressed','delivery_verified':False}
        audit = folder/('canvas_'+uuid.uuid4().hex+'.json')
        record = {'status':'local_only','description':text,'variable':sample,'node':node['id'],
                  'timestamp':now,'delivery_verified':False}
        if enabled:
            record['status'] = 'native_pending'
        write_json(audit, record)
        ledger = {k:v for k,v in ledger.items() if now-v < 86400}
        ledger[fingerprint] = now
        write_json(path,ledger)
        if enabled:
            try:
                native_sender(text)
                record['status'] = 'native_submitted'
            except Exception:
                # Framework exceptions may contain provider credentials.
                record['status'] = 'native_submission_failed'
            write_json(audit, record)
        return {'status':record['status'],'delivery_verified':False,'audit_path':str(audit)}
    finally:
        lock_path.unlink(missing_ok=True)
