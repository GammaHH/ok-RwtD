"""Bounded same-frame Solver Guard evidence, independent of execution decisions."""
import math


def describe(node,result):
    try:
        return _describe(node,result)
    except Exception as exc:
        # Diagnostic formatting must never stop an observer or change a Guard.
        return {'node':node.get('id'),'frame_id':result.get('frame_id') if isinstance(result,dict) else None,
                'result':'UNKNOWN','reason':'diagnostic_error:'+type(exc).__name__,'conditions':[]}


def _describe(node,result):
    from .canvas_entry_guard import clauses,result_allowed
    result=result if isinstance(result,dict) else {}
    allowed=result_allowed(result)
    traces={r['index']:r.get('result') for r in result.get('guard_trace',[])
            if isinstance(r,dict) and type(r.get('index')) is int}
    evidence={r['clause_index']:r for r in result.get('recognition',[])
              if isinstance(r,dict) and type(r.get('clause_index')) is int}
    rows=[]
    def score(value):
        return value if type(value) in (int,float) and math.isfinite(value) else None
    for index,clause in enumerate(clauses(node.get('entry_guard',{}))[:10]):
        sample=evidence.get(index,{})
        value=traces.get(index)
        state=('YES' if value is True else 'NO' if value is False else 'UNKNOWN') if index in traces else 'NOT_EVALUATED'
        best=score(sample.get('best_confidence'))
        if best is None:best=score(sample.get('confidence'))
        tokens=[{'text':str(t.get('text',''))[:200],'confidence':score(t.get('confidence'))}
                for t in sample.get('tokens',[])[:10] if isinstance(t,dict)]
        if best is None and tokens:
            best=max((t['confidence'] for t in tokens if t['confidence'] is not None),default=None)
        reason=sample.get('reason') or sample.get('confidence_reason')
        if index not in traces:
            reason=('short_circuit' if traces else result.get('reason') or 'no_clause_evidence')
        elif state=='UNKNOWN' and not reason:reason='condition_result_unknown'
        elif best is None and clause['kind'] in {'coco','ocr'} and not reason:
            reason='highest_score_not_returned'
        rows.append({'index':index+1,'kind':clause['kind'],
                     'target':clause.get('feature') or clause.get('text') or clause.get('variable') or clause.get('mode'),
                     'match':clause.get('match') or clause.get('op'),
                     'threshold':clause.get('threshold'),'result':state,'best_confidence':best,
                     'configured_roi':sample.get('configured_roi',clause.get('roi')),
                     'effective_roi':sample.get('roi'),'reason':str(reason)[:240] if reason else None,
                     'tokens':tokens})
    return {'node':node['id'],'frame_id':result.get('frame_id'),
            'operator':node.get('entry_guard',{}).get('operator','and'),
            'result':'YES' if allowed is True else 'NO' if allowed is False else 'UNKNOWN',
            'reason':str(result.get('reason') or '')[:240] or None,'conditions':rows}
