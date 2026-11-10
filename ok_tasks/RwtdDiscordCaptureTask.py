"""Request-driven read-only TriggerTask; idle polling never captures a frame."""
from ok import TriggerTask
from ok_tasks.RwtdControlDashboardTask import RwtdControlDashboardTask


class RwtdDiscordCaptureTask(RwtdControlDashboardTask,TriggerTask):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.name='RwtD Discord 截圖 / Remote screenshot'
        self.description='只處理已授權 Discord 指令的一次截圖，不啟動 FARM。'
        self.support_schedule_task=False
        self.default_config={'_enabled':True,'Display Size':'Auto'}
        self.config_type={}
        self.config_description={'Display Size':'依官方 ADB 原生尺寸，沿用 RwtD 畫面正規化。'}
        self.trigger_interval=1
        self.remote_job=None

    def should_trigger(self):
        return self.remote_job is not None and super().should_trigger()

    def run(self):
        from src.rwtd.discord_control import current
        service=current()
        job,self.remote_job=self.remote_job,None
        if service is None or job is None:return False
        try:
            if job['future'].done() or service.mailbox.clock()>=job['expires']:
                raise ValueError('截圖指令已逾時')
            service.capture(self,job)
        except Exception as exc:
            service.mailbox.finish(job,{'status':'FAILED','message':'截圖失敗：'+type(exc).__name__})
        return False  # Let OK release the native TriggerTask owner after a read-only capture.
