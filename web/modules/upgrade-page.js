"use strict";
// Presentation for the 1.5 capabilities; persistence belongs to their APIs.
window.WorkbenchV15 = (() => {
  const actualLocal=()=>state.actualLocal!==false;
  const manage=()=>state.canManage??state.localAccess;
  const choice=(id,title,value,items,note)=>`<label>${title}<select id="${id}">${items.map(([v,t])=>`<option value="${v}" ${v===value?'selected':''}>${t}</option>`).join('')}</select><p class="settings-help">${note}</p></label>`;
  function boot() {
    window.WorkbenchLayout.boot();
    if(manage()&&!actualLocal()&&!document.querySelector('#remote-full-notice')){const note=document.createElement('span');note.id='remote-full-notice';note.className='remote-full-notice';note.textContent='远程完整控制 · 可操作宿主机';note.title='文件、Python、COM和管理操作在宿主机执行；仅可信设备使用。';document.querySelector('.top-actions').prepend(note);}
    for(const name of ['tools','memory','settings'])document.querySelector(`[data-view="${name}"]`)?.toggleAttribute('hidden',!manage());
  }
  function controls() {window.WorkbenchReasoning.controls();}
  function providerForm(provider) {window.WorkbenchReasoning.providerForm(provider);}
  function settings(anchor) {
    window.WorkbenchLayout.settings(anchor);
    const c=state.cfg;
    const card=document.createElement('section');card.className='settings-section card';
    card.innerHTML=`<h2>设备偏好与积累控制</h2><div class="form-grid">${choice('setting-inference-device','聊天推理设备',c.inference_device||'auto',[['auto','自动 · GPU / 混合 / CPU'],['cpu','仅CPU']],'按本轮快照应用；自动由Ollama安排，资源错误不反复降级。保存后实际分配仍以模型动态状态为准。')}${choice('setting-embedding-device','知识库嵌入设备',c.embedding_device||'cpu',[['cpu','CPU · 默认稳定'],['gpu','优先GPU · 必要时CPU兜底']],'GPU嵌入与GPU聊天排队共用显卡，可能交替加载。失败最多一次CPU兜底并显示原因。')}<label>活动对话上限<input id="setting-chat-limit" type="number" min="1" max="500" value="${c.chat_limit??30}"><p class="settings-help">默认30个未放入回收站的对话。满额阻止新增、导入和恢复，提示自行移入回收站；已有对话仍可继续。不会自动挑选旧对话删除。</p></label><label>统一日志保留 · 天<input id="setting-log-retention" type="number" min="1" max="90" value="${c.log_retention_days??7}"><p class="settings-help">默认7天，仍受大小轮转保护。回收站不限数量，从移入时间起默认保留30天；只清软件自管数据。</p></label></div><p class="notice">聊天端口11437、嵌入端口11438；显卡实际分配、GPU兜底原因和后台状态可在下方诊断查看。设备切换有活动任务时会明确提示。</p>`;anchor.before(card);
    const net=document.querySelector('#setting-web-bind')?.closest('section');
    if(net){
      const label=document.createElement('label');label.innerHTML=`<span class="check-label"><input id="setting-remote-full" type="checkbox" ${c.remote_full_access?'checked':''} ${actualLocal()?'':'disabled'}>允许远程完整使用工作台</span><p class="settings-help">默认关闭，只能在宿主机授权。密码可选；设置密码后所有入口统一登录。完整模式可操作宿主文件、COM、Python及管理功能，只向可信设备开放。${actualLocal()?'':'请在宿主机修改授权或访问密码。'}</p>`;net.querySelector('.form-grid').append(label);
      net.querySelector('.network-note').textContent='共享同一个工作空间，不是多用户隔离。远程完整模式可搜索、读取宿主机文件，运行Python/COM并变更资料和设置；选择云模型时资料可能外发。仅可信设备使用，HTTP不加密，建议HTTPS/VPN，避免直接公网端口映射。';
      for(const id of ['set-web-password']){const button=document.getElementById(id);if(button&&!actualLocal())button.disabled=true;}
      const status=document.createElement('div');status.id='gateway-open-summary';status.className='notice gateway-open-summary';
      const bind=c.gateway_bind==='0.0.0.0'?'所有网卡 · 请使用主机局域网IP':'仅本机 127.0.0.1';
      status.innerHTML=`<b>模型共享网关 · ${c.gateway_enabled?'已启用':'未启用'}</b><p>监听范围：${h(bind)}<br>开放端口：<strong>${h(c.gateway_port??11436)}</strong><br>原生API：http://主机IP:${h(c.gateway_port??11436)}<br>OpenAI兼容：http://主机IP:${h(c.gateway_port??11436)}/v1<br>鉴权：${c.gateway_require_key?'要求Bearer API密钥':'未要求API密钥，请仅在可信网络使用'}</p><small>远程工作台界面使用8765，模型API使用共享网关端口，分别控制。宿主聊天11437和嵌入11438默认只监听本机。</small>`;net.append(status);
    }
    const diag=document.createElement('section');diag.className='settings-section card';diag.innerHTML='<div class="page-actions"><h2>诊断 · 服务与模型状态</h2><button id="refresh-diagnostics" class="button">刷新诊断</button><button id="copy-diagnostics" class="button">复制脱敏信息</button></div><p class="field-note">显示服务实际状态和采样时间。刷新沿用监测缓存与退避，不强制重复探测显卡。</p><div id="diagnostics-content" aria-live="polite">点击刷新查看当前状态。</div>';anchor.before(diag);
    let last=null;
    const refresh=async()=>{
      const button=document.querySelector('#refresh-diagnostics');if(button)button.disabled=true;
      try{
        last=window.WorkbenchRetrieval.redact(await api('/api/diagnostics'));
        const node=document.querySelector('#diagnostics-content');if(!node)return;
        const models=last.models?.models||last.models||last.available_models||[],loaded=last.loaded||[],hw=last.hardware||{},ports=last.ports||{web:8765,gateway:11436,chat:11437,embedding:11438},service=last.ollama||{},embedding=last.embedding||{};
        node.innerHTML=`<div class="diagnostic-grid"><div><span>软件版本</span><b>${h(last.version||state.cfg.version)}</b></div><div><span>Ollama版本 / 归属</span><b>${h(last.ollama_version||'未连接')} / ${h(service.ownership==='managed'?'本工作台管理':service.ownership==='external'?'外部服务':'未运行')}</b></div><div><span>物理内存余量</span><b>${fmtBytes(hw.ram_available)}</b></div><div><span>系统提交余量</span><b>${fmtBytes(hw.commit_available)}</b></div><div><span>后台队列 / 索引任务</span><b>${last.queue?.total??0} / ${last.index_tasks?.total??0}</b></div><div><span>采样时间</span><b>${h(last.sampled_at||last.checked_at||'由服务提供')}</b></div></div><div class="diagnostic-grid">${Object.entries(ports).map(([role,port])=>`<div><span>${h({web:'工作台Web',gateway:'模型共享网关',chat:'聊天Ollama',embedding:'独立嵌入'}[role]||role)}</span><b>${h(port)}</b></div>`).join('')}</div><p class="field-note">聊天偏好：${h(last.device_preferences?.inference_device==='cpu'?'仅CPU':'自动')} · 嵌入偏好：${h(last.device_preferences?.embedding_device==='gpu'?'优先GPU':'CPU')} · 监测：${h(hw.gpu_monitor_status||'未提供')}</p>${embedding.fallback_reason||embedding.error?`<p class="notice">嵌入状态：${h(embedding.fallback_reason||embedding.error)}</p>`:''}<h3>可用模型 · ${Array.isArray(models)?models.length:0}</h3><div class="diagnostic-models">${Array.isArray(models)?models.map(m=>`<span class="tag">${h(typeof m==='string'?m:m.name||m.model)}</span>`).join(''):''}</div><h3>实际加载 · ${loaded.length}</h3><div class="table-wrap"><table class="data-table"><thead><tr><th>模型</th><th>占用 / 显存分配</th><th>上下文</th></tr></thead><tbody>${loaded.map(m=>`<tr><td>${h(m.name||m.model)}</td><td>${fmtBytes(m.size)} / ${fmtBytes(m.size_vram)}</td><td>${fmtK(m.context_length)}</td></tr>`).join('')||'<tr><td colspan="3">当前没有已加载模型，发送时按需加载。</td></tr>'}</tbody></table></div><details><summary>完整脱敏诊断 · 端口、设备、队列与错误</summary><pre>${h(JSON.stringify(last,null,2))}</pre></details>`;
      }finally{if(button)button.disabled=false;}
    };
    diag.querySelector('#refresh-diagnostics').onclick=handle(refresh);diag.querySelector('#copy-diagnostics').onclick=handle(async()=>{if(!last)await refresh();await copy(JSON.stringify(last,null,2));});
  }
  function settingsPatch(){return {chat_limit:Number(document.querySelector('#setting-chat-limit').value),log_retention_days:Number(document.querySelector('#setting-log-retention').value),inference_device:document.querySelector('#setting-inference-device').value,embedding_device:document.querySelector('#setting-embedding-device').value,...(actualLocal()?{remote_full_access:document.querySelector('#setting-remote-full')?.checked||false}:{})};}
  async function ragSettings(el) {
    const c=state.cfg,defaults=await api('/api/rag-query/defaults');if(!el.isConnected||!document.querySelector('#save-rag-defaults'))return;
    const card=document.createElement('section');card.className='card settings-section';card.innerHTML=`<h2>检索前关键词提取</h2><label><span class="check-label"><input id="rag-query-enabled" type="checkbox" ${c.rag_query_enabled!==false?'checked':''}>原始RAG先提取关键词与检索问句</span><p class="settings-help">独立的一次短模型调用。保留型号、数字、单位与工况；失败回退原问题，不反复调用。传统JSON工具协议与此设置独立。</p></label><label>关键词提取系统提示词<textarea id="rag-query-prompt" rows="7">${h(c.rag_query_prompt||defaults.prompt||'')}</textarea></label><div class="page-actions"><button id="restore-rag-prompt" class="button">恢复默认提示词</button><button id="save-rag-query" class="button primary">保存检索提示词</button></div><label>提取测试问题<input id="rag-extract-question" maxlength="2000" placeholder="例如：查找DEMO-A01的Z向10Hz刚度条件"></label><button id="test-rag-extraction" class="button">只测试提取关键词</button><div id="rag-extraction-result" aria-live="polite"></div>`;
    document.querySelector('#save-rag-defaults').closest('section').after(card);
    const head=document.createElement('label');head.innerHTML=`<span class="check-label"><input id="rag-markdown-headers" type="checkbox" ${c.rag_markdown_headers!==false?'checked':''}>先按Markdown标题分节 · 默认开启</span><p class="settings-help">按h1–h6标题，再按段落和Token分段；保留标题路径。修改默认只影响新库，已有库需配置后重建。</p>`;document.querySelector('#rag-default-overlap').closest('.form-grid').append(head);
    const oldSave=document.querySelector('#save-rag-defaults'),newSave=oldSave.cloneNode(true);oldSave.replaceWith(newSave);
    newSave.onclick=handle(async()=>{state.cfg=await api('/api/settings','PUT',{rag_enabled:document.querySelector('#rag-global-enabled').checked,embedding_model:document.querySelector('#rag-default-model').value.trim(),rag_top_k:Number(document.querySelector('#rag-default-topk').value),rag_chunk_tokens:tokenValue('#rag-default-chunk'),rag_overlap_tokens:tokenValue('#rag-default-overlap'),rag_markdown_headers:document.querySelector('#rag-markdown-headers').checked});toast('知识库默认设置已保存，已有库按各自配置使用');renderControls();});
    document.querySelector('#restore-rag-prompt').onclick=()=>{document.querySelector('#rag-query-prompt').value=defaults.prompt||'';toast('已填入默认提示词，保存后生效');};
    document.querySelector('#save-rag-query').onclick=handle(async()=>{state.cfg=await api('/api/settings','PUT',{rag_query_enabled:document.querySelector('#rag-query-enabled').checked,rag_query_prompt:document.querySelector('#rag-query-prompt').value});toast('关键词提取设置已保存');});
    document.querySelector('#test-rag-extraction').onclick=handle(async()=>{const button=document.querySelector('#test-rag-extraction'),query=document.querySelector('#rag-extract-question').value.trim();if(!query)throw new Error('请填写测试问题');button.disabled=true;try{const r=await api('/api/rag-query/test','POST',{query,prompt:document.querySelector('#rag-query-prompt').value,provider:options().provider,model:options().model});const q=r.query_info||r;document.querySelector('#rag-extraction-result').innerHTML=`<div class="notice"><b>${h(({extracted:'关键词已提取',fallback:'已回退原问题',disabled:'原问题检索'}[q.status])||'提取完成')}</b><p>${h(q.query||query)}</p><div class="keyword-chips">${(q.keywords||[]).map(k=>`<span>${h(k)}</span>`).join('')}</div>${q.error?`<p>${h(q.error)}</p>`:''}<details><summary>原始提取结果</summary><pre>${h(JSON.stringify(window.WorkbenchRetrieval.redact(r),null,2))}</pre></details></div>`;}finally{button.disabled=false;}});
  }
  async function mdEditor(kb,doc=null) {
    const source=doc?await api('/api/knowledge/'+kb+'/documents/'+doc+'/source'):{name:'新建资料.md',text:'',editable:true};
    if(!source.editable)throw new Error('此资料不是可编辑MD，请另存为知识库MD副本后编辑');
    modal(doc?'编辑知识库Markdown':'新建知识库Markdown',`<div class="md-editor-dialog"><label>文档名称<input id="kb-md-name" value="${h(source.name)}" maxlength="160"></label><div class="md-editor-columns"><label>Markdown正文<textarea id="kb-md-text" aria-label="知识库Markdown正文" spellcheck="false">${h(source.text)}</textarea></label><section><h3>阅读预览</h3><article id="kb-md-preview" class="message-content"></article></section></div><div class="md-editor-save"><span id="kb-md-save-state">保存文件后异步更新索引；处理中保留旧完整索引。</span><button id="save-kb-md" class="button primary">保存并异步索引</button></div></div>`);
    document.querySelector('#modal').classList.add('wide-dialog');document.querySelector('#modal').addEventListener('close',()=>document.querySelector('#modal').classList.remove('wide-dialog'),{once:true});
    const editor=document.querySelector('#kb-md-text'),preview=document.querySelector('#kb-md-preview'),nameInput=document.querySelector('#kb-md-name'),note=document.querySelector('#kb-md-save-state'),button=document.querySelector('#save-kb-md');
    const render=()=>{preview.innerHTML=markdown(editor.value);window.enhanceWorkMarkdown?.(preview,state.cfg.mermaid_enabled!==false).catch(()=>{});};render();let timer;
    editor.oninput=()=>{clearTimeout(timer);timer=setTimeout(render,150);note.textContent='有未保存修改；Ctrl+S保存后异步更新索引。';};
    const save=async()=>{if(button.disabled)return;const name=nameInput.value.trim(),text=editor.value;if(!name)throw new Error('请填写MD文档名称');button.disabled=true;
      try{await api('/api/knowledge/'+kb+'/documents'+(doc?'/'+doc+'/source':'/md'),doc?'PUT':'POST',{name,text});toast('MD已保存，索引已提交后台处理');closeModal();await window.Workbench.renderKnowledge();}
      catch(error){note.className='error-note';note.textContent=error.message||'保存失败，正文草稿保留。';throw error;}
      finally{button.disabled=false;}
    };
    button.onclick=handle(save);editor.onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();handle(save)();}};
  }
  function mountDocuments(kb,docs) {
    if(!manage())return;
    const input=document.querySelector('#kb-files');if(input){input.insertAdjacentHTML('beforebegin','<div class="page-actions"><button id="new-kb-md" class="button primary">＋ 新建Markdown</button><button id="import-kb-md" class="button">导入可编辑MD</button><input id="editable-md-file" type="file" accept=".md,.markdown,text/markdown,text/plain" hidden></div>');document.querySelector('#new-kb-md').onclick=handle(()=>mdEditor(kb));document.querySelector('#import-kb-md').onclick=()=>document.querySelector('#editable-md-file').click();document.querySelector('#editable-md-file').onchange=handle(async e=>{const file=e.target.files[0];e.target.value='';if(!file)return;if(file.size>2*1024*1024)throw new Error('可编辑MD请控制在2MiB内，较大资料可走普通导入');let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer()).replace(/^\uFEFF/,'');}catch{throw new Error('MD不是可读的UTF-8文本，请先另存为UTF-8；未导入乱码内容');}await api('/api/knowledge/'+kb+'/documents/md','POST',{name:file.name,text});toast('MD副本已导入，索引异步处理');await window.Workbench.renderKnowledge();});}
    const rows=document.querySelectorAll('#view-knowledge tbody tr');
    docs.items.forEach((doc,i)=>{if(!doc.editable&&!/\.(md|markdown)$/i.test(doc.name||''))return;const cell=rows[i]?.lastElementChild;if(!cell)return;cell.insertAdjacentHTML('afterbegin',`<button class="button small" data-edit-kb-md="${h(doc.id)}" data-edit-kb="${h(kb)}">编辑 / 重命名</button>`);cell.querySelector('[data-edit-kb-md]').onclick=handle(e=>{e.stopPropagation();return mdEditor(kb,doc.id);});});
  }
  function libraryConfig(kb,id,afterSave) {
    const area=document.querySelector('#kb-overlap')?.closest('.form-grid');if(!area)return;
    area.insertAdjacentHTML('beforeend',`<label><span class="check-label"><input id="kb-markdown-headers" type="checkbox" ${kb.config.markdown_headers!==false?'checked':''}>先按Markdown标题分节</span><p class="settings-help">保留标题路径后再按Token分段；改变此设置须重建当前知识库。</p></label>`);
    const old=document.querySelector('#save-kb'),button=old.cloneNode(true);old.replaceWith(button);
    button.onclick=handle(async()=>{const body={name:document.querySelector('#kb-name').value,category:document.querySelector('#kb-edit-category').value,config:{...kb.config,model:document.querySelector('#kb-model').value.trim(),chunk_tokens:tokenValue('#kb-chunk'),overlap_tokens:tokenValue('#kb-overlap'),markdown_headers:document.querySelector('#kb-markdown-headers').checked}};const result=await api('/api/knowledge'+(id?'/'+id:''),id?'PATCH':'POST',body);await afterSave(result);});
  }
  return Object.freeze({boot,controls,settings,settingsPatch,providerForm,ragSettings,mdEditor,mountDocuments,libraryConfig,toolsHelp:()=>window.WorkbenchFileBrowser.toolsHelp(),directoryBrowser:(tool,initial)=>window.WorkbenchFileBrowser.open(tool,initial)});
})();
