"""Canvas-local binding to a saved tap step; never writes module definitions."""
import copy
import math

from src.rwtd.android_core.module_store import identifier
from src.rwtd.android_core.workflow_model import resolve_step, validate_roi


def validate_action_node(node, modules):
    from src.rwtd.android_core.canvas_repeat import runs
    runs(node)
    if node.get('execution_scope','single') not in {'single','to_end','range'}:
        raise ValueError('Action execution scope must be single, to_end or range')
    if node.get('input_source','variable') not in {'step','variable'}:
        raise ValueError('Invalid action input source')
    if 'threshold' in node:
        value = node['threshold']
        if type(value) not in (int,float) or not math.isfinite(value) or not .5 <= value <= 1:
            raise ValueError('Action COCO threshold must be 0.5..1')
    identifier(node.get('module'))
    if not isinstance(node.get('step_id'), str) or not node['step_id'] or len(node['step_id']) > 80:
        raise ValueError('Action requires a saved Step ID')
    if node.get('execution_scope') == 'range' and not isinstance(node.get('end_step_id'), str):
        raise ValueError('Action range requires an ending Step ID')
    if isinstance(modules, dict):
        action_steps(node, modules)
    if node.get('input_source','variable') == 'step':
        if node.get('variable') or node.get('feature_map'):
            raise ValueError('Direct Step action must not retain variable mapping; choose one source')
        return
    if not node.get('variable'):
        raise ValueError('Variable mapping action requires an OCR variable')
    rows = node.get('feature_map')
    if not isinstance(rows,list) or not 1 <= len(rows) <= 100:
        raise ValueError('Action requires a variable-to-COCO mapping')
    values = set()
    for row in rows:
        if (not isinstance(row,dict) or not {'value','features'}.issubset(row)
                or set(row) - {'value','features','threshold','roi','roi_source'}):
            raise ValueError('Invalid Canvas COCO mapping row')
        if 'threshold' in row:
            score = row['threshold']
            if type(score) not in (int,float) or not math.isfinite(score) or not .5 <= score <= 1:
                raise ValueError('Mapping COCO threshold must be 0.5..1')
        if row.get('roi') is not None:
            validate_roi(row['roi'])
        if row.get('roi_source','shared') not in {'shared','module'}:
            raise ValueError('Invalid mapping ROI source')
        word, features = row['value'], row['features']
        if not isinstance(word,str) or not word or len(word) > 100 or word in values:
            raise ValueError('Duplicate/empty mapping value')
        values.add(word)
        if not isinstance(features,list) or not 1 <= len(features) <= 30 or any(not isinstance(f,str) for f in features) or len(set(features)) != len(features):
            raise ValueError('Mapping requires unique COCO Features')
        for feature in features:
            identifier(feature)
    if isinstance(modules,dict):
        module = modules.get(node['module'])
        steps = [s for s in (module or {}).get('steps',[]) if s.get('step_id') == node['step_id']]
        if len(steps) != 1 or steps[0].get('name') not in {'TapTest','LongPressTest','CocoTargetTest'}:
            raise ValueError('Select one saved tap/hold step')


def action_steps(node, modules):
    module = modules.get(node['module'])
    steps = (module or {}).get('steps', [])
    ids = [s.get('step_id') for s in steps]
    if ids.count(node['step_id']) != 1:
        raise ValueError('Select an existing unique saved Step ID')
    start = ids.index(node['step_id'])
    scope = node.get('execution_scope', 'single')
    end = len(steps) - 1 if scope == 'to_end' else start
    if scope == 'range':
        if ids.count(node.get('end_step_id')) != 1:
            raise ValueError('Select an existing unique ending Step ID')
        end = ids.index(node['end_step_id'])
        if end < start:
            raise ValueError('Action range end must follow its start in module order')
    return steps[start:end + 1]


def bind_action(node, variables, modules, feature_rois=None):
    validate_action_node(node,modules)
    module = modules[node['module']]
    selected = action_steps(node, modules)
    if node.get('input_source','variable') == 'step':
        from src.rwtd.android_core.canvas_repeat import runs
        steps = [resolve_step(module, copy.deepcopy(s)) for s in selected]
        return {'parameters': copy.deepcopy(steps[0]),
            'steps': [copy.deepcopy(s) for _ in range(runs(node)) for s in steps],
            'binding': {'node':node['id'], 'module':node['module'], 'step_id':node['step_id'],
                'input_source':'step', 'execution_scope':node.get('execution_scope','single'),
                'step_ids':[s['step_id'] for s in selected], 'module_revision':module.get('revision'),
                'repeat_runs':runs(node), 'variable':None, 'value':None, 'features':[], 'frame_id':None}}
    sample = variables.get(node.get('variable'),{})
    if sample.get('verified') is not True or not isinstance(sample.get('value'),str):
        raise ValueError('Action input variable UNKNOWN')
    row = next((r for r in node['feature_map'] if r['value'] == sample['value']),None)
    if not row:
        raise ValueError('No COCO mapping for the received variable; no guessed input')
    module = modules[node['module']]
    saved = next(s for s in module['steps'] if s['step_id'] == node['step_id'])
    parameters = resolve_step(module,copy.deepcopy(saved))
    if parameters.get('color_mode') == 'only':
        raise ValueError('Select a tap step compatible with COCO targeting')
    for key in ('feature','feature_priority','workflow_module','roi_tag','coco_feature_rois'):
        parameters.pop(key,None)
    parameters.update(features=copy.deepcopy(row['features']),recognize_before=True,
                      allow_unconfirmed_target=False,verification_policy='manual')
    if 'threshold' in node:
        parameters['threshold'] = node['threshold']
    if 'threshold' in row:
        parameters['threshold'] = row['threshold']
    if 'roi' in row:
        if row['roi'] is None:
            parameters.pop('coco_roi',None)  # Explicit full frame, even if module has a TAG/global ROI.
        else:
            parameters['coco_roi'] = copy.deepcopy(row['roi'])
    elif row.get('roi_source','shared') == 'shared' and any(f in (feature_rois or {}) for f in row['features']):
        fallback = parameters.pop('coco_roi',None)
        parameters['coco_feature_rois'] = {f:copy.deepcopy((feature_rois or {}).get(f,fallback))
                                          for f in row['features']}
    parameters.setdefault('target_policy','all_top_to_bottom')
    if parameters['target_policy'] == 'single' and len(row['features']) > 1:
        parameters['feature_priority'] = copy.deepcopy(row['features'])
    scope = node.get('execution_scope','single')
    start = next(i for i,s in enumerate(module['steps']) if s['step_id']==node['step_id'])
    steps = [copy.deepcopy(parameters)]
    if scope in {'to_end','range'}:
        steps.extend(resolve_step(module,copy.deepcopy(s)) for s in selected[1:])
    from src.rwtd.android_core.canvas_repeat import runs
    steps=[copy.deepcopy(s) for _ in range(runs(node)) for s in steps]
    return {'parameters':parameters, 'steps':steps, 'binding':{'node':node['id'],'module':node['module'],
        'step_id':node['step_id'],'module_revision':module.get('revision'),
        'execution_scope':scope,'step_ids':[s['step_id'] for s in selected],
        'repeat_runs':runs(node),
        'variable':node['variable'],'value':sample['value'],'features':copy.deepcopy(row['features']),
        'frame_id':sample.get('frame_id')}}
