"""Explicit fixed executions, independent of condition retry limits."""


def runs(node):
    value=node.get('repeat_runs',1)
    if type(value) is not int or not 1<=value<=100:
        raise ValueError('Fixed node executions must be integer 1..100')
    return value


def repeat_node(node, counters):
    count=counters.get(node['id'],0)+1
    if count<runs(node):
        counters[node['id']]=count
        return True
    counters.pop(node['id'],None)
    return False
