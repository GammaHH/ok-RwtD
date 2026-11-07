'use strict';
// View adapter only. Native Farm owns scheduling, capture and Android input.
window.createFarmWorkspacePanel=function(api){
 const root=document.createElement('section');root.className='editorPanel';
 const title=document.createElement('h2');title.textContent='FARM';
 const status=document.createElement('p');status.setAttribute('role','status');
 const controls=document.createElement('div');controls.className='toolbar';
 const start=document.createElement('button'),stop=document.createElement('button'),save=document.createElement('button');
 start.textContent='啟動 FARM';stop.textContent='停止 FARM';save.textContent='儲存 FARM 設定';controls.append(start,stop,save);
 const live=document.createElement('p'),diagram=document.createElement('div'),fields=document.createElement('div');fields.className='fields';
 const note=document.createElement('p');note.textContent='引用原生 FARM TASK 的設定與執行服務；請先手動停止並儲存，才能切換。圖中顯示實際執行的 Canvas，含插入活動。';
 const triggerHeading=document.createElement('h3'),triggerTasks=document.createElement('div'),triggerRequests=document.createElement('div');
 triggerHeading.textContent='Trigger 排程／操作佇列';triggerTasks.setAttribute('aria-label','FARM Trigger 狀態');triggerRequests.setAttribute('aria-label','FARM Trigger 待辦與結果');
 root.append(title,note,controls,status,live,diagram,triggerHeading,triggerTasks,triggerRequests,fields);
 let dirty=false,busy=false,state={},expected=null,inputs=new Map(),activities=[];
 const mark=()=>{dirty=true;status.textContent='有未儲存的 FARM 設定';paint();};
 function paint(){start.disabled=busy||dirty||!!state.running||!state.available;stop.disabled=busy||!state.running;save.disabled=busy||!!state.running||!dirty;for(const input of fields.querySelectorAll('input,select,textarea,button'))input.disabled=busy||!!state.running;}
 function activityEditor(raw,canvases){
  activities=typeof raw==='string'?JSON.parse(raw||'[]'):structuredClone(raw||[]);
  const wrap=document.createElement('div'),heading=document.createElement('h3'),rows=document.createElement('div'),add=document.createElement('button');
  heading.textContent='活動排程（共用原生 FARM 存檔）';add.textContent='新增活動';add.type='button';
  function render(){rows.replaceChildren();for(const entry of activities){const row=document.createElement('div');row.className='toolbar';
   const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=entry.enabled!==false;enabled.title='啟用';enabled.onchange=()=>{entry.enabled=enabled.checked;mark();};row.append(enabled);
   const canvas=document.createElement('select');for(const item of canvases)canvas.add(new Option(item.name,item.id));canvas.value=entry.canvas||'';if(!canvases.some(c=>c.id===entry.canvas)&&entry.canvas)canvas.add(new Option('已移除：'+entry.canvas,entry.canvas,true,true));canvas.onchange=()=>{entry.canvas=canvas.value;mark();};row.append(canvas);
   for(const [key,label,defaultValue,max] of [
    ['initial_runs','首次次數',1,1000],
    ['interval_days','天',0],
    ['interval_hours','小時',0],
    ['interval_minutes','分鐘',0],
    ['max_pending_runs','定期排程最多累積場數（0＝不限）',0,1000],
   ]){
    const field=document.createElement('label'),input=document.createElement('input');
    field.textContent=label;input.type='number';input.min='0';input.step='1';
    if(max!==undefined)input.max=String(max);
    input.value=entry[key]??defaultValue;
    input.oninput=()=>{entry[key]=Number(input.value);mark();};
    field.append(input);row.append(field);
   }
   const remove=document.createElement('button');remove.type='button';remove.textContent='移除';remove.onclick=()=>{activities=activities.filter(a=>a!==entry);mark();render();};row.append(remove);rows.append(row);
  }paint();}
  add.onclick=()=>{activities.push({id:'event_'+crypto.randomUUID(),canvas:canvases[0]?.id||'',enabled:true,initial_runs:1,interval_minutes:190});mark();render();};
  wrap.append(heading,rows,add);render();return wrap;
 }
 function settingsForm(){fields.replaceChildren();inputs=new Map();expected=structuredClone(state.settings||{});
  for(const [key,value]of Object.entries(expected)){if(key==='activity_schedules')continue;const label=document.createElement('label');label.textContent=key;label.title=state.descriptions?.[key]||'';
   let input;const choices=key==='Canvas File'?['目前選取',...(state.canvases||[]).map(c=>c.id+' · '+c.name)]:state.types?.[key]?.options;
   if(choices){input=document.createElement('select');for(const option of choices)input.add(new Option(option,option));if(!choices.includes(value))input.add(new Option(value,value));input.value=value;}
   else{input=document.createElement('input');input.type=typeof value==='boolean'?'checkbox':typeof value==='number'?'number':'text';if(input.type==='checkbox')input.checked=value;else input.value=value;if(input.type==='number')input.step='any';}
   input.oninput=mark;input.onchange=mark;inputs.set(key,input);label.append(input);fields.append(label);
  }
  if(Object.hasOwn(expected,'activity_schedules'))fields.append(activityEditor(expected.activity_schedules,state.canvases||[]));
 }
 const canvasMonitor=window.RwtdCanvasView.createMonitor(diagram);
 function draw(){canvasMonitor.update(state);RwtdCanvasView.renderTriggerStatus(triggerTasks,triggerRequests,{graph:state.graph||{nodes:[]},triggerState:state.trigger_runtime||{tasks:[],queue:[]}});}
 async function refresh(){try{state=await api.read();if(!dirty&&!busy&&JSON.stringify(expected)!==JSON.stringify(state.settings||{}))settingsForm();live.textContent=`${state.running?'執行中':'已停止'}｜Canvas：${state.canvas||'尚未啟動'}｜CHAIN：${state.chain||'—'}｜節點：${state.node||'—'}｜完成輪次：${state.rounds??0}`;draw();paint();}catch(error){status.textContent=error.message;}}
 async function act(command,payload){if(busy)return;busy=true;paint();try{await api.command(command,payload);if(command==='farm_save'){dirty=false;expected=null;}status.textContent='操作完成';}catch(error){status.textContent=error.message;}finally{busy=false;await refresh();}}
 start.onclick=()=>act('farm_start',{});stop.onclick=()=>act('farm_stop',{});
 save.onclick=()=>{try{const settings=structuredClone(expected);for(const [key,input]of inputs){const value=expected[key];settings[key]=typeof value==='boolean'?input.checked:typeof value==='number'?Number(input.value):input.value;}if(Object.hasOwn(settings,'activity_schedules'))settings.activity_schedules=JSON.stringify(activities);act('farm_save',{settings,expected});}catch(error){status.textContent=error.message;}};
 return {element:root,refresh,canLeave:()=>busy?'FARM 設定／指令仍在處理中。':dirty?'FARM 有未儲存設定，請先儲存。':state.running?'請先手動停止 FARM，等待退出後再切換。':''};
};

