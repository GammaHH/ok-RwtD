"""Local presentation settings only; never affect action coordinates or tasks."""
import json
from src.rwtd.android_core.module_store import atomic_json, safe_path


class UiPreferences:
    def __init__(self, profile):
        self.path = safe_path(profile.project_root, profile.data_root + "/dashboard/ui_preferences.json")

    def load(self):
        if not self.path.exists():
            return {"scheduler_open": False, "preview_scale": 100}
        return self.validate(json.loads(self.path.read_text(encoding="utf-8-sig")))

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) - {"scheduler_open", "preview_scale", "preview_threshold"}:
            raise ValueError("Unknown UI preference")
        if "scheduler_open" in value and type(value["scheduler_open"]) is not bool:
            raise ValueError("Disclosure preference requires boolean")
        scale = value.get("preview_scale", 100)
        if type(scale) is not int or not 35 <= scale <= 160:
            raise ValueError("Preview scale must be 35..160 percent")
        threshold = value.get('preview_threshold', .8)
        if type(threshold) not in (int, float) or not .5 <= threshold <= 1:
            raise ValueError('Preview threshold must be 0.50..1.00')
        return value

    def save(self, updates):
        self.validate(updates)
        value = dict(self.load(), **updates)
        atomic_json(self.path, value)
        return value
