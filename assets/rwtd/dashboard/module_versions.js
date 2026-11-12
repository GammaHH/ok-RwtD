'use strict';
window.installModuleVersions=({read,send,reload,load,error=()=>''})=>{
 const el=id=>document.getElementById(id);
 let history=null,pending=null,selection='';
 function blocked(){const s=read();return !!(s.pending||s.dirty||s.current?.module_run?.active||s.current?.scheduler_runtime?.input_blocked||s.current?.scheduler_runtime?.locked);}
 function selected(){return history?.items.find(i=>i.backup_id===selection);}
 function render(){
  const s=read(),m=s.editing,item=selected();
  el('moduleVersionLabel').textContent=m?'r'+m.version:'r1（新模組）';
  el('moduleVersionsOpen').disabled=!m||!!pending;
  el('moduleVersionRestore').disabled=!item||!item.changes.length||!!pending||blocked()||m?.id!==history?.module_id||m?.revision!==history?.current_revision;
  el('moduleVersionsReload').disabled=!m||!!pending;
  if(item){
   el('moduleVersionPreview').replaceChildren();
   const title=document.createElement('p');title.textContent='目前 r'+history.current_version+' → 回復 r'+item.version+' 的內容；另存為 r'+(history.current_version+1);el('moduleVersionPreview').append(title);
   const list=document.createElement('ul');for(const line of item.changes){const li=document.createElement('li');li.textContent=line;list.append(li);}el('moduleVersionPreview').append(list);
   if(!item.changes.length){const p=document.createElement('p');p.textContent='與目前內容相同，無須回復。';el('moduleVersionPreview').append(p);}
   if(item.changes_truncated){const p=document.createElement('p');p.textContent='差異超過 300 項，僅顯示前 300 項。';el('moduleVersionPreview').append(p);}
  }else el('moduleVersionPreview').textContent='請選擇歷史版本；預覽不會更動模組或操作手機。';
 }
 async function refresh(){
  const {editing:m}=read();if(!m||pending)return;
  history=null;selection='';el('moduleVersionSelect').replaceChildren();
  const token=crypto.randomUUID();pending={kind:'history',token,id:m.id,request_id:null};render();
  el('moduleVersionsNotice').textContent='讀取 '+m.name+' 的本機儲存歷史…';
  const receipt=await send('module_history',{id:m.id,request_token:token});
  if(receipt){pending.request_id=receipt.request_id;await reload();}else{pending=null;el('moduleVersionsNotice').textContent='版本清單讀取失敗：'+(error()||'後端未回應，請確認 Qt 工作區已啟動。');render();}
 }
 el('moduleVersionsOpen').onclick=()=>{el('moduleVersionsDialog').showModal();refresh();};
 el('moduleVersionsReload').onclick=refresh;
 el('moduleVersionSelect').onchange=()=>{selection=el('moduleVersionSelect').value;render();};
 el('moduleVersionRestore').onclick=async()=>{
  const s=read(),item=selected(),m=s.editing;
  if(!item||blocked()||pending||m?.id!==history?.module_id||m?.revision!==history?.current_revision)return;
  if(!confirm('回復 '+m.name+' 的 r'+item.version+' 內容？目前內容會先備份，並另存為 r'+(m.version+1)+'。'))return;
  pending={kind:'restore',id:m.id,request_id:null};render();
  const receipt=await send('module_restore',{id:m.id,backup_id:item.backup_id,backup_hash:item.backup_hash,expected_revision:history.current_revision,confirmed:true,system_update:m.kind==='system'});
  if(receipt){pending.request_id=receipt.request_id;el('moduleVersionsNotice').textContent='回復已排隊，等待儲存結果…';await reload();}else{pending=null;el('moduleVersionsNotice').textContent='回復未送出：'+(error()||'後端未回應。');render();}
 };
 return {update(){
  const s=read();
  if(pending?.request_id){
   const status=s.current?.requests?.[pending.request_id];
   if(status&&!['QUEUED','RUNNING'].includes(status)){
    const request=pending;pending=null;
    if(['READY','SUCCESS'].includes(status)){
     if(request.kind==='history'){
      const result=s.current.editor_result;
      if(result?.command==='module_history'&&result.request_token===request.token&&result.definition.module_id===request.id){
       history=result.definition;const select=el('moduleVersionSelect');select.replaceChildren(new Option('選擇歷史版本',''));
       for(const item of history.items)select.add(new Option('r'+item.version+' · '+(item.saved_at?new Date(item.saved_at).toLocaleString():'離線備份')+' · '+item.step_count+' Steps',item.backup_id));
       el('moduleVersionsNotice').textContent='目前 r'+history.current_version+'；可回復 '+history.items.length+' 份備份。'+(history.skipped?' '+history.skipped+' 份無法使用。':'')+(history.truncated?' 僅列最近 100 份。':'')+' 未備份的舊內容無法憑版本號重建。';
      }else el('moduleVersionsNotice').textContent='清單回應已更新；請重新載入。';
     }else{
      const restored=s.current.module_catalog.find(m=>m.id===request.id);
      if(restored&&s.editing?.id===request.id&&!s.dirty){load(restored);el('moduleVersionsNotice').textContent='已回復並儲存為 r'+restored.version+'；手機未送出操作。';history=null;selection='';el('moduleVersionSelect').replaceChildren();}
     }
    }else{const log=[...(s.current.logs||[])].reverse().find(i=>i.request_id===request.request_id);el('moduleVersionsNotice').textContent='操作失敗：'+(log?.message||status);}
   }
  }
  render();
 }};
};

window.installModuleTrash=({read,send,reload,load,error=()=>''})=>{
 const el=id=>document.getElementById(id);
 let items=[],pending=null,selection='',after='';
 const selected=()=>items.find(i=>i.backup_id===selection);
 const blocked=()=>{const s=read();return !!(s.pending||s.dirty||window.rwtdCanvasHasUnsavedChanges?.()||s.current?.module_run?.active||s.current?.scheduler_runtime?.locked||s.current?.scheduler_runtime?.input_blocked);};
 function render(){
  const item=selected();
  el('moduleTrashOpen').disabled=!!pending;
  el('moduleTrashReload').disabled=!!pending;
  el('moduleTrashSelect').disabled=!!pending;
  el('moduleTrashRestore').disabled=!!pending||blocked()||!item||item.conflict;
  el('moduleTrashPurge').disabled=!!pending||blocked()||!item;
  el('moduleTrashDetails').textContent=item?(item.item_type==='canvas'?'Canvas · '+item.name+' · '+item.node_count+' 節點':'模組 · '+item.name+' · r'+item.version+' · '+item.step_count+' Steps')+' · '+(item.conflict?'目前已有同 ID／名稱的項目，僅可刪除這份備份。':item.item_type==='canvas'?'還原後保留原 ID 並切換到該 Canvas。':'還原後為 r'+(item.version+1)+'；刪除備份仍保留，可自行清除。'):'';
 }
 async function refresh(message=''){
  if(pending)return;
  after=message;items=[];selection='';el('moduleTrashSelect').replaceChildren();
  pending={command:'module_trash',token:crypto.randomUUID(),request_id:null};render();
  el('moduleTrashNotice').textContent=after+'讀取垃圾桶…';
  const receipt=await send('module_trash',{request_token:pending.token});
  if(receipt){pending.request_id=receipt.request_id;await reload();}
  else{pending=null;el('moduleTrashNotice').textContent='讀取失敗：'+error();render();}
 }
 async function act(operation){
  const item=selected();if(!item||pending||blocked()||(operation==='restore'&&item.conflict))return;
  const description=item.item_type==='canvas'?'Canvas「'+item.name+'」':'模組「'+item.name+'」r'+item.version;
  const question=operation==='restore'?'還原 '+description+'？不會操作手機。':'永久刪除 '+description+' 的這份備份？此操作無法復原。';
  if(!confirm(question))return;
  pending={command:'module_trash_'+operation,token:crypto.randomUUID(),request_id:null};render();
  const receipt=await send(pending.command,{backup_id:item.backup_id,backup_hash:item.backup_hash,item_type:item.item_type||'module',confirmed:true,request_token:pending.token});
  if(receipt){pending.request_id=receipt.request_id;await reload();}
  else{pending=null;el('moduleTrashNotice').textContent='操作未送出：'+error();render();}
 }
 el('moduleTrashOpen').onclick=()=>{el('moduleTrashDialog').showModal();refresh();};
 el('moduleTrashReload').onclick=()=>refresh();
 el('moduleTrashSelect').onchange=()=>{selection=el('moduleTrashSelect').value;render();};
 el('moduleTrashRestore').onclick=()=>act('restore');
 el('moduleTrashPurge').onclick=()=>act('purge');
 return {update(){
  const s=read(),status=pending?.request_id&&s.current?.requests?.[pending.request_id];
  if(status&&!['QUEUED','RUNNING'].includes(status)){
   const request=pending;pending=null;
   const result=s.current.editor_result;
   if(!['READY','SUCCESS'].includes(status)){
    const log=[...(s.current.logs||[])].reverse().find(i=>i.request_id===request.request_id);
    el('moduleTrashNotice').textContent='操作失敗：'+(log?.message||status);
   }else if(result?.command!==request.command||result.request_token!==request.token){
    el('moduleTrashNotice').textContent='回應已更新，請重新載入垃圾桶。';
   }else if(request.command==='module_trash'){
    items=result.definition.items;
    const select=el('moduleTrashSelect');select.replaceChildren(new Option('選擇已刪除的項目',''));
    for(const item of items)select.add(new Option((item.item_type==='canvas'?'Canvas · ':'模組 · ')+item.name+(item.item_type==='canvas'?'':' · r'+item.version)+' · '+(item.deleted_at?new Date(item.deleted_at).toLocaleString():'離線備份'),item.backup_id));
    el('moduleTrashNotice').textContent=after+'垃圾桶共 '+items.length+' 份備份。'+(result.definition.skipped?' '+result.definition.skipped+' 份無法使用。':'')+(result.definition.truncated?' 僅列最近 100 份。':'');
   }else{
    const restored=result.definition.restored,canvas=result.definition.restored_canvas;
    if(restored&&!s.dirty){const saved=s.current.module_catalog.find(m=>m.id===restored.id);if(saved){load(saved);el('workflow').value=saved.id;}}
    refresh(restored||canvas?'已還原「'+(restored||canvas).name+'」。':'已永久刪除所選備份。');
   }
  }
  render();
 }};
};
