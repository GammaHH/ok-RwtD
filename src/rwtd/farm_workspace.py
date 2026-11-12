"""Shared HTTP interface; each view belongs to its registered native OK TASK."""
import copy
import threading
from collections import deque


class FarmWorkspace:
    def __init__(self, host, controller, preview, roi, task=None, calibration=None, statistics=None, theme=None):
        self.host, self.controller, self.preview, self.roi = host, controller, preview, roi
        self.calibration = calibration
        self.statistics = statistics
        self.theme = theme
        self.task = task or next((item for item in getattr(getattr(host,'executor',None),'onetime_tasks',[])
                                  if type(item).__name__=='RwtdFarmTask'),None)
        self.current = 'dashboard'
        tasks=getattr(getattr(host,'executor',None),'onetime_tasks',[])
        roi_task=next((item for item in tasks if type(item).__name__=='RwtdRoiOverviewTask'),None)
        calibration_task=next((item for item in tasks if type(item).__name__=='RwtdResolutionCalibrationTask'),None)
        self.owners={'dashboard':host,'farm':self.task,'roi':roi_task,'calibration':calibration_task}
        self.owners['statistics']=next((item for item in tasks if type(item).__name__=='RwtdRoundStatisticsTask'),None)
        self.owners['theme']=next((item for item in tasks if type(item).__name__=='RwtdThemeSettingsTask'),None)
        self.pending_handoff=None
        self.dashboard_turn=None
        self.cleanup=None
        self.closed=False
        self.exit_request=None
        self.exit_ready=False
        for owner in self.owners.values():
            if owner is not None:
                owner._native_workspace=self
        self.queue = deque()
        self.lock = threading.RLock()
        self.running = False
        self.service = None
        self.results = {}
        self.cached = {}
        self.refresh()

    def enqueue_remote(self,service,job,canvas_id=None):
        command='farm_start' if job['command']=='farm_start' else 'farm_module'
        return self.enqueue('discord:'+job['id'],command,{},_remote=(service,job,canvas_id))

    def enqueue_remote_exit(self,service,job,owner):
        return self.enqueue('discord:'+job['id'],'task_exit',{},_remote=(service,job,owner))

    def enqueue(self, request_id, command, payload, *, _remote=None):
        commands={'switch','farm_start','farm_stop','farm_save'}
        if _remote is not None:
            commands.update({'farm_module','task_exit'})
        if not isinstance(request_id,str) or not request_id or len(request_id)>100 or command not in commands or not isinstance(payload,dict):
            raise ValueError('Invalid workspace command')
        if _remote is None and request_id.startswith('discord:'):
            raise ValueError('Reserved workspace request ID')
        with self.lock:
            if self.closed or self.pending_handoff or self.exit_request is not None:
                raise ValueError('TASK 正在交接或工作區已關閉，請等待目標任務就緒')
            if request_id in self.results:
                return copy.deepcopy(self.results[request_id])
            if len(self.queue)>=32:
                raise ValueError('Workspace queue full')
            if command=='farm_stop' and self.running:
                # Cancellation latch only; HTTP workers never advance a service.
                controller=getattr(self.task,'controller',None)
                if controller:
                    controller.device_stop.set()
            self.queue.append((request_id,command,copy.deepcopy(payload),_remote))
            self.results[request_id]={'status':'QUEUED'}
            return {'status':'QUEUED','request_id':request_id}

    def blocked(self):
        state=self.controller.snapshot()
        runtime=state.get('scheduler_runtime',{})
        if self.running or self.service is not None:
            return '請先手動停止 FARM，等待目前操作退出後再切換。'
        if self.preview.automatic or self.preview.initial:
            return '請先手動停止 Dashboard 自動擷取，再切換。'
        if (runtime.get('locked') or runtime.get('main_status') in {'RUNNING','PAUSED','BLOCKED','WAITING_MANUAL'}
                or state.get('device_pending') or state.get('capture_pending') or self.host.preview_job is not None
                or self.controller.queue or self.controller.trigger_work is not None
                or self.controller.trigger_scheduler.active):
            return '請先停止 Dashboard 測試、監看與擷取，等待退出後再切換。'
        if self.roi and not self.roi.jobs.empty():
            return 'ROI 尚未完成目前工作，請稍後再切換。'
        if self.calibration and not self.calibration.jobs.empty():
            return '解析度對照尚未完成目前工作，請稍後再切換。'
        if self.statistics and not self.statistics.jobs.empty():
            return '輪次統計尚未完成，請稍後再切換。'
        if self.theme and not self.theme.jobs.empty():
            return '配色設定尚未完成，請稍後再切換。'
        return ''

    def dashboard_access(self, command):
        if self.closed or self.pending_handoff or self.exit_request is not None:
            raise ValueError('TASK 交接或退出期間禁止操作')
        if self.current!='dashboard':
            raise ValueError('請先停止並儲存目前功能，再切回 Dashboard。')

    def process_next(self):
        with self.lock:
            item=self.queue.popleft() if self.queue else None
        if not item:
            return
        request_id,command,payload,remote=item
        result={'status':'SUCCESS'}
        try:
            if remote is not None:
                service,job,remote_value=remote
                service.check_pending(job)
            if command=='switch':
                if set(payload)!={'source','target'} or payload['source']!=self.current or payload['target'] not in self.owners:
                    raise ValueError('Workspace changed; refresh before switching')
                if payload['target']=='roi' and self.roi is None:
                    raise ValueError('ROI service unavailable')
                if payload['target']=='calibration' and self.calibration is None:
                    raise ValueError('解析度對照服務不可用')
                if payload['target']=='statistics' and self.statistics is None:
                    raise ValueError('輪次統計服務不可用')
                if payload['target']=='theme' and self.theme is None:
                    raise ValueError('配色設定服務不可用')
                if payload['target']=='farm' and self.task is None:
                    raise ValueError('原生 FARM TASK 未註冊')
                target=self.owners.get(payload['target'])
                if target is None:
                    raise ValueError('目標原生 TASK 未註冊')
                reason=self.blocked()
                if reason:
                    raise ValueError(reason)
                source=self.owners[self.current]
                if getattr(source,'exit_after_task',False) or source.config.get('Exit After Task',False):
                    raise ValueError('目前 TASK 設定完成後退出 OK，請先關閉此選項再切換')
                if any(t is not source and (getattr(t,'running',False) or getattr(t,'enabled',False))
                       for t in getattr(source.executor,'onetime_tasks',[])):
                    raise ValueError('另一個 TASK 正在執行或排隊，請先停止該任務')
                self.preview.automatic=self.preview.initial=False
                self.pending_handoff=(request_id,payload['target'])
                self.refresh()
                return
            elif command=='task_exit':
                owner=remote_value
                if self.owners.get(self.current) is not owner or owner.executor.current_task is not owner:
                    raise ValueError('工作區任務已變更；未退出')
                if getattr(owner,'exit_after_task',False) or owner.config.get('Exit After Task',False):
                    raise ValueError('目前任務設定完成後退出 OK，請在電腦端關閉該選項')
                reason=self.blocked()
                if reason:
                    raise ValueError(reason)
                with self.lock:
                    if self.queue or any(t is not owner and (getattr(t,'enabled',False) or getattr(t,'running',False))
                                         for t in owner.executor.onetime_tasks):
                        raise ValueError('仍有工作排隊，請等待完成後再退出')
                    self.exit_request=(service,job)
                return
            elif self.current!='farm' or self.task is None:
                raise ValueError('請先切換到 FARM')
            elif command in {'farm_start','farm_module'}:
                if payload or self.running:
                    raise ValueError('FARM 已執行或參數無效')
                if self.task.executor.current_task is not self.task or not self.task.enabled:
                    raise ValueError('FARM 尚未取得原生 TASK 操作權，禁止啟動')
                reason=self.blocked()
                if reason:
                    raise ValueError(reason)
                if remote is not None and any(
                        t is not self.task and (getattr(t,'enabled',False) or getattr(t,'running',False))
                        for t in self.task.executor.onetime_tasks):
                    raise ValueError('另一個原生 TASK 正在執行或排隊')
                if command=='farm_module':
                    if not service.run_module_job(self.task,job):
                        result={'status':'FAILED','reason':'模組未完成'}
                else:
                    self.running=True
                    self.service=self.task.run_service(lambda:self.running and self.task.enabled)
                    if remote is not None:
                        self.task._discord_canvas_id=remote_value
                    try:
                        next(self.service)
                    finally:
                        if remote is not None:
                            self.task._discord_canvas_id=None
                    self.host.manual.decision_store.execution_locked=True
                    self.host.manual.decision_store.input_locked=True
            elif command=='farm_stop':
                if payload:
                    raise ValueError('Stop takes no parameters')
                self.close_service()
            elif command=='farm_save':
                if self.running or set(payload)!={'settings','expected'}:
                    raise ValueError('請先停止 FARM 再儲存設定')
                keys=set(self.task.default_config)-{'活動排程'}
                current={key:self.task.config.get(key,self.task.default_config[key]) for key in keys}
                if payload['expected']!=current:
                    raise ValueError('FARM 設定已變更，請重新載入後再編輯')
                settings=payload['settings']
                if not isinstance(settings,dict) or set(settings)!=keys:
                    raise ValueError('FARM settings incomplete')
                for key,value in settings.items():
                    default=self.task.default_config[key]
                    if type(default) is bool and type(value) is not bool or type(default) is int and type(value) is not int or type(default) is float and type(value) not in (int,float) or isinstance(default,str) and not isinstance(value,str):
                        raise ValueError('Invalid FARM setting: '+key)
                from .android_core.activity_settings import resolve_activity_schedules
                from .android_core.loop_settings_override import settings_from_ok
                settings_from_ok(settings)
                catalog=self.host.manual.decision_store.catalog()
                selected=settings['Canvas File'].split(' · ',1)[0]
                if selected=='目前選取':
                    selected=self.host.manual.decision_store.active_id()
                resolve_activity_schedules(settings,catalog,selected)
                self.task.config.update(settings)
                self.task.config.save_file()
                from pathlib import Path
                import json
                saved=json.loads(Path(self.task.config.config_file).read_text(encoding='utf-8-sig'))
                if any(saved.get(key)!=value for key,value in settings.items()):
                    raise ValueError('FARM 設定未完整寫入，請重試儲存')
        except Exception as exc:
            result={'status':'FAILED','reason':str(exc)}
            if command=='farm_start' and (self.running or self.service is not None):
                try:
                    self.close_service()
                except Exception as cleanup_error:
                    if remote is None:
                        raise
                    result={'status':'FAILED','reason':'FARM 啟動清理失敗：'+type(cleanup_error).__name__}
        if remote is not None:
            if command=='farm_start':
                service.finish_workspace_start(job,result)
            elif command=='task_exit':
                service.mailbox.finish(job,{'status':'FAILED','message':result['reason']})
            elif not job['future'].done():
                service.mailbox.finish(job,{'status':'FAILED','message':result.get('reason') or '模組未完成'})
        with self.lock:
            self.results[request_id]=result
            while len(self.results)>100:
                self.results.pop(next(iter(self.results)))
        self.refresh()

    def tick(self):
        if self.service is not None:
            try:
                next(self.service)
            except StopIteration:
                self.service=None
                self.close_service()
            except Exception as exc:
                self.results['runtime']={'status':'FAILED','reason':type(exc).__name__+': '+str(exc)}
                self.close_service()
        self.refresh()

    def close_service(self):
        owned=self.running or self.service is not None
        self.running=False
        controller=getattr(self.task,'controller',None) if self.task else None
        if self.service is not None:
            if controller:
                controller.device_stop.set()
            service,self.service=self.service,None
            try:
                service.close()
            finally:
                if owned:
                    self.host.manual.decision_store.execution_locked=False
                    self.host.manual.decision_store.input_locked=False
        # Release host edit locks only once Farm cleanup has completed.
        if owned:
            self.host.manual.decision_store.execution_locked=False
            self.host.manual.decision_store.input_locked=False

    def serve(self,owner,view):
        """Called by the target's real native run(), never by an HTTP worker."""
        if self.closed or self.owners.get(view) is not owner or owner.executor.current_task is not owner:
            raise ValueError('Workspace native TASK ownership mismatch')
        try:
            if any(task is not owner and getattr(task,'running',False)
                   for task in getattr(owner.executor,'onetime_tasks',[])):
                raise ValueError('另一個原生 TASK 尚未退出，禁止取得工作區操作權')
            if self.pending_handoff:
                request_id,target=self.pending_handoff
                if target!=view:
                    raise ValueError('Unexpected native TASK entered workspace')
                self.current=view
                self.pending_handoff=None
                self.results[request_id]={'status':'SUCCESS'}
            self.refresh()
            owner.log_info('RWTD_WORKSPACE_OWNER '+view+' current_task='+type(owner.executor.current_task).__name__)
            while owner.enabled and not owner.exit_is_set() and not self.preview.closing:
                if view=='roi' and self.roi.closing.is_set():
                    break
                if view=='calibration' and self.calibration.closing.is_set():
                    break
                if view=='statistics' and self.statistics.closing.is_set():
                    break
                if view=='theme' and self.theme.closing.is_set():
                    break
                self.process_next()
                if self.exit_request is not None:
                    break
                if self.pending_handoff:
                    break
                if view=='dashboard':
                    self.dashboard_turn()
                elif view=='farm' and self.running:
                    self.tick()
                elif view=='roi':
                    self.roi.process_next()
                elif view=='calibration':
                    self.calibration.process_next()
                elif view=='statistics':
                    self.statistics.process_next()
                elif view=='theme':
                    self.theme.process_next()
                self.controller.wake.wait(.2)
            if self.pending_handoff:
                _,target=self.pending_handoff
                # Public enable() queues the target. TaskExecutor finishes the
                # source run and clears running before it enters target.run().
                self.owners[target].enable()
                owner.log_info('RWTD_WORKSPACE_HANDOFF '+view+' -> '+target+'; source run returning to TaskExecutor')
                return
        except BaseException:
            self.shutdown()
            raise
        try:
            self.shutdown()
        except Exception as exc:
            if self.exit_request is not None:
                service,job=self.exit_request
                service.mailbox.finish(job,{'status':'FAILED','message':'工作區清理失敗：'+type(exc).__name__})
            raise
        if self.exit_request is not None:
            self.exit_ready=True

    def shutdown(self):
        if self.closed:
            return
        self.closed=True
        try:
            self.close_service()
        finally:
            with self.lock:
                pending=list(self.queue)
                self.queue.clear()
            for _,command,_,remote in pending:
                if remote is None:
                    continue
                service,job,_=remote
                result={'status':'FAILED','reason':'工作區已關閉；指令未執行'}
                if command=='farm_start':
                    service.finish_workspace_start(job,result)
                else:
                    service.mailbox.finish(job,{'status':'FAILED','message':result['reason']})
            for owner in self.owners.values():
                if owner is not None and getattr(owner,'_native_workspace',None) is self:
                    owner._native_workspace=None
            if self.cleanup:
                self.cleanup()

    def refresh(self):
        task=self.task
        native=getattr(self.host.executor,'current_task',None)
        value={'current':self.current,'running':self.running,'available':task is not None,
               'native_owner':type(native).__name__ if native is not None else None,
               'handoff_pending':bool(self.pending_handoff)}
        if task:
            value.update(settings={key:copy.deepcopy(task.config.get(key,default))
                         for key,default in task.default_config.items() if key!='活動排程'},
                         descriptions=copy.deepcopy(task.config_description),
                         types={key:copy.deepcopy(spec) for key,spec in task.config_type.items() if spec.get('type')=='drop_down'},
                         canvases=self.host.manual.decision_store.catalog())
            if getattr(task,'farm',None) is not None:
                owner=task._owner
                activity=task.farm.activity
                value.update(canvas=activity['name'] if task.farm.activity_active else task.info.get('Canvas'),
                             canvas_id=owner.graph.get('canvas_id') or (activity['canvas_id'] if task.farm.activity_active else task.manual.decision_store.active_id()),
                             modules=[{'id':m['id'],'name':m['name']} for m in task.manual.store.catalog()],
                             trigger_runtime=task.controller.trigger_scheduler.snapshot(),
                             chain=owner.schedule.current,node=owner.runtime.current_node,
                             runtime=owner.runtime.snapshot(),graph=copy.deepcopy(owner.graph),
                             rounds=task.farm.schedule.rounds)
        with self.lock:
            value['results']=copy.deepcopy(self.results)
            self.cached=value

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.cached)
