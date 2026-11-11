"""Data-only Canvas definition and incremental Simulation runtime; never imports ADB."""
import copy
import hashlib
import json
import math
import uuid
from src.rwtd.android_core.module_store import identifier

FIELDS = {"progress_km", "retreatable_km", "food_count", "teleport_distance_km", "exception", "stage_complete", "store_reward"}
FLAGS = {"exception", "stage_complete", "store_reward"}
KINDS = {"start", "module", "condition", "guard", "wait", "manual", "end", "exclusion", "ocr", "notify", "action", "coco", "ocr_decision"}
PROGRESS_OCR_ROI = [330, 370, 156, 55]
PROGRESS_OCR_THRESHOLD = .7


def number(value, low=0, high=100000):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def ports(node):
    if node['kind'] == 'ocr':
        from src.rwtd.android_core.canvas_ocr import ocr_ports
        return ocr_ports(node)
    return ["YES", "NO"] if node["kind"] in {"condition", "coco", "ocr_decision"} else ["TRIGGER"] if node["kind"] == "guard" else [] if node["kind"] == "end" else ["NEXT"]


def migrate_graph(value):
    """Pure migration preview. Existing file is untouched until explicit Save."""
    if value.get("schema_version") == 2:
        return copy.deepcopy(value)
    nodes, edges, positions = [], [], {}
    for old in value["nodes"]:
        n = {k: copy.deepcopy(v) for k, v in old.items() if k not in {"x", "y", "next", "yes", "no"}}
        nodes.append(n)
        positions[n["id"]] = {"x": old.get("x", 0), "y": old.get("y", 0)}
        for key, port in (("next", "NEXT"), ("yes", "YES"), ("no", "NO")):
            if key in old:
                edges.append({"id": "E" + str(len(edges) + 1), "source": n["id"], "port": port, "target": old[key]})
    return {"schema_version": 2, "entry": value["entry"], "nodes": nodes, "edges": edges,
            "settings": {"max_cycles": 10, "round_wait_seconds": 1, "node_handoff_seconds": 1},
            "ui": {"positions": positions, "viewport": {"x": 0, "y": 0, "zoom": 1}}}


def validate_canvas(value, module_ids):
    value = copy.deepcopy(value)
    if not isinstance(value, dict) or value.get("schema_version") != 2 or set(value) - {"schema_version", "entry", "nodes", "edges", "settings", "ui", "revision"}:
        raise ValueError("Invalid Canvas definition")
    nodes, edges = value.get("nodes"), value.get("edges")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 100 or not isinstance(edges, list) or len(edges) > 200:
        raise ValueError("Canvas supports 1..100 nodes and at most 200 edges")
    by_id = {}
    from src.rwtd.android_core.canvas_ocr import normalize_conditions
    normalize_conditions(nodes)
    allowed = {"id", "kind", "name", "chain", "entry_guard", "repeat_runs", "module", "field", "op", "value", "seconds", "feature", "roi", "tag", "threshold", "enabled", "priority", "handler", "match", "dictionary", "output_key", "variable", "message", "cooldown_seconds", "step_id", "feature_map", "execution_scope", "task_type", "trigger_interval", "preempt_operations", "ocr_cases", "ocr_retry", "ocr_region", "ocr_feature", "ocr_coco_threshold", "ocr_mode", "ocr_output_type", "ocr_trim", "ocr_conversion"}
    allowed.update({'entry_guard_retry', 'unknown_no_retry', 'suppress_failure_notification',
                    'share_guard_time_by_name'})
    allowed.update({'progress_exit_on_limit','input_source','end_step_id'})
    allowed.add('round_timing')
    allowed.update({'exclusion_mode', 'exclusion_scope', 'exclusion_colors'})
    for n in nodes:
        if not isinstance(n, dict) or set(n) - (allowed | {'handoff_chain'}):
            raise ValueError("Unknown Canvas node fields")
        if 'handoff_chain' in n and (n.get('kind')!='start' or n.get('task_type')!='TriggerTask'):
            raise ValueError('Trigger handoff belongs to a TriggerTask start')
        if 'handoff_chain' in n:
            raise ValueError('Trigger 完成後交接已移除；請改用 NEXT 連線並重新套用該開始節點；儲存檔未變更')
        identifier(n.get("id"))
        from src.rwtd.android_core.canvas_repeat import runs
        runs(n)
        if n["id"] in by_id or n.get("kind") not in KINDS:
            raise ValueError("Invalid Canvas node identity/type")
        by_id[n["id"]] = n
        if any(k in n for k in ('exclusion_mode', 'exclusion_scope', 'exclusion_colors')) and n['kind'] != 'exclusion':
            raise ValueError('Exclusion fields belong to exclusion nodes')
        if 'suppress_failure_notification' in n and type(n['suppress_failure_notification']) is not bool:
            raise ValueError('失敗時不傳通知須為布林值')
        if 'unknown_no_retry' in n and (type(n['unknown_no_retry']) is not bool
                or not (n['kind'] in {'coco','ocr','ocr_decision'}
                        or n['kind']=='condition' and n.get('variable')
                        or n.get('entry_guard'))):
            raise ValueError('UNKNOWN 不重試須為布林值，僅適用辨識節點、OCR 變數條件或附加 Guard')
        if 'progress_exit_on_limit' in n and (type(n['progress_exit_on_limit']) is not bool
                or n['kind'] != 'condition' or n.get('field') not in {'progress_km','retreatable_km'} or n.get('variable')):
            raise ValueError('PROGRESS 或次數退出僅適用於未引用 OCR 變數的 PROGRESS 條件節點')
        if 'execution_scope' in n and n['kind'] != 'action':
            raise ValueError('Execution scope belongs to an action node')
        if any(k in n for k in ('input_source','end_step_id')) and n['kind'] != 'action':
            raise ValueError('Action source/range belongs to an action node')
        if "name" in n and (not isinstance(n["name"], str) or len(n["name"]) > 80):
            raise ValueError("Invalid node name")
        if 'preempt_operations' in n:
            if (type(n['preempt_operations']) is not bool or n['kind']!='start'
                    or n.get('task_type')!='TriggerTask'
                    or (n['preempt_operations'] and n.get('trigger_interval',3)==0)):
                raise ValueError('Operation preemption requires a nonzero-interval TriggerTask')
        if 'task_type' in n or 'trigger_interval' in n:
            if n['kind'] != 'start' or n.get('task_type','ModuleTask') not in {'ModuleTask','TriggerTask'}:
                raise ValueError('Task properties belong to a start node')
        if n['kind'] == 'start' and n.get('task_type') == 'TriggerTask':
            interval = n.get('trigger_interval',3)
            if not (number(interval,0,3600) and (interval==0 or interval>=.5)) or type(n.get('enabled',True)) is not bool:
                raise ValueError('TriggerTask interval must be zero (failure event) or 0.5..3600 seconds')
        if n['kind'] == 'coco':
            identifier(n.get('feature'))
            if not number(n.get('threshold',.8),.5,1) or n.get('match','detected') not in {'detected','disappeared'}:
                raise ValueError('Invalid COCO decision')
            if n.get('tag'):
                raise ValueError('COCO decision uses an explicit ROI, not a module TAG')
            if 'roi' in n:
                from src.rwtd.android_core.workflow_model import validate_roi
                validate_roi(n['roi'])
        if n['kind'] == 'exclusion':
            from .canvas_exclusion import validate as validate_exclusion
            validate_exclusion(n)
            if n.get('exclusion_mode', 'coco') in {'coco', 'coco_or_color'}:
                identifier(n.get('feature'))
                if not number(n.get('threshold', .8), .5, 1):
                    raise ValueError('Invalid exclusion threshold')
        if n['kind'] == 'ocr_decision':
            from .canvas_ocr_decision import validate
            validate(n)
        elif n['kind'] == 'ocr':
            from src.rwtd.android_core.canvas_ocr import validate_ocr_node
            validate_ocr_node(n)
        elif any(key in n for key in ('ocr_cases', 'ocr_retry', 'ocr_region', 'ocr_feature', 'ocr_coco_threshold', 'ocr_mode', 'ocr_output_type', 'ocr_trim', 'ocr_conversion')):
            raise ValueError('OCR cases/retry belong to an OCR node')
        if n['kind'] == 'notify':
            from .notification_timing import ROLES
            if not isinstance(n.get('round_timing', 'none'), str) or n.get('round_timing', 'none') not in ROLES:
                raise ValueError('Invalid notification statistics marker')
            if not isinstance(n.get('message'), str) or not n['message'].strip() or len(n['message']) > 1000:
                raise ValueError('Notification requires a short message')
            if not number(n.get('cooldown_seconds', 120), 1, 86400):
                raise ValueError('Invalid notification cooldown')
        elif 'round_timing' in n:
            raise ValueError('Statistics markers belong to notification nodes')
        if n.get('variable'):
            if n['kind'] not in {'condition','notify','action'}:
                raise ValueError('Variable binding supports condition/notification/action nodes')
            from src.rwtd.android_core.canvas_ocr import variable_name
            variable_name(n['variable'])
        if n['kind'] == 'action':
            if n.get('input_source','variable') != 'step' and not n.get('variable'):
                raise ValueError('Action requires an OCR variable')
            from src.rwtd.android_core.canvas_action import validate_action_node
            validate_action_node(n,module_ids)
        if n["kind"] in {"module", "action"} or n.get("tag"):
            if n.get("module") not in module_ids:
                raise ValueError("Graph references a missing module")
        if n["kind"] in {"condition", "guard"}:
            if n['kind'] == 'condition' and n.get('variable'):
                numeric=type(n.get('value')) in (int,float) and math.isfinite(n['value'])
                textual=isinstance(n.get('value'),str) and 0<len(n['value'])<=100
                if not (numeric and n.get('op') in {'eq','gte','lte'} or textual and n.get('op') in {'eq','contains'}):
                    raise ValueError('OCR condition requires text eq/contains or numeric eq/gte/lte')
            elif n["kind"] == "guard" and n.get("feature"):
                identifier(n["feature"])
                if n.get("match", "detected") not in {"detected", "disappeared"} or not number(n.get("threshold", .8), .5, 1):
                    raise ValueError("Invalid guard recognition condition")
            else:
                if n.get("field") not in FIELDS or n.get("op") not in {"eq", "gte", "lte"}:
                    raise ValueError("Unknown Canvas condition")
                if n["field"] in FLAGS:
                    if n["op"] != "eq" or type(n.get("value")) is not bool:
                        raise ValueError("Boolean conditions require eq and boolean")
                elif not number(n.get("value"), 0, 1e15):
                    raise ValueError("Invalid numeric condition")
        if n["kind"] == "wait" and not number(n.get("seconds"), 0, 300):
            raise ValueError("Wait requires 0..300 seconds")
        if n["kind"] == "guard":
            if type(n.get("enabled", True)) is not bool or type(n.get("priority", 0)) is not int or not 0 <= n.get("priority", 0) <= 1000:
                raise ValueError("Invalid guard priority/enabled")
            if n.get("handler", "block") not in {"pause", "block", "stop", "manual", "queue"}:
                raise ValueError("Invalid guard handler")
            if n.get("tag"):
                identifier(n["tag"])
                if isinstance(module_ids, dict) and n["tag"] not in module_ids[n["module"]].get("tags", {}):
                    raise ValueError("Guard references an invalid module TAG")
            if "roi" in n and (not isinstance(n["roi"], list) or len(n["roi"]) != 4 or not all(number(v, 0, 2400) for v in n["roi"]) or n["roi"][2] <= 0 or n["roi"][3] <= 0 or n["roi"][0] + n["roi"][2] > 1080 or n["roi"][1] + n["roi"][3] > 2400):
                raise ValueError("Invalid guard ROI")
            if n.get("tag") and "roi" in n:
                raise ValueError("Use TAG or ROI, not both")
    for n in nodes:
        pre = n.get('ocr_retry',{}).get('precondition')
        if pre and (pre not in by_id or by_id[pre]['kind'] != 'coco'):
            raise ValueError('OCR precondition must reference a COCO node')
    outputs = [n['output_key'] for n in nodes if n['kind'] == 'ocr']
    if len(outputs) != len(set(outputs)):
        raise ValueError('OCR output variable names must be unique')
    for n in nodes:
        from src.rwtd.android_core.canvas_entry_guard import validate as validate_entry_guard
        validate_entry_guard(n, outputs)
        if n.get('variable') and n['variable'] not in outputs:
            raise ValueError('Missing module-local OCR variable producer')
    if value.get("entry") not in by_id or by_id[value["entry"]]["kind"] == "guard":
        raise ValueError("Missing/nonsequential entry")
    edge_ids, outputs = set(), set()
    for e in edges:
        if not isinstance(e, dict) or set(e) != {"id", "source", "port", "target"}:
            raise ValueError("Invalid edge fields")
        identifier(e["id"])
        pair = (e["source"], e["port"])
        if e.get('target') in by_id and by_id[e['target']]['kind'] == 'start':
            raise ValueError('START 只有出口，沒有入口；連線不能指向開始節點：' + e['target'])
        if e["id"] in edge_ids or pair in outputs or e["source"] not in by_id or e["target"] not in by_id or e["port"] not in ports(by_id[e["source"]]) or by_id[e["target"]]["kind"] == "guard":
            raise ValueError("Invalid/duplicate edge or port")
        edge_ids.add(e["id"])
        outputs.add(pair)
    for n in nodes:
        if n["kind"] != "guard" and any((n["id"], port) not in outputs for port in ports(n)):
            raise ValueError("Missing NEXT or YES/NO branch: " + n["id"])
    for n in nodes:
        if n['kind'] == 'guard' and n.get('handler') == 'queue':
            route = next((e for e in edges if e['source'] == n['id'] and e['port'] == 'TRIGGER'), None)
            if not route or by_id[route['target']]['kind'] != 'module':
                raise ValueError('Queued trigger requires TRIGGER connected to a module node')
    settings = value.setdefault("settings", {})
    if not isinstance(settings, dict) or set(settings) - {"max_cycles", "round_wait_seconds", "node_handoff_seconds", "chain_timeout_minutes", "game_stage", "exit_condition", "feature_rois"}:
        raise ValueError("Invalid loop settings")
    if 'game_stage' in settings and (type(settings['game_stage']) is not int or not 1<=settings['game_stage']<=5):
        raise ValueError('Game stage must be 1..5')
    shared = settings.get('feature_rois',{})
    if not isinstance(shared,dict) or len(shared)>200:
        raise ValueError('Invalid shared Feature ROIs')
    for feature,roi in shared.items():
        identifier(feature)
        from src.rwtd.android_core.workflow_model import validate_roi
        validate_roi(roi)
    maximum = settings.get("max_cycles", 10)
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= 1000):
        raise ValueError("Loop maximum must be null or 1..1000")
    if not number(settings.get("round_wait_seconds", 1), .1, 300):
        raise ValueError("Round wait must be 0.1..300 seconds; zero-wait loops prohibited")
    if not number(settings.get("node_handoff_seconds", 1), 0, 30):
        raise ValueError("Node handoff interval must be 0..30 seconds")
    if not number(settings.get('chain_timeout_minutes',25), .01, 10080):
        raise ValueError('CHAIN timeout must be between .01 and 10080 minutes')
    if "exit_condition" in settings:
        condition = settings["exit_condition"]
        if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
            raise ValueError("Invalid loop exit condition")
        probe = {"schema_version": 2, "entry": "Exit", "nodes": [dict(condition, id="Exit", kind="condition"), {"id": "End", "kind": "end"}], "edges": [{"id": "Y", "source": "Exit", "port": "YES", "target": "End"}, {"id": "N", "source": "Exit", "port": "NO", "target": "End"}]}
        validate_canvas(probe, module_ids)
    ui = value.get("ui", {})
    if not isinstance(ui, dict) or set(ui) - {"positions", "viewport"}:
        raise ValueError("Invalid Canvas presentation data")
    positions = ui.get("positions", {})
    if not isinstance(positions, dict) or set(positions) - set(by_id):
        raise ValueError("Invalid node position references")
    for position in positions.values():
        if not isinstance(position, dict) or set(position) != {"x", "y"} or not all(number(v, -100000, 100000) for v in position.values()):
            raise ValueError("Invalid Canvas position")
    view = ui.get("viewport", {"x": 0, "y": 0, "zoom": 1})
    if not isinstance(view, dict) or set(view) != {"x", "y", "zoom"} or not number(view["zoom"], .2, 3) or not all(number(view[k], -100000, 100000) for k in ("x", "y")):
        raise ValueError("Invalid Canvas viewport")
    from src.rwtd.android_core.canvas_chains import validate_chains
    validate_chains(value)
    from src.rwtd.android_core.canvas_triggers import validate_triggers
    validate_triggers(value)
    value.pop("revision", None)
    value["revision"] = hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return value


def condition(n, observations, variables=None):
    if n.get('variable'):
        sample = (variables or {}).get(n['variable'], {})
        if sample.get('verified') is not True:
            return None
        actual=sample.get('value')
        if type(n['value']) in (int,float):
            if type(actual) not in (int,float) or not math.isfinite(actual):
                return None
            return actual==n['value'] if n['op']=='eq' else actual>=n['value'] if n['op']=='gte' else actual<=n['value']
        if not isinstance(actual,str):
            return None
        return actual == n['value'] if n['op'] == 'eq' else n['value'] in actual
    actual = observations.get(n.get("field"))
    if actual is None:
        return None  # UNKNOWN never means absent/false.
    if n["field"] in FLAGS:
        if type(actual) is not bool:
            raise ValueError("Unverified simulation flag")
    elif not number(actual, 0, 1e15):
        raise ValueError("Invalid simulation count")
    return actual == n["value"] if n["op"] == "eq" else actual >= n["value"] if n["op"] == "gte" else actual <= n["value"]


class CanvasRuntime:
    """One task-thread tick per transition; manual confirmation never advances implicitly."""
    def __init__(self):
        from .time_guard import TimeGuards
        self.time_guards=TimeGuards()
        self.status, self.outcome, self.reason = "IDLE", None, "尚未啟動排程"
        self.cursor, self.graph, self.observations = None, None, {}
        self.current_node = None
        self.trace, self.completed, self.guard, self.edge = [], [], None, None
        self.visits, self.due, self.now, self.pending_next = {}, 0, 0, None
        self.waiting_kind = None
        self.trigger_queue, self.trigger_latched = [], set()
        self.running_trigger = None
        self.exclusion_regions = {}
        self.variables, self.notifications = {}, []
        self.ocr_attempts = {}
        self.unmet_attempts = {}
        self.exit_unmet_attempts = {}
        self.condition_probe = None
        self.progress_reading = None
        self.exit_progress_reading = None
        self.fixed_counts = {}
        self.chain_completed_runs = 0
        self.guard_repeat_due = {}
        self.entry_guard_ready = set()
        self.entry_guard_attempts = {}
        self.simulation_action_rounds = {}
        self.entry_visits = {}
        self.entry_pending = True
        self.pending_effect = None
        self.source, self.notify_discord = 'simulation', False
        self.action_plan, self.action_result = None, None
        self.device_inputs = 0
        self.unattended = False
        self.block_counts = {}
        self.exhaustion_callback = None
        self.evidence_run_id = uuid.uuid4().hex
        self.continuation_target = None
        self.owner_boundaries = False
        self.owner_chain_timeout = False
        self.chain_timeout_started = 0
        self.chain_timed_out = False
        self.chain_clock = None
        self.flow_key = None
        self.completion = None

    @property
    def locked(self):
        return self.status not in {"IDLE", "STOPPED"}

    def start(self, graph, observations, modules, source='simulation', notify_discord=False, unattended=False):
        if self.locked:
            raise ValueError("Scheduler already active; Stop before starting again")
        from .retreatable_km import resolve_graph
        graph = resolve_graph(validate_canvas(migrate_graph(graph), modules))
        if source not in {'simulation', 'android_readonly', 'android_manual'} or type(notify_discord) is not bool:
            raise ValueError('Invalid Scheduler observation source/notification permission')
        if source == 'simulation' and notify_discord:
            raise ValueError('Simulation cannot send Discord notifications')
        if source == 'android_readonly' and (any(n['kind'] not in {'start','ocr','coco','ocr_decision','exclusion','condition','notify','wait','manual','end'}
                or (n['kind'] == 'condition' and not n.get('variable') and n.get('field') != 'progress_km') for n in graph['nodes'])
                or (graph.get('settings', {}).get('exit_condition')
                    and graph['settings']['exit_condition']['field'] != 'progress_km')):
            raise ValueError('Read-only OCR flow cannot execute modules or simulated gameplay observations')
        if source == 'android_manual' and not unattended and (any(n['kind'] not in {'start','ocr','coco','ocr_decision','exclusion','condition','notify','action','wait','manual','end'}
                or (n['kind']=='condition' and not n.get('variable') and n.get('field') != 'progress_km') for n in graph['nodes'])
                or (graph.get('settings',{}).get('exit_condition')
                    and graph['settings']['exit_condition']['field'] != 'progress_km')):
            raise ValueError('Manual Canvas flow permits explicit single-step actions only')
        for n in graph["nodes"]:
            if n.get("tag") and n["tag"] not in modules[n["module"]].get("tags", {}):
                raise ValueError("Guard references an invalid module TAG")
        time_guards=self.time_guards
        exhaustion_callback=self.exhaustion_callback
        self.__init__()
        self.time_guards=time_guards
        self.exhaustion_callback=exhaustion_callback
        self.unattended = unattended
        self.source, self.notify_discord = source, notify_discord
        self.graph, self.observations, self.modules = graph, copy.deepcopy(observations), copy.deepcopy(modules)
        if not isinstance(self.observations, dict) or set(self.observations) - (FIELDS | {"features", "ocr"}):
            raise ValueError("Unknown simulated observations")
        self.cursor, self.status = graph['entry'], 'RUNNING'
        from .flow_key import start_key
        self.flow_key=start_key(graph['entry'])
        self.reason = ('Simulation 已啟動；沒有 Android 輸入' if source == 'simulation'
                       else 'Canvas 連續測試已啟動；依流程執行，未宣稱遊戲結果已驗證' if source == 'android_manual' and unattended
                       else 'OCR 流程已啟動；操作節點需人工執行一次' if source == 'android_manual'
                       else '唯讀 OCR 流程已啟動；沒有 Android 輸入')
        self.current_node = self.cursor

    def control(self, command, payload=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("Scheduler control requires a data object")
        if command == "stop":
            self.status, self.reason, self.pending_next = "STOPPED", "使用者停止排程", None
            self.exclusion_regions = {k:v for k,v in self.exclusion_regions.items() if not v.get('target_node')}
            self.trigger_queue.clear()
            self.running_trigger = None
            self.pending_effect = None
        elif command == "pause" and self.status == "RUNNING":
            self.status, self.reason = "PAUSED", "使用者暫停"
        elif command == "resume" and self.status in {"PAUSED", "BLOCKED"}:
            self.status, self.reason = "RUNNING", "繼續前重新檢查 Guard"
        elif command == 'execute_once' and self.status == 'WAITING_MANUAL' and self.waiting_kind == 'action_execute':
            if set(payload) != {'explicit_activation'} or payload['explicit_activation'] is not True:
                raise ValueError('Explicit Execute Once required')
            node = next(n for n in self.graph['nodes'] if n['id']==self.cursor)
            self.pending_effect = copy.deepcopy(node)
            self.status, self.waiting_kind, self.reason = 'RUNNING', None, '操作節點執行一次；不是成功判定'
        elif command == "verify" and self.status == "WAITING_MANUAL":
            if type(payload.get("pass")) is not bool:
                raise ValueError("Manual Pass/Fail required")
            if self.waiting_kind == 'action_execute':
                raise ValueError('Execute the action once before Pass/Fail')
            if not payload["pass"]:
                self.status, self.outcome, self.reason = "STOPPED", "FAILED", "人工 Fail"
            else:
                self.status, self.reason = "RUNNING", "MANUAL_VERIFIED"
                if self.waiting_kind in {"node", 'action_verify'}:
                    self._next("NEXT")
                self.waiting_kind = None
        elif command == "observe" and self.locked:
            if self.source != 'simulation' or set(payload) - (FIELDS | {"features", "ocr"}):
                raise ValueError("Unknown simulated observation")
            self.observations = copy.deepcopy(payload)
        else:
            raise ValueError("Control not available in current Scheduler state")

    def _guard_value(self, n):
        if not n.get("feature"):
            return condition(n, self.observations)
        features = self.observations.get("features")
        if not isinstance(features, dict):
            return None
        # Observation keys include scope: a whole-screen hit cannot satisfy a TAG/ROI guard.
        key = n["feature"] + ("@" + n["module"] + ":" + n["tag"] if n.get("tag") else "@" + ",".join(map(str, n["roi"])) if n.get("roi") else "")
        sample = features.get(key)
        if not isinstance(sample, dict) or sample.get("verified") is not True or not number(sample.get("confidence"), 0, 1) or type(sample.get("detected")) is not bool:
            return None
        if sample["detected"] and sample["confidence"] < n.get("threshold", .8):
            return None
        return sample["detected"] if n.get("match", "detected") == "detected" else not sample["detected"]

    def _next(self, port, allow_fixed=True, mark_completed=True):
        node=next(n for n in self.graph['nodes'] if n['id']==self.cursor)
        self.progress_reading = None
        self.entry_guard_ready.discard(self.cursor)
        self.entry_guard_attempts.pop(self.cursor,None)
        if allow_fixed and node['kind'] not in {'action','start'} and self._repeat_node(node):
            return
        if mark_completed and node['kind']!='start':self.time_guards.complete(node)
        self.unmet_attempts.pop(self.cursor,None)
        self.exit_unmet_attempts.pop(self.cursor,None)
        self.condition_probe = None
        self.ocr_attempts.pop(self.cursor,None)
        edge = next(e for e in self.graph["edges"] if e["source"] == self.cursor and e["port"] == port)
        from .canvas_exclusion import leave
        leave(self, self.cursor)
        self.edge = edge["id"]
        self.completed.append(self.cursor)
        self.completed = self.completed[-200:]
        target = edge["target"]
        target_node = next(n for n in self.graph['nodes'] if n['id'] == target)
        if target_node['kind'] == 'start' and target != self.graph['entry']:
            if self.owner_boundaries:
                self.continuation_target = target
                self.cursor = target
                self.status, self.outcome, self.reason = 'STOPPED', 'CONTINUE', 'NEXT → ' + target
                return
            # Standalone execution also treats the linked start as a fresh
            # entry; only the subsequently reached END earns SUCCESS.
            self.graph['entry'] = target
            self.variables.clear()
            self.chain_completed_runs = 0
            self.entry_guard_ready.clear()
        if target in self.entry_visits:
            self.pending_next = target
            self.due = self.now + self.graph.get("settings", {}).get("round_wait_seconds", 1)
        else:
            self.cursor = target
        if self.source == 'android_manual' or 'node_handoff_seconds' in self.graph.get('settings', {}):
            self.due = max(self.due, self.now + self.graph.get('settings', {}).get('node_handoff_seconds', 1))
        self.entry_pending=True

    def _repeat_node(self,node):
        from src.rwtd.android_core.canvas_repeat import repeat_node
        if not repeat_node(node,self.fixed_counts):
            return False
        self.entry_guard_ready.discard(node['id'])
        self.ocr_attempts.pop(node['id'],None)
        self.unmet_attempts.pop(node['id'],None)
        if node['kind']=='exclusion':
            self.exclusion_regions.pop(node['id'],None)
        if node['kind']=='condition' and node.get('variable') and any(p.get('output_key')==node['variable'] and p['id'] in getattr(self,'refreshable_nodes',{q['id'] for q in self.graph['nodes']}) for p in self.graph['nodes']):
            self.condition_probe=copy.deepcopy(next(p for p in self.graph['nodes']
                if p.get('kind')=='ocr' and p.get('output_key')==node['variable']))
        self.due=self.now+self.graph.get('settings',{}).get('round_wait_seconds',1)
        self.reason=f"固定執行 · {self.fixed_counts[node['id']]}/{node['repeat_runs']} 已完成"
        return True

    def _retry_unmet(self, node, *, unknown=False):
        attempt=self.unmet_attempts.get(node['id'],0)+1
        self.unmet_attempts[node['id']]=attempt
        if unknown and node.get('unknown_no_retry') is True:
            self.reason='UNKNOWN 不重試；不選擇分支'
            return False
        maximum=self.graph.get('settings',{}).get('max_cycles',10)
        if maximum is not None and attempt>=maximum:
            self.reason='條件未成立；嘗試次數已達 '+str(attempt)
            self._exhaustion(node,attempt,maximum,self.reason)
            return False
        self.due=self.now+self.graph.get('settings',{}).get('round_wait_seconds',1)
        self.reason='條件未成立；等待更新資料 · '+str(attempt)+'/'+str(maximum or '不限')
        return True

    def _exhaustion(self,node,attempt,maximum,reason,kind='recognition'):
        callback=self.exhaustion_callback
        if not callable(callback) or self.source=='simulation':return
        event={'kind':kind,'node':node['id'],'node_kind':node['kind'],
               'event_id':f"{self.evidence_run_id}:{self.chain_completed_runs}:{node['id']}:{self.visits.get(node['id'],0)}:{kind}:{attempt}",
               'attempt_count':attempt,'attempt_limit':maximum,'reason':reason,
               'source':self.source,'notify_discord':self.notify_discord}
        event['recognition']={k:copy.deepcopy(node[k]) for k in
            ('feature','threshold','roi','op','value') if k in node}
        if self.trace and self.trace[-1].get('progress_ocr') is not None:
            event['recognition']['progress_ocr']=copy.deepcopy(self.trace[-1]['progress_ocr'])
        if kind=='node_limit':
            event.update(source_node=next((e['source'] for e in self.graph['edges'] if e['id']==self.edge),None),
                         target_node=node['id'],executed_count=attempt,execution_limit=maximum)
        try:callback(event)
        except Exception:pass  # Evidence cannot change scheduling or replay an action.

    def _block_node(self,node,reason):
        """A consumed BLOCK result follows only an explicit NEXT, without replay."""
        self.block_counts[node['id']]=self.block_counts.get(node['id'],0)+1
        self._exhaustion(node,self.block_counts[node['id']],None,reason,kind='blocked')
        self.trace[-1]['block']={'reason':reason,'count':self.block_counts[node['id']]}
        if any(e['source']==node['id'] and e['port']=='NEXT' for e in self.graph['edges']):
            self.pending_effect=None
            self.waiting_kind=None
            self.status='RUNNING'
            self._next('NEXT',allow_fixed=False,mark_completed=False)
            if self.completed and self.completed[-1]==node['id']:self.completed.pop()
            self.reason=reason+'；BLOCK 已記錄，沿 NEXT 繼續'
        else:
            self.status,self.reason='BLOCKED',reason+'；缺少 NEXT，未猜測出口'

    def _complete_entry_guard(self, node, allowed, skip_now=False):
        if allowed is True:
            self.entry_guard_ready.add(node['id'])
            self.entry_guard_attempts.pop(node['id'],None)
            return
        attempt=self.entry_guard_attempts.get(node['id'],0)+1
        self.entry_guard_attempts[node['id']]=attempt
        from .canvas_entry_guard import retry_limit
        maximum=retry_limit(node,self.graph['settings'])
        if allowed is None and not skip_now and (maximum is None or attempt<maximum):
            self.due=self.now+self.graph['settings'].get('round_wait_seconds',1)
            self.reason=f"附加 Guard UNKNOWN；等待重試 · {attempt}/{maximum or '不限'}"
            return
        reason=('附加 Guard 未成立；跳過 ' if allowed is False else
                '附加 Guard UNKNOWN 不重試；跳過 ' if node.get('unknown_no_retry') is True else
                '附加 Guard UNKNOWN 次數用完；跳過 ')+node['id']
        if allowed is None and not skip_now and not node.get('unknown_no_retry'):
            self._exhaustion(node,attempt,maximum,reason)
        self._skip_node(node,reason)
        self.trace[-1]['entry_guard']='SKIPPED'

    def _skip_node(self,node,reason):
        """Apply the node's skip outcome, independently of what rejected it."""
        from src.rwtd.android_core.canvas_node_flow import skip_port,skip_reason
        self.fixed_counts.pop(node['id'],None)
        self.simulation_action_rounds.pop(node['id'],None)
        self.entry_guard_ready.discard(node['id'])
        self.entry_guard_attempts.pop(node['id'],None)
        port=skip_port(node)
        if port is None:
            self.status,self.outcome='STOPPED','SKIPPED'
        else:
            self._next(port,allow_fixed=False,mark_completed=False)
            if self.completed and self.completed[-1]==node['id']:
                self.completed.pop()
        self.trace.append({'node':node['id'],'kind':node['kind'],'outcome':'SKIPPED','device_inputs':0})
        self.trace=self.trace[-200:]
        self.reason=skip_reason(node,reason)

    def _check_entry_guard(self,node):
        guard=node.get('entry_guard')
        if not guard or node['id'] in self.entry_guard_ready:
            return True
        if self.time_guards.blocked(node):
            # Scheduled time is not an OCR failure and never sleeps through a cooldown.
            self._complete_entry_guard(node,False,skip_now=True)
            from src.rwtd.android_core.canvas_node_flow import skip_reason
            self.reason=skip_reason(node,'時間 Guard 未到期；跳過 '+node['id'])
            return False
        from src.rwtd.android_core.canvas_entry_guard import probe,evaluate,needs_frame,result_allowed
        if not needs_frame(guard):
            self._complete_entry_guard(node,evaluate(guard,self.variables,lambda c:None,lambda c:self.time_guards.allowed(node,c)))
        elif self.source=='simulation':
            from src.rwtd.android_core.canvas_triggers import TriggerTaskScheduler
            helper=TriggerTaskScheduler();helper.observations=self.observations;helper.time_guards=self.time_guards
            self._complete_entry_guard(node,result_allowed(helper.simulation_result({'node':probe(node),'variables':self.variables})))
        else:
            self.pending_effect=probe(node,self.graph['settings'],self.entry_guard_attempts.get(node['id'],0)+1)
        return False

    def tick(self, seconds):
        if not number(seconds, 0, 3600):
            raise ValueError("Invalid scheduler clock")
        if self.status != "RUNNING" or self.pending_effect:
            return
        self.now += seconds
        if not self.owner_chain_timeout:
            current = self.chain_clock() if self.chain_clock else self.now
            if current-self.chain_timeout_started >= self.graph['settings'].get('chain_timeout_minutes',25)*60:
                self.chain_timed_out = True
                self.status, self.outcome = 'STOPPED', 'FAILED'
                self.reason = 'CHAIN 執行時長達上限；失敗退出交接'
                return
        try:
            for n in sorted((n for n in self.graph["nodes"] if n["kind"] == "guard" and n.get("enabled", True)), key=lambda n: (-n.get("priority", 0), n["id"])):
                result = self._guard_value(n)
                if result is False:
                    self.fixed_counts.pop(n['id'],None)
                    self.guard_repeat_due.pop(n['id'],None)
                if result is True and n.get('repeat_runs',1)>1:
                    if self.now<self.guard_repeat_due.get(n['id'],0):
                        continue
                    from src.rwtd.android_core.canvas_repeat import repeat_node
                    if repeat_node(n,self.fixed_counts):
                        self.guard_repeat_due[n['id']]=self.now+self.graph.get('settings',{}).get('round_wait_seconds',1)
                        continue
                    self.guard_repeat_due.pop(n['id'],None)
                if n.get('handler') == 'queue':
                    if result is False:
                        self.trigger_latched.discard(n['id'])
                    elif result is True and n['id'] not in self.trigger_latched:
                        edge = next(e for e in self.graph['edges'] if e['source'] == n['id'] and e['port'] == 'TRIGGER')
                        target = next(node for node in self.graph['nodes'] if node['id'] == edge['target'])
                        module = target['module']
                        if not any(item['module'] == module for item in self.trigger_queue) and not (self.running_trigger and self.running_trigger['module'] == module):
                            if len(self.trigger_queue) >= 100:
                                raise ValueError('Recognition trigger queue capacity reached')
                            self.trigger_queue.append({'guard': n['id'], 'node': target['id'], 'module': module,
                                                       'reason': n.get('feature', n.get('field')), 'simulated': True})
                        self.trigger_latched.add(n['id'])
                    continue  # UNKNOWN never guesses a target or interrupts an active module.
                if result is None or result:
                    self.guard = n["id"]
                    self.reason = "Guard UNKNOWN：" + n["id"] if result is None else "Guard 觸發：" + n["id"]
                    self.status = "BLOCKED" if result is None else {"block": "BLOCKED", "pause": "PAUSED", "manual": "WAITING_MANUAL", "stop": "STOPPED"}[n.get("handler", "block")]
                    self.waiting_kind = "guard" if self.status == "WAITING_MANUAL" else None
                    self.edge = next((e["id"] for e in self.graph["edges"] if e["source"] == n["id"]), None)
                    return  # Guard TRIGGER is a handler route, never a blind module invocation.
            self.guard = None
            if self.now < self.due:
                return
            if self.condition_probe:
                producer=self.condition_probe
                if self.source=='simulation':
                    from src.rwtd.android_core.canvas_ocr import simulation_sample
                    self.pending_effect=copy.deepcopy(producer)
                    self.complete_effect(producer,simulation_sample(producer,self.observations,self.graph))
                else:
                    self.pending_effect=copy.deepcopy(producer)
                return
            if self.running_trigger:
                self.completed.append(self.running_trigger['node'])
                self.running_trigger = None
            if self.trigger_queue:
                item = self.trigger_queue.pop(0)
                self.running_trigger = item
                self.current_node = item['node']
                self.trace.append(dict(item, kind='module', device_inputs=0, trigger=True))
                self.reason = 'Queued trigger · ' + item['module'] + ' · SIMULATED'
                self.due = self.now + max(.3, self.modules[item['module']].get('simulation_seconds', 1))
                return
            if self.pending_next is not None:
                self.cursor, self.pending_next = self.pending_next, None
            # Reject a repeated node entry before any OCR capture for it.
            n = next(n for n in self.graph['nodes'] if n['id'] == self.cursor)
            if self.entry_pending:
                maximum = self.graph.get('settings', {}).get('max_cycles', 10)
                executed = self.entry_visits.get(self.cursor, 0)
                if maximum is not None and executed >= maximum:
                    # An entry Guard may skip this visit without executing it;
                    # preserve that outcome before applying the count limit.
                    if not self._check_entry_guard(n):
                        return
                    source = next((e['source'] for e in self.graph['edges'] if e['id'] == self.edge), None)
                    self.status, self.outcome = 'STOPPED', 'FAILED'
                    self.reason = f'節點 {n["id"]} 實際執行 {executed}/{maximum}；再次執行被上限拒絕'
                    self.current_node = n['id']
                    self.trace.append({'node': n['id'], 'kind': n['kind'], 'source_node': source,
                                       'limit_rejected': True, 'executed_count': executed,
                                       'execution_limit': maximum})
                    self._exhaustion(n, executed, maximum, self.reason, kind='node_limit')
                    return
            exit_sample = None
            if ("exit_condition" in self.graph.get("settings", {})
                    and n['kind'] != 'end'):
                rule = self.graph['settings']['exit_condition']
                if (rule['field'] == 'progress_km' and self.source != 'simulation'
                        and self.exit_progress_reading is None):
                    self.pending_effect = {'id': self.cursor, 'kind': 'ocr', 'name': 'PROGRESS KM',
                        'output_key': 'ProgressKM', 'roi': PROGRESS_OCR_ROI[:],
                        'threshold': PROGRESS_OCR_THRESHOLD, 'ocr_mode': 'raw',
                        'ocr_output_type': 'number', '_progress_for': '__exit__'}
                    return
                observations = self.observations
                if self.exit_progress_reading is not None:
                    sample = self.exit_progress_reading
                    self.exit_progress_reading = None
                    exit_sample = sample
                    observations = dict(observations)
                    value = sample.get('value')
                    observations['progress_km'] = value if sample.get('verified') is True and number(value, 0, 1e15) else None
                result = condition(rule, observations)
                if result is None:
                    if rule['field'] == 'progress_km' and self.source != 'simulation':
                        attempt = self.exit_unmet_attempts.get(self.cursor, 0) + 1
                        self.exit_unmet_attempts[self.cursor] = attempt
                        # This optional exit observer cannot gate node execution.
                        # Missing PROGRESS leaves the independent count exit active.
                    else:
                        self.status, self.reason = 'BLOCKED', '循環退出條件 UNKNOWN'
                        return
                else:
                    self.exit_unmet_attempts.pop(self.cursor, None)
                if result is True and not n.get('progress_exit_on_limit'):
                    self.status, self.outcome, self.reason = "STOPPED", "SKIPPED", "循環退出條件成立；尚未到結束節點"
                    return
            self.current_node = self.cursor
            if not self._check_entry_guard(n):
                return
            if (exit_sample is not None and n['kind'] == 'condition'
                    and n.get('field') == 'progress_km' and not n.get('variable')):
                self.progress_reading = exit_sample
            if (n['kind'] == 'condition' and n.get('field') == 'progress_km'
                    and not n.get('variable') and self.source != 'simulation'
                    and self.progress_reading is None):
                # A source condition reads PROGRESS on entry. Never reuse a
                # numeric result from an earlier visit or a different frame.
                self.pending_effect = {'id': n['id'], 'kind': 'ocr', 'name': 'PROGRESS KM',
                    'output_key': 'ProgressKM', 'roi': PROGRESS_OCR_ROI[:],
                    'threshold': PROGRESS_OCR_THRESHOLD, 'ocr_mode': 'raw',
                    'ocr_output_type': 'number', '_progress_for': n['id']}
                return
            if self.entry_pending:
                self.entry_visits[self.cursor]=self.entry_visits.get(self.cursor,0)+1
                self.entry_pending=False
            self.visits[self.cursor] = self.visits.get(self.cursor, 0) + 1
            if sum(self.visits.values()) > 10000:
                self.status, self.reason = "PAUSED", "Simulation 安全步數上限；請停止或檢查流程"
                return
            self.trace.append({"node": self.cursor, "kind": n["kind"], "simulated": self.source == 'simulation', "device_inputs": 0})
            self.trace = self.trace[-200:]
            if n['kind'] in {'wait','condition','exclusion','manual'}:
                from .flow_key import participate
                participate(self.flow_key,n['id'],{'node_executed':True},kind=n['kind'])
            self.reason = n.get("name", n["id"]) + " · SIMULATED"
            if n["kind"] == "end":
                if self._repeat_node(n):
                    return
                self.time_guards.complete(n)
                self.completed.append(self.cursor)
                start = next(p for p in self.graph['nodes'] if p['id'] == self.graph['entry'])
                self.chain_completed_runs += 1
                if start['kind'] == 'start' and self.chain_completed_runs < start.get('repeat_runs', 1):
                    self.chain_timeout_started = self.chain_clock() if self.chain_clock else self.now
                    self.cursor = start['id']
                    self.entry_visits.clear()
                    self.entry_pending = True
                    self.pending_next = None
                    self.fixed_counts.clear()
                    self.unmet_attempts.clear()
                    self.exit_unmet_attempts.clear()
                    self.ocr_attempts.clear()
                    self.entry_guard_ready.clear()
                    self.entry_guard_attempts.clear()
                    self.variables.clear()
                    self.condition_probe = self.action_plan = self.action_result = None
                    self.due = self.now + self.graph['settings'].get('round_wait_seconds', 1)
                    self.reason = f"整條流程固定執行 · {self.chain_completed_runs}/{start['repeat_runs']} 已完成"
                    return
                from .flow_key import has_execution
                if start['kind']=='start' and self.flow_key['start']==start['id'] and has_execution(self.flow_key):
                    self.time_guards.complete(start)
                from .flow_key import finish_key
                self.completion=finish_key(self.flow_key,n['id'])
                self.status, self.outcome = "STOPPED", "SUCCESS"
            elif n["kind"] == "manual":
                self.status, self.waiting_kind = "WAITING_MANUAL", "node"
            elif n['kind'] == 'exclusion':
                if n.get('exclusion_scope') == 'next':
                    if n.get('exclusion_mode', 'coco') == 'roi':
                        from .canvas_exclusion import remember
                        remember(self, n, [{'bbox': list(n['roi']), 'feature': 'ROI'}])
                        self._next('NEXT')
                    elif self.source != 'simulation':
                        self.pending_effect = copy.deepcopy(n)
                    else:
                        from .canvas_exclusion import remember
                        if n.get('exclusion_mode', 'coco') in {'color', 'coco_or_color'}:
                            self.status, self.reason = 'BLOCKED', '顏色禁區需實際畫面，模擬不推測'
                            return
                        sample = self.observations.get('features', {}).get(n['feature'])
                        if not isinstance(sample, dict) or sample.get('verified') is not True:
                            self.status, self.reason = 'BLOCKED', '禁止點擊區域 UNKNOWN'
                            return
                        regions = []
                        if sample.get('detected') is True:
                            if not number(sample.get('confidence'), n.get('threshold', .8), 1):
                                self.status, self.reason = 'BLOCKED', '禁止點擊區域信心值無效'
                                return
                            regions = [{'bbox': sample.get('bbox'), 'feature': n['feature']}]
                        elif sample.get('detected') is not False:
                            self.status, self.reason = 'BLOCKED', '禁止點擊區域 UNKNOWN'
                            return
                        remember(self, n, regions)
                        self._next('NEXT')
                    return
                if self.source != 'simulation' and n['id'] not in self.exclusion_regions:
                    self.pending_effect = copy.deepcopy(n)
                    return
                if n['id'] not in self.exclusion_regions:
                    sample = self.observations.get('features', {}).get(n['feature'])
                    if isinstance(sample,dict) and sample.get('verified') is True and sample.get('detected') is False:
                        if self._retry_unmet(n):
                            return
                        self.status,self.outcome='STOPPED','NO_MATCH'
                        return
                    if (not isinstance(sample, dict) or sample.get('verified') is not True
                            or sample.get('detected') is not True or not number(sample.get('confidence'), n.get('threshold', .8), 1)):
                        self.status, self.reason = 'BLOCKED', '禁止點擊區域 UNKNOWN：' + n['feature']
                        return
                    from src.rwtd.android_core.workflow_model import validate_roi
                    box = validate_roi(sample.get('bbox'))
                    self.exclusion_regions[n['id']] = {'feature':n['feature'], 'bbox':list(box),
                        'confidence':sample['confidence'], 'simulated':True, 'device_inputs':0}
                self.trace[-1]['exclusion'] = copy.deepcopy(self.exclusion_regions[n['id']])
                self._next('NEXT')
            elif n['kind'] == 'ocr_decision':
                if self.source == 'simulation':
                    from .canvas_ocr_decision import probe
                    from .canvas_ocr import simulation_sample
                    self.pending_effect = copy.deepcopy(n)
                    self.complete_effect(n, simulation_sample(probe(n), self.observations, self.graph))
                else:
                    self.pending_effect = copy.deepcopy(n)
            elif n['kind'] == 'coco':
                if self.source == 'simulation':
                    from src.rwtd.android_core.canvas_triggers import TriggerTaskScheduler
                    helper = TriggerTaskScheduler()
                    helper.observations = self.observations
                    self.complete_effect(n,helper.simulation_result({'node':n}))
                else:
                    self.pending_effect = copy.deepcopy(n)
            elif n['kind'] == 'ocr':
                self.variables[n['output_key']] = {'value':None, 'verified':False, 'reason':'Await OCR'}
                if self.source == 'simulation':
                    from src.rwtd.android_core.canvas_ocr import simulation_sample
                    self.complete_effect(n,simulation_sample(n,self.observations,self.graph))
                else:
                    self.pending_effect = copy.deepcopy(n)
            elif n['kind'] == 'notify':
                if n.get('variable') and self.variables.get(n['variable'], {}).get('verified') is not True:
                    self._block_node(n,'通知變數 UNKNOWN；不發送')
                elif self.source == 'simulation':
                    self.complete_effect(n, {'status':'SIMULATED', 'delivery_verified':False})
                else:
                    self.pending_effect = copy.deepcopy(n)
            elif n['kind'] == 'action':
                if self.source=='simulation' and self.simulation_action_rounds.get(n['id']):
                    self.pending_effect=copy.deepcopy(n)
                    return
                from src.rwtd.android_core.canvas_action import bind_action
                try:
                    self.action_plan = bind_action(n,self.variables,self.modules,self.graph.get('settings',{}).get('feature_rois',{}))
                except ValueError as exc:
                    self.action_plan, self.action_result = None, None
                    self._block_node(n,str(exc))
                    return
                self.action_result = None
                self.status, self.waiting_kind = 'WAITING_MANUAL', 'action_execute'
                self.reason = ('待執行一次：'+n['module']+' / '+n['step_id']+
                               (' · 直接引用步驟' if n.get('input_source') == 'step' else
                                ' → '+' | '.join(self.action_plan['binding']['features'])))
            elif n["kind"] == "condition":
                observations = self.observations
                if self.progress_reading is not None and not n.get('variable'):
                    sample = self.progress_reading
                    self.progress_reading = None
                    self.trace[-1]['progress_ocr'] = copy.deepcopy(sample)
                    observations = dict(observations)
                    value = sample.get('value')
                    observations['progress_km'] = value if sample.get('verified') is True and number(value, 0, 1e15) else None
                result = condition(n, observations, self.variables)
                if n.get('progress_exit_on_limit'):
                    maximum = self.graph['settings'].get('max_cycles', 10)
                    executed = self.entry_visits.get(n['id'], 0)
                    count_exit = maximum is not None and executed >= maximum
                    self.trace[-1]['progress_exit'] = {
                        'progress_matched': result, 'executed_count': executed,
                        'execution_limit': maximum, 'count_exit': count_exit}
                    self.reason = ('PROGRESS 達標' if result is True else
                                   'PROGRESS 累計次數達標' if count_exit else
                                   'PROGRESS UNKNOWN；沿 NO 繼續' if result is None else
                                   'PROGRESS 未達標；沿 NO 繼續')
                    self._next('YES' if result is True or count_exit else 'NO',
                               allow_fixed=False, mark_completed=result is True or count_exit)
                    return
                if result is None and self._retry_unmet(n, unknown=bool(n.get('variable'))):
                    if n.get('variable') and any(p.get('output_key')==n['variable'] and p['id'] in getattr(self,'refreshable_nodes',{q['id'] for q in self.graph['nodes']}) for p in self.graph['nodes']):
                        self.condition_probe=copy.deepcopy(next(p for p in self.graph['nodes']
                            if p.get('kind')=='ocr' and p.get('output_key')==n['variable']))
                    return
                if result is None:
                    self.status, self.reason = "BLOCKED", "條件 UNKNOWN；不選擇分支"
                else:
                    self.reason += " → " + ("YES" if result else "NO")
                    self._next("YES" if result else "NO",allow_fixed=result is True,
                               mark_completed=result is True)
            else:
                if n["kind"] == "module":
                    if self.unattended:
                        self.pending_effect = copy.deepcopy(n)
                        return
                    self.trace[-1]["module"] = n["module"]  # Reference only; never executes gameplay.
                self._next("NEXT")
                duration = n['seconds'] if n['kind'] == 'wait' else max(.3, self.modules[n['module']].get('simulation_seconds', .3)) if n['kind'] == 'module' else .3
                self.due = max(self.due, self.now + duration)
        except (ValueError, KeyError, StopIteration, TypeError) as exc:
            self.status, self.outcome, self.reason = "STOPPED", "FAILED", str(exc)

    def complete_effect(self, node, result):
        # A stopped/paused run cannot consume a late capture or delivery result.
        if (self.status == 'RUNNING' and node.get('_progress_for') == '__exit__'
                and self.graph.get('settings', {}).get('exit_condition', {}).get('field') == 'progress_km'
                and self.pending_effect == node):
            from src.rwtd.android_core.canvas_ocr import classify
            self.exit_progress_reading = classify(node, result)
            self.pending_effect = None
            return
        if (self.status == 'RUNNING' and node.get('_progress_for') == self.cursor
                and self.pending_effect == node):
            from src.rwtd.android_core.canvas_ocr import classify
            self.progress_reading = classify(node, result)
            self.pending_effect = None
            return
        if (self.status=='RUNNING' and self.condition_probe==node and self.pending_effect==node):
            from src.rwtd.android_core.canvas_ocr import classify
            self.variables[node['output_key']]=classify(node,result)
            self.pending_effect=None
            self.condition_probe=None
            return
        if self.status != 'RUNNING' or self.cursor != node['id']:
            return
        if self.source!='simulation' and self.pending_effect!=node:
            return  # A consumed BLOCK/effect cannot be applied twice on a self NEXT.
        if node.get('_entry_guard_for') and self.pending_effect!=node:
            return
        if not node.get('_entry_guard_for'):
            from .flow_key import participate
            record=dict(result or {})
            if node['kind'] in {'coco','ocr','ocr_decision','notify'} and (record.get('verified') is True or record.get('status') in {'SIMULATED','local_only','native_submitted','delivered'}):
                record['node_executed']=True
            participate(self.flow_key,node['id'],record,kind=node['kind'])
        self.pending_effect = None
        if node['kind'] == 'ocr_decision':
            from .canvas_ocr_decision import evaluate
            decision, reading = evaluate(node, result)
            self.trace[-1]['ocr_decision'] = reading
            if decision is None:
                if not self._retry_unmet(node, unknown=True):
                    self.status, self.reason = 'BLOCKED', 'OCR 決策 UNKNOWN；不選擇分支'
                return
            self._next('YES' if decision else 'NO', allow_fixed=decision, mark_completed=decision)
            return
        if result and result.get('preempted') and node['kind'] in {'module','action'}:
            count=result.get('device_inputs',0)
            self.device_inputs=None if count is None or self.device_inputs is None else self.device_inputs+count
            self.trace[-1]['preempted']=True
            self._skip_node(node,'插隊取消目前節點的所有剩餘動作')
            return
        if node.get('_entry_guard_for'):
            from src.rwtd.android_core.canvas_entry_guard import result_allowed
            target=next(n for n in self.graph['nodes'] if n['id']==node['_entry_guard_for'])
            self._complete_entry_guard(target,result_allowed(result))
            return
        if node['kind'] == 'exclusion':
            from src.rwtd.android_core.workflow_model import validate_roi
            if node.get('exclusion_scope') == 'next':
                if (not isinstance(result, dict) or result.get('verified') is not True
                        or not result.get('frame_id') or not isinstance(result.get('regions'), list)):
                    self.status, self.reason = 'BLOCKED', '禁止點擊區域辨識無效；不送出 NEXT 操作'
                    return
                from .canvas_exclusion import remember
                remember(self, node, result['regions'])
                self._next('NEXT')
                return
            if (isinstance(result,dict) and result.get('verified') is True and result.get('detected') is False):
                if self._retry_unmet(node):
                    return
                self.status,self.outcome='STOPPED','NO_MATCH'
                return
            if (not isinstance(result,dict) or result.get('verified') is not True
                    or result.get('detected') is not True or not result.get('frame_id')
                    or not number(result.get('confidence'),node.get('threshold',.8),1)):
                self.status, self.reason = 'BLOCKED', '禁止點擊區域未辨識：'+node['feature']
                return
            try:
                box = validate_roi(result.get('bbox'))
            except ValueError:
                self.status, self.reason = 'BLOCKED', '禁止點擊區域座標無效：'+node['feature']
                return
            self.exclusion_regions[node['id']] = {'feature':node['feature'],'bbox':list(box),
                'confidence':result['confidence'],'frame_id':result['frame_id'],'simulated':False,'device_inputs':0}
            self.trace[-1]['exclusion'] = copy.deepcopy(self.exclusion_regions[node['id']])
            self._next('NEXT')
            return
        if node['kind'] == 'ocr':
            from src.rwtd.android_core.canvas_ocr import decide
            attempt = self.ocr_attempts.get(node['id'],0)+1
            self.ocr_attempts[node['id']] = attempt
            sample, route = decide(node,result,attempt)
            self.variables[node['output_key']] = sample
            self.trace[-1]['ocr'] = copy.deepcopy(sample)
            self.trace[-1]['ocr_attempt'] = attempt
            if (route == 'UNKNOWN' and node.get('unknown_no_retry') is True
                    and (node.get('ocr_mode')=='raw' or sample.get('state')!='no_match')):
                self.status, self.reason = 'BLOCKED', sample['reason']+'；UNKNOWN 不重試'
                return
            if sample.get('state')=='no_match' and 'ocr_retry' not in node:
                if self._retry_unmet(node):
                    self.reason='OCR '+node['output_key']+'：'+sample['reason']+'；重試 '+str(attempt)+'/'+str(self.graph['settings'].get('max_cycles') or '不限')
                    return
                if route=='UNKNOWN':
                    self.status,self.outcome='STOPPED','NO_MATCH'
                    self.reason='OCR '+node['output_key']+'：'+sample['reason']+'；重試耗盡 '+str(attempt)
                    return
            if route == 'RETRY':
                self.due = self.now+node['ocr_retry'].get('interval_seconds',1)
                self.reason = sample['reason']+' · '+str(attempt)+'/'+str(node['ocr_retry']['max_attempts'])
                return
            if route == 'UNKNOWN':
                self.status, self.reason = 'BLOCKED', sample['reason'] + '：' + node['id']
                return
            if (route=='NO' and sample.get('state')=='no_match' and 'ocr_retry' in node
                    and attempt>=node['ocr_retry'].get('max_attempts',1)):
                self._exhaustion(node,attempt,node['ocr_retry'].get('max_attempts',1),sample['reason'],'ocr')
            self.reason = node['output_key'] + ' = ' + (str(sample['value']) if sample['value'] is not None else '未命中')
            self._next(route,allow_fixed=route!='NO',mark_completed=route!='NO')
            return
        elif node['kind'] == 'coco':
            if not isinstance(result,dict) or result.get('verified') is not True or not result.get('frame_id') or type(result.get('detected')) is not bool:
                if self._retry_unmet(node, unknown=True):
                    self.reason = 'COCO UNKNOWN；重新擷取 · '+str(self.unmet_attempts[node['id']])+'/'+str(self.graph['settings'].get('max_cycles') or '不限')
                    return
                self.status, self.reason = 'BLOCKED', ('COCO decision UNKNOWN；不重試，不選擇分支'
                    if node.get('unknown_no_retry') is True else 'COCO decision UNKNOWN；重試次數用完')
                return
            self.trace[-1]['recognition'] = copy.deepcopy(result)
            self._next('YES' if result['detected'] else 'NO',allow_fixed=result['detected'],
                       mark_completed=result['detected'])
            return
        elif node['kind'] == 'module' and self.unattended:
            count = (result or {}).get('device_inputs', 0)
            self.device_inputs = None if count is None or self.device_inputs is None else self.device_inputs+count
            if result and result.get('target_skipped') is True:
                self._skip_node(node,'COCO 操作目標未命中；已跳過動作')
                return
            if result and result.get('recognition_retry_exhausted') is True:
                self.status,self.outcome,self.reason='STOPPED','NO_MATCH','模組未找到目標；嘗試次數用完，結束流程'
                return
            if result and result.get('guard_skipped') is True:
                self._skip_node(node,'模組附加 Guard 未通過；跳過剩餘輪次')
                return
            if result and result.get('action_completed') and result.get('status') in {'SIMULATED','INPUT_SENT_UNVERIFIED','SUCCESS'}:
                self._next('NEXT', allow_fixed=False)
            else:
                reason=(result or {}).get('failure_reason') or '模組未完成'
                if (result or {}).get('status') in {'BLOCK','BLOCKED'}:
                    self._block_node(node,reason)
                else:self.status,self.reason='BLOCKED',reason
            return
        elif node['kind'] == 'action':
            self.action_result = copy.deepcopy(result)
            count = (result or {}).get('device_inputs',0)
            self.device_inputs = None if count is None or self.device_inputs is None else self.device_inputs+count
            if result and result.get('target_skipped') is True:
                self._skip_node(node,'COCO 操作目標未命中；已跳過動作')
                return
            if result and result.get('guard_skipped') is True:
                self._skip_node(node,'附加 Guard 未通過；跳過剩餘輪次')
                self.trace[-1]['action']={'status':'SKIPPED','device_inputs':count}
                return
            if result and result.get('recognition_retry_exhausted') is True:
                self.status,self.outcome,self.reason='STOPPED','NO_MATCH','未找到目標；嘗試次數用完，結束流程'
                return
            if not result or result.get('status') not in {'INPUT_SENT_UNVERIFIED','SIMULATED'} or not result.get('action_completed'):
                reason=(result or {}).get('failure_reason') or '操作未完整完成；查看證據'
                if (result or {}).get('status') in {'BLOCK','BLOCKED'}:
                    self._block_node(node,reason)
                else:self.status,self.reason='BLOCKED',reason
                return
            if self.source=='simulation' and node.get('entry_guard'):
                rounds=self.simulation_action_rounds.get(node['id'],0)+1
                if rounds<node.get('repeat_runs',1):
                    self.simulation_action_rounds[node['id']]=rounds
                    self.entry_guard_ready.discard(node['id'])
                    self.due=self.now+self.graph['settings'].get('round_wait_seconds',1)
                    self.waiting_kind=None
                    self.reason='模擬操作輪次完成；等待下輪 Guard'
                    return
                self.simulation_action_rounds.pop(node['id'],None)
            self.trace[-1]['action'] = {key:copy.deepcopy(result.get(key)) for key in
                                       ('report_id','status','device_inputs','canvas_binding','binding') if key in result}
            if self.unattended:
                self._next('NEXT', allow_fixed=False)
                self.waiting_kind = None
                self.reason = '輸入已送出；依巨集流程繼續，未宣稱結果驗證成功'
                return
            self.status, self.waiting_kind, self.reason = 'WAITING_MANUAL', 'action_verify', 'INPUT_SENT_UNVERIFIED；請人工 Pass／Fail'
            return
        else:
            record = dict(result or {'status':'FAILED'}, node=node['id'])
            if record.get('status') == 'SIMULATED':
                sample = self.variables.get(node.get('variable'), {})
                record['description'] = node['message'].replace('{value}', str(sample['value']) if sample.get('value') is not None else '')
            self.notifications.append(record)
            self.notifications = self.notifications[-100:]
            self.trace[-1]['notification'] = copy.deepcopy(record)
            if record['status'] not in {'SIMULATED','local_only','native_submitted','delivered','duplicate_suppressed','cooldown_suppressed'}:
                if record['status'] in {'BLOCK','BLOCKED'}:
                    self._block_node(node,'通知未完成：' + record['status'])
                else:self.status,self.reason='BLOCKED','通知未完成：'+record['status']
                return
            self.reason = '通知：' + record['status']
        self._next('NEXT')

    def snapshot(self):
        return {"status": self.status, "locked": self.locked, "outcome": self.outcome, "reason": self.reason,
                "current_node": self.current_node, "next_node": self.pending_next or self.cursor, "active_edge": self.edge, "guard": self.guard,
                "completed": list(dict.fromkeys(self.completed)), "trace": copy.deepcopy(self.trace),
                "trigger_queue": copy.deepcopy(self.trigger_queue), "running_trigger": copy.deepcopy(self.running_trigger),
                "exclusion_regions": copy.deepcopy(self.exclusion_regions),
                "variables":copy.deepcopy(self.variables), "notifications":copy.deepcopy(self.notifications),
                "ocr_attempts":copy.deepcopy(self.ocr_attempts),
                "condition_unknown_attempts":copy.deepcopy(self.unmet_attempts),
                "exit_unknown_attempts":copy.deepcopy(self.exit_unmet_attempts),
                "fixed_counts":copy.deepcopy(self.fixed_counts),
                "chain_completed_runs":self.chain_completed_runs,
                "chain_timed_out":self.chain_timed_out,
                "chain_timeout_minutes":self.graph.get('settings',{}).get('chain_timeout_minutes',25) if self.graph else 25,
                "entry_visits":copy.deepcopy(self.entry_visits),
                "block_counts":dict(self.block_counts),
                "flow_key":copy.deepcopy(self.flow_key),"completion":copy.deepcopy(self.completion),
                "source":self.source, "notify_discord":self.notify_discord,
                "waiting_kind":self.waiting_kind, "action_plan":copy.deepcopy(self.action_plan), "action_result":copy.deepcopy(self.action_result),
                "visits": dict(self.visits), "device_inputs": self.device_inputs, "simulated": self.source == 'simulation'}
