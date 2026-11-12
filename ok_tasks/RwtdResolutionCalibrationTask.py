"""Independent OK task for display profiles and explicit swipe calibration."""
import re
import webbrowser

from ok_tasks.RwtdControlDashboardTask import RwtdControlDashboardTask


class RwtdResolutionCalibrationTask(RwtdControlDashboardTask):
    supports_device_macro = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rwtd_offline_editor = True
        self.name = 'RwtD 解析度校正 / Resolution calibration'
        self.description = '擷取實機畫面，按模組／Step ID 調整 ROI、觸控位置及試滑；獨立保存各解析度校正。'
        self.support_schedule_task = False
        self.default_config = {'Open Calibration': True, 'Display Size': 'Auto', 'Calibration controls': ''}
        self.config_description = {
            'Open Calibration': '啟動時開啟左右對照校正表單；按「試滑一次」會操作實機。請先停止並儲存 FARM／DASH。',
            'Display Size': 'Auto 依實機尺寸建立獨立校正；指定尺寸時必須與擷取結果一致。'}
        self.config_type = {'Calibration controls': {'type': 'button', 'buttons': [
            {'text': '開啟解析度校正', 'callback': self.open_calibration},
            {'text': '關閉校正任務', 'callback': self.request_close}]}}
        self.model = None

    def open_calibration(self):
        workspace = getattr(self, '_native_workspace', None)
        bridge = workspace.host.bridge if workspace is not None else self.bridge
        if bridge and bridge.thread.is_alive():
            webbrowser.open(bridge.url)

    def request_close(self):
        if self.model: self.model.closing.set()

    def _capture_calibration(self):
        backend, device = self.manual_backend()
        # Bypass only the reference-frame proxy; use the official selected ADB capture.
        frame = backend.backend.get_frame()
        self.normalize_device_frame(frame)  # Assert configured size and bind geometry.
        return frame, device

    def _test_calibration_swipe(self, points, duration, expected_device):
        if self.executor.current_task is not self or not self.enabled or self.exit_is_set():
            raise ValueError('校正任務已失去實機操作權')
        _, device = self.manual_backend()
        if device.get('device_identifier') != expected_device['device_identifier']:
            raise ValueError('實機已切換；請重新啟動校正')
        if len(points) == 3:
            self.prepare_three_point_swipe(physical=True)(points, duration)
            return
        (x, y), (end_x, end_y) = points
        result = self.adb_shell(f'input swipe {x} {y} {end_x} {end_y} {duration}',
                                timeout=duration / 1000 + 10)
        if result is None or re.search(r'\b(error|exception|unknown command|usage:)\b', str(result), re.I):
            raise RuntimeError('ADB 試滑未確認；請先查看實機畫面')

    def run(self):
        from src.rwtd.workspace_entry import route_to_workspace
        if route_to_workspace(self, 'calibration'):
            return
        workspace = self._native_workspace
        self.model = workspace.calibration
        try:
            if getattr(self, '_workspace_capture_requested', False):
                self._workspace_capture_requested = False
                try:
                    self.model.command('capture', {})
                except (ValueError, OSError, RuntimeError) as exc:
                    self.model.state['message'] = str(exc)[:300]
            return workspace.serve(self, 'calibration')
        finally:
            self.model = None

    def on_destroy(self):
        self.request_close()
