"""Read native COCO captures in reference space without rewriting user assets."""
from .screen_geometry import ScreenGeometry


def extract(source,bbox,frame_size,*,warp=True):
    x,y,w,h=bbox
    source_h,source_w=source.shape[:2]
    if (w<=0 or h<=0 or x<0 or y<0 or x+w>source_w or y+h>source_h):
        raise ValueError('Invalid template bbox in its declared source image')
    if (source_w,source_h)==tuple(frame_size):
        return source[y:y+h,x:x+w].copy(),list(bbox),False
    if tuple(frame_size)!=(1080,2400):
        raise ValueError('Native template conversion requires reference 1080x2400 frame')
    geometry=ScreenGeometry(source_w,source_h)
    left,top=geometry.reference_point(x,y)
    right,bottom=geometry.reference_point(x+w-1,y+h-1)
    mapped=[left,top,right-left+1,bottom-top+1]
    if warp:
        normalized=geometry.normalize(source)
        template=normalized[top:bottom+1,left:right+1].copy()
    else:
        # Icon-size opt-out is independent of coordinate conversion.
        template=source[y:y+h,x:x+w].copy()
    return template,mapped,True
