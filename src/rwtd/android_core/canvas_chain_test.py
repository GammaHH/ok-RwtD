"""Isolate one operation chain for an explicit Dashboard test."""
import copy
from src.rwtd.android_core.canvas_scheduler import validate_canvas, migrate_graph
from src.rwtd.android_core.canvas_chains import definitions


def test_graph(graph, start_id, modules):
    graph = validate_canvas(migrate_graph(graph), modules)
    start = next((n for n in graph['nodes'] if n['id'] == start_id), None)
    if not start or start['kind'] != 'start' or start.get('task_type') == 'TriggerTask':
        raise ValueError('Chain test requires an operation start')
    # An unregistered start can also be tested without changing its saved settings.
    source = copy.deepcopy(graph)
    source['entry'] = start_id
    for n in source['nodes']:
        if n['id'] != start_id:
            n.pop('chain', None)
    selected = next(c for c in definitions(source) if c['id'] == start_id)
    result = selected['graph']
    entry = next(n for n in result['nodes'] if n['id'] == start_id)
    if selected['run_limit'] > 1 or 'repeat_runs' in start:
        entry['repeat_runs'] = selected['run_limit']
    policy = {k:copy.deepcopy(v) for k,v in selected.items() if k not in {'graph','next'}}
    return validate_canvas(result, modules), policy
