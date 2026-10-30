"use strict";
// Capability-driven presentation; requestOptions snapshots only the existing protocol fields.
window.WorkbenchReasoning = (() => {
  const ORDER=['off','low','medium','high','max'];
  const DEEPSEEK=['off','low','high','max'];
  const LABEL={off:'关闭',low:'低',medium:'中',high:'高',max:'最高'};
  const normalize=value=>value==='none'?'off':value;
  const own=(value,key)=>Object.prototype.hasOwnProperty.call(value||{},key);
  const pending=new Map();
  function profile(provider,metadata) {
    if(!provider||provider.id==='local'||provider.kind==='ollama')return null;
    const value={...provider};
    for(const key of ['reasoning_effort_supported','reasoning_effort_levels','reasoning_effort'])if(own(metadata,key))value[key]=metadata[key];
    const deepseek=value.thinking_mode==='deepseek';
    if(metadata?.reasoning_effort_supported===false)return null;
    const declared=Array.isArray(value.reasoning_effort_levels);
    if(!deepseek&&!(value.reasoning_effort_supported===true&&declared))return null;
    const raw=declared?value.reasoning_effort_levels.map(normalize):DEEPSEEK;
    if(!raw.length||raw.some(level=>!ORDER.includes(level)||(deepseek&&!DEEPSEEK.includes(level))))return null;
    const levels=ORDER.filter(level=>raw.includes(level));
    const defaultEffort=normalize(value.reasoning_effort||'high');
    if(!levels.includes(defaultEffort))return null;
    return {levels,defaultEffort,deepseek};
  }
  const keyFor=o=>JSON.stringify([state.conv?.id||'',o.provider,o.model]);
  function current(o=options()) {
    const provider=state.cfg.providers?.find(item=>item.id===o.provider);
    const same=state.modelCapsFor?.provider===o.provider&&state.modelCapsFor?.model===o.model;
    return profile(provider,same?state.modelCaps:null);
  }
  function selected(o=options()) {return pending.get(keyFor(o))??o.reasoning_effort??'inherit';}
  function requestOptions(o=options()) {
    const result={...o},cap=current(o);
    if(!cap)return result;
    const choice=normalize(selected(o)),effort=choice==='inherit'?cap.defaultEffort:choice;
    const available=cap.levels.includes(effort)?effort:cap.defaultEffort;
    return {...result,reasoning_effort:available,think:available!=='off'};
  }
  function controls() {
    const local=document.querySelector('#think-toggle');if(!local)return;
    const o=options(),provider=state.cfg.providers?.find(item=>item.id===o.provider);
    const cloud=o.provider!=='local'&&provider?.kind!=='ollama';local.hidden=cloud;
    let node=document.querySelector('#conversation-effort-control');
    if(!node){
      node=document.createElement('div');node.id='conversation-effort-control';node.className='cloud-reasoning';
      node.innerHTML='<label for="conversation-effort">思考 <output id="conversation-effort-label"></output></label><input id="conversation-effort" type="range" min="0" step="1" aria-label="当前对话云模型思考深度"><button id="conversation-effort-follow" type="button" class="reasoning-follow" title="恢复当前连接默认档位">跟随连接</button>';
      local.after(node);
    }
    const cap=current(o);node.hidden=!cap;if(!cap)return;
    const slider=node.querySelector('input'),output=node.querySelector('output'),follow=node.querySelector('button');
    let choice=normalize(selected(o));
    if(choice!=='inherit'&&!cap.levels.includes(choice)){
      o.reasoning_effort='inherit';choice='inherit';
      toast('原思考档位不在当前连接的支持范围，已改为跟随连接；排队中的消息保持原参数');
      handle(()=>saveOptions({reasoning_effort:'inherit'}))();
    }
    const effort=choice==='inherit'?cap.defaultEffort:choice;
    slider.max=String(cap.levels.length-1);slider.value=String(cap.levels.indexOf(effort));slider.disabled=cap.levels.length<2||pending.has(keyFor(o));
    const describe=level=>level==='off'&&!cap.deepseek?'服务默认':LABEL[level]||level;
    output.textContent=choice==='inherit'?'跟随 · '+describe(effort):describe(effort);
    slider.setAttribute('aria-valuetext',output.textContent);
    node.title='按服务商已声明档位设置；更深思考可能增加等待与费用，不能保证更准确。只影响之后发送的消息。';
    follow.classList.toggle('active',choice==='inherit');follow.disabled=pending.has(keyFor(o));
    slider.oninput=()=>{const text=describe(cap.levels[Number(slider.value)]);output.textContent=text;slider.setAttribute('aria-valuetext',text);};
    const commit=async value=>{
      const key=keyFor(o);pending.set(key,value);controls();
      const effort=value==='inherit'?cap.defaultEffort:value;
      try{await saveOptions({reasoning_effort:value,think:effort!=='off'});}
      finally{pending.delete(key);controls();}
    };
    slider.onchange=handle(()=>commit(cap.levels[Number(slider.value)]));
    follow.onclick=handle(()=>commit('inherit'));
  }
  function providerForm(provider) {
    const anchor=document.querySelector('#provider-thinking');if(!anchor)return;
    anchor.closest('label').insertAdjacentHTML('afterend','<section id="provider-reasoning" class="provider-reasoning"><label class="check-label"><input id="provider-reasoning-supported" type="checkbox">服务商明确支持思考档位</label><div id="provider-effort-levels" class="reasoning-levels"></div><label>连接默认思考深度<select id="provider-effort"></select></label><p class="settings-help">通用接口仅勾选服务商文档明确支持的档位；不确定时关闭声明。DeepSeek预设为关闭、低、高、最高。聊天输入区可覆盖，等待更久不代表一定更准确。</p></section>');
    const area=document.querySelector('#provider-reasoning'),supported=area.querySelector('#provider-reasoning-supported'),levels=area.querySelector('#provider-effort-levels'),select=area.querySelector('#provider-effort');
    const original=(provider.reasoning_effort_levels||[]).map(normalize);
    levels.innerHTML=ORDER.map(level=>`<label class="check-label"><input type="checkbox" data-effort-level="${level}" ${original.includes(level)?'checked':''}><span data-effort-text="${level}">${LABEL[level]}</span></label>`).join('');
    supported.checked=provider.reasoning_effort_supported===true;
    let lastMode=anchor.value,defaultValue=normalize(provider.reasoning_effort||'high');
    const refresh=()=>{
      const deepseek=anchor.value==='deepseek',cloud=document.querySelector('#provider-kind').value!=='ollama';area.hidden=!cloud;
      const describe=level=>level==='off'&&!deepseek?'服务默认':LABEL[level];levels.querySelector('[data-effort-text="off"]').textContent=describe('off');
      if(anchor.value!==lastMode){defaultValue='high';lastMode=anchor.value;for(const box of levels.querySelectorAll('input'))box.checked=false;supported.checked=deepseek;}
      if(deepseek)supported.checked=true;supported.disabled=deepseek;
      for(const box of levels.querySelectorAll('input')){if(deepseek)box.checked=DEEPSEEK.includes(box.dataset.effortLevel);box.disabled=deepseek||!supported.checked;}
      const declared=deepseek?DEEPSEEK:[...levels.querySelectorAll('input:checked')].map(box=>box.dataset.effortLevel);
      const items=supported.checked?declared:ORDER;
      if(!items.includes(defaultValue)&&items.length)defaultValue=items.includes('high')?'high':items[0];
      select.innerHTML=items.map(level=>`<option value="${level}">${describe(level)}</option>`).join('');select.value=defaultValue;select.disabled=!supported.checked;
    };
    supported.onchange=refresh;levels.onchange=refresh;select.onchange=()=>{defaultValue=select.value;};
    anchor.addEventListener('change',refresh);document.querySelector('#provider-kind').addEventListener('change',refresh);refresh();
  }
  function providerPatch() {
    if(document.querySelector('#provider-kind').value==='ollama')return {};
    const deepseek=document.querySelector('#provider-thinking').value==='deepseek';
    const supported=deepseek||document.querySelector('#provider-reasoning-supported').checked;
    const levels=deepseek?DEEPSEEK:[...document.querySelectorAll('#provider-effort-levels input:checked')].map(box=>box.dataset.effortLevel);
    if(supported&&!levels.length)throw new Error('请至少声明一个服务商支持的思考档位');
    const effort=document.querySelector('#provider-effort').value||'high';
    return {reasoning_effort:effort,reasoning_effort_supported:supported,...(supported?{reasoning_effort_levels:levels}:{})};
  }
  return Object.freeze({profile,current,requestOptions,controls,providerForm,providerPatch});
})();
