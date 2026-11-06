"""Data-only module repository. No code execution or device APIs."""
import hashlib
import json
import os
import re
import stat
import uuid
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from datetime import datetime, timezone
from pathlib import Path
from src.rwtd.android_core.workflow_model import validate_tags, validate_roi, resolve_step

ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")


def safe_path(root, relative):
    root = Path(root).absolute()
    path = root / relative
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Path outside managed project")
    for item in (root, *path.relative_to(root).parents):
        candidate = item if item == root else root / item
        if (candidate.exists() or candidate.is_symlink()) and linked(candidate):
            raise ValueError("Linked directory is forbidden")
    if (path.exists() or path.is_symlink()) and linked(path):
        raise ValueError("Linked file is forbidden")
    return path


def linked(path):
    metadata = Path(path).lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def file_signature(path):
    """Cheap invalidation for immutable, atomically replaced JSON definitions."""
    item = Path(path).stat()
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Module id must use letters, digits, underscore or hyphen")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *("COM" + str(index) for index in range(1, 10)), *("LPT" + str(index) for index in range(1, 10))}:
        raise ValueError("Reserved Windows filename")
    return value


def validate_module(value, step_validator):
    # The imported JSON is data: no imports, eval, exec, shell or callable fields.
    if not isinstance(value, dict) or set(value) - {"schema_version", "id", "name", "version", "steps", "role", "simulation_seconds", "revision", "tags", "global_roi", "dashboard_visible", "coordinate_metadata"}:
        raise ValueError("Unknown module schema field")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or type(value.get("version", 1)) is not int or value.get("version", 1) < 1:
        raise ValueError("Unsupported schema/version")
    identifier(value.get("id"))
    if 'coordinate_metadata' in value:
        validate_coordinate_metadata(value['coordinate_metadata'])
    if 'dashboard_visible' in value and type(value['dashboard_visible']) is not bool:
        raise ValueError('dashboard_visible must be boolean')
    if not isinstance(value.get("name"), str) or not 1 <= len(value["name"]) <= 120:
        raise ValueError("Module name required (1..120 characters)")
    steps = value.get("steps")
    if not isinstance(steps, list) or len(steps) > 100:
        raise ValueError("Module requires a step array (at most 100)")
    ids = set()
    tags = value.get("tags", {})
    validate_tags(tags)
    if "global_roi" in value:
        validate_roi(value["global_roi"])
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), str) or not 1 <= len(step["step_id"]) <= 80 or step["step_id"] in ids:
            raise ValueError("Steps need unique nonempty step_id")
        if "workflow_module" in step or step.get("reversible_ack"):
            raise ValueError("Stored steps cannot authorize device execution")
        step_validator(dict(step, reversible_ack=True))
        resolved = resolve_step(value, step)  # Flat, module-local TAG references must all exist.
        if step.get('color_mode') and not resolved.get('coco_roi'):
            raise ValueError('Color targeting requires a module global, TAG or step ROI')
        ids.add(step["step_id"])
    from .step_control import validate_ranges
    validate_ranges(steps)
    duration = value.get("simulation_seconds", 1)
    if type(duration) not in (int, float) or not 0 <= duration <= 3600:
        raise ValueError("Invalid simulation duration")
    if value.get("role"):
        identifier(value["role"])
    result = dict(value, version=value.get("version", 1))
    result.pop("revision", None)
    result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return result


def validate_coordinate_metadata(metadata):
    """Persist provenance separately from the canonical coordinate system."""
    if (not isinstance(metadata,dict) or set(metadata)-{
            'stored_resolution','last_edit_resolution','corrections'}
            or metadata.get('stored_resolution') != [1080,2400]):
        raise ValueError('Stored module coordinates must use 1080x2400')
    size=metadata.get('last_edit_resolution')
    if size is not None:
        from .screen_geometry import ScreenGeometry
        if not isinstance(size,list) or len(size)!=2:
            raise ValueError('Invalid module edit resolution')
        ScreenGeometry(*size)
    corrections=metadata.get('corrections',[])
    if (not isinstance(corrections,list) or len(corrections)>20
            or any(not isinstance(item,str) or not 1<=len(item)<=80 for item in corrections)):
        raise ValueError('Invalid module coordinate correction history')


from src.rwtd.android_core.execution_lease import definition_edit


class ModuleStore:
    def __init__(self, profile):
        self.profile = profile
        self.root = profile.project_root
        self._catalog_cache = OrderedDict()
        self._catalog_lock = RLock()

    def catalog(self):
        # Each poll still inventories files and checks metadata. Only unchanged
        # JSON parsing/step validation is reused; callers receive isolated data.
        with self._catalog_lock:
            return self._catalog()

    def _catalog(self):
        output = []
        present = set()
        # RwtD's bound step validator depends on the available COCO names.
        # Changes to annotations must invalidate an otherwise unchanged module.
        from .feature_catalog import feature_names
        try:
            feature_signature = frozenset(feature_names(self.root))
        except (ValueError, OSError, TypeError):
            # Preserve each validator's original fail-closed behavior. An
            # unreadable annotation source never authorizes a cached module.
            feature_signature = None
            self._catalog_cache.clear()
        for kind in ("system", "user"):
            directory = safe_path(self.profile.definitions_root, self.profile.modules_root + "/" + kind)
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                if linked(path):
                    continue
                present.add(path)
                try:
                    signature = (file_signature(path), feature_signature)
                    cached = self._catalog_cache.get(path)
                    if feature_signature is not None and cached and cached[0] == signature:
                        self._catalog_cache.move_to_end(path)
                        output.append(deepcopy(cached[1]))
                        continue
                    data = validate_module(json.loads(path.read_text(encoding="utf-8-sig")), self.profile.step_validator)
                    if path.stem != data["id"]:
                        self._catalog_cache.pop(path, None)
                        continue
                    item = dict(data, kind=kind)
                    if feature_signature is not None and file_signature(path) == signature[0]:
                        self._catalog_cache[path] = (signature, item)
                        self._catalog_cache.move_to_end(path)
                    output.append(deepcopy(item))
                except (ValueError, OSError, TypeError):
                    self._catalog_cache.pop(path, None)
                    continue  # Invalid definitions are never executable.
        for path in set(self._catalog_cache) - present:
            self._catalog_cache.pop(path, None)
        while len(self._catalog_cache) > 512:
            self._catalog_cache.popitem(last=False)
        return output

    def get(self, module_id):
        matches = [item for item in self.catalog() if item["id"] == identifier(module_id)]
        if len(matches) != 1:
            raise ValueError("Module missing, invalid, or duplicate id")
        return matches[0]

    @definition_edit
    def save(self, value, expected_revision=None, allow_system_update=False):
        data = validate_module(value, self.profile.step_validator)
        if type(allow_system_update) is not bool:
            raise ValueError("System update authorization must be boolean")
        matches = [item for item in self.catalog() if item["id"].casefold() == data["id"].casefold()]
        if len(matches) > 1 or (matches and matches[0]["id"] != data["id"]):
            raise ValueError("Duplicate or differently cased module id; reload before saving")
        kind = matches[0]["kind"] if matches else "user"
        if kind == "system" and not allow_system_update:
            raise ValueError("Explicit System save or import authorization required")
        path = safe_path(self.profile.definitions_root, f"{self.profile.modules_root}/{kind}/{data['id']}.json")
        if path.exists():
            previous = self.get(data["id"])
            if expected_revision != previous["revision"]:
                raise ValueError("Definition changed; reload before saving")
            # Saving identical execution content is not a new revision/test.
            ignored = {'version', 'revision', 'kind'}
            if ({k:v for k,v in data.items() if k not in ignored} ==
                    {k:v for k,v in previous.items() if k not in ignored}):
                return {k:v for k,v in previous.items() if k != 'kind'}
            backup = safe_path(self.profile.definitions_root, f"{self.profile.modules_root}/{kind}/backups/{data['id']}_{uuid.uuid4().hex}.json")
            atomic_json(backup, {key: val for key, val in previous.items() if key != "kind"})
            data["version"] = previous["version"] + 1
            data = validate_module(data, self.profile.step_validator)
        atomic_json(path, data)
        from .record_retention import enforce
        enforce(self.profile.definitions_root,'backups',f"{self.profile.modules_root}/{kind}/backups")
        return data

    @definition_edit
    def delete(self, module_id, expected_revision):
        previous = self.get(module_id)
        if previous["kind"] != "user" or previous["revision"] != expected_revision:
            raise ValueError("Only current user definitions may be deleted")
        if module_id in self.scheduler().values():
            raise ValueError("Rebind scheduler roles before deleting a referenced module")
        from src.rwtd.android_core.decision_graph import DecisionGraphStore
        graphs = DecisionGraphStore(self.profile, self)
        for item in graphs.catalog():
            graph = DecisionGraphStore(self.profile, self, item['id']).load()
            if graph and any(node.get("module") == module_id for node in graph["nodes"]):
                raise ValueError("Rebind Canvas " + item['name'] + " before deleting a referenced module")
        backup = safe_path(self.profile.definitions_root, f"{self.profile.modules_root}/user/backups/deleted_{module_id}_{uuid.uuid4().hex}.json")
        atomic_json(backup, {key: val for key, val in previous.items() if key != "kind"})
        safe_path(self.profile.definitions_root, f"{self.profile.modules_root}/user/{module_id}.json").unlink()
        from .record_retention import enforce
        enforce(self.profile.definitions_root,'backups',f"{self.profile.modules_root}/user/backups",protected={backup.name})
        return str(backup)

    def scheduler(self):
        path = safe_path(self.profile.definitions_root, self.profile.data_root + "/scheduler/config.json")
        if not path.exists():
            return {item["role"]: item["id"] for item in self.catalog() if item.get("role") and item["kind"] == "system"}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) != {"schema_version", "roles"} or data["schema_version"] != 1 or not isinstance(data["roles"], dict):
            raise ValueError("Invalid scheduler configuration")
        allowed = {item.get("role") for item in self.catalog()} - {"", None}
        if set(data["roles"]) - allowed:
            raise ValueError("Unknown scheduler role")
        for module_id in data["roles"].values():
            self.get(module_id)
        return data["roles"]

    @definition_edit
    def save_scheduler(self, roles):
        if not isinstance(roles, dict):
            raise ValueError("Roles must be a JSON object")
        defaults = {item.get("role") for item in self.catalog()} - {"", None}
        if set(roles) - defaults:
            raise ValueError("Unknown role")
        for module_id in roles.values():
            self.get(module_id)
        path = safe_path(self.profile.definitions_root, self.profile.data_root + "/scheduler/config.json")
        if path.exists():
            atomic_json(safe_path(self.profile.definitions_root, self.profile.data_root + "/scheduler/backups/" + uuid.uuid4().hex + ".json"), json.loads(path.read_text(encoding="utf-8")))
        atomic_json(path, {"schema_version": 1, "roles": roles})
        from .record_retention import enforce
        enforce(self.profile.definitions_root,'backups',self.profile.data_root+'/scheduler/backups')
        return roles
