"""Independent native one-time task; stays running until its HTML UI is closed."""
import webbrowser

from ok import BaseTask


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
        workspace = getattr(self, '_native_workspace', None)
        bridge = workspace.host.bridge if workspace is not None else self.bridge
        if bridge and bridge.thread.is_alive():
            webbrowser.open(bridge.url)

    def request_close(self):
        if self.model:
            self.model.closing.set()

    def run(self):
        from src.rwtd.workspace_entry import route_to_workspace
        if route_to_workspace(self, 'roi'):
            return
        workspace = self._native_workspace
        self.model = workspace.roi
        try:
            return workspace.serve(self, 'roi')
        finally:
            self.model = None

    def on_destroy(self):
        self.request_close()
