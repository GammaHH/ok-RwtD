"""Independent OK task: compare/edit display profiles without device input."""
import webbrowser
from pathlib import Path

from ok_tasks.RwtdControlDashboardTask import RwtdControlDashboardTask
from src.rwtd.dashboard_bridge import DashboardBridge
from src.rwtd.resolution_calibration_ui import CalibrationEditor


class RwtdResolutionCalibrationTask(RwtdControlDashboardTask):
    supports_device_macro = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rwtd_offline_editor = True
        self.name = 'RwtD 解析度校正 / Resolution calibration'
        self.description = '唯讀擷取實機，按模組／Step ID 對照調整 ROI 與觸控位置；獨立保存各解析度校正。'
        self.support_schedule_task = False
        self.default_config = {'Open Calibration': True, 'Display Size': 'Auto', 'Calibration controls': ''}
        self.config_description = {
            'Open Calibration': '啟動時開啟左右對照校正表單；不會操作手機。請先停止並儲存 FARM／DASH。',
            'Display Size': 'Auto 依實機尺寸建立獨立校正；指定尺寸時必須與擷取結果一致。'}
        self.config_type = {'Calibration controls': {'type': 'button', 'buttons': [
            {'text': '開啟解析度校正', 'callback': self.open_calibration},
            {'text': '關閉校正任務', 'callback': self.request_close}]}}
        self.model = None

    def open_calibration(self):
        if self.bridge and self.bridge.thread.is_alive(): webbrowser.open(self.bridge.url)

    def request_close(self):
        if self.model: self.model.closing.set()

    def _capture_calibration(self):
        backend, device = self.manual_backend()
        # Bypass only the reference-frame proxy; use the official selected ADB capture.
        frame = backend.backend.get_frame()
        self.normalize_device_frame(frame)  # Assert configured size and bind geometry.
        return frame, device

    def run(self):
        workspace = getattr(self, '_native_workspace', None)
        if workspace is not None and not workspace.closed:
            self.model = workspace.calibration
            try:
                return workspace.serve(self, 'calibration')
            finally:
                self.model = None
        from src.rwtd.android_core.execution_lease import active
        root = Path(__file__).resolve().parents[1]
        if active(root): raise ValueError('請先手動停止並儲存 FARM，再啟動解析度校正')
        self.root = root
        self.model = CalibrationEditor(root, self._capture_calibration)
        bridge = DashboardBridge(self.model, root/'assets/rwtd/calibration_ui',
                                 self.model.snapshot, self.model.image,
                                 max_command_bytes=9*1024**2)
        self.bridge = bridge
        try:
            try: self.model.command('capture', {})
            except (ValueError, OSError, RuntimeError) as exc:
                self.model.state['message'] = str(exc)[:300]
            bridge.start()
            self.info_set('Calibration', bridge.origin)
            if self.config.get('Open Calibration', True): self.open_calibration()
            while self.enabled and not self.exit_is_set() and not self.model.closing.is_set():
                self.model.process_next()
                self.model.closing.wait(.1)
        finally:
            self.model.closing.set()
            bridge.close()
            self.bridge = None
            self.model = None

    def on_destroy(self):
        self.request_close()
