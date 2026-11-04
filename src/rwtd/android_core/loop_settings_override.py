"""Optional OK task settings for a single Canvas execution.

The saved Canvas remains authoritative unless its OK task explicitly chooses
an in-memory override. Never mutate or save the caller's graph here.
"""

import copy
import math


SOURCE_KEY = 'Canvas Loop Settings Source'
MAX_KEY = 'Canvas Max Cycles (0=Unlimited)'
WAIT_KEY = 'Canvas Retry Wait (s)'
HANDOFF_KEY = 'Canvas Node Handoff (s)'
TIMEOUT_KEY = 'Canvas Chain Timeout (min)'

DEFAULTS = {
    SOURCE_KEY: 'Canvas',
    MAX_KEY: 10,
    WAIT_KEY: 1.0,
    HANDOFF_KEY: 1.0,
    TIMEOUT_KEY: 25.0,
}

DESCRIPTIONS = {
    SOURCE_KEY: 'Canvas：使用所選 Canvas 存檔；OK Override：僅覆蓋本次執行，不寫入 Canvas。',
    MAX_KEY: 'OK Override 時使用。每節點最多嘗試／循環次數，1～1000；0 代表不限次數。',
    WAIT_KEY: 'OK Override 時使用。UNKNOWN／回圈重試等待秒數，0.1～300。',
    HANDOFF_KEY: 'OK Override 時使用。NEXT／YES／NO 節點交接至少等待秒數，0～30；預設 1。',
    TIMEOUT_KEY: 'OK Override 時使用。每個 CHAIN 的執行時長上限，預設 25 分鐘；超時記一次失敗並直接交接，回訪重新計時。',
}


def settings_from_ok(config):
    source = config.get(SOURCE_KEY, 'Canvas')
    if source == 'Canvas':
        return None
    if source != 'OK Override':
        raise ValueError('Invalid Canvas loop settings source')
    maximum = config.get(MAX_KEY, 10)
    if type(maximum) is not int or maximum < 0 or maximum > 1000:
        raise ValueError('Canvas Max Cycles must be an integer from 0 to 1000')
    values = {}
    for key, field, low, high in (
        (WAIT_KEY, 'round_wait_seconds', .1, 300),
        (HANDOFF_KEY, 'node_handoff_seconds', 0, 30),
        (TIMEOUT_KEY, 'chain_timeout_minutes', .01, 10080),
    ):
        value = config.get(key, DEFAULTS[key])
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
        values[field] = float(value)
    values['max_cycles'] = maximum or None
    return values


def apply_ok_settings(graph, override):
    if override is None:
        return graph
    result = copy.deepcopy(graph)
    result.setdefault('settings', {}).update(override)
    if 'chain_timeout_minutes' in override:
        result['settings'].pop('exit_condition', None)
    return result
