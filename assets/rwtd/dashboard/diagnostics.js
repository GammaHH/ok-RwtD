'use strict';
window.DashboardDiagnostics={
 redact(value){
  if(Array.isArray(value))return value.map(v=>this.redact(v));
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,/token|authorization|secret|password|webhook|bearer|credential/i.test(k)?'[REDACTED]':this.redact(v)]));
  return typeof value==='string'?value.replace(/Bearer\s+[^\s"'<>]+/gi,'Bearer [REDACTED]').replace(/([?#&]token=)[^\s&#"']+/gi,'$1[REDACTED]').replace(/https:\/\/(?:discord(?:app)?\.com)\/api\/webhooks\/[^\s"'<>]+/gi,'[REDACTED WEBHOOK]'):value;
 },
 format(value){return JSON.stringify(this.redact(value),null,2);},
 export(value,name='dashboard_diagnostics.json'){
  const stamp=new Date().toISOString().replace(/[:.]/g,'-'),url=URL.createObjectURL(new Blob([this.format(value)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=name.replace(/\.json$/,'_'+stamp+'.json');a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
};
