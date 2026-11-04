"""Process-local definition lease shared by native tasks and Dashboard stores."""
import threading
from contextlib import contextmanager
from pathlib import Path
from functools import wraps

_lock = threading.RLock()
_owners = {}


def key(root):
    return str(Path(root).resolve()).casefold()


def active(root):
    with _lock:
        return key(root) in _owners


@contextmanager
def acquire(root, owner):
    with _lock:
        ident = key(root)
        if ident in _owners:
            raise ValueError('另一個原生任務正在使用流程；停止後才能啟動 Farm')
        _owners[ident] = owner
    try:
        yield
    finally:
        with _lock:
            if _owners.get(ident) is owner:
                _owners.pop(ident)


@contextmanager
def editing(root):
    with _lock:
        if active(root):
            raise ValueError('Farm 執行中；停止後才能修改 Canvas 或模組')
        yield


def definition_edit(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with editing(self.profile.project_root):
            return method(self, *args, **kwargs)
    return wrapped
