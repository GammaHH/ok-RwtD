"""Bounded Discord command mailbox and native command dispatch."""
import copy
import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timezone


COMMANDS = {'status','screenshot','task_exit','farm_start','farm_pause','farm_resume','farm_stop',
            'module_list','module_queue','module_preempt',
            'farm_stop_after_round','activity_list','activity_queue','trigger_status','diagnosis_latest'}
_service = None


def current():
    return _service


class ControlMailbox:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.RLock()
        self.queue, self.pending = deque(), {}
        self.completed = {}
        self.closed = False

    def submit(self, key, command, payload=None):
        if command not in COMMANDS or payload is not None and not isinstance(payload,dict):
            raise ValueError('Unknown remote command')
        with self.lock:
            if self.closed:raise ValueError('OK 控制服務已關閉')
            if key in self.pending:return self.pending[key]['future']
            if key in self.completed:return self.completed[key]
            if len(self.pending)>=32:raise ValueError('控制佇列已滿')
            job={'id':key,'command':command,'payload':copy.deepcopy(payload or {}),
                 'future':Future(),'expires':self.clock()+90,'accepted':False}
            self.pending[key]=job
            self.queue.append(job)
            return job['future']

    def claim(self):
        with self.lock:
            while self.queue:
                job=self.queue.popleft()
                if job['future'].done():
                    self.pending.pop(job['id'],None)
                    continue
                if self.clock()>=job['expires']:
                    self.finish(job,{'status':'FAILED','message':'指令等待逾時；未執行'})
                    continue
                job['accepted']=True
                return job

    def finish(self, job, result):
        import logging
        logging.getLogger(__name__).info('RWTD_DC_RESULT id=%s status=%s',job['id'],result.get('status'))
        with self.lock:
            self.pending.pop(job['id'],None)
            if not job['future'].done():job['future'].set_result(result)
            receipt=Future()
            receipt.set_result({k:v for k,v in result.items() if k not in {'png','data'}})
            self.completed[job['id']]=receipt
            while len(self.completed)>64:self.completed.pop(next(iter(self.completed)))

    def close(self):
        with self.lock:
            self.closed=True
            for job in list(self.pending.values()):
                self.finish(job,{'status':'FAILED','message':'OK 已關閉；不會於下次啟動重播'})
            self.queue.clear()
            self.completed.clear()


def select_canvas(catalog, value):
    matches=[row for row in catalog if row['id']==value or row['name']==value]
    if len(matches)!=1:raise ValueError('Canvas 不存在或同名；請使用選單選取')
    return matches[0]


class RemoteControl:
    """The TASK owns control and input; FARM screenshots use a read-only worker."""
    def __init__(self, app, root, mailbox=None):
        self.app,self.root=app,root
        self.mailbox=mailbox or ControlMailbox()
        self.owner_queue=deque()
        self.screenshot_worker=None
        self.cached={'status':'IDLE','message':'OK 已開啟，FARM 尚未啟動'}
        self.lock=threading.RLock()
        self.start_job=self.stop_job=None
        self.exit_job=None
        self.catalog=[]
        self.module_catalog=[]
        self.next_catalog_refresh=0

    def farm_task(self):
        executor=getattr(self.app,'task_executor',None)
        return next((t for t in getattr(executor,'onetime_tasks',[])
                     if type(t).__name__=='RwtdFarmTask'),None)

    def refresh_catalog(self):
        from functools import partial
        from .android_core.decision_graph import DecisionGraphStore
        from .android_core.feature_catalog import feature_names
        from .android_core.module_store import ModuleStore
        from .profile import rwtd_profile
        from .manual_device_tests import validate_parameters
        features=feature_names(self.root)
        profile=rwtd_profile(self.root,partial(validate_parameters,allowed_features=features or None))
        store=ModuleStore(profile)
        self.catalog=DecisionGraphStore(profile,store).catalog()
        self.module_catalog=[{key:row[key] for key in ('id','name','revision')} for row in store.catalog()]

    def preemption_pending(self):
        with self.lock:
            return any(job['command']=='module_preempt' and not job['future'].done()
                       and self.mailbox.clock()<job['expires'] for job in self.owner_queue)

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.cached)

    def finish(self,job,message,**extra):
        self.mailbox.finish(job,dict(status='COMPLETED',message=message,**extra))

    def check_pending(self,job):
        if job['future'].done() or self.mailbox.clock()>=job['expires']:
            raise ValueError('指令已取消或逾時；未執行')

    def idle_workspace(self,task,executor):
        workspace=getattr(task,'_native_workspace',None)
        owns_workspace=(workspace is not None and workspace.current=='farm'
                        and executor.current_task is task)
        busy=next((item for item in executor.onetime_tasks
                   if (item is not task or not owns_workspace)
                   and (getattr(item,'enabled',False) or getattr(item,'running',False))),None)
        if busy is None and not owns_workspace:
            busy=executor.current_task
        if busy is not None:
            name=getattr(busy,'name',type(busy).__name__)
            raise ValueError('目前任務占用：'+name+'；請先停止、儲存並退出該任務')
        if workspace is not None and not owns_workspace:
            raise ValueError('請先關閉現有工作區 TASK')
        if any(getattr(item,'remote_job',None) for item in getattr(executor,'trigger_tasks',[])):
            raise ValueError('請等待目前排隊的截圖完成')
        return workspace

    def finish_workspace_start(self,job,result):
        if result['status']=='SUCCESS':
            self.finish(job,'FARM 已啟動')
        else:
            self.mailbox.finish(job,{'status':'FAILED','message':result.get('reason') or 'FARM 啟動失敗'})
        self.start_job=None

    def run_module_job(self,task,job):
        try:
            self.check_pending(job)
            result=task.run_discord_module(job['module'],job['payload'].get('runs',1))
            if not result.get('action_completed'):
                raise ValueError(result.get('failure_reason') or '模組未完成，請檢查畫面')
            self.finish(job,'模組已完成，FARM 維持停止：'+job['module']['name'],
                        data={'result_status':result.get('status')})
        except Exception as exc:
            self.mailbox.finish(job,{'status':'FAILED','message':str(exc)[:500] or type(exc).__name__})
            return False
        return True

    def gui_tick(self):
        task=self.farm_task()
        executor=getattr(self.app,'task_executor',None)
        if self.exit_job is not None:
            exit_job,owner,workspace=self.exit_job
            if exit_job['future'].done():
                self.exit_job=None
            elif (workspace.exit_ready and getattr(executor,'current_task',None) is not owner
                  and not owner.running and not owner.enabled):
                self.finish(exit_job,'工作區任務已退出；OK 仍開啟')
                self.exit_job=None
            elif self.mailbox.clock()>=exit_job['expires']:
                self.mailbox.finish(exit_job,{'status':'FAILED','message':'等待工作區退出逾時，請用 /status 查詢'})
                self.exit_job=None
        native_idle=(getattr(executor,'current_task',None) is not task
                     and not getattr(task,'running',False) and not getattr(task,'enabled',False))
        module_job=getattr(task,'_discord_module_job',None)
        if module_job is not None and native_idle:
            task._discord_module_job=None
            self.mailbox.finish(module_job,{'status':'FAILED','message':'原生 TASK 已取消；模組未執行'})
        if self.mailbox.clock()>=self.next_catalog_refresh:
            self.next_catalog_refresh=self.mailbox.clock()+10
            if executor and executor.current_task is None:
                self.refresh_catalog()
        if self.stop_job and not getattr(task,'_discord_running',False):
            workspace=getattr(task,'_native_workspace',None)
            workspace_idle=(workspace is not None and not workspace.running
                            and workspace.service is None)
            if workspace_idle or native_idle:
                self.finish(self.stop_job,'FARM 已停止')
                self.stop_job=None
        job=self.mailbox.claim()
        if not job:return
        try:
            cmd=job['command'];payload=job['payload']
            if not executor or task is None:raise ValueError('OK 任務尚未就緒')
            running=getattr(task,'_discord_running',False)
            if cmd=='status':
                state=self.snapshot()
                if not running:state={'status':'IDLE','message':'FARM 未執行','task':type(executor.current_task).__name__ if executor.current_task else None}
                self.finish(job,'目前狀態',data=state)
            elif cmd=='task_exit':
                if payload:raise ValueError('退出指令不接受參數')
                if self.exit_job is not None:
                    raise ValueError('工作區已排隊退出，請等待完成')
                if running or self.start_job:
                    raise ValueError('請先用 /farm stop 停止 FARM，再退出工作區')
                owner=executor.current_task
                if owner is None:
                    self.finish(job,'目前沒有執行中的工作區任務')
                    return
                workspace=getattr(owner,'_native_workspace',None)
                if workspace is None or workspace.owners.get(workspace.current) is not owner:
                    raise ValueError('目前任務不支援遠端退出，請在 OK 停止該任務')
                workspace.enqueue_remote_exit(self,job,owner)
                self.exit_job=(job,owner,workspace)
            elif cmd=='farm_start':
                if set(payload)-{'canvas'}:raise ValueError('啟動參數無效')
                if self.exit_job is not None:raise ValueError('請等待工作區退出完成')
                if running or self.start_job:raise ValueError('FARM 已在執行或啟動中')
                workspace=self.idle_workspace(task,executor)
                self.refresh_catalog()
                value=payload.get('canvas')
                canvas_id=select_canvas(self.catalog,value)['id'] if value else None
                if workspace is not None:
                    self.start_job=job
                    try:workspace.enqueue_remote(self,job,canvas_id)
                    except Exception:
                        self.start_job=None
                        raise
                    return
                task._discord_canvas_id=canvas_id
                task._discord_start_job=job
                self.start_job=job
                try:
                    task.enable()
                    executor.start()
                except Exception:
                    task.disable()
                    self.start_job=None
                    task._discord_start_job=None
                    task._discord_canvas_id=None
                    raise
            elif cmd=='farm_stop':
                if self.stop_job:raise ValueError('停止已排隊，請等待完成')
                if not running:raise ValueError('FARM 未執行')
                self.stop_job=job
                task.controller.device_stop.set()  # latch only; no input from GUI/Bot
            elif cmd=='diagnosis_latest':self.diagnosis(job)
            elif cmd=='module_list':
                self.refresh_catalog()
                self.finish(job,'可執行模組',data=self.module_catalog)
            elif cmd in {'module_queue','module_preempt'}:
                if set(payload)-{'module','runs'}:raise ValueError('模組參數無效')
                runs=payload.get('runs',1)
                if type(runs) is not int or not 1<=runs<=100:raise ValueError('執行次數須為 1～100')
                matches=[row for row in self.module_catalog
                         if payload.get('module') in (row['id'],row['name'])]
                if len(matches)!=1:raise ValueError('模組不存在或同名；請從模組選單選取')
                row=matches[0]
                job['module']=copy.deepcopy(row)
                if not running:
                    if self.exit_job is not None:raise ValueError('請等待工作區退出完成')
                    if self.start_job:raise ValueError('FARM 正在啟動')
                    workspace=self.idle_workspace(task,executor)
                    if workspace is not None:
                        workspace.enqueue_remote(self,job)
                    else:
                        task._discord_module_job=job
                        try:
                            task.enable()
                            executor.start()
                        except Exception:
                            task._discord_module_job=None
                            task.disable()
                            raise
                    return
                with self.lock:
                    if cmd=='module_preempt':
                        index=next((i for i,item in enumerate(self.owner_queue)
                                    if item['command']!='module_preempt'),len(self.owner_queue))
                        self.owner_queue.insert(index,job)
                    else:self.owner_queue.append(job)
            elif cmd=='screenshot':
                if self.screenshot_worker is not None and self.screenshot_worker.is_alive():
                    raise ValueError('已有截圖請求處理中')
                if running:
                    self.start_farm_screenshot(executor.device_manager,task.config.get('Display Size','Auto'),job)
                else:
                    if any(getattr(t,'enabled',False) or getattr(t,'running',False) for t in executor.onetime_tasks) or executor.current_task:
                        raise ValueError('其他 TASK 使用手機中；請先停止並退出')
                    capture=next((t for t in executor.trigger_tasks if type(t).__name__=='RwtdDiscordCaptureTask'),None)
                    if capture is None:raise ValueError('遠端截圖 TASK 未註冊')
                    if not capture.enabled:raise ValueError('請先在 OK 啟用 Discord 截圖 TriggerTask')
                    if capture.remote_job is not None:raise ValueError('已有截圖請求等待執行')
                    capture.remote_job=job
                    executor.start()
            elif running:
                with self.lock:self.owner_queue.append(job)
            else:raise ValueError('FARM 未執行，無可控制的執行流程')
        except Exception as exc:
            self.mailbox.finish(job,{'status':'FAILED','message':str(exc)[:500]})

    def started(self,task):
        task._discord_running=True
        task._discord_paused=False
        task._discord_pause_total=0
        task._discord_pause_at=None
        task.controller.remote_preemption_pending=self.preemption_pending
        job=getattr(task,'_discord_start_job',None)
        if job:
            self.finish(job,'FARM 已啟動：'+task.info.get('Canvas',''))
            task._discord_start_job=None
            self.start_job=None

    def ended(self,task,error=None):
        task.controller.remote_preemption_pending=None
        task._discord_running=False
        task._discord_canvas_id=None
        job=getattr(task,'_discord_start_job',None)
        if job:
            self.mailbox.finish(job,{'status':'FAILED','message':'FARM 啟動失敗：'+(type(error).__name__ if error else '流程已退出')})
            task._discord_start_job=None
            self.start_job=None
        with self.lock:
            while self.owner_queue:
                self.mailbox.finish(self.owner_queue.popleft(),{'status':'FAILED','message':'FARM 已退出；指令未執行'})
            self.cached={'status':'STOPPED','message':'FARM 已退出'}

    def owner_tick(self,task):
        """Only invoked by the owning TaskExecutor at an operation boundary."""
        with self.lock:job=self.owner_queue.popleft() if self.owner_queue else None
        if job:
            try:
                if job['future'].done():raise ValueError('指令已取消；未執行')
                if self.mailbox.clock()>=job['expires']:raise ValueError('指令逾時；未執行')
                cmd=job['command']
                if cmd=='farm_pause':
                    if not task._discord_paused:
                        task._discord_paused=True
                        task._discord_pause_at=time.monotonic()
                        task.controller.activity_inline_observers=True
                    self.finish(job,'FARM 已在操作交接點暫停')
                elif cmd=='farm_resume':
                    if task._discord_paused:
                        task._discord_pause_total+=time.monotonic()-task._discord_pause_at
                        task._discord_pause_at=None
                        task._discord_paused=False
                        task.controller.activity_inline_observers=False
                    self.finish(job,'FARM 已恢復')
                elif cmd=='farm_stop_after_round':
                    task.farm.remote_stop_after_round=True
                    self.finish(job,'已設定本次主 FARM 輪次完成後停止，屆時不插入活動')
                elif cmd in {'module_queue','module_preempt'}:
                    if not task._activity_ready():
                        with self.lock:self.owner_queue.appendleft(job)
                        return False
                    try:
                        result=task.execute_remote_module(job['module'],job['payload'].get('runs',1))
                        if result.get('action_completed'):
                            state='維持暫停' if task._discord_paused else '接續原流程'
                            self.finish(job,'模組已完成，FARM '+state+'：'+job['module']['name'],
                                        data={'module':job['module']['id'],'runs':job['payload'].get('runs',1),
                                              'result_status':result.get('status')})
                        else:
                            task.controller.device_stop.set()
                            task.farm.stop()
                            self.mailbox.finish(job,{'status':'FAILED','message':'插入模組未完成；FARM 已停止，請檢查畫面'})
                    except Exception:
                        task.controller.device_stop.set()
                        task.farm.stop()
                        raise
                    finally:
                        task.controller.activity_inline_observers=task._discord_paused
                        task.controller._invalidate_trigger_frames()
                    return False  # The owner boundary was occupied by this module.
                elif cmd=='activity_queue':
                    task.farm.queue_activity(job['payload'].get('activity'),job['payload'].get('runs',1))
                    self.finish(job,'活動已加入待辦；等待主 FARM 安全輪次交接')
                elif cmd=='activity_list':
                    self.finish(job,'活動排程',data=task.farm.activity_snapshot(include_runtime=False))
                elif cmd=='trigger_status':
                    self.finish(job,'Trigger／Solver 狀態',data=task.controller.trigger_scheduler.snapshot())
                elif cmd=='diagnosis_latest':self.diagnosis(job)
                else:raise ValueError('不支援的執行期指令')
            except Exception as exc:
                if job['command'] in {'module_queue','module_preempt'}:
                    task.controller.activity_inline_observers=task._discord_paused
                self.mailbox.finish(job,{'status':'FAILED','message':str(exc)[:500]})
        owner=task._owner
        with self.lock:
            self.cached={'status':'PAUSED' if task._discord_paused else task.farm.status,
                         'canvas':owner.round_context.get('canvas',task.info.get('Canvas')),'chain':owner.schedule.current,
                         'node':owner.runtime.current_node or owner.runtime.cursor,
                         'rounds':task.farm.schedule.rounds,'activity':task.farm.activity_active,
                         'reason':owner.reason,'elapsed_seconds':round(max(0,owner.clock()-owner.round_started_at))}
        return not task._discord_paused

    def start_farm_screenshot(self,manager,display_size,job):
        from .android_core.watchdog import selected_android
        selection=selected_android(manager)
        if selection[1] is None:
            raise ValueError('目前沒有已連線的 Android 裝置')
        self.screenshot_worker=threading.Thread(
            target=self.capture_farm_frame,args=(manager,selection,display_size,job),
            name='RwtDDiscordScreenshot',daemon=True)
        self.screenshot_worker.start()

    def capture_farm_frame(self,manager,selection,display_size,job):
        from .android_core.screen_geometry import ScreenGeometry,expected_size
        from .android_core.watchdog import selected_android

        try:
            import cv2
            import numpy as np
            if job['future'].done():
                return
            if self.mailbox.clock()>=job['expires']:
                raise ValueError('截圖指令已逾時')
            if selected_android(manager)!=selection:
                raise ValueError('Android 裝置已變更')
            # Use a separate shell connection without the DeviceManager error
            # handler, which can disconnect the FARM device on capture failure.
            raw=selection[1].shell('screencap -p',encoding=None,timeout=10)
            captured=datetime.now(timezone.utc).isoformat()
            frame=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR) if raw else None
            if frame is None:
                raise ValueError('Android 截圖為空或無法解碼')
            height,width=frame.shape[:2]
            expected=expected_size(display_size)
            if expected is not None and expected!=(width,height):
                raise ValueError('Android 截圖尺寸與 Display Size 設定不符')
            frame=ScreenGeometry(width,height).normalize(frame)
            result=self.screenshot_result(frame,captured)
            if selected_android(manager)!=selection:
                raise ValueError('Android 裝置已變更')
        except Exception as exc:
            reason=str(exc) if type(exc) is ValueError else type(exc).__name__
            result={'status':'FAILED','message':'截圖失敗：'+reason[:160]}
        with self.mailbox.lock:
            if not job['future'].done():
                self.mailbox.finish(job,result)

    def capture(self,task,job):
        backend,_=task.manual_backend()
        frame=backend.get_frame()
        task.validate_native_frame(frame)
        captured=datetime.now(timezone.utc).isoformat()
        self.mailbox.finish(job,self.screenshot_result(frame,captured))

    @staticmethod
    def screenshot_result(frame,captured):
        import cv2
        ok,png=cv2.imencode('.png',frame)
        if not ok:raise ValueError('手機截圖編碼失敗')
        data=png.tobytes()
        if len(data)>8*1024*1024:raise ValueError('截圖超過 8 MiB')
        return {'status':'COMPLETED','message':'手機最新畫面；擷取時間：'+captured,'png':data}

    def diagnosis(self,job):
        import json
        folders=sorted((self.root/'docs/rwtd/exceptions').glob('exception_*'),reverse=True)
        if not folders:raise ValueError('沒有失敗紀錄')
        folder=folders[0]
        report=json.loads((folder/'report.json').read_text(encoding='utf-8'))
        frame=folder/'analyzed_frame.png'
        png=frame.read_bytes() if frame.is_file() and frame.stat().st_size<=8*1024*1024 else None
        self.finish(job,'最近失敗紀錄（歷史畫面，非目前截圖）：'+folder.name,data=report,png=png)


def install(app,root):
    """Qt lifecycle hook, called once from the app entry point; disabled by default."""
    import logging
    from pathlib import Path
    root=Path(root)
    from .discord_settings import NAME,BotSettings,settings as load_settings,read_token
    settings=load_settings(app.global_config.get_config(NAME))
    if settings['enabled'] is not True:return None
    if app.config.get('gui',{}).get('type')!='qt':
        raise ValueError('Discord control currently requires the native Qt desktop runtime')
    settings=BotSettings.load(settings,read_token())
    from .discord_bot import start_bot
    from PySide6.QtCore import QTimer
    global _service
    _service=service=RemoteControl(app,root)
    service.refresh_catalog()
    # OK owns this QApplication; only this timer performs lifecycle changes.
    qt_app=app.app.app
    timer=QTimer(qt_app);timer.setInterval(200)
    def tick():
        try:service.gui_tick()
        except Exception as exc:logging.getLogger(__name__).warning('Discord control timer failed: %s',type(exc).__name__)
    timer.timeout.connect(tick)
    timer.start()
    service.timer=timer
    try:
        service.bot=start_bot(service,settings)
    except Exception:
        timer.stop()
        service.mailbox.close()
        _service=None
        raise
    def close():
        timer.stop();service.mailbox.close();service.bot.close()
    qt_app.aboutToQuit.connect(close)
    return service


def prepare_config(config):
    from .discord_settings import option
    config=dict(config)
    config['global_configs']=list(config.get('global_configs',[]))+[option()]
    # custom_tasks loads ok_tasks/ and detects TriggerTask itself. Register
    # explicitly only for deployments without the native custom-task loader.
    if not config.get('custom_tasks'):
        config['trigger_tasks']=list(config.get('trigger_tasks',[]))+[
            ['ok_tasks.RwtdDiscordCaptureTask','RwtdDiscordCaptureTask']]
    return config
