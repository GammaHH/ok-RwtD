"""Simulation-only modules and state machine. No capture, ADB or input APIs."""
from __future__ import annotations

import math
import time
import copy
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

PHASES = ("REBORN", "INITIAL_SETUP", "FARMING", "SETTLEMENT", "RESTART")


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


@dataclass
class SimState:
    phase: str = "REBORN"
    now: float = 0
    current_progress_km: float | None = None
    progress_verified: bool = False
    last_successful_teleport_progress_km: float | None = None
    teleport_pending: bool = False
    food_count: int | None = None
    food_verified: bool = False
    store_reward_in_roi: bool = False
    store_reward_verified: bool = False
    exception_confirmed: bool = False
    stage_complete_confirmed: bool = False
    available_modules: set = field(default_factory=set)
    generation: int = 0

    def distance(self):
        if (not self.progress_verified or not valid_number(self.current_progress_km)
                or not valid_number(self.last_successful_teleport_progress_km)):
            return None
        difference = self.current_progress_km - self.last_successful_teleport_progress_km
        return difference if difference >= 0 else None


@dataclass
class SimContext:
    state: SimState
    verified_success: bool = True
    generation: int = 0


class GameplayModule:
    """Public module interface; timings are simulation contracts, not swipe calibration."""
    def __init__(self, name, cooldown=2, role=None, duration=1, steps=None):
        self.name = name
        self.role = role or name
        self.steps = steps or []
        self.cooldown = cooldown
        self.status = "READY"
        self.failure_reason = None
        self.next_allowed = 0
        self.duration = duration

    def can_run(self, state):
        if state.exception_confirmed:
            return False, "Confirmed exception blocks new operations"
        if self.name not in state.available_modules:
            return False, "Simulated precondition unavailable"
        if state.now < self.next_allowed:
            return False, "COOLDOWN"
        if self.role == "Teleport":
            distance = state.distance()
            if state.teleport_pending or distance is None or distance < 500:
                return False, "Teleport needs verified distance >=500 km, known anchor and no pending operation"
        if self.role == "FoodUpgrade" and (not state.food_verified or type(state.food_count) is not int or state.food_count < 50):
            return False, "Food needs verified count >=50"
        if self.role == "StoreReward" and not (state.store_reward_verified and state.store_reward_in_roi):
            return False, "Reward must be confirmed inside designated Store ROI"
        if self.role == "Settlement" and not (state.phase == "SETTLEMENT" and state.stage_complete_confirmed):
            return False, "Stage completion must be confirmed"
        return True, "Simulated preconditions ready"

    def execute(self, context):
        ready, reason = self.can_run(context.state)
        if not ready:
            self.status, self.failure_reason = ("COOLDOWN" if reason == "COOLDOWN" else "BLOCKED"), reason
            return None
        self.status, self.failure_reason = "RUNNING", None
        return {"simulated": True, "duration_seconds": self.duration,
                "quest_hold_seconds": 5 if self.role == "QuestUpgrade" else None,
                "quest_holds": 4 if self.role == "QuestUpgrade" else None,
                "summon_hold_seconds": 9 if self.role == "ArmySummon" else None,
                "store_wait_seconds": 3 if self.role == "StoreReward" else None,
                "steps": copy.deepcopy(self.steps), "swipe": None, "device_inputs": 0}

    def verify_success(self, context):
        return context.verified_success is True and context.generation == context.state.generation


class SimulationController:
    """One owner thread executes commands; HTTP workers only enqueue/read snapshots."""
    def __init__(self, failure_callback=None, capacity=64, manual_handler=None, allow_manual=False, definitions=None, roles=None, allow_simulation=True):
        self.state = SimState()
        if definitions is None:
            from pathlib import Path
            from src.rwtd.manual_device_tests import validate_parameters
            from src.rwtd.android_core.module_store import ModuleStore
            from src.rwtd.profile import rwtd_profile
            store = ModuleStore(rwtd_profile(Path(__file__).resolve().parents[2], validate_parameters))
            definitions, roles = store.catalog(), store.scheduler()
        self.roles = roles or {}
        self.modules = {item["id"]: GameplayModule(item["id"], role=next((role for role, target in self.roles.items() if target == item["id"]), item.get("role")),
                                                duration=item.get("simulation_seconds", 1), steps=item["steps"]) for item in definitions}
        self.state.available_modules = set(self.modules)
        self.queue = deque()
        self.preview_queue = deque()  # Bounded read-only channel, separate from commands.
        self.capture_pending = False
        self.requests = {}
        self.capacity = capacity
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.owner = None
        self.closed = False
        self.active = None
        self.auto_simulation = False
        self.next_default = "QuestUpgrade"
        self.setup_done = set()
        self.logs = deque(maxlen=500)
        self.decision = "Await simulated rebirth confirmation"
        self.failure_callback = failure_callback
        self.manual_handler = manual_handler
        self.loop_settings_override = None
        self.manual_confirmation_only = False
        self.chain_test = None
        self.schedule_test = None
        self.trigger_observers = None
        self.allow_manual = allow_manual
        self.allow_simulation = allow_simulation
        self.mode = "Simulation"
        self.device_pending = False
        self.device_stop = threading.Event()
        self.runtime_handler = None
        self.capture_suppressed = threading.Event()
        self.module_pause = threading.Event()
        self.manual_result = None
        self.manual_history = deque(maxlen=200)
        from src.rwtd.android_core.canvas_scheduler import CanvasRuntime
        from src.rwtd.android_core.time_guard import TimeGuards
        self.time_guards = getattr(getattr(manual_handler,'__self__',None),'time_guards',None) or TimeGuards()
        self.canvas_runtime = CanvasRuntime()
        self.canvas_runtime.time_guards=self.time_guards
        self.canvas_cancel = threading.Event()
        from src.rwtd.android_core.canvas_triggers import TriggerTaskScheduler
        self.trigger_scheduler = TriggerTaskScheduler()
        self.trigger_scheduler.time_guards=self.time_guards
        self.trigger_cancel = threading.Event()
        self.trigger_work = None
        self.continuation_owner = None
        self.continuation_visits = {}
        self.continuation_queue = []
        self.continuation_path_active = False
        self.execution_graph = None
        self.node_test = None
        self._log("SIMULATED", "Simulation initialized; Android inputs and Discord disabled")

    def _owned(self):
        current = threading.get_ident()
        if self.owner is None:
            self.owner = current
        elif self.owner != current:
            raise RuntimeError("Only the OK-Script task owner may execute simulation commands")

    def _log(self, event, message, **details):
        level=details.pop('level','INFO')
        if level=='DEBUG' and not getattr(self,'detailed_logging',False):
            return
        with self.lock:
            self.logs.append({"sim_seconds": round(self.state.now, 2), "event": event,
                              "message": message, "level":level,"simulated": self.mode == "Simulation",
                              "timestamp_utc": datetime.now(timezone.utc).isoformat(), **details})

    def enqueue(self, request_id, command, payload=None):
        payload = copy.deepcopy(payload or {})
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100 or not isinstance(payload, dict):
            raise ValueError("Invalid request identity/payload")
        if command not in {"decision_start", "decision_control", "trigger_control", "dashboard_preferences", "canvas_select", "canvas_save_as", "canvas_rename", "decision_save", "decision_simulate", "module", "step", "observe", "confirm_rebirth", "auto_simulation", "calibration", "stop", "mode", "preview_settings", "dashboard_open", "dashboard_close", "pause_module", "device_test", "execute_module", "refresh_frame", "view_report", "save_preset", "manual_verify", "configure_workflow", "module_save", "module_import", "module_export", "module_duplicate", "module_delete", "scheduler_save", "storage_scan", "storage_preview", "storage_clean", "storage_settings", "storage_open"}:
            raise ValueError("Unknown simulation command")
        if command in {"stop", "dashboard_close"}:
            # A control latch only: HTTP workers never execute Android operations.
            self.device_stop.set()
        if command in {'stop','dashboard_close'} or (command == 'decision_control' and payload.get('operation') in {'stop','pause'}):
            self.canvas_cancel.set()
        if command in {'stop','dashboard_close'} or (command == 'decision_control' and payload.get('operation') == 'stop') or (command == 'trigger_control' and payload.get('operation') in {'stop','pause'}):
            self.trigger_cancel.set()
        if command == "pause_module":
            self.module_pause.set()
        if command in {"stop", "dashboard_close"} or (command == "mode" and payload.get("value") == "Simulation"):
            self.capture_suppressed.set()
            self.device_stop.set()
        with self.lock:
            if self.closed:
                raise ValueError("Task stopped")
            self._check_canvas_conflict(command)
            identity = (command, payload)
            if request_id in self.requests:
                record = self.requests[request_id]
                if record["identity"] != identity:
                    raise ValueError("Request ID reused with different command")
                return {"request_id": request_id, "status": record["status"], "duplicate": True}
            # Keep device-input idempotency receipts. Completed read/config UI
            # receipts can expire without stalling a continuously running Farm.
            input_commands={'device_test','execute_module','step','module','decision_start','confirm_rebirth','auto_simulation','mode'}
            disposable=[key for key,item in self.requests.items() if item['status'] not in {'QUEUED','RUNNING'} and item['identity'][0] not in input_commands]
            for key in disposable[:-500]:
                self.requests.pop(key,None)
            if command not in {"stop", "dashboard_close", "pause_module"} and (len(self.queue) >= self.capacity or len(self.requests) >= 2000):
                raise ValueError("Command/session capacity reached")
            if command in {"device_test", "execute_module", "refresh_frame"}:
                if command != 'refresh_frame' and any(item[1] == 'mode' for item in self.queue):
                    raise ValueError('Mode transition pending; wait before requesting input')
                if command == "refresh_frame":
                    if self.mode not in {"Android Preview", "Manual Device Test"} or self.device_stop.is_set() or self.capture_suppressed.is_set():
                        raise ValueError("Read-only Android preview is not explicitly active")
                elif self.device_stop.is_set():
                    raise ValueError("Android input stopped; explicitly reactivate manual device mode")
                elif self.mode != "Manual Device Test" or not self.allow_manual:
                    raise ValueError("Manual device mode is not explicitly active")
                if self.device_pending or self.active:
                    raise ValueError("An operation is pending; overlapping requests rejected")
                if command == "refresh_frame":
                    if self.capture_pending:
                        raise ValueError("A capture is pending; duplicate preview rejected")
                    self.capture_pending = True
                else:
                    self.device_pending = True
            self.requests[request_id] = {"identity": identity, "status": "QUEUED"}
            (self.preview_queue if command == "refresh_frame" else self.queue).append((request_id, command, payload))
            self.wake.set()
            return {"request_id": request_id, "status": "QUEUED", "duplicate": False}

    def process_next(self):
        self._owned()
        with self.lock:
            source = self.queue if self.queue else self.preview_queue
            device_command = bool(source and source[0][1] in {"device_test", "execute_module", "refresh_frame"})
            if device_command:
                request_id, operation, payload = source.popleft()
                if not self.queue and not self.preview_queue:
                    self.wake.clear()
                self.requests[request_id]["status"] = "RUNNING"
                self.decision = ("RUNNING read-only Android capture" if operation == "refresh_frame"
                                 else "RUNNING real Android single-step test" if operation == "device_test"
                                 else "RUNNING real Android module test")
                event = 'CAPTURE' if operation == 'refresh_frame' else 'STEP_START' if operation == 'device_test' else 'MODULE_START'
                self._log(event, self.decision, request_id=request_id)
        if not device_command:
            return self._process_control_command()
        # Release snapshot lock during the blocking operation so Stop and polling
        # remain responsive. There is still just one task-thread command consumer.
        try:
            self._check_canvas_conflict(operation)
            preview_allowed = operation == "refresh_frame" and self.mode in {"Android Preview", "Manual Device Test"} and not self.capture_suppressed.is_set()
            input_allowed = operation in {"device_test", "execute_module"} and self.mode == "Manual Device Test" and self.allow_manual
            if self.closed or self.device_stop.is_set() or not (preview_allowed or input_allowed) or not self.manual_handler:
                result = {"status": "BLOCKED", "failure_reason": "Android preview/input permission stopped or unavailable", "device_inputs": 0}
            else:
                if operation in {'device_test','execute_module'}:
                    # Explicit manual input changes the screen outside a Trigger's
                    # own queued action. Retire its unfinished observations; the
                    # internal Trigger dispatch path retains its NEXT cursor.
                    self._invalidate_trigger_rounds()
                if operation in {"device_test", "execute_module"} and self.manual_confirmation_only:
                    payload = dict(payload, dashboard_manual=True)
                result = self.manual_handler("execute" if operation == "device_test" else operation, payload, self.device_stop)
        except Exception as exc:
            self.device_stop.set()
            result = {"status": "FAILED", "failure_reason": type(exc).__name__,
                      "error_message": str(exc)[:500]}
        with self.lock:
            if operation == "refresh_frame":
                self.capture_pending = False
            else:
                self.device_pending = False
            if operation != "refresh_frame":
                self.manual_result = result
                if self.node_test and self.node_test.get('request_id') == request_id:
                    self.node_test['status'] = result['status']
            if result.get("report_id"):
                self.manual_history.append(result)
            self.requests[request_id]["status"] = result["status"]
            if operation == "refresh_frame":
                # Bound only disposable preview receipts; input idempotency records remain intact.
                finished = [key for key, item in self.requests.items() if item["identity"][0] == "refresh_frame" and item["status"] not in {"QUEUED", "RUNNING"}]
                for key in finished[:-10]:
                    self.requests.pop(key, None)
            self.decision = result["status"]
            self._log(result["status"], "Read-only Android capture result" if operation == "refresh_frame" else "Real manual test result", result=result, request_id=request_id)
        return True

    def _process_control_command(self):
        self._owned()
        with self.lock:
            if not self.queue or self.closed:
                return False
            request_id, command, payload = self.queue.popleft()
            if not self.queue and not self.preview_queue:
                self.wake.clear()
            record = self.requests[request_id]
            try:
                self._check_canvas_conflict(command)
                if not self.allow_simulation and command in {'module','step','observe','confirm_rebirth','auto_simulation','calibration','decision_simulate'}:
                    raise ValueError('Simulation is unavailable; use physical-device Chain or node tests')
                if self.mode != "Simulation" and command in {"module", "step", "observe", "confirm_rebirth", "auto_simulation", "calibration"}:
                    raise ValueError("Simulation controls are disabled in Android preview/manual mode")
                if command == "decision_start":
                    if set(payload) - {'graph','observations','source','notify_discord','scope','node_id','output_port','chain_id','explicit_activation','save_reports','test_context'} or not {'graph','observations'}.issubset(payload) or self.active or self.device_pending or self.auto_simulation:
                        raise ValueError("Stop other operations before starting Scheduler")
                    if 'output_port' in payload and (not payload.get('node_id') or payload.get('chain_id')):
                        raise ValueError('Output test requires a source node and no Chain test')
                    if 'output_port' in payload and (not isinstance(payload['output_port'],str) or not payload['output_port']):
                        raise ValueError('Output test requires a named output port')
                    services = self.manual_handler("catalog", {}, self.device_stop) if self.manual_handler else {"module_catalog": []}
                    module_run = services.get("module_run")
                    if isinstance(module_run, dict) and module_run.get("active"):
                        raise ValueError("Stop the manual module before starting Scheduler")
                    definitions = {m["id"]: m for m in services["module_catalog"]}
                    source = payload.get('source', 'simulation')
                    if source=='simulation' and not self.allow_simulation:
                        raise ValueError('Choose physical-device observation or action testing')
                    self.time_guards=getattr(getattr(self.manual_handler,'__self__',None),'time_guards',None) or self.time_guards
                    permission = payload.get('notify_discord', False)
                    if source in {'android_readonly','android_manual'}:
                        if self.mode not in {'Android Preview','Manual Device Test'} or self.device_stop.is_set() or not self.manual_handler:
                            raise ValueError('Activate read-only Android mode before starting OCR flow')
                        if source == 'android_manual' and (self.mode != 'Manual Device Test' or not self.allow_manual):
                            raise ValueError('Activate permitted Manual Device Test before starting action flow')
                        self.manual_handler('canvas_permissions', {'notify_discord':permission,
                            'save_reports':payload.get('save_reports',True)}, self.canvas_cancel)
                    from src.rwtd.android_core.canvas_scheduler import CanvasRuntime, validate_canvas, migrate_graph
                    from src.rwtd.android_core.canvas_triggers import main_graph, TriggerTaskScheduler
                    from src.rwtd.android_core.loop_settings_override import apply_ok_settings
                    execution_input = apply_ok_settings(migrate_graph(payload['graph']), self.loop_settings_override)
                    from src.rwtd.android_core.test_context import project as project_test_context,validate as validate_test_context
                    test_context=validate_test_context(payload.get('test_context',{}))
                    execution_input=project_test_context(execution_input,test_context,
                        chain_id=payload.get('chain_id'),node_id=payload.get('node_id'),
                        explicit=payload.get('explicit_activation'),scope=payload.get('scope'))
                    self.test_context=test_context
                    if test_context:self._log('TEST_CONTEXT','本次 Dashboard 測試預設（進度仍讀實機）',values=test_context)
                    from src.rwtd.android_core.retreatable_km import with_stage, stage_from_name, resolve_graph
                    selected = next((row for row in services.get('canvas_files',[]) if row.get('selected')), {})
                    execution_input = resolve_graph(with_stage(execution_input,stage_from_name(selected.get('name',''))))
                    testing = None
                    chain_testing = None
                    if payload.get('chain_id'):
                        if payload.get('node_id') or payload.get('explicit_activation') is not True or payload.get('scope') != 'main':
                            raise ValueError('Chain test requires explicit initiation and main-only scope')
                        from src.rwtd.android_core.canvas_chain_test import test_graph
                        graph, policy = test_graph(execution_input,payload['chain_id'],definitions)
                        chain_testing = {'id':payload['chain_id'],'policy':policy,'failures':0,'status':'RUNNING'}
                    elif payload.get('node_id'):
                        if payload.get('explicit_activation') is not True or payload.get('scope','main') != 'main':
                            raise ValueError('Selected node test requires explicit initiation and main scope')
                        from src.rwtd.android_core.canvas_node_test import test_graph
                        graph, selected = test_graph(migrate_graph(execution_input),payload['node_id'],definitions,source,
                                                     output_port=payload.get('output_port'))
                        testing = {'id':selected['id'],'kind':selected['kind'],'module':selected.get('module'),
                                   'module_revision':definitions.get(selected.get('module'),{}).get('revision'),
                                   'source':source,'status':'CHECKING','armed':True,
                                   'source_node':selected.get('source_node'),'output_port':selected.get('output_port')}
                    else:
                        graph = validate_canvas(migrate_graph(execution_input),definitions)
                    scope = payload.get('scope','main' if testing else 'all')
                    if scope not in {'all','main','triggers'}:
                        raise ValueError('Invalid Scheduler start scope')
                    continuous_test = source == 'android_manual' and scope != 'triggers'
                    if continuous_test and payload.get('explicit_activation') is not True:
                        raise ValueError('Continuous Canvas test requires explicit initiation')
                    main = main_graph(graph) if scope != 'triggers' else None
                    watchers = TriggerTaskScheduler(clock=self.trigger_scheduler.clock)
                    from src.rwtd.android_core.time_guard import TimeGuards
                    watchers.time_guards=self.time_guards if source!='simulation' else TimeGuards()
                    if scope != 'main':
                        watchers.start(graph,payload['observations'],source,permission,modules=definitions,
                                       queue_inputs=source == 'android_manual' and payload.get('explicit_activation') is True)
                    candidate = CanvasRuntime()
                    candidate.exhaustion_callback=getattr(self,'exhaustion_callback',None)
                    watchers.exhaustion_callback=getattr(self,'exhaustion_callback',None)
                    candidate.time_guards=watchers.time_guards
                    if main:
                        candidate.start(main,payload['observations'],definitions,source,permission,
                                        unattended=bool(chain_testing) or continuous_test)
                        if source != 'simulation':
                            candidate.chain_clock = time.monotonic
                            candidate.chain_timeout_started = time.monotonic()
                    schedule_test = None
                    if continuous_test and not testing and any(n.get('chain') for n in graph['nodes']):
                        from src.rwtd.android_core.farm_runtime import FarmRuntime
                        schedule_test = FarmRuntime(graph, definitions, self._schedule_effect,
                            lambda request: None, self._schedule_failure_notice,
                            (lambda:self.state.now) if source=='simulation' else time.monotonic,
                            rounds=1, source=source, notifications=permission,
                            observe=self._schedule_observe, external_watchers=True, keep_watchers=True, time_guards=watchers.time_guards,
                            exhaustion_callback=getattr(self,'exhaustion_callback',None),
                            invalidate_observers=self._invalidate_trigger_frames if self.trigger_observers else None)
                        if scope == 'main':
                            schedule_test.watchers.stop()
                        candidate, watchers = schedule_test.runtime, schedule_test.watchers
                        if self.trigger_observers:
                            watchers.clock = self.trigger_scheduler.clock
                    if not main and not watchers.active:
                        raise ValueError('No enabled workflow in the selected start scope')
                    self.canvas_runtime, self.trigger_scheduler = candidate, watchers
                    self.execution_graph = graph
                    self.node_test = testing
                    if testing:
                        from src.rwtd.android_core.canvas_triggers import reachable
                        candidate.refreshable_nodes = reachable(candidate.graph, candidate.graph['entry'])
                    self.chain_test = chain_testing
                    self.schedule_test = schedule_test
                    self.continuation_owner = schedule_test
                    self.continuation_visits.clear()
                    self.continuation_queue.clear()
                    self.continuation_path_active=False
                    service = getattr(self.manual_handler,'__self__',None)
                    if (chain_testing or continuous_test) and hasattr(service,'reset_chain_test'):
                        service.reset_chain_test()
                    self.canvas_cancel.clear()
                    self.trigger_cancel.clear()
                    self._sync_canvas_lock()
                    record["status"] = "SUCCESS"
                elif command == "decision_control":
                    if set(payload) - {"operation", "values"}:
                        raise ValueError("Unexpected Scheduler control fields")
                    if payload.get('operation') == 'verify' and self.canvas_runtime.waiting_kind == 'action_verify' and self.canvas_runtime.source == 'android_manual':
                        result = self.canvas_runtime.action_result
                        if not result or not result.get('report_id') or not self.manual_handler:
                            raise ValueError('Action evidence missing')
                        verified = self.manual_handler('manual_verify',{'report_id':result['report_id'], **(payload.get('values') or {})},self.canvas_cancel)
                        self.canvas_runtime.action_result = verified
                    if payload.get('operation') == 'observe' and self.trigger_scheduler.active:
                        if self.trigger_scheduler.source != 'simulation':
                            raise ValueError('Real TriggerTasks cannot accept simulated observations')
                        self.trigger_scheduler.observe(payload.get('values') or {})
                        if self.canvas_runtime.locked:
                            self.canvas_runtime.control('observe',payload.get('values'))
                    else:
                        self.canvas_runtime.control(payload.get("operation"), payload.get("values"))
                    if payload.get('operation') == 'stop':
                        self.trigger_scheduler.stop()
                        if self.schedule_test:
                            self.schedule_test.stop()
                    if payload.get('operation') == 'resume':
                        self.canvas_cancel.clear()
                    self._sync_canvas_lock()
                    record["status"] = "SUCCESS"
                elif command == 'trigger_control':
                    if set(payload) - {'task_id','operation','pass','node','observations'}:
                        raise ValueError('Unknown TriggerTask control fields')
                    operation = payload.get('operation')
                    if operation == 'remove_request':
                        self.trigger_scheduler.remove_request(payload.get('task_id'),payload.get('node'))
                    elif operation == 'observe':
                        if self.trigger_scheduler.source != 'simulation' or not isinstance(payload.get('observations'),dict):
                            raise ValueError('Only Simulation observations can be injected')
                        self.trigger_scheduler.observe(payload['observations'])
                    else:
                        self.trigger_scheduler.control(payload.get('task_id'),operation,payload.get('pass'))
                    self.trigger_cancel.clear()
                    self._sync_canvas_lock()
                    record['status'] = 'SUCCESS'
                elif command == "module":
                    if set(payload) - {"name", "verified_success"}:
                        raise ValueError("Unexpected module fields")
                    if type(payload.get("verified_success", True)) is not bool:
                        raise ValueError("verified_success must be a simulation boolean")
                    started = self.start_module(payload.get("name"), request_id, payload.get("verified_success", True))
                    record["status"] = "RUNNING" if started else "BLOCKED"
                elif command == "step":
                    self.schedule()
                    record["status"] = "SUCCESS"
                elif command == "observe":
                    self.observe(payload)
                    record["status"] = "SUCCESS"
                elif command == "confirm_rebirth":
                    if self.active:
                        raise ValueError("Active operation cannot be interrupted")
                    self.state = SimState(now=self.state.now, generation=self.state.generation + 1,
                                          phase="INITIAL_SETUP", current_progress_km=0, progress_verified=True,
                                          last_successful_teleport_progress_km=0, available_modules=set(self.modules))
                    self.setup_done.clear()
                    self.next_default = "QuestUpgrade"
                    self._log("SUCCESS", "SIMULATED confirmed rebirth; teleport anchor reset to 0")
                    record["status"] = "SUCCESS"
                elif command == "auto_simulation":
                    if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                        raise ValueError("Use enabled boolean; real Auto Farming is unavailable")
                    self.auto_simulation = payload["enabled"]
                    record["status"] = "SUCCESS"
                elif command == "calibration":
                    # Deliberately record user proposals, never infer a usable gesture.
                    if self.active:
                        raise ValueError("Active operation cannot be interrupted")
                    if set(payload) - {"note", "vertical_delta_px"}:
                        raise ValueError("Only a note and proposed vertical delta are accepted")
                    delta = payload.get("vertical_delta_px")
                    if delta is not None and (type(delta) not in (int, float) or not math.isfinite(delta)):
                        raise ValueError("Invalid proposed distance")
                    self._log("SIMULATED", "Calibration proposal saved; UNVERIFIED, no gesture executed", proposal=payload)
                    record["status"] = "SUCCESS"
                elif command == "stop":
                    self.canvas_runtime.control("stop")
                    if self.schedule_test:
                        self.schedule_test.stop()
                    self.trigger_scheduler.stop()
                    self._sync_canvas_lock()
                    self.auto_simulation = False
                    if self.runtime_handler:
                        self.runtime_handler("stop", payload)
                    if self.manual_handler:
                        self.manual_handler("abort_module", {}, self.device_stop)
                    self._log("SIMULATED", "Scheduling paused; active operation may finish")
                    record["status"] = "SUCCESS"
                elif command == "mode":
                    value = payload.get("value")
                    if self.active or self.device_pending:
                        raise ValueError("Cannot change mode during an active operation")
                    if value not in {"Simulation", "Android Preview", "Manual Device Test"}:
                        raise ValueError("Auto Farming is disabled")
                    if value == "Android Preview" and payload.get("explicit_activation") is not True:
                        raise ValueError("Explicit activation required for read-only Android Preview")
                    if value == "Manual Device Test" and (not self.allow_manual or payload.get("explicit_activation") is not True):
                        raise ValueError("Enable manual permission in task config and explicitly activate this session")
                    self.mode, self.auto_simulation = value, False
                    if value == 'Simulation':
                        self.trigger_scheduler.stop()
                        self._sync_canvas_lock()
                    self.device_stop.clear()
                    if value == "Manual Device Test":
                        # Explicit reactivation clears an idle/user pause latch;
                        # it does not dispatch or resume a module by itself.
                        self.module_pause.clear()
                    if value in {"Android Preview", "Manual Device Test"}:
                        self.capture_suppressed.clear()
                    if self.runtime_handler:
                        self.runtime_handler("mode", payload)
                    self._log("READY", value + " activated explicitly")
                    record["status"] = "SUCCESS"
                elif command in {"preview_settings", "dashboard_open", "dashboard_close", "pause_module"}:
                    if not self.runtime_handler:
                        raise ValueError("Dashboard runtime unavailable")
                    record["status"] = self.runtime_handler(command, payload)["status"]
                elif command in {"dashboard_preferences", "canvas_select", "canvas_save_as", "canvas_rename", "decision_save", "decision_simulate", "save_preset", "manual_verify", "view_report", "configure_workflow", "module_save", "module_import", "module_export", "module_duplicate", "module_delete", "scheduler_save", "storage_scan", "storage_preview", "storage_clean", "storage_settings", "storage_open"}:
                    if not self.manual_handler:
                        raise ValueError("Manual preset service unavailable")
                    if self.active and (command.startswith("module_") or command == "scheduler_save"):
                        raise ValueError("Edit definitions only while simulation is idle")
                    result = self.manual_handler(command, payload, self.device_stop)
                    if command in {'canvas_select','canvas_save_as','canvas_rename'}:
                        self.time_guards=getattr(getattr(self.manual_handler,'__self__',None),'time_guards',None) or self.time_guards
                        if command in {'canvas_select','canvas_save_as'}:
                            from src.rwtd.android_core.canvas_scheduler import CanvasRuntime
                            from src.rwtd.android_core.canvas_triggers import TriggerTaskScheduler
                            self.canvas_runtime=CanvasRuntime()
                            self.trigger_scheduler=TriggerTaskScheduler(clock=self.trigger_scheduler.clock)
                            self.canvas_runtime.exhaustion_callback=getattr(self,'exhaustion_callback',None)
                            self.trigger_scheduler.exhaustion_callback=getattr(self,'exhaustion_callback',None)
                            self.execution_graph=None
                            self.chain_test=self.schedule_test=self.node_test=None
                            self.trigger_work=None
                        self.canvas_runtime.time_guards=self.time_guards
                        self.trigger_scheduler.time_guards=self.time_guards
                        if command in {'canvas_select','canvas_save_as'}:
                            if self.trigger_observers:self.trigger_observers.sync()
                            self._sync_canvas_lock()
                    if command.startswith("module_") or command == "scheduler_save":
                        services = self.manual_handler("catalog", {}, self.device_stop)
                        self.roles = services["scheduler_roles"]
                        self.modules = {item["id"]: GameplayModule(item["id"], role=next((role for role, target in self.roles.items() if target == item["id"]), item.get("role")),
                                                                duration=item.get("simulation_seconds", 1), steps=item["steps"]) for item in services["module_catalog"]}
                        self.state.available_modules = set(self.modules)
                    if command in {"manual_verify", "view_report"}:
                        self.manual_result = result
                    record["status"] = result["status"]
                    self._log(result["status"], "Preset result", result=result)
            except (ValueError, TypeError, OSError) as exc:
                record["status"] = "FAILED"
                self._log("FAILED", str(exc), request_id=request_id)
            return True

    def observe(self, payload):
        self._owned()
        allowed = {"current_progress_km", "progress_verified", "food_count", "food_verified", "store_reward_in_roi",
                   "store_reward_verified", "exception_confirmed", "stage_complete_confirmed", "available_modules"}
        if set(payload) - allowed:
            raise ValueError("Unrecognized observation fields")
        for key in allowed - {"current_progress_km", "food_count", "available_modules"}:
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        if "available_modules" in payload and (not isinstance(payload["available_modules"], list)
                or any(name not in self.modules for name in payload["available_modules"])):
            raise ValueError("Unknown available module")
        for key, value in payload.items():
            setattr(self.state, key, set(value) if key == "available_modules" else value)
        if not valid_number(self.state.current_progress_km):
            self.state.current_progress_km, self.state.progress_verified = None, False
            self._log("RECOGNITION_FAILURE", "Invalid simulated progress rejected")
        if type(self.state.food_count) is not int or self.state.food_count < 0:
            self.state.food_count, self.state.food_verified = None, False
        self._log("SIMULATED", "Simulation observations refreshed; these are not Android OCR values")

    def start_module(self, name, request_id=None, verified_success=True):
        self._owned()
        if name not in self.modules:
            raise ValueError("Unknown module")
        if self.active:
            self._log("BLOCKED", "Single queue: another operation is active", module=name)
            return False
        module = self.modules[name]
        context = SimContext(self.state, verified_success, self.state.generation)
        operation = module.execute(context)
        if operation is None:
            self._log(module.status, module.failure_reason, module=name)
            return False
        self.active = {"name": name, "context": context, "due": self.state.now + module.duration,
                       "request_id": request_id, "operation": operation}
        if module.role == "Teleport":
            self.state.teleport_pending = True
        self.decision = f"RUNNING {name} (SIMULATED)"
        self._log("MODULE_START", "Simulated operation started", module=name, operation=operation)
        return True

    def advance(self, seconds):
        self._owned()
        if not valid_number(seconds):
            raise ValueError("Invalid clock advance")
        if self.schedule_test and not self.canvas_cancel.is_set() and not self.device_stop.is_set():
            if self.schedule_test.check_chain_timeout():
                from src.rwtd.android_core.farm_runtime import failure_log_message
                self._log('CHAIN_TIMEOUT',failure_log_message(self.schedule_test.schedule.events[-1]))
        if self._trigger_boundary():
            # The next main node waits until this complete Trigger node returns.
            return
        self._apply_trigger_next()
        if self.schedule_test:
            run = self.schedule_test
            old_runtime = run.runtime
            if self.canvas_cancel.is_set() or self.device_stop.is_set():
                run.stop()
            run.tick(seconds)
            self.canvas_runtime, self.trigger_scheduler = run.runtime, run.watchers
            if self.trigger_observers and run.runtime is not old_runtime:
                self._invalidate_trigger_frames()
            self._advance_triggers()
            self._advance_trigger_queue()
            with self.lock:
                self._sync_canvas_lock()
                self.state.now += seconds
            return
        if self.chain_test and self.canvas_runtime.status=='RUNNING':
            runtime=self.canvas_runtime
            if self.canvas_cancel.is_set() or runtime.source!='simulation' and self.device_stop.is_set():
                runtime.control('stop')
            rule=self.chain_test['policy'].get('continue_condition')
            if rule and runtime.cursor==runtime.graph['entry'] and runtime.entry_pending and runtime.now>=runtime.due and runtime.status=='RUNNING':
                from src.rwtd.android_core.canvas_scheduler import condition
                values=runtime.observations
                if runtime.source!='simulation':
                    try:
                        values=self.manual_handler('chain_observe',{},self.canvas_cancel) if self.manual_handler else {}
                    except Exception:
                        values={}
                if self.canvas_cancel.is_set() or runtime.source!='simulation' and self.device_stop.is_set():
                    runtime.control('stop')
                else:
                    allowed=condition(rule,values or {})
                    if allowed is False:
                        runtime.status,runtime.outcome,runtime.reason='STOPPED','SKIPPED','Chain 繼續條件不符合；未到結束節點，測試跳過'
                    elif allowed is None:
                        runtime.status,runtime.reason='BLOCKED','Chain 繼續條件 UNKNOWN'
        with self.lock:
            old_chain_runs = self.canvas_runtime.chain_completed_runs
            if self.canvas_runtime.unattended and self.canvas_runtime.source=='android_manual' and self.canvas_runtime.status=='WAITING_MANUAL' and self.canvas_runtime.waiting_kind=='action_execute':
                self.canvas_runtime.control('execute_once',{'explicit_activation':True})
            if self.node_test and self.node_test.get('armed') and self.canvas_runtime.status=='WAITING_MANUAL' and self.canvas_runtime.waiting_kind=='action_execute' and self.canvas_runtime.cursor==self.node_test['id']:
                self.canvas_runtime.control('execute_once',{'explicit_activation':True})
                self.node_test.update(armed=False,status='RUNNING')
            self.canvas_runtime.tick(seconds)
            effect = copy.deepcopy(self.canvas_runtime.pending_effect)
            context = {'source':self.canvas_runtime.source, 'notify_discord':self.canvas_runtime.notify_discord,
                       'ocr_attempt':self.canvas_runtime.ocr_attempts.get(effect['id'],0)+1 if effect else 1,
                       'revision':self.canvas_runtime.graph.get('revision') if self.canvas_runtime.graph else None,
                       'variables':copy.deepcopy(self.canvas_runtime.variables)}
        if effect:
            try:
                if effect['kind'] == 'action':
                    self._log('STEP_START','Canvas 指定步驟執行一次',node=effect['id'],module=effect['module'],step_id=effect['step_id'])
                if effect['kind'] in {'action','module'} and context['source'] == 'simulation':
                    result = {'status':'SIMULATED','action_completed':True,'device_inputs':0}
                    if effect['kind'] == 'action':
                        result['binding'] = copy.deepcopy(self.canvas_runtime.action_plan['binding'])
                else:
                    if effect['kind'] == 'action':
                        self._prepare_trigger_input()
                    result = self.manual_handler('canvas_effect', {'node':effect, **context}, self.canvas_cancel) if self.manual_handler else None
            except Exception as exc:
                result = {'status':'FAILED', 'error_type':type(exc).__name__}
            with self.lock:
                if effect['kind'] in {'action','module'} and context['source'] == 'android_manual' and result:
                    self.manual_result = result
                    for report in result.get('records',[result]):
                        if report.get('report_id'):
                            self.manual_history.append(report)
                    self._log(result.get('status','FAILED'),'Chain 測試操作結果；輸入送出不等於遊戲結果成功' if self.chain_test else 'Canvas 連續測試操作結果；未宣稱遊戲結果已驗證' if self.canvas_runtime.unattended else 'Canvas 單步结果；仍需人工驗證',report_id=result.get('report_id'))
                if self.chain_test and result:
                    last=(result.get('records') or [result])[-1]
                    if (last.get('input_attempted') and not last.get('action_completed')
                            and not result.get('preempted')):
                        self.chain_test['delivery_uncertain']=True
                if self.canvas_cancel.is_set():
                    self.canvas_runtime.pending_effect = None
                    if effect['kind'] == 'action' and context['source'] == 'android_manual' and result:
                        self.canvas_runtime.action_result = copy.deepcopy(result)
                        count = result.get('device_inputs',0)
                        previous = self.canvas_runtime.device_inputs
                        self.canvas_runtime.device_inputs = None if count is None or previous is None else previous+count
                else:
                    self.canvas_runtime.complete_effect(effect, result)
        if self.trigger_observers and self.canvas_runtime.chain_completed_runs != old_chain_runs:
            self._invalidate_trigger_frames()
        self._complete_transferred_key()
        self._finish_chain_test(seconds)
        self._finish_node_test()
        self._advance_triggers()
        self._advance_trigger_queue()
        with self.lock:
            self._sync_canvas_lock()
            self.state.now += seconds
            if self.active and self.state.now >= self.active["due"]:
                active, self.active = self.active, None
                name, context = active["name"], active["context"]
                module = self.modules[name]
                role = module.role
                success = module.verify_success(context)
                if role == "Teleport":
                    distance = self.state.distance()
                    success = success and distance is not None and distance >= 500
                    self.state.teleport_pending = False
                    if success:
                        self.state.last_successful_teleport_progress_km = self.state.current_progress_km
                module.status = "SUCCESS" if success else "FAILED"
                module.failure_reason = None if success else "Simulated success verification failed"
                module.next_allowed = self.state.now + module.cooldown
                if success:
                    if role == "FoodUpgrade":
                        self.state.food_count = 0
                    elif role == "StoreReward":
                        self.state.store_reward_in_roi = False
                    elif self.state.phase == "INITIAL_SETUP" and role in {"ArmySummon", "BuffUpgrade"}:
                        self.setup_done.add(role)
                    elif role == "Settlement":
                        self.state.phase = "RESTART"
                    if role in {"QuestUpgrade", "ArmyUpgrade"}:
                        self.next_default = "ArmyUpgrade" if role == "QuestUpgrade" else "QuestUpgrade"
                if active["request_id"]:
                    self.requests[active["request_id"]]["status"] = module.status
                self._log("MODULE_SUCCESS" if success else "MODULE_FAILURE", "SIMULATED result", module=name)
                if not success and self.failure_callback:
                    try:
                        self.failure_callback(name, module.failure_reason)
                    except Exception as exc:
                        self.auto_simulation = False
                        self._log("FAILED", "Existing exception evidence service failed: " + type(exc).__name__)
                self.decision = "Operation complete; re-evaluate state"
            if self.auto_simulation and self.mode == "Simulation" and not self.active and not self.canvas_runtime.locked:
                self.schedule()

    def schedule(self):
        self._owned()
        if self.mode != "Simulation":
            raise ValueError("Real Auto Farming is disabled")
        state = self.state
        if self.active:
            return
        if state.exception_confirmed:
            self.auto_simulation = False
            self.decision = "BLOCKED: confirmed exception; no autonomous recovery"
            self._log("BLOCKED", self.decision)
            return
        if state.phase == "FARMING" and state.stage_complete_confirmed:
            state.phase = "SETTLEMENT"
            self._log("STATE", "Confirmed simulated completion -> SETTLEMENT")
        if state.phase in {"REBORN", "RESTART"}:
            self.decision = "Await explicitly confirmed simulated rebirth"
            return
        if state.phase == "INITIAL_SETUP":
            candidates = [name for name in ("ArmySummon", "BuffUpgrade") if name not in self.setup_done]
            if not candidates:
                state.phase = "FARMING"
                candidates = []
            else:
                return self._choose(candidates, "INITIAL_SETUP")
        if state.phase == "SETTLEMENT":
            return self._choose(["Settlement"], "Confirmed stage completion")
        candidates = []
        if state.distance() is not None and state.distance() >= 500 and not state.teleport_pending:
            candidates.append("Teleport")
        if state.food_verified and type(state.food_count) is int and state.food_count >= 50:
            candidates.append("FoodUpgrade")
        if state.store_reward_verified and state.store_reward_in_roi:
            candidates.append("StoreReward")
        candidates += [self.next_default, "ArmyUpgrade" if self.next_default == "QuestUpgrade" else "QuestUpgrade"]
        self._choose(candidates, "Priority: teleport -> food -> Store -> Quest/Army")

    def _choose(self, candidates, reason):
        for role in candidates:
            name = self.roles.get(role)
            if name not in self.modules:
                self._log("BLOCKED", "No module assigned to scheduler role", role=role)
                continue
            ready, blocked = self.modules[name].can_run(self.state)
            if ready:
                self._log("TRIGGER", reason, module=name)
                return self.start_module(name)
            self.modules[name].status = "COOLDOWN" if blocked == "COOLDOWN" else "BLOCKED"
            self.modules[name].failure_reason = blocked
            self._log("BLOCKED", blocked, module=name)
        self.decision = "No eligible module; wait for updated observations/cooldown"

    def snapshot(self):
        with self.lock:
            state = asdict(self.state)
            state["available_modules"] = sorted(self.state.available_modules)
            state["teleport_distance_km"] = self.state.distance()
            modules = []
            for name, module in self.modules.items():
                ready, reason = module.can_run(self.state)
                status = module.status
                if status != "RUNNING" and not ready:
                    status = "COOLDOWN" if reason == "COOLDOWN" else "BLOCKED"
                elif status in {"BLOCKED", "COOLDOWN"} and ready:
                    status = "READY"
                modules.append({"name": name, "status": status, "can_run": ready and self.active is None,
                                "failure_reason": module.failure_reason, "precondition": reason,
                                "cooldown": module.cooldown, "cooldown_remaining": max(0, module.next_allowed - self.state.now)})
            scheduler = self.canvas_runtime.snapshot()
            scheduled = self.schedule_test is not None and self.schedule_test.status == 'RUNNING'
            scheduler['input_blocked'] = self.canvas_runtime.locked or self.trigger_work is not None or scheduled
            scheduler['main_status'] = scheduler['status']
            scheduler['locked'] = self.canvas_runtime.locked or self.trigger_scheduler.active or scheduled
            if scheduled and self.schedule_test.handoff_pending:
                scheduler['status'], scheduler['reason'] = 'RUNNING', 'Chain 已完成；處理 Trigger 優先工作後交接'
            if not self.canvas_runtime.locked and not scheduled and self.trigger_scheduler.active:
                states = [t['status'] for t in self.trigger_scheduler.tasks.values()]
                scheduler['status'] = 'RUNNING' if 'RUNNING' in states else 'WAITING_MANUAL' if 'WAITING_MANUAL' in states else 'PAUSED'
                scheduler['reason'] = 'TriggerTask 監看獨立執行；操作加入共用佇列'
                scheduler['waiting_kind'] = 'trigger' if scheduler['status'] == 'WAITING_MANUAL' else None
                scheduler['source'] = self.trigger_scheduler.source
                scheduler['simulated'] = self.trigger_scheduler.source == 'simulation'
            from src.rwtd.android_core.test_context import FIELDS as test_context_fields
            return {"test_context_fields":copy.deepcopy(test_context_fields),
                    "test_context":copy.deepcopy(getattr(self,'test_context',{})),"scheduler_runtime": scheduler,
                    "chain_test":copy.deepcopy(self.chain_test),
                    "schedule_test":self.schedule_test.snapshot() if self.schedule_test else None,
                    "node_test":copy.deepcopy(self.node_test),
                    "trigger_runtime": self.trigger_scheduler.snapshot(),
                    "scheduler_execution_graph": copy.deepcopy(self.execution_graph or self.canvas_runtime.graph) if scheduler['locked'] else None,
                    "mode": self.mode, "simulated": self.mode == "Simulation", "device_inputs": "See manual evidence" if self.mode == "Manual Device Test" else 0, "discord_enabled": False,
                    "manual_test_enabled": self.mode == "Manual Device Test" and self.allow_manual and not self.closed and not self.device_stop.is_set(),
                    "device_input_stopped": self.device_stop.is_set(),
                    "preview_permission": True,
                    "preview_enabled": self.mode in {"Android Preview", "Manual Device Test"} and not self.closed and not self.device_stop.is_set() and not self.capture_suppressed.is_set(),
                    "auto_farming_enabled": False,
                    "auto_simulation": self.auto_simulation, "state": state, "modules": modules,
                    "active": {key: value for key, value in self.active.items() if key != "context"} if self.active else None,
                    "decision": self.decision, "logs": list(self.logs), "queued": len(self.queue),
                    "manual_result": self.manual_result, "manual_history": list(self.manual_history), "device_pending": self.device_pending,
                    "capture_pending": self.capture_pending, "preview_queued": len(self.preview_queue),
                    "requests": {key: item["status"] for key, item in self.requests.items()}}

    def close(self):
        self._owned()
        with self.lock:
            self.closed, self.auto_simulation = True, False
            if self.trigger_observers:
                self.trigger_observers.close()
            if self.schedule_test:
                self.schedule_test.stop()
            self.canvas_runtime.control("stop")
            self.trigger_scheduler.stop()
            self.trigger_cancel.set()
            self._sync_canvas_lock()
            self.device_stop.set()
            for record in self.requests.values():
                if record["status"] in {"QUEUED", "RUNNING"}:
                    record["status"] = "CANCELLED_SIMULATION_TASK_STOPPED"
            self.queue.clear()
            self.preview_queue.clear()
            self.capture_pending = self.device_pending = False
            self.active = None
            self.state.teleport_pending = False
            self.wake.set()

    def _check_canvas_conflict(self, command):
        # Both enqueue and task-thread execution validate current backend state.
        # Read-only refresh and ordinary manual verification never start/lock Canvas.
        scheduled = self.schedule_test is not None and self.schedule_test.status == 'RUNNING'
        if (self.canvas_runtime.locked or scheduled) and command in {"canvas_select", "canvas_save_as", "canvas_rename", "decision_save", "decision_simulate", "decision_start", "device_test", "execute_module", "module", "step", "auto_simulation", "mode", "confirm_rebirth"}:
            raise ValueError("Scheduler active: Stop before editing flow or running another module test")
        if self.trigger_scheduler.active and command in {'canvas_select','canvas_save_as','canvas_rename','decision_save','decision_simulate','decision_start'}:
            raise ValueError('Stop TriggerTasks before editing or replacing the Canvas')
        if self.trigger_work and command in {'device_test','execute_module','mode'}:
            raise ValueError('TriggerTask operation is executing; wait for the input queue')

    def _sync_canvas_lock(self):
        if self.trigger_observers:
            self.trigger_observers.sync()
        service = getattr(self.manual_handler, "__self__", None)
        if hasattr(service, "decision_store"):
            scheduled = self.schedule_test is not None and self.schedule_test.status == 'RUNNING'
            service.decision_store.execution_locked = self.canvas_runtime.locked or self.trigger_scheduler.active or scheduled
            service.decision_store.input_locked = self.canvas_runtime.locked or self.trigger_work is not None or scheduled

    def enable_trigger_observers(self, observe, cleanup=None):
        self._owned()
        from src.rwtd.android_core.trigger_observers import TriggerObservers
        with self.lock:
            if self.trigger_observers:
                raise ValueError('Trigger observer workers already configured')
            self.trigger_observers = TriggerObservers(self,observe,cleanup)
            self.trigger_observers.sync()

    def _trigger_boundary(self):
        """Only the owner calls this, between complete Canvas nodes."""
        from src.rwtd.android_core.log_policy import timing_start,timing_mark
        task=getattr(getattr(self.manual_handler,'__self__',None),'task',None)
        started=timing_start(task)
        timing_mark(task,'TRIGGER_BOUNDARY_START')
        try:
            self.trigger_scheduler.parent_key=self.canvas_runtime.flow_key
            if self.trigger_observers:
                self._advance_triggers()  # Notifications stay on the owner too.
                timing_mark(task,'TRIGGER_NOTIFICATIONS_DONE',started)
                if self.trigger_scheduler.urgent_handoff_pending():
                    # Farm owns its route in FarmRuntime.tick; a standalone
                    # Canvas can resolve it here. Neither yields to ordinary
                    # queued input first.
                    if self.continuation_owner is not None:
                        return False
                    if self._apply_trigger_next():
                        return True
                executed=self._advance_trigger_queue()
                self._apply_trigger_next()
                return executed
            return False
        finally:
            timing_mark(task,'TRIGGER_BOUNDARY_DONE',started)

    def _apply_trigger_next(self):
        """Resolve graph NEXT on the owner; Farm consumes its own requests."""
        with self.lock:
            return self._resolve_trigger_next()

    def _resolve_trigger_next(self):
        owner=self.continuation_owner
        if owner is not None and owner.status=='RUNNING':
            return False
        if (self.closed or self.device_stop.is_set() or self.trigger_cancel.is_set() or self.canvas_cancel.is_set()
                or self.device_pending or self.capture_pending or self.trigger_work
                or self.canvas_runtime.pending_effect):
            return False
        links=self.trigger_scheduler.take_continuations()
        urgent=next((link for link in links if link.get('preempt_operations')),None)
        if urgent:
            self.continuation_queue=self.trigger_scheduler.cancel_waiting_routes(
                urgent['trigger'],self.continuation_queue)
        for link in links:
            if (not link.get('chain') and self.canvas_runtime.locked and self.canvas_runtime.graph
                    and self.canvas_runtime.graph['entry']==link['target']):
                self._log('NEXT','目標入口已在執行；沿用同一執行端',**link)
                if not self.continuation_queue:
                    self.continuation_path_active=True
                continue
            if not any(p['target']==link['target'] and p.get('chain')==link.get('chain')
                       for p in self.continuation_queue):
                self.continuation_queue.append(link)
        self.continuation_queue.sort(key=lambda link:not link.get('preempt_operations'))
        if self.continuation_path_active and self.canvas_runtime.locked:
            return False
        if self.canvas_runtime.status=='STOPPED':
            self.continuation_path_active=False
        if not self.continuation_queue:
            return False
        link=self.continuation_queue.pop(0)
        target=link.get('chain',link['target'])
        if (not link.get('chain') and self.canvas_runtime.locked and self.canvas_runtime.graph
                and self.canvas_runtime.graph['entry']==target):
            self._log('NEXT','目標入口已在執行；沿用同一執行端',**link)
            return False
        from src.rwtd.android_core.canvas_chains import continuation_definition
        from src.rwtd.android_core.canvas_scheduler import CanvasRuntime
        try:
            source_chain=(self.canvas_runtime.graph or {}).get('entry')
            cross_chain=bool(link.get('chain') and source_chain != target)
            item=continuation_definition(self.trigger_scheduler.graph,target)
            graph=item['graph']
            if self.canvas_runtime.status=='STOPPED':
                self.continuation_visits.clear()
            elif self.canvas_runtime.graph:
                self.continuation_visits.setdefault(self.canvas_runtime.graph['entry'],1)
            maximum=graph['settings'].get('max_cycles',10)
            if maximum is not None and self.continuation_visits.get(target,0)>=maximum:
                self.canvas_runtime.status,self.canvas_runtime.outcome='STOPPED','FAILED'
                self.canvas_runtime.reason='跨入口 NEXT 循環次數上限已達；尚未到結束節點'
                self._log('FAILED',self.canvas_runtime.reason,**link)
                return False
            # Standalone owns start repeats; Farm owns them outside its runtime.
            if item.get('run_limit',1)>1:
                next(n for n in graph['nodes'] if n['id']==target)['repeat_runs']=item['run_limit']
            candidate=CanvasRuntime()
            candidate.time_guards=self.trigger_scheduler.time_guards
            candidate.exhaustion_callback=getattr(self,'exhaustion_callback',None)
            candidate.start(graph,{},self.trigger_scheduler.modules,self.trigger_scheduler.source,
                            self.trigger_scheduler.notify_discord,
                            unattended=self.trigger_scheduler.source=='android_manual')
            if link.get('chain'):
                if not any(n['id']==link['target'] and n['kind'] not in {'start','guard'}
                           for n in graph['nodes']):
                    raise ValueError('Trigger NEXT target is not in the selected Chain')
                candidate.cursor=candidate.current_node=link['target']
                candidate.reason='Trigger NEXT 接手節點 '+link['target']
            candidate.exclusion_regions=copy.deepcopy(self.canvas_runtime.exclusion_regions)
            if link.get('flow_key'):
                candidate.flow_key=copy.deepcopy(link['flow_key'])
            self.canvas_runtime.control('stop')
            self.canvas_runtime=candidate
            self.continuation_path_active=True
            self.continuation_visits[target]=self.continuation_visits.get(target,0)+1
            self.schedule_test=self.chain_test=self.node_test=None
            self.continuation_owner=None
            self._log('TRIGGER_CROSS_CHAIN' if cross_chain else 'TRIGGER_ROUTE' if link.get('chain') else 'NEXT',
                      'Trigger 達成跨鍊交接；來源未完成，不計成功或失敗' if cross_chain
                      else 'Trigger 接手流程；來源未完成，不計成功或失敗' if link.get('chain')
                      else '依出口連線接續入口；等待目標 Guard 判斷',**link)
            return True
        except (ValueError,KeyError,StopIteration) as exc:
            self._log('FAILED','NEXT 入口無法啟動：'+str(exc),**link)
            return False

    def _complete_transferred_key(self):
        runtime=self.canvas_runtime
        key=runtime.flow_key
        if runtime.status!='STOPPED' or not key or key.get('purpose') not in {'trigger','solver'}:
            return
        if runtime.outcome!='SUCCESS':
            self.trigger_scheduler.abandon_continuation(key,'流程未到 END；不產生完成結果')
            return
        with self.trigger_scheduler.lock:
            owned=any(t.get('route_active') and (t.get('flow_key') or {}).get('id')==key['id']
                      for t in self.trigger_scheduler.tasks.values())
        if not owned:return
        frame=None
        if key['purpose']=='solver' and runtime.source!='simulation':
            try:
                frame=self.manual_handler('canvas_effect',{
                    'node':{'id':runtime.current_node,'kind':'flow_frame','phase':'end'},
                    'source':runtime.source,'recovery_event':key.get('event')},self.canvas_cancel)
            except Exception:
                frame={'valid':False}
        completion=self.trigger_scheduler.complete_continuation(key,runtime.current_node,frame)
        if completion:runtime.completion=completion

    def _schedule_effect(self, payload):
        run = self.schedule_test
        self.canvas_runtime, self.trigger_scheduler = run.runtime, run.watchers
        if payload['node']['kind'] in {'action','module'}:
            self._prepare_trigger_input()
        result = self.manual_handler('canvas_effect', payload, self.canvas_cancel)
        if self.canvas_cancel.is_set() or self.device_stop.is_set():
            run.stop()
            return result
        if payload['node']['kind'] in {'action','module'} and result:
            self.manual_result = result
            self.manual_history.extend(r for r in result.get('records',[result]) if r.get('report_id'))
            self._log(result.get('status','FAILED'), 'Canvas 整組 Chain 測試操作結果', node=payload['node']['id'])
        return result

    def _schedule_observe(self, rule):
        return self.manual_handler('chain_observe', {'rule':rule}, self.canvas_cancel)

    def _schedule_failure_notice(self, event):
        callback=getattr(self,'exhaustion_callback',None)
        if callable(callback):
            try:
                callback(dict(event,kind='chain',source=self.canvas_runtime.source,
                    event_id=f"chain:{self.canvas_runtime.evidence_run_id}:{event['chain']}:{event.get('failure_count')}",
                    notify_discord=self.schedule_test.notifications))
            except Exception:pass
        from src.rwtd.android_core.farm_runtime import failure_log_message
        detail = failure_log_message(event)
        self._log('FAILED', detail, **{key: event.get(key) for key in
                  ('chain', 'node', 'node_name', 'node_kind', 'reason',
                   'failure_count', 'failure_limit')})
        service = getattr(self.manual_handler, '__self__', None)
        task = getattr(service, 'task', None)
        if callable(getattr(task, 'log_warning', None)):
            task.log_warning(detail)
        if self.schedule_test.notifications and callable(getattr(task, 'notification', None)):
            task.notification(event['name']+'：達到失敗上限，已交接。',
                              title='RwtD Canvas 測試', tray=True, screenshot=False)

    def _finish_chain_test(self, seconds):
        test=self.chain_test
        if not test or test['status']!='RUNNING':
            return
        runtime=self.canvas_runtime
        if self.canvas_cancel.is_set() or runtime.source!='simulation' and self.device_stop.is_set():
            runtime.control('stop');test['status']='STOPPED'
        elif runtime.status=='STOPPED' and runtime.outcome=='SKIPPED':
            test['status']='COMPLETED'
        elif runtime.status=='BLOCKED' or runtime.status=='STOPPED' and runtime.outcome!='SUCCESS':
            test['failures']+=1
            if runtime.chain_timed_out or test.get('delivery_uncertain') or test['failures']>=test['policy'].get('failure_limit',3):
                callback=getattr(self,'exhaustion_callback',None)
                if (callable(callback) and not test.get('delivery_uncertain')
                        and test['failures']>=test['policy'].get('failure_limit',3)):
                    try:callback({'kind':'chain','source':runtime.source,'node':runtime.current_node or runtime.cursor,
                        'event_id':f"chain:{runtime.evidence_run_id}:{test['failures']}",'reason':runtime.reason,
                        'failure_count':test['failures'],'notify_discord':runtime.notify_discord})
                    except Exception:pass
                runtime.status,runtime.outcome='STOPPED','FAILED'
                runtime.reason=('CHAIN 執行時長達上限；單鍊測試失敗退出' if runtime.chain_timed_out else
                    'Chain 測試停止：輸入送達不確定' if test.get('delivery_uncertain') else 'Chain 測試達失敗上限')
                test['status']='FAILED'
            else:
                runtime.status,runtime.outcome='RUNNING',None
                runtime.pending_effect=runtime.condition_probe=None
                runtime.unmet_attempts.clear();runtime.ocr_attempts.clear();runtime.entry_guard_attempts.clear()
                runtime.due=runtime.now+runtime.graph['settings'].get('round_wait_seconds',1)
                runtime.reason='Chain 測試等待重試；不重播先前已完成步驟'
        elif runtime.status=='STOPPED':
            test['status']='COMPLETED'
        if test['status']!='RUNNING':
            service=getattr(self.manual_handler,'__self__',None)
            if hasattr(service,'reset_chain_test'):
                service.reset_chain_test()

    def _finish_node_test(self):
        test=self.node_test
        runtime=self.canvas_runtime
        if not test or test.get('status') not in {'CHECKING','RUNNING'} or runtime.status!='STOPPED':
            return
        self._sync_canvas_lock()
        test.update(armed=False,status='COMPLETE' if runtime.outcome=='SUCCESS' and not self.canvas_cancel.is_set() else 'STOPPED')
        if test['status']=='COMPLETE':
            runtime.reason=(f"從 {test['source_node']} 的 {test['output_port']} 出口開始測試完成；已檢查目標 Guard"
                            if test.get('output_port') else '從選取節點向下測試完成；已檢查附加 Guard')

    def _prepare_trigger_input(self):
        # Independent observers keep running while nodes send input. Resetting
        # every short node would starve a two-second capture indefinitely.
        if not self.trigger_observers:
            # NEXT now waits at the queued operation. Retiring that round here
            # would erase its cursor before finish_request can continue it.
            service=getattr(self.manual_handler,'__self__',None)
            task=getattr(service,'task',None)
            if hasattr(task,'trigger_frames'):
                task.trigger_frames.clear()

    def _invalidate_trigger_frames(self):
        """Refresh pixels without cancelling a node's pending NEXT cursor."""
        with self.lock, self.trigger_scheduler.lock:
            if self.trigger_observers:
                self.trigger_observers.invalidate()
            for task in self.trigger_scheduler.tasks.values():
                task['fresh_frame']=True
            service=getattr(self.manual_handler,'__self__',None)
            task=getattr(service,'task',None)
            if hasattr(task,'trigger_frames'):
                task.trigger_frames.clear()

    def _invalidate_trigger_rounds(self):
        """Handoff/new round: discard unfinished observations and stale work."""
        with self.lock, self.trigger_scheduler.lock:
            if self.trigger_observers:
                self.trigger_observers.invalidate()
            for watcher in self.trigger_scheduler.tasks.values():
                if watcher['status'] == 'RUNNING' and (self.trigger_observers or watcher['cursor'] is not None):
                    self.trigger_scheduler.queue = [q for q in self.trigger_scheduler.queue
                        if q['task_id'] != watcher['definition']['id'] or q['status'] == 'RUNNING']
                    watcher['generation'] += 1
                    self.trigger_scheduler._finish_round(watcher,'交接／新輪次；等待新一輪擷取')
                    watcher['variables'] = {}
            service = getattr(self.manual_handler,'__self__',None)
            task = getattr(service,'task',None)
            if hasattr(task,'trigger_frames'):
                task.trigger_frames.clear()

    def _advance_triggers(self):
        # Worker observations are independent; notification dispatch stays owned.
        with self.lock, self.trigger_scheduler.lock:
            if self.trigger_observers and self.trigger_scheduler.source != 'simulation':
                self.trigger_observers.sync()
                effect = next((self.trigger_scheduler._effect(t)
                               for t in self.trigger_scheduler.tasks.values()
                               if t['status']=='RUNNING' and t.get('pending')
                               and t['pending']['kind']=='notify'),None)
                if not effect or self.closed or self.device_stop.is_set() or self.trigger_cancel.is_set():
                    return
            else:
                effect = None
            busy = (self.closed or self.device_stop.is_set() or self.trigger_cancel.is_set()
                    or self.device_pending or self.capture_pending or self.active is not None
                    or bool(self.queue))
            if not self.trigger_observers or self.trigger_scheduler.source == 'simulation':
                effect = self.trigger_scheduler.poll(busy=busy)
        if not effect:
            return
        try:
            if effect['source'] == 'simulation':
                result = self.trigger_scheduler.simulation_result(effect)
            else:
                result = self.manual_handler('canvas_effect',effect,self.trigger_cancel) if self.manual_handler else None
        except Exception as exc:
            result = {'status':'FAILED','error_type':type(exc).__name__}
        with self.lock:
            if self.trigger_cancel.is_set() or self.device_stop.is_set():
                task = self.trigger_scheduler.tasks.get(effect['task_id'])
                if task:
                    self.trigger_scheduler._finish_round(task,'擷取已取消；不採用舊結果')
                    task['variables'] = {}
            else:
                self.trigger_scheduler.complete(effect,result)

    def _advance_trigger_queue(self):
        """Owner dispatch at Canvas node boundaries; never between module steps."""
        with self.lock, self.trigger_scheduler.lock:
            if (self.closed or self.device_stop.is_set() or self.trigger_cancel.is_set()
                    or self.device_pending or self.capture_pending
                    or self.active is not None or self.queue or self.trigger_work):
                return
            if self.canvas_runtime.pending_effect:
                return  # A partially executed node has not reached an insertion point.
            if self.canvas_runtime.status=='WAITING_MANUAL':
                return
            if (self.canvas_runtime.status == 'RUNNING'
                    and self.canvas_runtime.cursor in self.canvas_runtime.fixed_counts):
                return  # Finish this node's fixed repetitions before insertion.
            if not self.trigger_scheduler.queue or self.trigger_scheduler.queue[0]['status'] != 'QUEUED':
                return
            candidate=self.trigger_scheduler.queue[0]
            # A failure-event Solver has the lowest operation priority. Read
            # observations continue independently; ordinary work is not paused
            # merely because a Solver has queued an input.
            owner=getattr(self,'continuation_owner',None)
            allow_solver=(getattr(owner,'handoff_pending',False)
                          or self.canvas_runtime.status not in {'RUNNING','WAITING_MANUAL'})
            if candidate.get('solver') and not allow_solver:
                return
            timing_details={'request_id':candidate['id'],'trigger':candidate['task_id'],
                            'node':candidate['node'],'module':candidate['module']}
        from src.rwtd.android_core.log_policy import timing_start,timing_mark
        task=getattr(getattr(self.manual_handler,'__self__',None),'task',None)
        catalog_started=timing_start(task)
        timing_mark(task,'QUEUE_READY',**timing_details)
        timing_mark(task,'CATALOG_START',**timing_details)
        services = self.manual_handler('execution_status',{},self.trigger_cancel) if self.manual_handler else {}
        timing_mark(task,'CATALOG_DONE',catalog_started,**timing_details)
        if services.get('module_run',{} ) and services['module_run'].get('active'):
            return
        with self.lock:
            simulated_result=None
            if self.trigger_scheduler.source=='simulation' and self.trigger_scheduler.queue[0]['definition'].get('entry_guard'):
                simulated_result=self.trigger_scheduler.simulate_guarded_request(self.trigger_scheduler.queue[0])
                if simulated_result is None:
                    return
            request = self.trigger_scheduler.claim(allow_solver=allow_solver)
            if not request:
                return
            timing_mark(task,'QUEUE_TAKE',request_id=request['id'],trigger=request['task_id'],
                        node=request['node'],module=request['module'])
            self.trigger_work = request
            self.device_pending = True
            self._sync_canvas_lock()
        try:
            if self.trigger_scheduler.source == 'simulation':
                result = simulated_result or {'status':'SIMULATED','action_completed':True,'device_inputs':0}
            elif (self.mode != 'Manual Device Test' or not self.allow_manual
                    or not self.trigger_scheduler.queue_inputs):
                result = {'status':'BLOCKED','action_completed':False,'device_inputs':0}
            else:
                prepare_started=timing_start(task)
                self._prepare_trigger_input()
                timing_mark(task,'TRIGGER_PREPARE_DONE',prepare_started,
                            request_id=request['id'],node=request['node'])
                from src.rwtd.android_core.log_policy import trigger_event
                trigger_event(self,'TRIGGER_START','TriggerTask 操作佇列開始',node=request['node'],module=request['module'])
                timing_mark(task,'TRIGGER_EXECUTE_START',request_id=request['id'],
                            trigger=request['task_id'],node=request['node'],module=request['module'])
                execute_started=timing_start(task)
                result = self.manual_handler('trigger_execute',request,self.trigger_cancel)
                timing_mark(task,'TRIGGER_EXECUTE_RETURN',execute_started,
                            request_id=request['id'],node=request['node'],status=result.get('status'))
        except Exception as exc:
            result = {'status':'FAILED','action_completed':False,'failure_reason':str(exc),'device_inputs':0}
        finally:
            with self.lock:
                self.device_pending = False
                self.trigger_work = None
                self._sync_canvas_lock()
        with self.lock:
            self.trigger_scheduler.finish_request(request,result)
            self.manual_result = result
            if result.get('report_id'):
                self.manual_history.append(result)
            from src.rwtd.android_core.log_policy import trigger_event
            trigger_event(self,'TRIGGER_DONE','TriggerTask 佇列執行結果',node=request['node'],status=result.get('status','FAILED'))
        return True

