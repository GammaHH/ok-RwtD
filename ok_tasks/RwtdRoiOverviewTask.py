"""Independent native one-time task; stays running until its HTML UI is closed."""
from pathlib import Path
import webbrowser

from ok import BaseTask
from src.rwtd.dashboard_bridge import DashboardBridge
from src.rwtd.roi_overview import RoiOverview


class RwtdRoiOverviewTask(BaseTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = 'RwtD ROI 總覽 / ROI overview'
        self.description = '獨立 HTML ROI 編輯與圖片評估；關閉任務後釋放 OK 一次性任務。'
        self.support_schedule_task = False
        self.default_config = {'Open ROI Overview': True, 'ROI controls': ''}
        self.config_description = {'Open ROI Overview': '啟動後開啟本機 HTML 介面；不操作手機。'}
        self.config_type = {'ROI controls': {'type': 'button', 'buttons': [
            {'text': '開啟 ROI 總覽', 'callback': self.open_overview},
            {'text': '關閉 ROI 總覽並結束任務', 'callback': self.request_close}]}}
        self.bridge = None
        self.model = None

    def open_overview(self):
        bridge = self.bridge
        if bridge and bridge.thread.is_alive():
            webbrowser.open(bridge.url)

    def request_close(self):
        if self.model:
            self.model.closing.set()

    def run(self):
        workspace=getattr(self,'_native_workspace',None)
        if workspace is not None:
            self.model=workspace.roi
            try:
                return workspace.serve(self,'roi')
            finally:
                self.model=None
        root = Path(__file__).resolve().parents[1]
        self.model = RoiOverview(root)
        bridge = DashboardBridge(self.model, root/'assets/rwtd/roi_overview',
            self.model.snapshot, self.model.image, max_command_bytes=12*1024**2)
        self.bridge = bridge
        try:
            bridge.start()
            self.info_set('ROI Overview', bridge.origin)
            if self.config.get('Open ROI Overview', True):
                self.open_overview()
            # Native OK one-time execution remains occupied while the server lives.
            while self.enabled and not self.exit_is_set() and not self.model.closing.is_set():
                self.model.process_next()
                self.sleep(.1)
        finally:
            self.model.closing.set()
            bridge.close()
            self.bridge = None

    def on_destroy(self):
        self.request_close()
