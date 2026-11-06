"""Physical-device dashboard; offline fixtures remain in the test backend."""
import json
import math
import re
import threading
import time
import webbrowser
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from ok import BaseTask
from ok.device.capture import ADBCaptureMethod
from ok.device.interaction_methods.adb import ADBInteraction
from ok.feature.Box import Box

from src.rwtd.preview_matcher import PreviewMatcher
from src.rwtd.dashboard_bridge import DashboardBridge
from src.rwtd.manual_device_tests import ManualDeviceTests
from src.rwtd.simulation_scheduler import SimulationController
from src.rwtd.exception_monitor import get_monitor, device_snapshot
from src.rwtd.android_core.recognition import consolidate
from src.rwtd.dashboard_runtime import DashboardRuntime
from src.rwtd.android_core.screen_geometry import ScreenGeometry, expected_size
from src.rwtd.android_core.loop_settings_override import (
    DEFAULTS as LOOP_DEFAULTS, DESCRIPTIONS as LOOP_DESCRIPTIONS,
    SOURCE_KEY as LOOP_SOURCE, settings_from_ok,
)


class _ReferenceCapture:
    """Stable proxy: every caller receives a 1080x2400 reference frame."""

    def __init__(self, task, backend):
        self.task, self.backend = task, backend

    def get_frame(self, *args, **kwargs):
        return self.task.normalize_device_frame(self.backend.get_frame(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self.backend, name)


class RwtdControlDashboardTask(BaseTask):
    # Official DeviceManager -> adbutils shell(stream=True); Farm inherits this.
    supports_device_macro = True
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rwtd_offline_editor = type(self).__name__ == 'RwtdControlDashboardTask'
        self.name = "RwtD 控制面板 / Control dashboard"
        self.description = "啟動後唯讀擷取手機並分析；自動刷新預設關閉。另行啟用後可手動測試；Auto Farming、Discord 停用。"
        self.default_config = {"Start Live Preview": True, "Allow Manual Device Test": False, "Open Dashboard": True,
                               "Display Size": "Auto",
                               "Operation Step Interval (s)": 1.0,
                               "Allow Scheduler Discord": False,"Detailed Logging":False,"Timing Logging":False,
                               "Template Mapping Logging":False}
        self.default_config.update(LOOP_DEFAULTS)
        self.config_description = {"Allow Manual Device Test": "允許 Dashboard 明確啟動真實 Android 模組、單 Chain 或選取節點測試。",
                                   "Start Live Preview": "啟動此任務後唯讀擷取目前選定的 Android 裝置一次，並以 COCO 分析。自動刷新預設關閉；不授權觸控輸入。",
                                   "Open Dashboard": "開啟僅供本機使用的驗證面板。"}
        self.config_description['Allow Scheduler Discord'] = '僅允許手動啟動的 OCR 流程通知節點；Dashboard 還需勾選 Discord。預設關閉。'
        self.config_description['Detailed Logging']='一般 INFO；勾選後 DEBUG 包含完整 ADB 指令、每次辨識結果及操作佇列交接耗時。'
        self.config_description['Timing Logging']='只增加節點／操作佇列耗時標記；不開啟完整 ADB 腳本或普通辨識 DEBUG。整晚量測建議勾選此項，關閉 Detailed Logging。'
        self.config_description['Template Mapping Logging']='逐一記錄 COCO 模板的螢幕映射倍率、模板尺寸、搜尋 ROI、門檻及最高信心值；偵錯時才開啟。'
        self.config_description['Display Size']='Auto 依 OK 擷取的 Android 原生畫面尺寸轉換；也可填 WIDTHxHEIGHT（如 1080x2340）作為預期尺寸，不符時停止輸入。模組仍維持 1080x2400 座標。'
        self.config_description['Operation Step Interval (s)']='每次點擊／滑動後至少等待的秒數，0～30；預設 1 秒。與 STEP 原有的操作後等待取較大值，不重複相加；等待可被停止或 Trigger 插隊中斷。'
        self.config_description.update(LOOP_DESCRIPTIONS)
        self.support_schedule_task = False
        self.default_config["Dashboard controls"] = ""
        self.config_type = {LOOP_SOURCE: {"type": "drop_down", "options": ["Canvas", "OK Override"]},
                            "Dashboard controls": {"type": "button", "buttons": [
            {"text": "開啟 Dashboard", "callback": self.open_dashboard},
            {"text": "關閉 Dashboard 並結束任務", "callback": self.request_dashboard_close}]}}
        self.bridge = None
        self.controller = None
        self.image_lock = threading.Lock()
        self.image_cache = {}
        self.visual_source = "預覽已停止；尚無手機畫面"
        self.observations = []
        self.device_metadata = {"selected": False}
        self.frame_metadata = {"frame_id": None, "source": "Synthetic", "stale": False, "recognition_valid": False}
        self.preview_device = None
        self.trigger_frames = {}
        self._trigger_local = threading.local()
        self._trigger_observer_token = object()
        self._ocr_lock = threading.RLock()
        self.template_images = {}
        self.template_stamps = {}
        self.preview_worker = None
        self.preview_job = None
        self._recognition_log_lock = threading.RLock()
        self._recognition_log_states = {}
        self._screen_backend = None
        self._screen_geometry = None
        self._screen_device_id = None
        self._screen_lock = threading.RLock()

    def info_set(self,key,value):
        previous=self.info.get(key,object())
        self.info[key]=value
        if previous!=value and key not in {'Log','Error'}:
            self.logger.info(f'info_set {key} {value}')

    def _failure_notifier(self):
        from src.rwtd.discord_notifications import DiscordNotifier
        return DiscordNotifier(self.root, native_sender=self._send_failure_notification)

    def _send_failure_notification(self, text, png):
        images = []
        if png is not None:
            frame = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError('Invalid saved failure image')
            images.append(frame)
        self.notification(text, title='RwtD 失敗診斷', error=True,
                          tray=True, images=images, screenshot=False)

    def on_exhaustion(self,event):
        reporter=getattr(self,'failure_evidence',None)
        if reporter is None:return
        event=dict(event)
        controller=getattr(self,'controller',None)
        request=getattr(controller,'trigger_work',None)
        # Only owner-thread operation evidence may inherit the running request;
        # another observer can report concurrently and carries its own task_id.
        if (not event.get('task_id') and request
                and threading.get_ident()==getattr(controller,'owner',None)):
            event['task_id']=request['task_id']
            event['trigger_interval']=request.get('definition',{}).get('trigger_interval')
            if event['trigger_interval'] is None:
                watcher=getattr(getattr(controller,'trigger_scheduler',None),'tasks',{}).get(request['task_id'],{})
                event['trigger_interval']=watcher.get('definition',{}).get('trigger_interval')
        owner=getattr(self,'_owner',None)
        from src.rwtd.android_core.failure_evidence import canvas_failure_context
        graph,context=canvas_failure_context(
            owner=owner, runtime=getattr(controller,'canvas_runtime',None),
            scheduler=getattr(controller,'trigger_scheduler',None),
            store=getattr(getattr(self,'manual',None),'decision_store',None),
            canvas_name=self.info.get('Canvas'))
        for key,value in context.items():
            if event.get(key) is None:event[key]=value
        event['notify_discord']=(event.get('notify_discord') is True
                                 and self.config.get('Allow Scheduler Discord',False) is True)
        if event.get('kind')=='chain':
            event['notify_discord']=event['notify_discord'] and self.config.get('Failure Notifications',True) is True
        from src.rwtd.android_core.failure_evidence import apply_node_notification_policy
        apply_node_notification_policy(event,graph)
        result=reporter.on_exhaustion(event)
        self.log_info('RWTD_FAILURE_EVIDENCE '+json.dumps(dict(event=event,result=result),ensure_ascii=False,default=str))
        return result

    def _remember_failure_frame(self,node,frame,device):
        reporter=getattr(self,'failure_evidence',None)
        if reporter is not None:
            try:
                reporter.remember(node.get('_entry_guard_for',node['id']),frame,device=device)
                rows=[]
                clauses=(node.get('entry_guard',{}).get('conditions',[node.get('entry_guard',{})])
                         if node.get('kind')=='entry_guard' else [node])
                for clause in clauses:
                    from src.rwtd.android_core.farm_round_variable import is_round_guard
                    if is_round_guard(clause):continue  # Runtime counter has no OCR/COCO ROI.
                    kind=clause.get('kind');roi=clause.get('roi');label=clause.get('feature') or clause.get('output_key') or node['id']
                    if clause.get('mode')=='ocr_cooldown' or clause.get('source')=='shared':
                        from src.rwtd.android_core.shared_ocr import PRESETS
                        key=clause['variable'];roi=self.manual.shared_ocr.node(key)['roi']
                        kind='ocr';label=PRESETS[key]['label']
                    if kind not in {'ocr','coco'}:continue
                    if kind=='ocr' and clause.get('ocr_region')=='coco':kind='coco';label=clause.get('ocr_feature',label)
                    effective=self.recognition_roi(roi,kind,progress=bool(node.get('_progress_for')) or clause.get('variable')=='ProgressKM')
                    rows.append({'kind':kind,'label':label,'roi':effective or [0,0,1080,2400]})
                reporter.annotate(node.get('_entry_guard_for',node['id']),frame,rows)
            except Exception:pass  # Optional evidence cannot change a recognition outcome.

    def _remember_failure_regions(self,node,frame,regions):
        reporter=getattr(self,'failure_evidence',None)
        if reporter is not None:
            try:reporter.annotate(node.get('_entry_guard_for',node['id']),frame,regions)
            except Exception:pass

    def configure_startup_preview(self):
        # Starting this task with the explicit read-only setting authorizes one
        # initial preview. The task thread consumes the queued activation and
        # capture; this helper never contacts ADB or enables manual input.
        enabled = self.config.get("Start Live Preview", True)
        if type(enabled) is not bool:
            raise ValueError("Start Live Preview must be boolean")
        device=getattr(self,'device_metadata',{})
        if enabled and (device.get('kind')!='adb' or device.get('connected') is not True):
            logger=getattr(self,'log_info',None)
            if callable(logger):logger('RWTD_OFFLINE_EDITOR device unavailable or non-ADB; preview remains stopped')
            return False
        if enabled:
            self.controller.enqueue("startup-preview-" + uuid.uuid4().hex, "mode",
                                    {"value": "Android Preview", "explicit_activation": True})
        return enabled

    def open_dashboard(self):
        # Official GUI callback: URL only, no device work and no new server.
        bridge = self.bridge
        if bridge and bridge.thread.is_alive():
            webbrowser.open(bridge.url)

    def request_dashboard_close(self):
        if self.controller and not self.controller.closed:
            try:
                self.controller.enqueue(uuid.uuid4().hex, "dashboard_close", {})
            except ValueError:
                self.controller.device_stop.set()

    def restore_simulation(self):
        # Keep the outstanding job bounded; its frame_id no longer matches below.
        with self.image_lock:
            self.image_cache["frame"] = self.image_cache["synthetic"]
            frame_id = uuid.uuid4().hex
            self.image_cache["frame:" + frame_id] = self.image_cache["synthetic"]
            for key in [key for key in self.image_cache if key.startswith("frame:")][:-3]:
                self.image_cache.pop(key, None)
            self.observations = []
            self.visual_source = "預覽已停止；尚無手機畫面"
            self.frame_metadata = {"frame_id": frame_id, "source": "Synthetic", "stale": False, "recognition_valid": False}
        self.preview_device = None

    def capture_preview(self, stop, payload=None):
        try:
            if self.preview_job is not None:
                return {'status':'BLOCKED','device_inputs':0,'failure_reason':'主頁辨識仍在背景處理；不累積重複工作'}
            payload = payload or {}
            if set(payload) - {'full_recognition'} or type(payload.get('full_recognition', False)) is not bool:
                raise ValueError('Invalid preview recognition scope')
            backend, device = self.manual_backend()
            if not device.get("selected") or device.get("connected") is not True or not device.get("device_identifier"):
                raise ValueError("Selected Android device is offline or unverified")
            if self.preview_device and device["device_identifier"] != self.preview_device:
                raise ValueError("Selected device changed; stop preview before explicitly reactivating Android preview/manual mode")
            frame = backend.get_frame()
            self.validate_native_frame(frame)
            _, after_device = self.manual_backend()
            if after_device.get("device_identifier") != device["device_identifier"] or after_device.get("connected") is not True:
                raise ValueError("Device changed during capture")
            if stop.is_set() or self.controller.capture_suppressed.is_set():
                return {"status": "BLOCKED", "device_inputs": 0, "failure_reason": "Capture cancelled before publication"}
            self.preview_device = device["device_identifier"]
            from src.rwtd.android_core.preview_features import referenced_features
            features = None if payload.get('full_recognition') else referenced_features(
                self.manual.decision_store.load(), self.manual.workflows)
            self.publish_frame("refresh", frame, features=features)
            if stop.is_set() or self.controller.capture_suppressed.is_set():
                return {"status": "BLOCKED", "device_inputs": 0, "failure_reason": "Capture publication cancelled"}
            if self.frame_metadata.get('recognition_pending') is True:
                return {'status':'READY','device_inputs':0,'frame_id':self.frame_metadata['frame_id'],
                        'recognition_pending':True}
            if self.frame_metadata.get("recognition_valid") is not True:
                return {"status": "FAILED", "stage": "recognition", "device_inputs": 0,
                        "frame_id": self.frame_metadata["frame_id"],
                        "failure_reason": self.frame_metadata.get("recognition_error") or "COCO recognition did not produce a valid result"}
            return {"status": "READY", "device_inputs": 0, "frame_id": self.frame_metadata["frame_id"]}
        except Exception as exc:
            with self.image_lock:
                self.frame_metadata.update(source="Cached" if self.frame_metadata.get("captured_utc") else "Error",
                                           stale=True, recognition_valid=False, matched_count=0, error=str(exc))
                self.observations = []  # Cached image is evidence, never fresh detections.
                self.visual_source = "ERROR / STALE: " + str(exc)
            # Keep local editing available after a read-only capture disconnect.
            # Explicit reactivation is required before more captures or input.
            self.controller.capture_suppressed.set()
            self.controller.device_stop.set()
            self.controller.module_pause.set()
            self.monitor.report(self.name, "ADB_PREVIEW_FAILURE", str(exc), notify=False, count_failure=False,
                                save_screenshot=False)
            return {"status": "FAILED", "stage": "capture", "device_inputs": 0, "failure_reason": str(exc)}

    _read_image = staticmethod(PreviewMatcher._read_image)
    _search_region = staticmethod(PreviewMatcher._search_region)

    def exit_is_set(self):
        return super().exit_is_set() or not self.enabled

    def dashboard_event(self, category, message):
        if self.controller:
            level='DEBUG' if category in {'PREPARATION','VALIDATION','INPUT','RETRY'} else 'INFO'
            self.controller._log(category, message,level=level)

    @staticmethod
    def validate_native_frame(frame):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.shape != (2400, 1080, 3):
            raise ValueError("Require normalized 1080x2400 BGR reference frame")

    @staticmethod
    def write_raw_png(path, frame):
        ok, encoded = cv2.imencode(".png", frame)
        if not ok:
            raise ValueError("PNG encoding failed")
        path.write_bytes(encoded.tobytes())

    def publish_action_frame(self, which, frame):
        # Action evidence must not run the unrelated full preview catalog.
        # Leave the last analyzed frame and its matching frame_id intact.
        ok, png = cv2.imencode('.png', frame)
        if not ok:
            raise ValueError('PNG encoding failed')
        with self.image_lock:
            self.image_cache[which] = png.tobytes()

    def publish_frame(self, which, frame, features=None):
        ok, png = cv2.imencode(".png", frame)
        if not ok:
            raise ValueError("PNG encoding failed")
        observations, recognition_error = [], None
        captured_utc = datetime.now(timezone.utc).isoformat()
        frame_id = uuid.uuid4().hex
        # Publish new pixels immediately and clear old boxes before recognition.
        if which in {'before', 'after', 'refresh'}:
            with self.image_lock:
                self.image_cache['frame'] = png.tobytes()
                self.image_cache['frame:' + frame_id] = png.tobytes()
                for key in [key for key in self.image_cache if key.startswith('frame:')][:-3]:
                    self.image_cache.pop(key,None)
                self.observations = []
                self.frame_metadata = {'frame_id':frame_id, 'source':'Real Capture',
                    'stale':False, 'captured_utc':captured_utc, 'recognition_valid':False,
                    'recognition_pending':True, 'recognition_scope':'full' if features is None else 'canvas'}
        if which == 'refresh' and self.preview_worker is not None:
            self.preview_job = (frame_id, self.preview_worker.submit(frame,features))
            self.monitor.accept_frame(self.name,frame,device={'frame_source':'official_adb'})
            return
        if which in {"before", "after", "refresh"}:
            try:
                from src.rwtd.android_core.feature_catalog import feature_names
                observations = self.detect_manual_features(frame, feature_names(self.root) if features is None else features, .5)
                for item in observations:
                    item['matched'] = bool(item.get('matched') and item.get('confidence', 0) >= .8)
            except Exception as exc:
                recognition_error = str(exc)
        with self.image_lock:
            self.image_cache[which] = png.tobytes()
            if which in {"before", "after", "refresh"} and not self.controller.capture_suppressed.is_set():
                self.image_cache["frame"] = png.tobytes()
                self.image_cache["frame:" + frame_id] = png.tobytes()
                for key in [key for key in self.image_cache if key.startswith("frame:")][:-3]:
                    self.image_cache.pop(key, None)
                self.observations = observations
                self.frame_metadata = {"frame_id": frame_id, "source": "Real Capture", "stale": False,
                    "captured_utc": captured_utc, "recognized_utc": datetime.now(timezone.utc).isoformat(),
                    "recognition_valid": recognition_error is None, "recognition_error": recognition_error,
                    "recognition_pending":False, "recognition_scope":"full" if features is None else "canvas",
                    "threshold": .8, "matched_count": sum(bool(item.get("matched")) for item in observations)}
                self.visual_source = "REAL capture" + ("; recognition ERROR: " + recognition_error if recognition_error else "")
                self.controller._log("RECOGNITION_FAILURE" if recognition_error else "RECOGNITION", self.visual_source,
                                     level='INFO' if recognition_error else 'DEBUG',frame_id=frame_id)
                if which == "before":
                    self.image_cache.pop("after", None)
        self.monitor.accept_frame(self.name, frame, device={"frame_source": "official_adb" if which != "synthetic" else "synthetic"})

    def finish_preview_recognition(self):
        job = self.preview_job
        if job is None or not job[1].done():
            return
        self.preview_job = None
        observations, error = [], None
        try:
            observations = job[1].result()
        except Exception as exc:
            error = type(exc).__name__+': '+str(exc)[:600]
        with self.image_lock:
            if (observations is None or self.frame_metadata.get('frame_id') != job[0]
                    or self.controller.device_stop.is_set() or self.controller.capture_suppressed.is_set()):
                return
            for item in observations:
                item['matched'] = bool(item.get('matched') and (item.get('confidence') or 0) >= .8)
            self.observations = observations
            self.frame_metadata.update(recognition_pending=False, recognition_valid=error is None,
                recognition_error=error,recognized_utc=datetime.now(timezone.utc).isoformat(),
                threshold=.8,matched_count=sum(bool(item.get('matched')) for item in observations))
        self.controller._log('RECOGNITION_FAILURE' if error else 'RECOGNITION',
                             '主頁獨立辨識完成' if error is None else '主頁辨識失敗：'+error,
                             level='INFO' if error else 'DEBUG',frame_id=job[0])

    def manual_backend(self):
        physical = self.executor.method
        if not self.is_adb() or not isinstance(physical, ADBCaptureMethod) or not isinstance(self.executor.interaction, ADBInteraction):
            raise ValueError("Select official ADB capture and ADBInteraction; Windows backends are forbidden")
        for task in self.get_tasks():
            if task is not self and getattr(task, "running", False):
                raise ValueError("Another OK-Script task is running")
        device = device_snapshot(self)
        if not getattr(self._trigger_local,'observing',False):
            self.device_metadata = device
        with self._screen_lock:
            identifier = device.get('device_identifier')
            if (self._screen_backend is None or self._screen_backend.backend is not physical
                    or self._screen_device_id != identifier):
                self._screen_backend = _ReferenceCapture(self, physical)
                self._screen_geometry = None
                self._screen_device_id = identifier
            backend = self._screen_backend
        return backend, dict(device)

    def normalize_device_frame(self, frame):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError('Require a physical BGR Android frame')
        height, width = frame.shape[:2]
        expected = expected_size(self.config.get('Display Size', 'Auto'))
        if expected is not None and expected != (width, height):
            raise ValueError(f'Display Size {expected[0]}x{expected[1]} differs from Android frame {width}x{height}')
        geometry = ScreenGeometry(width, height)
        with self._screen_lock:
            self._screen_geometry = geometry
        self.info_set('Display', f'{width}x{height} → 1080x2400')
        return geometry.normalize(frame)

    def screen_geometry(self):
        with self._screen_lock:
            geometry = self._screen_geometry
        if geometry is None:
            # Fixed-coordinate modules can start without a recognition frame.
            # Capture once before sending their first input to bind dimensions.
            backend, _ = self.manual_backend()
            self.validate_native_frame(backend.get_frame())
            with self._screen_lock:
                geometry = self._screen_geometry
        return geometry

    def _preview_geometry(self):
        with self._screen_lock:
            return self._screen_geometry

    def _log_template_mapping(self, **details):
        if self.config.get('Template Mapping Logging', False) is True:
            self.log_info('RWTD_TEMPLATE_MAP '+json.dumps(details, ensure_ascii=False, allow_nan=False))

    def screen_point(self, x, y):
        return self.screen_geometry().point(x, y)

    def screen_step(self, step):
        return self.screen_geometry().step(step)

    def recognition_roi(self, roi, kind, *, progress=False):
        """Expose the same effective reference ROI for execution and evidence."""
        from src.rwtd.android_core.roi_policy import effective_roi, new_resolution
        return effective_roi(roi, kind, enabled=new_resolution(self._preview_geometry()), progress=progress)

    def ocr(self, *args, **kwargs):
        # All owned OCR call sites pass box by keyword. Do not expand upstream:
        # direct/Guard/Step/verification OCR share this single final boundary.
        progress = kwargs.pop('_rwtd_progress', False)
        context=kwargs.pop('_rwtd_context',None)
        numeric_node=kwargs.pop('_rwtd_numeric_node',None)
        box = kwargs.get('box')
        configured=None
        if box is not None:
            configured = [box.x, box.y, box.width, box.height]
            roi = self.recognition_roi(configured, 'ocr', progress=progress)
            kwargs['box'] = Box(*roi)
        # The native OCR engine is shared. Serialize inference, never captures
        # or input, without modifying OK internals or its model configuration.
        with self._ocr_lock:
            from src.rwtd.android_core.ocr_records import capture
            returned=capture(self,super().ocr,args,kwargs,configured_roi=configured,context=context)
            from src.rwtd.android_core.numeric_ocr_fallback import eligible,recognize
            if returned==[] and eligible(numeric_node):
                return recognize(self,kwargs.get('frame'),configured,numeric_node,
                                 kwargs.get('threshold') or self.ocr_default_threshold,context,Box)
            return returned

    def prepare_three_point_swipe(self):
        # Called on the existing task thread, only for an explicit manual action.
        backend, device = self.manual_backend()
        interaction = self.executor.interaction
        from src.rwtd.android_core.optional_u2 import enable
        enable(self.root)
        # The official u2 property uses the selected DeviceManager device.
        # Do not change its cached use_u2 flag or any framework source.
        u2 = interaction.u2
        touch = u2.touch
        if not all(callable(getattr(touch, name, None)) for name in ("down", "move", "up")):
            raise ValueError("Official continuous touch API unavailable")
        from src.rwtd.android_core.three_point_swipe import perform
        def execute(points, duration_ms):
            current_backend, current_device = self.manual_backend()
            if current_backend is not backend or current_device != device or self.executor.interaction is not interaction:
                raise ValueError("Selected official device changed before continuous gesture")
            return perform(touch, [self.screen_point(*point) for point in points], duration_ms,
                           bounds=(self.screen_geometry().width, self.screen_geometry().height))
        return execute

    def detect_manual_features(self, frame, features, threshold, coco_roi=None):
        local = self._trigger_local
        if getattr(local,'observing',False):
            if not getattr(local,'matcher',None):
                from src.rwtd.android_core.preview_worker import PreviewWorker
                local.matcher = PreviewWorker(self.root,PreviewMatcher._observe,self._read_image,self._search_region,
                                              self._preview_geometry,self._log_template_mapping)
            return local.matcher.analyze(frame,set(features),threshold,coco_roi)
        from src.rwtd.android_core.feature_catalog import annotation_sources
        observations = []
        # Cache reference images only, never live frames or detection results.
        for path in list(self.template_images):
            stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.is_file() else None
            if stamp != self.template_stamps.get(path):
                self.template_images.pop(path, None)
                self.template_stamps.pop(path, None)
        for path, coco, available in annotation_sources(self.root):
            requested = set(features) & available
            if requested:
                observations.extend(PreviewMatcher._observe(self, self.root, frame, coco, requested, threshold,
                    "Full frame", 6, self.template_images, coco_roi=coco_roi, template_root=path.parent,
                    multi_match=True, screen_geometry=self._preview_geometry(),
                    mapping_logger=self._log_template_mapping if self.config.get('Template Mapping Logging',False) else None))
        for path in self.template_images:
            stat = path.stat()
            self.template_stamps[path] = (stat.st_mtime_ns, stat.st_size)
        if not set(features).issubset({item["feature"] for item in observations}):
            raise ValueError("Requested COCO feature has no annotation")
        if any(item.get("status") != "SNAPSHOT_ONLY_NOT_EXECUTION_TARGET" for item in observations):
            from src.rwtd.android_core.recognition import incomplete_reason
            raise ValueError(incomplete_reason(observations))
        observations = consolidate(observations)
        return observations

    def verification_observation(self, frame, parameters):
        features = {parameters.get("verify_feature"), parameters.get("previous_feature")} - {None, ""}
        detections = self.detect_manual_features(frame, features, parameters["threshold"]) if features else []
        result = {"features": [item["feature"] for item in detections if item.get("matched")], "detections": detections}
        if parameters.get("verification_policy", "").startswith("ocr_"):
            x, y, w, h = parameters["ocr_roi"]
            boxes = self.ocr(box=Box(x, y, w, h), frame=frame)
            texts = [str(item.text).strip() for item in boxes]
            result["ocr_text"] = texts
            values = [float(text.replace(",", "")) for text in texts if re.fullmatch(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", text)]
            result["ocr_value"] = values[0] if len(values) == 1 else None
        return result

    def observe_trigger(self, payload, stop):
        """Read-only worker entry; no input/notification dispatch grant."""
        if payload.get('node',{}).get('kind') not in {'coco','ocr','ocr_decision','entry_guard','flow_frame'} or not payload.get('task_id'):
            raise ValueError('Background Trigger permits observation only')
        self._trigger_local.observing = True
        try:
            return self.canvas_effect(payload,stop,_observer_token=self._trigger_observer_token)
        finally:
            self._trigger_local.observing = False

    def release_trigger_observer(self):
        local = self._trigger_local
        if getattr(local,'matcher',None):
            local.matcher.close()
            local.matcher = None
        local.frames = {}

    def canvas_effect(self, payload, stop, _observer_token=None):
        """Existing owner task thread dispatch; TriggerTask effects are read-only."""
        import json
        controller = self.controller
        observer = _observer_token is self._trigger_observer_token
        if observer and (not payload.get('task_id') or payload['node']['kind'] not in {'coco','ocr','ocr_decision','entry_guard','flow_frame'}):
            raise ValueError('Observer has no input or notification permission')
        if (not controller or (not observer and threading.get_ident() != controller.owner)
                or payload.get('source') not in {'android_readonly','android_manual'}
                or controller.mode not in {'Android Preview','Manual Device Test'}
                or stop.is_set() or controller.device_stop.is_set()):
            raise ValueError('Read-only Scheduler permission unavailable')
        node = payload['node']
        if payload.get('task_id'):
            with controller.lock, controller.trigger_scheduler.lock:
                watcher = controller.trigger_scheduler.tasks.get(payload['task_id'])
                if (not watcher or watcher['status'] != 'RUNNING' or watcher['pending'] != node
                        or watcher['generation'] != payload.get('generation')
                        or payload.get('run_id') != controller.trigger_scheduler.run_id
                        or payload.get('round_id') != watcher['rounds']
                        or payload['source'] != controller.trigger_scheduler.source
                        or node['kind'] not in {'ocr','coco','ocr_decision','notify','entry_guard','flow_frame'}):
                    raise ValueError('TriggerTask has no active read-only effect')
                if observer:
                    self._trigger_local.graph = watcher['definition']
        if node['kind'] == 'ocr_decision':
            from src.rwtd.android_core.canvas_ocr_decision import probe
            node = probe(node)
            payload = dict(payload, fresh_frame=True)
        if node['kind'] == 'action':
            return self.manual.execute_canvas_action(node,stop)
        backend, device = self.manual_backend()
        if not device.get('selected') or device.get('connected') is not True:
            raise ValueError('Selected device unavailable')
        if node['kind']=='flow_frame':
            recovery=controller.trigger_scheduler.recovery
            if not recovery.event or recovery.event['id']!=payload.get('recovery_event'):
                return {'valid':False,'reason':'Stale Solver event'}
            try:
                if node.get('phase')=='baseline':
                    frame,frame_id=recovery.shared_frame(backend,device,self.validate_native_frame)
                else:
                    if recovery.device!=device:
                        return {'valid':False,'reason':'Solver baseline device changed'}
                    frame=backend.get_frame()
                    self.validate_native_frame(frame)
                    import uuid
                    frame_id=uuid.uuid4().hex
                if stop.is_set() or controller.device_stop.is_set():
                    return {'valid':False,'reason':'Cancelled Solver frame'}
                if not recovery.event or recovery.event['id']!=payload.get('recovery_event'):
                    return {'valid':False,'reason':'Stale Solver event'}
                _,after_device=self.manual_backend()
                if after_device!=device:return {'valid':False,'reason':'Solver device changed'}
                return recovery.describe(frame,frame_id)
            except Exception as exc:
                return {'valid':False,'reason':type(exc).__name__+': '+str(exc)[:160]}
        if node['kind']=='entry_guard':
            from src.rwtd.android_core.canvas_entry_guard import evaluate,coco_allowed,ocr_allowed
            import numpy as np
            try:
                frame,frame_id=self.trigger_frame(backend,device,dict(payload,fresh_frame=True))
                captured_at=time.monotonic()
                remember=getattr(self,'_remember_failure_frame',None)
                if callable(remember):remember(node,frame,device)
                self.validate_native_frame(frame)
                if isinstance(frame,np.ndarray) and float(np.std(frame[::8,::8])) < 1:
                    raise ValueError('Uniform/black frame cannot establish valid recognition')
            except Exception as exc:
                reason=type(exc).__name__+': '+str(exc)[:160]
                from src.rwtd.android_core.log_policy import recognition as log_recognition
                from src.rwtd.android_core.canvas_entry_guard import clauses
                kind='OCR_GUARD' if any(c['kind']=='ocr' for c in clauses(node['entry_guard'])) else 'COCO_GUARD'
                message=(f"{kind} guard_node={node.get('_entry_guard_for',node['id'])} result=UNKNOWN "
                         f"confidence=N/A trigger={payload.get('task_id') or '-'} reason={reason}")
                log_recognition(self,controller,kind,message,
                            (payload.get('task_id'),node['id'],'capture'),'UNKNOWN',node=node['id'])
                return {'verified':False,'detected':None,'reason':reason}
            recognition=[]
            def observe(clause):
                if stop.is_set() or controller.device_stop.is_set():
                    return None
                from src.rwtd.android_core.shared_ocr import PRESETS
                if clause.get('source')=='shared' or clause.get('mode')=='ocr_cooldown':
                    key=clause['variable']
                    def tokens(probe):
                        x,y,w,h=probe['roi']
                        boxes=self.ocr(box=Box(x,y,w,h),frame=frame,threshold=probe['threshold'],
                                       _rwtd_progress=key=='ProgressKM',
                                       _rwtd_numeric_node=probe,
                                       _rwtd_context={'route':'shared_guard','node':node['id'],'frame_id':frame_id})
                        from src.rwtd.android_core.ocr_records import box_tokens
                        return box_tokens(boxes)
                    series=(self.manual.time_guards.namespace,payload.get('task_id'),node['id'])
                    sample=self.manual.shared_ocr.read(key,frame_id,captured_at,tokens,series=series,
                        attempt=node.get('_guard_attempt',1),limit=node.get('_guard_attempt_limit',1))
                    if clause.get('mode')=='ocr_cooldown':
                        from src.rwtd.android_core.shared_ocr import expedition_cooldown_sample
                        def recognize_completion(check):
                            return self.detect_manual_features(frame,[check['feature']],
                                check['threshold'],coco_roi=check.get('roi'))
                        sample=expedition_cooldown_sample(sample,recognize_completion,
                            node.get('_expedition_completion_clause'))
                        allowed=self.manual.time_guards.prepare(node['id'],clause,sample)
                    else:
                        from src.rwtd.android_core.canvas_entry_guard import variable_result
                        allowed=variable_result(clause,{key:sample})
                    recognition.append(dict(sample,kind='ocr',text=PRESETS[key]['label'],
                        threshold=PRESETS[key]['threshold'],match=clause.get('op','cooldown'),
                        detected=allowed,configured_roi=sample.get('roi'),
                        roi=self.recognition_roi(sample.get('roi'),'ocr',progress=key=='ProgressKM'),reason=sample.get('reason')))
                    return allowed
                if clause['kind']=='ocr':
                    try:
                        x,y,w,h=clause['roi']
                        boxes=self.ocr(box=Box(x,y,w,h),frame=frame,threshold=clause['threshold'],
                            _rwtd_context={'route':'direct_ocr_guard','node':node['id'],'frame_id':frame_id})
                        from src.rwtd.android_core.ocr_records import box_tokens
                        tokens=box_tokens(boxes)
                        allowed=ocr_allowed(tokens,clause)
                    except Exception as exc:
                        tokens=[]
                        allowed=None
                        reason=type(exc).__name__+': '+str(exc)[:160]
                    else:
                        reason=None
                    recognition.append({'kind':'ocr','text':clause['text'],'threshold':clause['threshold'],
                                        'roi':self.recognition_roi(clause['roi'],'ocr'),
                                        'configured_roi':clause['roi'],'match':clause['match'],'detected':allowed,
                                        'tokens':tokens[:10],'reason':reason})
                    return allowed
                matches=self.detect_manual_features(frame,[clause['feature']],clause.get('threshold',.8),coco_roi=clause.get('roi'))
                allowed=coco_allowed(matches,clause)
                scores=[h['confidence'] for h in matches if h.get('feature')==clause['feature']
                        and type(h.get('confidence')) in (int,float) and math.isfinite(h['confidence'])]
                effective_roi = next(
                    (h.get('search_bbox') for h in matches if h.get('feature') == clause['feature']),
                    self.recognition_roi(clause.get('roi'),'coco'))
                recognition.append({'feature':clause['feature'],'confidence':max(scores,default=None),
                                    'best_confidence':max((h.get('best_confidence') for h in matches
                                        if h.get('feature')==clause['feature'] and
                                        type(h.get('best_confidence')) in (int,float) and
                                        math.isfinite(h['best_confidence'])),default=max(scores,default=None)),
                                    'threshold':clause.get('threshold',.8),'roi':effective_roi,
                                    'match':clause.get('match','detected'),'detected':allowed,
                                    'confidence_reason':None if scores else 'matcher_returned_no_scored_candidate'})
                return allowed
            guard_trace=[]
            from src.rwtd.android_core.canvas_entry_guard import clauses as guard_clauses
            guard_rows=guard_clauses(node['entry_guard'])
            def traced_observe(clause):
                start=len(recognition)
                value=observe(clause)
                for evidence in recognition[start:]:
                    evidence['clause_index']=next(i for i,c in enumerate(guard_rows) if c is clause)
                return value
            allowed=evaluate(node['entry_guard'],payload.get('variables',{}),traced_observe,
                lambda c:self.manual.time_guards.allowed(node['id'],c),trace=guard_trace)
            getattr(self,'_remember_failure_regions',lambda *args:None)(node,frame,[{'kind':r.get('kind','coco'),
                'label':r.get('text') or r.get('feature') or node['id'],
                'roi':r.get('roi') or [0,0,1080,2400]} for r in recognition])
            current_backend,current_device=self.manual_backend()
            if (stop.is_set() or controller.device_stop.is_set() or current_device!=device or current_backend is not backend):
                return {'verified':False}
            for evidence in recognition:
                if evidence.get('kind')=='ocr':
                    route='YES' if evidence['detected'] is True else 'NO' if evidence['detected'] is False else 'UNKNOWN'
                    from src.rwtd.android_core.ocr_records import publish_result
                    evidence['ocr_record']=publish_result(self,evidence.get('tokens'),frame,
                        roi=evidence.get('roi'),configured_roi=evidence.get('configured_roi'),
                        threshold=evidence['threshold'],route='attached_guard',result=route,
                        value=evidence.get('value'),reason=evidence.get('reason'),node=node['id'],frame_id=frame_id)
                    message=(f"OCR guard_node={node.get('_entry_guard_for',node['id'])} "
                             f"text={evidence['text']} result={route} threshold={evidence['threshold']} "
                             f"match={evidence['match']} roi={evidence['roi']} frame={frame_id} "
                             f"trigger={payload.get('task_id') or '-'}")
                    if 'value' in evidence:message+=' value='+str(evidence['value'])
                    if evidence['reason']:
                        message+=' reason='+evidence['reason']
                    from src.rwtd.android_core.log_policy import recognition as log_recognition
                    log_recognition(self,controller,'OCR_GUARD',message,
                        (payload.get('task_id'),node['id'],evidence['text']),route,
                        node=node.get('_entry_guard_for',node['id']),recognition=evidence)
                    continue
                score=f"{evidence['confidence']:.4f}" if evidence['confidence'] is not None else 'N/A'
                route='YES' if evidence['detected'] is True else 'NO' if evidence['detected'] is False else 'UNKNOWN'
                message=(f"COCO guard_node={node.get('_entry_guard_for',node['id'])} "
                         f"feature={evidence['feature']} result={route} "
                         f"confidence={score} threshold={evidence['threshold']} "
                         f"match={evidence['match']} roi={evidence['roi'] or 'full'} "
                         f"frame={frame_id} trigger={payload.get('task_id') or '-'}")
                if evidence['confidence_reason']:
                    message+=' confidence_reason='+evidence['confidence_reason']
                from src.rwtd.android_core.log_policy import recognition as log_recognition
                log_recognition(self,controller,'COCO_GUARD',message,
                            (payload.get('task_id'),node['id'],evidence['feature']),route,
                            node=node.get('_entry_guard_for',node['id']),recognition=evidence)
            return {'verified':allowed is not None,'detected':allowed,'frame_id':frame_id,
                    'source':'REAL_ANDROID','recognition':recognition,'guard_trace':guard_trace}
        if node['kind'] == 'exclusion':
            frame, frame_id = self.trigger_frame(backend,device,payload)
            self.validate_native_frame(frame)
            matches = self.detect_manual_features(frame,[node['feature']],node.get('threshold',.8))
            _, current_device = self.manual_backend()
            if stop.is_set() or current_device != device:
                return {'verified':False}
            hits = [h for h in matches if h.get('feature') == node['feature'] and h.get('matched')
                    and h.get('confidence',0) >= node.get('threshold',.8)]
            unique = {tuple(h.get('bbox') or []):h for h in hits}
            if len(unique) != 1:
                return {'verified':True,'detected':False,'frame_id':frame_id,'source':'REAL_ANDROID'}
            hit = next(iter(unique.values()))
            return {'verified':True,'detected':True,'frame_id':frame_id,'source':'REAL_ANDROID',
                    'confidence':hit['confidence'],'bbox':hit['bbox']}
        if node['kind'] == 'coco':
            frame_id = None
            matches = []
            try:
                frame, frame_id = self.trigger_frame(backend,device,payload)
                remember=getattr(self,'_remember_failure_frame',None)
                if callable(remember):remember(node,frame,device)
                self.validate_native_frame(frame)
                if stop.is_set():
                    raise ValueError('Capture cancelled')
                matches = self.detect_manual_features(frame,[node['feature']],node.get('threshold',.8),coco_roi=node.get('roi'))
                _, current_device = self.manual_backend()
                if stop.is_set() or current_device != device:
                    raise ValueError('Capture cancelled or selected device changed')
                hits = [item for item in matches if item.get('feature') == node['feature'] and item.get('matched')
                        and type(item.get('confidence')) in (int,float) and math.isfinite(item['confidence'])
                        and item['confidence'] >= node.get('threshold',.8)]
                detected = bool(hits)
                scores=[h['confidence'] for h in matches if h.get('feature')==node['feature']
                        and type(h.get('confidence')) in (int,float) and math.isfinite(h['confidence'])]
                result = {'verified':True,'frame_id':frame_id,'source':'REAL_ANDROID',
                          'detected':detected if node.get('match','detected') == 'detected' else not detected,
                          'target_detected':detected,'confidence':max(scores,default=None),
                          'confidence_reason':None if scores else 'matcher_returned_no_scored_candidate',
                          'match_count':len(hits),
                          'detections':[{'confidence':h['confidence'],'bbox':h.get('bbox')} for h in hits[:16]]}
            except Exception as exc:
                result = {'verified':False,'frame_id':frame_id,'source':'REAL_ANDROID',
                          'confidence':None,'reason':type(exc).__name__+': '+str(exc)[:160]}
            result.update(feature=node['feature'],threshold=node.get('threshold',.8),
                          configured_roi=node.get('roi'),
                          roi=next((hit.get('search_bbox') for hit in matches if hit.get('feature')==node['feature']),
                                   self.recognition_roi(node.get('roi'),'coco')),
                          match=node.get('match','detected'))
            if frame_id is not None:
                getattr(self,'_remember_failure_regions',lambda *args:None)(node,frame,[{'kind':'coco','label':node['feature'],
                    'roi':result.get('roi') or [0,0,1080,2400]}])
            route = 'YES' if result.get('detected') is True else 'NO' if result.get('verified') is True else 'UNKNOWN'
            score = f"{result['confidence']:.4f}" if result['confidence'] is not None else 'N/A'
            message = (f"COCO node={node['id']} feature={node['feature']} result={route} "
                       f"confidence={score} threshold={result['threshold']} matches={result.get('match_count',0)} "
                       f"match={result['match']} roi={result['roi'] or 'full'} frame={frame_id or 'N/A'} "
                       f"trigger={payload.get('task_id') or '-'}")
            if result.get('reason'):
                message += ' reason='+result['reason']
            if result.get('confidence_reason'):
                message += ' confidence_reason='+result['confidence_reason']
            with controller.lock, controller.trigger_scheduler.lock:
                if observer and (stop.is_set() or not self.trigger_observation_current(payload)):
                    return {'verified':False}
                from src.rwtd.android_core.log_policy import recognition as log_recognition
                log_recognition(self,controller,'COCO_CHECK',message,
                            (payload.get('task_id'),node['id'],node['feature']),route,
                            node=node['id'],recognition=result)
            return result
        if node['kind'] == 'ocr':
            # Evidence uses the same frame as OCR; no additional device call.
            from src.rwtd.android_core.canvas_ocr import validate_ocr_node
            validate_ocr_node(node)
            frame, frame_id = self.trigger_frame(backend,device,dict(payload,
                fresh_frame=bool(node.get('_progress_for') or payload.get('fresh_frame')
                                 or payload.get('ocr_attempt',1)>1)))
            remember=getattr(self,'_remember_failure_frame',None)
            if callable(remember):remember(node,frame,device)
            self.validate_native_frame(frame)
            if stop.is_set():
                return {'verified':False}
            precondition = None
            pre_id = node.get('ocr_retry',{}).get('precondition')
            if pre_id:
                graph = self._trigger_local.graph if observer else controller.trigger_scheduler.tasks[payload['task_id']]['definition'] if payload.get('task_id') else controller.canvas_runtime.graph
                pre = next(item for item in graph['nodes'] if item['id']==pre_id and item['kind']=='coco')
                matches = self.detect_manual_features(frame,[pre['feature']],pre.get('threshold',.8),coco_roi=pre.get('roi'))
                detected = any(item.get('feature')==pre['feature'] and item.get('matched') and item.get('confidence',0)>=pre.get('threshold',.8) for item in matches)
                precondition = {'verified':True,'frame_id':frame_id,'detected':not detected if pre.get('match')=='disappeared' else detected}
                _, current_device = self.manual_backend()
                if stop.is_set() or current_device != device:
                    return {'verified':False}
                if not precondition['detected']:
                    return {'verified':True,'frame_id':frame_id,'tokens':[],'precondition':precondition,'source':'REAL_ANDROID'}
            from src.rwtd.android_core.canvas_ocr import reading_regions
            detections = self.detect_manual_features(frame, [node['ocr_feature']], node.get('ocr_coco_threshold', .8), coco_roi=node['roi']) if node.get('ocr_region') == 'coco' else []
            region_node = node
            if node.get('ocr_region') == 'coco':
                # Do not clip a successfully located target back to its smaller
                # configured search window before reading the target's text.
                region_node = dict(node, roi=self.recognition_roi(node['roi'],'coco'))
            regions = reading_regions(region_node, detections)
            getattr(self,'_remember_failure_regions',lambda *args:None)(node,frame,[{'kind':'ocr','label':node['output_key'],
                'roi':self.recognition_roi(region,'ocr',progress=bool(node.get('_progress_for')))} for region in regions])
            boxes = []
            for x, y, w, h in regions:
                if stop.is_set():
                    return {'verified':False}
                boxes.extend(self.ocr(box=Box(x,y,w,h), frame=frame, threshold=node.get('threshold', .8),
                                      _rwtd_progress=bool(node.get('_progress_for')),
                                      _rwtd_numeric_node=node,
                                      _rwtd_context={'route':'canvas_ocr','node':node['id'],'frame_id':frame_id}))
            _, current_device = self.manual_backend()
            if stop.is_set() or current_device != device:
                return {'verified':False}
            from src.rwtd.android_core.ocr_records import box_tokens
            result={'verified':True, 'frame_id':frame_id, 'source':'REAL_ANDROID','precondition':precondition,
                    'configured_regions':regions,
                    'regions':[self.recognition_roi(region,'ocr',progress=bool(node.get('_progress_for'))) for region in regions],
                    'tokens':box_tokens(boxes)}
            from src.rwtd.android_core.canvas_ocr import classify,output_type
            import json
            classified=classify(node,result)
            from src.rwtd.android_core.ocr_records import publish
            result['ocr_record']=classified['ocr_record']
            publish(self,result['ocr_record'],frame)
            if node.get('_progress_for'):
                self.manual.shared_ocr.accept('ProgressKM',classified,time.monotonic())
            detail={'node':node['id'],'variable':node['output_key'],'output_type':output_type(node),
                    'state':classified['state'],'value':classified['value'],'reason':classified['reason'],
                    'threshold':node.get('threshold',.8),'regions':result['regions'],
                    'configured_regions':regions,'frame':frame_id,
                    'trigger':payload.get('task_id'),
                    'tokens':[dict(text=t['text'][:160],confidence=t['confidence']) for t in result['tokens'][:10]],
                    'token_count':len(result['tokens'])}
            message='OCR_READ '+json.dumps(detail,ensure_ascii=False)
            logger=getattr(self,'log_debug',None)
            if callable(logger):logger(message)
            sink=getattr(controller,'_log',None)
            if callable(sink):sink('OCR_READ',message,level='DEBUG',node=node['id'],recognition=detail)
            return result
        if node['kind'] != 'notify':
            raise ValueError('Unsupported read-only Scheduler effect')
        sample = payload.get('variables', {}).get(node.get('variable'))
        if node.get('variable') and (not sample or sample.get('verified') is not True):
            raise ValueError('Notification variable UNKNOWN')
        enabled = payload.get('notify_discord') is True
        if (enabled and self.config.get('Allow Scheduler Discord', False) is not True) or stop.is_set():
            raise ValueError('Discord permission unavailable')
        from src.rwtd.android_core.canvas_notifications import notify
        return notify(self.root, node, payload, device, enabled=enabled,
                      native_sender=lambda text: self.notification(
                          text, title='RwtD Dashboard', tray=True, screenshot=False))

    def trigger_frame(self, backend, device, payload):
        """Fresh per observer round; COCO and OCR in that round share frame identity."""
        from src.rwtd.android_core.canvas_triggers import round_frame
        cache = self.trigger_frames
        if getattr(self._trigger_local,'observing',False):
            if not hasattr(self._trigger_local,'frames'):
                self._trigger_local.frames = {}
            cache = self._trigger_local.frames
        with self.controller.lock, self.controller.trigger_scheduler.lock:
            scheduler=self.controller.trigger_scheduler
            recovery=scheduler.recovery
            shared=bool(payload.get('recovery_event') and recovery.event
                        and payload['recovery_event']==recovery.event['id']
                        and recovery.event['selected'] is None)
            tasks = {key:{name:item.get(name) for name in ('status','cursor','generation','rounds')}
                     for key,item in self.controller.trigger_scheduler.tasks.items()}
        if shared:
            frame,frame_id=recovery.shared_frame(backend,device,self.validate_native_frame)
        else:
            frame, frame_id = round_frame(cache,backend,device,payload,tasks,self.validate_native_frame)
        if getattr(self._trigger_local,'observing',False):
            frame = frame.copy()
            frame.flags.writeable = False
        return frame,frame_id

    def trigger_observation_current(self, payload):
        scheduler = self.controller.trigger_scheduler
        watcher = scheduler.tasks.get(payload['task_id'])
        return (watcher and watcher['status']=='RUNNING'
                and watcher['generation']==payload['generation']
                and scheduler.run_id==payload['run_id']
                and watcher['rounds']==payload['round_id']
                and watcher['pending']==payload['node'])

    def report_manual_failure(self, record):
        self.monitor.report(self.name, "GAMEPLAY_TASK_FAILURE", record["failure_reason"], notify=False,
                            save_screenshot=False,error_identifier=record["module"],
                            error_details={"parameters": record["parameters"], "status": record["status"]})

    def show_history_evidence(self, record, storage):
        # Load only fixed session image names; never trust arbitrary paths from JSON.
        with self.image_lock:
            for side in ("before", "after"):
                self.image_cache.pop(side, None)
                path = storage.path("session_" + record["report_id"] + "/" + side + ".png")
                if path.is_file() and not record.get("screenshots_cleaned"):
                    frame = self._read_image(path)
                    self.validate_native_frame(frame)
                    okay, encoded = cv2.imencode(".png", frame)
                    if okay:
                        self.image_cache[side] = encoded.tobytes()

    def run(self):
        from src.rwtd.android_core.log_policy import LogSession
        manager=getattr(getattr(self,'executor',None),'device_manager',None)
        with LogSession(self.config.get('Detailed Logging',False),manager) as session:
            self._log_session=session
            workspace=getattr(self,'_native_workspace',None)
            if workspace is not None:
                self.controller.runtime_metrics=session.metrics
                return workspace.serve(self,'dashboard')
            return self._run_dashboard()

    def _run_dashboard(self):
        self.root = Path(__file__).resolve().parents[1]
        from src.rwtd.android_core.failure_evidence import FailureEvidence
        self.failure_evidence=FailureEvidence(self.root,notifier=self._failure_notifier())
        from src.rwtd.android_core.record_retention import install_log_limits,enforce
        install_log_limits(self.root)
        enforce(self.root,'logs','logs')
        self.trigger_frames.clear()
        self.device_metadata = device_snapshot(self)
        with self.image_lock:
            self.image_cache.clear()
            self.observations = []
            self.frame_metadata = {"frame_id": None, "source": "Synthetic", "stale": False, "recognition_valid": False}
        self.preview_device = None
        self.monitor = get_monitor(self.root)
        if self.monitor.stop_requested:
            self.log_warning("異常服務已要求停止；先檢閱證據再重新啟動。")
            return
        self.manual = ManualDeviceTests(self, self.root)
        controller = SimulationController(manual_handler=self.manual.handle,
                                          allow_simulation=False,
                                          allow_manual=bool(self.config.get("Allow Manual Device Test", False)),
                                          definitions=self.manual.store.catalog(), roles=self.manual.services_snapshot()["scheduler_roles"])
        controller.manual_history.extend(list(self.manual.reports.values())[-200:])
        controller.manual_confirmation_only = True
        controller.loop_settings_override = settings_from_ok(self.config)
        self.controller = controller
        controller.detailed_logging=self.config.get('Detailed Logging',False)
        controller.runtime_metrics=self._log_session.metrics
        controller.exhaustion_callback=self.on_exhaustion
        controller.canvas_runtime.exhaustion_callback=self.on_exhaustion
        controller.trigger_scheduler.exhaustion_callback=self.on_exhaustion
        controller.enable_trigger_observers(self.observe_trigger,self.release_trigger_observer)
        synthetic = np.zeros((2400, 1080, 3), dtype=np.uint8)
        for y, text in enumerate(("PREVIEW STOPPED", "NO ANDROID CAPTURE")):
            cv2.putText(synthetic, text, (35, 180 + y * 180), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (40, 220, 240), 3)
        self.publish_frame("synthetic", synthetic)
        with self.image_lock:
            self.image_cache["frame"] = self.image_cache["synthetic"]
        self.restore_simulation()

        def image(which):
            if which.startswith('reference:'):
                from src.rwtd.android_core.feature_catalog import reference_png
                try:
                    return reference_png(self.root, which.split(':', 1)[1])
                except (ValueError, OSError, KeyError):
                    return None
            if which.startswith("evidence:"):
                _, report_id, side = which.split(":")
                try:
                    record = self.manual.storage.load_report(report_id)
                    if not record or record.get("screenshots_cleaned") or side not in {"before", "after"}:
                        return None
                    path = self.manual.storage.path("session_" + report_id + "/" + side + ".png")
                    return path.read_bytes() if path.is_file() else None
                except (ValueError, OSError):
                    return None
            with self.image_lock:
                return self.image_cache.get(which)

        def snapshot():
            value = controller.snapshot()
            with self.image_lock:
                value.update(visual_source=self.visual_source, matches=list(self.observations), frame=dict(self.frame_metadata),
                             device=dict(self.device_metadata), manual_permission=controller.allow_manual)
            value.update(runtime.snapshot())
            value.update(self.manual.services_snapshot())
            value["manual_history"] = list(self.manual.reports.values())[-300:]
            with self._screen_lock:
                geometry = self._screen_geometry
            value['display_size'] = ([geometry.width, geometry.height] if geometry else None)
            value['module_display_mapping'] = self.manual.display_mapping.describe(geometry)
            value['display_size_mode'] = self.config.get('Display Size', 'Auto')
            from src.rwtd.android_core.feature_catalog import feature_names
            value["feature_catalog"] = [{"name": name, "executable": True}
                                        for name in sorted(feature_names(self.root))]
            value['scheduler_discord_permission'] = self.config.get('Allow Scheduler Discord', False) is True
            value['ok_canvas_loop_override'] = controller.loop_settings_override
            return value

        def padding_recognizer(frame,row):
            # CPU-only matcher owns separate caches; no live capture or input.
            from src.rwtd.android_core.preview_worker import PreviewWorker
            from src.rwtd.android_core.padding_coco import evaluate
            from types import SimpleNamespace
            worker=PreviewWorker(self.root,PreviewMatcher._observe,
                self._read_image,self._search_region,self._preview_geometry)
            def detect(pixels,features,threshold,coco_roi=None):
                return worker.analyze(pixels,set(features),threshold,coco_roi)
            try:return evaluate(SimpleNamespace(detect_manual_features=detect,ocr=self.ocr),frame,row)
            finally:worker.close()
        bridge = DashboardBridge(controller, self.root / "assets/rwtd/dashboard", snapshot, image,
                                 padding_recognizer=padding_recognizer)
        self.bridge = bridge
        roi_model = roi_bridge = None
        calibration_model = calibration_bridge = calibration_task = None
        statistics_model = statistics_bridge = None
        runtime = DashboardRuntime(controller, self.restore_simulation, self.open_dashboard)
        controller.runtime_handler = runtime.command
        from src.rwtd.android_core.preview_worker import PreviewWorker
        self.preview_worker = PreviewWorker(self.root,PreviewMatcher._observe,
                                            self._read_image,self._search_region,
                                            self._preview_geometry,self._log_template_mapping)
        def cleanup():
            self.preview_worker.close()
            self.preview_worker = self.preview_job = None
            controller.close()
            try:
                self.manual.handle("abort_module", {}, controller.device_stop)
            finally:
                if roi_model:
                    roi_model.closing.set()
                if roi_bridge:
                    roi_bridge.close()
                if calibration_model:
                    calibration_model.closing.set()
                if calibration_bridge:
                    calibration_bridge.close()
                if statistics_model:
                    statistics_model.closing.set()
                if statistics_bridge:
                    statistics_bridge.close()
                if calibration_task and calibration_task.bridge is calibration_bridge:
                    calibration_task.bridge = None
                bridge.close()
                self.bridge = None
                self.trigger_frames.clear()
        try:
            try:
                from src.rwtd.roi_overview import RoiOverview
                roi_model = RoiOverview(self.root)
                roi_bridge = DashboardBridge(roi_model,self.root/'assets/rwtd/roi_overview',
                    roi_model.snapshot,roi_model.image,max_command_bytes=12*1024**2,
                    embedded_parent_origin=bridge.origin)
                bridge.embedded_frame_origin = roi_bridge.origin
                bridge.workspace_roi_url = roi_bridge.url+'&parent_origin='+bridge.origin
                roi_bridge.start()
            except Exception as exc:
                if roi_bridge:
                    roi_bridge.close()
                roi_model = roi_bridge = None
                bridge.workspace_roi_url = None
                bridge.embedded_frame_origin = None
                self.log_warning('RWTD_ROI_WORKSPACE_UNAVAILABLE '+type(exc).__name__)
            from src.rwtd.farm_workspace import FarmWorkspace
            try:
                from src.rwtd.resolution_calibration_ui import CalibrationEditor
                calibration_task = next((task for task in self.executor.onetime_tasks
                    if type(task).__name__=='RwtdResolutionCalibrationTask'),None)
                if calibration_task is None:
                    raise ValueError('Resolution calibration TASK not registered')
                calibration_task.root = self.root
                calibration_model = CalibrationEditor(self.root, calibration_task._capture_calibration)
                calibration_bridge = DashboardBridge(calibration_model,self.root/'assets/rwtd/calibration_ui',
                    calibration_model.snapshot,calibration_model.image,max_command_bytes=9*1024**2,
                    embedded_parent_origin=bridge.origin)
                calibration_bridge.start()
                calibration_task.bridge = calibration_bridge
                bridge.embedded_frame_origins = [calibration_bridge.origin]
                bridge.workspace_calibration_url = calibration_bridge.url+'&parent_origin='+bridge.origin
            except Exception as exc:
                if calibration_bridge:
                    calibration_bridge.close()
                if calibration_task:
                    calibration_task.bridge = None
                calibration_model = calibration_bridge = None
                self.log_warning('RWTD_CALIBRATION_WORKSPACE_UNAVAILABLE '+type(exc).__name__)
            try:
                from src.rwtd.round_statistics_ui import RoundStatisticsEditor
                statistics_model = RoundStatisticsEditor(self.root)
                statistics_bridge = DashboardBridge(statistics_model,self.root/'assets/rwtd/statistics_ui',
                    statistics_model.snapshot,statistics_model.image,embedded_parent_origin=bridge.origin)
                statistics_bridge.start()
                bridge.embedded_frame_origins.append(statistics_bridge.origin)
                bridge.workspace_statistics_url = statistics_bridge.url+'&parent_origin='+bridge.origin
            except Exception as exc:
                if statistics_bridge:
                    statistics_bridge.close()
                statistics_model = statistics_bridge = None
                self.log_warning('RWTD_STATISTICS_WORKSPACE_UNAVAILABLE '+type(exc).__name__)
            workspace=FarmWorkspace(self,controller,runtime,roi_model,calibration=calibration_model,statistics=statistics_model)
            workspace.cleanup=cleanup
            bridge.workspace_host=workspace
            bridge.command_access=workspace.dashboard_access
            if roi_bridge:
                def roi_access(command):
                    if workspace.closed or workspace.pending_handoff or workspace.current!='roi' or workspace.running:
                        raise ValueError('請先停止並儲存目前功能，再切到 ROI 總覽。')
                roi_bridge.command_access=roi_access
            if calibration_bridge:
                def calibration_access(command):
                    if workspace.closed or workspace.pending_handoff or workspace.current!='calibration' or workspace.running:
                        raise ValueError('請先停止並儲存目前功能，再切到解析度對照。')
                calibration_bridge.command_access=calibration_access
            if statistics_bridge:
                def statistics_access(command):
                    if workspace.closed or workspace.pending_handoff or workspace.current!='statistics':
                        raise ValueError('請先停止並儲存目前功能，再切到輪次統計。')
                statistics_bridge.command_access=statistics_access
            bridge.start()
            self.info_set("Dashboard", bridge.origin)
            live_preview = self.configure_startup_preview()
            self.log_info("Dashboard 啟動：" + ("Android Preview 待擷取；唯讀、自動刷新關閉。" if live_preview else "預覽已停止。") + "Discord 與 Auto Farming 停用。")
            if self.config.get("Open Dashboard", True):
                webbrowser.open(bridge.url)
            previous = time.monotonic()
            next_log_cleanup = previous + 60
            def dashboard_turn():
                nonlocal previous,next_log_cleanup
                if self.monitor.stop_requested:
                    controller.device_stop.set()
                    runtime.closing=True
                    return
                controller.process_next()
                self.finish_preview_recognition()
                now = time.monotonic()
                if now >= next_log_cleanup:
                    enforce(self.root,'logs','logs')
                    next_log_cleanup = now + 60
                controller.advance(min(now - previous, 1))
                previous = now
                self._log_session.report()
                # Watchers get their due turn before a disposable preview capture.
                # A single preview job bounds memory and avoids CPU backlog.
                if self.preview_job is None:
                    runtime.tick(bridge.last_seen)
            workspace.dashboard_turn=dashboard_turn
            workspace.serve(self,'dashboard')
        finally:
            if 'workspace' in locals():
                if not workspace.pending_handoff and not workspace.closed:
                    workspace.shutdown()
            else:
                cleanup()

