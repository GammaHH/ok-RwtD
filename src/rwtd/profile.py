"""RwtD data and frame policy. Existing assets remain at their original locations."""
from pathlib import Path
from src.rwtd.android_core.profile import AndroidProfile


def rwtd_profile(root, step_validator):
    return AndroidProfile("rwtd", Path(root).absolute(), "assets/rwtd",
                          "docs/rwtd/analysis/module_tests", "ok_templates/coco_annotations.json",
                          1080, 2400, step_validator, "docs/rwtd/analysis", "docs/rwtd/exceptions", "docs/rwtd/notifications")
