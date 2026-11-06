"""RwtD data locations and frame policy."""
from pathlib import Path
from src.rwtd.android_core.profile import AndroidProfile


def rwtd_profile(root, step_validator):
    from src.rwtd.android_core.user_data import definition_root
    return AndroidProfile("rwtd", Path(root).absolute(), "assets/rwtd",
                          "docs/rwtd/analysis/module_tests", "ok_templates/coco_annotations.json",
                          1080, 2400, step_validator, "docs/rwtd/analysis", "docs/rwtd/exceptions",
                          "docs/rwtd/notifications", persistent_root=definition_root(root))
