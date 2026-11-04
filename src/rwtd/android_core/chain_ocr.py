"""Shared Chain numeric reading defaults for Farm and Dashboard tests."""
import math
import re
from .canvas_scheduler import PROGRESS_OCR_ROI
from .workflow_model import validate_roi


def reading_roi(rule):
    roi = rule.get('roi')
    if roi is None and rule.get('field') == 'progress_km':
        roi = PROGRESS_OCR_ROI
    if roi is None:
        return None
    validate_roi(roi)
    return list(roi)


def numeric_result(boxes):
    """Explain an ambiguous/empty numeric OCR without inventing a reading."""
    tokens, numbers = [], []
    for box in boxes:
        text = str(box.name or '')
        confidence = getattr(box, 'confidence', None)
        tokens.append({'text':text[:200], 'confidence':confidence if
                       type(confidence) in (int,float) and math.isfinite(confidence) else None})
        numbers.extend(float(value) for value in re.findall(r'\d+(?:\.\d+)?', text.replace(',', '')))
    valid = len(numbers)==1 and math.isfinite(numbers[0])
    result={'tokens':tokens[:20], 'number':numbers[0] if valid else None,
            'number_count':len(numbers), 'result':'VALID' if valid else 'UNKNOWN',
            'reason':None if valid else 'no_numeric_token' if not numbers else 'ambiguous_or_nonfinite_number'}
    from .ocr_records import attach,box_tokens
    attach(result,box_tokens(boxes),route='chain_numeric')
    result['ocr_record']['stage']='parsed'
    return result
