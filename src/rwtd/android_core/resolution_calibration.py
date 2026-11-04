"""Separate, revision-bound per-display Step geometry; never rewrite modules."""
import copy
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager

from .module_store import atomic_json, safe_path, identifier
from .screen_geometry import ScreenGeometry

RELATIVE = 'assets/rwtd/calibration'
ROI_KEYS = {'roi', 'coco_roi', 'ocr_roi', 'color_roi', 'global_roi', 'search_roi'}
POINTS = (('x', 'y'), ('mid_x', 'mid_y'), ('end_x', 'end_y'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def geometry_fields(step):
    """JSON-pointer-like paths, including resolved TAG/condition/feature ROIs."""
    found = {}
    for x, y in POINTS:
        if x in step and y in step:
            found[x + ',' + y] = {'kind': 'point', 'value': [step[x], step[y]]}

    def walk(value, path=''):
        if isinstance(value, dict):
            for key, child in value.items():
                escaped = key.replace('~', '~0').replace('/', '~1')
                here = path + '/' + escaped
                if ((key in ROI_KEYS or path.endswith('/coco_feature_rois'))
                        and isinstance(child, list) and len(child) == 4
                        and all(type(v) is int for v in child)):
                    found[here] = {'kind': 'roi', 'value': list(child)}
                else:
                    walk(child, here)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + '/' + str(index))
    walk(step)
    return found


def step_signature(step):
    # Hash the saved, resolved execution definition, ignoring only runtime grants.
    ignored = {'reversible_ack', 'dashboard_manual', 'workflow_module',
               '_entry_guard_checked', '_step_retry_authorized'}
    source = {k: v for k, v in step.items() if k not in ignored and not k.startswith('_')}
    source.setdefault('threshold', .9)
    source.setdefault('repeat_count', 1)
    if source.get('name') in {'TapTest', 'CocoTargetTest'}:
        source['duration_ms'] = source.get('duration_ms', 0) or 50
    return digest(source)


def set_field(step, key, values):
    if not key.startswith('/'):
        x, y = key.split(',')
        step[x], step[y] = values
        return
    parts = [s.replace('~1', '/').replace('~0', '~') for s in key[1:].split('/')]
    target = step
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = list(values)


def native_fields(step, geometry, *, auto_swipe=False):
    if auto_swipe and step.get('name') == 'SwipeCalibration' and 'mid_x' in step:
        step = copy.deepcopy(step)
        points = geometry.calibrated_three_point_reference([[step[x],step[y]] for x,y in POINTS])
        for (x,y),point in zip(POINTS,points): step[x],step[y] = point
    return {key: dict(row, value=list(geometry.point(*row['value']))
                     if row['kind'] == 'point' else geometry.physical_roi(row['value']))
            for key, row in geometry_fields(step).items()}


def reference_roi(roi, geometry):
    x, y, w, h = roi
    reference, physical = geometry.anchors
    def inverse(v):
        logical = v / geometry.scale
        for i in range(len(physical)-1):
            if logical <= physical[i+1]:
                return reference[i] + (logical-physical[i]) / (physical[i+1]-physical[i]) * (reference[i+1]-reference[i])
        raise ValueError('ROI exceeds display')
    left, top = math.floor(x/geometry.scale), math.floor(inverse(y))
    right, bottom = math.ceil((x+w)/geometry.scale), math.ceil(inverse(y+h))
    return [left, top, right-left, bottom-top]


def validate_native(kind, values, geometry):
    if kind not in {'point','roi'}: raise ValueError('Invalid calibration geometry type')
    length = 2 if kind == 'point' else 4
    if not isinstance(values, list) or len(values) != length or any(type(v) is not int for v in values):
        raise ValueError('座標必須為整數')
    x, y = values[:2]
    if not 0 <= x < geometry.width or not 0 <= y < geometry.height:
        raise ValueError('座標超出實機畫面')
    if kind == 'roi':
        w, h = values[2:]
        if w <= 0 or h <= 0 or x+w > geometry.width or y+h > geometry.height:
            raise ValueError('ROI 超出實機畫面')


@contextmanager
def file_lock(path):
    # OS lock automatically releases on crash; no stale exclusive-create lock.
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class CalibrationStore:
    def __init__(self, root):
        self.root = root
        self.path = safe_path(root, RELATIVE + '/profiles.json')

    def load(self):
        if self.path.exists() and self.path.stat().st_size > 16*1024**2:
            raise ValueError('校正存檔超過 16 MiB 上限')
        data = json.loads(self.path.read_text(encoding='utf-8-sig')) if self.path.exists() else {
            'schema_version': 1, 'profiles': {}}
        if (not isinstance(data, dict) or set(data) != {'schema_version', 'profiles'}
                or data['schema_version'] != 1 or not isinstance(data['profiles'], dict)):
            raise ValueError('校正存檔格式錯誤；請由備份還原')
        for size, modules in data['profiles'].items():
            if not re.fullmatch(r'[0-9]{3,4}x[0-9]{3,4}', size):
                raise ValueError('Invalid calibration display')
            geometry = ScreenGeometry(*map(int, size.split('x')))
            if not isinstance(modules, dict): raise ValueError('Invalid calibration modules')
            for module, steps in modules.items():
                identifier(module)
                if not isinstance(steps, dict): raise ValueError('Invalid calibration Steps')
                for step_id, row in steps.items():
                    if (not isinstance(step_id, str) or not 1 <= len(step_id) <= 80
                            or not isinstance(row, dict) or set(row) != {
                                'signature', 'module_revision', 'base_fields', 'baseline_fields', 'fields', 'reference'}
                            or not isinstance(row['signature'],str)
                            or not re.fullmatch(r'[a-f0-9]{64}', row['signature'])
                            or not isinstance(row['module_revision'],str)
                            or not isinstance(row['fields'], dict) or not isinstance(row['base_fields'], dict)
                            or not isinstance(row['baseline_fields'], dict)):
                        raise ValueError('Invalid calibration Step binding')
                    for key, item in row['fields'].items():
                        if (not isinstance(item,dict) or set(item)!={'kind','value'}
                                or key not in row['base_fields'] or not isinstance(row['base_fields'][key],dict)
                                or item.get('kind') != row['base_fields'][key].get('kind')):
                            raise ValueError('Invalid calibration field binding')
                        validate_native(item['kind'], item['value'], geometry)
                    if row['reference'] is not None and (not isinstance(row['reference'],str)
                            or not re.fullmatch(r'[a-f0-9]{64}', row['reference'])):
                        raise ValueError('Invalid reference image')
        return data

    def snapshot(self):
        data = self.load()
        return data, digest(data)

    def save(self, module, source, size, fields, revision, reference=None, remove=False, baseline=None):
        from .execution_lease import editing
        geometry = ScreenGeometry(*size)
        key = f'{geometry.width}x{geometry.height}'
        available = geometry_fields(source)
        if reference is not None and (not isinstance(reference,str) or not re.fullmatch(r'[a-f0-9]{64}',reference)):
            raise ValueError('Invalid reference image identity')
        if not isinstance(fields, dict) or set(fields) - set(available):
            raise ValueError('不能新增未存在於 Step 的座標欄位')
        for field, row in fields.items():
            if not isinstance(row, dict) or set(row) != {'kind', 'value'} or row['kind'] != available[field]['kind']:
                raise ValueError('Invalid calibration field')
            validate_native(row['kind'], row['value'], geometry)
        if source.get('name') == 'SwipeCalibration' and 'mid_x' in source:
            all_points = {key for key,row in available.items() if row['kind']=='point'}
            if any(row['kind']=='point' for row in fields.values()) and not all_points.issubset(fields):
                raise ValueError('三點滑動需一起保存起點、中點與終點，避免重複長度補償')
        with editing(self.root):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(safe_path(self.root, RELATIVE + '/write.lock')):
                data, current = self.snapshot()
                if current != revision:
                    raise ValueError('校正存檔已更新；請重新載入，避免覆蓋其他視窗')
                steps = data['profiles'].setdefault(key, {}).setdefault(module['id'], {})
                if remove:
                    steps.pop(source['step_id'], None)
                else:
                    steps[source['step_id']] = {'signature': step_signature(source),
                        'module_revision': module['revision'], 'base_fields': available,
                        'baseline_fields': geometry_fields(baseline if baseline is not None else source),
                        'fields': copy.deepcopy(fields), 'reference': reference}
                if len(json.dumps(data,ensure_ascii=False).encode('utf-8')) > 16*1024**2:
                    raise ValueError('校正存檔超過 16 MiB 上限，未覆寫')
                # Validate the full file on next load; preserve ten immutable prior files.
                if self.path.exists():
                    backup = safe_path(self.root, RELATIVE + '/backups/' + current + '.json')
                    if not backup.exists(): atomic_json(backup, self.load())
                backups = sorted(safe_path(self.root, RELATIVE + '/backups').glob('*.json'),
                                 key=lambda p: p.stat().st_mtime_ns, reverse=True)
                for old in backups[10:]:
                    safe_path(self.root, str(old.relative_to(self.root))).unlink()
                atomic_json(self.path, data)
        return digest(data)

    def apply(self, profiles, geometry, module, source, mapped):
        row = profiles.get(f'{geometry.width}x{geometry.height}', {}).get(module, {}).get(str(source.get('step_id')))
        if (not row or row['signature'] != step_signature(source)
                or row['baseline_fields'] != geometry_fields(mapped)):
            return mapped
        available = geometry_fields(source)
        for key, field in row['fields'].items():
            if key not in available or field['kind'] != available[key]['kind']:
                raise ValueError('校正欄位與 Step 不符；停止套用')
            native = field['value']
            value = list(geometry.reference_point(*native)) if field['kind'] == 'point' else reference_roi(native, geometry)
            set_field(mapped, key, value)
        return mapped

    def has_points(self, profiles, geometry, module, source, mapped):
        row = profiles.get(f'{geometry.width}x{geometry.height}', {}).get(module, {}).get(str(source.get('step_id')))
        return bool(row and row['signature'] == step_signature(source) and
                    row['baseline_fields'] == geometry_fields(mapped) and
                    any(field['kind'] == 'point' for field in row['fields'].values()))
