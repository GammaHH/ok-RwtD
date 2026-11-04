"""Allowlisted Dashboard-only test context; never synthetic OCR observations."""
import copy
import math

FIELDS=[
    {'key':'game_stage','label':'測試關卡','type':'enum','options':[
        {'value':1,'label':'第一關（100 km）'},{'value':2,'label':'第二關（100 km）'},
        {'value':3,'label':'第三關（100 km）'},{'value':4,'label':'第四關（100 km）'},
        {'value':5,'label':'第五關（200 km）'}],
     'description':'只作為本次測試的預設條件來源，不儲存到 Canvas。'},
    {'key':'retreatable_km_threshold','label':'Retreat KM 測試門檻','type':'number','min':0,'max':1000000000,
     'description':'填寫後優先於關卡預設；目前進度仍由實機 OCR 讀取。'}]


def validate(values):
    registry={row['key']:row for row in FIELDS}
    if not isinstance(values,dict) or set(values)-set(registry):
        raise ValueError('Unknown Dashboard test preset field')
    for key,value in values.items():
        field=registry[key];kind=field['type']
        if kind=='enum':
            valid=any(type(value) is type(row['value']) and value==row['value'] for row in field['options'])
        elif kind in {'number','integer'}:
            valid=(type(value) in (int,float) and math.isfinite(value)
                and (kind!='integer' or type(value) is int)
                and field.get('min',float('-inf'))<=value<=field.get('max',float('inf')))
        elif kind=='boolean':valid=type(value) is bool
        elif kind=='text':valid=isinstance(value,str) and len(value)<=field.get('max_length',200)
        else:valid=False
        if not valid:raise ValueError('Invalid Dashboard test preset: '+key)
    return copy.deepcopy(values)


def project(graph,values,*,chain_id=None,node_id=None,explicit=False,scope=None):
    values=validate(values)
    if values and (explicit is not True or scope not in {None,'all','main'}
                   or ((chain_id or node_id) and scope!='main')):
        raise ValueError('Test presets require an explicitly initiated Dashboard Canvas/Chain/node test')
    result=copy.deepcopy(graph)
    if 'game_stage' in values:result.setdefault('settings',{})['game_stage']=values['game_stage']
    if 'retreatable_km_threshold' in values:
        rules=[]
        for node in result['nodes']:
            if node.get('field')=='retreatable_km' and not node.get('variable'):rules.append(node)
            rule=node.get('chain',{}).get('continue_condition')
            if rule and rule.get('field')=='retreatable_km':rules.append(rule)
        rule=result.get('settings',{}).get('exit_condition')
        if rule and rule.get('field')=='retreatable_km':rules.append(rule)
        for rule in rules:rule.update(field='progress_km',value=values['retreatable_km_threshold'])
        from .retreatable_km import guard_rules
        for rule in guard_rules(result):
            rule.update(variable='ProgressKM',value=values['retreatable_km_threshold'])
    return result
