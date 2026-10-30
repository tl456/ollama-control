"use strict";
window.WorkbenchDownloads = (() => {
  function render(jobs) {
    const visible=(jobs||[]).filter(j=>!j.dismissed&&!j.dismissed_at);
    return (visible.some(j=>j.done)?'<div class="page-actions download-history-actions"><button class="button small" data-dismiss-finished>清除已结束记录</button><small>关闭记录保留已下载分层，可稍后重新下载。</small></div>':'')+visible.map(j=>`<article class="download-job" data-download-id="${h(j.id)}"><div class="flex-row"><b>${h(j.model)}</b><span class="tag">${h(j.status||'等待')}</span>${j.done?`<button class="button small download-dismiss" data-dismiss-download="${h(j.id)}" aria-label="关闭 ${h(j.model)} 下载记录" title="关闭记录，保留模型分层">× 关闭提示</button>`:`<button class="button small" data-download-cancel="${h(j.id)}">取消下载</button>`}</div>${!j.done?`<div class="progress"><i style="width:${j.total?Math.min(100,j.completed/j.total*100):0}%"></i></div>`:''}<p class="subtle">${fmtBytes(j.completed||0)} / ${fmtBytes(j.total||0)}${!j.done?' · '+fmtBytes(j.speed||0)+'/s':''}${j.error?' · '+h(j.error):''}</p></article>`).join('');
  }
  async function act(button) {
    if(button.dataset.dismissDownload)await api('/api/downloads/'+encodeURIComponent(button.dataset.dismissDownload)+'/dismiss','POST',{});
    else if(button.hasAttribute('data-dismiss-finished'))await api('/api/downloads/dismiss-finished','POST',{});
    else if(button.dataset.downloadCancel)await api('/api/downloads/'+encodeURIComponent(button.dataset.downloadCancel)+'/cancel','POST',{});
    else return;
    if(state.view==='models')await refreshDownloads();
    if(state.view==='knowledge')await window.Workbench.renderKnowledge();
  }
  document.addEventListener('click',e=>{const b=e.target.closest('[data-dismiss-download],[data-dismiss-finished],[data-download-cancel]');if(b){b.disabled=true;handle(async()=>{try{await act(b);}finally{b.disabled=false;}})();}});
  return Object.freeze({render});
})();
