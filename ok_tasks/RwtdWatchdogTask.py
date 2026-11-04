"""Native OK TriggerTask switch, with independent passive supervision."""
import json
import sys
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np
from ok import TriggerTask

from src.rwtd.android_core.watchdog import registry, WatchPolicy
from src.rwtd.discord_notifications import DiscordNotifier
from src.rwtd.exception_monitor import ExceptionMonitor


class RwtdWatchdogTask(TriggerTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name='RwtD 流程看門狗 / Farm Watchdog'
        self.description='獨立監督 Farm 卡住與觀測逾時；只保存診斷及通知，不操作手機或自動重啟。'
        self.default_config.update({'_enabled':False,'Check Interval (s)':5,
            'Executor Timeout (s)':120,'Flow Timeout (s)':300,
            'Observer Timeout (s)':60,'Notify Discord':True})
        self.config_description.update({
            'Check Interval (s)':'每次監督檢查間隔，1～60 秒；不改變辨識頻率。',
            'Executor Timeout (s)':'主執行緒無回報上限，10～3600 秒；批次另加四倍理論時間。',
            'Flow Timeout (s)':'流程無實際進展上限，30～86400 秒；正常等待與暫停不算卡住。',
            'Observer Timeout (s)':'單次觀測工作逾時上限，10～3600 秒。',
            'Notify Discord':'使用既有通知通道，附可取得的原辨識畫面；不另截圖。'})
        self.trigger_interval=5
        self.support_schedule_task=False
        self._shutdown=threading.Event()
        self._worker_lock=threading.Lock()
        self._worker=None
        self._policy=WatchPolicy()
        self._paused=set()
        self._last_error=None

    def validate_config(self,key,value):
        ranges={'Check Interval (s)':(1,60),'Executor Timeout (s)':(10,3600),
                'Flow Timeout (s)':(30,86400),'Observer Timeout (s)':(10,3600)}
        if key in ranges and (type(value) is not int or not ranges[key][0]<=value<=ranges[key][1]):
            return f'{key}: {ranges[key][0]}～{ranges[key][1]}'

    def on_create(self):
        super().on_create()
        self.root=Path(__file__).resolve().parents[1]
        self._monitor=ExceptionMonitor(self.root,notifier=DiscordNotifier(
            self.root,native_sender=self._native_notice))
        self._ensure_worker()

    def _ensure_worker(self):
        with self._worker_lock:
            if not self._shutdown.is_set() and (self._worker is None or not self._worker.is_alive()):
                self._worker=threading.Thread(target=self._supervise,
                    name='RwtDWatchdog',daemon=True)
                self._worker.start()

    def run(self):
        self._ensure_worker()
        return False  # This Trigger never claims Android work.

    def on_destroy(self):
        self._shutdown.set()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=.2)
        super().on_destroy()

    def _native_notice(self,text,png):
        images=[]
        if png is not None:
            frame=cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError('Invalid watchdog evidence image')
            images.append(frame)
        self.notification(text,title='RwtD Watchdog',error=True,tray=True,
                          images=images,screenshot=False)

    def _report(self,task,sample,alert):
        if not registry.is_current(task,sample['session']):
            return  # Farm ended/restarted after the monitoring snapshot.
        state=sample['state']
        node=str(state.get('node') or 'Farm')
        self._monitor.states.clear()
        self._monitor.latest_frame=None
        # Never wait on a lock held by the task we suspect is stuck.
        evidence=getattr(task,'failure_evidence',None)
        frame_state=None
        if evidence is not None and evidence.lock.acquire(blocking=False):
            try:
                cached=evidence.monitor.states.get(node)
                if cached and cached.get('frame') is not None:
                    frame_state=dict(cached,frame=cached['frame'].copy())
            finally:
                evidence.lock.release()
        if frame_state:
            self._monitor.accept_frame(node,frame_state['frame'],device=frame_state.get('device'))
            self._monitor.states[node].update(frame_utc=frame_state.get('frame_utc'),
                                              frame_tick=frame_state.get('frame_tick'))
        frames=sys._current_frames()
        # No locals, credentials or arbitrary object repr in thread evidence.
        stack=traceback.extract_stack(frames[sample['thread_id']],limit=18) if sample['thread_id'] in frames else []
        details=dict(alert,session=sample['session'],canvas_name=state.get('canvas'),
            chain=state.get('chain'),node=node,round=state.get('round'),
            phase=state.get('status'),queue=state.get('queue'),
            executor_stack=[f'{Path(f.filename).name}:{f.lineno} {f.name}' for f in stack])
        reason=f"{alert['kind']}：{node} · {alert['elapsed_seconds']} s；未自動重試、操作或重啟"
        record=self._monitor.report(node,'WATCHDOG_STALLED',reason,stale=bool(frame_state),
            notify=self.config.get('Notify Discord',True) is True,count_failure=False,
            error_identifier=alert['kind'],error_details=details)
        self.info_set('Last alert',reason)
        self.info_set('Report',record['report_path'])
        self.info_set('Notification',record['notification']['status'])
        self.log_warning('RWTD_WATCHDOG '+json.dumps(details,ensure_ascii=False))
        # Watchdog retains no full images between incident reports.
        self._monitor.states.clear()
        self._monitor.latest_frame=None

    def _supervise(self):
        was_enabled=False
        while not self._shutdown.is_set():
            try:
                enabled=self.enabled
                if enabled and not was_enabled:
                    for task,_ in registry.snapshots():
                        registry.rebase(task)
                if not enabled:
                    self._policy=WatchPolicy()
                    self._paused.clear()
                else:
                    for task,sample in registry.snapshots():
                        paused=bool(getattr(getattr(task,'executor',None),'paused',False))
                        if paused:
                            self._paused.add(sample['session'])
                            continue
                        if sample['session'] in self._paused:
                            registry.rebase(task)
                            self._paused.discard(sample['session'])
                            continue
                        alerts=self._policy.evaluate(sample,time.monotonic(),
                            heartbeat=self.config.get('Executor Timeout (s)',120),
                            progress=self.config.get('Flow Timeout (s)',300),
                            observer=self.config.get('Observer Timeout (s)',60))
                        for alert in alerts:
                            if self.enabled and not self._shutdown.is_set():
                                self._report(task,sample,alert)
                was_enabled=enabled
                self._last_error=None
            except Exception as exc:
                # No tight retry loop or potentially credential-bearing exception text.
                if type(exc).__name__ != self._last_error:
                    self.log_warning('RWTD_WATCHDOG_ERROR '+type(exc).__name__)
                    self._last_error=type(exc).__name__
            self._shutdown.wait(max(1,min(60,self.config.get('Check Interval (s)',5))))
