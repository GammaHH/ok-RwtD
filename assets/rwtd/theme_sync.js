'use strict';
// Event-driven theme updates: initial load, window focus, and verified child saves.
window.RwtdThemeSync=(()=>{
 const params=new URLSearchParams(location.hash.slice(1)),key='rwtd-theme-token:'+location.origin;
 const token=params.get('token')||sessionStorage.getItem(key)||sessionStorage.getItem('dashboard-token:'+location.origin);
 if(token)sessionStorage.setItem(key,token);
 const parentOrigin=params.get('parent_origin');
 let busy=false;
 function apply(palette){
  if(!palette||typeof palette!=='object')return;
  for(const [name,value] of Object.entries(palette))if(/^[a-z][a-z_]{0,39}$/.test(name)&&/^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/i.test(value))document.documentElement.style.setProperty('--rwtd-'+name.replaceAll('_','-'),value);
 }
 function broadcast(palette){
  for(const frame of document.querySelectorAll('iframe'))try{const url=new URL(frame.src);if(/^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(url.origin))frame.contentWindow?.postMessage({type:'rwtd-theme-palette',palette},url.origin);}catch{}
 }
 async function refresh(){
  if(!token||busy||window.rwtdThemeHasDraft?.())return;busy=true;
   try{const response=await fetch('/api/theme',{headers:{Authorization:'Bearer '+token}});if(response.ok){const {palette}=await response.json();if(!window.rwtdThemeHasDraft?.()){apply(palette);broadcast(palette);}}}catch{}finally{busy=false;}
 }
 window.addEventListener('message',event=>{
  if(event.data?.type==='rwtd-theme-palette'&&parentOrigin&&event.origin===parentOrigin&&event.source===parent){if(!window.rwtdThemeHasDraft?.())apply(event.data.palette);return;}
  if(event.data?.type==='rwtd-theme-saved'){
   const owned=[...document.querySelectorAll('iframe')].some(frame=>{try{return frame.contentWindow===event.source&&new URL(frame.src).origin===event.origin;}catch{return false;}});
   if(owned)refresh();
  }
 });
 window.addEventListener('focus',refresh);
 refresh();
 return {apply,refresh};
})();
