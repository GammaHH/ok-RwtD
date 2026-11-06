"""Installed user state lives outside PyAppify's versioned working tree."""
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from threading import RLock

from .module_store import safe_path


_lock = RLock()
TIMING_DB = 'assets/rwtd/runtime/farm_round_timings.sqlite3'


def definition_root(project_root):
    root = Path(project_root).absolute()
    if (not os.environ.get('PYAPPIFY_APP_VERSION') or root.name.casefold() != 'working'
            or not (root.parent / 'repo').is_dir()):
        return root
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local').absolute()
    key = hashlib.sha256(str(root.resolve()).casefold().encode('utf-8')).hexdigest()[:16]
    destination = safe_path(local, 'RwtDUserData/' + key)
    with _lock:
        if destination.is_dir():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=destination.parent) as temporary:
            staged = Path(temporary) / 'data'
            staged.mkdir()
            for area in ('modules', 'scheduler', 'presets'):
                relative = 'assets/rwtd/' + area
                source = safe_path(root, relative)
                if source.is_dir():
                    _copy_json_tree(root, source, staged)
            legacy_db = safe_path(root, TIMING_DB)
            if legacy_db.is_file():
                output = safe_path(staged, TIMING_DB)
                output.parent.mkdir(parents=True, exist_ok=True)
                from contextlib import closing
                with closing(sqlite3.connect(legacy_db.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(output)) as snapshot:
                        original.backup(snapshot)
            # Publish only the complete migration. A concurrent winner is kept.
            try:
                staged.rename(destination)
            except OSError:
                if not safe_path(local, 'RwtDUserData/' + key).is_dir():
                    raise
        return destination


def _copy_json_tree(root, source, destination):
    for item in source.iterdir():
        relative = item.relative_to(root)
        checked = safe_path(root, relative)
        if checked.is_dir():
            _copy_json_tree(root, checked, destination)
        elif checked.is_file() and checked.suffix.lower() == '.json':
            output = safe_path(destination, relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked, output)


def timing_db_path(root):
    return safe_path(definition_root(root), TIMING_DB)
