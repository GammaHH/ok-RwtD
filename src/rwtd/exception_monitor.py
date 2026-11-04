"""Shared exception evidence and explicit health contracts; no capture/input loop."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from src.rwtd.discord_notifications import get_notifier, utc_now, write_json
from src.rwtd.notification_policy import device_identifier, exception_fingerprint, static_configuration_error


def safe_text(value):
    text = re.sub(r"https?://[^\s<>]+", "[URL redacted]", str(value))
    secret = os.environ.get("RWTD_DISCORD_WEBHOOK_URL", "").strip().rsplit("/", 1)[-1]
    if secret:
        text = text.replace(secret, "[credential redacted]")
    return text[:2000]


def failure_diagnostics(details):
    """Bounded decision context; never turn absent confidence into zero."""
    if not isinstance(details, dict):
        return ''
    lines = []
    for key, label in (('canvas_name','Canvas'),('canvas_id','Canvas ID'),
                       ('chain','Chain'),('node','Node'),('node_kind','Type'),
                       ('module','Module'),('step_id','Step')):
        if details.get(key) is not None:
            lines.append(f'{label}: {safe_text(details[key])[:100]}')
    if details.get('attempt_count') is not None:
        lines.append(f"Attempts: {details['attempt_count']}/{details.get('attempt_limit', '?')}")
    recognition = details.get('recognition')
    if isinstance(recognition, dict):
        for key in ('features','feature','threshold','roi','feature_rois','op','value','progress_ocr'):
            if recognition.get(key) is not None:
                lines.append(f'{key}: {safe_text(json.dumps(recognition[key],ensure_ascii=False,default=str))[:180]}')
        for row in (recognition.get('detections') or [])[:8]:
            if not isinstance(row,dict):
                continue
            confidence=row.get('confidence')
            lines.append('COCO '+safe_text(row.get('feature','?'))[:60]+
                         f" matched={row.get('matched')} confidence={confidence if confidence is not None else 'N/A'}"+
                         ' status='+safe_text(row.get('status','unspecified'))[:70])
        if recognition.get('features') and not recognition.get('detections'):
            lines.append('COCO: no candidate evidence; confidence=N/A')
    return '\n'.join(lines)[:1100]


def scoped_path(root, relative):
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root / "docs/rwtd"):
        raise ValueError("Evidence path escaped docs/rwtd or used a link")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("Evidence path uses a link")
    return path


def device_snapshot(task):
    """Only read existing device selection metadata; do not probe/capture/connect."""
    result = {"selected": False, "connected": None}
    try:
        preferred = task.executor.device_manager.get_preferred_device() or {}
        result.update(selected=bool(preferred), kind=preferred.get("device"),
                      connected=preferred.get("connected"), model=preferred.get("nick"))
        identifier = preferred.get("serial") or preferred.get("device_id") or preferred.get("address") or preferred.get("id")
        if identifier:
            result["device_identifier"] = hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()
        backend = getattr(task.executor, "method", None)
        result["capture_backend"] = type(backend).__name__ if backend is not None else None
    except Exception:
        result["metadata_unavailable"] = True
    return result


class ExceptionMonitor:
    def __init__(self, project_root, *, notifier=None, clock=None, failure_limit=3):
        self.root = Path(project_root).resolve()
        self.notifier = notifier or get_notifier(self.root)
        self.clock = clock or time.monotonic
        self.failure_limit = max(1, int(failure_limit))
        self.states = {}
        self.latest_frame = None
        self.latest_frame_task = None
        self.latest_frame_utc = None
        self.latest_frame_tick = None
        self.stop_requested = False
        self.lock = threading.RLock()

    def _state(self, task_name):
        return self.states.setdefault(task_name, {"last_successful_step": None, "failures": 0,
            "unknown_count": 0, "progress_token": None, "progress_started": None})

    def accept_frame(self, task_name, frame, *, device=None):
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 \
                or frame.shape[2] not in (3, 4) or frame.shape[0] < 1 or frame.shape[1] < 1:
            raise ValueError("Invalid analyzed frame")
        with self.lock:
            state = self._state(task_name)
            # Independent copy: matching/overlays cannot later change evidence.
            state.update(frame=frame.copy(), frame_utc=utc_now(), frame_tick=self.clock(),
                         device=dict(device or {}),recognition_regions=[],ocr_records=[])
            if state["device"].get("frame_source") not in ("offline_file", "synthetic"):
                self.latest_frame = state["frame"]
                self.latest_frame_task = task_name
                self.latest_frame_utc = state["frame_utc"]
                self.latest_frame_tick = state["frame_tick"]

    def success(self, task_name, step):
        with self.lock:
            self._state(task_name).update(last_successful_step=safe_text(step), failures=0)

    def check_expected_state(self, task_name, *, expected, observed, confirm_frames=3, notify=True):
        """Empty expectations are UNKNOWN, not an error. Any expected label suffices."""
        with self.lock:
            state = self._state(task_name)
            if not expected:
                state["unknown_count"] = 0
                return None
            if set(expected) & set(observed):
                state["unknown_count"] = 0
                return None
            state["unknown_count"] += 1
            if state["unknown_count"] < max(2, int(confirm_frames)):
                return None
            state["unknown_count"] = 0
            return self.report(task_name, "UNEXPECTED_SCREEN_STATE",
                "Explicit expected features absent on consecutive frames: " + ", ".join(expected), notify=notify)

    def check_progress(self, task_name, token, *, expecting_change=False, timeout_seconds=120, notify=True):
        """Requires a producer's explicit progress expectation; static art is not a stall."""
        with self.lock:
            state = self._state(task_name)
            if not expecting_change or token is None:
                state["progress_started"] = None
                state["progress_token"] = None
                return None
            if state["progress_started"] is None or token != state["progress_token"]:
                state.update(progress_token=token, progress_started=self.clock())
                return None
            if self.clock() - state["progress_started"] < max(1, float(timeout_seconds)):
                return None
            state["progress_started"] = self.clock()
            return self.report(task_name, "STALLED_PROGRESS", "Expected progress token did not change before deadline",
                               notify=notify)

    def report(self, task_name, category, description, *, stale=False, device=None,
               exception_type=None, recovery_attempts=0, notify=True, simulated=False,
               error_identifier=None, error_details=None, count_failure=True,save_screenshot=True):
        with self.lock:
            state = self._state(task_name)
            frame = state.get("frame") if save_screenshot else None
            frame_task = task_name
            frame_utc = state.get("frame_utc")
            frame_tick = state.get("frame_tick")
            if category == "ADB_CAPTURE_UNAVAILABLE" and state.get("device", {}).get("frame_source") in ("offline_file", "synthetic"):
                frame = None
                frame_utc = None
                frame_tick = None
            if save_screenshot and self.latest_frame is not None and (frame is None or (stale and
                    (frame_tick is None or self.latest_frame_tick > frame_tick))):
                frame = self.latest_frame
                frame_task = self.latest_frame_task
                frame_utc = self.latest_frame_utc
                frame_tick = self.latest_frame_tick
                stale = True
            identity = {"category": safe_text(category),
                        "error_identifier": safe_text(error_identifier or exception_type or category),
                        "device_identifier": safe_text(device_identifier(device or state.get("device", {}))),
                        "details": {"task": safe_text(task_name), "description": safe_text(description),
                                    "details": safe_text(json.dumps(error_details, sort_keys=True, ensure_ascii=False, default=str))
                                               if error_details is not None else None}}
            local_only = static_configuration_error(category, identity["error_identifier"], description)
            if count_failure and not simulated and not local_only:
                state["failures"] += 1
                if state["failures"] >= self.failure_limit:
                    self.stop_requested = True
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            output = scoped_path(self.root, "docs/rwtd/exceptions/exception_" + timestamp + "_" + uuid.uuid4().hex[:8])
            output.mkdir(parents=True, exist_ok=False)
            record = {"timestamp_utc": utc_now(), "task_name": safe_text(task_name),
                "exception_category": safe_text(category), "exception_type": safe_text(exception_type or category),
                "description": safe_text(description), "last_successful_step": state["last_successful_step"],
                "device_state": device or state.get("device", {"selected": False, "connected": None}),
                "recovery_attempts": int(recovery_attempts), "autonomous_recovery_enabled": False,
                "simulated": bool(simulated), "device_inputs": 0, "stale": bool(stale),
                "stale_reason": "current_capture_unavailable_or_previous_task_frame" if stale else None,
                "frame_timestamp_utc": frame_utc, "frame_task_name": frame_task if frame is not None else None,
                "frame_device_state": self._state(frame_task).get("device") if frame is not None else None,
                "frame_age_seconds": round(max(0, self.clock() - frame_tick), 3) if frame_tick is not None else None,
                "consecutive_failures": state["failures"], "failure_limit": self.failure_limit,
                "stop_requested": self.stop_requested, "screenshot": None,
                "notification": {"status": "disabled", "delivery_verified": False}}
            record.update(exception_fingerprint=exception_fingerprint(**identity),
                          error_identifier=identity["error_identifier"], configuration_local_only=local_only)
            record['ocr_records']=copy.deepcopy(self._state(frame_task).get('ocr_records',[]))
            record['ocr_records_task']=frame_task if record['ocr_records'] else None
            # Keep the same redacted diagnostic payload used by fingerprinting
            # so a report can identify the exact Canvas/Chain/Input to edit.
            if identity['details']['details'] is not None:
                record['error_details']=identity['details']['details']
            screenshot = None
            if frame is not None:
                screenshot = output / "analyzed_frame.png"
                from src.rwtd.android_core.evidence_overlay import draw
                metadata=self._state(frame_task).get('recognition_regions',[])
                # Another task's fallback frame must not inherit this event's ROI.
                if not metadata and frame_task==task_name and not stale:
                    metadata=(error_details or {}).get('recognition_regions',[]) if isinstance(error_details,dict) else []
                try:
                    annotated,used=draw(frame,metadata,frame_task)
                    record['screenshot_overlays']=used
                except Exception as exc:
                    annotated=frame
                    record['screenshot_overlay_error']=type(exc).__name__
                encoded_ok, encoded = cv2.imencode(".png", annotated)
                if not encoded_ok:
                    record["screenshot_error"] = "PNG_ENCODING_FAILED"
                    screenshot = None
                else:
                    screenshot.write_bytes(encoded.tobytes())
                    record.update(screenshot=str(screenshot), native_size=[frame.shape[1], frame.shape[0]],
                                  screenshot_sha256=hashlib.sha256(screenshot.read_bytes()).hexdigest())
            else:
                record["screenshot_unavailable"] = True
            report_path = output / "report.json"
            write_json(report_path, record)
            if local_only:
                record["notification"] = {"status": "static_configuration_local_only", "delivery_verified": False, "attempts": 0}
                write_json(report_path, record)
            elif notify:
                notice = (f"[ok-RwtD][{safe_text(category)}] {'SIMULATED' if simulated else 'EXCEPTION'}\n"
                          f"Task: {safe_text(task_name)}\n{safe_text(description)}\n"
                          f"{failure_diagnostics(error_details)}\n"
                          f"UTC: {record['timestamp_utc']}\nLast success: {state['last_successful_step']}\n"
                          f"Frame: {'STALE' if stale else 'analyzed'}; captured: {frame_utc}\n"
                          f"Recovery attempts: {recovery_attempts}; Stop: {self.stop_requested}")
                record["notification"] = self.notifier.send(notice, png_path=screenshot,
                    exception=identity)
                write_json(report_path, record)
            record['retention_complete']=True
            write_json(report_path,record)
            from src.rwtd.android_core.record_retention import enforce
            enforce(self.root,'exceptions','docs/rwtd/exceptions',protected={output.name})
            return dict(record, report_path=str(report_path))


_monitors = {}
_lock = threading.Lock()


def get_monitor(project_root):
    root = Path(project_root).resolve()
    with _lock:
        if root not in _monitors:
            _monitors[root] = ExceptionMonitor(root)
        return _monitors[root]


def stop_rwtd_tasks(task):
    """Disable RwtD tasks through official API; no global exit or device input."""
    stopped = []
    for other in task.get_tasks():
        if type(other).__name__.startswith("Rwtd") and other.enabled:
            other.disable()
            stopped.append(type(other).__name__)
    return stopped
