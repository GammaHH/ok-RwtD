"""Resolve the retreat milestone using the owning Farm's stage, not event names."""
import copy
import re

FIELD = 'retreatable_km'
DEFAULTS = {1:100, 2:100, 3:100, 4:100, 5:200}


def stage_from_name(name):
    match = re.search(r'第([一二三四五1-5])關', name or '')
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else '一二三四五'.index(value)+1


def with_stage(graph, stage, *, inherit=False):
    result = copy.deepcopy(graph)
    if stage is not None:
        if type(stage) is not int or stage not in DEFAULTS:
            raise ValueError('Game stage must be 1..5')
        settings = result.setdefault('settings', {})
        if inherit or 'game_stage' not in settings:
            settings['game_stage'] = stage
    return result


def guard_rules(graph):
    rules=[]
    for node in graph['nodes']:
        guard=node.get('entry_guard',{})
        rules.extend(c for c in guard.get('conditions',[guard])
                     if c.get('kind')=='variable' and c.get('source')=='shared'
                     and c.get('variable')=='RetreatableKM')
    return rules


def resolve_graph(graph):
    """Execution-only projection; saved preset references remain dynamic."""
    result = copy.deepcopy(graph)
    stage = result.get('settings', {}).get('game_stage')
    rules = []
    for node in result['nodes']:
        if node.get('field') == FIELD and not node.get('variable'):
            rules.append(node)
        rule = node.get('chain', {}).get('continue_condition')
        if rule and rule.get('field') == FIELD:
            rules.append(rule)
    if result.get('settings', {}).get('exit_condition', {}).get('field') == FIELD:
        rules.append(result['settings']['exit_condition'])
    guards=guard_rules(result)
    if (rules or guards) and (type(stage) is not int or stage not in DEFAULTS):
        raise ValueError('Retreatable KM 需要主 FARM 關卡（第一～第五關）；單獨測試活動請先設定關卡')
    for rule in rules:
        # Reuse the complete PROGRESS capture, ROI, UNKNOWN and count policy.
        rule['field'] = 'progress_km'
        rule['value'] = DEFAULTS[stage]
    for rule in guards:
        rule.update(variable='ProgressKM',value=DEFAULTS[stage])
    return result
