"""Allowlisted decision data and bounded simulation. No device or input APIs."""
import hashlib
import json
import math
import uuid
from copy import deepcopy
from threading import RLock
from src.rwtd.android_core.module_store import safe_path, atomic_json, identifier, file_signature

FIELDS = {"progress_km", "food_count", "teleport_distance_km", "exception", "stage_complete", "store_reward"}
OPS = {"eq", "gte", "lte"}


def validate_graph(value, module_ids):
    if isinstance(value, dict) and value.get("schema_version") == 2:
        from src.rwtd.android_core.canvas_scheduler import validate_canvas
        return validate_canvas(value, module_ids)
    if not isinstance(value, dict) or set(value) - {"schema_version", "entry", "nodes", "revision"} or value.get("schema_version") != 1:
        raise ValueError("Invalid simulation decision graph")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 100:
        raise ValueError("Graph requires 1..100 nodes")
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"id", "kind", "x", "y", "module", "field", "op", "value", "next", "yes", "no"}:
            raise ValueError("Unknown node fields")
        identifier(node.get("id"))
        if node["id"] in ids or node.get("kind") not in {"condition", "module", "end"}:
            raise ValueError("Invalid node identity/type")
        ids.add(node["id"])
        for axis in ("x", "y"):
            n = node.get(axis, 0)
            if type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1000:
                raise ValueError("Invalid graph node position")
        if node["kind"] == "module" and node.get("module") not in module_ids:
            raise ValueError("Graph references a missing module")
        if node["kind"] == "condition":
            if node.get("field") not in FIELDS or node.get("op") not in OPS:
                raise ValueError("Unknown simulation condition")
            v = node.get("value")
            if type(v) not in (int, float, bool) or (type(v) is not bool and (not math.isfinite(v) or v < 0)):
                raise ValueError("Invalid simulation condition value")
    if value.get("entry") not in ids:
        raise ValueError("Missing graph entry")
    for node in nodes:
        keys = ("yes", "no") if node["kind"] == "condition" else ("next",) if node["kind"] == "module" else ()
        if any(node.get(key) not in ids for key in keys):
            raise ValueError("Invalid graph edge")
    result = {key: v for key, v in value.items() if key != "revision"}
    result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return result


def simulate_graph(graph, observations, module_ids, limit=100):
    if isinstance(graph, dict) and graph.get("schema_version") == 2:
        from src.rwtd.android_core.canvas_scheduler import CanvasRuntime
        runtime = CanvasRuntime()
        runtime.start(graph, observations, module_ids if isinstance(module_ids, dict) else {key: {} for key in module_ids})
        for _ in range(limit):
            runtime.tick(1)
            if runtime.status != "RUNNING":
                break
        if runtime.status == "RUNNING":
            runtime.control("pause")
        return runtime.snapshot()
    graph = validate_graph(graph, module_ids)
    if not isinstance(observations, dict) or set(observations) - FIELDS:
        raise ValueError("Unknown simulated observation")
    for field, value in observations.items():
        if field in {"exception", "stage_complete", "store_reward"}:
            if type(value) is not bool:
                raise ValueError("Simulation flags require boolean")
        elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Simulation counts require finite nonnegative values")
    nodes = {node["id"]: node for node in graph["nodes"]}
    cursor, trace = graph["entry"], []
    for _ in range(limit):
        node = nodes[cursor]
        item = {"node": cursor, "kind": node["kind"], "simulated": True, "device_inputs": 0}
        trace.append(item)
        if node["kind"] == "end":
            return {"status": "IDLE", "simulated": True, "trace": trace, "device_inputs": 0}
        if node["kind"] == "module":
            item["module"] = node["module"]  # Plan only: never calls ManualDeviceTests.
            cursor = node["next"]
        else:
            if node["field"] not in observations:
                return {"status": "ERROR", "reason": "Missing simulated observation", "trace": trace, "device_inputs": 0}
            actual, expected = observations[node["field"]], node["value"]
            if type(actual) is bool and (node["op"] != "eq" or type(expected) is not bool):
                raise ValueError("Boolean conditions use eq with a boolean")
            if type(actual) is not bool and type(expected) is bool:
                raise ValueError("Numeric conditions require a numeric value")
            outcome = actual == expected if node["op"] == "eq" else actual >= expected if node["op"] == "gte" else actual <= expected
            item["condition"] = outcome
            cursor = node["yes" if outcome else "no"]
    return {"status": "PAUSED", "reason": "Simulation step budget reached", "trace": trace, "device_inputs": 0}


from src.rwtd.android_core.execution_lease import definition_edit


class DecisionGraphStore:
    def __init__(self, profile, module_store, canvas_id=None):
        self.profile, self.modules = profile, module_store
        self.canvas_id=canvas_id
        self.execution_locked = False
        self.input_locked = None  # None retains legacy execution-lock behavior.
        self._read_lock = RLock()
        self._registry_cache = None
        self._graph_cache = None

    def path(self, relative):
        if relative == 'decision.json' and self.active_id() != 'default':
            relative='canvases/'+identifier(self.active_id())+'.json'
        return safe_path(self.profile.project_root, self.profile.data_root + "/scheduler/" + relative)

    def registry_path(self):
        return safe_path(self.profile.project_root,self.profile.data_root+'/scheduler/canvases.json')

    def registry(self):
        with self._read_lock:
            return self._registry()

    def _registry(self):
        path=self.registry_path()
        signature=file_signature(path) if path.exists() else None
        if signature is not None and self._registry_cache and self._registry_cache[0]==signature:
            return deepcopy(self._registry_cache[1])
        value=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'schema_version':1,'active':'default','entries':{'default':{'name':'預設 Canvas'}}}
        if not isinstance(value,dict) or value.get('schema_version')!=1 or not isinstance(value.get('entries'),dict) or value.get('active') not in value['entries']:
            raise ValueError('Invalid Canvas file catalog')
        for canvas_id,item in value['entries'].items():
            identifier(canvas_id)
            if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not 1<=len(item['name'])<=80:
                raise ValueError('Invalid Canvas name')
        if signature is not None and file_signature(path)==signature:
            self._registry_cache=(signature,deepcopy(value))
        else:
            self._registry_cache=None
        return value

    def active_id(self):
        registry=self.registry()
        canvas_id=self.canvas_id or registry['active']
        if canvas_id not in registry['entries']:raise ValueError('Unknown Canvas file')
        return canvas_id

    def catalog(self):
        registry=self.registry()
        active=self.canvas_id or registry['active']
        if active not in registry['entries']:raise ValueError('Unknown Canvas file')
        return [dict(id=canvas_id,name=item['name'],selected=canvas_id==active) for canvas_id,item in registry['entries'].items()]

    def _editable(self):
        if self.execution_locked:raise ValueError('Stop all Canvas/Trigger execution before switching or saving a Canvas file')

    @definition_edit
    def select(self,canvas_id):
        self._editable();registry=self.registry()
        if canvas_id not in registry['entries']:raise ValueError('Unknown Canvas file')
        candidate=DecisionGraphStore(self.profile,self.modules,canvas_id)
        graph, _ = candidate.load_for_edit()  # Broken Step references remain editable, never executable.
        registry['active']=canvas_id;atomic_json(self.registry_path(),registry)
        self.canvas_id=None
        return graph

    @definition_edit
    def save_as(self,value,name):
        self._editable();registry=self.registry();self._name(name,registry)
        if len(registry['entries'])>=1000:raise ValueError('Canvas file limit reached (1000)')
        graph=validate_graph(value,{m['id']:m for m in self.modules.catalog()})
        canvas_id='Canvas_'+uuid.uuid4().hex
        path=safe_path(self.profile.project_root,self.profile.data_root+'/scheduler/canvases/'+canvas_id+'.json')
        atomic_json(path,graph)
        registry['entries'][canvas_id]={'name':name.strip()};registry['active']=canvas_id
        atomic_json(self.registry_path(),registry);self.canvas_id=None
        return graph

    @definition_edit
    def rename(self,name):
        self._editable();registry=self.registry();canvas_id=self.active_id()
        self._name(name,registry,canvas_id)
        registry['entries'][canvas_id]['name']=name.strip();atomic_json(self.registry_path(),registry)
        return self.load()

    @staticmethod
    def _name(name,registry,current=None):
        if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('Canvas name requires 1..80 characters')
        if any(item['name'].casefold()==name.strip().casefold() for key,item in registry['entries'].items() if key!=current):
            raise ValueError('Canvas name already exists')

    def load(self, force_read=False):
        with self._read_lock:
            path = self.path("decision.json")
            if not path.exists():
                self._graph_cache=None
                return None
            modules={m["id"]:m for m in self.modules.catalog()}
            signature=file_signature(path)
            key=(path,signature,tuple((m['id'],m.get('revision'),m.get('kind')) for m in modules.values()))
            if not force_read and self._graph_cache and self._graph_cache[0]==key:
                return deepcopy(self._graph_cache[1])
            graph=validate_graph(json.loads(path.read_text(encoding="utf-8-sig")),modules)
            if file_signature(path)==signature:
                self._graph_cache=(key,deepcopy(graph))
            return graph

    def load_for_edit(self):
        """Recover an editable graph with stale Step references; execution stays strict."""
        try:
            return self.load(), None
        except ValueError as exc:
            allowed={'Select an existing unique saved Step ID',
                     'Select an existing unique ending Step ID',
                     'Action range end must follow its start in module order'}
            if str(exc) not in allowed:raise
            with self._read_lock:
                raw=json.loads(self.path('decision.json').read_text(encoding='utf-8-sig'))
                modules={m['id']:m for m in self.modules.catalog()}
                graph=validate_graph(deepcopy(raw),set(modules))
                from .canvas_action import validate_action_node
                warnings=[]
                for node in graph['nodes']:
                    if node['kind']!='action':continue
                    try:validate_action_node(node,modules)
                    except ValueError as error:
                        if str(error) not in allowed:raise
                        warnings.append(f"{node['id']}：{node['module']} / Step {node['step_id']}：{error}")
                if not warnings:raise exc
                return graph,'；'.join(warnings)

    @definition_edit
    def save(self, value, expected_revision=None):
        if self.execution_locked:
            raise ValueError("Scheduler active: Canvas is read-only until Stop")
        module_ids = {m["id"]: m for m in self.modules.catalog()}
        graph = validate_graph(value, module_ids)
        path = self.path("decision.json")
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8-sig"))
            # load() returns a normalized graph and its recomputed revision.
            # Compare in that same representation: legacy string OCR values
            # may migrate without any external change to the underlying file.
            try:
                current = validate_graph(deepcopy(previous), module_ids)
            except ValueError:
                # Only the editor's narrowly allowlisted recovery can supply
                # a revision for a broken reference. The new graph above must
                # still pass full validation before any write is attempted.
                current, warning = self.load_for_edit()
                if not warning:raise
            if expected_revision != current["revision"]:
                raise ValueError("Graph changed; reload before saving")
            if graph==current:
                # A repeated Save is already durable; do not create a backup
                # and perform two fsyncs when the complete normalized JSON agrees.
                return current
            atomic_json(self.path("backups/decision_" + uuid.uuid4().hex + ".json"), previous)
        atomic_json(path, graph)
        from .record_retention import enforce
        enforce(self.profile.project_root,'backups',self.profile.data_root+'/scheduler/backups')
        return graph

    def trigger_tasks(self):
        """Start records in decision.json are the canonical JSON TriggerTasks."""
        from src.rwtd.android_core.canvas_triggers import definitions
        graph = self.load()
        return definitions(graph) if graph and graph.get('schema_version') == 2 else []
