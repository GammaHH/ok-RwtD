"""Backward compatible, data-only workflow targets and module-local shared ROIs."""
import copy
import math
import re

TAG_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")


class TargetNotFound(ValueError):
    """Valid recognition found no eligible target; no input has been planned."""


class ExclusionIntersection(ValueError):
    """An input is deliberately unsent; the enclosing module can continue."""
    def __init__(self, points, region):
        self.points=copy.deepcopy(points)
        self.region=copy.deepcopy(region)
        super().__init__('Touch intersects remembered exclusion: '+region.get('feature','region'))


def validate_roi(roi):
    if (not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi)
            or roi[0] < 0 or roi[1] < 0 or roi[2] <= 0 or roi[3] <= 0
            or roi[0] + roi[2] > 1080 or roi[1] + roi[3] > 2400):
        raise ValueError("ROI requires native integer [x,y,width,height] inside 1080x2400")
    return roi


def require_outside_exclusions(points, regions):
    """Reject touch points or continuous path segments intersecting remembered boxes."""
    for region in regions.values():
        if 'pixel_mask' in region:
            from .canvas_exclusion import mask_intersects
            if mask_intersects(region, points):
                raise ExclusionIntersection(points, region)
            continue
        x, y, w, h = validate_roi(region['bbox'])
        for start, end in zip(points, points[1:] or points):
            low, high = 0., 1.
            for axis, left, right in ((0,x,x+w),(1,y,y+h)):
                delta = end[axis]-start[axis]
                if delta == 0:
                    if not left <= start[axis] <= right:
                        high = -1.
                        break
                else:
                    a, b = (left-start[axis])/delta, (right-start[axis])/delta
                    low, high = max(low,min(a,b)), min(high,max(a,b))
            if low <= high:
                raise ExclusionIntersection(points,region)


def validate_tags(tags):
    if not isinstance(tags, dict) or len(tags) > 100:
        raise ValueError("Module tags must be an object (at most 100)")
    for tag_id, tag in tags.items():
        if not isinstance(tag_id, str) or not TAG_ID.fullmatch(tag_id):
            raise ValueError("Invalid module-local TAG id")
        if not isinstance(tag, dict) or set(tag) != {"name", "roi"}:
            raise ValueError("TAG only supports name and ROI; nested/cross-module references forbidden")
        if not isinstance(tag["name"], str) or not 1 <= len(tag["name"]) <= 120:
            raise ValueError("TAG display name required")
        validate_roi(tag["roi"])


def candidate_features(step):
    return step.get("features", [step["feature"]] if step.get("feature") else [])


def validate_candidates(step, allowed):
    features = candidate_features(step)
    if (not isinstance(features, list) or len(features) > 30 or any(not isinstance(f, str) or f not in allowed for f in features)
            or len(set(features)) != len(features)):
        raise ValueError("Unique authorized COCO candidates required (at most 30)")
    if "features" in step and step.get("feature") and features != [step["feature"]]:
        raise ValueError("Conflicting legacy feature and candidate features")
    priority = step.get("feature_priority")
    if priority is not None and (not isinstance(priority, list) or any(not isinstance(f, str) for f in priority)
                                 or len(priority) != len(features) or set(priority) != set(features)):
        raise ValueError("Explicit priority must list each candidate exactly once")
    if "roi_tag" in step and (not isinstance(step["roi_tag"], str) or not TAG_ID.fullmatch(step["roi_tag"])):
        raise ValueError("TAG reference must be a local id")
    if "roi_tag" in step and "coco_roi" in step:
        raise ValueError("A step uses either shared TAG ROI or legacy inline ROI, never both")
    return features


def resolve_step(module, step):
    result = copy.deepcopy(step)
    global_roi = module.get("global_roi")
    if "global_roi" in module:
        validate_roi(global_roi)
    tag_id = result.pop("roi_tag", None)
    if tag_id:
        tags = module.get("tags", {})
        validate_tags(tags)
        if tag_id not in tags:
            raise ValueError("Missing module-local TAG")
        result["coco_roi"] = list(tags[tag_id]["roi"])
    elif global_roi is not None:
        # Global is module-local and overrides inline ROIs only at execution.
        # Preserve the original step data so disabling global restores it.
        result["coco_roi"] = list(global_roi)
    if result.get('input_conditions'):
        from .step_conditions import resolve
        result['input_conditions'] = resolve(result['input_conditions'], module)
    if result.get('target_conditions'):
        from .step_conditions import resolve
        scope=dict(module,global_roi=result.get('coco_roi') or module.get('global_roi') or [0,0,1080,2400])
        result['target_conditions'] = resolve(result['target_conditions'], scope)
    return result


def target_inside_search(step, hit):
    """Use the matcher's effective ROI, without expanding the saved ROI again."""
    bx,by,bw,bh=validate_roi(hit.get('bbox'))
    configured=(step.get('coco_feature_rois') or {}).get(hit.get('feature'),step.get('coco_roi'))
    roi=configured
    search=hit.get('search_bbox')
    provenance=hit.get('configured_search_bbox')
    if search is not None:
        validate_roi(search)
        if provenance is not None:
            validate_roi(provenance)
            if configured is not None and list(configured)!=list(provenance):return False
            from .roi_policy import effective_roi
            margin=hit.get('roi_margin_px',0)
            if type(margin) is not int or margin not in (0,10):return False
            if list(search)!=effective_roi(provenance,'coco',enabled=margin==10):return False
            roi=search
        elif configured is None:
            roi=search
    if roi is None:return True
    x,y,w,h=validate_roi(roi)
    return x<=bx and y<=by and bx+bw<=x+w and by+bh<=y+h


def select_target(step, detections):
    candidates = candidate_features(step)
    matches = []
    for hit in detections:
        if hit.get("feature") not in candidates or not hit.get("matched"):
            continue
        confidence = hit.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or confidence < step.get("threshold", .9):
            continue
        if not target_inside_search(step,hit):continue
        matches.append(hit)
    if not matches:
        raise TargetNotFound("No valid COCO target; no guessed input")
    from .target_order import POSITION_POLICIES,choose
    if step.get('target_policy') in POSITION_POLICIES:
        return choose(matches,step['target_policy'])[0]
    if step.get('target_policy')=='unique_only':
        distinct=select_targets(step,matches)
        if len(distinct)!=1:raise ValueError('Multiple COCO targets matched; unique-only policy sends no input')
        return distinct[0]
    if len(matches) == 1:
        return matches[0]
    priority = step.get("feature_priority")
    if not priority:
        raise ValueError("Ambiguous COCO candidates; set explicit priority or handle manually")
    for feature in priority:
        hits = [hit for hit in matches if hit["feature"] == feature]
        if len(hits) > 1:
            raise ValueError("Multiple positions for highest-priority feature; manual handling required")
        if hits:
            return hits[0]
    raise ValueError("No deterministic target")


def select_targets(step, detections):
    """Frozen finite click plan, top-to-bottom, then left-to-right."""
    candidates = candidate_features(step)
    hits = []
    for hit in detections:
        if hit.get('feature') not in candidates or not hit.get('matched'):
            continue
        score = hit.get('confidence')
        if type(score) not in (int, float) or not math.isfinite(score) or score < step.get('threshold', .9):
            continue
        if not target_inside_search(step,hit):continue
        hits.append(copy.deepcopy(hit))
    distinct = []
    for hit in sorted(hits, key=lambda h: (-h['confidence'], h['feature'])):
        x, y, w, h = hit['bbox']
        overlap = False
        for old in distinct:
            a, b, c, d = old['bbox']
            intersection = max(0, min(x+w, a+c)-max(x, a))*max(0, min(y+h, b+d)-max(y, b))
            if intersection/(w*h+c*d-intersection) >= .65:
                overlap = True
                break
        if not overlap:
            distinct.append(hit)
    if not distinct:
        raise TargetNotFound('No valid COCO batch; no guessed input')
    if len(distinct) > 100:
        raise ValueError('Too many COCO targets; no guessed input')
    return sorted(distinct, key=lambda h: (h['bbox'][1], h['bbox'][0], h['feature']))


def migrate_inline_rois(module):
    """Explicit in-memory preview only. Caller must save with revision to persist."""
    result = copy.deepcopy(module)
    tags = result.setdefault("tags", {})
    for step in result["steps"]:
        if "coco_roi" not in step:
            continue
        roi = validate_roi(step["coco_roi"])
        tag_id = next((key for key, tag in tags.items() if tag["roi"] == roi), None)
        if tag_id is None:
            index = 1
            while "ImportedROI_" + str(index) in tags:
                index += 1
            tag_id = "ImportedROI_" + str(index)
            tags[tag_id] = {"name": tag_id, "roi": list(roi)}
        step.pop("coco_roi")
        step["roi_tag"] = tag_id
    return result
