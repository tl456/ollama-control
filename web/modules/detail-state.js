"use strict";
// Per-device presentation state only: identifiers and booleans, never message text.
window.WorkbenchDetails = (() => {
  const storageKey="workbench-detail-state-v1", limit=2000, ttl=30*86400000;
  const entries=new Map();
  try {
    const rows=JSON.parse(localStorage.getItem(storageKey)||"[]");
    if(Array.isArray(rows))for(const row of rows){
      if(Array.isArray(row)&&row.length===3&&typeof row[0]==="string"&&row[0].length<=500&&typeof row[1]==="boolean"&&Number.isFinite(row[2])&&Date.now()-row[2]<ttl)entries.set(row[0],{open:row[1],at:row[2]});
    }
  } catch {}
  function prune(){
    const now=Date.now();
    for(const [key,value] of entries)if(now-value.at>ttl)entries.delete(key);
    while(entries.size>limit)entries.delete(entries.keys().next().value);
  }
  function persist(){
    prune();
    try{localStorage.setItem(storageKey,JSON.stringify([...entries].map(([key,value])=>[key,value.open,value.at])));}catch{}
  }
  prune();
  function remember(key,open){
    if(!key||key.length>500)return;
    open=!!open;
    if(entries.get(key)?.open===open||(!entries.has(key)&&!open))return;
    entries.delete(key);entries.set(key,{open,at:Date.now()});persist();
  }
  function capture(container){
    if(!container)return;
    for(const detail of container.querySelectorAll("details[data-fold-memory]"))remember(detail.dataset.foldMemory,detail.open);
  }
  function apply(container,conversationId){
    if(!container||!conversationId)return;
    const messages=container.matches?.("[data-message]")?[container]:[...container.querySelectorAll("[data-message]")];
    for(const message of messages){
      const messageId=message.dataset.message;if(!messageId)continue;
      for(const detail of message.querySelectorAll("details")){
        const kind=detail.dataset.toolKey?"tool:"+detail.dataset.toolKey:detail.dataset.detailKind;
        if(!kind)continue;
        const key=JSON.stringify([String(conversationId),String(messageId),kind]);
        detail.dataset.foldMemory=key;
        const saved=entries.get(key);
        if(saved&&Date.now()-saved.at<ttl)detail.open=saved.open;
      }
    }
  }
  document.addEventListener("toggle",event=>{
    const detail=event.target;
    if(detail?.tagName==="DETAILS"&&detail.dataset.foldMemory)remember(detail.dataset.foldMemory,detail.open);
  },true);
  // Save the requested state before any synchronous message redraw can detach it.
  document.addEventListener("click",event=>{
    if(event.defaultPrevented||event.target.closest?.("button,a,input,select,textarea"))return;
    const summary=event.target.closest?.("summary"),detail=summary?.parentElement;
    if(detail?.tagName==="DETAILS"&&detail.dataset.foldMemory)remember(detail.dataset.foldMemory,!detail.open);
  });
  return Object.freeze({apply,capture});
})();
