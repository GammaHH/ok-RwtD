"""Recognition-only search margins for non-reference Android resolutions.

Saved ROIs always remain in 1080x2400 reference space. Apply this once, after
choosing the final override/default ROI and immediately before recognition.
"""
REFERENCE_SIZE = (1080, 2400)


def new_resolution(geometry):
    """Unknown/imported reference geometry never silently enables margins."""
    return geometry is not None and (geometry.width, geometry.height) != REFERENCE_SIZE


def effective_roi(roi, kind, *, enabled=True, progress=False):
    """Copy and expand a reference ROI; clipping never translates its center.

    ``None`` still means full-frame search. ``progress`` protects the fixed
    PROGRESS OCR window. Callers with physical geometry must pass ``enabled``.
    """
    if kind not in {'coco', 'ocr'}:
        raise ValueError('ROI policy kind must be coco or ocr')
    if type(enabled) is not bool or type(progress) is not bool:
        raise ValueError('ROI policy switches must be boolean')
    if roi is None:
        return None
    if (not isinstance(roi, (list, tuple)) or len(roi) != 4 or
            any(type(value) is not int for value in roi)):
        raise ValueError('ROI must be [x,y,width,height] integers')
    x, y, width, height = roi
    if (x < 0 or y < 0 or width <= 0 or height <= 0 or
            x + width > 1080 or y + height > 2400):
        raise ValueError('ROI outside the 1080x2400 reference frame')
    margin = (10 if kind == 'coco' else 5) if enabled and not progress else 0
    left, top = max(0, x - margin), max(0, y - margin)
    right, bottom = min(1080, x + width + margin), min(2400, y + height + margin)
    return [left, top, right - left, bottom - top]
