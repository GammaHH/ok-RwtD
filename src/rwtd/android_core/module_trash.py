"""Deleted module snapshots, restored without overwriting an existing identity."""
import hashlib
import json
import re
from datetime import datetime, timezone

from .execution_lease import definition_edit
from .module_store import safe_path, validate_module


def _directory(store):
    return safe_path(store.profile.definitions_root, store.profile.modules_root + '/user/backups')


def _read(store, backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch(r'deleted_[A-Za-z][A-Za-z0-9_-]{0,79}_[0-9a-f]{32}\.json', backup_id):
        raise ValueError('無效的垃圾桶備份識別碼')
    path = safe_path(_directory(store), backup_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError('備份不存在或類別不符；請重新載入垃圾桶') from None
    data = validate_module(json.loads(raw.decode('utf-8-sig')), store.profile.step_validator)
    if not re.fullmatch('deleted_' + re.escape(data['id']) + r'_[0-9a-f]{32}\.json', backup_id):
        raise ValueError('備份內容與模組不一致')
    return path, data, hashlib.sha256(raw).hexdigest()


def listing(store, graphs=None):
    folder = _directory(store)
    current = {m['id'].casefold() for m in store.catalog()}
    items, skipped = [], 0
    paths = []
    if folder.is_dir():
        for path in folder.glob('deleted_*.json'):
            try:
                paths.append((path.stat().st_mtime_ns, path.name))
            except OSError:
                skipped += 1
    paths.sort(reverse=True)
    for _, name in paths[:100]:
        try:
            path, data, fingerprint = _read(store, name)
            items.append(dict(item_type='module',backup_id=name, backup_hash=fingerprint, id=data['id'], name=data['name'],
                              version=data['version'], step_count=len(data['steps']),
                              deleted_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                              conflict=data['id'].casefold() in current))
        except (OSError, ValueError, TypeError, UnicodeError):
            skipped += 1
    from .decision_graph import DecisionGraphStore
    from .canvas_trash import listing as canvas_listing
    canvas = canvas_listing(graphs or DecisionGraphStore(store.profile,store))
    items.extend(canvas['items'])
    items.sort(key=lambda item:item['deleted_at'],reverse=True)
    return dict(items=items[:100], skipped=skipped+canvas['skipped'],
                truncated=len(paths)>100 or canvas['truncated'] or len(items)>100)


@definition_edit
def apply(store, backup_id, backup_hash, *, operation, confirmed=False, item_type='module', graphs=None):
    if operation not in {'restore', 'purge'} or confirmed is not True:
        raise ValueError('請明確確認還原或永久刪除')
    if item_type == 'canvas':
        from .decision_graph import DecisionGraphStore
        from .canvas_trash import apply as canvas_apply
        return canvas_apply(graphs or DecisionGraphStore(store.profile,store),backup_id,backup_hash,operation,confirmed)
    if item_type != 'module':raise ValueError('無效的垃圾桶項目類別')
    path, data, fingerprint = _read(store, backup_id)
    if fingerprint != backup_hash:
        raise ValueError('備份已變更，請重新載入垃圾桶')
    if operation == 'purge':
        path.unlink()
        return dict(purged=True, name=data['name'])
    if any(m['id'].casefold() == data['id'].casefold() for m in store.catalog()):
        raise ValueError('同一模組 ID 已存在；不覆寫目前模組')
    for kind in ('system', 'user'):
        if safe_path(store.profile.definitions_root, f"{store.profile.modules_root}/{kind}/{data['id']}.json").exists():
            raise ValueError('同一模組檔案已存在；請先檢查目前存檔')
    # Restoring the same identity continues its counter, unlike duplication.
    data['version'] += 1
    data.pop('revision', None)
    restored = store.save(data)
    # Keep the deleted snapshot available until explicit permanent deletion.
    return dict(restored=restored, backup_retained=path.is_file())
