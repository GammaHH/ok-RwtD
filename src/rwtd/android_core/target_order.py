"""Deterministic single-position choices shared by all target recognizers."""
POSITION_POLICIES={'first_top_to_bottom','first_bottom_to_top'}


def choose(hits,policy):
    direction=-1 if policy=='first_bottom_to_top' else 1
    return sorted(hits,key=lambda hit:(direction*hit['bbox'][1],hit['bbox'][0],
                                      -(hit.get('confidence') or 0),hit.get('feature','')))[:1]
