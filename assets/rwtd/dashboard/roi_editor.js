'use strict';
// Pure native geometry: image element and SVG share exactly the same 1080:2400 box.
window.RoiGeometry={
 point(clientX,clientY,bounds){return [Math.round(Math.max(0,Math.min(1080,(clientX-bounds.left)*1080/bounds.width))),Math.round(Math.max(0,Math.min(2400,(clientY-bounds.top)*2400/bounds.height)))];},
 valid(r){return Array.isArray(r)&&r.length===4&&r.every(Number.isInteger)&&r[0]>=0&&r[1]>=0&&r[2]>0&&r[3]>0&&r[0]+r[2]<=1080&&r[1]+r[3]<=2400;},
 fromPoints(a,b){return [Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(1,Math.abs(a[0]-b[0])),Math.max(1,Math.abs(a[1]-b[1]))].map((v,i)=>i===0?Math.min(v,1079):i===1?Math.min(v,2399):v);}
};
window.createRoiEditor=function(svg,inputs,onChange,isEditable){let roi=null,reference=null,gesture=null,offsetY=0;
 const ns='http://www.w3.org/2000/svg';
 function render(){svg.replaceChildren();const shown=roi||reference;for(let i=0;i<4;i++)inputs[i].value=shown?.[i]??'';if(!shown)return;const[x,storedY,w,h]=shown,y=storedY+offsetY;let rect=document.createElementNS(ns,'rect');for(const[k,v]of Object.entries({x,y,width:w,height:h,fill:'#ffbf4720',stroke:'#ffc04c','stroke-width':6}))rect.setAttribute(k,v);rect.dataset.roiPart='move';svg.append(rect);for(const[index,point]of [[x,y],[x+w,y],[x+w,y+h],[x,y+h]].entries()){let corner=document.createElementNS(ns,'rect');for(const[k,v]of Object.entries({x:point[0]-18,y:point[1]-18,width:36,height:36,fill:'#ffc04c'}))corner.setAttribute(k,v);corner.dataset.roiPart=String(index);svg.append(corner);}}
 function canonicalPoint(event){const p=window.RoiGeometry.point(event.clientX,event.clientY,svg.getBoundingClientRect());return [p[0],Math.max(0,Math.min(2400,p[1]-offsetY))];}
 function changed(next){if(!window.RoiGeometry.valid(next))return;roi=next;render();onChange(roi);}
 for(const input of inputs)input.addEventListener('change',()=>{if(input.disabled)return;const next=inputs.map(field=>field.value===''?NaN:Number(field.value));if(window.RoiGeometry.valid(next))changed(next);else{render();onChange(roi,'ROI 超出原生範圍，未套用');}});
 svg.addEventListener('pointerdown',event=>{if(!isEditable()||event.button!==0)return;event.preventDefault();const point=canonicalPoint(event);gesture={start:point,original:(roi||reference)?.slice(),part:event.target.dataset.roiPart||'new'};svg.setPointerCapture(event.pointerId);});
 svg.addEventListener('pointermove',event=>{if(!gesture)return;const point=canonicalPoint(event),old=gesture.original;let next;if(gesture.part==='move'&&old){next=[Math.max(0,Math.min(1080-old[2],old[0]+point[0]-gesture.start[0])),Math.max(0,Math.min(2400-old[3],old[1]+point[1]-gesture.start[1])),old[2],old[3]];}else if(gesture.part!=='new'&&old){const opposite=[[old[0]+old[2],old[1]+old[3]],[old[0],old[1]+old[3]],[old[0],old[1]],[old[0]+old[2],old[1]]][Number(gesture.part)];next=window.RoiGeometry.fromPoints(point,opposite);}else next=window.RoiGeometry.fromPoints(gesture.start,point);changed(next);});
 function end(){gesture=null;}svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
 return {setOffset(value){if(offsetY!==value){offsetY=value;gesture=null;render();}},get:()=>roi?.slice()||null,referenceRoi:()=>reference?.slice()||null,set(value){roi=value?.slice()||null;gesture=null;render();},reference(value){reference=value?.slice()||null;if(!gesture&&!inputs.includes(document.activeElement))render();},reset(){roi=null;render();onChange(null);}};
};

