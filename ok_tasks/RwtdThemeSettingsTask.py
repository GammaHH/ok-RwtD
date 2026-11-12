"""Native theme editor TASK; never captures or operates the selected device."""
from ok import BaseTask


class RwtdThemeSettingsTask(BaseTask):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._rwtd_offline_editor=True
        self.name='RwtD 配色設定 / Theme settings'
        self.description='設定 Dashboard、Canvas 與 Qt 共用配色；即時預覽、儲存及恢復預設。'
        self.support_schedule_task=False
        self.default_config={}
        self.model=None

    def run(self):
        from src.rwtd.workspace_entry import route_to_workspace
        if route_to_workspace(self,'theme'):return
        workspace=self._native_workspace
        self.model=workspace.theme
        try:return workspace.serve(self,'theme')
        finally:self.model=None

    def on_destroy(self):
        if self.model:self.model.closing.set()
