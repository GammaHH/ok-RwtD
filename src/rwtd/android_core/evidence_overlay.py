"""Annotate a copy of the analyzed pixels using recognition-time ROI metadata."""
import copy
import cv2
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def unicode_font():
    from PIL import ImageFont
    font=Path('C:/Windows/Fonts/msjh.ttc')
    return ImageFont.truetype(str(font),21) if font.is_file() else None


def regions(details):
    """Bounded extraction of structured recognition evidence, not prose parsing."""
    result=[]
    def walk(value,kind='coco',label='',depth=0):
        if depth>8 or len(result)>=80:return
        if isinstance(value,list):
            for row in value[:100]:walk(row,kind,label,depth+1)
            return
        if not isinstance(value,dict):return
        kind='ocr' if value.get('kind',value.get('type'))=='ocr' or value.get('ocr_target') else kind
        label=str(value.get('label') or value.get('feature') or value.get('text') or value.get('anchor_feature') or value.get('output_key') or label or kind.upper())[:100]
        roi=next((value[k] for k in ('effective_roi','search_bbox','b_band') if isinstance(value.get(k),list)),None)
        if roi is not None:
            item={'kind':kind,'label':label,'roi':copy.deepcopy(roi)}
            if item not in result:result.append(item)
        for key in ('recognition','input_conditions','target_conditions','clauses','detections','candidates','check_detections'):
            if key in value:walk(value[key],kind,label,depth+1)
    walk(details)
    return result[:80]


def draw(frame,items,node):
    """Reference-space metadata only; no scaling guesses for a physical frame."""
    output=frame.copy();used=[];unicode_labels=[]
    if frame.shape[:2]!=(2400,1080):return output,used
    for item in items[:80]:
        roi=item.get('roi')
        if (not isinstance(roi,list) or len(roi)!=4 or any(type(v) is not int for v in roi)):
            continue
        x,y,w,h=roi
        if x<0 or y<0 or w<=0 or h<=0 or x+w>1080 or y+h>2400:continue
        spec={'kind':item.get('kind','coco'),'label':str(item.get('label','ROI'))[:100],'roi':roi[:]}
        if spec in used:continue
        used.append(spec)
        color=(255,200,0) if spec['kind']=='ocr' else (70,230,70)
        cv2.rectangle(output,(x,y),(x+w-1,y+h-1),color,3)
        text=f"{node} {spec['kind'].upper()} {spec['label']} [{x},{y},{w},{h}]"
        ascii_text=text.encode('ascii','replace').decode('ascii')
        size=cv2.getTextSize(ascii_text,cv2.FONT_HERSHEY_SIMPLEX,.48,1)[0]
        tx=min(x,max(0,1078-size[0]));ty=max(size[1]+4,min(2396,y-6))
        cv2.rectangle(output,(tx,max(0,ty-size[1]-4)),(min(1079,tx+size[0]+3),min(2399,ty+4)),(0,0,0),-1)
        if text.isascii():cv2.putText(output,text,(tx,ty),cv2.FONT_HERSHEY_SIMPLEX,.48,color,1,cv2.LINE_AA)
        else:unicode_labels.append((text,ascii_text,tx,ty,color))
    if unicode_labels:
        font=unicode_font()
        if font is not None:
            from PIL import Image,ImageDraw
            canvas=Image.fromarray(cv2.cvtColor(output,cv2.COLOR_BGR2RGB));painter=ImageDraw.Draw(canvas)
            for text,_,x,y,color in unicode_labels:
                bounds=painter.textbbox((0,0),text,font=font,anchor='lt')
                x=min(x,max(0,1078-(bounds[2]-bounds[0])))
                top=max(0,y-23)
                painter.rectangle((x,top,min(1079,x+bounds[2]-bounds[0]+3),min(2399,top+26)),fill=(0,0,0))
                painter.text((x,top),text,font=font,fill=color[::-1],anchor='lt')
            import numpy as np
            output=cv2.cvtColor(np.asarray(canvas),cv2.COLOR_RGB2BGR)
        else:
            for _,text,x,y,color in unicode_labels:cv2.putText(output,text,(x,y),cv2.FONT_HERSHEY_SIMPLEX,.48,color,1,cv2.LINE_AA)
    return output,used
