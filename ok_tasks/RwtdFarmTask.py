"""Native OK one-time task owning the saved Canvas Farm schedule."""
import copy
import json
import time
from pathlib import Path
from src.rwtd.android_core.log_policy import timing_start,timing_mark

from ok_tasks.RwtdControlDashboardTask import RwtdControlDashboardTask
from src.rwtd.manual_device_tests import ManualDeviceTests
from src.rwtd.simulation_scheduler import SimulationController
from src.rwtd.exception_monitor import get_monitor
from src.rwtd.android_core.execution_lease import acquire
from src.rwtd.android_core.farm_runtime import FarmRuntime, failure_log_message
from src.rwtd.android_core.activity_settings import (
    DEFAULTS as ACTIVITY_DEFAULTS, DESCRIPTIONS as ACTIVITY_DESCRIPTIONS,
    SCHEDULES_KEY, resolve_activity_schedules,
)
from src.rwtd.android_core.loop_settings_override import (
    DEFAULTS as LOOP_DEFAULTS, DESCRIPTIONS as LOOP_DESCRIPTIONS,
    SOURCE_KEY as LOOP_SOURCE, settings_from_ok, apply_ok_settings,
)


class RwtdFarmTask(RwtdControlDashboardTask):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = 'RwtD Farm / 獨立流程排程'
        self.description = '讀取已儲存 Canvas；chain 順序交接，Trigger 優先。啟動即授權該流程的 Android 操作。'
        self.default_config = {'Canvas File':'目前選取', 'Stop Mode':'Until stopped', 'Farm Rounds':1,
                               'Display Size':'Auto',
                               'Operation Step Interval (s)':1.0,
                               'Allow Scheduler Discord':False, 'Failure Notifications':True,
                               'Detailed Logging':False,'Timing Logging':False,
                               'Template Mapping Logging':False}
        self.default_config.update(LOOP_DEFAULTS)
        self.default_config.update(ACTIVITY_DEFAULTS)
        self.config_description = {
            'Canvas File':'指定已儲存 Canvas；目前選取沿用 Dashboard 選擇。新增檔案後重新開啟 OK 更新選單；名稱變更仍以檔案 ID 識別。',
            'Stop Mode':'Until stopped：直到手動停止；Round count：完成指定整組 chain 輪數。',
            'Farm Rounds':'整組排程完成一次算一輪，記憶回訪不另算一輪。',
            'Allow Scheduler Discord':'通知節點沿用 OK 原生通知設定，預設關閉。',
            'Failure Notifications':'操作 chain 達失敗上限退出時，以 OK 原生通知送出；Trigger 未命中不通知。',
            'Detailed Logging':'一般 INFO；DEBUG 包含完整 ADB 指令、普通輪詢及節點／交接階段耗時。',
            'Timing Logging':'只增加耗時標記，保留一般 INFO；整晚量測勾選此項並關閉 Detailed Logging，不輸出完整 ADB 腳本或普通辨識 DEBUG。'}
        self.config_description['Template Mapping Logging']='逐一記錄 COCO 模板的螢幕映射倍率、模板尺寸、搜尋 ROI、門檻及最高信心值；偵錯時才開啟。'
        self.config_description['Display Size']='Auto 依 OK 擷取的 Android 原生畫面尺寸轉換；也可填 WIDTHxHEIGHT（如 1080x2340）作為預期尺寸，不符時停止輸入。模組仍維持 1080x2400 座標。'
        self.config_description['Operation Step Interval (s)']='每次點擊／滑動後至少等待的秒數，0～30；預設 1 秒。與 STEP 原有的操作後等待取較大值，不重複相加；等待可被停止或 Trigger 插隊中斷。'
        self.config_description.update(LOOP_DESCRIPTIONS)
        self.config_description.update(ACTIVITY_DESCRIPTIONS)
        self.config_type = {'Stop Mode':{'type':'drop_down','options':['Until stopped','Round count']},
                            LOOP_SOURCE:{'type':'drop_down','options':['Canvas','OK Override']}}
        self.config_type[SCHEDULES_KEY] = {'hidden':True}
        self.config_type['活動排程'] = {'type':'button', 'buttons':[
            {'text':'編輯活動清單', 'callback':self.edit_activity_schedules}]}
        self.config_description['活動排程'] = '新增／移除活動、選擇 Canvas、首次次數及間隔；依列表順序執行。'
        from src.rwtd.android_core.decision_graph import DecisionGraphStore
        from src.rwtd.android_core.module_store import ModuleStore
        from src.rwtd.profile import rwtd_profile
        from src.rwtd.manual_device_tests import validate_parameters
        profile=rwtd_profile(Path(__file__).resolve().parents[1],validate_parameters)
        canvases=DecisionGraphStore(profile,ModuleStore(profile)).catalog()
        self.config_type['Canvas File']={'type':'drop_down','options':['目前選取']+
            [row['id']+' · '+row['name'] for row in canvases]}
        self.support_schedule_task = True
        self.farm = None
        self.module_progress = {}
        self.farm_action_progress = {}
        self.uncertain_inputs = {}

    def edit_activity_schedules(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        parent = QApplication.activeWindow()
        if self.farm is not None and self.farm.status == 'RUNNING':
            QMessageBox.information(parent, '活動排程', '請先停止 FARM，再修改活動排程。')
            return
        from src.rwtd.android_core.decision_graph import DecisionGraphStore
        from src.rwtd.android_core.module_store import ModuleStore
        from src.rwtd.profile import rwtd_profile
        from src.rwtd.manual_device_tests import validate_parameters
        from src.rwtd.activity_schedule_dialog import ActivityScheduleDialog
        try:
            profile = rwtd_profile(Path(__file__).resolve().parents[1], validate_parameters)
            store = DecisionGraphStore(profile, ModuleStore(profile))
            selected = self.config.get('Canvas File', '目前選取')
            main_id = store.active_id() if selected == '目前選取' else selected.split(' · ',1)[0]
            ActivityScheduleDialog(self.config, store.catalog(), main_id, parent).exec()
        except ValueError as exc:
            QMessageBox.warning(parent, '活動排程設定無效', str(exc))

    @property
    def _owner(self):
        return self.farm.active_runtime

    def _activity_ready(self):
        # Retire outstanding captures before changing Canvas ownership. This
        # flag is independent of the user's latched Stop/capture cancellation.
        with self.controller.lock:
            self.controller.activity_inline_observers = True
            observers = self.controller.trigger_observers
            if observers:
                observers.sync()
                if any(slot['thread'].is_alive() for slot in observers.slots.values()):
                    return False
            if self.controller.trigger_work is not None:
                return False
            self.controller._invalidate_trigger_rounds()
            return True

    def _activity_switch(self, owner):
        previous = getattr(self, '_activity_owner', self.farm)
        self._activity_contexts[id(previous)] = (
            self.module_progress, self.farm_action_progress, self.uncertain_inputs)
        (self.module_progress, self.farm_action_progress,
         self.uncertain_inputs) = self._activity_contexts.pop(id(owner), ({}, {}, {}))
        if previous is not self.farm:
            self._activity_contexts.pop(id(previous), None)
        self._activity_owner = owner
        self.manual.time_guards = owner.time_guards
        self.controller.time_guards = owner.time_guards
        # Only the transition drains workers. The newly bound Canvas resumes
        # independent observation, including during activity repetitions.
        self.controller.activity_inline_observers = False
        self.trigger_frames.clear()
        self.controller._invalidate_trigger_frames()
        from src.rwtd.android_core.failure_evidence import FailureEvidence
        self.failure_evidence = FailureEvidence(self.root, notifier=self._failure_notifier())
        self._sync_farm()
        self.log_info('RWTD_ACTIVITY_SWITCH '+json.dumps(self.farm.activity_snapshot(include_runtime=False),
                                                        ensure_ascii=False))

    def _sync_farm(self):
        with self.controller.lock:
            self.controller.canvas_runtime = self._owner.runtime
            self.controller.trigger_scheduler = self._owner.watchers
            self.controller.continuation_owner = self._owner
            if self.controller.trigger_observers:
                self.controller.trigger_observers.sync()
        self.manual.decision_store.execution_locked = True
        self.manual.decision_store.input_locked = True
        event=self._owner.watchers.recovery.event
        if (event and event.get('cycle_restarted') and
                getattr(self,'_published_solver_cycle',None)!=event['id']):
            self._published_solver_cycle=event['id']
            self.log_info('RWTD_SOLVER_CYCLE_RESTART '+json.dumps(
                {key:event[key] for key in ('id','problem','candidates')},ensure_ascii=False))

    def _farm_tick_boundary(self):
        # While draining for an activity, do not dispatch old Canvas work.
        # Once active, its own matched Trigger inputs use the normal boundary.
        if self.farm.activity_pending and not self.farm.activity_active:
            return True
        return not self.controller._trigger_boundary()

    def _save_round_timing(self, event):
        """Persist summaries even when detailed/timing logging is disabled."""
        if (event.get('outcome') != 'SUCCESS' or
                event.get('details', {}).get('timing_basis') != 'notification_nodes'):
            return  # Failure exits remain diagnostic events, not timing samples.
        try:
            from datetime import datetime
            from src.rwtd.android_core.round_timing_store import RoundTimingStore
            from src.rwtd.android_core.retreatable_km import stage_from_name
            row = dict(event, session=self._farm_evidence_run,
                       stage=stage_from_name(event.get('canvas', '')))
            # Reports use the same local wall-time convention as OK logs;
            # elapsed seconds always come from the monotonic runtime clock.
            row['start'] = datetime.fromisoformat(event['start_utc']).astimezone().replace(tzinfo=None).isoformat()
            row['end'] = datetime.fromisoformat(event['end_utc']).astimezone().replace(tzinfo=None).isoformat()
            row['details'] = dict(event.get('details', {}), start_utc=event['start_utc'],
                                  end_utc=event['end_utc'], activity_id=event.get('activity_id'),
                                  repetition=event.get('repetition'))
            from src.rwtd.android_core.user_data import timing_db_path
            RoundTimingStore(timing_db_path(self.root)).append(row)
        except Exception as exc:
            self.log_warning('RWTD_ROUND_STORE_FAILED '+type(exc).__name__+': '+str(exc))
            self.info_set('Round statistics', '寫入失敗；請查看 RWTD_ROUND_STORE_FAILED')

    def _publish_farm_status(self):
        from src.rwtd.android_core.watchdog import mark
        runtime=self._owner.runtime
        mark(self,'WATCHDOG_STATE',canvas=self.info.get('Canvas'),
             chain=self._owner.schedule.current,node=runtime.cursor,
             round=self.farm.schedule.rounds,visits=runtime.visits.get(runtime.cursor,0),
             status=runtime.status,wait_seconds=max(0,runtime.due-runtime.now),
             queue=len(self.controller.trigger_scheduler.queue))
        session=getattr(self,'_log_session',None)
        if session is not None:session.report()
        activity = self.farm.activity_snapshot(include_runtime=False)
        activity_status = {'active':activity['active'], 'pending':activity['pending'],
                           'entries':activity['entries'],
                           'last_event':activity['history'][-1] if activity['history'] else None}
        if activity_status['last_event'] != getattr(self, '_published_activity_event', None):
            self.log_info('RWTD_ACTIVITY_EVENT '+json.dumps(activity_status['last_event'], ensure_ascii=False))
            self._published_activity_event = activity_status['last_event']
        values={'Chain':self._owner.schedule.current or '完成',
                'Farm rounds':self.farm.schedule.rounds,
                'Node':self._owner.runtime.current_node or self._owner.runtime.cursor,
                'Activities':json.dumps(activity_status, ensure_ascii=False)}
        previous=getattr(self,'_published_farm_status',{})
        for key,value in values.items():
            if key not in previous or previous[key]!=value:
                self.info_set(key,value)
        self._published_farm_status=values

    def _module(self, node, stop):
        from src.rwtd.android_core.workflow_model import resolve_step
        from src.rwtd.android_core.canvas_repeat import runs
        frozen = self._owner.modules[node['module']]
        current = self.manual.store.get(node['module'])
        if current['revision'] != frozen['revision']:
            raise ValueError('模組已變更；停止 Farm 後重新啟動')
        steps = [resolve_step(current, copy.deepcopy(s)) for s in current['steps']]
        steps = [copy.deepcopy(s) for _ in range(runs(node)) for s in steps]
        key = (self._owner.schedule.current, node['id'])
        completed = self.module_progress.get(key, 0)
        # Resume only the uncompleted tail; no extra capture/reverification.
        result = self.manual.execute_canvas_steps(steps[completed:], node['module'], frozen['revision'], stop,
            loop_settings=self._owner.runtime.graph['settings'], guard_node=node,
            guard_variables=self._owner.runtime.variables, first_guard_checked=True)
        if result.get('action_completed') or result.get('preempted'):
            self.module_progress.pop(key, None)
        else:
            records = result.get('records', [result])
            advanced = sum(bool(r.get('target_skipped') is True or
                               r.get('action_completed') and not r.get('failure_reason')
                                and r.get('status') in {'SUCCESS','MANUAL_VERIFIED','INPUT_SENT_UNVERIFIED'})
                           for r in records)
            self.module_progress[key] = completed+result.get('completed_step_index',advanced)
        return result

    def execute_remote_module(self, selected, runs):
        """Only RemoteControl.owner_tick calls this on the native device owner."""
        import threading
        from src.rwtd.android_core.workflow_model import resolve_step
        if self.controller.owner != threading.get_ident() or self.controller.device_stop.is_set():
            raise ValueError('遠端模組必須由有效的 FARM 操作執行端執行')
        if type(runs) is not int or not 1<=runs<=100:
            raise ValueError('執行次數須為 1～100')
        current=self.manual.store.get(selected['id'])
        if current['revision']!=selected['revision']:
            raise ValueError('模組在排隊後已變更；未執行')
        steps=[resolve_step(current,copy.deepcopy(step)) for _ in range(runs) for step in current['steps']]
        self.log_info('RWTD_DC_MODULE_START '+json.dumps({'module':current['id'],'runs':runs}))
        result=self.manual.execute_canvas_steps(steps,current['id'],current['revision'],
            self.controller.device_stop,request={'task_id':'DiscordModule','node':'DiscordModule',
                'preempt_operations':True,'definition':{'repeat_runs':runs}},
            loop_settings=self._owner.runtime.graph['settings'])
        self.log_info('RWTD_DC_MODULE_DONE '+json.dumps({'module':current['id'],
            'status':result.get('status'),'action_completed':result.get('action_completed')}))
        return result

    def _effect(self, payload):
        effect_started=timing_start(self)
        self._sync_farm()
        if self.controller.device_stop.is_set() or self.exit_is_set():
            self.farm.stop()
            return {'status':'BLOCKED','action_completed':False,'device_inputs':0}
        key = (self._owner.schedule.current, payload['node']['id'])
        if key in self.uncertain_inputs:
            return dict(self.uncertain_inputs[key], device_inputs=0)
        if payload['node']['kind']=='module':
            result = self._module(payload['node'], self.controller.device_stop)
        else:
            result = self.canvas_effect(payload, self.controller.device_stop)
        if self.controller.device_stop.is_set() or self.exit_is_set():
            self.farm.stop()
            return result
        if (payload['node']['kind'] in {'module','action'} and not result.get('action_completed')
                and not result.get('preempted')):
            last = result.get('records', [result])[-1]
            if last.get('input_attempted'):
                # Never resend an input whose delivery is uncertain. Consume
                # the bounded failure policy without another device command.
                self.uncertain_inputs[key] = copy.deepcopy(result)
        timing_mark(self,'NODE_EFFECT_RETURN',effect_started,node=payload['node']['id'],
                    kind=payload['node']['kind'],chain=self._owner.schedule.current,
                    round=self._owner.schedule.rounds,status=result.get('status'))
        self._timing_last_effect=(payload['node']['id'],result.get('status'),
                                 result.get('action_completed')) if effect_started is not None else None
        return result

    def canvas_effect(self, payload, stop, _observer_token=None):
        result = super().canvas_effect(payload, stop, _observer_token=_observer_token)
        farm = getattr(self, 'farm', None)
        if farm is not None and payload.get('source') == 'android_manual':
            owner = farm.active_runtime
            # Notification-to-notification elapsed time includes manual pauses.
            event = owner.notification_timing.observe(payload['node'], result, time.monotonic())
            if event is not None:
                self._save_round_timing(dict(owner.round_context, **event,
                    runtime_id=owner.round_runtime_id, round=owner.schedule.rounds+1,
                    revision=owner.graph.get('revision')))
        return result

    def _send_failure_notification(self, text, png):
        farm = getattr(self, 'farm', None)
        if farm is not None:
            farm.active_runtime.notification_timing.failure()
        return super()._send_failure_notification(text, png)

    def _queued(self, request):
        self._sync_farm()
        if self.controller.device_stop.is_set() or self.exit_is_set():
            self.farm.stop()
            return {'status':'BLOCKED','action_completed':False,'device_inputs':0}
        self.controller.trigger_work = request
        self.log_info('TRIGGER_START '+str({'trigger':request['task_id'],'node':request['node'],'id':request['id']}))
        try:
            result=self.manual.execute_trigger_request(request, self.controller.device_stop)
            self.log_info('TRIGGER_DONE '+str({'trigger':request['task_id'],'node':request['node'],
                                             'id':request['id'],'status':result.get('status'),
                                             'action_completed':result.get('action_completed'),
                                             'failure_reason':result.get('failure_reason'),
                                             'error_type':result.get('error_type')}))
            return result
        except Exception as exc:
            self.log_warning('TRIGGER_ERROR '+str({'trigger':request['task_id'],'node':request['node'],
                             'id':request['id'],'error_type':type(exc).__name__,'error_message':str(exc)[:500]}))
            raise
        finally:
            self.controller.trigger_work = None
            self.trigger_frames.clear()

    def _failure_notice(self, event):
        if self.config.get('Failure Notifications', True):
            self.farm.active_runtime.notification_timing.failure()
        if not event.get('evidence_dispatched'):
            self.on_exhaustion(dict(event,kind='chain',source='android_manual',
                event_id=f"chain:{self._farm_evidence_run}:{self.farm.schedule.rounds}:{event['chain']}:{event.get('failure_count')}",
                notify_discord=self.config.get('Failure Notifications',True)))
        detail = failure_log_message(event)
        self.log_warning(detail)
        self.controller._log('FAILED', detail, **{key: event.get(key) for key in
                             ('chain', 'node', 'node_name', 'node_kind', 'reason',
                              'failure_count', 'failure_limit')})
        text = event['name']+'：達到失敗上限，已交接並檢查失敗事件 Trigger。'
        self.log_warning(text)
        if self.config.get('Failure Notifications', True):
            try:
                self.notification(text, title='RwtD Farm', tray=True, screenshot=False)
            except Exception:
                self.log_warning('原生通知提交失敗；保留 chain 失敗狀態，繼續交接。')

    def _observe(self, rule):
        # PROGRESS shares its built-in ROI; other fields need a custom ROI.
        from src.rwtd.android_core.chain_ocr import reading_roi
        from src.rwtd.android_core.chain_ocr import numeric_result
        owner = self._owner
        node = owner.runtime.current_node or owner.runtime.cursor
        detail = {'chain':owner.schedule.current, 'node':node,
                  'field':rule.get('field'), 'op':rule.get('op'), 'value':rule.get('value'),
                  'threshold':rule.get('threshold', .8), 'result':'UNKNOWN', 'tokens':[]}
        try:
            self.failure_evidence.forget(node)
            roi = reading_roi(rule)
            detail['roi'] = roi
            if not roi:
                detail['reason'] = 'missing_roi'
                return {}
            from ok.feature.Box import Box
            backend, device = self.manual_backend()
            frame = backend.get_frame()
            self.validate_native_frame(frame)
            detail['captured_at_unix'] = time.time()
            detail['frame_size'] = [frame.shape[1], frame.shape[0]]
            x, y, width, height = roi
            patch = frame[y:y+height, x:x+width]
            detail['roi_mean_pixel'] = round(float(patch.mean()), 2) if patch.size else None
            # Retain exactly this node's OCR frame in the bounded evidence cache.
            # Disk output still occurs only when the failure budget is exhausted.
            self.failure_evidence.remember(node, frame, device=device)
            if self.controller.device_stop.is_set() or self.exit_is_set():
                detail['reason'] = 'cancelled'
                self.farm.stop()
                return {}
            boxes = self.ocr(box=Box(*roi), frame=frame, threshold=detail['threshold'],
                             _rwtd_progress=rule.get('field') == 'progress_km')
            parsed = numeric_result(boxes)
            detail.update(parsed)
            _, after = self.manual_backend()
            if after != device or self.controller.device_stop.is_set() or self.exit_is_set():
                detail.update(result='UNKNOWN', reason='device_changed_or_cancelled')
                return {}
            if parsed['result'] == 'VALID':
                return {rule['field']:parsed['number']}
            return {}
        except Exception as exc:
            detail.update(result='UNKNOWN', reason=type(exc).__name__+': '+str(exc)[:200])
            return {}
        finally:
            try:
                from src.rwtd.android_core.ocr_records import box_tokens,publish_result
                if 'frame' in locals():
                    effective=getattr(self,'recognition_roi',None)
                    raw=box_tokens(boxes) if 'boxes' in locals() else None
                    detail['ocr_record']=publish_result(self,raw,frame,
                        roi=effective(roi,'ocr',progress=rule.get('field')=='progress_km') if callable(effective) and roi else roi,
                        configured_roi=roi,threshold=detail['threshold'],route='farm_chain',node=node,
                        result=detail['result'],value=detail.get('number'),reason=detail.get('reason'))
            except Exception:pass
            owner.chain_reading = detail
            from src.rwtd.android_core.log_policy import recognition
            recognition(self,getattr(self,'controller',None),'CHAIN_OCR',
                'RWTD_CHAIN_OCR node='+str(node)+' result='+detail['result']+' reason='+str(detail.get('reason')),
                ('chain_ocr',node,rule.get('field')),detail['result'],recognition=detail.get('ocr_record'))

    def run(self):
        from src.rwtd.discord_control import current
        try:
            workspace=getattr(self,'_native_workspace',None)
            if workspace is not None:
                return workspace.serve(self,'farm')
            # Standalone native Farm keeps its original execution path.
            for _ in self.run_service():
                pass
        except Exception as exc:
            if current():current().ended(self,exc)
            raise

    def run_service(self, continue_running=None):
        task_started=timing_start(self)
        self.root = Path(__file__).resolve().parents[1]
        from src.rwtd.android_core.record_retention import install_log_limits,disk_space_reason,enforce
        install_log_limits(self.root)
        enforce(self.root,'logs','logs')
        from src.rwtd.android_core.log_policy import LogSession
        from src.rwtd.android_core.watchdog import registry as watchdog_registry
        manager=getattr(getattr(self,'executor',None),'device_manager',None)
        with acquire(self.root, self),LogSession(self.config.get('Detailed Logging',False),manager) as session, watchdog_registry.session(self):
            self._log_session=session
            timing_mark(self,'TASK_RUN_START',task_started,task_name=self.name)
            self.monitor = get_monitor(self.root)
            import uuid
            from src.rwtd.android_core.failure_evidence import FailureEvidence
            self._farm_evidence_run=uuid.uuid4().hex
            self.failure_evidence=FailureEvidence(self.root,notifier=self._failure_notifier())
            self.manual = ManualDeviceTests(self, self.root)
            canvas_file=getattr(self,'_discord_canvas_id',None) or self.config.get('Canvas File','目前選取')
            if canvas_file!='目前選取':
                canvas_id=canvas_file.split(' · ',1)[0]
                if canvas_id not in {row['id'] for row in self.manual.decision_store.catalog()}:
                    raise ValueError('指定 Canvas 設定檔不存在；請重新選擇')
                self.manual.decision_store.canvas_id=canvas_id
            from src.rwtd.android_core.time_guard import TimeGuards
            self.manual.time_guards=TimeGuards(self.root,namespace=self.manual.decision_store.active_id())
            graph = self.manual.decision_store.load()
            if not graph:
                raise ValueError('請先在 Dashboard 儲存 Canvas 流程')
            override = settings_from_ok(self.config)
            graph = apply_ok_settings(graph, override)
            chosen=next(row for row in self.manual.decision_store.catalog() if row['selected'])
            from src.rwtd.android_core.retreatable_km import with_stage, stage_from_name, DEFAULTS
            graph = with_stage(graph, stage_from_name(chosen['name']), inherit=True)
            game_stage = graph.get('settings',{}).get('game_stage')
            self.log_info('RWTD_RETREATABLE_KM '+json.dumps({'canvas':chosen['id'],
                'game_stage':game_stage,'retreatable_km':DEFAULTS.get(game_stage)},ensure_ascii=False))
            self.log_info('Farm Canvas：'+chosen['name']+' ['+chosen['id']+']')
            self.info_set('Canvas',chosen['name'])
            modules = {m['id']:m for m in self.manual.store.catalog()}
            from src.rwtd.android_core.decision_graph import DecisionGraphStore
            activity_entries = []
            catalog = self.manual.decision_store.catalog()
            for settings in resolve_activity_schedules(self.config, catalog, chosen['id']):
                store = DecisionGraphStore(self.manual.decision_store.profile,
                                           self.manual.store, canvas_id=settings['canvas_id'])
                activity_graph = store.load()
                if not activity_graph:
                    raise ValueError('活動 Canvas 尚未儲存：'+settings['canvas_id'])
                name = next(row['name'] for row in catalog if row['id']==settings['canvas_id'])
                activity_entries.append(dict(settings, graph=activity_graph, name=name,
                    time_guards=TimeGuards(self.root, namespace=settings['canvas_id'])))
            mode = self.config.get('Stop Mode', 'Until stopped')
            if mode not in {'Until stopped','Round count'}:
                raise ValueError('Invalid Farm stop mode')
            rounds = self.config.get('Farm Rounds', 1) if mode=='Round count' else None
            self.controller = SimulationController(manual_handler=self.manual.handle, allow_manual=True,
                                                   definitions=list(modules.values()),allow_simulation=False)
            self.controller.mode = 'Manual Device Test'
            self.controller.detailed_logging=self.config.get('Detailed Logging',False)
            self.controller.runtime_metrics=session.metrics
            self.controller.exhaustion_callback=self.on_exhaustion
            self.controller._owned()
            # Reuse macro input preparation while suppressing automatic
            # before/after outcome verification; Farm advances on delivery.
            self.controller.manual_confirmation_only = True
            self.controller.device_stop.clear()
            self.controller.canvas_cancel.clear()
            self.controller.trigger_cancel.clear()
            self.trigger_frames.clear()
            self.module_progress.clear()
            self.farm_action_progress.clear()
            self.uncertain_inputs.clear()
            permission = self.config.get('Allow Scheduler Discord', False) is True
            self.manual.canvas_notify_discord=permission
            self._discord_pause_total=0
            self._discord_pause_at=None
            def farm_clock():
                now=time.monotonic()
                pending=now-self._discord_pause_at if self._discord_pause_at is not None else 0
                return now-self._discord_pause_total-pending
            self.farm = FarmRuntime(graph, modules, self._effect, self._queued, self._failure_notice,
                                    farm_clock, rounds, 'android_manual', permission, self._observe,
                                    external_watchers=True,
                                    round_complete=None,  # SQL samples come only from marked notifications.
                                    round_context={'session':self._farm_evidence_run, 'scope':'farm',
                                                   'canvas_id':chosen['id'], 'canvas':chosen['name']},
                                    time_guards=self.manual.time_guards,
                                    exhaustion_callback=self.on_exhaustion,
                                    invalidate_observers=self.controller._invalidate_trigger_frames)
            self._activity_owner = self.farm
            self._activity_contexts = {}
            self.farm.configure_activities(activity_entries, activity_ready=self._activity_ready,
                                           activity_switch=self._activity_switch)
            self.log_info('RWTD_ACTIVITY_CONFIG '+json.dumps(self.farm.activity_snapshot(),
                                                          ensure_ascii=False))
            self._sync_farm()
            from src.rwtd.android_core.manual_module_request import ManualModuleRequest
            manual_requests=ManualModuleRequest(self.root)
            manual_requests.consume(self)
            self.controller.enable_trigger_observers(self.observe_trigger,self.release_trigger_observer)
            previous = time.monotonic()
            next_log_cleanup = previous + 60
            self._published_farm_status={}
            timing_mark(self,'TASK_READY',task_started,task_name=self.name,
                        chain=self.farm.schedule.current,round=self.farm.schedule.rounds)
            try:
                from src.rwtd.discord_control import current as remote_control
                remote=remote_control()
                if remote:remote.started(self)
                yield self
                while (continue_running() if continue_running else self.enabled) and not self.exit_is_set() and self.farm.status=='RUNNING':
                    if self.monitor.stop_requested:
                        self.controller.device_stop.set()
                        break
                    if self.controller.device_stop.is_set():break
                    if remote and not remote.owner_tick(self):
                        self.controller.trigger_observers.sync()
                        self.sleep(.2)
                        previous=time.monotonic()
                        yield self
                        continue
                    if manual_requests.consume(self):
                        self.controller._invalidate_trigger_frames()
                        self.sleep(.2)
                        yield self
                        continue
                    if time.monotonic() >= next_log_cleanup:
                        enforce(self.root,'logs','logs')
                        next_log_cleanup = time.monotonic() + 60
                    observer_reason = self.controller.trigger_observers.unavailable_reason()
                    if observer_reason:
                        reason = 'RWTD_FARM_OBSERVERS_UNAVAILABLE ' + observer_reason + '；停止 Farm，未自動清除取消或繼續送出操作'
                        self.log_warning(reason)
                        self.info_set('Warning', reason)
                        self.controller.device_stop.set()
                        self.farm.stop()
                        break
                    disk_reason=disk_space_reason(self.root)
                    if disk_reason:
                        self.log_warning(disk_reason)
                        self.info_set('Storage',disk_reason)
                        self.controller.device_stop.set()
                        break
                    now = time.monotonic()
                    old_owner = self._owner
                    old = old_owner.schedule.current
                    old_runtime = old_owner.runtime
                    previous_handoff = (old_owner.schedule.handoff_events[-1]
                                        if old_owner.schedule.handoff_events else None)
                    if old_owner.check_chain_timeout():
                        self.log_warning(failure_log_message(old_owner.schedule.events[-1]))
                    # Insert queued operations only after the preceding node
                    # has returned; observers never execute device input.
                    if self._farm_tick_boundary():
                        tick_started=timing_start(self)
                        tick_node=self._owner.runtime.cursor
                        tick_runtime=self._owner.runtime
                        tick_visits=tick_runtime.visits.get(tick_node,0)
                        self._timing_last_effect=None
                        timing_mark(self,'FARM_TICK_START',node=tick_node,chain=old,
                                    round=self.farm.schedule.rounds,scheduler_now=tick_runtime.now,
                                    due=tick_runtime.due)
                        self.farm.tick(min(now-previous,1))
                        timing_mark(self,'FARM_TICK_DONE',tick_started,node=tick_node,
                                    next_node=self._owner.runtime.cursor,chain=old,
                                    status=self._owner.runtime.status,
                                    current_node=self._owner.runtime.current_node,
                                    scheduler_now=self._owner.runtime.now,due=self._owner.runtime.due,
                                    pending=bool(self._owner.runtime.pending_effect))
                        if self._timing_last_effect is not None:
                            effect_node,effect_status,action_completed=self._timing_last_effect
                            timing_mark(self,'NODE_COMPLETE',node=effect_node,chain=old,
                                        next_node=self._owner.runtime.cursor,status=self._owner.runtime.status,
                                        result_status=effect_status,action_completed=action_completed,
                                        pending_next=self._owner.runtime.pending_next,
                                        pending=bool(self._owner.runtime.pending_effect))
                        elif (tick_started is not None and self._owner.runtime is tick_runtime
                              and tick_runtime.visits.get(tick_node,0)>tick_visits):
                            flow_node=next((n for n in tick_runtime.graph['nodes']
                                            if n['id']==tick_node),{})
                            if flow_node.get('kind') in {'start','end'}:
                                timing_mark(self,'NODE_COMPLETE',tick_started,node=tick_node,
                                            kind=flow_node['kind'],chain=old,
                                            status=tick_runtime.status,next_node=tick_runtime.cursor,
                                            due=tick_runtime.due,scheduler_now=tick_runtime.now)
                    sync_started=timing_start(self)
                    self._sync_farm()
                    previous = now
                    if (self._owner is old_owner and
                            (old != self._owner.schedule.current or self._owner.runtime is not old_runtime)):
                        self.module_progress.clear()
                        self.farm_action_progress.clear()
                        self.uncertain_inputs.clear()
                        self.trigger_frames.clear()
                        self.controller._invalidate_trigger_frames()
                    handoff = (self._owner.schedule.handoff_events[-1]
                               if self._owner.schedule.handoff_events else None)
                    if (handoff is not None and handoff is not previous_handoff
                            and handoff.get('reason') == 'TRIGGER_CROSS_CHAIN'):
                        self.log_info('RWTD_TRIGGER_CROSS_CHAIN '+json.dumps(handoff,ensure_ascii=False))
                        self.info_set('Chain handoff',
                                      f"Trigger {handoff['trigger']}：{handoff['source']} → "
                                      f"{handoff['target']} / {handoff['target_node']}；來源未完成，不計成功或失敗")
                    self._publish_farm_status()
                    timing_mark(self,'FARM_STATUS_DONE',sync_started,chain=self.farm.schedule.current,
                                node=self.farm.runtime.cursor,round=self.farm.schedule.rounds)
                    wait_started=timing_start(self)
                    self.sleep(.2)
                    timing_mark(self,'FARM_WAIT_DONE',wait_started,requested_seconds=.2,
                                node=self.farm.runtime.cursor)
                    yield self
                if self.farm.status == 'BLOCKED':
                    self.log_warning('RWTD_FARM_BLOCKED '+self.farm.reason)
                    self.info_set('Warning', self.farm.reason)
            finally:
                self.controller.device_stop.set()
                self.farm.stop()
                self.controller.close()
                self.manual.decision_store.execution_locked = False
                self.manual.decision_store.input_locked = False
                self.trigger_frames.clear()
                self.module_progress.clear()
                self.farm_action_progress.clear()
                self.uncertain_inputs.clear()
                self._activity_contexts.clear()
                if remote:remote.ended(self)
