"""Canvas deletion snapshots in the shared trash; no execution or device work."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from .decision_graph import validate_graph
from .module_store import atomic_json, identifier, safe_path


def directory(store):
    return safe_path(store.definitions_root, store.profile.data_root + '/scheduler/backups')


def read(store, backup_id):
    if not isinstance(backup_id,str) or not re.fullmatch(r'deleted_canvas_[0-9a-f]{32}\.json',backup_id):
        raise ValueError('無效的 Canvas 垃圾桶識別碼')
    path = safe_path(directory(store),backup_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError('備份不存在或類別不符；請重新載入垃圾桶') from None
    value = json.loads(raw.decode('utf-8-sig'))
    if (not isinstance(value,dict) or set(value) != {'schema_version','trash_kind','canvas_id','name','graph'}
            or value['schema_version'] != 1 or value['trash_kind'] != 'canvas'
            or not isinstance(value['name'],str) or not 1 <= len(value['name']) <= 80
            or not isinstance(value['graph'],dict) or not isinstance(value['graph'].get('nodes'),list)):
        raise ValueError('Canvas 刪除備份格式無效')
    identifier(value['canvas_id'])
    return path,value,hashlib.sha256(raw).hexdigest()


def listing(store):
    folder = directory(store)
    registry = store.registry()
    paths,items,skipped = [],[],0
    if folder.is_dir():
        for path in folder.glob('deleted_canvas_*.json'):
            try:paths.append((path.stat().st_mtime_ns,path.name))
            except OSError:skipped += 1
    paths.sort(reverse=True)
    for _,name in paths[:100]:
        try:
            path,value,fingerprint = read(store,name)
            items.append(dict(item_type='canvas',backup_id=name,backup_hash=fingerprint,
                              id=value['canvas_id'],name=value['name'],node_count=len(value['graph']['nodes']),
                              deleted_at=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                              conflict=value['canvas_id'] in registry['entries']
                              or any(v['name'].casefold()==value['name'].casefold() for v in registry['entries'].values())))
        except (ValueError,OSError,TypeError,UnicodeError):skipped += 1
    return dict(items=items,skipped=skipped,truncated=len(paths)>100)


def blank():
    return dict(schema_version=2,entry='Start',nodes=[dict(id='Start',kind='start',name='開始'),dict(id='End',kind='end',name='結束')],
                edges=[dict(id='E1',source='Start',port='NEXT',target='End')],
                settings=dict(max_cycles=10,round_wait_seconds=1,node_handoff_seconds=1,chain_timeout_minutes=25),
                ui=dict(positions=dict(Start=dict(x=60,y=60),End=dict(x=60,y=200)),viewport=dict(x=0,y=0,zoom=1)))


def delete(store, expected_revision, confirmed):
    store._editable()
    if confirmed is not True:raise ValueError('請明確確認刪除 Canvas')
    registry = store.registry()
    canvas_id = store.active_id()
    graph,_ = store.load_for_edit()
    if graph is None or graph['revision'] != expected_revision:raise ValueError('Canvas 已變更，請重新載入')
    path = store.path('decision.json')
    snapshot = json.loads(path.read_text(encoding='utf-8-sig'))
    backup = safe_path(directory(store),'deleted_canvas_'+uuid.uuid4().hex+'.json')
    atomic_json(backup,dict(schema_version=1,trash_kind='canvas',canvas_id=canvas_id,
                            name=registry['entries'][canvas_id]['name'],graph=snapshot))
    original = store.registry()
    del registry['entries'][canvas_id]
    created = None
    if not registry['entries']:
        new_id = 'Canvas_'+uuid.uuid4().hex
        created = safe_path(store.definitions_root,store.profile.data_root+'/scheduler/canvases/'+new_id+'.json')
        atomic_json(created,validate_graph(blank(),{}))
        registry['entries'][new_id] = {'name':'空白 Canvas'}
    registry['active'] = next(iter(registry['entries']))
    try:
        atomic_json(store.registry_path(),registry)
        path.unlink()
    except OSError:
        atomic_json(store.registry_path(),original)
        if created is not None:created.unlink(missing_ok=True)
        raise
    store.canvas_id = None
    store._registry_cache = store._graph_cache = None
    from .record_retention import enforce
    enforce(store.definitions_root,'backups',store.profile.data_root+'/scheduler/backups',protected={backup.name})
    return dict(backup=str(backup),deleted_canvas_id=canvas_id)


def apply(store, backup_id, backup_hash, operation, confirmed):
    store._editable()
    if operation not in {'restore','purge'} or confirmed is not True:raise ValueError('請明確確認垃圾桶操作')
    path,value,fingerprint = read(store,backup_id)
    if fingerprint != backup_hash:raise ValueError('備份已變更，請重新載入垃圾桶')
    if operation == 'purge':
        path.unlink()
        return dict(purged=True,name=value['name'])
    registry = store.registry()
    canvas_id = value['canvas_id']
    if canvas_id in registry['entries']:raise ValueError('同 ID Canvas 已存在，不覆寫')
    if len(registry['entries']) >= 1000:raise ValueError('Canvas 數量已達上限')
    store._name(value['name'],registry)
    relative = 'decision.json' if canvas_id == 'default' else 'canvases/'+canvas_id+'.json'
    destination = safe_path(store.definitions_root,store.profile.data_root+'/scheduler/'+relative)
    if destination.exists():raise ValueError('同 ID Canvas 檔案已存在，不覆寫')
    graph = validate_graph(value['graph'],{m['id']:m for m in store.modules.catalog()})
    atomic_json(destination,graph)
    registry['entries'][canvas_id] = {'name':value['name']}
    registry['active'] = canvas_id
    try:atomic_json(store.registry_path(),registry)
    except OSError:
        destination.unlink(missing_ok=True)
        raise
    store.canvas_id = None
    store._registry_cache = store._graph_cache = None
    return dict(restored_canvas=dict(id=canvas_id,name=value['name']),backup_retained=True)
