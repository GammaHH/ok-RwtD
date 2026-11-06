"""Native-owner-only calibration editor, using the authenticated loopback bridge."""
import base64
import copy
import hashlib
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from .android_core.module_store import ModuleStore, safe_path
from .android_core.resolution_calibration import (
    CalibrationStore, POINTS, RELATIVE, geometry_fields, native_fields, step_signature, validate_native,
)
from .android_core.module_display_mapping import ModuleDisplayMapping
from .android_core.screen_geometry import ScreenGeometry
from .android_core.workflow_model import resolve_step
from .profile import rwtd_profile
from .manual_device_tests import validate_parameters


def png(frame):
    ok, encoded = cv2.imencode('.png', frame)
    if not ok: raise ValueError('PNG encoding failed')
    return encoded.tobytes()


def project_reference(frame, geometry):
    """Inverse of the same piecewise frame normalization used by runtime."""
    reference, physical = geometry.anchors
    boundaries = [round(v) for v in physical]
    parts = [cv2.resize(frame[reference[i]:reference[i+1]],
                       (1080, boundaries[i+1]-boundaries[i]), interpolation=cv2.INTER_LINEAR)
             for i in range(len(reference)-1)]
    logical = np.concatenate(parts, axis=0)
    return cv2.resize(logical, (geometry.width, geometry.height), interpolation=cv2.INTER_LINEAR)


class CalibrationEditor:
    def __init__(self, root, capture, swipe=None):
        self.root = Path(root)
        self.capture = capture
        self.swipe = swipe
        self.store = CalibrationStore(self.root)
        self.modules = ModuleStore(rwtd_profile(root, self.validate_step))
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.jobs = queue.Queue(maxsize=8)
        self.receipts = {}
        self.images = {}
        self.geometry = None
        self.device = None
        self.module = None
        self.source = None
        self.reference = None
        self.reference_label = None
        self.reference_hash = None
        self.state = {'message': '請擷取實機畫面，再選模組與 Step', 'selection': None}
        self.refresh()

    def validate_step(self, payload):
        from .android_core.feature_catalog import feature_names
        names = feature_names(self.root)
        return validate_parameters(payload, names if names else None)

    def _log(self, *args):
        pass

    def refresh(self):
        self.profiles, self.revision = self.store.snapshot()
        self.mapping = ModuleDisplayMapping(self.root)
        self.baseline_mapping = copy.copy(self.mapping)
        self.baseline_mapping.calibrations = {}
        self.catalog = self.modules.catalog()

    def _size(self):
        return f'{self.geometry.width}x{self.geometry.height}' if self.geometry else None

    def _rows(self, module):
        previous = self.profiles['profiles'].get(self._size(), {}).get(module['id'], {})
        rows = []
        for step in module['steps']:
            resolved = resolve_step(module, step)
            saved = previous.get(step['step_id'])
            try:
                baseline = self.baseline_mapping.apply(SimpleNamespace(screen_geometry=lambda: self.geometry), module['id'], resolved) if self.geometry else resolved
            except (ValueError,TypeError,KeyError) as exc:
                rows.append({'step_id':step['step_id'],'name':step['name'],'status':'ERROR',
                             'changed_fields':[],'geometry_count':0,'error':str(exc)[:160]})
                continue
            mapping_changed = bool(saved and saved['baseline_fields'] != geometry_fields(baseline))
            status = 'NEW' if not saved else 'VALID' if saved['signature'] == step_signature(resolved) and not mapping_changed else 'CHANGED'
            fields = geometry_fields(resolved)
            old = (saved or {}).get('base_fields', {})
            changes = [key for key in sorted(set(fields) | set(old)) if fields.get(key) != old.get(key)] if saved else list(fields)
            rows.append({'step_id': step['step_id'], 'name': step['name'], 'status': status,
                         'changed_fields': changes, 'geometry_count': len(fields),
                         'other_changes': bool(saved and status == 'CHANGED' and not changes),
                         'mapping_changed': mapping_changed})
        current = {s['step_id'] for s in module['steps']}
        rows.extend({'step_id': sid, 'name': '已刪除的 Step', 'status': 'DELETED',
                     'changed_fields': [], 'geometry_count': 0} for sid in previous if sid not in current)
        return rows

    def snapshot(self):
        with self.lock:
            summary = []
            for module in self.catalog:
                rows = self._rows(module)
                summary.append({'id': module['id'], 'name': module['name'], 'version': module['version'],
                                'counts': {k: sum(r['status'] == k for r in rows)
                                           for k in ('NEW', 'VALID', 'CHANGED', 'DELETED','ERROR')}})
            return copy.deepcopy(dict(self.state, modules=summary,
                steps=self._rows(self.module) if self.module else [],
                resolution=self._size(), device=self.device, revision=self.revision,
                reference_label=self.reference_label, has_reference='source' in self.images,
                receipts=self.receipts, closing=self.closing.is_set()))

    def image(self, which):
        with self.lock: return self.images.get(which)

    def enqueue(self, request_id, command, payload):
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100
                or not isinstance(payload, dict) or command not in {
                    'capture', 'select', 'save', 'remove', 'reference', 'close', 'refresh', 'test_swipe'}):
            raise ValueError('Invalid calibration command')
        with self.lock:
            if request_id in self.receipts: return self.receipts[request_id]
            if self.closing.is_set(): raise ValueError('校正任務已關閉')
            try: self.jobs.put_nowait((request_id, command, copy.deepcopy(payload)))
            except queue.Full: raise ValueError('操作佇列已滿，請稍候')
            self.receipts[request_id] = {'status': 'QUEUED'}
            return self.receipts[request_id]

    def process_next(self):
        try: request_id, command, payload = self.jobs.get_nowait()
        except queue.Empty: return
        with self.lock:
            try:
                self.command(command, payload)
                receipt = {'status': 'COMPLETED'}
            except Exception as exc:
                receipt = {'status': 'FAILED', 'error': str(exc)[:400]}
                self.state['message'] = receipt['error']
            self.receipts[request_id] = receipt
            while len(self.receipts) > 32: self.receipts.pop(next(iter(self.receipts)))

    def _capture(self):
        frame, device = self.capture()
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError('需使用原生 Android 彩色畫面')
        geometry = ScreenGeometry(frame.shape[1], frame.shape[0])
        identity = {'device_identifier': device.get('device_identifier'), 'resolution': [geometry.width, geometry.height]}
        if self.device and self.device != identity:
            raise ValueError('裝置或解析度已變更；關閉此校正 TASK 後重新啟動')
        self.geometry, self.device = geometry, identity
        self.images['frame'] = png(frame)
        self.state['captured_utc'] = datetime.now(timezone.utc).isoformat()

    def select(self, module_id, step_id=None):
        if not self.geometry: raise ValueError('請先擷取目前實機畫面')
        module = self.modules.get(module_id)
        self.module, self.source = module, None
        self.reference = self.reference_hash = self.reference_label = None
        self.images.pop('source', None)
        self.state['selection'] = None
        if step_id is None: return
        selected = next((s for s in module['steps'] if s['step_id'] == step_id), None)
        if not selected: raise ValueError('Step 已刪除；無法套用到其他 Step')
        self.source = resolve_step(module, selected)
        task = SimpleNamespace(screen_geometry=lambda: self.geometry)
        effective = self.mapping.apply(task, module_id, self.source)
        defaults = self.baseline_mapping.apply(task, module_id, self.source)
        default_fields = native_fields(defaults, self.geometry, auto_swipe=True)
        saved = self.profiles['profiles'].get(self._size(), {}).get(module_id, {}).get(step_id)
        valid = (saved and saved['signature'] == step_signature(self.source)
                 and saved['baseline_fields'] == geometry_fields(defaults))
        calibrated_points = valid and any(row['kind']=='point' for row in saved['fields'].values())
        fields = native_fields(effective, self.geometry, auto_swipe=not calibrated_points)
        # Keep exactly authored physical pixels in the editor (inverse roundoff is runtime-only).
        if valid:
            for key, row in saved['fields'].items(): fields[key] = copy.deepcopy(row)
        self.state['selection'] = {'module': module_id, 'module_revision': module['revision'],
            'step_id': step_id, 'signature': step_signature(self.source),
            'can_test_swipe': self.swipe is not None and self.source['name'] == 'SwipeCalibration',
            'fields': fields, 'defaults': default_fields,
            'overrides': list(saved['fields']) if valid else [],
            'saved_fields': copy.deepcopy(saved['fields']) if saved and not valid else None,
            'base_fields': geometry_fields(self.source),
            'warning': '原校正已過期；右圖顯示目前預設轉換，請重新校正。' if saved and not valid else None}
        if saved and saved.get('reference'):
            path = safe_path(self.root, RELATIVE + '/images/' + saved['reference'] + '.png')
            if path.is_file():
                self.reference = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                self.reference_hash = saved['reference']
                self.reference_label = ('獨立校正存檔的參考圖（基準 1080×2400）' if valid
                                        else '舊版本參考圖（僅供對照，請確認是否需要重新匯入）')
        if self.reference is None: self._history_reference(module, step_id)
        self._render_reference()

    def _history_reference(self, module, step_id):
        from .android_core.storage_history import StorageHistory
        storage = StorageHistory(self.modules.profile)
        # Only matching saved revision + Step ID; never guess another Step's picture.
        for record in sorted(storage.read_history(), key=lambda r:r['report_id'], reverse=True):
            if (record.get('workflow_module') != module['id'] or record.get('step_id') != step_id
                    or record.get('workflow_revision') != module['revision'] or record.get('screenshots_cleaned')):
                continue
            path = storage.path('session_' + record['report_id'] + '/before.png')
            if path.is_file():
                frame = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                if frame is not None and frame.shape == (2400,1080,3):
                    self.reference = frame
                    self.reference_label = '相同版本 Step 測試前圖：' + record['report_id']
                    return

    def _render_reference(self):
        if self.reference is not None:
            self.images['source'] = png(project_reference(self.reference, self.geometry))

    def _persist_reference(self):
        if self.reference is None: return None
        data = png(self.reference)
        stamp = hashlib.sha256(data).hexdigest()
        path = safe_path(self.root, RELATIVE + '/images/' + stamp + '.png')
        if not path.exists():
            directory = path.parent
            directory.mkdir(parents=True, exist_ok=True)
            total = sum(p.stat().st_size for p in directory.glob('*.png'))
            if total + len(data) > 128*1024**2:
                raise ValueError('校正參考圖已達 128 MiB 上限；請先備份並整理，不自動刪除圖片')
            temp = safe_path(self.root, RELATIVE + '/images/' + uuid.uuid4().hex + '.tmp')
            try:
                temp.write_bytes(data)
                temp.replace(path)
            finally: temp.unlink(missing_ok=True)
        return stamp

    def _test_swipe(self, payload):
        selection = self.state['selection']
        if not selection or not selection['can_test_swipe']:
            raise ValueError('請先選擇可試滑的滑動 Step')
        if any(payload.get(key) != selection[key] for key in ('module', 'step_id', 'signature')):
            raise ValueError('選取的 Step 已變更；請重新載入')
        keys = [f'{x},{y}' for x, y in POINTS if x in self.source]
        draft = payload.get('points')
        if not isinstance(draft, dict) or set(draft) != set(keys):
            raise ValueError('滑動觸點不完整')
        points = [draft[key] for key in keys]
        for point in points:
            validate_native('point', point, self.geometry)
        if all(point == points[0] for point in points):
            raise ValueError('滑動路徑至少需要兩個不同位置')
        from .android_core.execution_lease import acquire
        with acquire(self.root, self):
            if self.modules.get(self.module['id'])['revision'] != self.module['revision']:
                raise ValueError('模組已更新；請重新選取 Step')
            self._capture()
            self.swipe(points, self.source['duration_ms'], self.device)
            try:
                self._capture()
            except Exception as exc:
                self.state['message'] = f'試滑已送出，但更新截圖失敗：{exc}'
            else:
                self.state['message'] = '試滑已送出並刷新實機畫面；草稿仍可調整或儲存'

    def command(self, command, payload):
        if command == 'close': self.closing.set(); return
        if command == 'capture':
            self._capture()
            self._render_reference()
            self.state['message'] = '已擷取實機畫面'
        elif command == 'test_swipe':
            self._test_swipe(payload)
        elif command == 'refresh':
            self.refresh()
            self.module = self.source = None
            self.state['selection'] = None
            self.images.pop('source', None)
        elif command == 'select': self.select(payload['module'], payload.get('step_id'))
        elif command == 'reference':
            if not self.source: raise ValueError('請先選擇 Step')
            encoded = payload.get('image', '')
            if not isinstance(encoded, str) or len(encoded) > 8*1024**2:
                raise ValueError('參考圖片過大（上限 6 MiB）')
            data = base64.b64decode(encoded, validate=True)
            # Header validation before decompression prevents oversized image allocation.
            from io import BytesIO
            from PIL import Image
            with Image.open(BytesIO(data)) as header:
                geometry = ScreenGeometry(*header.size)
                if header.width*header.height > 12000000: raise ValueError('參考圖像素過大')
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is None: raise ValueError('圖片讀取失敗')
            self.reference = geometry.normalize(frame)
            self.reference_hash = None
            self.reference_label = f'使用者匯入 {geometry.width}×{geometry.height} → 1080×2400'
            self._render_reference()
        elif command in {'save', 'remove'}:
            if not self.source: raise ValueError('請先選擇 Step')
            if payload.get('revision') != self.revision or payload.get('signature') != step_signature(self.source):
                raise ValueError('編輯內容已更新；請重新載入')
            from .android_core.execution_lease import editing
            with editing(self.root):
                current = self.modules.get(self.module['id'])
                if current['revision'] != self.module['revision']:
                    raise ValueError('模組已更新；請重新選取 Step 後校正')
                if self.store.snapshot()[1] != payload['revision']:
                    raise ValueError('校正存檔已更新；請重新載入，草稿尚未覆寫')
                # Verify physical device again before saving; do not trust an old preview size.
                self._capture()
                reference = self._persist_reference() if command == 'save' else None
                baseline = self.baseline_mapping.apply(SimpleNamespace(screen_geometry=lambda:self.geometry), current['id'], self.source)
                self.store.save(current, self.source, [self.geometry.width,self.geometry.height],
                    payload.get('fields', {}), payload['revision'], reference,
                    remove=command == 'remove', baseline=baseline)
            module_id, step_id = current['id'], self.source['step_id']
            self.refresh()
            self.select(module_id, step_id)
            self.state['message'] = '獨立校正已儲存；原模組未更動。下次啟動此解析度的任務時套用。' if command == 'save' else '已移除本解析度的 Step 校正'
