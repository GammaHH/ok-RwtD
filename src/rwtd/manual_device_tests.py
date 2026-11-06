"""Single-request tests on the owning OK-Script thread; no independent ADB client."""
import re
import time
import uuid
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from src.rwtd.discord_notifications import write_json
from src.rwtd.exception_monitor import scoped_path
from src.rwtd.step_verification import POLICIES, verify, module_outcome
from src.rwtd.android_core.module_store import ModuleStore, atomic_json, safe_path
from src.rwtd.profile import rwtd_profile
from src.rwtd.android_core.storage_history import StorageHistory
from src.rwtd.android_core.workflow_model import candidate_features, validate_candidates, resolve_step, select_target, select_targets, TargetNotFound
from src.rwtd.android_core.decision_graph import DecisionGraphStore, simulate_graph
from src.rwtd.android_core.ui_preferences import UiPreferences
from src.rwtd.android_core.screen_geometry import ScreenGeometry
from src.rwtd.android_core.evidence_policy import requires_screenshots, screenshots_omitted

SAFE_FEATURES = {"quest", "army", "food", "buff", "store", "locate", "buy1", "buy2",
                 "chest", "food_chest", "reward_get", "get_reward", "buff_1", "buff_2", "buff_3",
                 "reborn", "backtolobby", "backtolobby_comfirm", "buy_max"}
TEST_NAMES = {"TapTest", "LongPressTest", "SwipeCalibration", "CocoTargetTest", "WaitTest"}
REPEATABLE_TEST_NAMES = {"TapTest", "LongPressTest", "CocoTargetTest"}


class ManualActionInterrupted(ValueError):
    """A user Stop/Pause was observed between completed Android gestures."""


def coordinate(value, limit):
    if type(value) is not int or not 0 <= value < limit:
        raise ValueError(f"Coordinate must be integer in [0,{limit})")
    return value


def canonical_import_definition(definition):
    """Store only 1080x2400 points; invert explicitly marked native points.

    Dashboard's gesture editor already produces reference points.  An
    external JSON may mark only the points recorded directly on a physical
    display, leaving copied or independently edited points untouched.  The
    import marker is consumed before schema validation and never persisted.
    """
    if not isinstance(definition, dict) or 'physical_coordinates' not in definition:
        return definition
    result = deepcopy(definition)
    source = result.pop('physical_coordinates')
    if (not isinstance(source, dict) or set(source) != {'source_size', 'steps'} or
            not isinstance(source['source_size'], list) or len(source['source_size']) != 2 or
            not isinstance(source['steps'], dict) or not source['steps']):
        raise ValueError('physical_coordinates requires source_size and marked steps')
    geometry = ScreenGeometry(*source['source_size'])
    steps = result.get('steps')
    if not isinstance(steps, list):
        raise ValueError('Physical coordinate import requires module steps')
    by_id = {step.get('step_id'): step for step in steps if isinstance(step, dict)}
    if len(by_id) != len(steps) or set(source['steps']) - set(by_id):
        raise ValueError('Physical coordinate marker references an unknown step')
    pairs = {'start': ('x', 'y'), 'mid': ('mid_x', 'mid_y'),
             'end': ('end_x', 'end_y')}
    for step_id, points in source['steps'].items():
        if (not isinstance(points, list) or not points or
                not all(isinstance(point, str) for point in points) or
                len(points) != len(set(points)) or
                set(points) - set(pairs)):
            raise ValueError('Physical coordinate marker needs distinct start/mid/end points')
        step = by_id[step_id]
        for point in points:
            x_key, y_key = pairs[point]
            if x_key not in step or y_key not in step:
                raise ValueError('Marked physical point is missing from its step')
            step[x_key], step[y_key] = geometry.reference_point(step[x_key], step[y_key])
    return result


def validate_parameters(payload, allowed_features=None):
    allowed = SAFE_FEATURES if allowed_features is None else set(allowed_features)
    if set(payload) - {"name", "x", "y", "end_x", "end_y", "mid_x", "mid_y", "duration_ms", "feature", "threshold", "reversible_ack", "verify_feature",
                       "verification_policy", "previous_feature", "timeout_seconds", "retry_policy", "precondition_feature", "allow_unconfirmed_target", "ocr_roi", "coco_roi", "coco_feature_rois", "roi_tag", "features", "feature_priority", "post_wait_seconds", "step_id", "workflow_module", "delay_seconds", "recognize_before", "repeat_count", "color_mode", "color_rgb", "color_tolerance", "color_min_pixels", "target_policy", "input_conditions", "unknown_no_retry", "ocr_target", "target_conditions", "condition_effect"}:
        raise ValueError("Unknown test parameter")
    if type(payload.get('unknown_no_retry', False)) is not bool:
        raise ValueError('unknown_no_retry must be boolean')
    from src.rwtd.android_core.target_conditions import validate as validate_target_conditions
    validate_target_conditions(payload,allowed)
    from src.rwtd.android_core.step_control import validate as validate_step_control
    validate_step_control(payload)
    from src.rwtd.android_core.ocr_target import validate as validate_ocr_target
    validate_ocr_target(payload)
    if 'input_conditions' in payload:
        from src.rwtd.android_core.step_conditions import validate
        validate(payload['input_conditions'], allowed)
    name = payload.get("name")
    if name not in TEST_NAMES:
        raise ValueError("Gameplay module needs a verified plan; only basic manual tests are currently executable")
    repeat_count = payload.get("repeat_count", 1)
    if type(repeat_count) is not int or not 1 <= repeat_count <= 100:
        raise ValueError("Repeat count must be integer 1..100")
    if name not in REPEATABLE_TEST_NAMES and repeat_count != 1:
        raise ValueError("Only tap/hold actions support repeated operation; swipe/wait count must be 1")
    if payload.get("reversible_ack") is not True:
        raise ValueError("Confirm that the selected target is a reversible, non-premium approved test")
    threshold = payload.get("threshold", .9)
    if type(threshold) not in (int, float) or not .5 <= threshold <= 1:
        raise ValueError("Invalid confidence threshold")
    feature = payload.get("feature", "")
    features = validate_candidates(payload, allowed)
    if 'coco_feature_rois' in payload:
        from src.rwtd.android_core.workflow_model import validate_roi
        rois=payload['coco_feature_rois']
        if not isinstance(rois,dict) or set(rois)-set(features):
            raise ValueError('Feature ROI requires selected COCO features')
        for roi in rois.values():
            if roi is not None:
                validate_roi(roi)
    if payload.get('target_policy', 'single') not in {'single','unique_only','all_top_to_bottom','first_top_to_bottom','first_bottom_to_top'}:
        raise ValueError('Unknown COCO target policy')
    if payload.get('target_policy') == 'all_top_to_bottom' and (name not in REPEATABLE_TEST_NAMES or not (features or payload.get('target_conditions') or payload.get('ocr_target') or payload.get('color_mode'))):
        raise ValueError('Multi-target batch requires COCO tap/hold candidates')
    from src.rwtd.android_core.color_target import validate_color
    validate_color(payload)
    if (payload.get('color_mode') == 'only' and features) or (payload.get('color_mode') in {'coco', 'either'} and not features):
        raise ValueError('Combined mode requires COCO candidates; color-only cannot have them')
    if "coco_roi" in payload:
        roi = payload["coco_roi"]
        if not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi):
            raise ValueError("COCO ROI requires native integer [x,y,width,height]")
        x, y, w, h = roi
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1080 or y+h > 2400:
            raise ValueError("COCO ROI outside native frame")
    if feature and feature not in allowed:
        raise ValueError("Unknown COCO feature")
    verify_feature = payload.get("verify_feature", "")
    if verify_feature and verify_feature not in allowed:
        raise ValueError("Unsupported resulting-state verification feature")
    if name == "CocoTargetTest" and not features:
        raise ValueError("Select an authorized COCO feature")
    recognize = payload.get("recognize_before", True)
    if type(recognize) is not bool:
        raise ValueError("recognize_before must be boolean")
    if features and not recognize and name != "WaitTest":
        raise ValueError("COCO-target action requires recognition")
    if name != "WaitTest" and not payload.get('color_mode') and not payload.get('ocr_target') and not payload.get('target_conditions') and (not features or not recognize) and payload.get("allow_unconfirmed_target") is not True:
        raise ValueError("Explicitly enable fixed coordinates without template confirmation")
    policy = payload.get("verification_policy", "manual")
    if policy not in POLICIES or payload.get("retry_policy", "never") not in {"never", "manual_only"}:
        raise ValueError("Unknown policy; automatic input retries are not implemented")
    timeout = payload.get("timeout_seconds", 5)
    if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
        raise ValueError("Timeout must be 0..30 seconds")
    delay = payload.get("delay_seconds", 0)
    if type(delay) not in (int, float) or not 0 <= delay <= 30:
        raise ValueError("Delay must be 0..30 seconds")
    post_wait = payload.get("post_wait_seconds", 0)
    if type(post_wait) not in (int, float) or not 0 <= post_wait <= 300:
        raise ValueError("Post-action wait must be 0..300 seconds")
    for key in ("precondition_feature", "previous_feature"):
        if payload.get(key) and payload[key] not in allowed:
            raise ValueError("Unsupported precondition/previous feature")
    if policy in {"coco_detected", "coco_disappeared", "screen_transition"} and not verify_feature:
        raise ValueError("Configure expected verification feature")
    if policy == "screen_transition" and not payload.get("previous_feature"):
        raise ValueError("Configure previous screen feature")
    if policy.startswith("ocr_"):
        roi = payload.get("ocr_roi")
        if not isinstance(roi, list) or len(roi) != 4 or any(type(value) is not int for value in roi):
            raise ValueError("Numeric OCR policy requires an explicit pixel ROI [x,y,width,height]")
        x, y, w, h = roi
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1080 or y + h > 2400:
            raise ValueError("OCR ROI outside native frame")
    result = dict(payload, threshold=threshold, repeat_count=repeat_count)
    if "mid_x" in payload or "mid_y" in payload:
        if name != "SwipeCalibration":
            raise ValueError("Midpoint is only supported for swipe")
        result["mid_x"] = coordinate(payload.get("mid_x"), 1080)
        result["mid_y"] = coordinate(payload.get("mid_y"), 2400)
    if name != "WaitTest" and not payload.get('color_mode') and not payload.get('ocr_target') and not payload.get('target_conditions') and (not features or not recognize):
        result["x"], result["y"] = coordinate(payload.get("x"), 1080), coordinate(payload.get("y"), 2400)
    if name == "SwipeCalibration":
        result["end_x"] = coordinate(payload.get("end_x"), 1080)
        result["end_y"] = coordinate(payload.get("end_y"), 2400)
    if name in {"LongPressTest", "SwipeCalibration"}:
        duration = payload.get("duration_ms")
        if type(duration) is not int or not 1 <= duration <= 15000:
            raise ValueError("Duration must be integer 1..15000 ms")
    if name in {"TapTest", "CocoTargetTest"} and "duration_ms" in payload:
        duration = payload["duration_ms"]
        if type(duration) is not int or not 0 <= duration <= 15000:
            raise ValueError("Tap hold duration must be integer 0..15000 ms")
    if name in {"TapTest", "CocoTargetTest"}:
        result['duration_ms'] = payload.get('duration_ms', 0) or 50
    return result


class ManualDeviceTests:
    def __init__(self, task, root):
        self.task, self.root = task, root
        from src.rwtd.android_core.time_guard import TimeGuards
        self.last_report = None
        self.reports = {}
        self.profile = rwtd_profile(root, self.validate_step)
        self.store = ModuleStore(self.profile)
        self.storage = StorageHistory(self.profile)
        from src.rwtd.android_core.module_display_mapping import ModuleDisplayMapping
        self.display_mapping = ModuleDisplayMapping(self.root)
        from src.rwtd.android_core.record_retention import BoundedCache
        self.reports = BoundedCache({item["report_id"]: item for item in self.storage.read_history()})
        self.workflow_runs = {}
        self.module_run = None
        self.decision_store = DecisionGraphStore(self.profile, self.store)
        self.time_guards=TimeGuards(root,namespace=self.decision_store.active_id())
        from src.rwtd.android_core.shared_ocr import SharedOcr
        self.shared_ocr=SharedOcr(root)
        self.decision_result = None
        self.ui_preferences = UiPreferences(self.profile)
        self._canvas_dispatch_token = object()

    def validate_step(self, payload):
        from src.rwtd.android_core.feature_catalog import feature_names
        names = feature_names(self.root)
        # Temporary offline test profiles have no annotation files.
        return validate_parameters(payload, names if names else None)

    def _check_gesture_boundary(self, stop, check_pause=True):
        # The current official adb_shell call is synchronous. Never terminate
        # it or send a competing command to interrupt a finger that is down.
        if stop.is_set() or self.task.exit_is_set():
            raise ManualActionInterrupted("Stop requested at gesture boundary; no remaining input sent")
        controller = getattr(self.task, "controller", None)
        pause = getattr(controller, "module_pause", None)
        if check_pause and pause is not None and pause.is_set():
            raise ManualActionInterrupted("Pause requested at gesture boundary; no remaining input sent")

    @property
    def workflows(self):
        return {item["id"]: item for item in self.store.catalog()}

    def _canvas_save_receipt(self, graph):
        """A save acknowledgement describes the validated file read from disk."""
        stored = self.decision_store.load(force_read=True)
        if stored != graph:
            raise ValueError('Canvas 儲存後讀回不一致；保留草稿並重新檢查檔案')
        receipt = {'id': self.decision_store.active_id(),
                   'path': str(self.decision_store.path('decision.json')),
                   'revision': stored['revision']}
        logger = getattr(self.task, 'log_info', None)
        if logger:
            logger('CANVAS_SAVED id=' + receipt['id'] + ' path=' + receipt['path']
                   + ' revision=' + receipt['revision'])
        return receipt

    def services_snapshot(self):
        scheduler_error = None
        canvas_load_error = None
        canvas_edit_warning = None
        try:
            roles = self.store.scheduler()
        except (ValueError, OSError, TypeError) as exc:
            roles, scheduler_error = {}, str(exc)
        try:
            graph, canvas_edit_warning = self.decision_store.load_for_edit()
            canvas_files=self.decision_store.catalog()
            canvas_active_id=self.decision_store.active_id()
        except (ValueError, OSError, TypeError) as exc:
            graph, scheduler_error = None, str(exc)
            canvas_load_error = str(exc)
            canvas_files,canvas_active_id=[],'default'
        try:
            preferences = self.ui_preferences.load()
        except (ValueError, OSError, TypeError):
            preferences = {"scheduler_open": False, "preview_scale": 100}
        from .android_core.feature_settings import load as load_feature_settings
        feature_settings_error = None
        try:
            feature_settings = load_feature_settings(self.root)['features']
        except (ValueError, OSError, TypeError) as exc:
            feature_settings, feature_settings_error = {}, str(exc)
        return {"profile_id": self.profile.id, "module_catalog": self.store.catalog(), "decision_graph": graph,
                'shared_ocr_values':self.shared_ocr.snapshot(),
                "canvas_load_error": canvas_load_error,
                "canvas_edit_warning": canvas_edit_warning,
                'feature_settings':feature_settings,'feature_settings_error':feature_settings_error,
                'canvas_files':canvas_files,'canvas_active_id':canvas_active_id,
                "decision_result": self.decision_result, "ui_preferences": preferences,
                "scheduler_roles": roles, "scheduler_error": scheduler_error, "storage_result": getattr(self, "storage_result", None),
                "editor_result": getattr(self, "editor_result", None), "module_run": self.module_run,
                'module_failure_report':getattr(self,'latest_module_failure_report',None),
                'module_step_reports':[record for record in self.reports.values()
                    if self.module_run and record.get('run_id')==self.module_run['run_id']]}

    def handle(self, command, payload, stop, *, _dispatch_token=None):
        if command == 'chain_observe':
            from ok.feature.Box import Box
            controller = self.task.controller
            if not (controller.chain_test or controller.schedule_test) or stop.is_set() or controller.device_stop.is_set():
                return {}
            rule = (controller.schedule_test.schedule.by_id[controller.schedule_test.schedule.current].get('continue_condition',{})
                    if controller.schedule_test else controller.chain_test['policy'].get('continue_condition',{}))
            from src.rwtd.android_core.chain_ocr import reading_roi
            roi = reading_roi(rule)
            if roi is None:
                return {}
            backend,device = self.task.manual_backend()
            frame = backend.get_frame();self.task.validate_native_frame(frame)
            boxes = self.task.ocr(box=Box(*roi),frame=frame,threshold=rule.get('threshold',.8),
                                  _rwtd_progress=rule.get('field')=='progress_km')
            _,after = self.task.manual_backend()
            if stop.is_set() or controller.device_stop.is_set() or after!=device:
                return {}
            numbers=[float(m) for b in boxes for m in re.findall(r'\d+(?:\.\d+)?',str(b.name or '').replace(',',''))]
            from src.rwtd.android_core.ocr_records import box_tokens,publish_result
            effective=getattr(self.task,'recognition_roi',None)
            publish_result(self.task,box_tokens(boxes),frame,
                roi=effective(roi,'ocr',progress=rule.get('field')=='progress_km') if callable(effective) else roi,
                configured_roi=roi,threshold=rule.get('threshold',.8),route='dashboard_chain',
                result='YES' if len(numbers)==1 else 'UNKNOWN',value=numbers[0] if len(numbers)==1 else None,
                reason=None if len(numbers)==1 else 'OCR number absent or ambiguous')
            return {rule['field']:numbers[0]} if len(numbers)==1 else {}
        if command == 'canvas_permissions':
            save_reports=payload.get('save_reports',True)
            if type(save_reports) is not bool:
                raise ValueError('save_reports must be boolean')
            self.canvas_save_reports=save_reports
            latest={}
            for r in self.reports.values():
                if not r.get('reports_saved',True) and not r.get('manual_verification'):
                    continue
                key=(r.get('workflow_module'),r.get('workflow_revision'),r.get('step_id'))
                latest[key]=r
            self.canvas_verified_steps={key for key,r in latest.items()
                if r.get('status') in {'SUCCESS','MANUAL_VERIFIED'} and not r.get('failure_reason')}
            permission = payload.get('notify_discord', False)
            if type(permission) is not bool or (permission and self.task.config.get('Allow Scheduler Discord', False) is not True):
                raise ValueError('Scheduler Discord is disabled in task settings')
            self.canvas_notify_discord=permission
            return {'status':'READY','device_inputs':0}
        if command == 'canvas_effect':
            if payload.get('node',{}).get('kind')=='module' and self.task.controller.canvas_runtime.unattended:
                return self.execute_chain_test_module(payload['node'],stop)
            return self.task.canvas_effect(payload, stop)
        if command == 'trigger_execute':
            return self.execute_trigger_request(payload, stop)
        input_locked = self.decision_store.input_locked
        if input_locked is None:
            input_locked = self.decision_store.execution_locked
        if command in {"execute", "execute_module"} and input_locked and _dispatch_token is not self._canvas_dispatch_token:
            raise ValueError("Scheduler active: competing manual Android input is blocked")
        if command == "catalog":
            return self.services_snapshot()
        if command == "execution_status":
            # Queue dispatch only needs ownership; avoid loading every Canvas,
            # module and stored report at each insertion boundary.
            return {"module_run": dict(self.module_run) if self.module_run else None}
        if command == "dashboard_preferences":
            return {"status": "IDLE", "preferences": self.ui_preferences.save(payload), "device_inputs": 0}
        if command in {'canvas_select','canvas_save_as','canvas_rename'}:
            if self.module_run and self.module_run.get('active'):raise ValueError('Stop module execution before switching Canvas files')
            if payload.get('canvas_id') is not None and payload['canvas_id']!=self.decision_store.active_id():
                raise ValueError('Canvas selection changed; reload before saving')
            if command=='canvas_select':graph=self.decision_store.select(payload['id'])
            elif command=='canvas_save_as':graph=self.decision_store.save_as(payload['graph'],payload['name'])
            else:graph=self.decision_store.rename(payload['name'])
            from src.rwtd.android_core.time_guard import TimeGuards
            self.time_guards=TimeGuards(self.root,namespace=self.decision_store.active_id())
            self.decision_result={'status':'IDLE','graph':graph,'canvas_active_id':self.decision_store.active_id(),'device_inputs':0}
            if command == 'canvas_save_as':
                self.decision_result['saved_canvas'] = self._canvas_save_receipt(graph)
            return dict(self.decision_result)
        if command in {"decision_save", "decision_simulate"}:
            if self.decision_store.execution_locked:
                raise ValueError("Scheduler active: Canvas read-only until Stop")
            if command == "decision_simulate" and self.module_run and self.module_run.get("active"):
                raise ValueError("Stop the manual module before starting simulation flow")
            if command == "decision_save":
                if payload.get('canvas_id') is not None and payload['canvas_id']!=self.decision_store.active_id():
                    raise ValueError('Canvas selection changed; reload before saving')
                graph = self.decision_store.save(payload.get("graph"), payload.get("expected_revision"))
                receipt = self._canvas_save_receipt(graph)
                self.decision_result = {"status": "IDLE", "graph": graph, "saved_canvas": receipt, "device_inputs": 0}
            else:
                self.decision_result = simulate_graph(payload.get("graph"), payload.get("observations", {}), {m["id"]: m for m in self.store.catalog()})
            return dict(self.decision_result)
        if command == "view_report":
            report = self.storage.load_report(payload.get("report_id"))
            self.task.show_history_evidence(report, self.storage)
            return report
        if command == "abort_module":
            if self.module_run:
                self.module_run.update(active=False, status="STOPPED")
                self.storage.protected_runs.discard(self.module_run["run_id"])
            return {"status": "READY"}
        if (command == "execute" and self.module_run and self.module_run.get("active")
                and not getattr(self, "inside_module", False)
                and _dispatch_token is not self._canvas_dispatch_token):
            raise ValueError("A complete module is paused; continue it or Stop before a separate step test")
        if (command in {"module_save", "module_import", "module_delete", "scheduler_save"}
                and self.module_run and self.module_run.get("active")):
            raise ValueError("Stop the paused module before editing definitions or scheduler bindings")
        if command == "execute_module":
            return self.execute_module(payload, stop)
        if (command == 'execute' and payload.get('dashboard_manual') is True
                and payload.get('workflow_module') and not getattr(self,'inside_module',False)
                and _dispatch_token is not self._canvas_dispatch_token):
            return self.execute_step_test(payload,stop)
        if command == "refresh_frame":
            if hasattr(self.task, "capture_preview"):
                return self.task.capture_preview(stop, payload)
            backend, _ = self.task.manual_backend()
            frame = backend.get_frame()
            self.task.validate_native_frame(frame)
            self.task.publish_frame("refresh", frame)
            return {"status": "READY", "device_inputs": 0, "note": "Explicit read-only refresh"}
        if command.startswith("storage_"):
            if command == "storage_scan":
                result = self.storage.scan()
            elif command == "storage_preview":
                result = self.storage.preview()
            elif command == "storage_clean":
                result = self.storage.clean(payload.get("plan_id"), approved=payload.get("approved") is True)
            elif command == "storage_settings":
                result = self.storage.save_settings(payload)
            elif command == "storage_open":
                import os
                directory = self.storage.path("history")
                directory.mkdir(parents=True, exist_ok=True)
                os.startfile(str(directory))
                result = {"status": "READY", "folder": str(directory)}
            else:
                raise ValueError("Unknown storage command")
            self.storage_result = result
            if command == "storage_clean":
                self.reports.update({item["report_id"]: item for item in self.storage.read_history()})
            return {"status": "READY", "storage": result}
        if command.startswith("module_") or command == "scheduler_save":
            if command == "module_save":
                definition=deepcopy(payload.get('definition'))
                if isinstance(definition,dict):
                    metadata=definition.setdefault('coordinate_metadata',{'stored_resolution':[1080,2400]})
                    if isinstance(metadata,dict):
                        geometry=getattr(self.task,'_screen_geometry',None)
                        if geometry is not None:
                            metadata['last_edit_resolution']=[geometry.width,geometry.height]
                result = self.store.save(definition, payload.get("expected_revision"),
                                         allow_system_update=payload.get("system_update", False))
            elif command == "module_import":
                definition = canonical_import_definition(payload.get("definition"))
                result = self.store.save(definition, payload.get("expected_revision"),
                                         allow_system_update=True)
            elif command == "module_duplicate":
                result = self.store.get(payload.get("id"))
                result = {key: value for key, value in result.items() if key not in {"kind", "revision"}}
                result.update(id=payload.get("new_id"), name=payload.get("new_name"), version=1)
                result = self.store.save(result)
            elif command == "module_delete":
                if payload.get("confirmed") is not True:
                    raise ValueError("Explicit Delete User Module confirmation required")
                result = {"backup": self.store.delete(payload.get("id"), payload.get("expected_revision"))}
            elif command == "module_export":
                result = {key: value for key, value in self.store.get(payload.get("id")).items() if key != "kind"}
            elif command == "scheduler_save":
                result = self.store.save_scheduler(payload.get("roles"))
            else:
                raise ValueError("Unknown editor command")
            self.editor_result = {"command": command, "definition": result}
            return {"status": "READY", "editor": self.editor_result}
        if command == "configure_workflow":
            raise ValueError("Use the persistent module editor to save a user definition")
        if command == "save_preset":
            return self.save_preset(payload)
        if command == "manual_verify":
            report = self.storage.load_report(payload.get("report_id")) or self.reports.get(payload.get('report_id'))
            if self.module_run and self.module_run.get('post_run_verification') and self.module_run.get('active') and report and report.get('run_id')==self.module_run['run_id']:
                raise ValueError('Complete or stop the module before reviewing its steps')
            if not report or not report.get("action_completed", report.get("input_command_returned")) or (not report.get('after') and not screenshots_omitted(report)):
                raise ValueError("A completed real test with valid screenshot policy is required")
            if report.get("failure_reason") or report.get("status") in {"BLOCKED", "INPUT_DELIVERY_UNKNOWN"}:
                raise ValueError("Failed delivery/capture or blocked input cannot be overridden by a human Pass")
            if report.get("input_attempted") and not report.get("input_command_returned"):
                raise ValueError("Input delivery has not been confirmed")
            for side in ("before", "after"):
                if screenshots_omitted(report):
                    continue
                expected = self.storage.path("session_" + report["report_id"] + "/" + side + ".png")
                if str(expected) != report[side]:
                    raise ValueError("Evidence path does not belong to this session")
            if report.get("screenshots_cleaned") or (not screenshots_omitted(report) and not Path(report["after"]).is_file()):
                raise ValueError("Evidence has already been cleaned")
            if report.get("manual_verification"):
                raise ValueError("This report already has an explicit human judgment")
            if payload.get("pass") not in (True, False) or type(payload.get("pass")) is not bool:
                raise ValueError("Use explicit human Pass or Fail")
            report["automatic_status"] = report["status"]
            report["status"] = "MANUAL_VERIFIED" if payload["pass"] else "FAILED"
            report["manual_verification"] = {"pass": payload["pass"], "note": str(payload.get("note", ""))[:500],
                                             "timestamp_utc": datetime.now(timezone.utc).isoformat()}
            self.reports[report["report_id"]] = report
            report["module_result"] = self.workflow_result(report.get("workflow_module"), report.get("run_id"))
            if report.get('reports_saved',True):
                self.storage.finish(report)
            run=self.module_run
            if run and run['run_id']==report.get('run_id') and not run.get('active'):
                run['status']=report['module_result']['status']
            elif run and run.get('active') and run.get('waiting_report')==report['report_id'] and run['run_id']==report.get('run_id'):
                if not payload['pass']:
                    run.update(active=False,status='FAILED')
                    self.storage.protected_runs.discard(run['run_id'])
            return report
        report_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:8]
        omit_records=getattr(self,'_omit_execution_records',False)
        output = self.storage.path('session_'+report_id) if omit_records else self.storage.begin(report_id)
        record = {"report_id": report_id, "module": payload.get("name"), "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                  "mode": "Manual Device Test", "simulated": False, "status": "BLOCKED", "input_attempted": False,
                  "input_command_returned": False, "device_inputs": 0, "before": None, "after": None,
                  "action_completed": False,
                  "repeat_count_requested": 0, "repeat_count_completed": 0,
                  "inputs_attempted": 0, "inputs_confirmed": 0, "repetitions": [],
                  "detection": [], "parameters": None, "failure_reason": None, "discord_enabled": False,
                  'reports_saved':not omit_records}
        path = output / "report.json"
        parameters = {}
        try:
            payload = dict(payload)
            manual_only = payload.pop("dashboard_manual", False)
            if type(manual_only) is not bool:
                raise ValueError("dashboard_manual must be boolean")
            record["manual_confirmation_only"] = manual_only
            parameters = self.validate_step(payload)
            record["parameters"] = parameters
            record["repeat_count_requested"] = parameters["repeat_count"]
            module_name = parameters.get("workflow_module")
            if module_name:
                workflow = self.workflows.get(module_name)
                if not workflow:
                    raise ValueError("Configure explicit module steps first")
                selected = [item for item in workflow["steps"] if item["step_id"] == parameters.get("step_id")]
                if len(selected) != 1:
                    raise ValueError("Configured step missing")
                expected_parameters = self.validate_step(dict(selected[0], reversible_ack=True, workflow_module=module_name))
                if parameters != expected_parameters:
                    raise ValueError("Parameters differ from saved step configuration; configure again")
                index = workflow["steps"].index(selected[0])
                if self.module_run and self.module_run.get("module") == module_name and self.module_run.get("active"):
                    run_id = self.module_run["run_id"]
                elif index == 0 or module_name not in self.workflow_runs:
                    run_id = uuid.uuid4().hex
                    self.workflow_runs[module_name] = run_id
                else:
                    run_id = self.workflow_runs[module_name]
                for prior in (workflow["steps"][:index] if getattr(self, "inside_module", False) else []):
                    reports = [value for value in self.reports.values() if value.get("workflow_module") == module_name
                               and value.get("workflow_revision") == workflow["revision"] and value.get("run_id") == run_id and value.get("step_id") == prior["step_id"]]
                    continuous=(self.module_run and self.module_run.get('active') and self.module_run.get('require_manual_verification') is False)
                    if not reports or (reports[-1]["status"] not in {"SUCCESS", "MANUAL_VERIFIED"}
                                       and not (continuous and reports[-1].get('action_completed') and not reports[-1].get('failure_reason'))
                                       and not (prior.get("verification_policy") == "none" and reports[-1].get("action_completed"))
                                       and not reports[-1].get('exclusion_skipped') and not reports[-1].get('condition_skipped')):
                        raise ValueError("A prior required step needs outcome verification first")
                record.update(workflow_module=module_name, step_id=parameters["step_id"], workflow_revision=workflow["revision"], run_id=run_id)
                parameters = resolve_step(workflow, parameters)
                record["resolved_tag"] = selected[0].get("roi_tag")
                record["resolved_roi"] = parameters.get("coco_roi")
            elif parameters.get("roi_tag"):
                raise ValueError("TAG references require a saved module context")
            elif parameters.get('input_conditions'):
                from src.rwtd.android_core.step_conditions import resolve
                parameters['input_conditions'] = resolve(parameters['input_conditions'], {})
            self._check_gesture_boundary(stop)
            calibrated_points=self.display_mapping.calibrated_points(self.task,
                module_name or getattr(self,'_condition_module_id',None),parameters)
            parameters=self.display_mapping.apply(self.task,
                module_name or getattr(self,'_condition_module_id',None),parameters)
            record['parameters']=parameters
            if 'resolved_roi' in record:
                record['resolved_roi']=parameters.get('coco_roi')
            if (parameters.get('condition_effect',{}).get('action')=='retry_step'
                    and getattr(self,'_step_retry_authorized',None)!=(
                        module_name or getattr(self,'_condition_module_id',None),parameters.get('step_id'))):
                raise ValueError('Conditional Step must use the shared Step execution runner')
            if parameters["name"] != "WaitTest" and stop.wait(parameters.get("delay_seconds", 0)):
                raise ValueError("Stop requested during configured delay")
            backend, device = self.task.manual_backend()
            if getattr(self, "inside_module", False):
                if "device" in self.module_run and self.module_run["device"] != device:
                    raise ValueError("Device changed within module run")
                self.module_run["device"] = device
            record["device"] = device
            recognition_screenshots = requires_screenshots(parameters)
            capture_outcome = recognition_screenshots and not omit_records
            # Only explicit retry/chain exhaustion callbacks persist PNG evidence.
            # Keep the original capture decision independent of file persistence.
            save_screenshots = False
            record.update(screenshot_policy='COCO_ONLY', screenshots_required=save_screenshots)
            preparation_started = time.monotonic()
            record['timing_seconds'] = {}
            prepared_three_point = None
            if parameters['name'] == 'SwipeCalibration' and 'mid_x' in parameters:
                prepare = getattr(self.task, 'prepare_three_point_swipe', None)
                if not callable(prepare):
                    raise ValueError('Official continuous three-point swipe unavailable; no split-swipe fallback')
                prepared_three_point = prepare()
                if not callable(prepared_three_point):
                    raise ValueError('Official continuous touch unavailable; no split-swipe fallback')
            record['timing_seconds']['gesture_preparation'] = round(time.monotonic()-preparation_started, 3)
            capture_started = time.monotonic()
            policy = 'manual' if manual_only else parameters.get('verification_policy', 'manual')
            capture_required = bool(recognition_screenshots or parameters.get('color_mode') or parameters.get('input_conditions') or policy not in {'manual', 'none'})
            record['capture_required'] = capture_required
            record['execution_policy_revision'] = 'explicit-checkpoints-v1'
            if hasattr(self.task, 'dashboard_event'):
                self.task.dashboard_event('PREPARATION', 'Configured recognition/checkpoint requires capture' if capture_required
                                          else 'Fixed coordinates: no capture or COCO recognition')
            before = self._recognition_frame(backend) if capture_required else None
            if capture_required:
                self.task.validate_native_frame(before)
                evidence=getattr(self.task,'failure_evidence',None)
                if evidence is not None:
                    try:evidence.remember(getattr(self,'_evidence_node_id',None) or parameters.get('step_id') or record['module'],
                                          before,device=device)
                    except Exception:pass
            record['timing_seconds']['initial_capture'] = round(time.monotonic()-capture_started, 3)
            publish_action_frame = getattr(self.task, 'publish_action_frame', self.task.publish_frame)
            if capture_required:
                publish_action_frame("before", before)
            if parameters.get('input_conditions') and parameters.get('condition_effect',{}).get('action')!='retry_step':
                from src.rwtd.android_core.step_conditions import evaluate, ConditionRejected, ConditionUnknown
                record['input_conditions'] = evaluate(parameters['input_conditions'], before, self.task)
                from src.rwtd.android_core.step_control import decision,log as log_condition_control
                control=decision(parameters,record['input_conditions'])
                if control:
                    record['condition_control']=dict(control,source_step_id=parameters.get('step_id'))
                    log_condition_control(self.task,getattr(self,'_condition_module_id',None) or record.get('workflow_module') or record['module'],parameters.get('step_id'),control)
                    if (control['action'] in {'skip_step','skip_remaining_now'}
                            or control.get('skip_self_on_match') is True):
                        raise ConditionRejected(control['reason'])
                if record['input_conditions']['result'] == 'NO' and not parameters.get('condition_effect'):
                    raise ConditionRejected('Step input conditions are NO; no input sent')
                if record['input_conditions']['result'] == 'UNKNOWN':
                    raise ConditionUnknown('Step input conditions UNKNOWN; no input sent: '+json.dumps(record['input_conditions'],ensure_ascii=False,default=str)[:1500])
            if save_screenshots:
                self.task.write_raw_png(output / "before.png", before)
                record["before"] = str(output / "before.png")
            started = time.monotonic()
            name = parameters["name"]
            duration = parameters.get("duration_ms", 0) if name != "WaitTest" else 0
            record["input_duration_ms"] = duration
            record["post_wait_seconds"] = parameters.get("post_wait_seconds", 0)
            from src.rwtd.android_core.device_macro import step_interval
            record["effective_post_wait_seconds"] = (max(record["post_wait_seconds"], step_interval(self.task))
                                                     if name != "WaitTest" else record["post_wait_seconds"])
            baseline = None
            batch = (parameters.get('target_policy') == 'all_top_to_bottom'
                     or parameters.get('target_conditions',{}).get('position_policy')=='all'
                     and parameters.get('target_policy') not in {'first_top_to_bottom','first_bottom_to_top','unique_only'}
                     and parameters['name'] in REPEATABLE_TEST_NAMES)
            batch_plan = []
            if batch and parameters.get('target_conditions'):
                batch_plan=self._composed_targets(before,parameters,record)
                record['target_plan']=batch_plan
                record['target_count_requested']=len(batch_plan)
                record['target_count_completed']=0
            elif batch and parameters.get('ocr_target'):
                from src.rwtd.android_core.ocr_target import locate
                batch_plan,tokens=locate(self.task,before,parameters,multiple=True)
                batch_plan=sorted(batch_plan,key=lambda hit:(hit['bbox'][1],hit['bbox'][0]))
                record.update(detection=tokens,ocr_target=dict(parameters['ocr_target']))
                if not batch_plan:raise TargetNotFound('OCR target text not matched; no input sent')
                record.update(target_plan=batch_plan,target_count_requested=len(batch_plan),target_count_completed=0)
            elif batch and parameters.get('color_mode')=='only':
                from src.rwtd.android_core.color_target import components
                batch_plan=[dict(hit,feature='__color__',confidence=1,matched=True) for hit in components(before,parameters)]
                batch_plan.sort(key=lambda hit:(hit['bbox'][1],hit['bbox'][0]))
                if not batch_plan:raise TargetNotFound('Color target missing; no guessed input')
                record.update(target_plan=batch_plan,target_count_requested=len(batch_plan),target_count_completed=0)
            elif batch:
                batch_hits = self.detect_target_features(before,parameters)
                record['detection'] = batch_hits
                if parameters.get('color_mode') == 'coco':
                    from src.rwtd.android_core.color_target import components
                    for hit in batch_hits:
                        if hit.get('matched'):
                            hit['matched'] = bool(components(before, parameters, hit['bbox']))
                if parameters.get('color_mode') == 'either':
                    from src.rwtd.android_core.color_target import either_targets
                    batch_plan = self._resolve_target(either_targets, before, parameters, batch_hits, multiple=True,
                        evidence=record.setdefault('color_target_evidence',{}))
                else:
                    batch_plan = self._resolve_target(select_targets, parameters, batch_hits)
                record['target_plan'] = batch_plan
                record['target_count_requested'] = len(batch_plan)
                record['target_count_completed'] = 0
            plan_size = len(batch_plan) if batch else 1
            operations = parameters['repeat_count'] * plan_size
            if operations > 100:
                raise ValueError('A manual step may send at most 100 intentional inputs')
            for repetition_index in range(operations):
                self._check_gesture_boundary(stop)
                # repeat_count repeats the located input, not its recognition.
                # Keep Stop/pause/device/exclusion checks for every delivery.
                before_path = output / "before.png" if save_screenshots and repetition_index == 0 else None
                repetition = {"index": repetition_index + 1, "before": str(before_path) if before_path else None, "after": None,
                              "status": "BLOCKED", "input_attempted": False, "input_command_returned": False,
                              "detection": [], "duration_ms": duration,
                              "post_wait_seconds": record["post_wait_seconds"],
                              "effective_post_wait_seconds": record["effective_post_wait_seconds"]}
                record["repetitions"].append(repetition)
                if repetition_index and not batch:
                    first = record['repetitions'][0]
                    repetition['detection'] = deepcopy(first['detection'])
                    repetition['target_reused_from'] = 1
                    if 'selected_feature' in first:
                        repetition['selected_feature'] = first['selected_feature']
                precondition = parameters.get("precondition_feature")
                if precondition and repetition_index == 0 and not any(item.get("matched") for item in self.task.detect_manual_features(before, {precondition}, parameters["threshold"])):
                    raise ValueError("Configured step precondition feature not observed")
                features = candidate_features(parameters)
                if parameters.get('target_conditions') and not batch and repetition_index == 0:
                    plan=self._composed_targets(before,parameters,record)
                    if len(plan)!=1:
                        from src.rwtd.android_core.step_conditions import ConditionUnknown
                        raise ConditionUnknown('Multiple target positions; enable all targets or narrow ROI')
                    target=plan[0]
                    tx,ty,tw,th=target['bbox']
                    parameters['x'],parameters['y']=coordinate(tx+tw//2,1080),coordinate(ty+th//2,2400)
                    repetition['detection']=record['detection']
                    record['selected_feature']=repetition['selected_feature']=target['feature']
                if parameters.get('ocr_target') and not batch and repetition_index == 0:
                    from src.rwtd.android_core.ocr_target import locate
                    target,tokens=locate(self.task,before,parameters)
                    record['detection']=repetition['detection']=tokens
                    record['ocr_target']=dict(parameters['ocr_target'])
                    if target is None:
                        raise TargetNotFound('OCR target text not matched; no input sent')
                    tx,ty,tw,th=target['bbox']
                    parameters['x'],parameters['y']=coordinate(tx+tw//2,1080),coordinate(ty+th//2,2400)
                    record['selected_feature']=repetition['selected_feature']='__ocr__'
                if batch:
                    target = batch_plan[repetition_index % plan_size]
                    repetition['detection'] = [target]
                    record['detection'] = batch_plan
                    record['selected_feature'] = repetition['selected_feature'] = target['feature']
                    tx, ty, tw, th = target['bbox']
                    parameters['x'], parameters['y'] = coordinate(tx+tw//2, 1080), coordinate(ty+th//2, 2400)
                if not batch and repetition_index == 0 and features and parameters.get("recognize_before", True) and name != "WaitTest":
                    detections = self.detect_target_features(before,parameters)
                    record["detection"] = repetition["detection"] = detections
                    if parameters.get('color_mode') == 'coco':
                        from src.rwtd.android_core.color_target import components
                        evidence = []
                        for hit in detections:
                            if hit.get('matched'):
                                color_hits = components(before, parameters, hit['bbox'])
                                hit['matched'] = bool(color_hits)
                                evidence.append({'feature': hit['feature'], 'components': color_hits})
                        repetition['color_evidence'] = evidence
                    if parameters.get('color_mode') == 'either':
                        from src.rwtd.android_core.color_target import either_targets
                        target = self._resolve_target(either_targets, before, parameters, detections,
                            evidence=record.setdefault('color_target_evidence',{}))[0]
                        repetition['color_evidence'] = [target] if target['feature'] == '__color__' else []
                    else:
                        target = self._resolve_target(select_target, parameters, detections)
                    record["selected_feature"] = repetition["selected_feature"] = target["feature"]
                    x, y, width, height = target["bbox"]
                    parameters["x"], parameters["y"] = coordinate(x + width // 2, 1080), coordinate(y + height // 2, 2400)
                if parameters.get('color_mode') == 'only' and not batch and repetition_index == 0:
                    from src.rwtd.android_core.color_target import color_target
                    target, evidence = self._resolve_target(color_target, before, parameters)
                    repetition['color_evidence'] = evidence
                    x, y, width, height = target['bbox']
                    parameters['x'], parameters['y'] = coordinate(x + width // 2, 1080), coordinate(y + height // 2, 2400)
                x, y = parameters.get("x", 0), parameters.get("y", 0)
                saved_three_point_path = effective_three_point_path = None
                if name == 'SwipeCalibration' and 'mid_x' in parameters:
                    saved_three_point_path = [[x, y], [parameters['mid_x'], parameters['mid_y']],
                                              [parameters['end_x'], parameters['end_y']]]
                    effective_three_point_path = saved_three_point_path
                    if hasattr(self.task, 'screen_geometry') and not calibrated_points:
                        effective_three_point_path = self.task.screen_geometry().calibrated_three_point_reference(
                            saved_three_point_path, record.get('workflow_module'), parameters.get('step_id'))
                if name != 'WaitTest':
                    runtime = getattr(getattr(self.task,'controller',None),'canvas_runtime',None)
                    regions = getattr(runtime,'exclusion_regions',{})
                    if regions:
                        from src.rwtd.android_core.workflow_model import require_outside_exclusions
                        points = [[x,y]]
                        if name == 'SwipeCalibration':
                            if effective_three_point_path is not None:
                                points = effective_three_point_path
                            else:
                                points.append([parameters['end_x'],parameters['end_y']])
                        require_outside_exclusions(points,regions)
                repetition["target"] = [x, y] if name != "WaitTest" else None
                current_backend, current_device = self.task.manual_backend()
                if current_backend is not backend or current_device != device:
                    raise ValueError("Selected device changed between capture and input")
                self._check_gesture_boundary(stop)
                if baseline is None:
                    baseline = {} if policy in {'manual', 'none'} else self.task.verification_observation(before, parameters)
                three_point_input = prepared_three_point
                if name == "WaitTest" and stop.wait(parameters.get("delay_seconds", 0)):
                    raise ManualActionInterrupted("Stop requested during configured delay")
                self._check_gesture_boundary(stop)
                if name != "WaitTest" and hasattr(self.task, 'screen_geometry'):
                    # Resolve the physical frame before marking an input as
                    # attempted. A failed size check has sent no gesture.
                    self.task.screen_geometry()
                record["status"] = "RUNNING"
                if name != "WaitTest":
                    record["input_attempted"] = repetition["input_attempted"] = True
                    record["input_command_returned"] = False
                    record["inputs_attempted"] += 1
                    record["device_inputs"] = None  # Delivery is uncertain until this command returns.
                    repetition["status"] = "RUNNING"
                if not omit_records:
                    write_json(path, record)
                if hasattr(self.task, "dashboard_event"):
                    self.task.dashboard_event("VALIDATION", f"Operation {repetition_index + 1}/{operations}: preconditions verified; input is not outcome success")
                input_started = time.monotonic()
                if repetition_index == 0:
                    record['timing_seconds']['preparation_to_first_input'] = round(input_started-preparation_started, 3)
                # Official task API -> selected DeviceManager ADB device. A hold
                # is a same-point swipe with its requested duration preserved.
                if name == "WaitTest":
                    output_text = ""
                elif three_point_input is not None:
                    record["gesture_path"] = effective_three_point_path
                    if effective_three_point_path != saved_three_point_path:
                        record["gesture_path_saved"] = saved_three_point_path
                    record["gesture_backend"] = "official ADBInteraction.u2.touch (8+3 moves)"
                    output_text = three_point_input(record["gesture_path"], duration)
                else:
                    end_x, end_y = (parameters["end_x"], parameters["end_y"]) if name == "SwipeCalibration" else (x, y)
                    physical_x, physical_y = self.task.screen_point(x, y)
                    physical_end_x, physical_end_y = self.task.screen_point(end_x, end_y)
                    output_text = self.task.adb_shell(
                        f"input swipe {physical_x} {physical_y} {physical_end_x} {physical_end_y} {duration}",
                        timeout=duration / 1000 + 10)
                repetition["input_elapsed_seconds"] = round(time.monotonic() - input_started, 3)
                if output_text is None or re.search(r"\b(error|exception|unknown command|usage:)\b", str(output_text), re.I):
                    raise RuntimeError("Official input command response unavailable or indicated an error")
                if name != "WaitTest":
                    record["inputs_confirmed"] += 1
                    record["input_command_returned"] = repetition["input_command_returned"] = True
                if (repetition_index + 1) % plan_size == 0:
                    record["repeat_count_completed"] += 1
                if batch:
                    record['target_count_completed'] = min(plan_size, record['inputs_confirmed'])
                record["device_inputs"] = record["inputs_confirmed"]
                record["status"] = repetition["status"] = "INPUT_SENT_UNVERIFIED"
                remaining_gestures = repetition_index + 1 < operations
                self._check_gesture_boundary(stop, check_pause=remaining_gestures)
                # Wait after each completed touch-up. It is distinct from
                # retry_policy: repeated operation never retries a failed input.
                if stop.wait(record["effective_post_wait_seconds"]):
                    raise ManualActionInterrupted("Stop requested during post-action wait")
                self._check_gesture_boundary(stop, check_pause=remaining_gestures)
                if hasattr(self.task, "dashboard_event"):
                    self.task.dashboard_event("INPUT", f"Operation {repetition_index + 1}/{operations} completed; INPUT_SENT_UNVERIFIED")
                if (batch and remaining_gestures) or (not capture_outcome and (manual_only or parameters.get('verification_policy', 'manual') in {'manual', 'none'})):
                    after = before  # No outcome claim; human confirmation follows the complete batch.
                    continue
                after = backend.get_frame()
                self.task.validate_native_frame(after)
                publish_action_frame("after", after)
                # Preserve the existing retention allowlist: this step keeps
                # its first before and latest after PNG, plus compact per-input
                # detection evidence. Never imply one saved PNG is every frame.
                for previous_repetition in record["repetitions"][:-1]:
                    previous_repetition["after"] = None
                if save_screenshots:
                    self.task.write_raw_png(output / "after.png", after)
                    repetition["after"] = str(output / "after.png")
                    record["after"] = str(output / "after.png")
                record["after_repetition_index"] = repetition_index + 1
            record["input_elapsed_seconds"] = round(time.monotonic() - started, 3)
            record["action_completed"] = True
            expected = parameters.get("verify_feature")
            policy = "manual" if manual_only else parameters.get("verification_policy", "manual")
            deadline = time.monotonic() + parameters.get("timeout_seconds", 5)
            while True:
                observation = {} if policy in {'manual', 'none'} else self.task.verification_observation(after, parameters)
                status, reason = verify(policy, baseline, observation, expected or "", parameters.get("previous_feature", ""))
                record["verification"] = {"policy": policy, "before": baseline, "after": observation, "reason": reason,
                                          "automatic": policy not in {"manual", "none"}}
                if status != "PENDING":
                    record["status"] = status
                    break
                if stop.is_set():
                    record["verification"]["reason"] = "Stop requested; verification ended without interrupting the completed input"
                    break
                if time.monotonic() >= deadline:
                    record["status"] = "TIMEOUT"
                    break
                stop.wait(.25)
                after = backend.get_frame()
                self.task.validate_native_frame(after)
                publish_action_frame("after", after)
                if save_screenshots:
                    self.task.write_raw_png(output / "after.png", after)
                if self.task.exit_is_set():
                    stop.set()
        except Exception as exc:
            record["failure_reason"] = str(exc)[:600]
            from src.rwtd.android_core.workflow_model import ExclusionIntersection
            if isinstance(exc,ExclusionIntersection) and not record['input_attempted']:
                record['exclusion_skipped']=True
                record['exclusion_details']={'points':exc.points,'region':exc.region}
            record['retryable_target_miss'] = (isinstance(exc,TargetNotFound)
                and not record['input_attempted'] and record['device_inputs']==0)
            from src.rwtd.android_core.step_conditions import ConditionRejected, ConditionUnknown
            device_stop = getattr(getattr(self.task, 'controller', None), 'device_stop', None)
            cancelled = (stop.is_set() or (device_stop is not None and device_stop.is_set())
                         or self.task.exit_is_set())
            safe_unsent = not record['input_attempted'] and record['device_inputs'] == 0 and not cancelled
            confirmed_prior_inputs = (record['input_attempted'] and record['input_command_returned']
                and all(not item['input_attempted'] or item['input_command_returned']
                        for item in record['repetitions']) and not cancelled)
            condition_skip = isinstance(exc, ConditionRejected) and safe_unsent
            unknown_skip = (isinstance(exc, (TargetNotFound, ConditionUnknown)) and (safe_unsent or confirmed_prior_inputs)
                            and parameters.get('unknown_no_retry') is True)
            if isinstance(exc, ConditionUnknown) and safe_unsent:
                record['retryable_target_miss'] = True
            if condition_skip or unknown_skip:
                record.update(condition_skipped=True, status='SKIPPED', action_completed=False,
                              skip_reason=record['failure_reason'], failure_reason=None, retryable_target_miss=False)
            if record["repetitions"]:
                record["repetitions"][-1]["failure_reason"] = record["failure_reason"]
            if isinstance(exc, ManualActionInterrupted):
                record["interrupted"] = True
            if record["input_attempted"]:
                record["status"] = "INPUT_SENT_UNVERIFIED" if record["input_command_returned"] else "INPUT_DELIVERY_UNKNOWN"
                if not record["input_command_returned"]:
                    record["device_inputs"] = None
                    if record["repetitions"]:
                        record["repetitions"][-1]["status"] = "INPUT_DELIVERY_UNKNOWN"
                # A later COCO repetition may disappear after an earlier gesture
                # returned normally. That is a bounded target miss, not uncertain
                # delivery, and must not latch the shared Trigger stop event.
                confirmed_target_miss = (isinstance(exc, (TargetNotFound, ConditionUnknown))
                    and record["input_command_returned"]
                    and all(not item["input_attempted"] or item["input_command_returned"]
                            for item in record["repetitions"]))
                if not isinstance(exc, ManualActionInterrupted) and not confirmed_target_miss:
                    stop.set()  # No next action after an uncertain execution/capture failure.
                    if not omit_records:
                        self.task.report_manual_failure(record)
            elif not record.get('condition_skipped'):
                record["status"] = "BLOCKED"
            if record.get('condition_skipped'):
                record['status'] = 'SKIPPED'
        finally:
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            evidence=getattr(self.task,'failure_evidence',None)
            evidence_frame=locals().get('before')
            if evidence is not None and evidence_frame is not None:
                try:
                    from src.rwtd.android_core.evidence_overlay import regions
                    details={'target_conditions':record.get('target_conditions'),
                        'detections':record.get('detection',[])}
                    if len(record.get('repetitions',[]))<=1:
                        details['input_conditions']=record.get('input_conditions')
                    items=regions(details)
                    if not items and not parameters.get('target_conditions'):
                        kind='ocr' if parameters.get('ocr_target') else 'coco'
                        if (parameters.get('ocr_target') or candidate_features(parameters)
                                and parameters.get('coco_roi') is not None
                                and not parameters.get('coco_feature_rois')):
                            roi=parameters.get('coco_roi') or [0,0,1080,2400]
                            effective=getattr(self.task,'recognition_roi',None)
                            items=[{'kind':kind,'label':parameters.get('ocr_target',{}).get('text')
                                or '|'.join(candidate_features(parameters)),
                                'roi':effective(roi,kind) if callable(effective) else roi}]
                    evidence.annotate(getattr(self,'_evidence_node_id',None) or parameters.get('step_id') or record['module'],
                                      evidence_frame,items)
                except Exception:pass
            record["report_path"] = None if omit_records else str(path)
            self.last_report = record
            self.reports[report_id] = record
            record["module_result"] = self.workflow_result(record.get("workflow_module"), record.get("run_id"))
            if not omit_records:
                atomic_json(output / "execution.json", {"report_id": report_id, "status": record["status"],
                                                    "input_attempted": record["input_attempted"], "device_inputs": record["device_inputs"],
                                                    "repeat_count_requested": record["repeat_count_requested"],
                                                    "repeat_count_completed": record["repeat_count_completed"],
                                                    "inputs_attempted": record["inputs_attempted"], "inputs_confirmed": record["inputs_confirmed"],
                                                    "verification": record.get("verification"), "failure_reason": record["failure_reason"]})
                self.storage.finish(record)
                # self.reports already holds this record by reference, including
                # finish() metadata. Reloading every historical JSON here blocks
                # the device owner on each recognition retry. The bounded cache
                # is updated incrementally; disk history is loaded at startup
                # or after explicit storage cleanup, never per input attempt.
        return record

    def detect_target_features(self, frame, parameters):
        from src.rwtd.android_core.step_conditions import ConditionUnknown
        try:
            return self._detect_target_features(frame, parameters)
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            raise ConditionUnknown('Target recognition invalid: '+str(exc)[:300]) from exc

    def _composed_targets(self,frame,parameters,record):
        from src.rwtd.android_core.target_conditions import evaluate
        from src.rwtd.android_core.step_conditions import ConditionUnknown
        hits,report=evaluate(self.task,frame,parameters)
        record['target_conditions']=report
        for row in report['clauses']:
            if row.get('type')=='padding_coco':
                logger=getattr(self.task,'log_info',None)
                if callable(logger):
                    logger('RWTD_PADDING_COCO '+json.dumps({'step':parameters.get('step_id'),
                        'result':row['result'],'check_feature':row.get('check_feature'),
                        'selection_mode':row.get('selection_mode','paired'),'requested_index':row.get('requested_index'),
                        'selected_index':row.get('selected_index'),'bypassed_b':row.get('bypassed_b',False),
                        'match':row.get('match','disappeared'),'reason':row.get('reason'),
                        'candidates':row.get('candidates',[])},ensure_ascii=False,default=str))
        record['detection']=[dict(hit,target_row=index) for index,row in enumerate(report['clauses'])
                             for hit in row.get('detections',[])]
        if parameters.get('target_policy')=='unique_only' and len(hits)>1:
            raise ConditionUnknown('Multiple composed target positions; unique-only policy sends no input')
        if report['result']=='UNKNOWN':raise ConditionUnknown('Target conditions UNKNOWN; no input sent')
        if not hits:raise TargetNotFound('Target conditions/position policy found no eligible position')
        return hits

    @staticmethod
    def _resolve_target(callback, *args, **kwargs):
        from src.rwtd.android_core.step_conditions import ConditionUnknown
        try:
            return callback(*args, **kwargs)
        except TargetNotFound:
            raise
        except (ValueError, TypeError) as exc:
            raise ConditionUnknown('Target localization invalid: '+str(exc)[:300]) from exc

    def _recognition_frame(self, backend):
        from src.rwtd.android_core.step_conditions import ConditionUnknown
        try:
            frame = backend.get_frame()
            self.task.validate_native_frame(frame)
            return frame
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            raise ConditionUnknown('Recognition frame unavailable: '+str(exc)[:300]) from exc

    def _detect_target_features(self, frame, parameters):
        """Different Feature ROIs use the same captured frame; never add captures."""
        names = set(candidate_features(parameters))
        scopes = parameters.get('coco_feature_rois')
        if scopes is None:
            return self.task.detect_manual_features(frame,names,parameters['threshold'],
                **({'coco_roi':parameters['coco_roi']} if parameters.get('coco_roi') else {}))
        found=[]
        for feature in sorted(names):
            roi=scopes.get(feature,parameters.get('coco_roi'))
            found.extend(self.task.detect_manual_features(frame,{feature},parameters['threshold'],
                **({'coco_roi':roi} if roi is not None else {})))
        return found

    def execute_canvas_action(self, node, stop):
        """Private owner-thread dispatch, tied to the explicit active Canvas effect."""
        import copy
        import threading
        controller = self.task.controller
        runtime = controller.canvas_runtime
        if (controller.owner != threading.get_ident() or controller.mode != 'Manual Device Test'
                or not controller.allow_manual or runtime.source != 'android_manual'
                or runtime.status != 'RUNNING' or runtime.pending_effect != node
                or stop.is_set() or controller.device_stop.is_set()):
            raise ValueError('Canvas action has no explicit manual execution grant')
        from src.rwtd.android_core.canvas_action import bind_action
        plan = bind_action(node,runtime.variables,runtime.modules,runtime.graph.get('settings',{}).get('feature_rois',{}))
        if plan != runtime.action_plan:
            raise ValueError('Canvas action input changed before execution')
        current = self.store.get(node['module'])
        if current.get('revision') != plan['binding']['module_revision']:
            raise ValueError('Referenced module changed; Stop and restart the Canvas flow')
        return self.execute_canvas_steps(plan['steps'],node['module'],
            plan['binding']['module_revision'],stop,binding=plan['binding'],loop_settings=runtime.graph.get('settings',{}),
            guard_node=node,guard_variables=runtime.variables,first_guard_checked=True)

    def execute_trigger_request(self, request, stop):
        """Owner-only queued dispatch; permission comes from explicit watcher activation."""
        import copy
        import threading
        controller = self.task.controller
        scheduler = controller.trigger_scheduler
        watcher = scheduler.tasks.get(request.get('task_id'))
        if (controller.owner != threading.get_ident() or controller.trigger_work != request
                or controller.mode != 'Manual Device Test' or not controller.allow_manual
                or scheduler.source != 'android_manual' or not scheduler.queue_inputs
                or not watcher or watcher['status'] != 'RUNNING'
                or request['run_id'] != scheduler.run_id or request['generation'] != watcher['generation']
                or not any(q['id'] == request['id'] and q['status'] == 'RUNNING' for q in scheduler.queue)
                or stop.is_set() or controller.device_stop.is_set()):
            raise ValueError('TriggerTask queue has no active execution grant')
        module = self.store.get(request['module'])
        if module.get('revision') != request['module_revision']:
            raise ValueError('Queued module revision changed; restart monitoring')
        binding = None
        if request['kind'] == 'action':
            from src.rwtd.android_core.canvas_action import bind_action
            plan = bind_action(request['definition'],request['variables'],scheduler.modules,scheduler.graph.get('settings',{}).get('feature_rois',{}))
            steps, binding = plan['steps'], plan['binding']
        else:
            from src.rwtd.android_core.workflow_model import resolve_step
            steps = [resolve_step(module,copy.deepcopy(s)) for s in module['steps']]
            from src.rwtd.android_core.canvas_repeat import runs
            steps=[copy.deepcopy(s) for _ in range(runs(request['definition'])) for s in steps]
        return self.execute_canvas_steps(steps,request['module'],request['module_revision'],
                                        stop,binding=binding,request=request,
                                        loop_settings=scheduler.graph.get('settings',{}) if binding or request['definition'].get('entry_guard') else None,
                                        guard_node=request['definition'],guard_variables=request['variables'])

    def check_execution_entry_guard(self,node,variables,settings,stop,module_id,revision):
        """Check only an explicitly configured Guard, once per actual macro round."""
        from src.rwtd.android_core.canvas_entry_guard import probe,evaluate,needs_frame,result_allowed
        guard=node['entry_guard'];attempt=0
        from src.rwtd.android_core.canvas_entry_guard import retry_limit
        maximum=retry_limit(node,settings)
        while True:
            if self.time_guards.blocked(node):return False
            controller=self.task.controller
            if (stop.is_set() or controller.device_stop.is_set() or self.task.exit_is_set()
                    or self.store.get(module_id).get('revision')!=revision):
                return None
            if not needs_frame(guard):
                allowed=evaluate(guard,variables,lambda c:None,lambda c:self.time_guards.allowed(node,c))
            else:
                result=self.task.canvas_effect({'node':probe(node,settings,attempt+1),'source':'android_manual',
                                               'fresh_frame':True,'variables':variables},stop)
                allowed=result_allowed(result)
            if stop.is_set() or controller.device_stop.is_set():
                return None
            if allowed is True:
                return True
            if allowed is False:
                return False
            attempt+=1
            if node.get('unknown_no_retry') is True:
                return False
            if maximum is not None and attempt>=maximum:
                exhausted=getattr(self.task,'on_exhaustion',None)
                if callable(exhausted):
                    try:exhausted({'kind':'recognition','node':node['id'],'module':module_id,
                        'event_id':'guard:'+uuid.uuid4().hex,'attempt_count':attempt,'attempt_limit':maximum,
                        'reason':'Guard UNKNOWN 重試耗盡','source':'android_manual',
                        'notify_discord':getattr(self,'canvas_notify_discord',False)})
                    except Exception:pass
                return False
            stop.wait(settings.get('round_wait_seconds',1))

    def _macro_tail(self, steps, start, end):
        """Recognition, three-point gestures and verified outcomes split a macro."""
        from src.rwtd.android_core.device_macro import eligible
        if not getattr(self.task, 'supports_device_macro', False):
            return []
        tail=[]
        operations=0
        for step in steps[start:end]:
            if step.get('input_conditions') or not eligible(step):
                break
            # Split before an excluded input so it receives the normal local
            # skip result; safe inputs before/after remain eligible for batches.
            if step.get('name')!='WaitTest':
                runtime=getattr(getattr(self.task,'controller',None),'canvas_runtime',None)
                regions=getattr(runtime,'exclusion_regions',{})
                if regions:
                    from src.rwtd.android_core.workflow_model import require_outside_exclusions,ExclusionIntersection
                    points=[[step['x'],step['y']]]
                    if step.get('name')=='SwipeCalibration':points.append([step['end_x'],step['end_y']])
                    try:require_outside_exclusions(points,regions)
                    except ExclusionIntersection:break
            count=step.get('repeat_count',1)
            if tail and operations+count>100:
                break  # Bound ADB shell command size; still owned by the same node.
            tail.append(step)
            operations+=count
        return tail

    def _execute_device_macro(self, steps, module_id, revision, stop, *, run_id=None, node_id=None):
        from src.rwtd.android_core.device_macro import execute
        from src.rwtd.android_core.workflow_model import require_outside_exclusions
        # Entire segment is validated before any Android input.
        self._check_gesture_boundary(stop)
        prepared=[]
        for step in steps:
            payload=dict(step)
            payload.pop('dashboard_manual',None)
            payload.pop('workflow_module',None)
            payload=self.display_mapping.apply(self.task,module_id,payload)
            prepared.append(self.validate_step(dict(payload,reversible_ack=True)))
        self._check_gesture_boundary(stop)
        backend, device=self.task.manual_backend()
        if run_id and self.module_run:
            if 'device' in self.module_run and self.module_run['device']!=device:
                raise ValueError('Device changed within module run')
            self.module_run['device']=device
        runtime=getattr(self.task.controller,'canvas_runtime',None)
        regions=getattr(runtime,'exclusion_regions',{})
        for step in prepared:
            if step['name']=='WaitTest':
                continue
            points=[[step['x'],step['y']]]
            if step['name']=='SwipeCalibration':
                points.append([step['end_x'],step['end_y']])
            require_outside_exclusions(points,regions)
        def cancelled():
            controller=self.task.controller
            if (stop.is_set() or self.task.exit_is_set() or controller.device_stop.is_set()
                    or controller.module_pause.is_set()
                    or self.store.get(module_id).get('revision')!=revision):
                return True
            current_backend,current_device=self.task.manual_backend()
            return current_backend is not backend or current_device!=device
        result=execute(self.task,prepared,cancelled,
                       context={'module':module_id,'revision':revision,'node':node_id,'run_id':run_id})
        from src.rwtd.android_core.log_policy import timing_start,timing_mark
        bookkeeping_started=timing_start(self.task)
        timing_mark(self.task,'MACRO_BOOKKEEPING_START',segment=result['segment_id'],
                    node=node_id,module=module_id,run_id=run_id)
        if result['failure'] and len(result['completed'])==len(prepared):
            result['completed'].discard(len(prepared)-1)
        records=[]
        for index,step in enumerate(prepared):
            # Include the first cancelled unsent step as a blocked boundary.
            # Otherwise the caller might attempt to start a new segment there.
            attempted=result['attempted'].get(index,0)
            returned=result['returned'].get(index,0)
            completed=index in result['completed']
            uncertain=bool(result['failure'] and not completed)
            report_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')+'_'+uuid.uuid4().hex[:8]
            omit=(not getattr(self,'canvas_save_reports',True) or
                  (module_id,revision,step.get('step_id')) in getattr(self,'canvas_verified_steps',set()))
            record={'report_id':report_id,'module':step['name'],'timestamp_utc':datetime.now(timezone.utc).isoformat(),
                'finished_utc':datetime.now(timezone.utc).isoformat(),'mode':'Manual Device Test','simulated':False,
                'status':'INPUT_SENT_UNVERIFIED' if completed else 'BLOCKED',
                'input_attempted':bool(attempted),'input_command_returned':bool(returned) and attempted==returned,
                'device_inputs':None if uncertain else returned,'before':None,'after':None,
                'action_completed':completed,'inputs_attempted':attempted,'inputs_confirmed':returned,
                'repeat_count_requested':step['repeat_count'],
                'repeat_count_completed':step['repeat_count'] if completed else returned,
                'repetitions':[],'detection':[],'parameters':step,'device':device,
                'failure_reason':None if completed else result['failure'] or 'Macro stopped at gesture boundary',
                'discord_enabled':False,'reports_saved':not omit,'manual_confirmation_only':True,
                'workflow_module':module_id,'workflow_revision':revision,'step_id':step.get('step_id'),
                'screenshot_policy':'COCO_ONLY','screenshots_required':False,
                'transport':'official_adb_macro','per_input_response_ignored':True}
            record['stop_policy']='finish_current_gesture'
            record['stop_confirmed']=result['stop_confirmed']
            record['macro_segment_id']=result['segment_id']
            record['macro_sent_utc']=result.get('sent_utc')
            record['macro_received_utc']=result.get('received_utc')
            # Segment totals are recorded once, not summed once per step.
            if index==0:
                record['macro_timing_seconds']=dict(result['timing_seconds'])
            if run_id:
                record['run_id']=run_id
            record['report_path']=None if omit else str(self.storage.path('session_'+report_id+'/report.json'))
            self.reports[report_id]=record
            self.last_report=record
            if not omit:
                output=self.storage.begin(report_id)
                atomic_json(output/'execution.json',{key:record[key] for key in
                    ('report_id','status','input_attempted','device_inputs','repeat_count_requested',
                     'repeat_count_completed','inputs_attempted','inputs_confirmed','failure_reason','transport')})
                self.storage.finish(record)
            records.append(record)
            if not completed:
                break
        timing_mark(self.task,'MACRO_BOOKKEEPING_DONE',bookkeeping_started,
                    segment=result['segment_id'],node=node_id,module=module_id,run_id=run_id,
                    record_count=len(records))
        return records

    def execute_step_test(self,payload,stop):
        """One saved Step, using the same retry runner as Canvas/Farm."""
        parameters=dict(payload)
        parameters.pop('dashboard_manual',None)
        parameters=self.validate_step(parameters)
        module_id=parameters['workflow_module']
        saved=self.store.get(module_id)
        selected=next((step for step in saved['steps'] if step['step_id']==parameters.get('step_id')),None)
        if selected is None:raise ValueError('Configured step missing')
        expected=self.validate_step(dict(selected,reversible_ack=True,workflow_module=module_id))
        if parameters!=expected:
            raise ValueError('Parameters differ from saved step configuration; reload before testing')
        graph=self.decision_store.load() or {}
        from src.rwtd.android_core.loop_settings_override import apply_ok_settings
        settings=apply_ok_settings(graph,getattr(self.task.controller,'loop_settings_override',None)).get('settings',{})
        result=self.execute_canvas_steps([resolve_step(saved,selected)],module_id,saved['revision'],
                                        stop,loop_settings=settings)
        if 'records' not in result:
            result=dict(result,records=[dict(result)])
        # The shared runner receives a resolved Step, so preserve its saved TAG
        # in the dashboard response along with the effective execution ROI.
        if selected.get('roi_tag'):
            result['resolved_tag']=selected['roi_tag']
            result['resolved_roi']=result.get('parameters',{}).get('coco_roi')
        # Range/remaining control is observable, but no later Step is scheduled
        # by this explicit single-Step request.
        result['single_step_test']=True
        result['test_loop_settings']={key:settings.get(key,default) for key,default in
                                     (('max_cycles',10),('round_wait_seconds',1))}
        return result

    def execute_canvas_steps(self, steps, module_id, revision, stop, binding=None, request=None, loop_settings=None,
                             guard_node=None,guard_variables=None,first_guard_checked=False):
        from src.rwtd.android_core.trigger_preemption import NodeStop
        token = NodeStop(stop,self.task.controller,request)
        try:
            result = self._execute_canvas_steps(steps,module_id,revision,token,binding,request,loop_settings,
                                                guard_node,guard_variables,first_guard_checked)
        except ManualActionInterrupted:
            if not token.preempted:
                raise
            result={'records':[],'device_inputs':0}
        if token.preempted and not stop.is_set() and not self.task.controller.device_stop.is_set():
            # No tail/repetition is resumed, and cancellation is not evidence
            # that the game action or its completion Guard succeeded.
            progress=getattr(self.task,'farm_action_progress',None)
            if progress is not None and binding:
                progress.pop((module_id,binding.get('node'),binding.get('step_id')),None)
            result=dict(result,status='CANCELLED',preempted=True,action_completed=False,
                        failure_reason=None,cancel_reason='插隊取消目前節點的所有剩餘動作')
            node_id=(binding or {}).get('node') or (request or {}).get('node') or (guard_node or {}).get('id')
            message=str(node_id or module_id)+' / '+module_id+' · '+result['cancel_reason']
            logger=getattr(self.task,'log_info',None)
            if callable(logger):logger('NODE_PREEMPTED '+message)
            event=getattr(self.task,'dashboard_event',None)
            if callable(event):event('NODE_PREEMPTED',message)
        return result

    def _prepare_step_retry(self,step,module_id,revision,stop,node_id=None):
        from src.rwtd.android_core.step_loop import before
        previous=getattr(self,'_evidence_node_id',None)
        self._evidence_node_id=node_id or previous or module_id
        try:return before(self,step,module_id,revision,stop)
        finally:self._evidence_node_id=previous

    def _execute_canvas_steps(self, steps, module_id, revision, stop, binding=None, request=None, loop_settings=None,
                              guard_node=None,guard_variables=None,first_guard_checked=False):
        """Retry only an empty target before input; never replay completed steps."""
        import copy
        if not steps:
            raise ValueError('Queued module has no steps')
        prepared=[]
        # Validate the entire tail before the first input, rather than discover
        # an invalid later operation after already executing the entry step.
        for step in steps:
            parameters=copy.deepcopy(step)
            parameters.pop('workflow_module',None)
            parameters.update(reversible_ack=True)
            validated=self.validate_step(parameters)
            # Execution-envelope flag is consumed by handle(), not a saved
            # operation parameter. Add it only after strict parameter checks.
            validated['dashboard_manual']=True
            prepared.append(validated)
        records = []
        # Farm retries a partially completed action at the uncompleted step.
        # This state is task-local and is cleared when its chain hands off.
        farm_resume = getattr(self.task, 'farm_action_progress', None) if binding else None
        farm_key = (module_id, binding.get('node'), binding.get('step_id')) if farm_resume is not None else None
        start_index = farm_resume.get(farm_key, 0) if farm_resume is not None else 0
        def input_count():
            counts = [r.get('device_inputs') for r in records]
            return None if any(c is None for c in counts) else sum(counts)
        fixed_runs=(binding or (request or {}).get('definition',{})).get('repeat_runs',1)
        round_size=len(prepared)//fixed_runs
        from src.rwtd.android_core.step_loop import is_loop,blocked_record
        macro_records={}
        condition_skips={}
        condition_remaining=None
        for index,parameters in enumerate(prepared):
            if index < start_index:
                continue
            if index not in macro_records and (stop.is_set() or self.task.controller.device_stop.is_set()
                    or self.task.exit_is_set() or self.store.get(module_id).get('revision')!=revision):
                return {'status':'BLOCKED','action_completed':False,'records':records,
                        'failure_reason':'Sequence stopped or referenced module changed',
                        'device_inputs':input_count()}
            if index%round_size==0:condition_skips.clear()
            origin=condition_remaining or condition_skips.get(parameters.get('step_id'))
            if origin:
                from src.rwtd.android_core.step_control import skipped_record
                records.append(skipped_record(parameters,module_id,revision,origin))
                if farm_resume is not None:farm_resume[farm_key]=index+1
                continue
            if index and fixed_runs>1 and index%round_size==0:
                stop.wait((loop_settings or {}).get('round_wait_seconds',1))
            if index%round_size==0 and guard_node and guard_node.get('entry_guard') and not (index==0 and first_guard_checked):
                allowed=self.check_execution_entry_guard(guard_node,guard_variables or {},loop_settings or {},stop,module_id,revision)
                if allowed is None:
                    return {'status':'BLOCKED','action_completed':False,'device_inputs':input_count(),
                            'failure_reason':'附加 Guard 檢查已取消或失敗'}
                if not allowed:
                    return {'status':'SKIPPED','guard_skipped':True,'action_completed':True,
                            'device_inputs':input_count(),'completed_steps':len(records),'steps':records}
            allowed,retry_report,repeated=self._prepare_step_retry(parameters,module_id,revision,stop,
                (binding or {}).get('node') or (request or {}).get('node') or (guard_node or {}).get('id'))
            records.extend(repeated)
            if not allowed:
                record=blocked_record(parameters,module_id,revision,retry_report,repeated)
                self.reports[record['report_id']]=record
                records.append(record)
                return dict(record,records=records,device_inputs=input_count(),completed_step_index=index)
            attempts=[]
            attempt_count=0
            maximum=(loop_settings or {}).get('max_cycles',10) if loop_settings is not None else 1
            if is_loop(parameters):maximum=1
            interval=(loop_settings or {}).get('round_wait_seconds',1)
            if index not in macro_records:
                tail=self._macro_tail(prepared,index,min(len(prepared),(index//round_size+1)*round_size))
                for offset,candidate in enumerate(tail):
                    if candidate.get('step_id') in condition_skips:
                        tail=tail[:offset];break
                if tail:
                    node_id=(binding or {}).get('node') or (request or {}).get('node') or (guard_node or {}).get('id')
                    for offset,record in enumerate(self._execute_device_macro(tail,module_id,revision,stop,node_id=node_id)):
                        macro_records[index+offset]=record
            while True:
                if index not in macro_records and (stop.is_set() or self.task.controller.device_stop.is_set() or self.task.exit_is_set()
                        or self.store.get(module_id).get('revision') != revision):
                    return {'status':'BLOCKED','action_completed':False,'records':records,
                            'failure_reason':'Sequence stopped or referenced module changed',
                            'device_inputs':input_count()}
                # handle copies parameters and captures a fresh frame on every
                # attempt. A previously successful tail step is never replayed.
                previous_omit=getattr(self,'_omit_execution_records',False)
                previous_condition_module=getattr(self,'_condition_module_id',None)
                previous_retry_authorized=getattr(self,'_step_retry_authorized',None)
                self._step_retry_authorized=(module_id,parameters.get('step_id')) if retry_report else None
                self._condition_module_id=module_id
                self._omit_execution_records=(not getattr(self,'canvas_save_reports',True)
                    or (module_id,revision,parameters.get('step_id')) in getattr(self,'canvas_verified_steps',set()))
                try:
                    previous_evidence_node=getattr(self,'_evidence_node_id',None)
                    self._evidence_node_id=((binding or {}).get('node') or (request or {}).get('node')
                        or (guard_node or {}).get('id') or module_id)
                    record = macro_records.pop(index) if index in macro_records else self.handle('execute',copy.deepcopy(parameters),stop,_dispatch_token=self._canvas_dispatch_token)
                finally:
                    self._omit_execution_records=previous_omit
                    self._condition_module_id=previous_condition_module
                    self._step_retry_authorized=previous_retry_authorized
                    self._evidence_node_id=previous_evidence_node
                record.update(workflow_module=module_id,workflow_revision=revision,step_id=parameters.get('step_id'))
                if retry_report:
                    record['step_retry']=retry_report
                    record['input_conditions']=copy.deepcopy(retry_report['conditions'])
                attempt_count+=1
                attempts.append({'report_id':record['report_id'],'status':record['status']})
                attempts=attempts[-100:]
                record['recognition_attempts']=copy.deepcopy(attempts)
                record['recognition_attempt_count']=attempt_count
                # An unsent target miss is a skipped step even when the queued
                # Trigger has no loop settings (one allowed attempt).
                record['recognition_retry_exhausted']=(record.get('retryable_target_miss') is True
                    and maximum is not None and attempt_count>=maximum)
                can_retry=(record.get('retryable_target_miss') is True and not record.get('input_attempted')
                    and record.get('device_inputs')==0 and (maximum is None or attempt_count<maximum))
                if not can_retry:
                    break
                if record.get('reports_saved',True):
                    self.storage.finish(record)
                event=getattr(self.task,'dashboard_event',None)
                if event:
                    event('RETRY',f"COCO target absent · attempt {attempt_count}/{maximum or 'unlimited'} · retry after {interval}s")
                stop.wait(interval)
            exclusion_skipped=record.get('exclusion_skipped') is True
            condition_skipped=record.get('condition_skipped') is True
            control=record.get('condition_control')
            from src.rwtd.android_core.step_control import applies_to_tail
            if applies_to_tail(record):
                from src.rwtd.android_core.step_control import skipped_ids,log as log_condition_control
                if control['action'] in {'skip_remaining','skip_remaining_now'}:
                    condition_remaining=control
                    affected=[p['step_id'] for p in prepared[index+1:]]
                else:
                    affected=skipped_ids(control,self.store.get(module_id)['steps'],parameters['step_id'])
                    for step_id in affected:condition_skips[step_id]=control
                log_condition_control(self.task,module_id,parameters['step_id'],control,affected)
            target_skipped=record.get('recognition_retry_exhausted') is True
            if exclusion_skipped:
                runtime=getattr(self.task.controller,'canvas_runtime',None)
                node_id=(binding or {}).get('node') or (request or {}).get('node') or (guard_node or {}).get('id') or module_id
                details=record.get('exclusion_details',{})
                reason=(f"請修改 INPUT：Canvas revision {getattr(runtime,'graph',{}).get('revision') if runtime else None} / "
                    f"Chain {getattr(runtime,'graph',{}).get('entry') if runtime else None} / {node_id} / "
                    f"{module_id}@{revision} / Step {parameters.get('step_id')} / {parameters['name']}；"
                    f"路徑 {details.get('points')} 與禁止區域 {details.get('region')} 相交，未送出並跳過")
                record.update(status='SKIPPED',target_skipped=True,skip_reason=reason,
                              failure_reason=None,action_completed=False)
                event=getattr(self.task,'dashboard_event',None)
                if callable(event):event('SKIPPED',f"{module_id}/{parameters.get('step_id')} · {reason}")
                report=getattr(self.task,'on_exhaustion',None)
                if callable(report):
                    try:report({'kind':'exclusion','node':(binding or {}).get('node') or (request or {}).get('node')
                        or (guard_node or {}).get('id') or module_id,'event_id':'exclusion:'+record['report_id'],
                        'source':'android_manual','module':module_id,'module_revision':revision,
                        'step_id':parameters.get('step_id'),'action':parameters['name'],
                        'canvas_revision':getattr(runtime,'graph',{}).get('revision') if runtime else None,
                        'chain':getattr(runtime,'graph',{}).get('entry') if runtime else None,
                        'reason':reason,'exclusion':record.get('exclusion_details'),
                        'notify_discord':getattr(self,'canvas_notify_discord',False)})
                    except Exception:pass
            if target_skipped and not is_loop(parameters):
                exhausted=getattr(self.task,'on_exhaustion',None)
                if callable(exhausted):
                    try:
                        exhausted({'kind':'recognition','node':(binding or {}).get('node') or (request or {}).get('node')
                            or (guard_node or {}).get('id') or module_id,
                            'event_id':'target:'+record['report_id'],'module':module_id,
                            'step_id':parameters.get('step_id'),'attempt_count':attempt_count,
                            'module_revision':revision,
                            'recognition':{'features':candidate_features(parameters),'ocr_target':parameters.get('ocr_target'),'target_conditions':record.get('target_conditions'),
                                'threshold':parameters.get('threshold'),
                                'roi':parameters.get('coco_roi'),
                                'feature_rois':parameters.get('coco_feature_rois'),
                                'color':record.get('color_target_evidence'),
                                'detections':record.get('detection',[])},
                            'attempt_limit':maximum,'reason':record.get('failure_reason') or 'COCO 目標重試耗盡',
                            'source':'android_manual','notify_discord':getattr(self,'canvas_notify_discord',False)})
                    except Exception:pass
                record.update(status='SKIPPED',target_skipped=True,
                              skip_reason=record.get('failure_reason') or 'COCO 目標未命中；重試耗盡',
                              failure_reason=None,recognition_retry_exhausted=False)
                event=getattr(self.task,'dashboard_event',None)
                if event:
                    event('SKIPPED',f"{module_id}/{parameters.get('step_id')} · COCO 重試耗盡；跳過此動作")
            if is_loop(parameters) and (target_skipped or exclusion_skipped or condition_skipped):
                record.update(status='BLOCKED',action_completed=False,
                              failure_reason='Conditional Step gate passed but action was not delivered',
                              retryable_target_miss=False,recognition_retry_exhausted=False)
                target_skipped=exclusion_skipped=condition_skipped=False
            if request:
                record['trigger_binding'] = {'task_id':request['task_id'],'node':request['node'],
                                             'module':module_id,'module_revision':revision}
            if binding:
                record['canvas_binding'] = copy.deepcopy(binding)
            if record.get('reports_saved',True):
                self.storage.finish(record)
            self.reports[record['report_id']] = record
            records.append(record)
            if target_skipped or exclusion_skipped or condition_skipped:
                if farm_resume is not None:
                    farm_resume[farm_key]=index+1
                continue
            if (farm_resume is not None and record.get('action_completed') and not record.get('failure_reason')
                    and record.get('status') in {'SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'}):
                farm_resume[farm_key] = index+1
            if (not record.get('action_completed') or record.get('failure_reason')
                    or record.get('status') not in {'SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'}):
                return dict(record, status=record['status'],
                            action_completed=False,records=records,device_inputs=input_count(),completed_step_index=index)
        if farm_resume is not None:
            farm_resume.pop(farm_key, None)
        skipped=sum(r.get('target_skipped') is True or r.get('condition_skipped') is True for r in records)
        if skipped:
            return dict(records[-1],status='SKIPPED' if skipped==len(records) else 'INPUT_SENT_UNVERIFIED',
                        action_completed=True,failure_reason=None,recognition_retry_exhausted=False,
                        target_skipped=skipped==len(records),skipped_steps=skipped,
                        device_inputs=input_count(),records=records)
        if len(records)==1:
            return records[0]
        return dict(records[-1],status='INPUT_SENT_UNVERIFIED',action_completed=True,
                    device_inputs=input_count(),records=records)

    def workflow_result(self, name, run_id=None):
        if not name or name not in self.workflows:
            return None
        workflow = self.workflows[name]
        statuses = []
        for step in workflow["steps"]:
            reports = [record for record in self.reports.values() if record.get("workflow_module") == name
                       and record.get("workflow_revision") == workflow["revision"] and record.get("run_id") == (run_id or self.workflow_runs.get(name)) and record.get("step_id") == step["step_id"]]
            statuses.append(reports[-1]["status"] if reports else "PENDING")
        return {"module": name, "status": module_outcome(statuses), "required_steps": len(statuses), "step_outcomes": statuses,
                "verification_sources": ["manual" if value == "MANUAL_VERIFIED" else "automatic" if value == "SUCCESS" else "unverified" for value in statuses]}

    def save_preset(self, payload):
        record = self.storage.load_report(payload.get("report_id"))
        if not record or payload.get("report_id") != record["report_id"] or record["status"] not in {"SUCCESS", "MANUAL_VERIFIED"}:
            raise ValueError("Explicit human confirmation for the current report is required")
        if (record["module"] not in TEST_NAMES or not record.get("action_completed") or record["failure_reason"]
                or (not screenshots_omitted(record) and (not record.get('after') or not Path(record['after']).is_file()))):
            raise ValueError("Only verified completed tests with valid evidence policy can become parameter presets")
        output = safe_path(self.profile.definitions_root, self.profile.data_root + "/presets")
        output.mkdir(parents=True, exist_ok=True)
        path = output / ("preset_" + record["report_id"] + "_" + uuid.uuid4().hex[:8] + ".json")
        atomic_json(path, {"name": str(payload.get("name", "Swipe preset"))[:80], "parameters": record["parameters"],
                          "human_verified": record["status"] == "MANUAL_VERIFIED", "automatically_verified": record["status"] == "SUCCESS",
                          "original_status": record["status"], "source_report": record["report_path"],
                          "native_size": [1080, 2400], "device": record.get("device"), "execution_enabled": False})
        return {"status": "SUCCESS", "preset_path": str(path), "note": "Human-verified preset saved; no input and no Auto Farming"}

    def reset_chain_test(self):
        self.chain_module_progress = {}
        self.task.farm_action_progress = {}

    def execute_chain_test_module(self,node,stop):
        import copy
        import threading
        from src.rwtd.android_core.workflow_model import resolve_step
        from src.rwtd.android_core.canvas_repeat import runs
        controller = self.task.controller
        if (not controller.canvas_runtime.unattended
                or controller.owner!=threading.get_ident()
                or controller.canvas_runtime.source!='android_manual'
                or controller.canvas_runtime.status!='RUNNING'
                or controller.canvas_runtime.pending_effect!=node
                or controller.mode!='Manual Device Test' or not controller.allow_manual
                or stop.is_set() or controller.device_stop.is_set()):
            raise ValueError('Continuous Canvas test input is not active')
        frozen = controller.canvas_runtime.modules[node['module']]
        module = self.store.get(node['module'])
        if module['revision']!=frozen['revision']:
            raise ValueError('Module changed; restart Chain test')
        steps=[resolve_step(module,copy.deepcopy(s)) for s in module['steps']]
        steps=[copy.deepcopy(s) for _ in range(runs(node)) for s in steps]
        progress=getattr(self,'chain_module_progress',{})
        completed=progress.get(node['id'],0)
        result=self.execute_canvas_steps(steps[completed:],module['id'],module['revision'],stop,
            loop_settings=controller.canvas_runtime.graph['settings'],guard_node=node,
            guard_variables=controller.canvas_runtime.variables,first_guard_checked=True)
        if result.get('action_completed') or result.get('preempted'):
            progress.pop(node['id'],None)
        else:
            records=result.get('records',[result])
            progress[node['id']]=completed+sum(bool(r.get('action_completed') and not r.get('failure_reason')) for r in records)
        self.chain_module_progress=progress
        return result

    def execute_module(self, payload, stop):
        if payload.get("explicit_execution") is not True or payload.get("reversible_ack") is not True:
            raise ValueError("Explicit complete-module initiation and reversible-target acknowledgment required")
        definition = self.store.get(payload.get("id"))
        if not definition["steps"]:
            raise ValueError("Module has no configured steps")
        manual_only = payload.get("dashboard_manual", False)
        post_run_verification=payload.get('post_run_verification',False)
        if type(post_run_verification) is not bool:
            raise ValueError('post_run_verification must be boolean')
        require_verification=False if post_run_verification else payload.get('require_manual_verification',True)
        if type(require_verification) is not bool:
            raise ValueError('require_manual_verification must be boolean')
        if type(manual_only) is not bool:
            raise ValueError("dashboard_manual must be boolean")
        if payload.get("resume"):
            run = self.module_run
            if not run or run["module"] != definition["id"] or run["revision"] != definition["revision"] or not run["active"]:
                raise ValueError("No matching paused module run")
            if payload.get('expected_run_id',run['run_id'])!=run['run_id']:
                raise ValueError('Queued continuation belongs to a different module run')
            controller=getattr(self.task,'controller',None)
            if payload.get('expected_run_id') and controller and controller.module_pause.is_set():
                run['status']='PAUSED'
                return {'status':'PAUSED','module_run':dict(run)}
            if manual_only != run.get("manual_confirmation_only", False):
                raise ValueError("Cannot change confirmation mode within a module run")
            if require_verification!=run.get('require_manual_verification',True):
                raise ValueError('Cannot change verification selection within a module run')
            if post_run_verification!=run.get('post_run_verification',False):
                raise ValueError('Cannot change post-run review mode within a module run')
            previous = self.reports.get(run.get("waiting_report"))
            if require_verification and run.get("status") != "PAUSED" and (not previous or previous["status"] not in {"SUCCESS", "MANUAL_VERIFIED"}):
                raise ValueError("Confirm the paused step before continuing")
            if require_verification and manual_only and run.get("waiting_report") and (not previous or previous["status"] != "MANUAL_VERIFIED"):
                raise ValueError("Every Dashboard step requires explicit human Pass")
            if hasattr(self.task, "controller"):
                self.task.controller.module_pause.clear()
            run["status"] = "RUNNING"
        else:
            if self.module_run and self.module_run.get("active"):
                raise ValueError("Stop the current module run before beginning another")
            previous_run=self.module_run
            flagged={}
            if previous_run and previous_run.get('post_run_verification') and previous_run['module']==definition['id'] and previous_run['revision']==definition['revision']:
                flagged={r['step_id']:r['report_id'] for r in self.reports.values()
                         if r.get('run_id')==previous_run['run_id'] and r.get('manual_verification',{}).get('pass') is False}
            run = {"module": definition["id"], "revision": definition["revision"], "run_id": uuid.uuid4().hex,
                   "next_step": 0, "active": True, "status": "RUNNING", "manual_confirmation_only": manual_only,
                   'require_manual_verification':require_verification,'post_run_verification':post_run_verification,
                   'failure_report_steps':flagged}
            self.module_run = run
            self.workflow_runs[definition["id"]] = run["run_id"]
            self.storage.protected_runs.add(run["run_id"])
            if hasattr(self.task, "controller"):
                self.task.controller.module_pause.clear()
        macro_records={}
        from src.rwtd.android_core.step_loop import blocked_record
        while run["next_step"] < len(definition["steps"]):
            if run['next_step'] not in macro_records and (stop.is_set() or self.task.exit_is_set()):
                run.update(active=False, status="STOPPED")
                self.storage.protected_runs.discard(run["run_id"])
                return {"status": "STOPPED", "module_run": dict(run)}
            if run['next_step'] not in macro_records and self.store.get(definition["id"])["revision"] != run["revision"]:
                run.update(active=False, status="BLOCKED")
                self.storage.protected_runs.discard(run["run_id"])
                return {"status": "BLOCKED", "module_run": dict(run)}
            if run['next_step'] not in macro_records and hasattr(self.task, "controller") and self.task.controller.module_pause.is_set():
                run["status"] = "PAUSED"
                return {"status": "PAUSED", "module_run": dict(run)}
            step = definition["steps"][run["next_step"]]
            origin=run.get('condition_remaining') or run.get('condition_skips',{}).get(step['step_id'])
            if origin:
                from src.rwtd.android_core.step_control import skipped_record
                record=skipped_record(step,definition['id'],definition['revision'],origin)
                record['run_id']=run['run_id'];self.reports[record['report_id']]=record
                run['next_step']+=1
                continue
            if manual_only and not require_verification and run['next_step'] not in macro_records:
                tail=self._macro_tail(definition['steps'],run['next_step'],len(definition['steps']))
                for offset,candidate in enumerate(tail):
                    if candidate['step_id'] in run.get('condition_skips',{}):tail=tail[:offset];break
                if tail:
                    for offset,record in enumerate(self._execute_device_macro(tail,definition['id'],definition['revision'],stop,run_id=run['run_id'])):
                        macro_records[run['next_step']+offset]=record
            allowed,retry_report,repeated=self._prepare_step_retry(step,definition['id'],definition['revision'],stop)
            for row in repeated:row['run_id']=run['run_id']
            if not allowed:
                record=blocked_record(step,definition['id'],definition['revision'],retry_report,repeated)
                record['run_id']=run['run_id'];self.reports[record['report_id']]=record
                run.update(active=False,status='BLOCKED')
                self.storage.protected_runs.discard(run['run_id'])
                counts=[r.get('device_inputs') for r in repeated]
                return dict(record,module_run=dict(run),records=repeated+[record],
                            device_inputs=None if any(c is None for c in counts) else sum(counts))
            self.inside_module = True
            previous_retry_authorized=getattr(self,'_step_retry_authorized',None)
            self._step_retry_authorized=(definition['id'],step['step_id']) if retry_report else None
            try:
                record = macro_records.pop(run['next_step']) if run['next_step'] in macro_records else self.handle("execute", dict(step, reversible_ack=True, workflow_module=definition["id"], dashboard_manual=manual_only), stop)
            finally:
                self.inside_module = False
                self._step_retry_authorized=previous_retry_authorized
            if retry_report:
                record['step_retry']=retry_report
                record['input_conditions']=deepcopy(retry_report['conditions'])
                if (not record.get('action_completed') or record.get('failure_reason')
                        or record.get('exclusion_skipped') or record.get('condition_skipped')):
                    record.update(status='BLOCKED',action_completed=False,
                        exclusion_skipped=False,condition_skipped=False,
                        failure_reason=record.get('failure_reason') or 'Conditional Step action was not delivered')
                if record.get('reports_saved',True):self.storage.finish(record)
            if step['step_id'] in run.get('failure_report_steps',{}):
                failure_path=self.storage.path('session_'+record['report_id']+'/failure_retest_report.json')
                failure_report={'schema_version':1,'reason':'previous_manual_fail',
                    'previous_report_id':run['failure_report_steps'][step['step_id']],
                    'previous_judgment':self.reports.get(run['failure_report_steps'][step['step_id']],{}).get('manual_verification'),
                    'module':definition['id'],'revision':definition['revision'],'run_id':run['run_id'],
                    'step_id':step['step_id'],'execution':dict(record)}
                atomic_json(failure_path,failure_report)
                record['failure_report_path']=str(failure_path)
                self.storage.finish(record)
                self.latest_module_failure_report=dict(failure_report,path=str(failure_path))
            control=record.get('condition_control')
            from src.rwtd.android_core.step_control import applies_to_tail
            if applies_to_tail(record):
                from src.rwtd.android_core.step_control import skipped_ids,log as log_condition_control
                affected=skipped_ids(control,definition['steps'],step['step_id'])
                if control['action'] in {'skip_remaining','skip_remaining_now'}:run['condition_remaining']=control
                else:
                    for step_id in affected:run.setdefault('condition_skips',{})[step_id]=control
                log_condition_control(self.task,definition['id'],step['step_id'],control,affected)
            run["next_step"] += 1
            if record.get('condition_skipped'):
                continue
            if record.get('exclusion_skipped') and not record.get('input_attempted'):
                record.update(status='SKIPPED',skip_reason=record.get('failure_reason'),failure_reason=None)
                if record.get('reports_saved',True):self.storage.finish(record)
                event=getattr(self.task,'dashboard_event',None)
                if callable(event):event('SKIPPED',f"{definition['id']}/{step['step_id']} · 禁止區域相交；跳過 INPUT")
                continue
            if not macro_records and (stop.is_set() or self.task.exit_is_set()):
                run.update(active=False, status="STOPPED")
                self.storage.protected_runs.discard(run["run_id"])
                return dict(record, module_run=dict(run))
            if record["status"] not in {"SUCCESS", "MANUAL_VERIFIED"}:
                if not require_verification and record['status']=='INPUT_SENT_UNVERIFIED' and record.get('action_completed') and not record.get('failure_reason'):
                    continue
                if not manual_only and record["status"] == "INPUT_SENT_UNVERIFIED" and step.get("verification_policy", "manual") == "none" and record.get("action_completed"):
                    continue  # Explicitly configured unverified step; module can never be SUCCESS.
                run.update(status=record["status"], waiting_report=record["report_id"])
                if record["status"] != "INPUT_SENT_UNVERIFIED" or record.get("failure_reason"):
                    run["active"] = False
                    self.storage.protected_runs.discard(run["run_id"])
                return dict(record, module_run=dict(run))
        outcome = self.workflow_result(definition["id"], run["run_id"])
        run.update(active=False, status=outcome["status"])
        self.storage.protected_runs.discard(run["run_id"])
        if outcome["status"] == "SUCCESS":
            self.storage.retain_successful()
        return {"status": outcome["status"], "module_result": outcome, "module_run": dict(run)}
