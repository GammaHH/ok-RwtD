'use strict';
// The Dashboard editor and FARM monitor share this exact renderer.
window.RwtdCanvasView={
 fitView(svg,graph,padding=36){
  const nodes=graph?.nodes||[];
  if(!nodes.length)return {x:0,y:0,zoom:1};
  let left=Infinity,top=Infinity,right=-Infinity,bottom=-Infinity;
  for(const node of nodes){
   const p=graph.ui?.positions?.[node.id]||{x:20,y:20};
   left=Math.min(left,p.x);top=Math.min(top,p.y-(node.entry_guard?18:0));
   right=Math.max(right,p.x+this.nodeWidth(node)+(node.entry_guard?175:0));bottom=Math.max(bottom,p.y+110);
  }
  try{const box=svg.querySelector?.(':scope > g')?.getBBox?.();if(box&&[box.x,box.y,box.width,box.height].every(Number.isFinite)&&box.width>0&&box.height>0){left=Math.min(left,box.x);top=Math.min(top,box.y);right=Math.max(right,box.x+box.width);bottom=Math.max(bottom,box.y+box.height);}}catch{}
  const viewport=svg.viewBox?.baseVal||{x:0,y:0,width:1000,height:520};
  const zoom=Math.min(1.5,Math.max(.001,Math.min(Math.max(1,viewport.width-2*padding)/Math.max(1,right-left),Math.max(1,viewport.height-2*padding)/Math.max(1,bottom-top))));
  return {x:(viewport.x||0)+viewport.width/2-(left+right)/2*zoom,y:(viewport.y||0)+viewport.height/2-(top+bottom)/2*zoom,zoom};
 },
 layoutByChains(graph){
  const nodes=graph.nodes||[],byId=new Map(nodes.map(n=>[n.id,n])),next=new Map(nodes.map(n=>[n.id,[]]));
  for(const edge of graph.edges||[])if(byId.has(edge.source)&&byId.has(edge.target)&&!next.get(edge.source).includes(edge.target))next.get(edge.source).push(edge.target);
  const roots=nodes.filter(n=>n.kind==='start').sort((a,b)=>(a.task_type==='TriggerTask')-(b.task_type==='TriggerTask')||(a.chain?.order??9999)-(b.chain?.order??9999));
  const groups=roots.map(root=>({root:root.id,nodes:[]})),owner=new Map(),queue=[];
  roots.forEach((root,index)=>{owner.set(root.id,index);queue.push(root.id);});
  for(let i=0;i<queue.length;i++){const id=queue[i],index=owner.get(id);groups[index].nodes.push(byId.get(id));for(const target of next.get(id))if(!owner.has(target)){owner.set(target,index);queue.push(target);}}
  // Orphan/always-on components keep every node, rather than disappearing.
  for(const node of nodes)if(!owner.has(node.id)){
   const index=groups.length,group={root:node.id,nodes:[]},pending=[node.id];groups.push(group);owner.set(node.id,index);
   for(let i=0;i<pending.length;i++){const id=pending[i];group.nodes.push(byId.get(id));for(const target of next.get(id))if(!owner.has(target)){owner.set(target,index);pending.push(target);}}
  }
  const positions={};let x=30,y=40,rowHeight=0;
  groups.forEach((group,index)=>{
   if(index&&index%3===0){x=30;y+=rowHeight+130;rowHeight=0;}
   const members=new Set(group.nodes.map(n=>n.id)),incoming=new Map(group.nodes.map(n=>[n.id,0]));
   for(const node of group.nodes)for(const target of next.get(node.id))if(members.has(target))incoming.set(target,incoming.get(target)+1);
   const linear=group.nodes.every(n=>next.get(n.id).filter(id=>members.has(id)).length<=1&&incoming.get(n.id)<=(n.id===group.root?0:1));
   const columns=linear?1:Math.min(4,group.nodes.length),cellWidth=Math.max(...group.nodes.map(n=>this.nodeWidth(n)+(n.entry_guard?175:0)))+60;
   const bodyWidth=Math.max(...group.nodes.map(n=>this.nodeWidth(n)));
   group.nodes.forEach((n,i)=>positions[n.id]={x:x+(i%columns)*cellWidth+(linear?(bodyWidth-this.nodeWidth(n))/2:0),y:y+Math.floor(i/columns)*140});
   const height=Math.ceil(group.nodes.length/columns)*140;
   rowHeight=Math.max(rowHeight,height);x+=columns*cellWidth+100;
  });
  return positions;
 },
 findNodes(graph,query){
  const q=String(query||'').trim().toLocaleLowerCase();if(!q)return [];
  const number=q.match(/^(?:node\s*)?(\d+)$/)?.[1];
  const score=n=>{const id=String(n.id).toLocaleLowerCase(),name=String(n.name||'').toLocaleLowerCase();
   if(id===q||number&&id.match(/^(?:node)?(\d+)$/)?.[1]===number)return 0;
   if(name===q)return 1;if(id.includes(q)||name.includes(q))return 2;return 3;};
  return (graph?.nodes||[]).map((node,index)=>({node,index,score:score(node)}))
   .filter(row=>row.score<3).sort((a,b)=>a.score-b.score||a.index-b.index).map(row=>row.node);
 },
 centerNode(svg,graph,node,view){
  const p=graph.ui?.positions?.[node.id]||{x:20,y:20},box=svg.viewBox?.baseVal||{x:0,y:0,width:1000,height:520};
  const zoom=Math.max(.8,view.zoom);
  return {x:(box.x||0)+box.width/2-(p.x+this.nodeWidth(node)/2)*zoom,
          y:(box.y||0)+box.height/2-(p.y+41)*zoom,zoom};
 },
 createSearch(container,getGraph,selectNode){
  const input=document.createElement('input'),results=document.createElement('select'),status=document.createElement('small');
  container.classList.add('canvasNodeSearch');input.type='search';input.placeholder='搜尋 NODE 編號／名稱';
  input.setAttribute('aria-label','搜尋節點編號或節點名稱');results.setAttribute('aria-label','節點搜尋結果');
  results.size=5;results.hidden=true;status.setAttribute('role','status');container.append(input,results,status);
  let signature=null;
  const refresh=()=>{const graph=getGraph(),key=JSON.stringify([input.value,(graph?.nodes||[]).map(n=>[n.id,n.name])]);
   if(key===signature)return;signature=key;results.replaceChildren();
   const matches=this.findNodes(graph,input.value);results.hidden=!matches.length;
   for(const node of matches.slice(0,50)){const option=document.createElement('option');option.value=node.id;
    option.textContent=node.id+(node.name?' · '+node.name:'');results.append(option);}
   if(matches.length)results.value=matches[0].id;
   status.textContent=input.value.trim()?(matches.length?'找到 '+matches.length+' 個節點'+(matches.length>50?'（顯示前 50 個，請縮小搜尋）':''):'找不到符合的節點'):'';
  };
  const locate=()=>{const node=(getGraph()?.nodes||[]).find(n=>n.id===results.value);if(node)selectNode(node);};
  input.addEventListener('input',refresh);input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();refresh();locate();}
   else if(e.key==='ArrowDown'&&!results.hidden){e.preventDefault();results.focus();}
   else if(e.key==='Escape'){input.value='';refresh();}});
  results.addEventListener('change',locate);results.addEventListener('click',locate);results.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();locate();}});
  return {refresh};
 },
 world(svg,e,view){const point=svg.createSVGPoint();point.x=e.clientX;point.y=e.clientY;const local=point.matrixTransform(svg.getScreenCTM().inverse());return{x:(local.x-view.x)/view.zoom,y:(local.y-view.y)/view.zoom,sx:local.x,sy:local.y};},
 zoomAt(view,p,factor){const zoom=Math.min(3,Math.max(.001,view.zoom*factor));return{x:p.sx-p.x*zoom,y:p.sy-p.y*zoom,zoom};},
 renderTriggerStatus(tasksElement,requestsElement,{graph,triggerState={tasks:[],queue:[]},actions=null}){tasksElement.replaceChildren();for(const task of triggerState.tasks){const row=document.createElement('div');row.className='toolbar';const info=document.createElement('span');info.style.color=task.status==='RUNNING'?'var(--rwtd-mint, #79DEC0)':task.status==='ERROR'?'var(--rwtd-danger, #F08391)':task.status==='PAUSED'?'var(--rwtd-highlight, #FFD800)':'var(--rwtd-text-secondary, #B2B1C6)';info.textContent=task.name+(task.preempt_operations?' [插隊]':'')+' · '+task.status+' · '+task.trigger_interval+' s · '+(task.current_node||'')+' · '+task.reason+(task.observation_thread?' · '+(task.observation_busy?'觀測中':'等待觀測')+(task.observation_seconds!=null?' · '+task.observation_seconds+' s':''):'');row.append(info);if(actions)for(const[op,label,show]of [['pause','暫停',task.status==='RUNNING'],['resume','恢復',task.status==='PAUSED'],['stop','停止',!['IDLE','STOPPED','ERROR'].includes(task.status)],['verify','人工處理完成',task.status==='WAITING_MANUAL']])if(show){const button=document.createElement('button');button.textContent=label;button.onclick=()=>actions.submit('trigger_control',{task_id:task.id,operation:op,...(op==='verify'?{pass:true}:{})});row.append(button);}tasksElement.append(row);}for(const n of graph.nodes.filter(n=>n.kind==='start'&&n.task_type==='TriggerTask'&&!triggerState.tasks.some(t=>t.id===n.id))){const info=document.createElement('p');info.style.color=n.enabled===true?'var(--rwtd-mint, #79DEC0)':'var(--rwtd-text-secondary, #B2B1C6)';info.textContent=(n.name||n.id)+' · '+(n.enabled===true?'已納入排程；尚未啟動':'未納入排程')+' · '+(n.trigger_interval??3)+' s';tasksElement.append(info);}requestsElement.replaceChildren();for(const item of triggerState.queue){const row=document.createElement('div');row.className='toolbar';const info=document.createElement('span');info.textContent=item.task_id+' → '+item.module+(item.kind==='action'?' / Step '+item.definition.step_id:'')+' · '+(item.status==='WAITING_DEVICE'?'唯讀模式：切換實機模式並重新啟動監看後才執行':item.status==='RUNNING'?'執行中':'已排隊，等待目前操作完成');row.append(info);const load=document.createElement('button');load.textContent='載入模組';load.onclick=()=>actions.load({id:item.module,revision:item.module_revision});const remove=document.createElement('button');remove.textContent='移除待辦';remove.onclick=()=>actions.submit('trigger_control',{task_id:item.task_id,operation:'remove_request',node:item.node});if(actions){if(item.kind!=='action')row.append(load);if(item.status!=='RUNNING')row.append(remove);}requestsElement.append(row);}for(const item of (triggerState.results||[]).slice(-5)){const info=document.createElement('p');info.className='compactStatus';info.textContent=item.task_id+' → '+item.module+' · '+item.status;requestsElement.append(info);}},
 createMonitor(container){
  const bar=document.createElement('div'),reset=document.createElement('button'),expand=document.createElement('button'),hint=document.createElement('span'),svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  bar.className='toolbar';reset.type=expand.type='button';reset.textContent='重設視野';expand.textContent='放大顯示';hint.className='canvasHint';hint.textContent='滾輪縮放、拖曳移動視野；唯讀';bar.append(reset,expand,hint);
  svg.id='farmDecisionCanvas';svg.classList.add('rwtdCanvas','canvasReadOnly');svg.setAttribute('viewBox','0 0 1000 520');svg.setAttribute('tabindex','0');svg.setAttribute('aria-label','FARM 目前執行的 Canvas，唯讀');container.append(bar,svg);
  let state=null,key=null,view={x:0,y:0,zoom:1},pan=null,selected=new Set();
  const searchHost=document.createElement('div');bar.replaceChildren(searchHost,reset,expand,hint);
  const search=this.createSearch(searchHost,()=>state?.graph,node=>{selected=new Set([node.id]);view=this.centerNode(svg,state.graph,node,view);draw();});
  const initial=()=>{const v=state?.graph?.ui?.viewport;return v&&[v.x,v.y,v.zoom].every(Number.isFinite)&&v.zoom>=.001&&v.zoom<=3?{...v}:{x:0,y:0,zoom:1};};
  const draw=()=>{search.refresh();if(state?.graph)window.RwtdCanvasView.render(svg,{graph:state.graph,runtime:state.runtime||{},view,selected,catalog:state.modules||[],triggerState:state.trigger_runtime||{tasks:[]},locked:state.running===true,markerPrefix:'farm-'});else svg.replaceChildren();};
  reset.onclick=()=>{view=this.fitView(svg,state?.graph);draw();};expand.onclick=()=>{container.classList.toggle('canvasExpanded');expand.textContent=container.classList.contains('canvasExpanded')?'縮回':'放大顯示';};
  svg.addEventListener('wheel',e=>{e.preventDefault();view=this.zoomAt(view,this.world(svg,e,view),e.deltaY<0?1.1:1/1.1);draw();},{passive:false});
  svg.addEventListener('pointerdown',e=>{if(![0,1].includes(e.button))return;e.preventDefault();svg.focus();pan={id:e.pointerId,start:this.world(svg,e,view),view:{...view}};svg.setPointerCapture(e.pointerId);});
  svg.addEventListener('pointermove',e=>{if(!pan||pan.id!==e.pointerId)return;const p=this.world(svg,e,view);view={...pan.view,x:pan.view.x+p.sx-pan.start.sx,y:pan.view.y+p.sy-pan.start.sy};draw();});
  const release=e=>{if(pan?.id!==e.pointerId)return;pan=null;if(svg.hasPointerCapture(e.pointerId))svg.releasePointerCapture(e.pointerId);};svg.addEventListener('pointerup',release);svg.addEventListener('pointercancel',release);
  return {update(next){const nextKey=next.canvas_id||next.canvas||null;state=next;if(key!==nextKey){key=nextKey;view=initial();pan=null;selected=new Set();}draw();}};
 },
 nodeWidth(n){return Math.max(190,CanvasModel.ports(n).length*70);},
 render(svg,options){
 const {graph,runtime={},view={x:0,y:0,zoom:1},catalog=[],selected=new Set(),selectedEdge=null,drag=null,triggerState={tasks:[]},markerPrefix=''}=options;
 const ns='http://www.w3.org/2000/svg',locked=()=>options.locked===true;
 function element(tag,attrs,text){const e=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs)){const value=k==='id'&&tag==='marker'?markerPrefix+v:k==='marker-end'?v.replace('url(#','url(#'+markerPrefix):v;e.setAttribute(k,value);}if(text!==undefined)e.textContent=text;return e;}
 function position(id){return graph.ui.positions[id]||{x:20,y:20};}
 function nodeWidth(n){return Math.max(190,CanvasModel.ports(n).length*70);}
 function portLabel(n,port){return n.ocr_cases?.find(c=>'CASE_'+c.id===port)?.name||port;}
 function portPoint(id,port){const n=graph.nodes.find(n=>n.id===id),p=position(id),list=CanvasModel.ports(n);return {x:p.x+nodeWidth(n)*(list.indexOf(port)+1)/(list.length+1),y:p.y+82};}
 function inputPoint(id){const p=position(id);return{x:p.x+nodeWidth(graph.nodes.find(n=>n.id===id))/2,y:p.y};}
 function summary(n){if(n.kind==='start'&&n.chain)return '#'+n.chain.order+' · '+(n.chain.type==='memory'?'記憶 Chain':'一般 Chain');if(n.kind==='start')return n.task_type==='TriggerTask'?(n.enabled===true?'已納入 · ':'未納入 · ')+(n.preempt_operations?'插隊 Trigger · ':'Trigger · ')+(n.trigger_interval??3)+' s':n.id===graph.entry?'主流程入口':'主流程開始（未設入口）';const names={start:'開始',end:'結束',manual:'等候人工 Pass / Fail'};if(names[n.kind])return names[n.kind];if(n.kind==='action')return n.module+' / '+n.step_id+(n.execution_scope==='to_end'?' → 結尾':n.execution_scope==='range'?' → '+n.end_step_id:'')+(n.input_source==='step'?' · 直接操作':' ← '+n.variable);if(n.kind==='ocr_decision')return 'OCR '+n.op+' '+n.value;if(n.kind==='ocr')return 'OCR → '+n.output_key;if(n.kind==='notify')return '通知 '+(n.variable||'');if(n.variable)return n.variable+' '+n.op+' '+n.value;if(n.kind==='exclusion')return (n.exclusion_scope==='next'?'NEXT 禁點：':'記住禁區：')+({roi:'固定 ROI',color:'顏色',coco_or_color:'顏色 OR '+n.feature}[n.exclusion_mode]||n.feature);if(n.kind==='module')return catalog.find(m=>m.id===n.module)?.name||'選取模組';if(n.kind==='wait')return '等待 '+n.seconds+' 秒';if(n.feature)return n.feature+' '+(n.match==='disappeared'?'消失':'偵測到')+(n.tag?' @ '+n.tag:'');return n.field+' '+({gte:'≥',lte:'≤',eq:'='}[n.op]||'')+' '+n.value;}
 function renderEntryGuard(n,p,group){const a=n.entry_guard,x=p.x+nodeWidth(n)+20,y=p.y-18,g=element('g',{'data-node-id':n.id,'data-entry-guard-for':n.id});g.append(element('path',{d:`M ${x} ${y+35} L ${p.x+nodeWidth(n)} ${p.y+12}`,stroke:'var(--rwtd-accent, #844FB5)','stroke-width':2,'stroke-dasharray':'5 4',fill:'none'}));g.append(element('rect',{x,y,width:155,height:66,rx:8,fill:'var(--rwtd-selected-surface, #352442)',stroke:'var(--rwtd-accent, #844FB5)'}));g.append(element('text',{x:x+8,y:y+20,fill:'var(--rwtd-text, #EAE9F2)','font-size':13},'🛡 附加 Guard'));g.append(element('text',{x:x+8,y:y+40,fill:'var(--rwtd-text-secondary, #B2B1C6)','font-size':11},(a.conditions?(a.operator.toUpperCase()+' · '+a.conditions.length+' 個條件'):(a.kind==='coco'?a.feature:a.kind==='ocr'?'OCR '+a.text:a.kind==='time'?(a.mode==='daily'?'每日 '+a.at:'冷卻 '+a.seconds+' 秒'):a.variable)).slice(0,23)));g.append(element('text',{x:x+8,y:y+57,fill:'var(--rwtd-accent, #844FB5)','font-size':10},n.unknown_no_retry?'未成立／UNKNOWN → 跳過':'未成立 → 跳過；UNKNOWN 重試'));group.append(g);}
 function renderChainLinks(group){const starts=graph.nodes.filter(n=>n.kind==='start'&&n.task_type!=='TriggerTask'&&n.chain).sort((a,b)=>a.chain.order-b.chain.order);function arrow(a,b,label,retry=false){const p=position(a.id),q=position(b.id),ax=retry?p.x:p.x+nodeWidth(a),bx=retry?q.x:q.x+nodeWidth(b),ay=p.y+32,by=q.y+32,bend=retry?-90:90,color=retry?'var(--rwtd-orange, #FFB900)':'var(--rwtd-mint, #79DEC0)',g=element('g',{'data-chain-link':retry?'Return / Retry':'Next','data-chain-source':a.id,'data-chain-target':b.id,'data-node-id':a.id});g.append(element('path',{d:`M${ax} ${ay} C${ax+bend} ${ay} ${bx+bend} ${by} ${bx} ${by}`,fill:'none',stroke:color,'stroke-width':2,'stroke-dasharray':retry?'8 5':'none','marker-end':`url(#${retry?'chainRetryArrow':'chainNextArrow'})`}));g.append(element('text',{x:(ax+bx)/2+bend,y:(ay+by)/2-8,fill:color,'font-size':12},label));group.append(g);}for(let i=0;i<starts.length;i++){const a=starts[i],next=Object.hasOwn(a.chain,'next')?starts.find(n=>n.id===a.chain.next):starts[i+1];if(next)arrow(a,next,'Next · Chain');if(a.chain.type==='memory'&&next)arrow(next,a,'成功後才回訪 · 僅失敗待重試／上限內',true);}} function render(){svg.replaceChildren();const defs=element('defs',{}),marker=element('marker',{id:'canvasArrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});marker.append(element('path',{d:'M0 0 L10 5 L0 10 Z',fill:'var(--rwtd-mint, #79DEC0)'}));defs.append(marker);for(const[id,color]of [['chainNextArrow','var(--rwtd-mint, #79DEC0)'],['chainRetryArrow','var(--rwtd-orange, #FFB900)']]){const m=element('marker',{id,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto'});m.append(element('path',{d:'M0 0 L10 5 L0 10 Z',fill:color}));defs.append(m);}svg.append(defs);const group=element('g',{transform:`translate(${view.x} ${view.y}) scale(${view.zoom})`});svg.append(group);renderChainLinks(group);
 for(const edge of graph.edges){const a=portPoint(edge.source,edge.port),b=inputPoint(edge.target),bend=Math.max(60,Math.abs(b.y-a.y)/2),d=`M${a.x} ${a.y} C${a.x} ${a.y+bend} ${b.x} ${b.y-bend} ${b.x} ${b.y}`;const g=element('g',{'data-edge-id':edge.id});g.append(element('path',{d,fill:'none',stroke:'transparent','stroke-width':18,'pointer-events':'stroke'}));g.append(element('path',{d,fill:'none',stroke:edge.id===runtime.active_edge?'var(--rwtd-graph-highlight, #FFD000)':edge.id===selectedEdge?'var(--rwtd-accent, #844FB5)':'var(--rwtd-mint, #79DEC0)','stroke-width':edge.id===selectedEdge?4:2,'marker-end':'url(#canvasArrow)','pointer-events':'none'}));g.append(element('text',{x:a.x+8,y:a.y+25,fill:'var(--rwtd-text-secondary, #B2B1C6)','font-size':12},portLabel(graph.nodes.find(n=>n.id===edge.source),edge.port)));group.append(g);}
 for(const n of graph.nodes){const p=position(n.id),g=element('g',{'data-node-id':n.id}),color=n.id===runtime.guard?'var(--rwtd-danger, #F08391)':(n.id===runtime.current_node||triggerState.tasks.some(t=>t.status==='RUNNING'&&t.current_node===n.id))&&locked()?'var(--rwtd-graph-highlight, #FFD000)':(runtime.completed||[]).includes(n.id)?'var(--rwtd-green, #A1DC86)':'var(--rwtd-border-strong, #55516E)';g.append(element('rect',{x:p.x,y:p.y,width:nodeWidth(n),height:82,rx:9,fill:selected.has(n.id)?'var(--rwtd-selected-surface, #352442)':'var(--rwtd-surface, #181B2B)',stroke:n.kind==='start'&&n.task_type==='TriggerTask'?(n.enabled!==true?'var(--rwtd-text-disabled, #6C6B80)':triggerState.tasks.find(t=>t.id===n.id)?.status==='ERROR'?'var(--rwtd-danger, #F08391)':triggerState.tasks.find(t=>t.id===n.id)?.status==='PAUSED'?'var(--rwtd-highlight, #FFD800)':'var(--rwtd-green, #A1DC86)'):color,'stroke-width':selected.has(n.id)?3:2}));g.append(element('text',{x:p.x+10,y:p.y+23,fill:'var(--rwtd-text, #EAE9F2)','font-size':15},(n.kind==='guard'?'🛡 ':'')+(n.name||n.id).slice(0,20)));g.append(element('text',{x:p.x+10,y:p.y+46,fill:'var(--rwtd-text-secondary, #B2B1C6)','font-size':12},summary(n).slice(0,27)));g.append(element('text',{x:p.x+10,y:p.y+68,fill:'var(--rwtd-text-tertiary, #9694AD)','font-size':10},n.id+' · '+n.kind.toUpperCase()+(n.kind==='guard'?' · ALWAYS-ON':'')));if(!['guard','start'].includes(n.kind))g.append(element('circle',{cx:p.x+nodeWidth(n)/2,cy:p.y,r:7,fill:'var(--rwtd-mint, #79DEC0)','data-input-id':n.id}));for(const port of CanvasModel.ports(n)){const pt=portPoint(n.id,port);g.append(element('circle',{cx:pt.x,cy:pt.y,r:7,fill:'var(--rwtd-accent, #844FB5)','data-output-id':n.id,'data-port':port}));}group.append(g);if(n.entry_guard)renderEntryGuard(n,p,group);}
 if(drag?.type==='connect'){const a=portPoint(drag.id,drag.port),b=drag.to;group.append(element('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:'var(--rwtd-accent, #844FB5)','stroke-width':3}));}
 if(drag?.type==='select'){const a=drag.start,b=drag.to;group.append(element('rect',{x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),width:Math.abs(a.x-b.x),height:Math.abs(a.y-b.y),fill:'var(--rwtd-selection-fill, #844FB522)',stroke:'var(--rwtd-accent, #844FB5)'}));}
 }
 render();
 }
};

