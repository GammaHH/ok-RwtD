"""Shared feature search defaults. Explicit caller ROIs always take precedence."""
import hashlib
import json
import math
import threading
from types import SimpleNamespace

from .module_store import atomic_json, safe_path
from .workflow_model import validate_roi
from .execution_lease import editing

RELATIVE = 'assets/rwtd/scheduler/feature_settings.json'
_lock = threading.RLock()
_cache = {}


def revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate(options):
    if not isinstance(options, dict) or set(options) - {'roi', 'grayscale', 'scale', 'warp_template'}:
        raise ValueError('Feature settings support ROI, grayscale, scale and warp_template only')
    roi = options.get('roi')
    if roi is not None:
        validate_roi(roi)
    gray, scale = options.get('grayscale', False), options.get('scale', 1)
    if type(gray) is not bool or type(scale) not in (float, int) or not math.isfinite(scale) or not .5 <= scale <= 2:
        raise ValueError('Scale must be 0.5..2; grayscale must be boolean')
    result={'roi': roi, 'grayscale': gray, 'scale': scale}
    if 'warp_template' in options:
        if type(options['warp_template']) is not bool:raise ValueError('warp_template must be boolean')
        result['warp_template']=options['warp_template']
    return result


def template_for_geometry(template,bbox,geometry,options):
    """Template scaling and ROI/coordinate mapping are independent policies."""
    if geometry is None or options.get('warp_template',True) is False:return template,None
    return geometry.map_template(template,bbox)


def load(root):
    from .user_data import definition_root
    path = safe_path(definition_root(root), RELATIVE)
    stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
    key = str(path)
    with _lock:
        if key in _cache and _cache[key][0] == stamp:
            return json.loads(_cache[key][1])
        data = json.loads(path.read_text(encoding='utf-8-sig')) if stamp else {'schema_version': 1, 'features': {}}
        if not isinstance(data, dict) or set(data) != {'schema_version', 'features'} or data['schema_version'] != 1 or not isinstance(data['features'], dict):
            raise ValueError('Invalid shared feature settings')
        data['features'] = {name: validate(value) for name, value in data['features'].items()}
        # A few project roots may be used by offline fixtures; keep this cache bounded.
        if len(_cache) >= 8:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (stamp, json.dumps(data, allow_nan=False))
        return json.loads(_cache[key][1])


def save(root, feature, options, expected_revision):
    from .feature_catalog import feature_names
    with editing(root):
        data = load(root)
        if expected_revision != revision(data):
            raise ValueError('Shared settings changed; reload before saving')
        if feature not in feature_names(root):
            raise ValueError('Unknown COCO feature')
        data['features'][feature] = validate(options)
        from .user_data import definition_root
        atomic_json(safe_path(definition_root(root), RELATIVE), data)
        return revision(data)


def preprocess(image, options):
    import cv2
    output = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if options['grayscale'] and image.ndim == 3 else image
    scale = options['scale']
    if scale != 1:
        output = cv2.resize(output, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return output


def match(frame, template, roi, threshold, options, limit=100):
    """Evaluate the same preprocessing in preview and live matching; native boxes."""
    import cv2
    import numpy as np
    from .workflow_model import validate_roi
    if frame.shape[:2] != (2400, 1080):
        raise ValueError('Matching requires a native 1080x2400 image')
    x, y, w, h = validate_roi(roi or [0, 0, 1080, 2400])
    search = preprocess(frame[y:y+h, x:x+w], options)
    target = preprocess(template, options)
    if target.shape[0] > search.shape[0] or target.shape[1] > search.shape[1]:
        raise ValueError('Template does not fit search ROI')
    if float(np.std(search)) < 1 or float(np.std(target)) < 1:
        raise ValueError('Uniform/black image cannot establish valid recognition')
    scores = cv2.matchTemplate(search, target, cv2.TM_CCOEFF_NORMED)
    if not np.isfinite(scores).all():
        raise ValueError('Nonfinite match confidence')
    best = float(np.max(scores))
    boxes = []
    th, tw = target.shape[:2]
    # Account for OpenCV dimension rounding when mapping coordinates back.
    sx, sy = search.shape[1]/w, search.shape[0]/h
    for _ in range(limit):
        _, confidence, _, (bx, by) = cv2.minMaxLoc(scores)
        if confidence < threshold:
            break
        boxes.append(SimpleNamespace(x=x+round(bx/sx), y=y+round(by/sy),
            width=template.shape[1], height=template.shape[0], confidence=float(confidence)))
        scores[max(0, by-th+1):by+th, max(0, bx-tw+1):bx+tw] = -1
    return boxes, best
