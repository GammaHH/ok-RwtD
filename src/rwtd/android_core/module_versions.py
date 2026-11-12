"""Read stored module backups and restore their content as a new revision."""
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone

from .module_store import safe_path, linked, validate_module


def _directory(store, current):
    return safe_path(store.profile.definitions_root,
                     f'{store.profile.modules_root}/{current["kind"]}/backups')


def _read(store, current, backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch(re.escape(current['id']) + r'_[0-9a-f]{32}\.json', backup_id):
        raise ValueError('無效的模組備份識別碼')
    path = safe_path(_directory(store, current), backup_id)
    if not path.is_file() or linked(path):
        raise ValueError('備份已不存在；請重新載入版本清單')
    raw = path.read_bytes()
    data = validate_module(json.loads(raw.decode('utf-8-sig')), store.profile.step_validator)
    if data['id'] != current['id']:
        raise ValueError('備份不屬於目前模組')
    return path, data, hashlib.sha256(raw).hexdigest()


def _value(value):
    if value is None:
        return '未設定'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (str, int, float)):
        return str(value)[:100]
    if isinstance(value, list) and all(type(v) in (int, float) for v in value):
        return ', '.join(map(str, value))
    return None


def differences(current, target):
    """Human-readable current -> proposed restored values, without raw JSON UI."""
    lines = []
    for key in sorted(set(current) | set(target)):
        if key in {'steps', 'version', 'revision', 'kind'} or current.get(key) == target.get(key):
            continue
        before, after = _value(current.get(key)), _value(target.get(key))
        lines.append(f'{key}：{before} → {after}' if before is not None and after is not None else f'{key} 已變更')
    old = {s['step_id']: s for s in current['steps']}
    new = {s['step_id']: s for s in target['steps']}
    if list(old) != list(new):
        lines.append('Step 順序／清單：' + ', '.join(old) + ' → ' + ', '.join(new))
    for ident in dict.fromkeys([*old, *new]):
        if ident not in old:
            lines.append(f'Step {ident}：回復後新增')
        elif ident not in new:
            lines.append(f'Step {ident}：回復後移除')
        else:
            for key in sorted(set(old[ident]) | set(new[ident])):
                if old[ident].get(key) == new[ident].get(key):
                    continue
                before, after = _value(old[ident].get(key)), _value(new[ident].get(key))
                lines.append(f'Step {ident} · {key}：{before} → {after}' if before is not None and after is not None else f'Step {ident} · {key} 已變更')
    return lines[:300], len(lines) > 300


def history(store, module_id):
    current = store.get(module_id)
    directory = _directory(store, current)
    items, skipped = [], 0
    paths = sorted(directory.glob(current['id'] + '_*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True) if directory.is_dir() else []
    for path in paths[:100]:
        try:
            path, data, fingerprint = _read(store, current, path.name)
            changes, truncated = differences(current, data)
            items.append(dict(backup_id=path.name, backup_hash=fingerprint, version=data['version'],
                              saved_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                              step_count=len(data['steps']), name=data['name'], changes=changes,
                              changes_truncated=truncated))
        except (ValueError, OSError, TypeError, UnicodeError):
            skipped += 1
    return dict(module_id=current['id'], current_revision=current['revision'], current_version=current['version'],
                items=items, skipped=skipped, truncated=len(paths) > 100)


def restore(store, module_id, backup_id, backup_hash, expected_revision, confirmed=False, allow_system_update=False):
    if confirmed is not True:
        raise ValueError('請先確認回復模組版本')
    current = store.get(module_id)
    if current['revision'] != expected_revision:
        raise ValueError('目前模組已變更；請重新載入版本清單')
    _, data, fingerprint = _read(store, current, backup_id)
    if not isinstance(backup_hash, str) or fingerprint != backup_hash:
        raise ValueError('備份內容已變更；請重新載入版本清單')
    # save() owns the definition lease, conflict check, backup and increasing
    # version counter. It never rewrites history with an old version number.
    data = deepcopy(data)
    return store.save(data, expected_revision, allow_system_update=allow_system_update)
