'use strict';
const $=id=>document.getElementById(id),authKey='dashboard-token:'+location.origin;
const token=new URLSearchParams(location.hash.slice(1)).get('token')||sessionStorage.getItem(authKey);
window.rwtdWorkspaceInitialView=new URLSearchParams(location.hash.slice(1)).get('workspace_view');
if(token)sessionStorage.setItem(authKey,token);history.replaceState(null,'',location.pathname);
const headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'};
let pendingRename=null;
let current=null,busyPoll=false,steps=[],urls={},editing=null,dirty=false,catalogKey='',editorResultKey='',cleanupPlan=null,historySelection=null,displayedFrameId=null;
let editorIndex=null,editorKind='new',editorContext=null,editorOriginal=null,editorInitial='',moduleTags={},moduleGlobalRoi=null,stepSettings=null;
// Step gates are independent of the touch target. No OCR text becomes a point.
let stepConditionDraft=[];
window.readStepConditionDraft=()=>structuredClone(stepConditionDraft);
function conditionEffectFields(){const action=$('stepConditionEffect').value;$('stepConditionWhenLabel').hidden=action==='legacy';$('stepConditionRange').hidden=action!=='skip_range';$('stepLoopOptions').hidden=action!=='retry_step';$('stepConditionMatchLabel').hidden=!['skip_range','skip_remaining'].includes(action);$('stepConditionLegacyNotice').hidden=!['skip_range','skip_remaining'].includes(action)||$('stepConditionLegacyNotice').dataset.legacy!=='true';}
function fillConditionEffect(p){const effect=p.condition_effect;const loopStart=$('stepLoopStart');loopStart.replaceChildren();const loopIndex=steps.findIndex(s=>s.step_id===p.step_id),past=loopIndex<0?steps:steps.slice(0,loopIndex);loopStart.add(new Option('選擇要重做的 Step',''));for(const item of past.filter(s=>!s.condition_effect||s.condition_effect.action==='skip_step'))loopStart.add(new Option(item.step_id+' · '+item.name,item.step_id));loopStart.value=effect?.retry_step_id||past.at(-1)?.step_id||'';$('stepLoopMax').value=effect?.max_cycles??5;$('stepLoopWait').value=effect?.wait_seconds??1;$('stepLoopTimeout').value=effect?.timeout_seconds??60;$('stepConditionEffect').value=effect?.action||'legacy';$('stepConditionWhen').value=effect?.when||'YES';$('stepConditionSkipSelfMatch').checked=(effect?.skip_self_on_match??effect?.skip_self_on_mismatch)===true;$('stepConditionLegacyNotice').dataset.legacy=String(!!effect&&Object.hasOwn(effect,'skip_self_on_mismatch'));const index=steps.findIndex(s=>s.step_id===p.step_id),future=index<0?[]:steps.slice(index+1);for(const[id,key]of [['stepConditionRangeStart','start_step_id'],['stepConditionRangeEnd','end_step_id']]){const select=$(id);select.replaceChildren(new Option('選擇後續 Step',''));for(const step of future)select.add(new Option(step.step_id+' · '+step.name,step.step_id));if(effect?.[key]&&!future.some(s=>s.step_id===effect[key]))select.add(new Option(effect[key]+'（無效或不在後續）',effect[key]));select.value=effect?.[key]||'';}conditionEffectFields();}
function conditionEffectParameters(p){delete p.condition_effect;const action=$('stepConditionEffect').value;if(action==='legacy')return p;p.condition_effect={action,when:$('stepConditionWhen').value};if(['skip_range','skip_remaining'].includes(action))p.condition_effect.skip_self_on_match=$('stepConditionSkipSelfMatch').checked;if(action==='retry_step')Object.assign(p.condition_effect,{retry_step_id:$('stepLoopStart').value,max_cycles:Number($('stepLoopMax').value),wait_seconds:Number($('stepLoopWait').value),timeout_seconds:Number($('stepLoopTimeout').value)});if(action==='skip_range')Object.assign(p.condition_effect,{start_step_id:$('stepConditionRangeStart').value,end_step_id:$('stepConditionRangeEnd').value});return p;}
for(const id of ['stepConditionEffect','stepConditionWhen','stepConditionRangeStart','stepConditionRangeEnd','stepConditionSkipSelfMatch','stepLoopStart','stepLoopMax','stepLoopWait','stepLoopTimeout'])$(id).onchange=()=>{conditionEffectFields();updateEditorExecution();};
function conditionControl(label,type,value,change,options=null){const wrap=document.createElement('label'),el=document.createElement(options?'select':'input');wrap.append(document.createTextNode(label));if(options){for(const [v,t]of options){const o=document.createElement('option');o.value=v;o.textContent=t;el.append(o);}}else el.type=type;el.value=value??'';el.onchange=()=>{change(el.value);updateEditorExecution();};wrap.append(el);return wrap;}
function renderStepConditions(){const host=$('stepConditionsRows');host.replaceChildren();for(const[rowIndex,row]of stepConditionDraft.entries()){const box=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent='條件 '+(rowIndex+1);box.append(legend);if(rowIndex)box.append(conditionControl('連接','',row.join||'AND',v=>row.join=v,[['AND','AND'],['OR','OR']]));box.append(conditionControl('類型','',row.type,v=>{stepConditionDraft[rowIndex]={type:v,join:row.join||'AND',...(v==='coco'?{feature:'',threshold:.8,match:'detected'}:v==='ocr'?{text:'',threshold:.8,match:'contains'}:{rgb:[0,255,0],tolerance:10,min_pixels:9,match:'detected'})};renderStepConditions();},[['coco','COCO'],['color','顏色'],['ocr','OCR']]));if(row.type==='coco')box.append(conditionControl('Feature','',row.feature,v=>row.feature=v,[['','選擇 Feature'],...(current?.feature_catalog||[]).map(f=>{const n=typeof f==='string'?f:f.name||f.feature;return[n,n];})]));if(row.type==='ocr')box.append(conditionControl('預期文字','text',row.text,v=>row.text=v));if(row.type!=='color')box.append(conditionControl('信心門檻','number',row.threshold??.8,v=>row.threshold=Number(v)));else{box.append(conditionControl('RGB','text',row.rgb?.join(',')||'0,255,0',v=>row.rgb=v.split(',').map(Number)));box.append(conditionControl('容差 ±','number',row.tolerance??10,v=>row.tolerance=Number(v)));box.append(conditionControl('最小色塊','number',row.min_pixels??9,v=>row.min_pixels=Number(v)));}box.append(conditionControl('判斷','',row.match||(row.type==='ocr'?'contains':'detected'),v=>row.match=v,row.type==='ocr'?[['contains','包含'],['exact','完全相同']]:[['detected','存在'],['disappeared','不存在']]));box.append(conditionControl('ROI TAG','',row.roi_tag||'',v=>{if(v){row.roi_tag=v;delete row.roi;}else delete row.roi_tag;renderStepConditions();},[['','自行指定／沿用模組全域'],...Object.entries(stepSettings?.tags()||moduleTags).map(([id,tag])=>[id,tag.name])]));if(!row.roi_tag)box.append(conditionControl('ROI x,y,width,height（空白沿用全域）','text',row.roi?.join(',')||'',v=>{if(v.trim())row.roi=v.split(',').map(Number);else delete row.roi;}));const remove=document.createElement('button');remove.type='button';remove.textContent='移除條件';remove.onclick=()=>{stepConditionDraft.splice(rowIndex,1);renderStepConditions();updateEditorExecution();};box.append(remove);host.append(box);}}
$('addStepCondition').onclick=()=>{if(stepConditionDraft.length>=30){$('editorNotice').textContent='最多 30 個條件';return;}stepConditionDraft.push({type:'coco',join:'AND',feature:'',threshold:.8,match:'detected'});renderStepConditions();updateEditorExecution();};
function renderConditionReport(container,report){for(const [key,label,id]of [['input_conditions','操作前條件','stepConditionsResult'],['target_conditions','目標條件','targetConditionResult']]){const group=report?.[key];if(!group)continue;const summary=document.createElement('small');summary.textContent=label+' '+group.result+' · '+group.clauses.map((r,i)=>`${i+1}:${r.result}${r.reason?' ('+r.reason+')':''}`).join(' / ');container.append(summary);if(editorOpen()&&(report.step_id||report.parameters?.step_id)===$('stepId').value)$(id).textContent=summary.textContent;}}
let editorFrozen=null,displayedFrameBlob=null;
window.loadGuardRoiBackground=async feature=>{
 if(displayedFrameBlob)return {blob:displayedFrameBlob,label:'已取得的手機畫面（僅供 ROI 編輯）'};
 const response=await fetch('/api/image?which=reference&feature='+encodeURIComponent(feature),{headers});
 if(!response.ok)throw Error('找不到此 Feature 的原圖；仍可直接輸入 ROI 座標。');
 return {blob:await response.blob(),label:'COCO 標記原圖（僅供 ROI 編輯）'};
};
let deviceDispatchPending=false,deviceRequestId=null,connectionHealthy=false;
let editorColorHits=[],editorColorKey='',editorColorCache=[];
let editorQualifiedCoco=[];
const roiEditor=createRoiEditor($('editorBoxes'),['roiX','roiY','roiW','roiH'].map($),(roi,error)=>{if(error)$('editorNotice').textContent=error;stepSettings?.roiChanged(roi);updateEditorExecution();},()=>editorFrameUsable()&&!$('editorFrame').hidden&&(stepSettings?.roiEditable()??false));
const gestureEditor=createGestureEditor($('gestureBoxes'),{x:$('x'),y:$('y'),endx:$('endX'),endy:$('endY'),midx:$('midX'),midy:$('midY')},()=>{stepSettings.setFixed();updateGesture();updateEditorExecution();},()=>editorFrameUsable()&&!$('gestureFrame').hidden);
stepSettings=createStepSettings(roiEditor,()=>{renderStepConditions();updateGesture();renderEditorMatches();updateEditorExecution();});
const decisionEditor=createDecisionEditor(send);
window.rwtdWorkspaceCanLeave=()=>{
 if(document.querySelector('dialog[open]'))return '請先完成儲存或取消目前的編輯視窗，再切換頁籤。';
 if(dirty)return '模組有未儲存修改，請先按「儲存模組」再切換頁籤。';
 if(window.rwtdCanvasHasUnsavedChanges?.())return 'Canvas 有未儲存修改，請先套用節點設定並按「儲存」再切換頁籤。';
 return '';
};
const workspaceScript=document.createElement('script');workspaceScript.src='/workspace_tabs.js';document.head.append(workspaceScript);
let preferencesLoaded=false;const judgmentRequests=new Map();
function previewHits(){return current?.frame?.recognition_valid?(current.matches||[]).filter(m=>m.bbox&&Number.isFinite(m.confidence)&&m.confidence>=Number($('previewThreshold').value)):[];}
function renderExclusionRegions(){for(const region of Object.values(current?.scheduler_runtime?.exclusion_regions||{})){if(!RoiGeometry.valid(region.bbox))continue;const [x,y,w,h]=region.bbox,rect=document.createElementNS('http://www.w3.org/2000/svg','rect'),text=document.createElementNS('http://www.w3.org/2000/svg','text');for(const[k,v]of Object.entries({x,y,width:w,height:h,fill:'#ff445533',stroke:'#ff4455','stroke-width':6}))rect.setAttribute(k,v);for(const[k,v]of Object.entries({x,y:Math.max(35,y-8),fill:'#ff6677','font-size':32}))text.setAttribute(k,v);text.textContent=region.feature+' 禁止點擊（'+(region.simulated?'模擬':'實機記憶')+'）';$('boxes').append(rect,text);}}
function updatePreviewThreshold(){ $('previewThresholdValue').textContent=Number($('previewThreshold').value).toFixed(2);renderBoxes();if(current?.frame?.recognition_valid)$('recognitionStatus').textContent='COCO Detected: '+previewHits().length;}
$('previewThreshold').oninput=updatePreviewThreshold;
$('previewThreshold').onchange=()=>send('dashboard_preferences',{preview_threshold:Number($('previewThreshold').value)});
function applyPreviewSize(){const panel=document.querySelector('.previewPanel'),top=panel.querySelector('.mainFrame').getBoundingClientRect().top,footer=panel.querySelector('.captureTools').getBoundingClientRect().height+panel.querySelector('.previewSize').getBoundingClientRect().height+panel.querySelector('.previewThreshold').getBoundingClientRect().height+48,scale=Number($('previewScale').value)/100,width=WorkflowModel.frameWidth(innerWidth,innerHeight-top-footer,scale);document.documentElement.style.setProperty('--frame-width',width+'px');$('previewScaleValue').textContent=Math.round(scale*100)+'%';}
try{$('previewScale').value=localStorage.getItem('rwtd-preview-scale')||100;}catch{}
$('previewScale').oninput=()=>{applyPreviewSize();try{localStorage.setItem('rwtd-preview-scale',$('previewScale').value);}catch{}};
$('previewScale').onchange=()=>send('dashboard_preferences',{preview_scale:Number($('previewScale').value)});
$('resetPreviewScale').onclick=()=>{$('previewScale').value=100;applyPreviewSize();try{localStorage.setItem('rwtd-preview-scale','100');}catch{}send('dashboard_preferences',{preview_scale:100});};
addEventListener('resize',applyPreviewSize);applyPreviewSize();
function moduleDisplayOffset(points=false){const rule=current?.module_display_mapping?.[editing?.id];return points&&rule?.point_exempt_steps.includes(String($('stepId').value))?0:rule?.offset_y||0;}
function updateGesture(){const kind=WorkflowModel.action({name:$('action').value}),action=kind==='swipe'?'SwipeCalibration':kind==='wait'?'WaitTest':'TapTest',view=editorView(),hit=$('targetMode').value!=='ocr'&&view.frame?.recognition_valid?view.matches.find(m=>m.feature===stepSettings.referenceFeature()&&m.bbox&&Number.isFinite(m.confidence)&&m.confidence>=Number($('threshold').value)&&($('targetMode').value!=='coco_color'||editorQualifiedCoco.includes(m))):null;const previewTarget=$('targetMode').value==='color'?(editorColorHits.length===1?editorColorHits[0]:null):(hit||(['coco_or_color'].includes($('targetMode').value)&&editorColorHits.length===1?editorColorHits[0]:null));const ghost=previewTarget?.bbox?[Math.min(1079,Math.round(previewTarget.bbox[0]+previewTarget.bbox[2]/2)),Math.min(2399,Math.round(previewTarget.bbox[1]+previewTarget.bbox[3]/2))]:null;gestureEditor.configure(action,$('stepId').value,ghost,$('threePointSwipe').checked,moduleDisplayOffset(true));$('threePointLabel').hidden=kind!=='swipe';$('midX').disabled=$('midY').disabled=kind!=='swipe'||!$('threePointSwipe').checked;$('gestureFrame').parentElement.hidden=kind==='wait';document.querySelector('.gestureEditor').classList.toggle('waitOnly',kind==='wait');document.querySelector('.gestureTypes').hidden=kind==='wait';document.querySelector('[data-tab="recognition"]').hidden=kind==='wait';for(const button of document.querySelectorAll('[data-ui-action]'))button.setAttribute('aria-pressed',String(button.dataset.uiAction===kind));$('durationLabel').hidden=kind==='wait';$('repeatLabel').hidden=kind!=='tap';$('waitStepNotice').hidden=kind!=='wait';$('durationText').textContent=kind==='swipe'?'滑動時間':'按住時間（預設 50 毫秒）';$('duration').min=kind==='swipe'?1:0;$('gestureHint').textContent=kind==='swipe'?($('threePointSwipe').checked?'拖曳起點、中間點、終點；中途不抬手。':'拖出起點與終點；COCO 模式以辨識目標中心作起點。'):kind==='wait'?'純等待，不送出觸控。':$('targetMode').value!=='fixed'?'辨識定位；執行前重新取得目標，拖曳標記改用固定座標。':'點畫面放置標記，拖曳調整位置。';}
for(const button of document.querySelectorAll('[data-ui-action]'))button.onclick=()=>{$('action').value=button.dataset.uiAction==='swipe'?'SwipeCalibration':'TapTest';updateGesture();updateEditorExecution();};
function editorOpen(){return $('stepEditor').open;}
function selectTab(name){for(const b of document.querySelectorAll('[data-tab]'))b.setAttribute('aria-pressed',String(b.dataset.tab===name));for(const p of document.querySelectorAll('[data-panel]'))p.classList.toggle('hidden',p.dataset.panel!==name);}
function params(){let p=structuredClone(editorOriginal||{verification_policy:'manual',retry_policy:'never',timeout_seconds:5});const kind=WorkflowModel.action({name:$('action').value}),hold=Number($('duration').value);p.name=kind==='wait'?'WaitTest':kind==='swipe'?'SwipeCalibration':hold>0?'LongPressTest':'TapTest';p.step_id=$('stepId').value.trim();p.threshold=Number($('threshold').value);p.post_wait_seconds=Number($('delay').value);p.delay_seconds=kind==='wait'?0:editorOriginal?.delay_seconds??0;p=stepSettings.parameters(p);if(kind!=='wait'&&$('targetMode').value==='fixed'){p.x=$('x').value.trim()===''?null:Number($('x').value);p.y=$('y').value.trim()===''?null:Number($('y').value);}if(kind!=='wait')p.duration_ms=hold;if(kind==='tap')p.repeat_count=Number($('repeatCount').value);else delete p.repeat_count;if(kind==='swipe'){p.end_x=$('endX').value.trim()===''?null:Number($('endX').value);p.end_y=$('endY').value.trim()===''?null:Number($('endY').value);}if(kind==='swipe'&&$('threePointSwipe').checked){p.mid_x=$('midX').value.trim()===''?null:Number($('midX').value);p.mid_y=$('midY').value.trim()===''?null:Number($('midY').value);}else{delete p.mid_x;delete p.mid_y;}delete p.reversible_ack;delete p.workflow_module;if($('stepUnknownNoRetry').checked)p.unknown_no_retry=true;else delete p.unknown_no_retry;if(stepConditionDraft.length)p.input_conditions={evaluation:'left_to_right',clauses:structuredClone(stepConditionDraft)};else delete p.input_conditions;return conditionEffectParameters(p);}
function editorSignature(){return JSON.stringify({parameters:params(),tags:stepSettings.tags(),global_roi:stepSettings.global()});}
function fillStep(p){fillConditionEffect(p);stepConditionDraft=structuredClone(p.input_conditions?.clauses||[]);$('stepUnknownNoRetry').checked=p.unknown_no_retry===true;renderStepConditions();$('stepConditionsResult').textContent='執行後顯示各條件結果；預覽不執行條件。';for(const[id,key]of Object.entries({action:'name',stepId:'step_id',threshold:'threshold',x:'x',y:'y',endX:'end_x',endY:'end_y',midX:'mid_x',midY:'mid_y'}))$(id).value=p[key]??({threshold:.9}[id]??'');$('threePointSwipe').checked=p.mid_x!==undefined||p.mid_y!==undefined;$('duration').value=WorkflowModel.hold(p);$('repeatCount').value=WorkflowModel.repeats(p);$('delay').value=p.post_wait_seconds??(p.name==='WaitTest'?p.delay_seconds:0)??0;stepSettings.fill(p,moduleTags,steps,editorIndex,moduleGlobalRoi);renderStepConditions();$('legacyDelayNotice').hidden=p.name==='WaitTest'||!p.delay_seconds;$('legacyDelayNotice').textContent='保留舊設定：操作前延遲 '+(p.delay_seconds||0)+' 秒。';}
function openEditor(index=null,kind='new'){releaseEditorFreeze();editorIndex=index;editorKind=kind;editorContext={id:editing?.id,revision:editing?.revision};editorOriginal=index===null?null:structuredClone(steps[index]);let id=1;while(steps.some(s=>s.step_id===String(id)))id++;fillStep(editorOriginal||{step_id:String(id),name:'TapTest'});$('stepId').readOnly=index!==null;$('editorTitle').textContent='編輯 Step '+$('stepId').value;$('editorScope').textContent=(editing?.name||'新模組')+' · 步驟設定／模組共用 ROI';$('editorNotice').textContent='套用只更新草稿；儲存模組後即可執行一次。編輯不送出輸入。';selectTab('action');updateGesture();editorInitial=editorSignature();$('stepEditor').showModal();syncEditorFrame();renderEditorMatches();updateEditorExecution();}
function closeEditor(){if(editorSignature()!==editorInitial&&!confirm('放棄尚未套用的修改？'))return;$('stepEditor').close();}
function schedulerInputBlocked(){return current?.scheduler_runtime?.input_blocked??current?.scheduler_runtime?.locked??false;}
function canExecuteDevice(){return connectionHealthy&&current?.mode==='Manual Device Test'&&current?.manual_test_enabled!==false&&!deviceDispatchPending&&!current.device_pending&&!schedulerInputBlocked();}
function executionBlockReason(){
 if(!editing)return '請先儲存使用者模組。';
 if(dirty)return '有未儲存的修改：先按「儲存模組」。';
 if(!connectionHealthy)return 'Dashboard 尚未連線或任務已停止。';
 if(schedulerInputBlocked())return 'Scheduler 執行中：先停止排程，避免輸入競爭。';
 if(current?.device_input_stopped)return '操作已停止，請按上方「啟用手動操作」重新啟用。';
 if(current?.mode!=='Manual Device Test')return current?.manual_permission?'目前唯讀：按上方「啟用手動操作」。':'任務未允許手動操作：開啟 Allow Manual Device Test 並重啟任務。';
 if(deviceDispatchPending||current.device_pending)return '正在擷取或執行操作；完成後可執行。若持續忙碌，可先關閉自動刷新。';
 if(current.module_catalog.find(i=>i.id===editing.id)?.revision!==editing.revision)return '模組版本已改變，請重新載入。';
 return '';
}
function updateExecutionStatus(){const reason=executionBlockReason();$('executionStatus').textContent=reason||'整段模組依設定時間連續執行；完成後逐條勾選 Pass / Fail。';$('configure').textContent=editing?.kind==='system'?'儲存 System 模組':'儲存模組';return reason;}
function updateEditorExecution(){const saved=editorKind==='edit'&&!dirty&&editing?.id===editorContext?.id&&editing?.revision===editorContext?.revision&&current?.module_catalog.find(i=>i.id===editing.id)?.revision===editorContext?.revision&&editorSignature()===editorInitial,reason=executionBlockReason()||(!saved?'先套用修改並儲存模組，再重新開啟此步驟。':'');$('execute').disabled=!editorOpen()||!canExecuteDevice()||!!reason||!saved;$('execute').title=reason;$('editorExecutionStatus').textContent=reason;}
function editorView(){return editorFrozen||{frame:current?.frame||{},matches:current?.matches||[],url:urls.frame,frame_id:displayedFrameId,usable:usableFrame()&&connectionHealthy};}
function editorFrameUsable(){const view=editorView();return !!view.usable&&!view.frame.stale&&!!view.url&&view.frame_id===view.frame.frame_id;}
function releaseEditorFreeze(){if(editorFrozen)URL.revokeObjectURL(editorFrozen.url);editorFrozen=null;$('freezeEditorFrame').checked=false;}
function syncEditorFrame(){
 if(!editorOpen())return;
 const view=editorView(),usable=editorFrameUsable();roiEditor.setOffset(moduleDisplayOffset());
 for(const id of ['editorFrame','gestureFrame']){if(usable){$(id).src=view.url;$(id).hidden=false;const note=$(id).parentElement.querySelector('.emptyFrame');if(note)note.hidden=true;}else hideImage(id,'STALE · 尚無可編輯畫面');}
 $('freezeEditorFrame').disabled=!editorFrozen&&(!usable||!displayedFrameBlob);
 $('editorFrameStatus').textContent=(editorFrozen?'已凍結':'即時')+' · '+localTime(view.frame.captured_utc)+' · '+(view.frame.recognition_valid?'辨識 '+(view.frame.matched_count??0)+' 個目標':'辨識無效');
}
function renderEditorMatches(){
 if(!editorOpen())return;
 const view=editorView(),hits=editorFrameUsable()&&view.frame.recognition_valid?view.matches:[];
 $('editorDetections').replaceChildren();let reference=null;
 const colorMode=$('targetMode').value,colorEnabled=['color','coco_color','coco_or_color'].includes(colorMode),rgb=$('colorRgb').value.slice(1).match(/../g).map(v=>parseInt(v,16)),tolerance=Number($('colorTolerance').value),minimum=Number($('colorMinPixels').value),storedRoi=stepSettings.effectiveRoi(),roi=storedRoi?[storedRoi[0],storedRoi[1]+moduleDisplayOffset(),storedRoi[2],storedRoi[3]]:null;
 editorColorHits=[];
 editorQualifiedCoco=[];
 if(colorEnabled){try{if(!editorFrameUsable())throw Error('STALE · 等待有效編輯畫面');const key=JSON.stringify([view.frame_id,roi,rgb,tolerance,minimum]);if(key!==editorColorKey){editorColorCache=ColorPreview.components($('editorFrame'),roi,rgb,tolerance,minimum);editorColorKey=key;}editorColorHits=editorColorCache;$('colorPreviewStatus').textContent='預覽色塊：'+editorColorHits.length+(colorMode==='coco_or_color'&&$('allCocoTargets').checked?' · OR 合併 COCO 與顏色目標':editorColorHits.length===1?' · 唯一色塊':' · 單一顏色定位需唯一色塊');}catch(e){$('colorPreviewStatus').textContent=e.message;}}
 for(const hit of hits){
  if(hit.feature===stepSettings.referenceFeature())reference=hit.search_bbox??hit.reference_bbox??hit.bbox;
  if(!hit.bbox||!Number.isFinite(hit.confidence)||hit.confidence<Number($('threshold').value))continue;
  if(colorMode==='color'||colorMode==='ocr')continue;
  if(roi){const[x,y,w,h]=hit.bbox;if(x<roi[0]||y<roi[1]||x+w>roi[0]+roi[2]||y+h>roi[1]+roi[3])continue;}
  if(colorMode==='coco_color'){try{if(!ColorPreview.components($('editorFrame'),hit.bbox,rgb,tolerance,minimum).length)continue;}catch{continue;}}
  editorQualifiedCoco.push(hit);
  const[x,y,w,h]=hit.bbox,rect=document.createElementNS('http://www.w3.org/2000/svg','rect'),text=document.createElementNS('http://www.w3.org/2000/svg','text');
  for(const[k,v]of Object.entries({x,y,width:w,height:h,fill:'none',stroke:'#26fa7c','stroke-width':5}))rect.setAttribute(k,v);
  for(const[k,v]of Object.entries({x,y:Math.max(35,y-8),fill:'#26fa7c','font-size':32}))text.setAttribute(k,v);
  text.textContent=hit.feature+' '+(hit.evidence_kind==='annotation_fixture'?'標註（模擬）':Number(hit.confidence||0).toFixed(2));$('editorDetections').append(rect,text);
 }
 if(colorEnabled)for(const hit of editorColorHits){const[x,y,width,height]=hit.bbox,rect=document.createElementNS('http://www.w3.org/2000/svg','rect');for(const[k,v]of Object.entries({x,y,width,height,fill:'none',stroke:'#ffcc40','stroke-width':5}))rect.setAttribute(k,v);$('editorDetections').append(rect);}
 const referenceRoi=reference?[reference[0],reference[1]-moduleDisplayOffset(),reference[2],reference[3]]:null;roiEditor.reference(RoiGeometry.valid(referenceRoi)?referenceRoi:null);$('roiStatus').textContent='辨識座標 1080×2400 · 綠框：目前辨識命中；橘框：搜尋 ROI（不是命中）。'+(moduleDisplayOffset()?'新手機 ROI Y−50；欄位與存檔維持舊手機基準。':'')+(editorFrozen?'畫面及辨識結果已凍結。':'');
}
$('freezeEditorFrame').onchange=()=>{if($('freezeEditorFrame').checked){if(!editorFrameUsable()||!displayedFrameBlob){$('freezeEditorFrame').checked=false;return;}editorFrozen={frame:structuredClone(current.frame),matches:structuredClone(current.matches),frame_id:displayedFrameId,url:URL.createObjectURL(displayedFrameBlob),usable:true};}else releaseEditorFreeze();syncEditorFrame();renderEditorMatches();updateGesture();};
$('stepEditor').addEventListener('close',releaseEditorFreeze);
$('editorFrame').addEventListener('load',()=>{renderEditorMatches();updateGesture();});
for(const b of document.querySelectorAll('[data-tab]'))b.onclick=()=>selectTab(b.dataset.tab);
for(const b of document.querySelectorAll('[data-close]'))b.onclick=()=>$(b.dataset.close).close();
$('closeEditor').onclick=closeEditor;$('cancelEditor').onclick=closeEditor;$('stepEditor').addEventListener('cancel',e=>{e.preventDefault();closeEditor();});$('stepEditor').addEventListener('input',e=>{updateEditorExecution();if(['feature','threshold','colorRgb','colorTolerance','colorMinPixels'].includes(e.target.id))renderEditorMatches();updateGesture();});$('resetRoi').onclick=()=>roiEditor.reset();
$('threePointSwipe').onchange=()=>{if($('threePointSwipe').checked&&['x','y','endX','endY'].every(id=>$(id).value.trim()!=='')){if($('midX').value.trim()===''||$('midY').value.trim()===''){$('midX').value=Math.round((Number($('x').value)+Number($('endX').value))/2);$('midY').value=Math.round((Number($('y').value)+Number($('endY').value))/2);}}updateGesture();updateEditorExecution();};
$('newStep').onclick=()=>openEditor();$('newWaitStep').onclick=()=>{openEditor();$('action').value='WaitTest';$('delay').value=1;updateGesture();};
async function send(command,payload={}){try{const r=await fetch('/api/command',{method:'POST',headers,body:JSON.stringify({request_id:crypto.randomUUID(),command,payload})}),v=await r.json();if(!r.ok)throw Error(v.error);$('notice').textContent='已排入 '+command+'；輸入完成不等於成功。';return v;}catch(e){diagnosticError=e.message;$('notice').textContent=safeMessage(e.message);updateDiagnostics();return null;}}
async function dispatchDevice(command,payload){if(!canExecuteDevice())return;deviceDispatchPending=true;renderSteps();updateEditorExecution();$('executeModule').disabled=true;const result=await send(command,payload);if(result){deviceRequestId=result.request_id;await poll();}else{deviceDispatchPending=false;renderSteps();updateEditorExecution();}}
function executeStep(step,moduleId){if(dirty||!moduleId){$('notice').textContent='先儲存模組再執行步驟。';return;}try{WorkflowModel.validateRepeats(step);}catch(e){$('editorNotice').textContent=$('notice').textContent=e.message;return;}return dispatchDevice('device_test',{...step,workflow_module:moduleId,reversible_ack:true,dashboard_manual:true});}
function renderSteps(){const reason=updateExecutionStatus();$('steps').replaceChildren();for(const[index,s]of steps.entries()){let row=document.createElement('tr');const batch=current?.module_run?.module===editing?.id&&current.module_run.revision===editing?.revision;const report=[...(batch?(current.module_step_reports||[]):(current?.manual_history||[]))].reverse().find(r=>r.workflow_module===editing?.id&&r.workflow_revision===editing?.revision&&r.step_id===s.step_id);for(const value of [s.step_id,({TapTest:'點擊',LongPressTest:'點擊',SwipeCalibration:'滑動',CocoTargetTest:'點擊',WaitTest:'等待'}[s.name]||s.name),(s.target_conditions?s.target_conditions.clauses.map((r,i)=>(i?' '+(r.join||'AND')+' ':'')+(r.type==='ocr'?'OCR '+r.text:r.type==='color'?'顏色 RGB '+r.color_rgb.join(','):(r.features||[]).join(' | ')+(r.type==='coco_color'?' 且顏色':r.type==='coco_or_color'?' 或顏色':''))).join(''):s.ocr_target?'OCR '+s.ocr_target.text:WorkflowModel.features(s).join(' | ')+(s.color_mode==='either'?' 或顏色':s.color_mode==='coco'?' 且顏色':''))||(s.color_mode?'顏色 RGB '+s.color_rgb.join(','):[s.x,s.y].join(',')),parameterSummary(s)]){let td=document.createElement('td');td.textContent=value;row.append(td);}let preview=document.createElement('td');const match=current?.frame?.recognition_valid&&displayedFrameId===current.frame.frame_id?current.matches.find(m=>WorkflowModel.features(s).includes(m.feature)&&m.matched):null;if(match?.bbox&&urls.frame){let svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),image=document.createElementNS('http://www.w3.org/2000/svg','image');svg.setAttribute('viewBox',match.bbox.join(' '));svg.classList.add('stepPreview');image.setAttribute('href',urls.frame);image.setAttribute('width',1080);image.setAttribute('height',2400);svg.append(image);preview.append(svg);}else preview.textContent='—';row.append(preview);let status=document.createElement('td');status.textContent=report?.manual_verification?.pass===false?'人工驗證失敗':report?.status||'UNTESTED';renderConditionReport(status,report);if(report){const reviews=document.createElement('div');for(const[label,pass]of [['Pass',true],['Fail',false]]){const wrap=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.checked=report.manual_verification?.pass===pass;check.disabled=!!current?.module_run?.active||current?.device_pending||deviceDispatchPending||judgmentRequests.has(report.report_id)||!canJudge(report);check.onchange=async()=>{if(!check.checked)return;judgmentRequests.set(report.report_id,null);renderSteps();const response=await send('manual_verify',{report_id:report.report_id,pass,note:$('passNote').value});if(response)judgmentRequests.set(report.report_id,response.request_id);else judgmentRequests.delete(report.report_id);await poll();};wrap.append(check,document.createTextNode(label));reviews.append(wrap);}status.append(reviews);if(report.failure_report_path){const link=document.createElement('small');link.textContent='重測報告：'+report.failure_report_path;status.append(link);}}row.append(status);let edit=document.createElement('td');for(const[label,fn]of [['編輯',()=>openEditor(index,'edit')],['↑',()=>move(index,-1)],['↓',()=>move(index,1)],['移除',()=>{steps.splice(index,1);dirty=true;renderSteps();}]]){let b=document.createElement('button');b.textContent=label;b.onclick=fn;edit.append(b);}row.append(edit);let execute=document.createElement('td'),b=document.createElement('button');b.textContent='執行一次';b.disabled=!!reason||!canExecuteDevice();b.title=reason;b.onclick=()=>executeStep(s,editing.id);execute.append(b);row.append(execute);$('steps').append(row);}}
function move(i,delta){const j=i+delta;if(j<0||j>=steps.length)return;[steps[i],steps[j]]=[steps[j],steps[i]];dirty=true;renderSteps();}
function loadModule(i){editing=structuredClone(i);steps=structuredClone(i.steps);moduleTags=structuredClone(i.tags||{});moduleGlobalRoi=i.global_roi?.slice()||null;$('moduleId').value=i.id;$('moduleName').value=i.name;dirty=false;moduleRenameFields(true);renderSteps();}
function definition(){const coordinateMetadata=structuredClone(editing?.coordinate_metadata||{stored_resolution:[1080,2400]});if(current?.display_size)coordinateMetadata.last_edit_resolution=current.display_size.slice();return {coordinate_metadata:coordinateMetadata,schema_version:1,id:$('moduleId').value.trim(),name:$('moduleName').value.trim(),version:editing?.version??1,steps,...(Object.keys(moduleTags).length?{tags:moduleTags}:{}),...(moduleGlobalRoi?{global_roi:moduleGlobalRoi}:{}),...(editing?.role?{role:editing.role}:{}),...(editing?.simulation_seconds!==undefined?{simulation_seconds:editing.simulation_seconds}:{})};}
$('addStep').onclick=()=>{let p;try{p=params();WorkflowModel.validateRepeats(p);WorkflowModel.validateSwipe(p);WorkflowModel.validateConditions(p);}catch(e){$('editorNotice').textContent=$('notice').textContent=e.message;$('editorNotice').scrollIntoView({block:'nearest'});return;}if(!p.step_id){$('editorNotice').textContent='Step ID 不可空白';return;}if(editorContext?.id!==editing?.id||editorContext?.revision!==editing?.revision||(editing&&current?.module_catalog.find(i=>i.id===editing.id)?.revision!==editorContext?.revision)){$('editorNotice').textContent='版本已改變，請重新載入';return;}if(editorIndex!==null){if(p.step_id!==editorOriginal.step_id)return;steps[editorIndex]=editorSignature()===editorInitial?editorOriginal:p;}else{if(steps.some(s=>s.step_id===p.step_id)){$('editorNotice').textContent='Step ID 已存在';return;}steps.push(p);}moduleTags=stepSettings.tags();moduleGlobalRoi=stepSettings.global();dirty=true;renderSteps();$('stepEditor').close();};
$('execute').onclick=()=>{updateEditorExecution();if(!$('execute').disabled)executeStep(steps[editorIndex],editing.id);};
$('workflow').onchange=()=>{if(dirty&&!confirm('放棄未儲存草稿？')){$('workflow').value=editing?.id||'';return;}const i=current.module_catalog.find(i=>i.id===$('workflow').value);if(i)loadModule(i);};
$('createModule').onclick=()=>{if(dirty&&!confirm('放棄未儲存草稿？'))return;editing=null;steps=[];moduleTags={};moduleGlobalRoi=null;dirty=true;$('moduleId').value='';$('moduleName').value='';$('importJson').value='';$('importFile').value='';$('moduleNotice').textContent='填入 ID 與名稱，再按新增空白模組。';$('workflow').value='';renderSteps();$('moduleEditor').showModal();};
function moduleRenameFields(reset=false){
 const field=$('moduleRenameField').value;
 if(reset)$('moduleRenameValue').value=editing?.[field]||'';
 $('moduleRenameValue').maxLength=field==='id'?80:120;
 const locked=!!(pendingRename||pendingSave||pendingImport||current?.module_run?.active||schedulerInputBlocked());
 $('renameModule').disabled=!editing||dirty||locked||field==='id'&&editing.kind==='system';
 $('moduleRenameField').disabled=$('moduleRenameValue').disabled=locked||!editing;
 $('workflow').disabled=!!pendingRename;
 $('moduleRenameHint').textContent=field==='id'?(editing?.kind==='system'?'系統 ID 固定；可改外顯名稱，或先複製':'改 ID 前需解除 Canvas、排程及校正綁定；不會自動改線'):'保留 ID、步驟、TAG 與 Canvas 綁定';
 window.moduleVersions?.update();
 window.moduleTrash?.update();
}
$('moduleRenameField').onchange=()=>moduleRenameFields(true);
$('renameModule').onclick=async()=>{
 if(!editing||pendingRename||pendingSave||pendingImport)return;
 if(dirty){$('notice').textContent='請先儲存模組草稿，再重新命名。';return;}
 const field=$('moduleRenameField').value,value=$('moduleRenameValue').value.trim();
 if(!value||value.length>(field==='id'?80:120)||field==='id'&&!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(value)){$('notice').textContent='請填入有效的新名稱／ID。';return;}
 if(field==='id'&&decisionEditor.inspect().definition.nodes.some(n=>n.module===editing.id)){$('notice').textContent='目前 Canvas（包含未儲存草稿）仍引用此 ID；可先改外顯名稱。';return;}
 const request={id:editing.id,expected_revision:editing.revision,field,value,system_update:editing.kind==='system'};
 pendingRename={request_id:null,id:editing.id,new_id:field==='id'?value:editing.id};moduleRenameFields();
 const receipt=await send('module_rename',request);
 if(receipt){pendingRename.request_id=receipt.request_id;$('notice').textContent='重新命名已排隊，等待結果…';await poll();}
 else{pendingRename=null;moduleRenameFields();}
};
$('moduleInfo').onclick=()=>$('moduleEditor').showModal();
$('importOpen').onclick=()=>{if(!pendingImport){importIdentity={id:editing?.id,revision:editing?.revision,fieldId:$('moduleId').value,fieldName:$('moduleName').value};$('moduleId').value='';$('moduleName').value='';$('importJson').value='';$('importFile').value='';$('moduleNotice').textContent='選擇 JSON 後，可修改 Module ID／名稱再匯入。';}$('moduleEditor').showModal();};


window.addEventListener('rwtd-load-module',event=>{const module=current?.module_catalog.find(m=>m.id===event.detail?.id);if(!module)return;if(event.detail.revision&&module.revision!==event.detail.revision){$('notice').textContent='待辦引用的模組版本已改變；請移除待辦並重新啟動監看。';return;}if(dirty&&!confirm('放棄未儲存草稿並載入待辦模組？'))return;loadModule(module);$('workflow').value=module.id;$('notice').textContent='已載入 '+module.name+'；請自行按執行模組，未送出手機操作。';});
function saveUserCopy(){
 const new_id=prompt('另存為使用者模組 ID（保留目前所有草稿步驟）',(editing?.id||'Module')+'_user');if(!new_id)return;
 if((current?.module_catalog||[]).some(m=>m.id.toLowerCase()===new_id.toLowerCase())){$('notice').textContent='ID 已存在，請使用新 ID；不覆寫既有模組。';return;}
 const new_name=prompt('使用者模組名稱',(editing?.name||'新模組')+' copy');if(!new_name)return;
 const data=definition();Object.assign(data,{id:new_id,name:new_name,version:1});
 return send('module_save',{definition:data});
}
$('configure').onclick=async()=>{
 if(pendingSave)return;
 const data=definition();
 if(!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(data.id)||!data.name||data.name.length>120){$('notice').textContent='請先在模組資訊填入有效的 Module ID 與名稱。';$('moduleEditor').showModal();return;}
 $('configure').disabled=true;const signature=JSON.stringify(data);
 const response=await send('module_save',{definition:data,expected_revision:editing?.revision,system_update:editing?.kind==='system'});
 if(response){pendingSave={request_id:response.request_id,id:data.id,signature};$('notice').textContent='儲存已排隊，等待完成…';await poll();}
 else $('configure').disabled=false;
};
$('duplicateModule').onclick=()=>{if(editing)saveUserCopy();};
$('deleteModule').onclick=()=>{if(editing?.kind!=='user'){$('notice').textContent='系統模組受保護';return;}if(confirm('刪除 '+editing.name+'？將保留備份。'))send('module_delete',{id:editing.id,expected_revision:editing.revision,confirmed:true});};
$('importFile').onchange=async()=>{const f=$('importFile').files[0];if(!f)return;try{if(f.size>28000)throw Error('JSON 上限 28 KB');const text=await f.text();$('importJson').value=text;const data=JSON.parse(text);$('moduleId').value=data.id||'';$('moduleName').value=data.name||'';$('moduleNotice').textContent='已讀取 JSON；可修改 ID／名稱再匯入。';}catch(e){$('moduleNotice').textContent=e.message;}};
let pendingBlankModuleId=null,pendingImport=null,importIdentity=null,pendingSave=null;
$('blankModule').onclick=async()=>{
 const id=$('moduleId').value.trim(),name=$('moduleName').value.trim();
 if(!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(id)){$('moduleNotice').textContent='ID 須以英文字母開頭，限英數字、底線或連字號，最多 80 字元。';return;}
 if(!name||name.length>120){$('moduleNotice').textContent='請填入名稱（最多 120 字元）。';return;}
 if((current?.module_catalog||[]).some(m=>m.id.toLowerCase()===id.toLowerCase())){$('moduleNotice').textContent='ID 已存在，請使用新 ID；不覆寫既有模組。';return;}
 if(dirty&&steps.length&&!confirm('建立空白模組會切換目前未儲存的步驟草稿，是否繼續？'))return;
 $('blankModule').disabled=true;pendingBlankModuleId=id;
 try{const receipt=await send('module_save',{definition:{schema_version:1,id,name,version:1,steps:[]}});if(receipt){$('moduleNotice').textContent='已送出建立要求，等待儲存結果。';await poll();}else{pendingBlankModuleId=null;$('moduleNotice').textContent=$('notice').textContent;}}finally{$('blankModule').disabled=false;}
};
$('moduleEditor').addEventListener('close',()=>{if(importIdentity&&editing?.id===importIdentity.id&&editing?.revision===importIdentity.revision){$('moduleId').value=importIdentity.fieldId;$('moduleName').value=importIdentity.fieldName;}importIdentity=null;});
$('importModule').onclick=async()=>{
 try{
  const text=$('importJson').value.trim();if(!text)throw Error('請選擇或貼上 JSON；建立空白模組請按新增空白模組。');
  const data=JSON.parse(text);if(!data||Array.isArray(data)||typeof data!=='object')throw Error('JSON 必須是模組物件。');
  data.id=$('moduleId').value.trim()||data.id;data.name=$('moduleName').value.trim()||data.name;
  const existing=current?.module_catalog.find(i=>i.id.toLowerCase()===String(data.id).toLowerCase());
  if(dirty&&!confirm('匯入成功後會切換模組並放棄目前未儲存草稿，是否繼續？'))return;
  $('importModule').disabled=true;$('moduleNotice').textContent=existing?'備份並覆寫 '+existing.id+'…':'驗證並匯入中…';
  const response=await send('module_import',{definition:data,expected_revision:existing?.revision});
  if(response){pendingImport={request_id:response.request_id,id:data.id};await poll();}
  else{$('moduleNotice').textContent=$('notice').textContent;$('importModule').disabled=false;}
 }catch(e){$('moduleNotice').textContent=$('notice').textContent=e.message;$('importModule').disabled=false;}
};
$('exportModule').onclick=()=>{if(!editing)return;const data=definition(),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=editing.id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('previewMode').onclick=()=>send('mode',{value:(current?.preview_enabled??(current?.mode==='Manual Device Test'))?'Simulation':'Android Preview',explicit_activation:true});
$('manualMode').onclick=()=>send('mode',{value:current?.mode==='Manual Device Test'&&current?.manual_test_enabled!==false?'Android Preview':'Manual Device Test',explicit_activation:true});
$('refreshFrame').onclick=()=>send('refresh_frame',{full_recognition:false});$('fullRecognitionOnce').onclick=()=>send('refresh_frame',{full_recognition:true});const settings=()=>send('preview_settings',{enabled:$('autoRefresh').checked,interval_seconds:Number($('refreshInterval').value),full_recognition:$('fullRecognition').checked});$('fullRecognition').onchange=settings;$('autoRefresh').onchange=settings;$('refreshInterval').onchange=settings;
$('pauseModule').onclick=()=>send('pause_module');$('stop').onclick=()=>send('stop');
$('executeModule').onclick=()=>{if(dirty||!editing){$('notice').textContent='先儲存模組';return;}dispatchDevice('execute_module',{id:editing.id,resume:current?.module_run?.active===true,explicit_execution:true,reversible_ack:true,dashboard_manual:true,require_manual_verification:false,post_run_verification:true});};
function canJudge(r){return r?.action_completed&&((r.before&&r.after)||(r.screenshot_policy==='COCO_ONLY'&&r.screenshots_required===false))&&!r.failure_reason&&!r.manual_verification&&!r.screenshots_cleaned&&(!r.input_attempted||r.input_command_returned)&&r.status==='INPUT_SENT_UNVERIFIED';}
function judge(pass){if(canJudge(current?.manual_result))send('manual_verify',{report_id:current.manual_result.report_id,pass,note:$('passNote').value});}
$('pass').onclick=()=>judge(true);$('fail').onclick=()=>judge(false);$('preset').onclick=()=>{let r=current?.manual_result;if(r?.status==='MANUAL_VERIFIED')send('save_preset',{report_id:r.report_id,name:(r.workflow_module||r.module)+' step '+(r.step_id||r.parameters?.step_id||'test')});};
$('closeDashboard').onclick=()=>{if(confirm('關閉 Dashboard 並結束任務？'))send('dashboard_close');};$('openStorage').onclick=()=>$('storageEditor').showModal();
$('saveStorage').onclick=()=>send('storage_settings',{keep_successful:Number($('keepSuccess').value),automatic_success_retention:$('autoRetention').checked});$('scanStorage').onclick=()=>send('storage_scan');$('previewCleanup').onclick=()=>send('storage_preview');$('openHistory').onclick=()=>send('storage_open');$('cleanSuccess').onclick=()=>{if(!cleanupPlan){$('notice').textContent='先預覽清理';return;}if(confirm('清理預覽所列 '+cleanupPlan.candidates.length+' 個成功測試？保留摘要與模組。'))send('storage_clean',{plan_id:cleanupPlan.plan_id,approved:true});};
function localTime(value){return value?new Date(value).toLocaleTimeString('zh-TW',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';}
function hideImage(id,note){let img=$(id);img.hidden=true;img.removeAttribute('src');const status=img.parentElement.querySelector('.evidenceStatus,.emptyFrame');if(status){status.hidden=false;status.textContent=note;}}
async function loadImage(which,ids,{frameId=null,reportId=null,cleaned=false}={}){if(cleaned||(!frameId&&!reportId)){ids.forEach(id=>hideImage(id,cleaned?'圖片已清理，保留摘要':'尚無操作證據'));return false;}let response=await fetch('/api/image?which='+which+(frameId?'&frame_id='+encodeURIComponent(frameId):'')+(reportId?'&report_id='+encodeURIComponent(reportId):''),{headers});if(!response.ok){ids.forEach(id=>hideImage(id,which==='frame'?'畫面暫不可用':'尚無操作證據'));return false;}const blob=await response.blob();let url=URL.createObjectURL(blob);try{await Promise.all(ids.map(async id=>{const img=$(id);img.src=url;await img.decode();img.dataset.frameId=frameId||'';img.hidden=false;const status=img.parentElement.querySelector('.evidenceStatus,.emptyFrame');if(status)status.hidden=true;}));}catch{URL.revokeObjectURL(url);ids.forEach(id=>hideImage(id,'圖片不可用'));return false;}if(urls[which])URL.revokeObjectURL(urls[which]);urls[which]=url;if(which==='frame'){displayedFrameId=frameId;displayedFrameBlob=blob;}return true;}
function updateCatalog(){
 if(editorOpen())return;
 const resultKey=JSON.stringify(current.editor_result);
 if(resultKey!==editorResultKey){editorResultKey=resultKey;const saved=current.module_catalog.find(i=>i.id===current.editor_result?.definition?.id);if(saved){if(!dirty||pendingBlankModuleId===saved.id)loadModule(saved);if(pendingBlankModuleId===saved.id&&current.editor_result.command==='module_save'){pendingBlankModuleId=null;$('moduleEditor').close();$('notice').textContent='已建立空白模組 '+saved.name+'；可按新增操作加入步驟。';}}}
 const visible=current.module_catalog;
 const key=JSON.stringify(visible.map(i=>[i.id,i.revision]));
 if(key===catalogKey)return;catalogKey=key;$('workflow').replaceChildren();
 for(const i of visible){const option=document.createElement('option');option.value=i.id;option.textContent=i.name+' ['+i.kind+']';$('workflow').append(option);}
 if(!dirty){const selected=visible.find(i=>i.id===editing?.id)||visible[0];if(selected)loadModule(selected);else{editing=null;steps=[];renderSteps();}}
 if(editing)$('workflow').value=editing.id;
}
function renderHistory(){$('history').replaceChildren();for(const r of [...current.manual_history].reverse()){let row=document.createElement('tr');for(const value of [localTime(r.finished_utc||r.timestamp_utc),(r.workflow_module||r.module)+' / '+(r.step_id||r.parameters?.step_id||'—'),(r.simulated?'SIMULATED / ':'Manual / ')+r.status,parameterSummary(r.parameters),r.screenshots_cleaned?'圖片已清理，保留摘要':r.failure_reason?DashboardDiagnostics.redact(r.failure_reason):r.screenshots_required===false?'未使用 COCO；不保存截圖':'Before / After']){let td=document.createElement('td');td.textContent=value;row.append(td);}row.onclick=()=>{historySelection=r.report_id;$('evidence').textContent='載入證據…';hideImage('historyBefore','載入中…');hideImage('historyAfter','載入中…');send('view_report',{report_id:r.report_id});$('historyEditor').showModal();};$('history').append(row);}}
function renderBoxes(){$('boxes').replaceChildren();if(!current.frame?.recognition_valid)return;for(const hit of previewHits()){let[x,y,w,h]=hit.bbox,rect=document.createElementNS('http://www.w3.org/2000/svg','rect'),text=document.createElementNS('http://www.w3.org/2000/svg','text');for(const[k,v]of Object.entries({x,y,width:w,height:h,fill:'none',stroke:'#26fa7c','stroke-width':5}))rect.setAttribute(k,v);for(const[k,v]of Object.entries({x,y:Math.max(35,y-8),fill:'#26fa7c','font-size':32}))text.setAttribute(k,v);text.textContent=hit.feature+' '+(hit.evidence_kind==='annotation_fixture'?'標註':Number(hit.confidence||0).toFixed(2));$('boxes').append(rect,text);}renderExclusionRegions();}
async function poll(){
 if(busyPoll)return;busyPoll=true;
 try{
  $('boxes').style.visibility='hidden';const response=await fetch('/api/state',{headers});if(!response.ok)throw Error('任務已停止或驗證失敗');current=await response.json();connectionHealthy=true;
  const receipt=deviceRequestId&&current.requests?.[deviceRequestId];if(receipt&&!['QUEUED','RUNNING'].includes(receipt)){deviceDispatchPending=false;deviceRequestId=null;}
  if(!preferencesLoaded){preferencesLoaded=true;if(current.ui_preferences){$('previewScale').value=current.ui_preferences.preview_scale??100;$('previewThreshold').value=current.ui_preferences.preview_threshold??.8;applyPreviewSize();}updatePreviewThreshold();}
  const manual=current.mode==='Manual Device Test',frame=current.frame||{},fixture=current.data_source==='OFFLINE FIXTURE',real=frame.source==='Real Capture'||fixture,preview=current.preview_enabled??manual,source=fixture?'OFFLINE FIXTURE':real||preview?'REAL ANDROID':'SIMULATION';
  $('modeBanner').textContent=fixture?'OFFLINE FIXTURE':manual?'Manual Device Test':preview?'ANDROID PREVIEW · 唯讀':'尚未連接裝置';$('manualMode').textContent=manual&&current.manual_test_enabled!==false?'停用手動操作':'啟用手動操作';$('manualMode').disabled=fixture||!current.manual_permission||current.device_pending||deviceDispatchPending||schedulerInputBlocked();$('manualMode').title=current.manual_permission?'手動操作仍由每次按下執行觸發':'需於 OK-Script 此任務設定允許 Manual Device Test';$('previewMode').textContent=preview?'停止預覽':'連接畫面';$('previewMode').disabled=fixture||current.device_pending||deviceDispatchPending||schedulerInputBlocked();
  $('device').textContent=fixture?'固定參考圖 · 無 Android 連線':(current.device.model||'未選裝置')+' · '+(frame.stale?'ADB 擷取未確認':current.device.connected?'ADB Connected':'ADB 未確認')+(current.display_size?' · 實機 '+current.display_size.join('×')+' → 辨識座標 1080×2400':'');
  $('source').textContent=source+(frame.stale?' · CAPTURE FAILED / STALE':'')+' · '+localTime(frame.captured_utc);$('source').removeAttribute('title');$('recognitionStatus').removeAttribute('title');
  $('recognitionStatus').textContent=frame.recognition_valid?(fixture?'COCO 縮放測試框：':'COCO Detected: ')+previewHits().length:frame.stale?'CAPTURE FAILED / STALE':frame.recognition_pending?'COCO 辨識中 · '+(frame.recognition_scope==='full'?'完整清單':'Canvas 引用'):real?'RECOGNITION FAILED'+(frame.recognition_error?' · '+frame.recognition_error:''):'COCO Detected: 0';
  $('fullRecognition').checked=current.full_recognition===true;$('fullRecognitionOnce').disabled=!preview||current.device_pending||current.capture_pending||frame.recognition_pending||deviceDispatchPending;$('fullRecognition').disabled=!preview;$('autoRefresh').checked=current.auto_refresh===true;$('refreshFrame').disabled=!preview||current.device_pending||current.capture_pending||frame.recognition_pending||deviceDispatchPending;$('executeModule').disabled=!canExecuteDevice()||!editing||dirty;$('autoRefresh').disabled=$('refreshInterval').disabled=!preview;
  if(document.activeElement!==$('refreshInterval'))$('refreshInterval').value=current.refresh_interval_seconds??1.5;
  stepSettings.catalog(current.feature_catalog||[]);try{decisionEditor.update(current);}catch(error){diagnosticError='Canvas 介面更新失敗：'+error.message;$('canvasFeedback').textContent=diagnosticError+'；手機連線與模組編輯仍可使用。';decisionEditor.loadFailed?.(error);}updateCatalog();renderHistory();
  if(pendingRename?.request_id){const receipt=current.requests?.[pendingRename.request_id];if(receipt&&!['QUEUED','RUNNING'].includes(receipt)){
   const renamed=current.module_catalog.find(m=>m.id===pendingRename.new_id);
   if(['READY','SUCCESS'].includes(receipt)&&renamed){
    if(!dirty){loadModule(renamed);$('workflow').value=renamed.id;}
    else if(editing?.id===pendingRename.id){const old=editing;editing={...editing,...renamed,steps:editing.steps};if($('moduleId').value===old.id)$('moduleId').value=renamed.id;if($('moduleName').value===old.name)$('moduleName').value=renamed.name;moduleRenameFields(true);}
    $('notice').textContent='已重新命名：'+renamed.name+' · '+renamed.id;
   }else{const error=[...current.logs].reverse().find(i=>i.request_id===pendingRename.request_id);$('notice').textContent='重新命名失敗：'+safeMessage(error?.message||'請查看開發診斷。');}
   pendingRename=null;moduleRenameFields();
  }}
  moduleRenameFields();
  if(pendingSave){const receipt=current.requests?.[pendingSave.request_id];if(receipt&&!['QUEUED','RUNNING'].includes(receipt)){const saved=current.module_catalog.find(i=>i.id===pendingSave.id);if(['READY','SUCCESS'].includes(receipt)&&saved){if(JSON.stringify(definition())===pendingSave.signature){loadModule(saved);$('workflow').value=saved.id;$('notice').textContent='已儲存 '+saved.name;}else{if(editing?.id===saved.id)editing={...editing,revision:saved.revision,version:saved.version};$('notice').textContent='已儲存送出時的版本；之後新增的修改仍保留為草稿。';}}else{const error=[...current.logs].reverse().find(i=>i.request_id===pendingSave.request_id);$('notice').textContent='儲存失敗：'+safeMessage(error?.message||'請查看開發診斷。');}pendingSave=null;$('configure').disabled=false;}}
  if(pendingImport){const receipt=current.requests?.[pendingImport.request_id];if(receipt&&!['QUEUED','RUNNING'].includes(receipt)){const imported=current.module_catalog.find(i=>i.id===pendingImport.id);if(['READY','SUCCESS'].includes(receipt)&&imported){loadModule(imported);$('workflow').value=imported.id;$('moduleNotice').textContent=$('notice').textContent='已匯入 '+imported.name;$('moduleEditor').close();}else{const error=[...current.logs].reverse().find(i=>i.request_id===pendingImport.request_id);$('moduleNotice').textContent=safeMessage(error?.message||'匯入失敗，請查看開發診斷。');}pendingImport=null;$('importModule').disabled=false;}}
  $('logs').textContent=current.logs.map(i=>`${localTime(i.timestamp_utc)} [${i.event}] ${i.event==='COCO_CHECK'?String(DashboardDiagnostics.redact(i.message)).split('\n')[0]:safeMessage(i.message)}`).join('\n');
  for(const[id,request]of judgmentRequests){const receipt=current.requests?.[request];if(receipt&&!['QUEUED','RUNNING'].includes(receipt))judgmentRequests.delete(id);}
  $('moduleResult').textContent=current.module_run?current.module_run.status+' · 已執行 '+current.module_run.next_step+' 步':'';
  if(current.module_failure_report)$('moduleResult').textContent+=' · Fail 重測報告：'+(current.module_failure_report.path||'離線預覽');
  const r=current.manual_result,repeatResult=r?.parameters&&WorkflowModel.action(r.parameters)==='tap'?' · '+(r.simulated?'模擬 '+r.simulated_repeat_count_completed:'已確認送出 '+(r.repeat_count_completed??(r.action_completed?1:0)))+'/'+(r.repeat_count_requested??WorkflowModel.repeats(r.parameters))+' 次':'';const retryResult=r?.single_step_test&&r.recognition_attempt_count?' · 辨識嘗試 '+r.recognition_attempt_count+'/'+(r.test_loop_settings?.max_cycles??'不限')+' 次':'';$('result').textContent=r?(r.manual_verification?.pass===false?'人工驗證失敗':r.status)+repeatResult+retryResult+' · '+DashboardDiagnostics.redact(r.failure_reason||r.skip_reason||''):'';
  $('judgment').hidden=!!current.module_run?.active||!(canJudge(r)||r?.status==='MANUAL_VERIFIED');$('pass').hidden=$('fail').hidden=!canJudge(r);$('pass').disabled=$('fail').disabled=!canJudge(r)||current.device_pending;$('preset').disabled=r?.status!=='MANUAL_VERIFIED'||current.device_pending;
  $('state').textContent='Scheduler: '+(current.scheduler_runtime?.status||'IDLE');const service=current.storage_result;if(service){$('storageUsage').textContent=storageSummary(service);cleanupPlan=service.cleanup_preview||(service.dry_run?service:null);}
  updateDiagnostics();
  if(real){const loaded=displayedFrameId===frame.frame_id&&!$('matchFrame').hidden?true:await loadImage('frame',['matchFrame'],{frameId:frame.frame_id});if(loaded){renderBoxes();$('boxes').style.visibility='visible';}else{$('source').textContent=source+' · CAPTURE FAILED / STALE';$('recognitionStatus').textContent='STALE · 圖片不可用';$('boxes').replaceChildren();displayedFrameId=null;}}
  else{displayedFrameId=null;for(const id of ['matchFrame'])hideImage(id,frame.stale?'STALE · 擷取失敗':preview?'正在取得 Android 畫面':'尚未取得 Android 畫面');$('boxes').replaceChildren();}
  syncEditorFrame();renderSteps();renderEditorMatches();if(editorOpen())updateGesture();updateEditorExecution();
  $('before').closest('details').hidden=r?.screenshots_required===false;if(r?.screenshots_required===false){hideImage('before','此步驟未保存截圖');hideImage('after','此步驟未保存截圖');}else{await loadImage('before',['before'],{reportId:r?.report_id,cleaned:r?.screenshots_cleaned});await loadImage('after',['after'],{reportId:r?.report_id,cleaned:r?.screenshots_cleaned});}
  if($('historyEditor').open){const selected=current.manual_history.find(i=>i.report_id===historySelection);$('evidence').textContent=selected?selected.status+' · '+parameterSummary(selected.parameters)+' · '+(selected.failure_reason?DashboardDiagnostics.redact(selected.failure_reason):selected.screenshots_required===false?'未使用 COCO；保留操作紀錄，未保存截圖':'人工驗證紀錄'):'尚無紀錄';$('historyBefore').closest('.comparison').hidden=selected?.screenshots_required===false;await loadImage('before',['historyBefore'],{reportId:historySelection,cleaned:selected?.screenshots_cleaned});await loadImage('after',['historyAfter'],{reportId:historySelection,cleaned:selected?.screenshots_cleaned});}
 }catch(e){connectionHealthy=false;diagnosticError=e.message;$('notice').textContent='連線／資料取得失敗；開發診斷有詳細資訊。';$('source').textContent='CAPTURE FAILED / STALE';$('recognitionStatus').textContent='RECOGNITION FAILED';$('boxes').replaceChildren();displayedFrameId=null;['matchFrame'].forEach(id=>hideImage(id,'STALE · 更新失敗'));syncEditorFrame();renderEditorMatches();for(const id of ['previewMode','manualMode','refreshFrame','autoRefresh','executeModule'])$(id).disabled=true;renderSteps();updateEditorExecution();updateDiagnostics();}
 finally{busyPoll=false;}
}
let diagnosticError=null;
function usableFrame(){return current?.frame?.source==='Real Capture'||current?.data_source==='OFFLINE FIXTURE';}
function safeMessage(value){const text=String(DashboardDiagnostics.redact(value||''));return /Traceback|^\s*[\[{]/.test(text)?'詳細資訊見開發診斷':text.split('\n')[0].slice(0,160);}
function parameterSummary(p={}){const kind=WorkflowModel.action(p);return (kind==='wait'?'等待':kind==='swipe'?(p.mid_x!==undefined?'三點滑動':'滑動'):'點擊 × '+WorkflowModel.repeats(p))+' · '+WorkflowModel.hold(p)+' ms · 等待 '+(p.post_wait_seconds??p.delay_seconds??0)+' s';}
function storageSummary(service){return '掃描／預覽資料已準備 · 候選 '+(service.candidates?.length??service.cleanup_preview?.candidates?.length??0)+' 個成功 session。完整數據見開發診斷。';}
function diagnosticReport(){return {source:current?.data_source||current?.mode,error:diagnosticError,state:current,canvas:decisionEditor.inspect(),viewport:{width:innerWidth,height:innerHeight,devicePixelRatio}};}
function updateDiagnostics(){if($('diagnosticsEditor').open)$('diagnosticDetails').textContent=DashboardDiagnostics.format(diagnosticReport());}
$('openDiagnostics').onclick=()=>{$('diagnosticsEditor').showModal();updateDiagnostics();};
$('copyDiagnostics').onclick=async()=>{const text=DashboardDiagnostics.format(diagnosticReport());try{await navigator.clipboard.writeText(text);}catch{$('diagnosticDetails').textContent=text;const range=document.createRange();range.selectNodeContents($('diagnosticDetails'));const selection=getSelection();selection.removeAllRanges();selection.addRange(range);$('notice').textContent='瀏覽器限制剪貼簿；已選取遮蔽後資訊，按 Ctrl+C。';}};
$('exportDiagnostics').onclick=()=>DashboardDiagnostics.export(diagnosticReport());
window.moduleVersions=window.installModuleVersions({read:()=>({current,editing,dirty,pending:!!(pendingSave||pendingImport||pendingRename)}),send,reload:poll,load:loadModule,error:()=>$('notice').textContent});
window.moduleTrash=window.installModuleTrash({read:()=>({current,editing,dirty,pending:!!(pendingSave||pendingImport||pendingRename)}),send,reload:poll,load:loadModule,error:()=>$('notice').textContent});
poll();setInterval(poll,1000);















