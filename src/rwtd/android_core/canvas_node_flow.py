"""Node continuation rules shared by every source of a skipped execution.

A Start opens its chain only when completed. Skipping it has no continuation.
Guards are one producer of a skip outcome; they do not own these rules.
"""


def skip_port(node):
    if node['kind'] in {'start', 'end'}:
        return None
    return 'NO' if node['kind'] in {'condition', 'coco', 'ocr_decision'} or node.get('ocr_cases') else 'NEXT'


def skip_reason(node, reason):
    return reason + ('；開始無效，整條 Chain 不啟動' if node['kind'] == 'start' else '')
