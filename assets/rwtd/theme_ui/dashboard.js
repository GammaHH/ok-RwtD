'use strict';
(()=>{
 const el=id=>document.getElementById(id),params=new URLSearchParams(location.hash.slice(1));
 const token=params.get('token')||sessionStorage.getItem('rwtd-theme-token:'+location.origin);
 const parentOrigin=params.get('parent_origin'),headers={Authorization:'Bearer '+token,'Content-Type':'application/json'};
 let saved=null,draft=null,pending=null,busy=false,loaded=false;
 const invalid=()=>[...el('colors').querySelectorAll('input[type=text]')].some(field=>!field.checkValidity());
 const dirty=()=>!!saved&&(JSON.stringify(draft)!==JSON.stringify(saved.palette)||invalid());
 window.rwtdThemeHasDraft=dirty;
 function message(text){el('status').textContent=text;}
 function controls(){for(const id of ['save','reload','defaults','close'])el(id).disabled=!!pending;el('save').disabled=!!pending||!dirty()||invalid()||!saved;el('defaults').disabled=!!pending||!saved;for(const field of el('colors').querySelectorAll('input'))field.disabled=!!pending;}
 function preview(){window.RwtdThemeSync?.apply(draft);controls();}
 function render(settings){
  saved=settings;draft=structuredClone(settings.palette);el('colors').replaceChildren();
  for(const [key,value] of Object.entries(draft)){
   const row=document.createElement('div');row.className='colorRow';
   const label=document.createElement('label'),picker=document.createElement('input'),text=document.createElement('input');
   label.textContent=settings.labels[key]||key;label.htmlFor='color-'+key;
   picker.type='color';picker.value=value.slice(0,7);picker.setAttribute('aria-label',label.textContent+' 選色器');
   text.type='text';text.id='color-'+key;text.value=value;text.maxLength=key==='selection_fill'?9:7;
   text.pattern=key==='selection_fill'?'#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?':'#[0-9a-fA-F]{6}';
   picker.oninput=()=>{draft[key]=(picker.value+(draft[key].length===9?draft[key].slice(7):'')).toUpperCase();text.value=draft[key];preview();};
   text.oninput=()=>{if(text.checkValidity()){draft[key]=text.value.toUpperCase();picker.value=draft[key].slice(0,7);preview();}else controls();};
   row.append(label,picker,text);el('colors').append(row);
  }
  loaded=true;preview();
 }
 async function send(command,payload={}){
  if(pending)return;
  if(command==='save'&&[...el('colors').querySelectorAll('input[type=text]')].some(field=>!field.reportValidity()))return;
  try{
   pending={id:crypto.randomUUID(),command};controls();
   const response=await fetch('/api/command',{method:'POST',headers,body:JSON.stringify({request_id:pending.id,command,payload})});
   const result=await response.json();if(!response.ok)throw Error(result.error||'請求失敗');
   message('等待配色設定 TASK 處理…');await poll();
  }catch(error){pending=null;message(error.message);controls();}
 }
 async function poll(){
  if(busy)return;busy=true;
  try{
   const response=await fetch('/api/state',{headers});if(!response.ok)throw Error('配色設定 TASK 已停止或連線失敗');
   const state=await response.json();
   if(!loaded){render(state.settings);message('修改只預覽本頁；儲存後才套用到 Qt 與工作區。');}
   const result=pending&&state.results[pending.id];
   if(result&&!['QUEUED','RUNNING'].includes(result.status)){
    const request=pending;pending=null;
    if(result.status==='SUCCESS'){
     render(state.settings);
     if(request.command==='save'){
      message(result.native_update_queued?'已儲存；Qt 更新已排入套用。':'已儲存；Qt 下次啟動會載入此配色。');
      window.RwtdThemeSync?.refresh();
      if(parentOrigin&&parent!==window)parent.postMessage({type:'rwtd-theme-saved'},parentOrigin);
     }else message('已重新載入儲存的配色。');
    }else message(result.reason||'配色設定失敗');
    controls();
   }
  }catch(error){message(error.message);}finally{busy=false;}
 }
 el('save').onclick=()=>{if(saved)send('save',{palette:draft,expected_revision:saved.revision});};
 el('reload').onclick=()=>{if(!dirty()||confirm('放棄目前配色草稿，重新載入？'))send('reload');};
 // Defaults need to retain the stored baseline for the unsaved-change check.
 el('defaults').onclick=()=>{if(!saved)return;const original=saved;render({...saved,palette:structuredClone(saved.defaults)});saved=original;preview();message('已預覽預設配色；按「儲存並套用」才會保存。');};
 el('close').onclick=()=>{if(pending){message('請等待儲存完成。');return;}if(dirty()&&!confirm('放棄目前未儲存的配色草稿？'))return;if(parentOrigin&&parent!==window){if(dirty())render(saved);parent.postMessage({type:'rwtd-workspace-dashboard'},parentOrigin);}else send('close');};
 window.addEventListener('message',event=>{
  if(!parentOrigin||event.origin!==parentOrigin||event.source!==parent)return;
  if(event.data?.type==='rwtd-workspace-can-leave')parent.postMessage({type:'rwtd-workspace-leave-result',id:event.data.id,reason:pending?'配色設定尚未完成。':dirty()?'配色尚未儲存；請先儲存或重新載入。':''},parentOrigin);
  if(event.data?.type==='rwtd-workspace-activate'&&!dirty())send('reload');
 });
 controls();poll();const timer=setInterval(poll,1000);window.addEventListener('pagehide',()=>clearInterval(timer));
})();
