'use strict';
window.createTargetConditionEditor=function(onChange){
 const el=id=>document.getElementById(id);let extra=[],active=false,firstScope={},tagCatalog={},tagSignature='',firstKind=null;
 const defaultPadding=()=>({a_tag:'',b_tag:'',check_feature:'',check_threshold:.8,offset_x:0,offset_y:0,match:'disappeared'});
 let firstPadding=defaultPadding();
 const paddingHost=document.createElement('fieldset');paddingHost.id='paddingFirstSettings';el('featureSettings').after(paddingHost);
 el('targetMode').add(new Option('PADDING COCO','padding_coco'));
 const types=window.TargetRecognitionKinds,choices=Object.entries(types).map(([key,type])=>[key,type.label]);
 el('targetFirstKind').replaceChildren(...choices.map(([key,label])=>new Option(label,key)),new Option('固定座標','fixed'));
 function paddingControls(host,row){
  function select(title,options,value,change){const label=document.createElement('label'),control=document.createElement('select');label.append(document.createTextNode(title),control);for(const[v,t]of options)control.add(new Option(t,v));if(value&&!options.some(([v])=>v===value))control.add(new Option(value+'（未找到）',value));control.value=value||'';control.onchange=()=>{change(control.value);active=true;onChange();};host.append(label);}
  select('PADDING 選取方式',[['paired','依 A／B 配對條件過濾'],['index','從上數第 N 個（忽略 B／NO_GEM）']],row.selection_mode||'paired',v=>{row.selection_mode=v;if(v==='index')row.selection_index??=1;render();});
  if(row.selection_mode==='index'){const label=document.createElement('label'),input=document.createElement('input');label.textContent='從上數第幾個（1 起算，超出則選第一個）';input.type='number';input.min=1;input.max=100;input.step=1;input.value=row.selection_index??1;input.onchange=()=>{const n=Number(input.value);if(!Number.isInteger(n)||n<1||n>100){input.setCustomValidity('請填 1～100 的整數');input.reportValidity();return;}input.setCustomValidity('');row.selection_index=n;active=true;onChange();};label.append(input);host.append(label);const hint=document.createElement('small');hint.textContent='先依 A 候選由上到下、同高度由左到右排序；此模式不執行 B／NO_GEM 檢查。只找到 5 個而填 6，選第 1 個；A 未命中仍不操作。';host.append(hint);}
  const tags=[['','選擇 TAG'],...Object.entries(tagCatalog).map(([id,t])=>[id,t.name+' · '+t.roi.join(', ')])];
  for(const[key,title]of [['a_tag','A TAG：時鐘等可點目標的長條搜尋區'],['b_tag','B TAG：NO GEM 等條件的長條搜尋區']])select(title,tags,row[key],v=>{row[key]=v;delete row[key==='a_tag'?'a_roi':'b_roi'];});
  const features=[['','選擇檢查 Feature'],...Array.from(el('featureSelect').options).filter(o=>o.value&&!o.disabled).map(o=>[o.value,o.text])];
  const kind=row.check_kind||'coco';
  select('B 辨識類型',[['coco','COCO 圖示'],['ocr','OCR 文字'],['number','OCR 數字'],['percent','OCR 百分比'],['region','OCR 區域優先順序']],kind,v=>{
   for(const key of ['check_feature','match','check_text','check_text_match','check_op','check_value','region_priority','region_fallback','percent_fallback'])delete row[key];
   row.check_kind=v;
   if(v==='coco')Object.assign(row,{check_feature:'',match:'disappeared'});
   if(v==='ocr')Object.assign(row,{check_text:'',check_text_match:'exact'});
   if(v==='number')row.check_op='gte';
   if(v==='percent')Object.assign(row,{check_op:'lt',check_value:100});
   if(v==='region'){
    Object.assign(row,{region_priority:[],region_fallback:false,selection_mode:'paired'});
    el('targetPositionPolicy').value='same';
    el('targetMultiplicity').value='first_top_to_bottom';
   }
   render();
  });
  if(kind==='coco'){
   select('B 檢查 COCO Feature',features,row.check_feature,v=>row.check_feature=v);
   select('B 條件（符合才保留 A 的點擊位置）',[['disappeared','圖示不存在'],['detected','圖示存在']],row.match||'disappeared',v=>row.match=v);
  }else if(kind==='region'){
   const namesLabel=document.createElement('label'),names=document.createElement('textarea');
   namesLabel.textContent='區域名稱（每行一個，由上到下依序嘗試）';
   names.value=(row.region_priority||[]).join('\n');
   names.onchange=()=>{row.region_priority=names.value.split('\n').map(v=>v.trim()).filter(Boolean);active=true;onChange();};
   namesLabel.append(names);host.append(namesLabel);
   const fallbackLabel=document.createElement('label'),fallback=document.createElement('input');
   fallback.type='checkbox';fallback.checked=row.region_fallback===true;
   fallback.onchange=()=>{row.region_fallback=fallback.checked;active=true;onChange();};
   fallbackLabel.append(fallback,document.createTextNode('無符合區域時，依最上／最下設定點 A 候選（含 NO_GEM）'));
   host.append(fallbackLabel);
  }else{
   const numeric=kind==='number'||kind==='percent',label=document.createElement('label'),input=document.createElement('input');
   if(numeric){
    select('B 數值比較',[['eq','='],['ne','≠'],['gt','>'],['gte','≥'],['lt','<'],['lte','≤']],row.check_op||'gte',v=>row.check_op=v);
    label.textContent=kind==='percent'?'比較百分比（0～100）':'比較數值';input.type='number';input.step='any';input.value=row.check_value??'';
   }else{
    select('B 文字比對',[['exact','完全相同'],['contains','包含']],row.check_text_match||'exact',v=>row.check_text_match=v);
    label.textContent='尋找文字';input.value=row.check_text||'';input.maxLength=200;
   }
   input.onchange=()=>{
    if(numeric){if(input.value==='')delete row.check_value;else row.check_value=input.valueAsNumber;}
    else row.check_text=input.value;
    active=true;onChange();
   };
   label.append(input);host.append(label);
   if(kind==='percent'){
    const fallbackLabel=document.createElement('label'),fallback=document.createElement('input');
    fallback.type='checkbox';fallback.checked=row.percent_fallback===true;
    fallback.onchange=()=>{
     row.percent_fallback=fallback.checked;
     if(fallback.checked){
      row.selection_mode='paired';
      el('targetPositionPolicy').value='same';
      if(!['first_top_to_bottom','first_bottom_to_top'].includes(el('targetMultiplicity').value))el('targetMultiplicity').value='first_top_to_bottom';
     }
     active=true;render();onChange();
    };
    fallbackLabel.append(fallback,document.createTextNode('無可選列時依最上／最下保底（包含百分比無法辨識）'));
    host.append(fallbackLabel);
   }
  }
  paddingFilters(host,row,features);
  const rectLabel=document.createElement('label'),rectInput=document.createElement('input');rectLabel.textContent='偏移與檢查 ROI（X 偏移,Y 偏移,寬,高）';rectInput.placeholder='+20,-20,54,54';rectInput.value=row.check_width&&row.check_height?[row.offset_x??0,row.offset_y??0,row.check_width,row.check_height].join(','):'';rectInput.onchange=()=>{const values=rectInput.value.split(',').map(v=>v.trim());if(values.length!==4||values.some(v=>!/^[-+]?\d+$/.test(v))){rectInput.setCustomValidity('請輸入四個整數，例如 +20,-20,54,54');rectInput.reportValidity();return;}const[x,y,w,h]=values.map(Number);if(Math.abs(x)>2400||Math.abs(y)>2400||w<1||w>1080||h<1||h>2400){rectInput.setCustomValidity('偏移需在 ±2400，寬 1～1080，高 1～2400');rectInput.reportValidity();return;}rectInput.setCustomValidity('');delete row.padding_y;Object.assign(row,{offset_x:x,offset_y:y,check_width:w,check_height:h});active=true;render();onChange();};rectLabel.append(rectInput);host.append(rectLabel);
  for(const[key,title,min,max,step]of [['check_threshold','B 信心門檻',.5,1,.01],['offset_x','X 偏移量（+ 右／− 左）',-2400,2400,1],['offset_y','Y 偏移量（+ 下／− 上）',-2400,2400,1],['check_width','檢查 ROI 寬度（px）',1,1080,1],['check_height','檢查 ROI 高度（px）',1,2400,1]]){const label=document.createElement('label'),input=document.createElement('input');label.textContent=title;input.type='number';Object.assign(input,{min,max,step,value:row[key]??(key==='check_threshold'?.8:key.startsWith('offset_')?0:'')});input.placeholder=key==='check_width'?'空白沿用 B TAG 寬度':key==='check_height'?'空白沿用 A 目標高度':'';input.onchange=()=>{if(key!=='check_threshold'){delete row.padding_y;row.offset_x??=0;row.offset_y??=0;}if(input.value==='')delete row[key];else row[key]=Number(input.value);active=true;onChange();};label.append(input);host.append(label);}
  if(row.padding_y!==undefined){const legacy=document.createElement('small');legacy.textContent='舊版上下擴張 '+row.padding_y+' px 暫時保留；修改任一偏移／寬高欄位後，改用新的偏移矩形。';host.append(legacy);}
  const hint=document.createElement('small');hint.textContent='例：+20, −20, 54, 54 = 從 B 左緣右移 20、從 A 對應 Y 上移 20，以 54×54 檢查；超出 B TAG 的部分裁切。此列只提供保留的 A 目標位置。';host.append(hint);
  const previewButton=document.createElement('button'),previewResult=document.createElement('div');previewButton.type='button';previewButton.textContent='預覽配對（不點擊）';previewButton.onclick=()=>previewPadding(row,previewButton,previewResult);host.append(previewButton,previewResult);
 }
 function paddingFilters(host,row,features){
  if(row.selection_mode==='index')return;
  const filters=row.b_filters||[];
  const changed=()=>{active=true;onChange();};
  for(const [index,filter] of filters.entries()){
   const group=document.createElement('fieldset'),legend=document.createElement('legend');
   legend.textContent='B 篩選 '+(index+2)+'（共用同一個 B 檢查框）';group.append(legend);
   function field(title,control){
    const label=document.createElement('label');label.append(document.createTextNode(title),control);group.append(label);
   }
   function pick(title,options,value,change){
    const input=document.createElement('select');
    for(const [key,name] of options)input.add(new Option(name,key));
    if(value&&!options.some(([key])=>key===value))input.add(new Option(value+'（未找到）',value));
    input.value=value;input.onchange=()=>{change(input.value);changed();};field(title,input);
   }
   function entry(title,key,numeric,placeholder=''){
    const input=document.createElement('input');input.type=numeric?'number':'text';
    input.step=numeric?'any':'';input.maxLength=200;input.placeholder=placeholder;input.value=filter[key]??'';
    input.onchange=()=>{
     if(numeric&&input.value==='')delete filter[key];
     else filter[key]=numeric?input.valueAsNumber:input.value.trim();
     changed();
    };field(title,input);
   }
   const kind=filter.check_kind||'coco';
   pick('與前面結果結合',[['AND','AND'],['OR','OR']],filter.join||'AND',v=>filter.join=v);
   pick('辨識類型',[['coco','COCO 圖示'],['ocr','OCR 文字'],['number','OCR 數字'],['percent','OCR 百分比']],kind,v=>{
    const defaults=v==='coco'?{check_feature:'',match:'disappeared'}:v==='ocr'?{check_text:'',check_text_match:'exact'}:v==='percent'?{check_op:'lt',check_value:100}:{check_op:'gte'};
    filters[index]={join:filter.join||'AND',check_kind:v,...defaults};
    if(filter.check_threshold!==undefined)filters[index].check_threshold=filter.check_threshold;
    render();
   });
   if(kind==='coco'){
    pick('Feature',features,filter.check_feature||'',v=>filter.check_feature=v);
    pick('圖示條件',[['disappeared','不存在'],['detected','存在']],filter.match||'disappeared',v=>filter.match=v);
   }else if(kind==='ocr'){
    pick('文字比對',[['exact','完全相同'],['contains','包含']],filter.check_text_match||'exact',v=>filter.check_text_match=v);
    entry('尋找文字','check_text',false);
   }else{
    pick('數值比較',[['eq','='],['ne','≠'],['gt','>'],['gte','≥'],['lt','<'],['lte','≤']],filter.check_op||'gte',v=>filter.check_op=v);
    entry('比較數值','check_value',true);
   }
   entry('信心門檻','check_threshold',true,'0.8');
   const remove=document.createElement('button');remove.type='button';remove.textContent='移除此 B 篩選';
   remove.onclick=()=>{filters.splice(index,1);if(!filters.length)delete row.b_filters;render();changed();};
   group.append(remove);host.append(group);
  }
  const add=document.createElement('button');add.type='button';add.textContent='＋新增 B 篩選條件';add.disabled=filters.length>=8;
  add.onclick=()=>{(row.b_filters??=[]).push({join:'AND',check_kind:'coco',check_feature:'',match:'disappeared'});render();changed();};
  host.append(add);
 }
 async function previewPadding(row,button,host){
  const targetPolicy=el('targetMultiplicity').value;
  button.disabled=true;host.replaceChildren();
  let stage='讀取設定中的圖片';
  try{
   const display=el('editorFrame'),displayUrl=display.currentSrc||display.src;
   if(display.hidden||!displayUrl||!display.complete||!display.naturalWidth)throw Error('設定中目前沒有可讀取的預覽圖片');
   const index=row===firstPadding?0:extra.indexOf(row)+1,p=params(),module={tags:stepSettings.tags()};
   const resolved=WorkflowModel.resolve(module,p),clause=resolved.target_conditions?.clauses[index];
   if(clause?.type!=='padding_coco')throw Error('請先完成 PADDING COCO 設定');
   const snapshot=structuredClone({row:clause,frame_id:'editor-'+crypto.randomUUID()});
   // Read the already decoded image. fetch(blob:) is blocked by connect-src
   // (default-src 'self'); canvas drawing needs no extra network permission.
   const pixels=document.createElement('canvas');pixels.width=display.naturalWidth;pixels.height=display.naturalHeight;
   const context=pixels.getContext('2d');if(!context)throw Error('瀏覽器無法讀取圖片像素');
   context.drawImage(display,0,0);
   const imageBlob=await new Promise((resolve,reject)=>pixels.toBlob(blob=>blob?resolve(blob):reject(Error('圖片 PNG 編碼失敗')),'image/png'));
   snapshot.image_png=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(Error('無法讀取目前預覽圖'));reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.readAsDataURL(imageBlob);});
   stage='傳送圖片至 Dashboard 配對辨識';
   const response=await fetch('/api/padding-preview',{method:'POST',headers,body:JSON.stringify(snapshot)});
   stage='讀取 Dashboard 配對結果';const result=await response.json();
   if(!response.ok)throw Error(result.error||'配對預覽失敗');
   const previewFallback=clause.percent_fallback&&!result.retained?.length&&result.fallback_anchors?.length&&['first_top_to_bottom','first_bottom_to_top'].includes(targetPolicy);
   if(previewFallback||targetPolicy==='first_bottom_to_top'){
    const direction=targetPolicy==='first_bottom_to_top'?-1:1;
    result.selected=[...(previewFallback?result.fallback_anchors:result.retained||[])].sort((a,b)=>direction*(a.bbox[1]-b.bbox[1])||a.bbox[0]-b.bbox[0]||(b.confidence||0)-(a.confidence||0)||(a.feature||'').localeCompare(b.feature||'' )).slice(0,1);
   }
   const summary=document.createElement('p');summary.textContent='本列配對 '+result.result+' · 保留 '+(result.retained?.length||0)+' · '+(result.bypassed_b?'忽略 B／NO_GEM；指定第 '+result.requested_index+'，實選第 '+result.selected_index:targetPolicy==='first_bottom_to_top'?'最下方、同高度取最左':'最上方第一個')+' '+(result.selected?.[0]?.bbox?.join(', ')||'無')+' · Frame '+result.frame_id+(result.reason?' · '+result.reason:'');host.append(summary);
   const note=document.createElement('small');note.textContent=(previewFallback?'此列無合格百分比，已標出 A 保底位置。':'')+'直接重新辨識設定中目前顯示的圖片，不沿用舊辨識結果、不重拍、不點擊。相同編號／名稱的綠框是 A 目標與對應的 B 命中；藍框是該 A 的檢查 ROI。其他 AND／OR 列仍於執行時判斷。';host.append(note);
   const shownBlob=result.normalized_image_png?new Blob([Uint8Array.from(atob(result.normalized_image_png),c=>c.charCodeAt(0))],{type:'image/png'}):imageBlob;
   const url=URL.createObjectURL(shownBlob);previewUrls.push(url);if(previewUrls.length>6)URL.revokeObjectURL(previewUrls.shift());
   const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),image=document.createElementNS(ns,'image');svg.setAttribute('viewBox','0 0 1080 2400');svg.setAttribute('width','100%');svg.style.maxHeight='650px';image.setAttribute('href',url);image.setAttribute('width','1080');image.setAttribute('height','2400');svg.append(image);
   function box(rect,color,label){const[x,y,w,h]=rect,r=document.createElementNS(ns,'rect'),text=document.createElementNS(ns,'text');for(const[k,v]of Object.entries({x,y,width:w,height:h,fill:'none',stroke:color,'stroke-width':5}))r.setAttribute(k,v);for(const[k,v]of Object.entries({x,y:Math.max(25,y-5),fill:color,'font-size':28}))text.setAttribute(k,v);text.textContent=label;svg.append(r,text);}
   for(const[cIndex,c]of (result.candidates||[]).entries()){
    const id=String(cIndex+1),selected=JSON.stringify(c.anchor_bbox)===JSON.stringify(result.selected?.[0]?.bbox);
    box(c.anchor_bbox,'#26fa7c',id+' · '+(c.anchor_feature||clause.features.join('|'))+(selected?' · 將點擊':''));
    if(c.b_band)box(c.b_band,'#4da6ff',id+' · 檢查 ROI');
    for(const hit of c.matches||[])box(hit.bbox,'#26fa7c',id+' · '+clause.check_feature);
    const bLabel=clause.check_feature||clause.check_text||(clause.check_kind==='region'?'區域名稱':'數字 '+clause.check_op+' '+clause.check_value);
    const reading=c.ocr_tokens?.map(token=>token.text).filter(Boolean).join(' · ');
    const filters=(c.filter_results||[]).map(f=>f.check.join+' '+(f.check.check_feature||f.check.check_text||f.check.check_kind)+'='+ (f.result===null?'UNKNOWN':f.result?'YES':'NO')).join(' · ');
    const detail=document.createElement('small');detail.textContent=id+' · '+(c.anchor_feature||clause.features.join('|'))+' ↔ '+bLabel+'：'+(result.bypassed_b?'略過 B':c.detected===null?'UNKNOWN':c.detected?'有命中':'未命中')+(reading?'（OCR：'+reading+'）':'')+(filters?' · '+filters:'')+' → '+(c.accepted?'符合':'排除');host.append(detail);
   }
   host.append(svg);
  }catch(error){host.textContent=stage+'失敗：'+error.message;}finally{button.disabled=false;}
 }
 const previewUrls=[];
 function render(){
  paddingHost.replaceChildren();paddingControls(paddingHost,firstPadding);paddingHost.hidden=el('targetMode').value!=='padding_coco';
  const source=el('targetPositionSource'),selected=source.value;source.replaceChildren();
  for(let i=0;i<=extra.length;i++)source.add(new Option('第 '+(i+1)+' 列',String(i)));
  source.value=Number(selected)<=extra.length?selected:'0';if(source.selectedIndex<0)source.value='0';
  source.disabled=el('targetPositionPolicy').value!=='source';
  el('allCocoTargets').checked=el('targetMultiplicity').value==='all_top_to_bottom';
  const host=el('targetConditionRows');host.replaceChildren();
  extra.forEach((row,index)=>{
   const group=document.createElement('fieldset'),title=document.createElement('legend');title.textContent='目標條件 '+(index+2);group.append(title);
   function select(label,choices,value,change){const wrap=document.createElement('label'),control=document.createElement('select');wrap.append(document.createTextNode(label),control);for(const[v,name]of choices)control.add(new Option(name,v));control.value=value;control.onchange=()=>{change(control.value);active=true;render();onChange();};group.append(wrap);}
   select('與前面結果結合',[['AND','AND'],['OR','OR']],row.join||'OR',value=>row.join=value);
   select('辨識類型',choices,row.type,value=>{if(value==='padding_coco')el('targetMultiplicity').value='first_top_to_bottom';extra[index]={type:value,join:row.join||'OR',threshold:row.threshold??.8,
       ...(types[value].coco?{features:row.features?.slice()||[]}:{}),
       ...(types[value].color?{color_rgb:row.color_rgb?.slice()||[0,255,0],color_tolerance:row.color_tolerance??10,color_min_pixels:row.color_min_pixels??9}:{}),
       ...(value==='ocr'?{text:row.text||'OK',match:row.match||'exact'}:{}),
       ...(value==='padding_coco'?defaultPadding():{}),
       ...(row.roi_tag?{roi_tag:row.roi_tag}:row.roi?{roi:row.roi.slice()}:{})};});
   if(types[row.type]?.coco){
    row.features??=[];
    const heading=document.createElement('label'),toolbar=document.createElement('div'),picker=document.createElement('select'),add=document.createElement('button'),chips=document.createElement('div');heading.textContent='COCO Features';toolbar.className='toolbar';chips.className='chips';picker.setAttribute('aria-label','目標條件 '+(index+2)+' 的 COCO Feature');picker.add(new Option('選擇 Feature',''));
    for(const option of el('featureSelect').options)if(option.value&&!option.disabled&&!row.features.includes(option.value))picker.add(new Option(option.text,option.value));
    function changed(){if(row.feature_priority)row.feature_priority=row.features.slice();active=true;render();onChange();}
    add.type='button';add.textContent='＋新增';add.disabled=true;picker.onchange=()=>add.disabled=!picker.value;add.onclick=()=>addCocoFeature(row.features,picker.value,changed);renderCocoFeatureChips(chips,row.features,changed);toolbar.append(picker,add);group.append(heading,toolbar,chips);
    const priorityLabel=document.createElement('label'),priority=document.createElement('input');priority.type='checkbox';priority.checked=!!row.feature_priority;priority.onchange=()=>{if(priority.checked)row.feature_priority=row.features.slice();else delete row.feature_priority;active=true;onChange();};priorityLabel.append(priority,document.createTextNode('明確以左至右順序選擇候選（↑／↓調整）'));group.append(priorityLabel);
   }
   if(row.type==='ocr'){const label=document.createElement('label'),input=document.createElement('input');label.textContent='尋找文字';input.value=row.text||'';input.maxLength=200;input.oninput=()=>{row.text=input.value;active=true;onChange();};label.append(input);group.append(label);select('文字比對',[['exact','完全相同'],['contains','包含']],row.match||'exact',value=>row.match=value);}
   if(types[row.type]?.color){
    for(const[key,title,inputType,initial,min,max]of [
      ['color_rgb','目標顏色','color','#'+(row.color_rgb||[0,255,0]).map(n=>n.toString(16).padStart(2,'0')).join('')],
      ['color_tolerance','RGB 每通道容差','number',row.color_tolerance??10,0,255],
      ['color_min_pixels','最小色塊像素','number',row.color_min_pixels??9,1,2592000]]){
      const label=document.createElement('label'),input=document.createElement('input');label.textContent=title;input.type=inputType;input.value=initial;
      if(min!==undefined)input.min=min;if(max!==undefined)input.max=max;
      input.oninput=()=>{row[key]=inputType==='color'?input.value.slice(1).match(/../g).map(n=>parseInt(n,16)):Number(input.value);active=true;onChange();};label.append(input);group.append(label);
    }
   }
   const threshold=document.createElement('input'),label=document.createElement('label');label.textContent='信心門檻';threshold.type='number';threshold.min=.5;threshold.max=1;threshold.step=.01;threshold.value=row.threshold??.8;threshold.oninput=()=>{row.threshold=Number(threshold.value);active=true;onChange();};label.append(threshold);group.append(label);
   if(row.type==='padding_coco'){delete row.roi;delete row.roi_tag;paddingControls(group,row);}else{
   select('搜尋 ROI TAG',[['','沿用本步全域／TAG（或指定下方 ROI）'],...Object.entries(tagCatalog).map(([id,tag])=>[id,tag.name+' · '+tag.roi.join(', ')])],row.roi_tag||'',value=>{if(value){row.roi_tag=value;delete row.roi;}else delete row.roi_tag;});
   const roiLabel=document.createElement('label'),roi=document.createElement('input');roiLabel.textContent='ROI x,y,width,height（空白沿用本步全域／TAG）';roi.value=row.roi?.join(',')||'';roi.disabled=!!row.roi_tag;roi.onchange=()=>{if(roi.value.trim())row.roi=roi.value.split(',').map(Number);else delete row.roi;active=true;onChange();};roiLabel.append(roi);group.append(roiLabel);
   if(row.roi_tag){const hint=document.createElement('small');hint.textContent='沿用 TAG：'+row.roi_tag;group.append(hint);}
   }
   const remove=document.createElement('button');remove.type='button';remove.textContent='移除此列';remove.onclick=()=>{extra.splice(index,1);active=true;render();onChange();};group.append(remove);host.append(group);
  });
 }
 el('addTargetCondition').onclick=()=>{if(extra.length>=29)return;extra.push({type:'ocr',join:'OR',text:'OK',match:'exact',threshold:Number(el('threshold').value)});active=true;render();onChange();};
 el('targetPositionPolicy').onchange=()=>{active=true;render();onChange();};
 el('targetPositionSource').onchange=()=>{active=true;onChange();};
 el('targetMultiplicity').onchange=()=>{active=true;render();onChange();};
 el('targetFirstKind').onchange=()=>{el('targetMode').value=el('targetFirstKind').value;if(el('targetMode').value==='padding_coco')el('targetMultiplicity').value='first_top_to_bottom';active=true;render();el('targetMode').onchange();};
 return {
  firstKind(){return firstKind==='padding_coco'?firstKind:null;},
  fill(original){firstKind=original.target_conditions?.clauses[0]?.type;firstPadding=firstKind==='padding_coco'?structuredClone(original.target_conditions.clauses[0]):defaultPadding();el('targetConditionResult').textContent='組合目標在執行時辨識；預覽不代表整組條件已成立。';const step=structuredClone(original),g=step.target_conditions;active=!!g;firstScope={};if(g?.clauses[0].roi)firstScope.roi=g.clauses[0].roi.slice();if(g?.clauses[0].roi_tag)firstScope.roi_tag=g.clauses[0].roi_tag;extra=structuredClone(g?.clauses.slice(1)||[]);el('targetPositionPolicy').value=g?.position_policy||'source';el('targetPositionSource').value=String(g?.position_source||0);if(g){const first=g.clauses[0];delete step.target_conditions;delete step.features;delete step.feature;delete step.feature_priority;delete step.ocr_target;delete step.color_mode;for(const key of ['color_rgb','color_tolerance','color_min_pixels'])delete step[key];if(first.type==='ocr')step.ocr_target={text:first.text,match:first.match||'exact'};if(types[first.type].coco){step.features=first.features.slice();if(first.feature_priority)step.feature_priority=first.feature_priority.slice();}if(types[first.type].color){step.color_mode=first.type==='color'?'only':first.type==='coco_color'?'coco':'either';for(const key of ['color_rgb','color_tolerance','color_min_pixels'])if(first[key]!==undefined)step[key]=structuredClone(first[key]);}if(first.threshold!==undefined)step.threshold=first.threshold;}render();el('targetPositionSource').value=String(g?.position_source||0);return step;},
  refresh(){el('targetFirstKind').value=el('targetMode').value;const supported=Object.hasOwn(types,el('targetMode').value);paddingHost.hidden=el('targetMode').value!=='padding_coco';el('addTargetCondition').disabled=!supported||extra.length>=29;el('targetPositionPolicy').disabled=!supported;el('targetMultiplicity').disabled=!supported;const all=el('targetMultiplicity').querySelector('option[value=all_top_to_bottom]');all.disabled=el('action').value==='SwipeCalibration';if(all.disabled&&el('targetMultiplicity').value==='all_top_to_bottom')el('targetMultiplicity').value='first_top_to_bottom';el('targetPositionSource').disabled=!supported||el('targetPositionPolicy').value!=='source';},
  parameters(p){delete p.target_conditions;const kind=el('targetMode').value;if(!active||!types[kind])return p;let first={type:kind,...(kind==='padding_coco'?firstPadding:firstScope),threshold:p.threshold};first.type=kind;if(kind==='padding_coco'){delete first.roi;delete first.roi_tag;}if(types[kind].coco){first.features=(p.features||[]).slice();if(p.feature_priority)first.feature_priority=p.feature_priority.slice();else delete first.feature_priority;}if(kind==='ocr')Object.assign(first,p.ocr_target);if(types[kind].color)for(const key of ['color_rgb','color_tolerance','color_min_pixels'])if(p[key]!==undefined)first[key]=structuredClone(p[key]);const clauses=[first,...structuredClone(extra)];p.target_conditions={evaluation:'left_to_right',position_policy:el('targetPositionPolicy').value,position_source:Number(el('targetPositionSource').value),clauses};for(const key of ['features','feature','feature_priority','ocr_target','color_mode','color_rgb','color_tolerance','color_min_pixels','x','y'])delete p[key];p.recognize_before=true;p.allow_unconfirmed_target=false;p.target_policy=el('targetMultiplicity').value;return p;},
  references(){return [...extra,firstScope,firstPadding].flatMap(row=>[row.roi_tag,row.a_tag,row.b_tag].filter(Boolean));},draft(){return {clauses:[{...firstScope,...(el('targetMode').value==='padding_coco'?firstPadding:{})},...structuredClone(extra)]};},catalogChanged(){render();},setTags(tags){const signature=JSON.stringify(tags);if(signature!==tagSignature){tagCatalog=structuredClone(tags);tagSignature=signature;render();}}
 };
};
