"""Retention for explicit generated evidence only; never follows reparse points."""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .module_store import atomic_json, safe_path, linked

SESSION = re.compile(r"session_\d{8}T\d{6}_\d{6}Z_[0-9a-f]{8}\Z")
FILES = {"report.json", "before.png", "after.png", "execution.json"}
VERIFIED = {"SUCCESS", "MANUAL_VERIFIED"}


def now():
    return datetime.now(timezone.utc).isoformat()


class StorageHistory:
    def __init__(self, profile):
        self.profile = profile
        self.root = profile.project_root
        self.active = set()
        self.protected_runs = set()
        self.unsafe_runs = set()
        self.plans = {}

    def path(self, suffix=""):
        return safe_path(self.root, self.profile.evidence_root + ("/" + suffix if suffix else ""))

    def settings(self):
        path = safe_path(self.root, self.profile.data_root + "/scheduler/storage.json")
        if not path.exists():
            return {"keep_successful": 100, "automatic_success_retention": True}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return self.validate_settings(data)

    @staticmethod
    def validate_settings(data):
        if not isinstance(data, dict) or set(data) != {"keep_successful", "automatic_success_retention"}:
            raise ValueError("Unknown retention setting")
        if type(data["keep_successful"]) is not int or not 1 <= data["keep_successful"] <= 10000:
            raise ValueError("Retention must keep 1..10000 successful sessions")
        if type(data["automatic_success_retention"]) is not bool:
            raise ValueError("Automatic retention must be boolean")
        return data

    def save_settings(self, data):
        atomic_json(safe_path(self.root, self.profile.data_root + "/scheduler/storage.json"), self.validate_settings(data))
        # Changing settings never triggers deletion of existing evidence.
        return data

    def begin(self, report_id):
        if not SESSION.fullmatch("session_" + report_id):
            raise ValueError("Invalid generated session id")
        directory = self.path("session_" + report_id)
        directory.mkdir(parents=True, exist_ok=False)
        self.active.add(directory.name)
        return directory

    def persist(self, record, completed=False):
        directory = self.path("session_" + record["report_id"])
        atomic_json(directory / "report.json", record)
        if completed:
            summary = dict(record, screenshots_cleaned=False)
            summary.pop("detection", None)
            if summary.get("verification"):
                summary["verification"] = dict(summary["verification"])
                for side in ("before", "after"):
                    summary["verification"][side] = {key: value for key, value in summary["verification"].get(side, {}).items() if key != "detections"}
            atomic_json(self.path("history/" + record["report_id"] + ".json"), summary)

    def finish(self, record):
        record.update(storage_schema=1, managed_by="android_workflow_evidence_v1", profile_id=self.profile.id,
                      completed=True, finished_utc=record.get("finished_utc") or now())
        self.persist(record, completed=True)
        self.active.discard("session_" + record["report_id"])
        self.retain_generated(record['report_id'])
        # Only a newly verified complete result can trigger retention. No startup cleanup.
        if record["status"] in VERIFIED and not record.get("failure_reason") and self.settings()["automatic_success_retention"]:
            self.retain_successful()

    def retain_successful(self):
        self.retain_generated()
        if self.settings()["automatic_success_retention"]:
            plan = self.preview()
            if plan["candidates"]:
                self.clean(plan["plan_id"], approved=True, automatic=True)

    def retain_generated(self, current_id=None):
        from .record_retention import enforce
        if self.profile.id!='rwtd':
            return
        protected=self.active | ({'session_'+current_id} if current_id else set())
        enforce(self.root,'sessions',self.profile.evidence_root,protected=protected,protected_runs=self.protected_runs)
        # Summaries for existing evidence are kept until that evidence expires.
        folder=self.path()
        summaries={p.name.removeprefix('session_')+'.json' for p in folder.iterdir() if p.name.startswith('session_')} if folder.exists() else set()
        enforce(self.root,'history',self.profile.evidence_root+'/history',protected=summaries,protected_runs=self.protected_runs)
        enforce(self.root,'cleanup_audit',self.profile.evidence_root+'/cleanup_audit')

    def read_history(self):
        directory = self.path("history")
        if not directory.exists():
            return []
        records = []
        for path in sorted(directory.glob("*.json")):
            if linked(path):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                if (isinstance(record, dict) and record.get("profile_id") == self.profile.id and record.get("completed") is True
                        and isinstance(record.get("status"), str)
                        and (record.get("module_result") is None or isinstance(record.get("module_result"), dict))
                        and (record.get("run_id") is None or isinstance(record.get("run_id"), str))
                        and SESSION.fullmatch("session_" + str(record.get("report_id", "")))
                        and path.stem == record["report_id"] and record.get("managed_by") == "android_workflow_evidence_v1"):
                    records.append(record)
            except (OSError, ValueError, AttributeError):
                continue
        return records

    def load_report(self, report_id):
        if not isinstance(report_id, str) or not SESSION.fullmatch("session_" + report_id):
            raise ValueError("Invalid report id")
        path = self.path("session_" + report_id + "/report.json")
        if path.exists():
            report = json.loads(path.read_text(encoding="utf-8-sig"))
            if report.get("report_id") != report_id or report.get("profile_id") != self.profile.id:
                raise ValueError("History record identity mismatch")
            return report
        matches = [record for record in self.read_history() if record["report_id"] == report_id]
        if len(matches) != 1:
            raise ValueError("History record unavailable")
        return matches[0]

    def eligible(self, directory):
        """Return a fingerprint only when every required artifact is valid and closed."""
        try:
            if not SESSION.fullmatch(directory.name) or directory.name in self.active or linked(directory) or not directory.is_dir():
                return None
            paths = list(directory.iterdir())
            if {path.name for path in paths} not in (FILES, {'report.json', 'execution.json'}):
                return None
            if any(linked(path) or not path.is_file() or path.name not in FILES for path in paths):
                return None
            record = json.loads((directory / "report.json").read_text(encoding="utf-8-sig"))
            from src.rwtd.android_core.evidence_policy import screenshots_omitted
            if {path.name for path in paths} != ({'report.json', 'execution.json'} if screenshots_omitted(record) else FILES):
                return None
            if (record.get("run_id") in self.protected_runs | self.unsafe_runs
                    or record.get("automatic_status") in {"FAILED", "TIMEOUT", "BLOCKED", "INPUT_DELIVERY_UNKNOWN"}):
                return None
            execution = json.loads((directory / "execution.json").read_text(encoding="utf-8-sig"))
            if directory.name != "session_" + record.get("report_id", "") or execution.get("report_id") != record.get("report_id"):
                return None
            if (record.get("storage_schema") != 1 or record.get("managed_by") != "android_workflow_evidence_v1"
                    or record.get("profile_id") != self.profile.id or record.get("completed") is not True
                    or record.get("status") not in VERIFIED or record.get("failure_reason")
                    or record.get("action_completed", record.get("input_command_returned")) is not True or not record.get("finished_utc")):
                return None
            if record["status"] == "MANUAL_VERIFIED" and record.get("manual_verification", {}).get("pass") is not True:
                return None
            if record["status"] == "SUCCESS" and (record.get("verification", {}).get("automatic") is not True
                    or record.get("verification", {}).get("policy") in {None, "manual", "none"}):
                return None
            for key in ("before", "after"):
                from src.rwtd.android_core.evidence_policy import screenshots_omitted
                if screenshots_omitted(record):
                    if (directory / (key + '.png')).exists():
                        return None
                    continue
                path = directory / (key + ".png")
                if record.get(key) != str(path) or not path.is_file() or path.stat().st_size < 8:
                    return None
                from src.rwtd.discord_notifications import validate_png
                if validate_png(path.read_bytes()) != (self.profile.width, self.profile.height):
                    return None
            summary = self.path("history/" + record["report_id"] + ".json")
            data = json.loads(summary.read_text(encoding="utf-8-sig"))
            if data.get("status") != record["status"] or data.get("completed") is not True:
                return None
            signature = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(paths)]
            signature.append(("report_hash", hashlib.sha256((directory / "report.json").read_bytes()).hexdigest()))
            return {"session": directory.name, "bytes": sum(p.stat().st_size for p in paths),
                    "signature": signature, "report_id": record["report_id"], "group": record.get("run_id") or record["report_id"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def preview(self):
        folder = self.path()
        self.refresh_run_states()
        eligible = []
        if folder.exists():
            for path in folder.iterdir():
                if not SESSION.fullmatch(path.name):
                    continue
                item = self.eligible(path)
                if item:
                    eligible.append(item)
        eligible.sort(key=lambda item: item["session"], reverse=True)
        keep = self.settings()["keep_successful"]
        groups = list(dict.fromkeys(item["group"] for item in eligible))
        old_groups = set(groups[keep:])
        candidates = [item for item in eligible if item["group"] in old_groups]
        plan = {"plan_id": uuid.uuid4().hex, "created_utc": now(), "keep_successful": keep,
                "candidates": candidates, "estimated_reclaimable_bytes": sum(item["bytes"] for item in candidates),
                "retained_successful_sessions": min(len(groups), keep), "counting": "module run groups; standalone tests count individually", "dry_run": True}
        self.plans.clear()
        self.plans[plan["plan_id"]] = plan
        return plan

    def refresh_run_states(self):
        runs = {}
        for record in self.read_history():
            if record.get("run_id"):
                runs.setdefault(record["run_id"], []).append(record)
        self.unsafe_runs = {run_id for run_id, records in runs.items() if any(record["status"] not in VERIFIED for record in records)
                            or not any((record.get("module_result") or {}).get("status") == "SUCCESS" for record in records)}

    def clean(self, plan_id, approved=False, automatic=False):
        if approved is not True or plan_id not in self.plans:
            raise ValueError("A current dry-run plan and explicit cleanup approval are required")
        plan = self.plans.pop(plan_id)
        self.refresh_run_states()
        audit = {"timestamp_utc": now(), "plan_id": plan_id, "automatic": automatic, "removed": [], "skipped": []}
        audit_path = self.path("cleanup_audit/" + now().replace(":", "-") + "_" + uuid.uuid4().hex + ".json")
        atomic_json(audit_path, dict(audit, phase="STARTED", plan=plan))
        for item in plan["candidates"]:
            directory = self.path(item["session"])
            try:
                fresh = self.eligible(directory)
                if fresh != item:
                    audit["skipped"].append({"session": item["session"], "reason": "Changed, active or unsafe"})
                    continue
                summary_path = self.path("history/" + item["report_id"] + ".json")
                summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
                # Preserve metadata before touching evidence, including a pending audit marker.
                summary.update(cleanup_started_utc=now(), cleanup_plan=plan_id)
                atomic_json(summary_path, summary)
                for path in directory.iterdir():
                    if linked(path) or path.name not in FILES or not path.is_file():
                        raise ValueError("Session changed during cleanup")
                    path.unlink()
                directory.rmdir()
                summary.update(screenshots_cleaned=True, cleanup_completed_utc=now())
                atomic_json(summary_path, summary)
                audit["removed"].append(item["session"])
            except (OSError, ValueError):
                audit["skipped"].append({"session": item["session"], "reason": "Cleanup refused or incomplete; inspect audit"})
            atomic_json(audit_path, dict(audit, phase="IN_PROGRESS"))
        atomic_json(audit_path, dict(audit, phase="COMPLETED"))
        return dict(audit, status="READY", audit_path=str(audit_path))

    @staticmethod
    def usage(folder):
        total, sessions, skipped = 0, 0, 0
        if not folder.exists():
            return {"bytes": 0, "sessions": 0, "skipped": 0}
        stack = [folder]
        while stack:
            directory = stack.pop()
            try:
                if linked(directory):
                    skipped += 1
                    continue
                for path in directory.iterdir():
                    if linked(path):
                        skipped += 1
                    elif path.is_dir():
                        sessions += int(SESSION.fullmatch(path.name) is not None or path.name.startswith(("recognition_preview_", "exception_")))
                        stack.append(path)
                    elif path.is_file():
                        total += path.stat().st_size
            except OSError:
                skipped += 1
        return {"bytes": total, "sessions": sessions, "skipped": skipped}

    def scan(self):
        # No follow of junctions; these figures describe this profile's managed trees.
        groups = {"module_tests": self.usage(self.path()),
                  "definitions_presets_scheduler": self.usage(safe_path(self.root, self.profile.data_root))}
        if self.profile.preview_root:
            analysis = safe_path(self.root, self.profile.preview_root)
            previews = {"bytes": 0, "sessions": 0, "skipped": 0}
            if analysis.exists():
                for path in analysis.iterdir():
                    if path.name.startswith("recognition_preview_"):
                        size = self.usage(path)
                        previews["bytes"] += size["bytes"]
                        previews["sessions"] += int(size["bytes"] > 0)
                        previews["skipped"] += size["skipped"]
            groups.update(recognition_preview=previews,
                          exception_evidence=self.usage(safe_path(self.root, self.profile.exception_root)),
                          discord_records=self.usage(safe_path(self.root, self.profile.notification_root)))
        plan = self.preview()
        return {"groups": groups, "total_managed_bytes": sum(item["bytes"] for item in groups.values()),
                "settings": self.settings(), "cleanup_preview": plan,
                "history_sessions": len(self.read_history()), "active_sessions": sorted(self.active),
                "note": "Managed profile storage only; source images, macros, DEX and original evidence are excluded from cleanup."}
