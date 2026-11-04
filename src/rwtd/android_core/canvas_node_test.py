"""Selected-node downstream projection; upstream definitions are never executed."""
import copy
from src.rwtd.android_core.canvas_scheduler import validate_canvas, ports
from src.rwtd.android_core.canvas_triggers import reachable

def test_graph(graph, node_id, modules, source, output_port=None):
    by_id={n['id']:n for n in graph['nodes']}
    if len(by_id)!=len(graph['nodes']) or node_id not in by_id:
        raise ValueError('Select one existing node')
    selected=by_id[node_id]
    origin=node_id
    if output_port is not None:
        if not isinstance(output_port,str) or output_port not in ports(selected):
            raise ValueError('Select an existing output port')
        links=[e for e in graph['edges'] if e['source']==node_id and e['port']==output_port]
        if len(links)!=1 or links[0]['target'] not in by_id:
            raise ValueError('Selected output requires exactly one connected input')
        node_id=links[0]['target']
        selected=by_id[node_id]
        if selected['kind']=='start':
            raise ValueError('開始節點只有出口，不能作為出口測試的目標入口')
    elif selected['kind'] not in {'module','action','ocr','coco','ocr_decision','condition','notify','wait'}:
        raise ValueError('This node cannot be tested independently')
    if selected['kind']=='guard':
        raise ValueError('A background Guard is not an executable input')
    downstream=reachable(graph,node_id)
    ids=downstream | {n['id'] for n in graph['nodes'] if n['kind'] in {'ocr','coco','ocr_decision'}}
    projected=copy.deepcopy(graph)
    projected['entry']=node_id
    projected['nodes']=[n for n in projected['nodes'] if n['id'] in ids and n['kind']!='guard']
    if source=='android_readonly' and any(n['kind'] in {'module','action'} for n in projected['nodes'] if n['id'] in downstream):
        raise ValueError('Module testing requires Manual Device Test')
    for n in projected['nodes']:
        if n['kind']=='start':
            for key in ('task_type','trigger_interval','preempt_operations','enabled','chain','repeat_runs','handoff_chain'):
                if key=='repeat_runs' and output_port is not None:
                    continue
                n.pop(key,None)
    kept={n['id'] for n in projected['nodes']}
    projected['edges']=[e for e in projected['edges'] if e['source'] in downstream and e['source'] in kept and e['target'] in kept]
    terminal='NodeTestEnd'
    while terminal in by_id: terminal+='X'
    edge_ids={e['id'] for e in projected['edges']}
    for n in projected['nodes']:
        if n['id'] not in downstream:
            for port in ports(n):
                eid='TestE'
                while eid in edge_ids: eid+='X'
                edge_ids.add(eid)
                projected['edges'].append({'id':eid,'source':n['id'],'port':port,'target':terminal})
    projected['nodes'].append({'id':terminal,'kind':'end'})
    projected.setdefault('ui',{})['positions']={k:v for k,v in projected.get('ui',{}).get('positions',{}).items() if k in kept}
    descriptor=copy.deepcopy(selected)
    if output_port is not None:
        descriptor.update(source_node=origin,output_port=output_port)
    return validate_canvas(projected,modules),descriptor
