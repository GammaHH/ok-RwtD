"""Prune only completed read-only recognition previews, never exception history."""
import json
import re
import shutil
from pathlib import Path

from src.rwtd.exception_monitor import scoped_path

NAME = re.compile(r"recognition_preview_\d{8}T\d{6}_\d{6}Z\Z")


def retain_previews(project_root: Path, keep=3):
    root = Path(project_root).resolve()
    folder = scoped_path(root, "docs/rwtd/analysis")
    if not folder.exists():
        return []
    eligible = []
    for path in folder.iterdir():
        if not NAME.fullmatch(path.name) or not path.is_dir() or path.is_symlink():
            continue
        try:
            scoped_path(root, path.relative_to(root))
            report = json.loads((path / "report.json").read_text(encoding="utf-8-sig"))
            if report.get("device_inputs") != 0 or report.get("execution_enabled") is not False \
                    or report.get("mode") not in ("Offline Screenshot", "Live ADB Preview"):
                continue
            # Refuse all linked descendants before deleting any generated output.
            if any(item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
                   for item in path.rglob("*")):
                continue
            eligible.append(path)
        except (OSError, ValueError):
            continue
    removed = []
    for path in sorted(eligible, key=lambda item: item.name, reverse=True)[max(1, int(keep)):]:
        scoped_path(root, path.relative_to(root))
        shutil.rmtree(path)
        removed.append(path.name)
    return removed
