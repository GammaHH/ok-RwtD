"""Color components in native BGR frames; tolerance is per-channel RGB delta."""
import cv2
import numpy as np
from src.rwtd.android_core.workflow_model import validate_roi, TargetNotFound


def validate_color(step):
    if step.get('color_mode') is None:
        if any(key in step for key in ('color_rgb', 'color_tolerance', 'color_min_pixels')):
            raise ValueError('Color parameters require color_mode')
        return
    if step['color_mode'] not in {'only', 'coco', 'either'} or step.get('name') == 'WaitTest':
        raise ValueError('Unsupported color target mode')
    rgb = step.get('color_rgb')
    if not isinstance(rgb, list) or len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb):
        raise ValueError('Color requires RGB integers 0..255')
    tolerance, minimum = step.get('color_tolerance', 10), step.get('color_min_pixels', 9)
    if type(tolerance) is not int or not 0 <= tolerance <= 255 or type(minimum) is not int or not 1 <= minimum <= 2592000:
        raise ValueError('Invalid color tolerance/minimum area')
    if step.get('recognize_before', True) is not True:
        raise ValueError('Color targeting requires fresh recognition')


def components(frame, step, roi=None):
    validate_color(step)
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.shape != (2400, 1080, 3):
        raise ValueError('Color matching requires native 1080x2400 BGR')
    roi = roi or step.get('coco_roi')
    if roi is None:
        raise ValueError('Color targeting requires explicit global/TAG/step ROI')
    x, y, w, h = validate_roi(roi)
    pixels = frame[y:y+h, x:x+w].astype(np.int16)
    mask = np.all(np.abs(pixels-np.array(step['color_rgb'][::-1], dtype=np.int16)) <= step.get('color_tolerance', 10), axis=2).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    hits = []
    for index in range(1, count):
        bx, by, bw, bh, area = map(int, stats[index])
        if area >= step.get('color_min_pixels', 9):
            hits.append({'bbox': [x+bx, y+by, bw, bh], 'pixels': area,
                         'color_rgb': step['color_rgb'], 'tolerance': step.get('color_tolerance', 10)})
    return hits


def color_target(frame, step):
    hits = components(frame, step)
    if not hits:
        raise TargetNotFound('Color target missing; no guessed input')
    from .target_order import POSITION_POLICIES,choose
    if step.get('target_policy') in POSITION_POLICIES:
        hits=choose(hits,step['target_policy'])
    if len(hits) != 1:
        raise ValueError('Color target missing or ambiguous; no guessed input')
    return hits[0], hits


def either_targets(frame, step, detections, multiple=False, evidence=None):
    """COCO OR color: use one frame, preserve unique targets and old AND mode."""
    from src.rwtd.android_core.workflow_model import candidate_features, select_target, select_targets, target_inside_search
    names = candidate_features(step)
    qualified = [hit for hit in detections if hit.get('matched') and hit.get('feature') in names
                 and isinstance(hit.get('confidence'), (int, float)) and hit['confidence'] >= step.get('threshold', .9)]
    coco_detected=len(qualified)
    qualified=[hit for hit in qualified if target_inside_search(step,hit)]
    coco = select_targets(step, qualified) if qualified else []
    colors = []
    color_hits=components(frame, step)
    for hit in color_hits:
        x, y, w, h = hit['bbox']
        cx, cy = x+w/2, y+h/2
        if any(bx <= cx < bx+bw and by <= cy < by+bh for bx, by, bw, bh in (h['bbox'] for h in coco)):
            continue  # A color inside a recognized button is not a second click.
        colors.append(dict(hit, feature='__color__', matched=True, confidence=1,
                           evidence_kind='color_predicate'))
    if evidence is not None:
        evidence.update(coco_detected=coco_detected,coco_eligible=len(coco),color_components=len(color_hits),
                        color_additional_targets=len(colors),color_rgb=step['color_rgb'],
                        color_tolerance=step.get('color_tolerance',10),
                        color_min_pixels=step.get('color_min_pixels',9),color_roi=step.get('coco_roi'),
                        color_positions=[hit['bbox'] for hit in color_hits[:100]])
    if not coco and not colors:
        raise TargetNotFound('COCO OR color: no eligible target; COCO=0; color components=0; '
                             f"RGB={step['color_rgb']}; tolerance={step.get('color_tolerance',10)}; "
                             f"min_pixels={step.get('color_min_pixels',9)}; ROI={step.get('coco_roi')}")
    from .target_order import POSITION_POLICIES,choose
    if not multiple and step.get('target_policy') in POSITION_POLICIES:
        return choose(select_targets(dict(step,features=names+['__color__']),coco+colors),step['target_policy'])
    if not multiple and step.get('target_policy')=='unique_only':
        targets=select_targets(dict(step,features=names+['__color__']),coco+colors)
        if len(targets)!=1:raise ValueError('Multiple COCO OR color targets; unique-only policy sends no input')
        return targets
    if multiple:
        return select_targets(dict(step, features=names+['__color__']), coco+colors)
    if coco:
        return [select_target(step, coco)]
    if len(colors) != 1:
        raise ValueError('COCO OR color target missing or ambiguous; no guessed input')
    return colors
