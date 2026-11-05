"""Native, device-independent timing report TASK."""
import webbrowser
from pathlib import Path
from ok import BaseTask
from src.rwtd.dashboard_bridge import DashboardBridge
from src.rwtd.round_statistics_ui import RoundStatisticsEditor


class RwtdRoundStatisticsTask(BaseTask):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._rwtd_offline_editor=True
        self.name='RwtD 輪次統計 / Round statistics'
        self.description='依關卡／活動、版本與筆數統計成功輪次；排除失敗退出，匯出 CSV／Markdown。'
        self.support_schedule_task=False
        self.default_config={'Open Statistics':True}
        self.bridge=self.model=None

    def run(self):
        workspace=getattr(self,'_native_workspace',None)
        if workspace is not None and not workspace.closed:
            self.model=workspace.statistics
            try:
                return workspace.serve(self,'statistics')
            finally:
                self.model=None
        root=Path(__file__).resolve().parents[1]
        self.model=RoundStatisticsEditor(root)
        self.bridge=DashboardBridge(self.model,root/'assets/rwtd/statistics_ui',self.model.snapshot,self.model.image)
        try:
            self.bridge.start()
            self.info_set('Round statistics',self.bridge.origin)
            if self.config.get('Open Statistics',True):
                webbrowser.open(self.bridge.url)
            while self.enabled and not self.exit_is_set() and not self.model.closing.is_set():
                self.model.process_next()
                self.model.closing.wait(.1)
        finally:
            self.model.closing.set()
            self.bridge.close()
            self.bridge=self.model=None

    def on_destroy(self):
        if self.model:
            self.model.closing.set()
