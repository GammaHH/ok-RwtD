'use strict';
(() => {
  const token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem('rwtd-calibration-token');
  const parentKey='rwtd-calibration-parent:'+location.origin;
  const workspaceParent=new URLSearchParams(location.hash.slice(1)).get('parent_origin') || sessionStorage.getItem(parentKey);
  const embedded=window.parent!==window && /^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(workspaceParent||'');
  if(embedded)sessionStorage.setItem(parentKey,workspaceParent);
  if(token)sessionStorage.setItem('rwtd-calibration-token',token);
  history.replaceState(null, '', location.pathname);
  const $ = id => document.getElementById(id);
  const labels = {NEW:'新增／未校正',VALID:'有效',CHANGED:'已變更／待校正',DELETED:'已刪除',ERROR:'原轉換無效'};
  let state, selection, fields = {}, overrides = new Set(), dirty = false, busy = false;
  let images = {}, urls = {}, drag = null;
  let readingReference = false;
  addEventListener('message',async event=>{
    if(!embedded || event.origin!==workspaceParent || event.source!==window.parent)return;
    if(event.data?.type==='rwtd-workspace-can-leave'){
      const reason=busy||readingReference||drag?'解析度對照操作尚未完成，請等待後再切換。':dirty?'解析度對照有未儲存校正，請先儲存再切換。':!state?'解析度對照尚未就緒，請稍後再試。':'';
      window.parent.postMessage({type:'rwtd-workspace-leave-result',id:event.data.id,reason},workspaceParent);
    }
    if(event.data?.type==='rwtd-workspace-activate' && !busy && !dirty && !readingReference){
      try{await command('refresh');render();}catch(error){notice(error.message);}
    }
  });
  function notice(text) {$('message').textContent = text;}
  function changed() {dirty = true; $('dirty').textContent = '尚未儲存'; draw();}
  function discard() {return !dirty || confirm('目前有未儲存校正，確定放棄後切換？');}
  async function api(path, options={}) {
    const response = await fetch(path, {...options, headers: {'Authorization':'Bearer '+token,
      ...(options.body ? {'Content-Type':'application/json'} : {})}});
    if (!response.ok) {let error = await response.json(); throw Error(error.error || response.status);}
    return response;
  }
  async function command(command, payload={}) {
    if (busy) return;
    busy = true; controls();
    const request_id = crypto.randomUUID();
    try {
      await api('/api/command', {method:'POST', body:JSON.stringify({request_id,command,payload})});
      for (let attempt=0; attempt<120; attempt++) {
        await new Promise(resolve=>setTimeout(resolve,250));
        const next = await (await api('/api/state')).json();
        const receipt = next.receipts[request_id];
        if (receipt && receipt.status !== 'QUEUED') {
          if (receipt.status === 'FAILED') throw Error(receipt.error);
          state = next; return next;
        }
      }
      throw Error('操作仍未確認，請勿重複儲存；重載頁面確認結果。');
    } catch(error) {notice(error.message); throw error;}
    finally {busy=false; controls();}
  }
  function controls() {
    document.querySelectorAll('button,select,input').forEach(el=>{el.disabled=busy || el.dataset.blocked==='true';});
  }
  async function loadImage(which, canvas) {
    if (urls[which]) {URL.revokeObjectURL(urls[which]); delete urls[which];}
    delete images[which];
    if (which==='source' && !state.has_reference) {draw(); return;}
    try {
      const blob = await (await api('/api/image?which='+which)).blob();
      const img = new Image(); const url = URL.createObjectURL(blob); urls[which]=url;
      await new Promise((resolve,reject)=>{img.onload=resolve; img.onerror=reject; img.src=url;});
      images[which]=img; canvas.width=img.naturalWidth; canvas.height=img.naturalHeight; draw();
    } catch(error) {notice('圖片無法載入：'+(error.message||which));}
  }
  function render() {
    $('size').textContent = '目前解析度：'+(state.resolution || '尚未擷取') + (state.captured_utc?' · 擷取 '+new Date(state.captured_utc).toLocaleTimeString(): '');
    notice(state.message || '');
    const chosen = state.selection?.module || $('module').value;
    $('module').replaceChildren(new Option('請選擇模組',''));
    for (const mod of state.modules) {
      const c=mod.counts;
      $('module').add(new Option(`${mod.name} [${mod.id}] · 有效 ${c.VALID}／待校正 ${c.CHANGED}／新增 ${c.NEW}／刪除 ${c.DELETED}／錯誤 ${c.ERROR}`,mod.id));
    }
    $('module').value=chosen;
    $('steps').replaceChildren();
    for (const row of state.steps) {
      const tr=document.createElement('tr');
      for (const text of [row.step_id,row.name,labels[row.status],row.error|| (row.mapping_changed?'既有解析度轉換已變更':row.other_changes?'操作／條件設定已變更':row.changed_fields.join(', '))]) {
        const td=document.createElement('td');td.textContent=text;tr.append(td);
      }
      const td=document.createElement('td'), button=document.createElement('button');
      button.textContent='校正此 Step';button.disabled=['DELETED','ERROR'].includes(row.status);
      button.dataset.blocked=String(['DELETED','ERROR'].includes(row.status));
      button.onclick=async()=>{if(!discard())return;await select(chosen,row.step_id);};
      td.append(button);tr.append(td);$('steps').append(tr);
    }
    selection=state.selection;
    $('editor').hidden=!selection;
    if (!selection) return;
    fields=structuredClone(selection.fields); overrides=new Set(selection.overrides);
    dirty=false;$('dirty').textContent='';
    $('warning').textContent=selection.warning || '';
    $('stale').textContent=selection.saved_fields?JSON.stringify(selection.saved_fields,null,2):'沒有過期校正';
    $('reference-label').textContent=state.reference_label || '沒有此 Step 的存檔圖片；可匯入參考圖。左圖暫僅顯示預設框線。';
    const [w,h]=state.resolution.split('x').map(Number);
    for(const id of ['saved','live']){$(id).width=w;$(id).height=h;}
    $('field').replaceChildren();
    for (const [key,row] of Object.entries(fields)) $('field').add(new Option((row.kind==='point'?'觸點 ':'ROI ')+key,key));
    editorValues(); draw();
  }
  async function select(module,step_id) {
    await command('select',{module,step_id});render();
    await Promise.all([loadImage('source',$('saved')),loadImage('frame',$('live'))]);
  }
  function editorValues() {
    $('values').replaceChildren();
    const key=$('field').value,row=fields[key];if(!row)return;
    const names=row.kind==='point'?['X','Y']:['X','Y','Width','Height'];
    names.forEach((name,index)=>{
      const label=document.createElement('label');label.textContent=name;
      const input=document.createElement('input');input.type='number';input.step='1';input.value=row.value[index];
      input.onchange=()=>{row.value[index]=Number(input.value);overrides.add(key);changed();};
      label.append(input);$('values').append(label);
    });
  }
  function drawOne(canvas, image, rows) {
    const c=canvas.getContext('2d');c.clearRect(0,0,canvas.width,canvas.height);
    c.fillStyle='#000';c.fillRect(0,0,canvas.width,canvas.height);
    if(image)c.drawImage(image,0,0,canvas.width,canvas.height);
    const active=$('field').value;
    for (const [key,row] of Object.entries(rows)) {
      const [x,y,w,h]=row.value;c.strokeStyle=key===active?'#ffe45a':'#48c5ef';c.fillStyle=c.strokeStyle;
      c.lineWidth=key===active?5:2;c.beginPath();
      if(row.kind==='point'){c.arc(x,y,14,0,Math.PI*2);c.moveTo(x-24,y);c.lineTo(x+24,y);c.moveTo(x,y-24);c.lineTo(x,y+24);}
      else {c.rect(x,y,w,h); if(key===active)c.fillRect(x+w-12,y+h-12,24,24);}
      c.stroke();c.font='22px sans-serif';c.fillText(key,x+8,Math.max(25,y-8));
    }
  }
  function draw(){if(!selection)return;drawOne($('saved'),images.source,selection.defaults);drawOne($('live'),images.frame,fields);}
  function point(event){const c=$('live'),r=c.getBoundingClientRect();return[(event.clientX-r.left)*c.width/r.width,(event.clientY-r.top)*c.height/r.height];}
  $('live').onpointerdown=event=>{
    if(busy)return;const key=$('field').value,row=fields[key];if(!row)return;
    const p=point(event),[x,y,w,h]=row.value; const tolerance=24;
    const resize=row.kind==='roi' && Math.abs(p[0]-x-w)<tolerance && Math.abs(p[1]-y-h)<tolerance;
    const inside=row.kind==='point'?Math.hypot(p[0]-x,p[1]-y)<45:p[0]>=x&&p[0]<=x+w&&p[1]>=y&&p[1]<=y+h;
    if(!resize&&!inside)return;
    drag={key,p,original:[...row.value],resize};$('live').setPointerCapture(event.pointerId);
  };
  $('live').onpointermove=event=>{
    if(!drag)return;const p=point(event),row=fields[drag.key],v=[...drag.original],c=$('live');
    const dx=Math.round(p[0]-drag.p[0]),dy=Math.round(p[1]-drag.p[1]);
    if(drag.resize){v[2]=Math.max(1,Math.min(c.width-v[0],v[2]+dx));v[3]=Math.max(1,Math.min(c.height-v[1],v[3]+dy));}
    else {v[0]=Math.max(0,Math.min(c.width-(row.kind==='roi'?v[2]:1),v[0]+dx));v[1]=Math.max(0,Math.min(c.height-(row.kind==='roi'?v[3]:1),v[1]+dy));}
    row.value=v;overrides.add(drag.key);changed();editorValues();
  };
  $('live').onpointerup=$('live').onpointercancel=()=>{drag=null;};
  $('field').onchange=()=>{editorValues();draw();};
  $('reset').onclick=()=>{const k=$('field').value;if(!fields[k])return;fields[k]=structuredClone(selection.defaults[k]);overrides.delete(k);changed();editorValues();};
  $('module').onchange=async()=>{const id=$('module').value;if(!discard()){$('module').value=selection?.module||'';return;}if(id)await select(id);};
  $('capture').onclick=async()=>{await command('capture');notice(state.message);await loadImage('frame',$('live'));};
  $('refresh').onclick=async()=>{if(!discard())return;await command('refresh');dirty=false;selection=null;$('module').value='';render();};
  $('close').onclick=async()=>{if(embedded){window.parent.postMessage({type:'rwtd-workspace-dashboard'},workspaceParent);return;}if(!discard())return;await command('close');dirty=false;notice('任務已關閉');};
  $('upload').onchange=async event=>{
    const file=event.target.files[0];if(!file)return;
    if(file.size>6*1024**2){notice('參考圖不能超過 6 MiB');return;}
    const reader=new FileReader();readingReference=true;
    reader.onerror=()=>{readingReference=false;notice('參考圖讀取失敗');};
    reader.onabort=()=>{readingReference=false;};
    reader.onload=async()=>{try{await command('reference',{image:reader.result.split(',')[1]});dirty=true;$('dirty').textContent='參考圖尚未儲存';$('reference-label').textContent=state.reference_label;await loadImage('source',$('saved'));}finally{readingReference=false;}};
    reader.readAsDataURL(file);
  };
  $('save').onclick=async()=>{
    if(!selection)return;
    const authored=Object.fromEntries([...overrides].map(k=>[k,fields[k]]));
    if ('mid_x,mid_y' in fields && Object.values(authored).some(row=>row.kind==='point')) {
      for (const [key,row] of Object.entries(fields)) if(row.kind==='point') authored[key]=row;
    }
    await command('save',{revision:state.revision,signature:selection.signature,fields:authored});
    render();await Promise.all([loadImage('source',$('saved')),loadImage('frame',$('live'))]);
  };
  $('remove').onclick=async()=>{
    if(!selection || !confirm('移除此解析度、此 Step 的獨立校正？原模組不變。'))return;
    await command('remove',{revision:state.revision,signature:selection.signature});render();draw();
  };
  addEventListener('beforeunload',event=>{if(dirty||busy){event.preventDefault();event.returnValue='';}});
  addEventListener('unhandledrejection',event=>{notice(event.reason?.message||String(event.reason));event.preventDefault();});
  (async()=>{state=await(await api('/api/state')).json();render();await loadImage('frame',$('live'));})();
})();
