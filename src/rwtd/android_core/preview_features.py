"""Preview scope follows Canvas references, never the entire asset catalog by default."""

def referenced_features(graph, modules):
    found = set()
    def step_features(step):
        if step.get('color_mode') == 'only':
            return
        for key in ('feature', 'verify_feature', 'previous_feature'):
            if step.get(key):
                found.add(step[key])
        found.update(step.get('features', []))
    for node in (graph or {}).get('nodes', []):
        if node.get('entry_guard'):
            from src.rwtd.android_core.canvas_entry_guard import clauses
            found.update(c['feature'] for c in clauses(node['entry_guard']) if c['kind']=='coco')
        kind = node.get('kind')
        if kind == 'coco' or (kind == 'exclusion' and node.get('exclusion_mode', 'coco') in {'coco', 'coco_or_color'}) or (kind == 'guard' and node.get('feature')):
            if node.get('feature'):
                found.add(node['feature'])
        if kind == 'ocr' and node.get('ocr_region') == 'coco' and node.get('ocr_feature'):
            found.add(node['ocr_feature'])
        if kind == 'action':
            for row in node.get('feature_map', []):
                found.update(row.get('features', []))
        if kind in {'module', 'action'}:
            module = modules.get(node.get('module'), {})
            for step in module.get('steps', []):
                if kind == 'module' or step.get('step_id') == node.get('step_id'):
                    step_features(step)
    return found
