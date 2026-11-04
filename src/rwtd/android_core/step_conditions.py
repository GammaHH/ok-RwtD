"""Input gates evaluated against one caller-owned frame, never click targets.

Rows combine strictly left to right. AND propagates UNKNOWN; OR uses the
known operand when only one is known. No expression strings are evaluated.
"""
import copy
import math

from .workflow_model import validate_roi, TAG_ID


class ConditionUnknown(ValueError):
    pass


class ConditionRejected(ValueError):
    pass


def combine(left, right, join):
    if join == 'AND':
        return None if left is None or right is None else left and right
    if left is None:
        return right
    if right is None:
        return left
    return left or right


def validate(group, allowed):
    if not isinstance(group, dict) or set(group) != {'evaluation', 'clauses'} or group['evaluation'] != 'left_to_right':
        raise ValueError('Step conditions require explicit left_to_right evaluation')
    rows = group['clauses']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 30:
        raise ValueError('Step conditions require 1..30 rows')
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('type'), str)
                or row['type'] not in {'coco', 'color', 'ocr'}
                or not isinstance(row.get('join', 'AND'), str)
                or row.get('join', 'AND') not in {'AND', 'OR'}):
            raise ValueError('Invalid Step condition type/join')
        common = {'type', 'join', 'roi', 'roi_tag'}
        specific = {'coco': {'feature', 'threshold', 'match'}, 'color': {'rgb', 'tolerance', 'min_pixels', 'match'},
                    'ocr': {'text', 'threshold', 'match'}}[row['type']]
        if set(row) - common - specific or ('roi' in row and 'roi_tag' in row):
            raise ValueError('Invalid Step condition fields or conflicting ROI')
        if 'roi' in row:
            validate_roi(row['roi'])
        if 'roi_tag' in row and (not isinstance(row['roi_tag'], str) or not TAG_ID.fullmatch(row['roi_tag'])):
            raise ValueError('Invalid condition TAG')
        if row['type'] in {'coco', 'ocr'}:
            score = row.get('threshold', .8)
            if type(score) not in (int, float) or not math.isfinite(score) or not .5 <= score <= 1:
                raise ValueError('Invalid condition confidence')
        if row['type'] == 'coco':
            if (not isinstance(row.get('feature'), str) or row['feature'] not in allowed
                    or not isinstance(row.get('match', 'detected'), str)
                    or row.get('match', 'detected') not in {'detected', 'disappeared'}):
                raise ValueError('Invalid COCO condition')
        elif row['type'] == 'ocr':
            if (not isinstance(row.get('text'), str) or not row['text'].strip() or len(row['text']) > 200
                    or not isinstance(row.get('match', 'contains'), str)
                    or row.get('match', 'contains') not in {'contains', 'exact'}):
                raise ValueError('Invalid OCR condition')
        else:
            from .color_target import validate_color
            validate_color(color_parameters(row))
            if (not isinstance(row.get('match', 'detected'), str)
                    or row.get('match', 'detected') not in {'detected', 'disappeared'}):
                raise ValueError('Invalid color condition match')


def color_parameters(row):
    return {'color_mode': 'only', 'color_rgb': row.get('rgb'),
            'color_tolerance': row.get('tolerance', 10), 'color_min_pixels': row.get('min_pixels', 9)}


def resolve(group, module):
    result = copy.deepcopy(group)
    for row in result['clauses']:
        if row.get('type')=='padding_coco':
            from .padding_coco import resolve as resolve_padding
            resolve_padding(row,module)
            continue
        tag = row.pop('roi_tag', None)
        if tag:
            if tag not in module.get('tags', {}):
                raise ValueError('Missing condition module-local TAG')
            row['roi'] = list(module['tags'][tag]['roi'])
        elif 'roi' not in row and module.get('global_roi') is not None:
            row['roi'] = list(module['global_roi'])
        elif 'roi' not in row:
            row['roi'] = [0, 0, 1080, 2400]
    return result


def evaluate(group, frame, task):
    """All rows use frame identity; valid unmatched OCR is NO, exceptions UNKNOWN."""
    from .canvas_entry_guard import coco_allowed, ocr_allowed
    from .color_target import components
    rows = []
    value = None
    for index, clause in enumerate(group['clauses']):
        row = copy.deepcopy(clause)
        try:
            roi = clause.get('roi', [0, 0, 1080, 2400])
            effective = getattr(task, 'recognition_roi', None)
            row['configured_roi'] = list(roi)
            row['effective_roi'] = (effective(roi, clause['type']) if callable(effective)
                                    and clause['type'] in {'coco', 'ocr'} else list(roi))
            if clause['type'] == 'coco':
                matches = task.detect_manual_features(frame, [clause['feature']], clause.get('threshold', .8), coco_roi=roi)
                result = coco_allowed(matches, clause)
                row['detections'] = matches
            elif clause['type'] == 'color':
                hits = components(frame, color_parameters(clause), roi)
                result = bool(hits)
                if clause.get('match') == 'disappeared':
                    result = not result
                row['components'] = hits[:100]
            else:
                from ok import Box
                boxes = task.ocr(box=Box(*roi), frame=frame, threshold=clause.get('threshold', .8))
                from .ocr_records import box_tokens
                tokens = box_tokens(boxes)
                row['tokens'] = tokens
                result = ocr_allowed(tokens, dict(clause, threshold=clause.get('threshold', .8), match=clause.get('match', 'contains')))
            row['reason'] = None if result is not None else 'Recognition unavailable or invalid'
        except Exception as exc:
            result = None
            row['reason'] = type(exc).__name__ + ': ' + str(exc)[:200]
        row['result'] = 'UNKNOWN' if result is None else 'YES' if result else 'NO'
        if clause['type']=='ocr':
            from .ocr_records import attach,publish
            attach(row,row.get('tokens'),roi=row.get('effective_roi'),configured_roi=roi,
                threshold=clause.get('threshold',.8),route='step_condition')
            row['ocr_record']['stage']='parsed';publish(task,row['ocr_record'],frame)
        rows.append(row)
        value = result if index == 0 else combine(value, result, clause.get('join', 'AND'))
    return {'evaluation': 'left_to_right', 'result': 'UNKNOWN' if value is None else 'YES' if value else 'NO', 'clauses': rows}
