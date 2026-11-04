"""Read-only OCR decisions using the existing OCR capture and classification path."""
import math

from .canvas_ocr import classify, validate_ocr_node


def probe(node):
    return {'id': node['id'], 'kind': 'ocr', 'output_key': 'DecisionValue',
            'roi': node.get('roi'), 'threshold': node.get('threshold', .8),
            'ocr_mode': 'raw', 'ocr_output_type': node.get('ocr_output_type', 'text'),
            '_ocr_decision_for': node['id']}


def validate(node):
    if any(key in node for key in ('ocr_cases','ocr_retry','ocr_region','ocr_feature',
                                  'ocr_coco_threshold','ocr_mode','ocr_trim','ocr_conversion')):
        raise ValueError('OCR decision uses output type, ROI, confidence and comparison only')
    kind = node.get('ocr_output_type', 'text')
    if kind not in {'text', 'number'}:
        raise ValueError('OCR decision supports text or number')
    value = node.get('value')
    if kind == 'text':
        if node.get('op') not in {'eq', 'contains'} or not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ValueError('OCR text decision requires expected text and eq/contains')
    elif (node.get('op') not in {'eq', 'gte', 'lte'} or type(value) not in (int, float)
          or not math.isfinite(value)):
        raise ValueError('OCR numeric decision requires a finite number and eq/gte/lte')
    validate_ocr_node(probe(node))


def evaluate(node, sample):
    result = classify(probe(node), sample)
    if result.get('verified') is not True:
        # An empty valid text reading is a non-match. Malformed/low confidence
        # and ambiguous numeric readings stay UNKNOWN, never guessed NO.
        if (node.get('ocr_output_type', 'text') == 'text' and isinstance(sample, dict)
                and sample.get('verified') is True and sample.get('frame_id')
                and sample.get('tokens') == []):
            return False, result
        return None, result
    value, expected = result['value'], node['value']
    op = node['op']
    return (value == expected if op == 'eq' else expected in value if op == 'contains'
            else value >= expected if op == 'gte' else value <= expected), result
