'use strict';
window.requestRwtdTestContext=function(fields){
 const el=id=>document.getElementById(id),dialog=el('testContextDialog');
 if(dialog.open)return Promise.resolve(null);
 if(!fields?.length){el('testContextError').textContent='目前後端未提供測試條件欄位，請重新啟動 OK 載入更新。';}
 const defaults=window.rwtdTestContextDefaults||{},inputs=[],host=el('testContextFields');host.replaceChildren();
 for(const field of fields||[]){const label=document.createElement('label'),input=document.createElement(field.type==='enum'?'select':'input');label.append(document.createTextNode(field.label));
  if(field.type==='enum'){input.add(new Option('未指定（沿用原設定）',''));for(const [index,item]of field.options.entries())input.add(new Option(item.label,String(index)));}
  else if(field.type==='boolean'){input.type='checkbox';input.checked=defaults[field.key]===true;}
  else{input.type=field.type==='text'?'text':'number';input.step=field.type==='integer'?'1':'any';if(field.min!==undefined)input.min=field.min;if(field.max!==undefined)input.max=field.max;if(field.type==='text')input.maxLength=field.max_length??200;input.placeholder='未指定（沿用原設定）';}
  if(field.type==='enum'){const index=field.options.findIndex(item=>item.value===defaults[field.key]);input.value=index<0?'':String(index);}
  else if(field.type!=='boolean')input.value=defaults[field.key]??'';
  let enabled=null;if(field.type==='boolean'){enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=Object.hasOwn(defaults,field.key);const use=document.createElement('span');use.append(enabled,document.createTextNode('覆寫此項：'));label.append(use);input.disabled=!enabled.checked;enabled.onchange=()=>{input.disabled=!enabled.checked;};}
  label.append(input);if(field.description){const help=document.createElement('small');help.textContent=field.description;label.append(help);}host.append(label);inputs.push([field,input,enabled]);
 }
 if(fields?.length)el('testContextError').textContent='';
 return new Promise(resolve=>{let completed=false;const finish=value=>{if(completed)return;completed=true;dialog.close();resolve(value);};
  el('testContextCancel').onclick=()=>finish(null);
  dialog.oncancel=event=>{event.preventDefault();finish(null);};
  el('testContextApply').onclick=()=>{const values={};for(const[field,input,enabled]of inputs){
   if(field.type==='boolean'){if(enabled.checked)values[field.key]=input.checked;continue;}
   if(input.value==='')continue;
   let value,valid=true;
   if(field.type==='enum'){const option=field.options[Number(input.value)];valid=!!option;value=option?.value;}
   else if(field.type==='text'){value=input.value;valid=value.length<=(field.max_length??200);}
   else if(field.type==='number'||field.type==='integer'){value=Number(input.value);valid=Number.isFinite(value)&&(field.type!=='integer'||Number.isSafeInteger(value))&&(field.min===undefined||value>=field.min)&&(field.max===undefined||value<=field.max);}
   else valid=false;
   if(!valid){el('testContextError').textContent=field.label+' 的輸入值無效';return;}values[field.key]=value;
  }window.rwtdTestContextDefaults=structuredClone(values);finish(values);};
  dialog.showModal();
 });
};
