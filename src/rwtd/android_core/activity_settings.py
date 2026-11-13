"""Validate optional Farm activity insertion without modifying saved Canvases."""

import json

ENABLED_KEY = 'activity_enabled'
CANVAS_KEY = 'activity_canvas'
DAYS_KEY = 'activity_interval_days'
HOURS_KEY = 'activity_interval_hours'
MINUTES_KEY = 'activity_interval_minutes'
MAX_INTERVAL_SECONDS = 365 * 24 * 60 * 60
SCHEDULES_KEY = 'activity_schedules'

LEGACY_DEFAULTS = {
    ENABLED_KEY: False,
    CANVAS_KEY: '',
    DAYS_KEY: 0,
    HOURS_KEY: 0,
    MINUTES_KEY: 0,
}

DEFAULTS = {ENABLED_KEY: False, SCHEDULES_KEY: '[]'}

DESCRIPTIONS = {
    ENABLED_KEY: '啟用多活動插入；在正常 Farm 整輪完成後執行已到期的活動，活動完成後繼續 Farm。',
    SCHEDULES_KEY: '活動排程 JSON 陣列，可新增多項。每項設定 id、canvas（Canvas ID）、enabled、'
                   'initial_runs（首次連跑次數，預設 1；0 代表已完成，先等待間隔）、interval_days、interval_hours、interval_minutes、'
                   'max_pending_runs（定期排程最多累積場數，0 代表不限）。'
                   '可選 first_delay_days、first_delay_hours、first_delay_minutes：首次次數仍先執行，'
                   '首次倒數從排程啟用時計算，決定下一場回復時間，之後按原間隔累積；重啟保留。'
                   '首次在第一個正常整輪完成後插入，連跑 initial_runs 次；之後每次到期執行 1 次。間隔由天／小時／分鐘相加，'
                   '須大於 0 且不超過 365 天。依列表順序決定優先；[] 代表不排活動。範例：'
                   '[{"id":"event1","canvas":"CanvasID","initial_runs":3,"interval_minutes":190}]。'
                   '每項 enabled 留白視為啟用；總開關預設關閉，Canvas 和間隔須自行設定。',
}


def resolve_activity_config(config, catalog, main_canvas_id):
    """Return an explicit activity schedule, or None while disabled.

    Catalog entries follow DecisionGraphStore.catalog(): dictionaries containing
    id and name. Display labels are resolved by their stable ID, so renaming a
    saved Canvas does not invalidate an existing selection. The main Canvas is
    supplied by the caller after resolving its own 'current selection' option.
    """
    enabled = config.get(ENABLED_KEY, LEGACY_DEFAULTS[ENABLED_KEY])
    if type(enabled) is not bool:
        raise ValueError('活動啟用設定必須是布林值')
    if not enabled:
        return None

    selection = config.get(CANVAS_KEY, '')
    if not isinstance(selection, str) or not selection.strip():
        raise ValueError('請選擇活動 Canvas')
    canvas_id = selection.split(' · ', 1)[0].strip()
    known_ids = {row['id'] for row in catalog}
    if canvas_id not in known_ids:
        raise ValueError('活動 Canvas 不存在，請重新選擇')
    if canvas_id == main_canvas_id:
        raise ValueError('活動 Canvas 不能與主要 Farm Canvas 相同')

    total = 0
    for key, seconds in ((DAYS_KEY, 86400), (HOURS_KEY, 3600), (MINUTES_KEY, 60)):
        value = config.get(key, LEGACY_DEFAULTS[key])
        if type(value) is not int or value < 0:
            raise ValueError('活動間隔必須使用非負整數的天／小時／分鐘')
        total += value * seconds
    if not 0 < total <= MAX_INTERVAL_SECONDS:
        raise ValueError('活動總間隔必須大於 0 且不超過 365 天')
    return {'canvas_id': canvas_id, 'interval_seconds': total}


def resolve_activity_schedules(config, catalog, main_canvas_id):
    """Resolve an editable, arbitrary-length activity list in configured order.

    OK's native multiline editor stores JSON text. Structured lists are accepted
    for programmatic callers, but are not a native OK list-widget value. Legacy
    single-activity settings remain readable when the list key is absent.
    """
    if SCHEDULES_KEY not in config:
        legacy = resolve_activity_config(config, catalog, main_canvas_id)
        return [{'id': 'legacy', 'initial_runs': 1, **legacy}] if legacy else []
    enabled = config.get(ENABLED_KEY, False)
    if type(enabled) is not bool:
        raise ValueError('活動啟用設定必須是布林值')
    if not enabled:
        return []
    raw = config[SCHEDULES_KEY]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ValueError('活動排程必須是有效的 JSON 陣列') from exc
    if not isinstance(raw, list):
        raise ValueError('活動排程必須是 JSON 陣列')
    output = []
    seen = set()
    seen_canvases = set()
    allowed = {'id', 'canvas', 'enabled', 'initial_runs', 'interval_days', 'interval_hours', 'interval_minutes',
               'max_pending_runs', 'first_delay_days', 'first_delay_hours', 'first_delay_minutes'}
    for index, item in enumerate(raw, 1):
        prefix = f'活動排程第 {index} 項：'
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError(prefix + '包含不支援的欄位或格式')
        schedule_id = item.get('id')
        if not isinstance(schedule_id, str) or not schedule_id.strip() or len(schedule_id) > 80:
            raise ValueError(prefix + 'id 必須是 1～80 字元的名稱')
        schedule_id = schedule_id.strip()
        if schedule_id in seen:
            raise ValueError(prefix + 'id 不可重複')
        seen.add(schedule_id)
        candidate = {
            ENABLED_KEY: item.get('enabled', True),
            CANVAS_KEY: item.get('canvas', ''),
            DAYS_KEY: item.get('interval_days', 0),
            HOURS_KEY: item.get('interval_hours', 0),
            MINUTES_KEY: item.get('interval_minutes', 0),
        }
        try:
            resolved = resolve_activity_config(candidate, catalog, main_canvas_id)
        except ValueError as exc:
            raise ValueError(prefix + str(exc)) from exc
        if resolved:
            initial_runs = item.get('initial_runs', 1)
            if type(initial_runs) is not int or not 0 <= initial_runs <= 1000:
                raise ValueError(prefix + 'initial_runs 必須是 0～1000 的整數')
            max_pending_runs = item.get('max_pending_runs', 0)
            if type(max_pending_runs) is not int or not 0 <= max_pending_runs <= 1000:
                raise ValueError(prefix + 'max_pending_runs 必須是 0～1000 的整數')
            if max_pending_runs:
                resolved['max_pending_runs'] = max_pending_runs
            delay_units = (('first_delay_days', 86400), ('first_delay_hours', 3600),
                           ('first_delay_minutes', 60))
            if any(key in item for key, _ in delay_units):
                delay = 0
                for key, multiplier in delay_units:
                    value = item.get(key, 0)
                    if type(value) is not int or value < 0:
                        raise ValueError(prefix + '首次倒數必須使用非負整數的天／小時／分鐘')
                    delay += value * multiplier
                if delay > MAX_INTERVAL_SECONDS:
                    raise ValueError(prefix + '首次倒數不可超過 365 天')
                resolved['first_delay_seconds'] = delay
            if resolved['canvas_id'] in seen_canvases:
                raise ValueError(prefix + '同一活動 Canvas 不可重複排程')
            seen_canvases.add(resolved['canvas_id'])
            output.append({'id': schedule_id, 'initial_runs': initial_runs, **resolved})
    return output
