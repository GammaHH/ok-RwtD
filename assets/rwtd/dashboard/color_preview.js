'use strict';
// Read-only RGB analysis of the same native frame used by the ROI editor.
window.ColorPreview={
 components(image,roi,rgb,tolerance,minimum){
  if(!image.complete||image.naturalWidth!==1080||image.naturalHeight!==2400)throw Error('等待原生編輯畫面載入');
  if(!RoiGeometry.valid(roi))throw Error('先設定 ROI');
  if(!Number.isInteger(tolerance)||tolerance<0||tolerance>255||!Number.isInteger(minimum)||minimum<1)throw Error('Tolerance／最小像素無效');
  const[x,y,w,h]=roi,canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;
  const context=canvas.getContext('2d',{willReadFrequently:true});context.drawImage(image,x,y,w,h,0,0,w,h);
  const pixels=context.getImageData(0,0,w,h).data,mask=new Uint8Array(w*h),queue=new Int32Array(w*h),hits=[];
  for(let i=0;i<mask.length;i++)mask[i]=Number([0,1,2].every(c=>Math.abs(pixels[i*4+c]-rgb[c])<=tolerance));
  for(let i=0;i<mask.length;i++)if(mask[i]){let head=0,tail=1,left=w,top=h,right=0,bottom=0;queue[0]=i;mask[i]=0;
   while(head<tail){const p=queue[head++],px=p%w,py=Math.floor(p/w);left=Math.min(left,px);right=Math.max(right,px);top=Math.min(top,py);bottom=Math.max(bottom,py);
    for(const n of [px>0?p-1:-1,px<w-1?p+1:-1,py>0?p-w:-1,py<h-1?p+w:-1])if(n>=0&&mask[n]){mask[n]=0;queue[tail++]=n;}}
   if(tail>=minimum)hits.push({bbox:[x+left,y+top,right-left+1,bottom-top+1],pixels:tail});
  }
  return hits;
 }
};
