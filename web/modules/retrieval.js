"use strict";
window.WorkbenchRetrieval = (() => {
  const records=new Map();let sequence=0;
  const statusLabel=value=>({extracted:'关键词已提取',fallback:'已回退原问题',disabled:'原问题检索',original:'原问题检索'}[value]||'索引检索');
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function redact(value) {
    if(Array.isArray(value))return value.map(redact);
    if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,/api.?key|authorization|password|bearer|secret|cookie/i.test(k)||/^(token|access_token|refresh_token)$/i.test(k)?'[已隐藏]':redact(v)]));
    return value;
  }
  function card(result,id) {
    const ident=String(id||'retrieval-'+(++sequence));records.set(ident,redact(result));
    while(records.size>200)records.delete(records.keys().next().value);
    const items=result.items||[],seconds=result.elapsed_seconds??result.seconds;
    return `<button type="button" class="retrieval-summary-card" data-retrieval-record="${escape(ident)}"><span class="retrieval-symbol">▤</span><span><b>知识库检索 · ${items.length?'命中 '+items.length+' 个片段':'未找到匹配片段'}</b><small>${seconds!=null?'用时 '+Number(seconds).toFixed(2)+' 秒 · ':''}${result.query_info?.fallback?'已回退原问题 · ':''}点击核对检索词与来源</small></span><span>查看详情 ↗</span></button>`;
  }
  function open(id) {
    const result=records.get(String(id));if(!result)return;
    const q=result.query_info||{},items=result.items||[],raw=JSON.stringify(result,null,2);
    modal('本次知识库检索记录',`<div class="retrieval-dialog"><div class="retrieval-metrics"><span>命中 <b>${items.length}</b></span><span>耗时 <b>${Number(result.elapsed_seconds??result.seconds??0).toFixed(2)} 秒</b></span><span>方式 <b>${escape(statusLabel(q.status))}</b></span></div><div class="retrieval-tabs" role="tablist" aria-label="检索记录内容"><button role="tab" aria-selected="true" data-retrieval-tab="process">检索过程</button><button role="tab" aria-selected="false" data-retrieval-tab="sources">命中资料</button><button role="tab" aria-selected="false" data-retrieval-tab="raw">原始信息</button></div><section data-retrieval-pane="process"><dl class="retrieval-query"><dt>原始问题</dt><dd>${escape(q.original||result.original||result.query||'记录未提供')}</dd><dt>实际检索问句</dt><dd>${escape(q.query||result.query||'记录未提供')}</dd><dt>提取关键词</dt><dd><div class="keyword-chips">${(q.keywords||[]).map(k=>`<span>${escape(k)}</span>`).join('')||'未提供独立关键词'}</div></dd><dt>提取模型 / 状态</dt><dd>${escape(q.model||'未调用提取模型')} · ${escape(statusLabel(q.status))}${q.fallback?' · 已回退原问题':''}</dd></dl>${q.error?`<p class="notice">${escape(q.error)}</p>`:''}${(result.warnings||[]).map(w=>`<p class="notice">${escape(w)}</p>`).join('')}<p class="field-note">片段相关度不是正确率；型号、工况和单位仍需结合原文核对。关闭窗口不会删除记录或停止回答。</p></section><section data-retrieval-pane="sources" hidden>${items.map((item,i)=>`<article class="retrieval-source-card"><div class="page-actions"><span class="tag">片段 ${i+1}</span><span class="subtle">相关度 ${Number(item.score||0).toFixed(3)}</span></div><h3>${escape(item.source||'未命名文档')}</h3><p class="field-note">${escape(item.kb||'')} · ${escape((item.title_path||item.location?.heading_path||[]).join?.(' › ')||item.location?.heading_path||'未提供标题路径')}<br>片段 ${escape(item.location?.ordinal??'—')} · 字符 ${escape(item.location?.start??'—')}–${escape(item.location?.end??'—')}</p>${item.context?`<p class="source-context">${escape(item.context)}</p>`:''}<pre>${escape(item.text)}</pre></article>`).join('')||'<div class="inline-empty">没有命中片段。请检查索引状态、知识库选择及问题中的型号或关键词。</div>'}</section><section data-retrieval-pane="raw" hidden><div class="page-actions"><button id="copy-retrieval-json" class="button">复制脱敏JSON</button></div><pre class="raw-json">${escape(raw)}</pre></section></div>`);
    document.querySelector('#modal').classList.add('wide-dialog');
    document.querySelector('#modal').addEventListener('close',()=>document.querySelector('#modal').classList.remove('wide-dialog'),{once:true});
    document.querySelectorAll('[data-retrieval-tab]').forEach(button=>button.onclick=()=>{document.querySelectorAll('[data-retrieval-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));document.querySelectorAll('[data-retrieval-pane]').forEach(p=>p.hidden=p.dataset.retrievalPane!==button.dataset.retrievalTab);});
    document.querySelector('#copy-retrieval-json').onclick=()=>copy(raw);
  }
  document.addEventListener('click',e=>{const button=e.target.closest('[data-retrieval-record]');if(button)open(button.dataset.retrievalRecord);});
  return Object.freeze({card,open,redact});
})();
