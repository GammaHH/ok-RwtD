"""Native, device-independent timing report TASK."""
from ok import BaseTask


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
        from src.rwtd.workspace_entry import route_to_workspace
        if route_to_workspace(self, 'statistics'):
            return
        workspace = self._native_workspace
        self.model = workspace.statistics
        try:
            return workspace.serve(self, 'statistics')
        finally:
            self.model = None

    def on_destroy(self):
        if self.model:
            self.model.closing.set()
