"""Profiles own data locations and delegate device access to the host task."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AndroidProfile:
    id: str
    project_root: Path
    data_root: str
    evidence_root: str
    coco_path: str
    width: int
    height: int
    step_validator: object
    preview_root: str = ""
    exception_root: str = ""
    notification_root: str = ""
    persistent_root: Path | None = None

    @property
    def definitions_root(self):
        return self.persistent_root or self.project_root

    @property
    def modules_root(self):
        return self.data_root + "/modules"
