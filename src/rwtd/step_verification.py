"""Outcome policies independent of command completion and transport success."""
import math

POLICIES = ("coco_detected", "coco_disappeared", "ocr_increased", "ocr_decreased",
            "screen_transition", "manual", "none")


def verify(policy, before, after, expected="", previous=""):
    if policy not in POLICIES:
        raise ValueError("Unknown verification policy")
    if policy in {"manual", "none"}:
        return "INPUT_SENT_UNVERIFIED", "Await manual judgment" if policy == "manual" else "No automatic outcome verification configured"
    old, new = set(before.get("features", [])), set(after.get("features", []))
    if policy == "coco_detected":
        return ("SUCCESS", "Expected feature observed") if expected and expected in new else ("PENDING", "Expected feature absent")
    if policy == "coco_disappeared":
        if not expected or expected not in old:
            return "INPUT_SENT_UNVERIFIED", "Disappearance baseline unavailable"
        return ("SUCCESS", "Expected feature disappeared") if expected not in new else ("PENDING", "Feature still present")
    if policy == "screen_transition":
        if not previous or previous not in old:
            return "INPUT_SENT_UNVERIFIED", "Previous screen baseline unavailable"
        return ("SUCCESS", "Configured screen transition observed") if expected in new and previous not in new else ("PENDING", "Screen transition not yet demonstrated")
    a, b = before.get("ocr_value"), after.get("ocr_value")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (a, b)):
        return "INPUT_SENT_UNVERIFIED", "Verified numeric OCR baseline/result unavailable"
    success = b > a if policy == "ocr_increased" else b < a
    return ("SUCCESS", "Numeric OCR change observed") if success else ("PENDING", "OCR value has not changed as configured")


def module_outcome(required_steps):
    if not required_steps:
        return "BLOCKED"
    if any(status in {"FAILED", "TIMEOUT", "BLOCKED", "INPUT_DELIVERY_UNKNOWN"} for status in required_steps):
        return "FAILED"
    if all(status in {"SUCCESS", "MANUAL_VERIFIED"} for status in required_steps):
        return "SUCCESS"
    return "INPUT_SENT_UNVERIFIED"
