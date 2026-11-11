"""Canvas-owned exclusions; next-node masks never leak to other operations."""
import base64
import copy
import zlib

from .workflow_model import validate_roi


def validate(node):
    mode = node.get('exclusion_mode', 'coco')
    if mode not in {'coco', 'roi', 'color', 'coco_or_color'} or node.get('exclusion_scope', 'remember') not in {'remember', 'next'}:
        raise ValueError('Invalid exclusion source/scope')
    if mode != 'coco' and node.get('exclusion_scope') != 'next':
        raise ValueError('ROI/color exclusions require NEXT scope')
    if mode in {'roi', 'color', 'coco_or_color'} or 'roi' in node:
        validate_roi(node.get('roi'))
    if mode in {'color', 'coco_or_color'}:
        from .color_target import validate_color
        rows = node.get('exclusion_colors')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
            raise ValueError('Color exclusions require 1..10 colors')
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'rgb', 'tolerance', 'min_pixels'}:
                raise ValueError('Invalid exclusion color')
            validate_color(dict(color_mode='only', color_rgb=row.get('rgb'),
                                color_tolerance=row.get('tolerance', 10),
                                color_min_pixels=row.get('min_pixels', 9)))


def color_region(node, frame):
    """Freeze the real color pixels, preserving safe holes inside a large panel."""
    import cv2
    import numpy as np
    validate(node)
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.shape != (2400, 1080, 3):
        raise ValueError('Exclusion colors require native 1080x2400 BGR frame')
    x, y, w, h = node['roi']
    pixels = frame[y:y+h, x:x+w].astype(np.int16)
    mask = np.zeros((h, w), dtype=np.uint8)
    for row in node['exclusion_colors']:
        candidate = np.all(np.abs(pixels - np.array(row['rgb'][::-1], dtype=np.int16)) <= row.get('tolerance', 10), axis=2).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, connectivity=4)
        keep = np.zeros(count, dtype=np.uint8)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= row.get('min_pixels', 9)
        mask |= keep[labels]
    packed = base64.b64encode(zlib.compress(np.packbits(mask).tobytes())).decode('ascii')
    return {'bbox': list(node['roi']), 'feature': 'color', 'pixel_mask': packed,
            'matched_pixels': int(mask.sum())}


def mask_intersects(region, points):
    """Rasterize the complete gesture, not just its endpoints."""
    import numpy as np
    x, y, w, h = validate_roi(region['bbox'])
    limit = (w*h + 7)//8
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(region['pixel_mask'], validate=True), limit+1)
    if len(raw) != limit or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid exclusion pixel mask')
    mask = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))[:w*h].reshape(h, w)
    for start, end in zip(points, points[1:] or points):
        steps = max(abs(end[0]-start[0]), abs(end[1]-start[1]), 1)
        for index in range(steps+1):
            px = round(start[0]+(end[0]-start[0])*index/steps)-x
            py = round(start[1]+(end[1]-start[1])*index/steps)-y
            if 0 <= px < w and 0 <= py < h and mask[py, px]:
                return True
    return False


def remember(runtime, node, regions):
    """Replace this visit's snapshot, including an empty valid match set."""
    runtime.exclusion_regions = {key: value for key, value in runtime.exclusion_regions.items()
                                 if value.get('owner') != node['id'] and key != node['id']}
    target = next(e['target'] for e in runtime.graph['edges']
                  if e['source'] == node['id'] and e['port'] == 'NEXT')
    for index, region in enumerate(regions):
        item = copy.deepcopy(region)
        validate_roi(item['bbox'])
        item.update(owner=node['id'], target_node=target, device_inputs=0, simulated=runtime.source == 'simulation')
        runtime.exclusion_regions[node['id']+':'+str(index)] = item
    runtime.trace[-1]['exclusions'] = copy.deepcopy(regions)


def active_regions(runtime):
    """A different node (including a trigger) cannot inherit NEXT protection."""
    if runtime is None:
        return {}
    return {key: value for key, value in getattr(runtime, 'exclusion_regions', {}).items()
            if not value.get('target_node') or value['target_node'] == getattr(runtime, 'current_node', None)}


def leave(runtime, node_id):
    runtime.exclusion_regions = {key: value for key, value in runtime.exclusion_regions.items()
                                 if value.get('target_node') != node_id}
