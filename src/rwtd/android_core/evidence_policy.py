"""Optional screenshot persistence; capture safety and delivery checks are separate."""
from src.rwtd.android_core.workflow_model import candidate_features


def requires_screenshots(parameters):
    return bool(candidate_features(parameters) or any(parameters.get(key) for key in
                ('precondition_feature', 'verify_feature', 'previous_feature', 'ocr_target', 'target_conditions')) or
                parameters.get('verification_policy') in {'coco_detected', 'coco_disappeared', 'screen_transition'})


def screenshots_omitted(report):
    return (report.get('screenshot_policy') == 'COCO_ONLY' and report.get('screenshots_required') is False
            and isinstance(report.get('parameters'), dict) and not requires_screenshots(report['parameters'])
            and report.get('before') is None and report.get('after') is None)
