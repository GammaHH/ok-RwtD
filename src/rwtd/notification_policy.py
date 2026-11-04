"""Stable exception identity and narrowly defined local-only configuration errors."""
import hashlib
import json
import re


def exception_fingerprint(category, error_identifier, device_identifier, details):
    identity = {"category": str(category), "error_identifier": str(error_identifier),
                "device_identifier": str(device_identifier), "details": details}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def static_configuration_error(category, error_identifier, details):
    # Runtime categories must not be swallowed because a message mentions resolution.
    if str(category).upper() in {"ADB_CAPTURE_UNAVAILABLE", "ADB_DISCONNECTED", "TASK_FAILURE",
                                "GAMEPLAY_TASK_FAILURE", "UNEXPECTED_SCREEN_STATE", "RECOVERY_FAILED"}:
        return False
    if str(category).upper() in {"RESOLUTION_MISMATCH", "RESOLUTION_VALIDATION_FAILED", "STATIC_CONFIGURATION_ERROR"}:
        return True
    if str(category).upper() in {"CONFIGURATION_ERROR", "STARTUP_CONFIGURATION_ERROR", ""}:
        if str(error_identifier).lower() in {"supported_resolution", "resolution_mismatch", "resolution_validation_failed"}:
            return True
        return bool(re.search(r"\bResolution\s+\d+x\d+\s+check failed\b", str(details), re.IGNORECASE))
    return False


def device_identifier(device):
    for key in ("device_identifier", "serial", "device_id", "id", "address", "model"):
        if device.get(key):
            return str(device[key])
    return "unknown_device"
