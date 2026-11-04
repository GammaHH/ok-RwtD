'use strict';
// Keep both editors mounted. Switching never navigates or discards a draft.
window.RwtdWorkspaceTabs={
 validRoiUrl(value){try{const u=new URL(value);return /^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(u.origin)&&u.pathname==='/'&&!!new URLSearchParams(u.hash.slice(1)).get('token');}catch{return false;}},
 async install(){
  const auth=sessionStorage.getItem('dashboard-token:'+location.origin);
  const response=await fetch('/api/workspace',{headers:{Authorization:'Bearer '+auth}});
  if(!response.ok)throw Error('工作區設定載入失敗');
  const config=await response.json();
  if(!this.validRoiUrl(config.roi_url))throw Error('ROI 總覽入口不可用；請重新啟動 Dashboard 任務。');
  const roiOrigin=new URL(config.roi_url).origin;
  const dashboard=document.createElement('div');dashboard.id='dashboardWorkspaceView';dashboard.className='workspaceView';dashboard.setAttribute('role','tabpanel');
  for(const element of [...document.body.children])if(element.tagName!=='SCRIPT')dashboard.append(element);
  const nav=document.createElement('nav');nav.className='workspaceTabs';nav.setAttribute('aria-label','工作區頁籤');
  const buttons=document.createElement('div');buttons.setAttribute('role','tablist');
  const dashButton=document.createElement('button'),roiButton=document.createElement('button'),farmButton=document.createElement('button');
  dashButton.textContent='Dashboard';roiButton.textContent='ROI 總覽';farmButton.textContent='FARM';farmButton.id='farmWorkspaceTab';
  dashButton.id='dashboardWorkspaceTab';roiButton.id='roiWorkspaceTab';dashButton.setAttribute('aria-controls','dashboardWorkspaceView');roiButton.setAttribute('aria-controls','roiWorkspaceView');dashboard.setAttribute('aria-labelledby',dashButton.id);
  for(const button of [dashButton,roiButton,farmButton]){button.type='button';button.setAttribute('role','tab');buttons.append(button);}
  const status=document.createElement('span');status.setAttribute('role','status');status.tabIndex=-1;
  nav.append(buttons,status);
  const roi=document.createElement('section');roi.id='roiWorkspaceView';roi.className='workspaceView';roi.hidden=true;roi.setAttribute('role','tabpanel');
  roi.setAttribute('aria-labelledby',roiButton.id);
  const frame=document.createElement('iframe');frame.title='ROI 總覽編輯器';frame.referrerPolicy='no-referrer';roi.append(frame);
  const headers={Authorization:'Bearer '+auth,'Content-Type':'application/json'};async function readWorkspace(){const r=await fetch('/api/workspace/state',{headers});if(!r.ok)throw Error('工作區狀態不可用');return r.json();}async function workspaceCommand(command,payload){const id=crypto.randomUUID(),r=await fetch('/api/workspace/command',{method:'POST',headers,body:JSON.stringify({request_id:id,command,payload})});if(!r.ok)throw Error((await r.json()).error||'工作區指令失敗');for(let i=0;i<120;i++){const state=await readWorkspace(),result=state.results?.[id];if(result?.status==='SUCCESS')return state;if(result?.status==='FAILED')throw Error(result.reason);await new Promise(resolve=>setTimeout(resolve,250));}throw Error('工作區指令尚未完成，請等待後重新整理狀態');}const farmPanel=window.createFarmWorkspacePanel({read:readWorkspace,command:workspaceCommand}),farm=document.createElement('section');farm.id='farmWorkspaceView';farm.className='workspaceView';farm.hidden=true;farm.setAttribute('role','tabpanel');farm.setAttribute('aria-labelledby',farmButton.id);farmButton.setAttribute('aria-controls',farm.id);farm.append(farmPanel.element);document.body.prepend(nav,dashboard,roi,farm);
  let current='dashboard',switching=false,loaded=false,pending=null;
  function report(text){status.textContent=text;if(text)status.focus();}
  function paint(){dashButton.setAttribute('aria-selected',String(current==='dashboard'));roiButton.setAttribute('aria-selected',String(current==='roi'));dashboard.hidden=current!=='dashboard';roi.hidden=current!=='roi';farm.hidden=current!=='farm';farmButton.setAttribute('aria-selected',String(current==='farm'));}
  function canLeaveRoi(){return new Promise(resolve=>{const id=crypto.randomUUID(),timer=setTimeout(()=>{pending=null;resolve('ROI 編輯器尚未回應，保留目前頁面，請稍後再試。');},5000);pending={id,resolve:message=>{clearTimeout(timer);pending=null;resolve(message);}};frame.contentWindow.postMessage({type:'rwtd-workspace-can-leave',id},roiOrigin);});}
  async function activate(target){
   if(switching||target===current)return;
   switching=true;
   try{
    const reason=current==='dashboard'?(window.rwtdWorkspaceCanLeave?.()??'Dashboard 尚未就緒，請稍後再試。'):current==='farm'?farmPanel.canLeave():await canLeaveRoi();
    if(reason){report(reason);return;}await workspaceCommand('switch',{source:current,target});
    if(target==='roi'&&!loaded){frame.src=config.roi_url;loaded=true;}
    else if(target==='roi')frame.contentWindow.postMessage({type:'rwtd-workspace-activate'},roiOrigin);
    current=target;if(current==='farm')await farmPanel.refresh();paint();report('');({dashboard:dashButton,roi:roiButton,farm:farmButton}[current]).focus();
   }catch(error){report(error.message);}finally{switching=false;}
  }
  window.addEventListener('message',event=>{
   if(event.origin!==roiOrigin||event.source!==frame.contentWindow)return;
   if(event.data?.type==='rwtd-workspace-leave-result'&&pending&&event.data.id===pending.id)pending.resolve(typeof event.data.reason==='string'?event.data.reason:'ROI 狀態無效，請先儲存。');
   if(event.data?.type==='rwtd-workspace-dashboard')activate('dashboard');
  });
  dashButton.onclick=()=>activate('dashboard');roiButton.onclick=()=>activate('roi');farmButton.onclick=()=>activate('farm');const poll=setInterval(()=>{if(current==='farm')farmPanel.refresh();},1000);window.addEventListener('pagehide',()=>clearInterval(poll));paint();
 }
};
window.rwtdWorkspaceReady=window.RwtdWorkspaceTabs.install().catch(error=>{const notice=document.getElementById('notice');if(notice)notice.textContent=error.message;});
