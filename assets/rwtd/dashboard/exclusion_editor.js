// NEXT-scoped exclusions share Canvas persistence, not module-global settings.
window.ExclusionEditor={
 validate(n){CanvasModel.validateExclusion(n);},
 mount(el,locked){
  const host=document.createElement('fieldset');host.id='exclusionSettings';
  host.innerHTML='<legend>禁止點擊區域</legend><label>作用範圍<select id="exclusionScope" data-graph-edit><option value="next">只限制 NEXT 節點這次執行</option><option value="remember">舊版：跨節點記住 COCO 區域</option></select></label><label>來源<select id="exclusionMode" data-graph-edit><option value="coco">COCO 配對框</option><option value="roi">固定 ROI</option><option value="color">顏色</option><option value="coco_or_color">顏色 OR COCO</option></select></label><label>ROI：x,y,width,height<input id="exclusionRoi" data-graph-edit placeholder="0,0,1080,2400"></label><div id="exclusionColorRows"></div><button id="exclusionAddColor" type="button" data-graph-edit>＋禁用顏色</button><p>顏色 OR COCO 合併兩種禁區。NEXT 結束後失效；重新經過本節點會重新辨識。固定 ROI 會禁止整個框，顏色只禁止命中的像素。顏色採用 RGB 每通道 ±容差。</p>';
  el('guardFeature').closest('label').before(host);
  const edit=document.createElement('button');edit.type='button';edit.textContent='框選禁區／搜尋 ROI';edit.dataset.graphEdit='';
  const preview=document.createElement('div');preview.id='exclusionRoiEditor';preview.hidden=true;
  preview.innerHTML='<div class="frameBox" style="width:min(100%,480px)"><img id="exclusionRoiImage" alt="框選禁止點擊區域"><svg id="exclusionRoiBoxes" viewBox="0 0 1080 2400"></svg></div><div class="fields"><input id="exclusionRoiX" type="number" aria-label="禁區 X" data-graph-edit><input id="exclusionRoiY" type="number" aria-label="禁區 Y" data-graph-edit><input id="exclusionRoiW" type="number" aria-label="禁區寬" data-graph-edit><input id="exclusionRoiH" type="number" aria-label="禁區高" data-graph-edit></div>';
  el('exclusionRoi').closest('label').after(edit,preview);
  const roiEditor=createRoiEditor(el('exclusionRoiBoxes'),['exclusionRoiX','exclusionRoiY','exclusionRoiW','exclusionRoiH'].map(el),roi=>{if(roi)el('exclusionRoi').value=roi.join(',');},()=>!locked()&&!preview.hidden);
  edit.onclick=()=>{if(locked())return;const image=el('matchFrame');if(image.hidden||!image.src||!image.complete){el('exclusionRoi').setCustomValidity('請先刷新畫面再框選');el('exclusionRoi').reportValidity();el('exclusionRoi').setCustomValidity('');return;}el('exclusionRoiImage').src=image.src;roiEditor.set(el('exclusionRoi').value.split(',').map(Number));preview.hidden=false;};
  let colors=[];
  const render=()=>{el('exclusionColorRows').replaceChildren();for(const [index,c] of colors.entries()){
   const row=document.createElement('div');row.className='toolbar';
   for(const [label,key,type,value] of [['顏色','rgb','color','#'+c.rgb.map(v=>v.toString(16).padStart(2,'0')).join('')],['±容差','tolerance','number',c.tolerance??10],['最小連通像素','min_pixels','number',c.min_pixels??9]]){
    const l=document.createElement('label'),i=document.createElement('input');l.textContent=label;i.type=type;i.value=value;i.dataset.graphEdit='';i.disabled=locked();if(type==='number'){i.min=key==='tolerance'?0:1;i.max=key==='tolerance'?255:2592000;i.step=1;}i.oninput=()=>{if(locked())return;c[key]=key==='rgb'?i.value.slice(1).match(/../g).map(v=>parseInt(v,16)):Number(i.value);};l.append(i);row.append(l);
   }const remove=document.createElement('button');remove.type='button';remove.textContent='移除';remove.dataset.graphEdit='';remove.disabled=locked();remove.onclick=()=>{if(!locked()){colors.splice(index,1);render();}};row.append(remove);el('exclusionColorRows').append(row);
  }};
  const fields=()=>{const mode=el('exclusionMode').value,color=['color','coco_or_color'].includes(mode),coco=['coco','coco_or_color'].includes(mode);el('exclusionColorRows').hidden=!color;el('exclusionAddColor').hidden=!color;el('guardFeature').closest('label').hidden=!coco;el('guardThreshold').closest('label').hidden=!coco;};
  el('exclusionMode').onchange=()=>{if(el('exclusionMode').value!=='coco')el('exclusionScope').value='next';fields();};
  el('exclusionAddColor').onclick=()=>{if(!locked()&&colors.length<10){colors.push({rgb:[150,141,92],tolerance:25,min_pixels:25});render();}};
  return {fill(n){preview.hidden=true;host.hidden=n?.kind!=='exclusion';if(host.hidden)return;el('exclusionMode').value=n.exclusion_mode||'coco';el('exclusionScope').value=n.exclusion_scope||'remember';el('exclusionRoi').value=(n.roi||[0,0,1080,2400]).join(',');colors=structuredClone(n.exclusion_colors||[{rgb:[150,141,92],tolerance:25,min_pixels:25},{rgb:[201,176,119],tolerance:35,min_pixels:25}]);render();fields();},read(n){n.exclusion_mode=el('exclusionMode').value;n.exclusion_scope=el('exclusionScope').value;if(n.exclusion_mode!=='coco'||el('exclusionRoi').value.trim())n.roi=el('exclusionRoi').value.split(',').map(Number);if(['color','coco_or_color'].includes(n.exclusion_mode))n.exclusion_colors=structuredClone(colors);if(!['coco','coco_or_color'].includes(n.exclusion_mode)){delete n.feature;delete n.threshold;}ExclusionEditor.validate(n);}};
 }
};
