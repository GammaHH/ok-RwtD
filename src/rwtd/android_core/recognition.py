"""Merge overlapping reference-template hits, retaining genuinely distinct targets."""

def incomplete_reason(detections,prefix='Recognition incomplete'):
    errors=[row for row in detections if row.get('status')!='SNAPSHOT_ONLY_NOT_EXECUTION_TARGET']
    return prefix+': '+'; '.join(str(row.get('feature','?'))+'='+str(row.get('status','UNKNOWN'))+
        (' ('+str(row['error'])[:160]+')' if row.get('error') else '') for row in errors[:8])

def consolidate(detections):
    results = []
    for item in sorted(detections, key=lambda hit: hit.get("confidence") or 0, reverse=True):
        if not item.get("matched"):
            continue
        x, y, w, h = item["bbox"]
        duplicate = None
        for old in results:
            if old["feature"] != item["feature"]:
                continue
            a, b, c, d = old["bbox"]
            intersection = max(0, min(x+w, a+c)-max(x, a)) * max(0, min(y+h, b+d)-max(y, b))
            union = w*h+c*d-intersection
            if union > 0 and intersection/union >= .65:
                duplicate = old
                break
        if duplicate is not None:
            duplicate["reference_annotation_ids"].append(item.get("annotation_id"))
        else:
            results.append(dict(item, reference_annotation_ids=[item.get("annotation_id")]))
    missing = {item["feature"] for item in detections} - {item["feature"] for item in results}
    for feature in sorted(missing):
        results.append(next(item for item in detections if item["feature"] == feature))
    return results
