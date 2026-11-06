'use strict';
// Both the first target row and added rows use this shared recognition catalog.
window.TargetRecognitionKinds={
 coco:{label:'COCO Features（可多選）',coco:true},
 ocr:{label:'OCR 文字'},color:{label:'顏色',color:true},
 coco_color:{label:'COCO 且顏色（同一 COCO 目標內）',coco:true,color:true},
 coco_or_color:{label:'COCO 或顏色（任一符合）',coco:true,color:true},
 padding_coco:{label:'PADDING COCO（A／B TAG 依 Y 軸配對）',coco:true,padding:true}
};

// Data conversion only. No requests, Android input, or automatic migrations.
window.WorkflowModel={
 features(step){return step.features?.slice()||(step.feature?[step.feature]:[]);},
 action(step){return step.name==='WaitTest'?'wait':step.name==='SwipeCalibration'?'swipe':'tap';},
 hold(step){return this.action(step)==='tap'?(step.duration_ms||50):(step.duration_ms??0);},
 repeats(step){return step.repeat_count??1;},
 validateSwipe(step){if(step.mid_x!==undefined||step.mid_y!==undefined){if(step.name!=='SwipeCalibration'||![step.mid_x,step.mid_y].every(Number.isInteger)||step.mid_x<0||step.mid_x>=1080||step.mid_y<0||step.mid_y>=2400)throw Error('三點滑動需要合法的中間點');}return step;},
 validateRepeats(step){const count=this.repeats(step);if(!Number.isInteger(count)||count<1||count>100)throw Error('重複次數必須是 1–100 的整數');if(this.action(step)!=='tap'&&count!==1)throw Error('重複次數僅適用點擊（含長按）');return count;},
 validateConditions(step){this.validateConditionEffect(step);this.validateTargetConditions(step);if('ocr_target'in step){const t=step.ocr_target;if(!t||typeof t!=='object'||Array.isArray(t)||Object.keys(t).some(k=>!['text','match'].includes(k))||typeof t.text!=='string'||!t.text.trim()||t.text.length>200||!['exact','contains'].includes(t.match??'exact')||!['TapTest','LongPressTest','SwipeCalibration'].includes(step.name)||this.features(step).length||step.color_mode||!['single','unique_only','first_top_to_bottom','first_bottom_to_top','all_top_to_bottom'].includes(step.target_policy??'single')||step.name==='SwipeCalibration'&&step.target_policy==='all_top_to_bottom'||step.recognize_before===false||step.allow_unconfirmed_target===true)throw Error('OCR 定位需文字、比對方式及獨立辨識目標');}
  const has=(o,k)=>Object.prototype.hasOwnProperty.call(o,k),field=(o,k,d)=>has(o,k)?o[k]:d;
  if(has(step,'unknown_no_retry')&&typeof step.unknown_no_retry!=='boolean')throw Error('UNKNOWN 不重試必須為勾選值');
  if(!has(step,'input_conditions'))return;const g=step.input_conditions;
  if(!g||typeof g!=='object'||Array.isArray(g)||g.evaluation!=='left_to_right'||!Array.isArray(g.clauses)||!g.clauses.length||g.clauses.length>30||Object.keys(g).length!==2||Object.keys(g).some(k=>!['evaluation','clauses'].includes(k)))throw Error('條件須為由左至右的 1–30 列');
  for(const r of g.clauses){
   if(!r||typeof r!=='object'||Array.isArray(r)||!['coco','color','ocr'].includes(r.type)||!['AND','OR'].includes(field(r,'join','AND')))throw Error('條件類型或連接無效');
   const common=['type','join','roi','roi_tag'],specific={coco:['feature','threshold','match'],color:['rgb','tolerance','min_pixels','match'],ocr:['text','threshold','match']}[r.type];
   if(Object.keys(r).some(k=>!common.includes(k)&&!specific.includes(k)))throw Error('未知條件欄位');
   if(has(r,'roi')&&!RoiGeometry.valid(r.roi))throw Error('條件 ROI 超出範圍');
   if(has(r,'roi_tag')&&(typeof r.roi_tag!=='string'||!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(r.roi_tag)||has(r,'roi')))throw Error('條件 TAG 或 ROI 衝突');
   const score=field(r,'threshold',.8),match=field(r,'match',r.type==='ocr'?'contains':'detected');
   if(r.type!=='color'&&(!Number.isFinite(score)||score<.5||score>1))throw Error('條件信心門檻需為 0.5–1');
   if(r.type==='coco'&&(typeof r.feature!=='string'||!r.feature))throw Error('條件需選擇 Feature');
   if(r.type==='ocr'&&(typeof r.text!=='string'||!r.text.trim()||r.text.length>200||!['contains','exact'].includes(match)))throw Error('OCR 條件需預期文字及比對方法');
   if(r.type!=='ocr'&&!['detected','disappeared'].includes(match))throw Error('存在條件無效');
   const tolerance=field(r,'tolerance',10),minimum=field(r,'min_pixels',9);
   if(r.type==='color'&&(!Array.isArray(r.rgb)||r.rgb.length!==3||!r.rgb.every(v=>Number.isInteger(v)&&v>=0&&v<=255)||!Number.isInteger(tolerance)||tolerance<0||tolerance>255||!Number.isInteger(minimum)||minimum<1||minimum>2592000))throw Error('顏色條件需合法 RGB、容差及最小色塊');
  }
 },
 validateTargetConditions(step){if(!Object.hasOwn(step,'target_conditions'))return;const g=step.target_conditions;if(!g||Array.isArray(g)||typeof g!=='object'||Object.keys(g).sort().join(',')!=='clauses,evaluation,position_policy,position_source'||g.evaluation!=='left_to_right'||!['same','all','source'].includes(g.position_policy)||!Array.isArray(g.clauses)||g.clauses.length<1||g.clauses.length>30||!Number.isInteger(g.position_source)||g.position_source<0||g.position_source>=g.clauses.length||!['TapTest','LongPressTest','SwipeCalibration'].includes(step.name)||this.features(step).length||step.ocr_target||step.color_mode||step.recognize_before===false||step.allow_unconfirmed_target===true)throw Error('目標條件／定位來源無效');for(const r of g.clauses){if(!r||!Object.hasOwn(window.TargetRecognitionKinds,r.type)||!['AND','OR'].includes(r.join??'AND'))throw Error('目標列需有效辨識類型與 AND／OR');const type=window.TargetRecognitionKinds[r.type],keys=['type','join','threshold','roi','roi_tag',...(type.coco?['features','feature_priority']:[]),...(r.type==='ocr'?['text','match']:[]),...(type.color?['color_rgb','color_tolerance','color_min_pixels']:[])];if(Object.keys(r).some(k=>!keys.includes(k)))throw Error('未知目標列欄位');if(Object.hasOwn(r,'roi')&&!RoiGeometry.valid(r.roi)||Object.hasOwn(r,'roi_tag')&&(!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(r.roi_tag)||Object.hasOwn(r,'roi')))throw Error('目標 ROI／TAG 無效');const score=r.threshold??step.threshold??.9;if(!Number.isFinite(score)||score<.5||score>1)throw Error('目標信心門檻無效');if(r.type==='ocr'){if(typeof r.text!=='string'||!r.text.trim()||r.text.length>200||!['exact','contains'].includes(r.match??'exact'))throw Error('目標 OCR 文字無效');}if(type.color){if(!Array.isArray(r.color_rgb)||r.color_rgb.length!==3||r.color_rgb.some(v=>!Number.isInteger(v)||v<0||v>255)||!Number.isInteger(r.color_tolerance??10)||(r.color_tolerance??10)<0||(r.color_tolerance??10)>255||!Number.isInteger(r.color_min_pixels??9)||(r.color_min_pixels??9)<1||(r.color_min_pixels??9)>2592000)throw Error('目標顏色／容差／面積無效');}if(type.coco){if(!Array.isArray(r.features)||!r.features.length||r.features.length>30||r.features.some(f=>typeof f!=='string'||!f)||new Set(r.features).size!==r.features.length)throw Error('目標 COCO 需選擇 Feature');if(r.feature_priority&&(!Array.isArray(r.feature_priority)||r.feature_priority.length!==r.features.length||new Set(r.feature_priority).size!==r.features.length||r.feature_priority.some(f=>!r.features.includes(f))))throw Error('目標優先序無效');}}},
 validateConditionEffect(step){if(!Object.hasOwn(step,'condition_effect'))return;if(!step.input_conditions?.clauses?.length)throw Error('已選擇條件用途，請先按「＋新增條件」並設定 OCR／COCO／顏色條件；定位目標不會自動作為執行條件。');const effect=step.condition_effect;if(!step.input_conditions||!effect||Array.isArray(effect)||typeof effect!=='object'||!['skip_step','skip_remaining','skip_remaining_now','skip_range','retry_step'].includes(effect.action)||!['YES','NO'].includes(effect.when??'YES')||Object.keys(effect).some(k=>!['action','when',...(['skip_range','skip_remaining'].includes(effect.action)?['skip_self_on_mismatch','skip_self_on_match']:[]),...(effect.action==='skip_range'?['start_step_id','end_step_id']:[]),...(effect.action==='retry_step'?['retry_step_id','max_cycles','wait_seconds','timeout_seconds']:[])].includes(k)))throw Error('Step 條件用途無效');if(effect.action==='retry_step'&&(typeof effect.retry_step_id!=='string'||!effect.retry_step_id||!Number.isInteger(effect.max_cycles)||effect.max_cycles<1||effect.max_cycles>100||typeof effect.wait_seconds!=='number'||!Number.isFinite(effect.wait_seconds)||effect.wait_seconds<.1||effect.wait_seconds>60||typeof effect.timeout_seconds!=='number'||!Number.isFinite(effect.timeout_seconds)||effect.timeout_seconds<1||effect.timeout_seconds>1500))throw Error('條件重試需補做 Step、1～100 次、0.1～60 秒間隔及 1～1500 秒上限');if(Object.hasOwn(effect,'skip_self_on_mismatch')&&typeof effect.skip_self_on_mismatch!=='boolean')throw Error('舊版未符合時跳過本 Step 必須為勾選值');if(Object.hasOwn(effect,'skip_self_on_match')&&typeof effect.skip_self_on_match!=='boolean')throw Error('符合時跳過本 Step 必須為勾選值');if(Object.hasOwn(effect,'skip_self_on_match')&&Object.hasOwn(effect,'skip_self_on_mismatch'))throw Error('不能同時設定新版與舊版的跳過本 Step 規則');if(effect.action==='skip_range'&&['start_step_id','end_step_id'].some(k=>typeof effect[k]!=='string'||!effect[k]))throw Error('請選擇跳過範圍的起始與結束 Step');},
 conditionCombine(a,b,join){return join==='AND'?(a===null||b===null?null:a&&b):(a===null?b:b===null?a:a||b);},
 conditionCoco(matches,clause,roi=[0,0,1080,2400]){const hits=matches.filter(h=>h.feature===clause.feature),valid=hits.filter(h=>(h.status==null||h.status==='SNAPSHOT_ONLY_NOT_EXECUTION_TARGET')&&typeof h.matched==='boolean'&&((h.confidence==null&&h.matched===false)||(Number.isFinite(h.confidence)&&h.confidence>=0&&h.confidence<=1))),detected=valid.some(h=>h.matched&&h.confidence>=(clause.threshold??.8)&&h.bbox&&h.bbox[0]>=roi[0]&&h.bbox[1]>=roi[1]&&h.bbox[0]+h.bbox[2]<=roi[0]+roi[2]&&h.bbox[1]+h.bbox[3]<=roi[1]+roi[3]);if(!detected&&(!hits.length||valid.length!==hits.length))return null;return clause.match==='disappeared'?!detected:detected;},
 target(step){if(step.target_conditions)return step.target_conditions.clauses[0].type==='ocr'?'ocr':'coco';if(step.ocr_target)return 'ocr';return this.features(step).length&&step.recognize_before!==false?'coco':'fixed';},
 references(steps,tag,index=null,draft=null){return steps.filter((s,i)=>i!==index&&(s.roi_tag===tag||s.input_conditions?.clauses.some(r=>r.roi_tag===tag)||s.target_conditions?.clauses.some(r=>r.roi_tag===tag))).map(s=>s.step_id).concat(draft&&(draft.roi_tag===tag||draft.input_conditions?.clauses.some(r=>r.roi_tag===tag)||draft.target_conditions?.clauses.some(r=>r.roi_tag===tag))?[draft.step_id]:[]);},
 unboundReferences(steps,index=null,draft=null){return steps.filter((s,i)=>i!==index&&!s.roi_tag).map(s=>s.step_id).concat(draft&&!draft.roi_tag?[draft.step_id]:[]);},
 deleteTag(tags,steps,id){if(steps.some(s=>s.roi_tag===id||s.input_conditions?.clauses.some(r=>r.roi_tag===id)||s.target_conditions?.clauses.some(r=>r.roi_tag===id)))throw Error('TAG 仍被引用，請先解除或重新綁定');delete tags[id];},
 resolve(module,step){let s=structuredClone(step);if(module.global_roi!==undefined&&!RoiGeometry.valid(module.global_roi))throw Error('模組全域 ROI 無效');if(s.roi_tag){const tag=module.tags?.[s.roi_tag];if(!tag)throw Error('TAG 不存在於此模組');s.coco_roi=tag.roi.slice();delete s.roi_tag;}else if(module.global_roi)s.coco_roi=module.global_roi.slice();for(const r of [...(s.input_conditions?.clauses||[]),...(s.target_conditions?.clauses||[])]){if(r.roi_tag){if(!module.tags?.[r.roi_tag])throw Error('條件 TAG 不存在');r.roi=module.tags[r.roi_tag].roi.slice();delete r.roi_tag;}else if(!r.roi)r.roi=(s.target_conditions?.clauses.includes(r)?s.coco_roi?.slice():null)||module.global_roi?.slice()||[0,0,1080,2400];}return s;},
 validateFeatures(features){if(new Set(features).size!==features.length)throw Error('Feature 重複');},
 frameWidth(width,availableHeight,scale=1){const cap=width<=600?width-42:width-490;return Math.max(36,Math.min(Math.max(80,availableHeight)*.45*scale,cap));}
};
// Extend the shared target-row contract without changing existing row semantics.
const validateOriginalTargets=WorkflowModel.validateTargetConditions;
WorkflowModel.validateTargetConditions=function(step){
 const rows=step.target_conditions?.clauses;
 if(!Array.isArray(rows)||!rows.some(r=>r?.type==='padding_coco'))return validateOriginalTargets.call(this,step);
 const converted=structuredClone(step);
 const priority=rows.filter(r=>r?.check_kind==='region');
 const percentFallback=rows.filter(r=>r?.percent_fallback===true);
 if(percentFallback.length&&(percentFallback.length!==1||priority.length
    ||!['first_top_to_bottom','first_bottom_to_top'].includes(step.target_policy)
    ||rows.length>1&&(step.target_conditions.position_policy!=='same'||rows.some(r=>r?.type!=='padding_coco'||(r.selection_mode??'paired')!=='paired'||(r.join??'AND')!=='AND'))))throw Error('百分比保底需選最上或最下，多列時使用同位置 AND 配對');
 if(priority.length&&(priority.length!==1||step.target_conditions.position_policy!=='same'
    ||rows.some(r=>r?.type!=='padding_coco'||(r.selection_mode??'paired')!=='paired'||(r.join??'AND')!=='AND')
    ||!rows.some(r=>(r.check_kind??'coco')==='coco'&&(r.match??'disappeared')==='disappeared')
    ||!['first_top_to_bottom','first_bottom_to_top'].includes(step.target_policy)))throw Error('區域優先順序需搭配同位置 AND、COCO 圖示不存在，並選擇最上或最下');
 for(const r of converted.target_conditions.clauses){
  if(r?.type!=='padding_coco')continue;
  const modes={coco:['check_feature','match'],ocr:['check_text','check_text_match'],number:['check_op','check_value'],percent:['check_op','check_value','percent_fallback'],region:['region_priority','region_fallback']};
  const kind=r.check_kind??'coco';
  const fields=['type','join','features','feature_priority','threshold','a_tag','b_tag','a_roi','b_roi','check_kind','check_threshold','padding_y','offset_x','offset_y','check_width','check_height','selection_mode','selection_index',...(Object.hasOwn(modes,kind)?modes[kind]:[])];
  if(!Object.hasOwn(modes,kind))throw Error('PADDING B 辨識類型無效');
  if(!['paired','index'].includes(r.selection_mode??'paired')||r.selection_mode==='index'&&!Object.hasOwn(r,'selection_index')||Object.hasOwn(r,'selection_index')&&(!Number.isInteger(r.selection_index)||r.selection_index<1||r.selection_index>100))throw Error('PADDING 順位需為 1～100 的整數');
  if(Object.keys(r).some(k=>!fields.includes(k)))throw Error('PADDING COCO 含未知欄位');
  for(const[tag,roi]of [['a_tag','a_roi'],['b_tag','b_roi']]){
   if(Object.hasOwn(r,tag)===Object.hasOwn(r,roi))throw Error('請選擇 A／B TAG');
   if(Object.hasOwn(r,tag)&&(typeof r[tag]!=='string'||!/^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(r[tag])))throw Error('PADDING TAG 無效');
   if(Object.hasOwn(r,roi)&&!RoiGeometry.valid(r[roi]))throw Error('PADDING ROI 無效');
  }
  if(!Number.isFinite(r.check_threshold??.8)||(r.check_threshold??.8)<.5||(r.check_threshold??.8)>1||!Number.isInteger(r.padding_y??10)||(r.padding_y??10)<0||(r.padding_y??10)>1200)throw Error('PADDING B 的門檻或 Padding 無效');
  if(kind==='coco'&&(typeof r.check_feature!=='string'||!r.check_feature||!['detected','disappeared'].includes(r.match??'disappeared')))throw Error('請設定 PADDING B 的 COCO Feature');
  if(kind==='ocr'&&(typeof r.check_text!=='string'||!r.check_text.trim()||r.check_text.length>200||!['exact','contains'].includes(r.check_text_match??'exact')))throw Error('請設定 PADDING B 的 OCR 文字');
  if(['number','percent'].includes(kind)&&(!['eq','ne','gt','gte','lt','lte'].includes(r.check_op)||!Number.isFinite(r.check_value)||(kind==='percent'?(r.check_value<0||r.check_value>100):Math.abs(r.check_value)>1e15)))throw Error('請設定 PADDING B 的比較方式與數值（百分比 0～100）');
  if(kind==='region'&&(!Array.isArray(r.region_priority)||r.region_priority.length<1||r.region_priority.length>30||r.region_priority.some(name=>typeof name!=='string'||!name.trim()||name.length>200)||new Set(r.region_priority.map(name=>name.trim())).size!==r.region_priority.length||typeof (r.region_fallback??false)!=='boolean'))throw Error('請輸入 1～30 個不重複的區域名稱');
  if(kind==='percent'&&(Object.hasOwn(r,'percent_fallback')&&typeof r.percent_fallback!=='boolean'||r.percent_fallback&&(r.selection_mode??'paired')!=='paired'))throw Error('百分比保底需使用 A／B 配對');
  const modern=['offset_x','offset_y','check_width','check_height'];
  if(Object.hasOwn(r,'padding_y')&&modern.some(k=>Object.hasOwn(r,k)))throw Error('舊版上下擴張不可與偏移／寬高混用');
  for(const k of ['offset_x','offset_y'])if(Object.hasOwn(r,k)&&(!Number.isInteger(r[k])||r[k]<-2400||r[k]>2400))throw Error('偏移量需為 -2400～2400 整數');
  for(const[k,max]of [['check_width',1080],['check_height',2400]])if(Object.hasOwn(r,k)&&(!Number.isInteger(r[k])||r[k]<1||r[k]>max))throw Error('檢查 ROI 寬高無效');
  r.type='coco';if(r.a_tag)r.roi_tag=r.a_tag;else r.roi=r.a_roi;
  for(const k of fields.filter(k=>!['type','join','features','feature_priority','threshold'].includes(k)))delete r[k];
 }
 return validateOriginalTargets.call(this,converted);
};
const originalReferences=WorkflowModel.references;
WorkflowModel.references=function(steps,tag,index=null,draft=null){
 const refs=originalReferences.call(this,steps,tag,index,draft);
 for(const[s,i]of steps.map((s,i)=>[s,i]).concat(draft?[[draft,-1]]:[]))
  if(i!==index&&s.target_conditions?.clauses.some(r=>r.a_tag===tag||r.b_tag===tag))refs.push(s.step_id);
 return [...new Set(refs)];
};
const originalDeleteTag=WorkflowModel.deleteTag;
WorkflowModel.deleteTag=function(tags,steps,id){if(this.references(steps,id).length)throw Error('TAG 仍被引用，請先解除或重新綁定');return originalDeleteTag.call(this,tags,steps,id);};
const originalResolve=WorkflowModel.resolve;
WorkflowModel.resolve=function(module,step){
 const copy=structuredClone(step),padding=[];
 for(const[i,r]of (copy.target_conditions?.clauses||[]).entries())if(r.type==='padding_coco'){
  padding.push([i,r]);copy.target_conditions.clauses[i]={type:'coco',features:r.features};
 }
 const resolved=originalResolve.call(this,module,copy);
 for(const[i,r]of padding){for(const[tag,roi]of [['a_tag','a_roi'],['b_tag','b_roi']])if(r[tag]!==undefined){if(!module.tags?.[r[tag]])throw Error('PADDING TAG 不存在：'+r[tag]);r[roi]=module.tags[r[tag]].roi.slice();delete r[tag];}resolved.target_conditions.clauses[i]=r;}
 return resolved;
};
