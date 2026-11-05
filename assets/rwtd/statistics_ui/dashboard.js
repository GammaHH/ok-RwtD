'use strict';
(()=>{
 const $=id=>document.getElementById(id),hash=new URLSearchParams(location.hash.slice(1));
 const key='rwtd-statistics:'+location.origin,token=hash.get('token')||sessionStorage.getItem(key);
 const parentKey=key+':parent',parentOrigin=hash.get('parent_origin')||sessionStorage.getItem(parentKey);
 const embedded=window.parent!==window&&/^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(parentOrigin||'');
 if(token)sessionStorage.setItem(key,token);if(embedded)sessionStorage.setItem(parentKey,parentOrigin);history.replaceState(null,'',location.pathname);
 let state={},busy=false;
 async function read(){const response=await fetch('/api/state',{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw Error('統計資料讀取失敗');return response.json();}
 function hint(){const versions=state.versions?.[$('target').value]||[];$('latest').textContent='最新版本：'+(versions.at(-1)||'尚無資料');}
 function catalog(){const value=$('target').value;for(const option of [...$('target').options])if(option.value.startsWith('activity:'))option.remove();for(const activity of state.activities||[]){const option=document.createElement('option');option.value='activity:'+activity.id;option.textContent='活動：'+activity.name;$('target').append(option);}if([...$('target').options].some(o=>o.value===value))$('target').value=value;hint();}
 function minutes(value){return value==null?'—':(value/60).toFixed(2);}
 function render(){catalog();$('summary').replaceChildren();for(const group of state.report?.groups||[]){const tr=document.createElement('tr');for(const value of [group.canvas_version||'未標版本',group.canvas_names.join('、'),group.available_rounds+' / '+group.rounds,minutes(group.mean_seconds),minutes(group.median_seconds),minutes(group.p90_seconds),minutes(group.shortest?.seconds)+' / '+minutes(group.longest?.seconds)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('summary').append(tr);}$('report').textContent=state.markdown||'';$('downloads').hidden=!state.report;}
 async function command(command,payload={}){if(busy)throw Error('請等待目前統計完成');busy=true;$('generate').disabled=$('refresh').disabled=true;try{const id=crypto.randomUUID(),response=await fetch('/api/command',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({request_id:id,command,payload})});if(!response.ok)throw Error((await response.json()).error||'指令失敗');for(let i=0;i<1200;i++){state=await read();const result=state.results?.[id];if(result?.status==='FAILED')throw Error(result.reason);if(result?.status==='SUCCESS'){render();return;}await new Promise(resolve=>setTimeout(resolve,250));}throw Error('統計仍在處理，請稍後更新清單');}finally{busy=false;$('generate').disabled=$('refresh').disabled=false;}}
 $('filters').oninput=()=>{hint();};
 $('filters').onsubmit=async event=>{event.preventDefault();try{const target=$('target').value,[scope]=target.split(':'),activity=scope==='activity'?target.slice(9):'';await command('generate',{scope,stage:scope==='farm'?Number(target.slice(5)):1,activity,rounds:Number($('rounds').value),version:$('version').value});$('message').textContent='已完成：'+state.report.rounds+' 筆；'+state.output;}catch(error){$('message').textContent=error.message;}};
 $('refresh').onclick=()=>command('refresh').catch(error=>{$('message').textContent=error.message;});
 $('close').onclick=()=>{if(embedded){parent.postMessage({type:'rwtd-workspace-dashboard'},parentOrigin);return;}if(!busy)command('close').catch(error=>{$('message').textContent=error.message;});};
 function download(field,extension){const url=URL.createObjectURL(new Blob([(field==='csv'?'\ufeff':'')+state[field]],{type:'text/plain;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='rwtd-rounds-'+Date.now()+extension;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
 $('csv').onclick=()=>download('csv','.csv');$('markdown').onclick=()=>download('markdown','.md');
 addEventListener('message',event=>{if(!embedded||event.origin!==parentOrigin||event.source!==parent)return;if(event.data?.type==='rwtd-workspace-can-leave')parent.postMessage({type:'rwtd-workspace-leave-result',id:event.data.id,reason:busy?'統計尚未完成，請稍後再切換。':''},parentOrigin);if(event.data?.type==='rwtd-workspace-activate'&&!busy)command('refresh').catch(error=>{$('message').textContent=error.message;});});
 read().then(value=>{state=value;render();}).catch(error=>{$('message').textContent=error.message;});
})();
