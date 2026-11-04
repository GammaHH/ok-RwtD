"""Load user-installed project-local u2 dependencies; never installs/connects."""
import importlib
import importlib.util
import json
import struct
import sys
from pathlib import Path
from src.rwtd.android_core.module_store import safe_path


def enable(root):
    manifest = safe_path(root, "assets/rwtd/runtime/u2/active.json")
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("python") != list(sys.version_info[:2])
                or data.get("pointer_bits") != struct.calcsize("P") * 8):
            raise ValueError("Local u2 dependencies do not match this Python runtime")
        relative = data.get("relative_path", "")
        if not isinstance(relative, str) or not relative.startswith("assets/rwtd/runtime/u2/env_"):
            raise ValueError("Invalid project-local u2 dependency path")
        directory = safe_path(root, relative)
        if not (directory / "uiautomator2/__init__.py").is_file():
            raise ValueError("Local u2 installation is incomplete")
        if str(directory) not in sys.path:
            sys.path.append(str(directory))  # Preserve already-installed framework dependencies.
        importlib.invalidate_caches()
    if importlib.util.find_spec("uiautomator2") is None:
        raise ValueError("Run docs/rwtd/tools/Install-ThreePointSupport.ps1, then restart Dashboard")
    return importlib.import_module("uiautomator2")
