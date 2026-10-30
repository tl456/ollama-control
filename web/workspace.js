"use strict";
// Non-versioned feature coordinator; leaf pages and shared presentation live in modules/.
window.Workbench = (() => {
  const ui = { follow: true, fresh: false, kbPage: 1, kbSearch: "", kbCategory: "", kbDetail: null,
    docPage: 1, activityTab: "queue", activityPage: 1, accessIP: "", accessPath: "",
    kbTab: 'documents', kbTaskPage: 1, turnQueues: {}, queueLoading: {}, queueLoaded: {}, skills: [], settingsDirty: false, submitReceipts: {} };
  const statusNames = { empty: "尚无索引", ready: "可以检索", needs_rebuild: "需要重建", queued: "等待中",
    running: "正在运行", processing: "解析中", embedding: "建立索引中", done: "已完成", failed: "失败",
    cancelled: "已取消", interrupted: "重启中断", waiting_user: "等待用户回答" };
  const status = value => `<span class="status-pill ${h(value)}">${h(statusNames[value] || value)}</span>`;
  const field = (id, title, value, note, min = 0, max = 100000) => `<label>${title}<input id="${id}" type="number" min="${min}" max="${max}" value="${h(value)}"><p class="settings-help">${note}</p></label>`;
  const check = (id, title, value, note) => `<label><span class="check-label"><input id="${id}" type="checkbox" ${value ? "checked" : ""}>${title}</span><p class="settings-help">${note}</p></label>`;
  const pager = (page, total, size, key) => `<div class="pager"><button data-${key}="${Math.max(1,page-1)}" ${page<=1?"disabled":""}>上一页</button><span>${page} / ${Math.max(1,Math.ceil(total/size))} · ${total} 项</span><button data-${key}="${page+1}" ${page*size>=total?"disabled":""}>下一页</button></div>`;

  function syncCurrent() {
    const entry = state.streams[state.conv?.id];
    state.busy = !!entry;
    liveMessage = entry?.message || null;
    if (liveMessage && state.conv) {
      const index = state.conv.messages.findIndex(m=>m.id===liveMessage.id);
      if(index>=0)state.conv.messages[index]=liveMessage;
      else if(!liveMessage.parent||state.conv.messages.some(m=>m.id===liveMessage.parent))state.conv.messages.push(liveMessage);
      // A queue snapshot can know the assistant before this window has its new
      // user parent. Never make that disconnected node the visible branch root.
      if(state.conv.messages.some(m=>m.id===liveMessage.id)){state.conv.leaf=liveMessage.id;if(state.conv.message_page&&!state.conv.message_page.around)state.conv.message_page.window_leaf=liveMessage.id;}
    }
    const waiting=(ui.turnQueues[state.conv?.id]?.items||[]).filter(t=>['queued','paused'].includes(t.status)).length;
    $('#send').textContent=state.busy||waiting?'↑ 加入队列':'↑ 发送';$('#send').classList.remove('stopping');
    $('#send').title='发送当前输入；若本对话正在回答则加入待发队列，不停止上一轮';
    $('#send').setAttribute('aria-label','发送消息或加入本对话待发队列');
    if($('#stop-current'))$('#stop-current').hidden=!state.busy;
    $('#live-speed').textContent=entry?.phase || 'Enter 发送 · Shift Enter 换行';
  }
  function following(){const el=$('#messages');return ui.follow&&el.scrollHeight-el.scrollTop-el.clientHeight<160;}
  function scrollState(){const el=$('#messages');ui.follow=el.scrollHeight-el.scrollTop-el.clientHeight<80;if(ui.follow)ui.fresh=false;const b=$('#jump-bottom');b.hidden=ui.follow;b.classList.toggle('new-content',ui.fresh);b.textContent=ui.fresh?'有新内容 · 回到底部 ↓':'回到底部 ↓';}
  function newContent(id){if(state.conv?.id!==id)return;if(!following()){ui.fresh=true;$('#jump-bottom').hidden=false;$('#jump-bottom').classList.add('new-content');$('#jump-bottom').textContent='有新内容 · 回到底部 ↓';}}

  async function login({resume=false}={}){
    if(ui.loginActive)return;
    ui.loginActive=true;
    const content=`<div class="login-card"><img src="/assets/app-icon.svg" class="login-logo" alt="本地智能工作台"><label>访问密码<input id="login-password" type="password" autocomplete="current-password"></label><label class="check-label"><input id="login-remember" type="checkbox">记住此设备的登录状态（30 天）</label><p class="session-note">${resume?'当前输入和附件会保留，登录后请确认再继续操作。':'记住的是可撤销的登录会话，不保存明文密码。'}共享界面使用同一个工作空间，只向可信设备开放。</p><button id="login-submit" class="button primary">登录</button><p id="login-error" class="error-note" hidden></p></div>`;
    const dialog=$('#modal'),holder=$('#modal-content');
    const saved=resume&&dialog.open?{nodes:[...holder.childNodes],title:$('#modal-title').textContent,classes:dialog.className,top:holder.scrollTop}:null;
    if(saved)holder.replaceChildren(); // Keep actual editor nodes/listeners, not serialized drafts.
    modal('登录本地工作台',content);
    let active=true;
    const finish=()=>{
      if(!active)return;active=false;ui.loginActive=false;dialog.removeEventListener('close',closed);
      if(saved){$('#modal-title').textContent=saved.title;holder.replaceChildren(...saved.nodes);dialog.className=saved.classes;holder.scrollTop=saved.top;if(!dialog.open)dialog.showModal();}
      else if(dialog.open)closeModal();
    };
    const closed=()=>finish();dialog.addEventListener('close',closed,{once:true});
    const find=selector=>dialog.querySelector(selector);
    const submit=async()=>{const b=find('#login-submit');b.disabled=true;
      try{
        const r=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:find('#login-password').value,remember:find('#login-remember').checked})});
        if(!r.ok)throw new Error((await r.json()).detail||'登录失败');
        if(resume){await reconnectSession({manual:true});finish();toast('登录已恢复，当前输入和附件已保留');}
        else{finish();await bootstrap();}
      }catch(e){const note=find('#login-error');if(note){note.hidden=false;note.textContent=e.message;}}
      finally{b.disabled=false;}
    };
    find('#login-submit').onclick=submit;find('#login-password').onkeydown=e=>{if(e.key==='Enter')submit();};find('#login-password').focus();
  }

  async function boot(result){
    $('.brand-mark').innerHTML='<img src="/assets/app-icon.svg" alt="工作台图标">';
    for(const value of [102400,204800])if(![...$('#context-select').options].some(x=>Number(x.value)===value)){
      const option=new Option(fmtK(value),String(value));const before=[...$('#context-select').options].find(x=>Number(x.value)>value);$('#context-select').add(option,before||null);
    }
    if(!$('#stop-current'))$('#send').insertAdjacentHTML('beforebegin','<button id="stop-current" class="button danger stop-current" hidden title="停止当前这一轮；后续待发消息将暂停">■ 停止回答</button>');
    $('#stop-current').onclick=handle(stopCurrent);
    if(!$('#turn-queue'))$('.composer-area').insertAdjacentHTML('afterbegin','<section id="turn-queue" class="turn-queue" aria-label="本对话待发队列" hidden></section><div id="skill-tray" class="skill-tray"></div>');
    $('#attach-button').title='添加附件或使用技能';$('#attach-button').setAttribute('aria-label','添加附件或使用技能');$('#attach-button').onclick=addMenu;
    try{ui.skills=(await api('/api/skills')).items||[];}catch{ui.skills=[];}
    setupSidebar();
    if(!state.localAccess){for(const v of ['tools','memory','settings'])$(`[data-view="${v}"]`)?.setAttribute('hidden','');}
    const select=$('#context-select');if(![...select.options].some(x=>x.value==='custom'))select.add(new Option('自定义…','custom'));
    select.onchange=handle(async()=>{if(select.value==='custom'){modal('自定义上下文',`<p class="field-note">总上下文包括输入、历史、记忆、工具、附件和回答。模型上限不等于本机可运行容量。</p>${field('custom-context','总上下文 · K',options().context/1024,'1K = 1024 Token，例如 12K = 12288 Token；允许超过模型声明值；实际能否接受由模型与服务端决定。',2,2147483647/1024)}<button id="apply-context" class="button primary">应用到当前会话</button>`);$('#apply-context').onclick=handle(async()=>{const value=Math.round(Number($('#custom-context').value)*1024);if(!Number.isInteger(value)||value<2048||value>2147483647)throw new Error('请输入2K以上的有效容量');await saveOptions({context:value});closeModal();});return;}await saveOptions({context:Number(select.value)});});
    if(!ui.booted)$('#messages').addEventListener('scroll',scrollState,{passive:true});ui.booted=true;
    $('#jump-bottom').onclick=()=>{ui.follow=true;ui.fresh=false;$('#messages').scrollTop=$('#messages').scrollHeight;scrollState();};
    document.querySelector('#file-input').removeAttribute('accept');
    const apiPage=$('#view-api');if(!$('#gateway-tutorial'))apiPage.insertAdjacentHTML('beforeend',`<section id="gateway-tutorial" class="api-docs"><h2>局域网共享与借用其他 Ollama</h2><p>本机聊天端口11437、嵌入端口11438。共享网关默认使用11436，提供鉴权、排队和 IP 日志。只记录经过网关的调用，不会替外部客户端执行本机工具。</p><pre class="share-example">其他电脑原生连接：http://本机局域网IP:11436\nOpenAI 兼容 接口根地址：http://本机局域网IP:11436/v1\nAuthorization: Bearer 你生成的API密钥</pre><p>先生成密钥，再打开共享，并仅允许可信网络通过 Windows 防火墙。不会自动配置防火墙和路由器。聊天11437和嵌入11438仍仅本机，直接连接它的调用不会出现在网关日志。</p><p>借用其他电脑：设置 → 添加连接 → 局域网 Ollama，填写对方原生地址或受控网关地址。消息、附件、记忆及所选检索片段会发给该主机。HTTP 不加密，不应直接暴露到公网。</p></section>`);
    addCopyButtons(apiPage);window.WorkbenchV15.boot();window.WorkbenchV16.boot(result);controls();syncCurrent();if(state.conv)await loadQueue(state.conv.id,true);
  }

  function controls(){
    const o=options(),p=state.cfg.providers?.find(x=>x.id===o.provider),lan=p?.kind==='ollama';
    $('#privacy-badge').textContent=o.provider==='local'?'● 本机模式 · 默认不外发':`${lan?'▦ 局域网':'☁ 云模型'} · ${p?.name||o.provider} · 消息/资料将外发`;
    for(const option of $('#provider-select').options){const item=state.cfg.providers?.find(p=>p.id===option.value);if(item)option.textContent=(item.kind==='ollama'?'▦ ':'☁ ')+item.name;}
    for(const option of $('#context-select').options)if(option.value==='custom')option.disabled=false;
    const current=Object.values(state.streams).length;
    $('#knowledge-bar').innerHTML=`<label title="勾选后使用已选择的知识库"><input id="rag-enabled" type="checkbox" ${o.rag_enabled?'checked':''} ${!state.cfg.rag_enabled?'disabled':''}>知识库 RAG</label><button id="rag-choose" class="chip">选择知识库 · ${(o.rag_ids||[]).length} 个</button><select id="rag-mode" aria-label="知识库检索方式"><option value="original">自动检索 · 原始模式</option><option value="call" >按需检索 · 原生调用</option></select><button class="chip" id="queue-open">任务 ${current?`· ${current} 个对话处理中`:''}</button><button class="chip fixed-memory" id="memory-open">◈ ${state.cfg.memory_enabled?(state.cfg.memory_ai_write?'记忆 · AI 维护':'记忆 · 只读'):'记忆已关闭'}</button>`;
    $('#rag-mode').value=o.rag_mode||'original';
    $('#rag-choose').onclick=handle(chooseLibraries);
    $('#rag-enabled').onchange=handle(async()=>{if($('#rag-enabled').checked&&!(o.rag_ids||[]).length){await chooseLibraries();return;}await saveOptions({rag_enabled:$('#rag-enabled').checked});});
    $('#rag-mode').onchange=handle(async()=>{await saveOptions({rag_mode:$('#rag-mode').value});});
    $('#memory-open').onclick=()=>state.localAccess?showView('memory'):toast('长期记忆由宿主机管理；共享界面不会获得记忆写入工具');
    $('#queue-open').onclick=()=>showView('activity');
    document.querySelectorAll('[data-open-conv]').forEach(b=>b.classList.toggle('working',!!state.streams[b.dataset.openConv]));
    renderSkills();renderQueue();compactToolToggles();window.WorkbenchV15.controls();window.WorkbenchV16.controls();
    if(state.conv&&Date.now()-(ui.queueLoaded[state.conv.id]||0)>3000)loadQueue(state.conv.id).catch(()=>{});
  }

  async function chooseLibraries(){
    const selected=new Set(options().rag_ids||[]);let page=1;
    modal('选择用于当前对话的知识库',`<p class="field-note">最多 8 个。默认原始 RAG 会在每次提问前检索；现代 Call 则由模型决定是否查询。检索资料也会随当前连接发往局域网或云端。</p><input id="picker-search" placeholder="搜索知识库名称"><div class="kb-picker" id="kb-picker"></div><div id="picker-pager"></div><p id="picker-count" class="field-note"></p><button id="apply-kbs" class="button primary">应用并启用知识库</button>`);
    const load=async()=>{const result=await api('/api/knowledge?'+new URLSearchParams({page,search:$('#picker-search').value}));if(!$('#kb-picker'))return;page=result.page;$('#kb-picker').innerHTML=result.items.map(k=>`<label class="kb-choice"><input type="checkbox" data-pick-kb="${h(k.id)}" ${selected.has(k.id)?'checked':''} ${!k.enabled?'disabled':''}><span><b>${h(k.name)}</b><small>${h(k.category||'未分类')} · ${k.document_count} 份文档 · ${statusNames[k.status]||k.status}<br>${h(k.config.model)}</small></span></label>`).join('')||'<div class="inline-empty">没有知识库。请先到“知识库 RAG”栏目新建并添加资料。</div>';$('#picker-pager').innerHTML=pager(page,result.total,20,'picker-page');$('#picker-count').textContent=`已选择 ${selected.size} 个`;$('#kb-picker').onchange=e=>{const id=e.target.dataset.pickKb;if(!id)return;if(e.target.checked&&selected.size>=8){e.target.checked=false;toast('最多选择 8 个');return;}e.target.checked?selected.add(id):selected.delete(id);$('#picker-count').textContent=`已选择 ${selected.size} 个`;};$('#picker-pager').onclick=handle(async e=>{const b=e.target.closest('[data-picker-page]');if(b){page=Number(b.dataset.pickerPage);await load();}});};
    let timer;$('#picker-search').oninput=()=>{clearTimeout(timer);timer=setTimeout(handle(async()=>{page=1;await load();}),250);};
    $('#apply-kbs').onclick=handle(async()=>{await saveOptions({rag_ids:[...selected],rag_enabled:selected.size>0});closeModal();});await load();
  }

  async function renderKnowledge(){
    $('#view-knowledge').onclick=null;
    if(ui.kbTab!=='documents'){await renderKnowledgeManagement();return;}
    if(ui.kbDetail){await renderLibrary(ui.kbDetail);return;}
    const result=await api('/api/knowledge?'+new URLSearchParams({page:ui.kbPage,search:ui.kbSearch,category:ui.kbCategory}));ui.kbPage=result.page;
    $('#view-knowledge').innerHTML=`<div class="page-heading"><div><div class="eyebrow">知识资料管理</div><h1>让资料成为可检索的知识。</h1><p>先建立索引，再在对话中勾选使用。默认 0.6B、每次取 2 段、0.50K 分段、0.0625K 重叠。</p></div>${state.localAccess?'<button id="new-kb" class="button primary">＋ 新建知识库</button>':''}</div><div class="notice">嵌入模型负责查找相关片段，聊天模型负责回答；RAG 不是完整读取、全文核算或准确性保证。型号、单位、工况和结论仍需复核。CPU 小批量建库，不与聊天争抢显卡。</div><div class="filter-row"><input id="kb-search" placeholder="搜索知识库" value="${h(ui.kbSearch)}"><select id="kb-category"><option value="">全部分类</option>${result.categories.map(c=>`<option ${c===ui.kbCategory?'selected':''}>${h(c)}</option>`).join('')}</select></div><div class="kb-grid">${result.items.map(k=>`<article class="card kb-card"><div class="page-actions"><span class="tag">${h(k.category||'未分类')}</span>${status(k.status)}${!k.enabled?'<span class="tag">已停用</span>':''}</div><h2>${h(k.name)}</h2><p class="subtle">${h(k.config.model)}${k.active_config.model&&k.active_config.model!==k.config.model?`<br>当前完整索引仍用 ${h(k.active_config.model)}`:''}</p><div class="kb-meta"><div>文档<b>${k.document_count}</b></div><div>当前片段<b>${k.chunk_count}</b></div></div>${k.error?`<p class="error-note">${h(k.error)}</p>`:''}<div class="page-actions"><button class="button small primary" data-open-kb="${h(k.id)}">查看资料</button>${state.localAccess?`<button class="button small" data-edit-kb="${h(k.id)}">配置</button><button class="button small" data-toggle-kb="${h(k.id)}" data-enabled="${k.enabled?'1':'0'}">${k.enabled?'停用':'启用'}</button>`:''}</div></article>`).join('')||'<div class="card inline-empty">还没有知识库。新建后添加设备手册、工艺说明等资料。</div>'}</div>${pager(result.page,result.total,20,'kb-page')}`;
    mountKnowledgeTabs();if($('#new-kb'))$('#new-kb').onclick=()=>editLibrary();
    let timer;$('#kb-search').oninput=()=>{ui.kbSearch=$('#kb-search').value;clearTimeout(timer);timer=setTimeout(handle(()=>{ui.kbPage=1;return renderKnowledge();}),300);};
    $('#kb-category').onchange=handle(()=>{ui.kbCategory=$('#kb-category').value;ui.kbPage=1;return renderKnowledge();});restoreScroll();
  }

  async function editLibrary(id){
    const kb=id?await api('/api/knowledge/'+id):{name:'',category:'',config:{model:state.cfg.embedding_model,top_k:state.cfg.rag_top_k,chunk_tokens:state.cfg.rag_chunk_tokens,overlap_tokens:state.cfg.rag_overlap_tokens}};
    modal(id?'配置知识库':'新建知识库',`<div class="modal-form"><label>名称<input id="kb-name" value="${h(kb.name)}" placeholder="例如：设备维护资料"></label><label>分类<input id="kb-edit-category" value="${h(kb.category)}" placeholder="例如：机械 / 工艺 / 检验"></label><label>嵌入模型<input id="kb-model" value="${h(kb.config.model)}" list="embedding-model-options"><datalist id="embedding-model-options"><option value="qwen3-embedding:0.6b"><option value="qwen3-embedding:4b"><option value="qwen3-embedding:8b"></datalist><p class="settings-help">0.6B 默认，资源开销较小；4B/8B 需要另行下载且消耗更多内存。更大不保证你的资料检索必然更好。不要把聊天 9B/4B 填作嵌入模型。</p></label><div class="form-grid">${field('kb-chunk','分段 · Token',kb.config.chunk_tokens,'每个正文片段的容量，来源信息另占少量空间。',128,2048)}${field('kb-overlap','重叠 · Token',kb.config.overlap_tokens,'保留相邻片段间的联系，必须小于分段。',0,512)}</div><p class="notice">更换模型或分段设置必须重建。重建完成前仍使用旧的完整索引及旧嵌入模型；不会混合新旧向量。</p><button id="save-kb" class="button primary">${id?'保存配置（需要时手动重建）':'创建知识库'}</button></div>`);
    capacityInput('kb-chunk');capacityInput('kb-overlap');
    $('#save-kb').onclick=handle(async()=>{const body={name:$('#kb-name').value,category:$('#kb-edit-category').value,config:{...kb.config,model:$('#kb-model').value.trim(),chunk_tokens:tokenValue('#kb-chunk'),overlap_tokens:tokenValue('#kb-overlap')}};const result=await api('/api/knowledge'+(id?'/'+id:''),id?'PATCH':'POST',body);closeModal();ui.kbDetail=result.id;ui.docPage=1;await renderKnowledge();});
    window.WorkbenchV15.libraryConfig(kb,id,async result=>{closeModal();ui.kbDetail=result.id;ui.docPage=1;await renderKnowledge();});
  }

  async function renderLibrary(id){
    const [kb,docs,builds]=await Promise.all([api('/api/knowledge/'+id),api('/api/knowledge/'+id+'/documents?page='+ui.docPage),api('/api/knowledge/tasks?kb='+encodeURIComponent(id))]);
    const active=builds.items.find(x=>['queued','processing','embedding'].includes(x.status));
    $('#view-knowledge').innerHTML=`<div class="page-heading"><div><button class="button small" id="back-kbs">← 知识库列表</button><h1>${h(kb.name)}</h1><p>${h(kb.category||'未分类')} · ${h(kb.config.model)} · ${kb.chunk_count} 个可检索片段</p></div>${state.localAccess?`<div class="page-actions"><button class="button" data-edit-kb="${h(id)}">配置</button><button id="rebuild-kb" class="button primary">重建索引</button><button id="cancel-kb" class="button">取消处理</button><button id="delete-kb" class="button danger">删除库</button></div>`:''}</div>${active?`<div class="notice">${h(statusNames[active.status])} · 已完成 ${active.completed}/${active.total||'…'} 个片段<div class="progress"><i style="width:${active.total?Math.min(100,active.completed/active.total*100):0}%"></i></div></div>`:`<div class="notice">${h(statusNames[kb.status])}${kb.error?' · '+h(kb.error):''}。添加/删除文档后异步更新；索引失败不会冒充成功。</div>`}${state.localAccess?'<div class="card"><h2>添加资料</h2><p class="field-note">支持 Office 文字、TXT/MD、DAT 等可识别文本、图片 OCR。原文件不会被修改；解析和建库在后台完成。</p><input id="kb-files" type="file" multiple><div id="kb-upload-status" class="kb-upload-status"></div></div>':''}<h2>文档 · ${docs.total} 份</h2><div class="table-wrap"><table class="data-table"><thead><tr><th>资料</th><th>提取文字</th><th>操作</th></tr></thead><tbody>${docs.items.map(d=>`<tr><td>${h(d.name)}</td><td>${d.chars.toLocaleString()} 字符</td><td><button class="button small" data-view-kb-doc="${h(d.id)}" data-source-kb="${h(id)}">预览</button>${state.localAccess?`<button class="button small danger" data-delete-doc="${h(d.id)}" data-kb="${h(id)}">移除</button>`:''}</td></tr>`).join('')||'<tr><td colspan="3">尚未加入资料。</td></tr>'}</tbody></table></div>${pager(docs.page,docs.total,20,'doc-page')}<p class="field-note">删除只移除本库记录和索引；附件副本由引用及保留期管理，外部原件不动。</p>`;
    mountKnowledgeTabs();window.WorkbenchV15.mountDocuments(id,docs);$('#back-kbs').onclick=handle(async()=>{ui.kbDetail=null;await renderKnowledge();});
    $('#back-kbs').insertAdjacentHTML('afterend',`<button id="test-this-kb" class="button small">测试检索效果</button>`);$('#test-this-kb').onclick=handle(async()=>{ui.testKb=id;ui.kbTab='settings';await renderKnowledge();});
    if(state.localAccess){$('#rebuild-kb').onclick=handle(async()=>{await api('/api/knowledge/'+id+'/rebuild','POST',{});toast('已排队建立索引');await renderLibrary(id);});$('#cancel-kb').onclick=handle(async()=>{await api('/api/knowledge/'+id+'/cancel','POST',{});toast('正在取消后台任务');});$('#delete-kb').onclick=handle(async()=>{const name=await textPrompt('删除知识库','仅删除本软件中的库、文档引用和索引，不删除外部原文件。输入知识库名称确认。');if(name==null)return;await api('/api/knowledge/'+id,'DELETE',{confirm:name});ui.kbDetail=null;await renderKnowledge();});$('#kb-files').onchange=handle(async e=>{const files=[...e.target.files];e.target.value='';ui.uploading=true;try{for(const file of files){const progress=$('#kb-upload-status');if(progress)progress.textContent='正在上传与提取：'+file.name;const form=new FormData();form.append('file',file);let meta=await api('/api/attachments?read_mode='+encodeURIComponent(state.cfg.file_read_mode),'POST',form);for(let i=0;meta.status==='processing'&&i<180;i++){await wait(600);meta=await api('/api/attachments/'+meta.id);}if(meta.status!=='ready')throw new Error(meta.error||'解析尚未完成，请稍后重试');await api('/api/knowledge/'+id+'/documents','POST',{attachment_id:meta.id});}toast('资料已加入，索引在后台处理');}finally{ui.uploading=false;}if(state.view==='knowledge'&&ui.kbDetail===id)await renderLibrary(id);});}
  }

  function mountKnowledgeTabs(){
    $('#view-knowledge').insertAdjacentHTML('afterbegin',`<div class="knowledge-nav tab-row" role="tablist" aria-label="知识库管理区域">${[['documents','资料管理'],['maintenance','处理与维护'],['settings','检索设置与测试'],['models','嵌入模型']].map(([id,name])=>`<button role="tab" aria-selected="${ui.kbTab===id}" class="button ${ui.kbTab===id?'active':''}" data-knowledge-tab="${id}">${name}</button>`).join('')}</div>`);
    $('#view-knowledge .knowledge-nav').onclick=handle(async e=>{const tab=e.target.closest('[data-knowledge-tab]')?.dataset.knowledgeTab;if(!tab)return;saveScroll();ui.kbTab=tab;await renderKnowledge();$('#view-knowledge').scrollTop=0;});
  }

  async function renderKnowledgeManagement(){
    const el=$('#view-knowledge'),c=state.cfg;el.onclick=null;
    if(ui.kbTab==='maintenance'){
      const result=await api('/api/knowledge/tasks?page='+ui.kbTaskPage);
      el.innerHTML=`<div class="page-heading"><div><div class="eyebrow">异步处理 · 有界记录</div><h1>知识库处理与维护</h1><p>解析、建立索引、删除与重建均在后台完成；失败不覆盖上一次完整索引。</p></div><button id="refresh-kb-tasks" class="button">刷新进度</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>任务 / 知识库</th><th>状态与进度</th><th>说明</th><th>操作</th></tr></thead><tbody>${result.items.map(t=>`<tr><td>${h(t.id.slice(0,12))}<br>${h(t.kb||'')}</td><td>${status(t.status)}<br>${t.completed}/${t.total||'…'} 个片段</td><td>${h(t.error||t.note||'')}</td><td>${state.localAccess?['queued','processing','embedding','running'].includes(t.status)?`<button class="button small" data-maint-cancel="${h(t.kb)}">取消处理</button>`:['failed','interrupted','cancelled'].includes(t.status)?`<button class="button small" data-maint-retry="${h(t.kb)}">重新建立索引</button>`:'—':'—'}</td></tr>`).join('')||'<tr><td colspan="4">暂无处理任务。添加资料后可在这里查看进度。</td></tr>'}</tbody></table></div>${pager(result.page,result.total,result.page_size,'maintenance-page')}<section class="notice"><b>维护原则</b><p>模型或分段方式变化后请明确重建；索引成功切换前保留旧版本。删除库只清理本软件管理的资料引用与索引，不删除外部原件。历史任务有限保留、分页显示，避免长期积累。</p></section>`;
      $('#refresh-kb-tasks').onclick=handle(renderKnowledge);el.onclick=handle(async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.maintenancePage){ui.kbTaskPage=Number(b.dataset.maintenancePage);await renderKnowledge();}if(b.dataset.maintCancel){await api('/api/knowledge/'+b.dataset.maintCancel+'/cancel','POST',{});toast('正在取消处理');await renderKnowledge();}if(b.dataset.maintRetry){await api('/api/knowledge/'+b.dataset.maintRetry+'/rebuild','POST',{});toast('重建任务已加入后台队列');await renderKnowledge();}});
    }else if(ui.kbTab==='settings'){
      const libraries=await api('/api/knowledge?page=1');if(ui.testKb&&!libraries.items.some(k=>k.id===ui.testKb))try{libraries.items.unshift(await api('/api/knowledge/'+ui.testKb));}catch{ui.testKb=null;}
      el.innerHTML=`<div class="page-heading"><div><div class="eyebrow">知识库 · 独立配置</div><h1>先确认能找到资料，再让模型回答。</h1><p>检索测试只返回来源片段，不调用聊天模型生成答案，便于定位检索与理解问题。</p></div></div>${state.localAccess?`<section class="card settings-section"><h2>默认检索与分段</h2><div class="form-grid">${check('rag-global-enabled','启用知识库能力',c.rag_enabled,'开启能力不等于每段聊天都使用；仍需在对话勾选具体知识库。')}<label>新建库默认嵌入模型<input id="rag-default-model" value="${h(c.embedding_model)}" list="rag-model-options"><datalist id="rag-model-options"><option value="qwen3-embedding:0.6b"><option value="qwen3-embedding:4b"><option value="qwen3-embedding:8b"></datalist><p class="settings-help">默认 0.6B；更换现有库的模型需要在该库配置后重建。默认设置只影响新建库。</p></label>${field('rag-default-topk','每次取几段',c.rag_top_k,'默认 2。更多片段占上下文，不代表更准确。',1,10)}${field('rag-default-chunk','新建库分段 · Token',c.rag_chunk_tokens,'默认 0.50K = 512 Token。保留段落、表头、单位与来源。',128,2048)}${field('rag-default-overlap','新建库重叠 · Token',c.rag_overlap_tokens,'默认 0.0625K = 64 Token，必须小于分段。',0,512)}</div><button id="save-rag-defaults" class="button primary">保存知识库设置</button><p class="field-note">对话默认原始模式自动检索；原生按需检索由模型决定。刚度核算和全文取数宜用附件读取 + Python，不能把检索到几段当作读取全文。</p></section>`:''}<section class="card settings-section"><h2>检索效果测试</h2><div class="filter-row"><input id="rag-test-search" placeholder="按名称查找知识库"><button id="rag-test-find" class="button">查找</button></div><label>测试知识库<select id="rag-test-kb">${libraries.items.map(k=>`<option value="${h(k.id)}" ${ui.testKb===k.id?'selected':''}>${h(k.name)} · ${h(statusNames[k.status]||k.status)}</option>`).join('')}</select></label><p class="field-note">列表每次显示前 20 个匹配项；也可从资料详情进入“测试检索效果”。</p><label>问题<input id="rag-test-query" placeholder="例如：设备停机后需要检查哪些部位？" maxlength="2000"></label><button id="rag-test-run" class="button primary">查找相关片段</button><div id="rag-test-results" aria-live="polite"></div></section>`;
      for(const id of ['rag-default-chunk','rag-default-overlap'])capacityInput(id);
      $('#save-rag-defaults')?.addEventListener('click',handle(async()=>{state.cfg=await api('/api/settings','PUT',{rag_enabled:$('#rag-global-enabled').checked,embedding_model:$('#rag-default-model').value.trim(),rag_top_k:Number($('#rag-default-topk').value),rag_chunk_tokens:tokenValue('#rag-default-chunk'),rag_overlap_tokens:tokenValue('#rag-default-overlap')});toast('知识库默认设置已保存；现有库不静默更换模型');renderControls();}));
      $('#rag-test-find').onclick=handle(async()=>{const rows=await api('/api/knowledge?search='+encodeURIComponent($('#rag-test-search').value));$('#rag-test-kb').innerHTML=rows.items.map(k=>`<option value="${h(k.id)}">${h(k.name)}</option>`).join('');});
      $('#rag-test-run').onclick=handle(async()=>{const button=$('#rag-test-run'),id=$('#rag-test-kb').value,query=$('#rag-test-query').value.trim();if(!id)throw new Error('请先建立并选择一个知识库');if(!query)throw new Error('请填写要查找的问题');button.disabled=true;$('#rag-test-results').textContent='正在检索，首次可能需要加载 CPU 嵌入模型…';try{const r=await api('/api/knowledge-search','POST',{query,ids:[id]});$('#rag-test-results').innerHTML=(r.warnings||[]).map(w=>`<p class="notice">${h(w)}</p>`).join('')+(r.items||[]).map((item,i)=>`<details class="retrieval-event" open><summary>${i+1}. ${h(item.source)} · 相关评分 ${num(item.score,3)}</summary><p class="field-note">${h(item.kb)} · 片段 ${h(item.location?.ordinal)} · 字符 ${h(item.location?.start)}–${h(item.location?.end)}</p><pre>${h(item.text)}</pre></details>`).join('')+`<p class="field-note">${h(r.note||'未找到相关片段时，检查资料是否完成索引及问题是否明确。')} · 相关评分不是正确率。</p>`;}finally{button.disabled=false;}});
    }else{
      const [models,jobs]=await Promise.all([api('/api/models?provider=local'),state.localAccess?api('/api/downloads'):Promise.resolve([])]);
      const presets=[['qwen3-embedding:0.6b','默认 · 轻量检索','已包含在完整离线包；CPU 小批量建立索引，适合日常资料检索。'],['qwen3-embedding:4b','可选 · 更大嵌入模型','需要另行下载和更多内存；不同资料表现需用检索测试比较。'],['qwen3-embedding:8b','可选 · 更高资源需求','体积与建库资源开销更大，不保证检索一定更准确。']];
      el.innerHTML=`<div class="page-heading"><div><div class="eyebrow">嵌入模型 · 下载与状态</div><h1>查找资料的小模型，不是聊天模型。</h1><p>切换嵌入模型后必须重建对应知识库，不能混用新旧向量。不会静默下载大模型。</p></div><button id="refresh-embedding-models" class="button">刷新状态</button></div><div class="kb-grid">${presets.map(([id,title,note])=>{const installed=(models.models||[]).find(m=>m.name===id),job=jobs.find(j=>j.model===id&&!j.done);return `<article class="card kb-card"><span class="tag">${h(title)}</span><h2>${h(id)}</h2><p>${h(note)}</p><p>${installed?'✓ 已安装 · '+fmtBytes(installed.size):job?'正在下载':'尚未安装'}</p>${state.localAccess&&!installed&&!job?`<button class="button" data-embedding-download="${h(id)}">下载此模型</button>`:''}${installed?`<button class="button small" data-show-model="${h(id)}">查看能力与元数据</button>`:''}</article>`;}).join('')}</div><div id="embedding-download-jobs">${jobs.filter(j=>j.model.startsWith('qwen3-embedding:')).map(j=>`<div class="download-job"><div class="page-actions"><b>${h(j.model)}</b><span>${h(j.status||'等待')}</span>${!j.done?`<button class="button small" data-embedding-cancel="${h(j.id)}">取消下载</button>`:''}</div><div class="progress"><i style="width:${j.total?Math.min(100,j.completed/j.total*100):0}%"></i></div><p class="subtle">${fmtBytes(j.completed)} / ${fmtBytes(j.total)} · ${fmtBytes(j.speed)}/s${j.error?' · '+h(j.error):''}</p></div>`).join('')}</div><div class="notice">模型下载跟随下载代理设置；保留官方下载清单和校验逻辑。更换默认模型不会自动重建或删除现有库，需明确选择操作。</div>`;
      $('#refresh-embedding-models').onclick=handle(renderKnowledge);el.onclick=handle(async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.embeddingDownload){await api('/api/downloads','POST',{model:b.dataset.embeddingDownload});toast('嵌入模型已加入下载');await renderKnowledge();}if(b.dataset.embeddingCancel){await api('/api/downloads/'+b.dataset.embeddingCancel+'/cancel','POST',{});await renderKnowledge();}});
    }
    mountKnowledgeTabs();
    if(ui.kbTab==='settings'&&state.localAccess)await window.WorkbenchV15.ragSettings(el);
    if(ui.kbTab==='models'&&state.localAccess){const jobs=await api('/api/downloads');const panel=$('#embedding-download-jobs');if(panel)panel.innerHTML=window.WorkbenchDownloads.render(jobs.filter(j=>j.model.startsWith('qwen3-embedding:')));}
  }

  async function textPrompt(title,note){return new Promise(resolve=>{modal(title,`<p class="field-note">${h(note)}</p><input id="text-prompt-value"><div class="page-actions"><button id="text-prompt-cancel" class="button">取消</button><button id="text-prompt-apply" class="button danger">确认</button></div>`);let done=false;const end=value=>{if(done)return;done=true;$('#modal').removeEventListener('close',cancel);closeModal();resolve(value);};const cancel=()=>end(null);$('#text-prompt-cancel').onclick=cancel;$('#text-prompt-apply').onclick=()=>end($('#text-prompt-value').value);$('#modal').addEventListener('close',cancel,{once:true});});}

  async function renderMemory(){
    return window.WorkbenchMemoryPage.render({state,api,$,h,markdown,fmtK,toast,handle,confirmDialog,restoreScroll});
  }

  async function renderActivity(){
    const tab=ui.activityTab;
    if(['audit','snapshots'].includes(tab))return window.WorkbenchV16.activity(tab,ui.activityPage);
    let result,html='';
    if(tab==='access'&&state.localAccess){result=await api('/api/access?'+new URLSearchParams({page:ui.activityPage,ip:ui.accessIP,path:ui.accessPath}));html=`<div class="filter-row"><input id="access-ip" placeholder="筛选客户端 IP" value="${h(ui.accessIP)}"><input id="access-path" placeholder="筛选接口，例如 /api/chat" value="${h(ui.accessPath)}"><button id="filter-access" class="button">筛选</button><button id="clear-access" class="button danger">清空日志</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>时间 / 来源 IP</th><th>接口 / 模型</th><th>结果</th><th>耗时</th><th>输入 / 输出</th></tr></thead><tbody>${result.items.map(r=>`<tr><td>${h(new Date(r.time*1000).toLocaleString())}<br>${h(r.ip)}</td><td>${h(r.path)}<br>${h(r.model)}</td><td>${r.status}</td><td>${num(r.seconds)} s</td><td>${fmtK(r.input_tokens)} / ${fmtK(r.output_tokens)}</td></tr>`).join('')||'<tr><td colspan="5">暂无经过共享网关的调用。</td></tr>'}</tbody></table></div>`;
    }else if(tab==='builds'){result=await api('/api/knowledge/tasks?page='+ui.activityPage);html=`<div class="table-wrap"><table class="data-table"><thead><tr><th>任务</th><th>状态</th><th>片段</th><th>说明</th></tr></thead><tbody>${result.items.map(r=>`<tr><td>${h(r.id.slice(0,12))}</td><td>${status(r.status)}</td><td>${r.completed}/${r.total}</td><td>${h(r.error||'')}</td></tr>`).join('')||'<tr><td colspan="4">暂无建立索引任务。</td></tr>'}</tbody></table></div>`;
    }else{result=await api('/api/jobs?page='+ui.activityPage);html=`<div class="table-wrap"><table class="data-table"><thead><tr><th>模型 / 任务</th><th>状态</th><th>队列位置</th><th>操作</th></tr></thead><tbody>${result.items.map(r=>`<tr><td>${h(r.label)}<br><span class="subtle">${h(r.kind)}</span></td><td>${status(r.status)}</td><td>${r.position||'—'}</td><td>${['queued','running'].includes(r.status)?`<button class="button small danger" data-cancel-job="${h(r.id)}">取消</button>`:'—'}</td></tr>`).join('')||'<tr><td colspan="4">暂无任务。新对话可以排队，不会打断正在生成的回答。</td></tr>'}</tbody></table></div>`;}
    $('#view-activity').innerHTML=`<div class="page-heading"><div><div class="eyebrow">任务与有界日志</div><h1>任务与访问日志</h1><p>同一后端默认串行，可以取消等待中的任务；不会为了新请求打断旧回答。</p></div></div><div class="tab-row"><button class="button ${tab==='queue'?'active':''}" data-activity-tab="queue">推理队列</button><button class="button ${tab==='builds'?'active':''}" data-activity-tab="builds">知识库处理</button>${state.localAccess?`<button class="button ${tab==='access'?'active':''}" data-activity-tab="access">API 来源 IP 日志</button><button class="button" data-activity-tab="audit">工具审计</button><button class="button" data-activity-tab="snapshots">文件快照</button>`:''}</div>${html}${pager(result.page,result.total,result.page_size,'activity-page')}<p class="field-note">队列保留有限任务摘要；访问日志默认最多 ${state.cfg.access_retention_days} 天 / ${state.cfg.access_retention_count} 条。不保存提示词、附件正文、密码或 API Key。来源 IP 取连接本身，不信任任意转发头；绕过网关的原生请求不可见。</p>`;
    if($('#filter-access')){$('#filter-access').onclick=handle(async()=>{ui.accessIP=$('#access-ip').value;ui.accessPath=$('#access-path').value;ui.activityPage=1;await renderActivity();});$('#clear-access').onclick=handle(async()=>{if(await confirmDialog('清空访问日志','仅清除网关调用元数据，不影响模型和聊天。','清空')){await api('/api/access','DELETE');await renderActivity();}});}restoreScroll();
  }

  function settings(){
    if(!state.localAccess){$('#view-settings').innerHTML='<p class="readonly-shared">共享界面不提供宿主机设置。</p>';return;}
    const c=state.cfg,anchor=$('#view-settings .danger-zone');
    const old=$('#setting-context'),input=document.createElement('input');input.id=old.id;input.type='number';input.min='2048';input.max='2147483647';input.value=c.context;old.replaceWith(input);
    input.insertAdjacentHTML('afterend','<p class="settings-help">容量单位统一为 K，1K = 1024 Token。100K/200K 预设分别为 102400/204800 Token；256K 是理论窗口，实际输入还需扣除回答、记忆与工具。</p>');
    const label=$('#setting-recovery').parentElement;for(const node of label.childNodes)if(node.nodeType===3)node.textContent='仅在未输出且建连失败时，按原配置重连一次（默认关闭）';label.nextElementSibling.textContent='资源/驱动异常不重试，不自动切混合或 CPU。分配失败进入冷却；请检查系统“已提交”而不只看剩余显存。';
    const card=document.createElement('section');card.className='settings-section card';card.innerHTML=`<h2>稳定性与资源保护</h2><div class="form-grid">${check('setting-gpu-monitor','可选 NVIDIA 实时监测',c.gpu_monitor,'新安装 / 出厂默认开启。最多每 30 秒查询；3 秒超时，失败退避 5 分钟，连续两次失败暂停本次运行。监测不触发模型重载或自动降级。')}${check('setting-large-context','允许手动使用超过 32K 的上下文',c.allow_large_context,'新安装 / 出厂默认勾选，聊天仍默认 8K。允许超过模型声明值，仍检查内存余量，不保证硬件可承受。')}${field('setting-queue-limit','每后端队列上限',c.queue_limit,'一次执行一个任务，超出队列上限明确拒绝；不自动打断。',1,100)}</div><p class="field-note">增大 Windows 分页文件可能缓解系统提交额度不足，但不能增加显存，也不能保证长上下文可运行。软件不会修改你的分页文件或驱动。</p>`;anchor.before(card);window.WorkbenchResourceUI.settings(card,{api,$,state,toast,handle});
    const mem=document.createElement('section');mem.className='settings-section card';mem.innerHTML=`<h2>固定能力 · Markdown 长期记忆</h2><div class="form-grid">${check('setting-memory-enabled','启用长期记忆',c.memory_enabled,'默认开启。把限额内的长期偏好与明确事实提供给模型。')}${check('setting-memory-ai','允许 AI 自动维护',c.memory_ai_write,'默认开启。AI 可自由完整改写、整理、删除或清空这一份 MD，无须逐次确认，不依赖 Python。增量只是建议；全文覆盖按最后成功保存生效。')}${field('setting-memory-kb','记忆文件上限 · KiB',c.memory_max_kb,'限制长期积累；不自动保存密钥、附件全文和全部聊天。',1,256)}${field('setting-memory-tokens','保存及注入硬上限 · Token',c.memory_tokens,'含注入包装，默认1K；人工/AI保存超额会拒绝，保留原文件并提示精简。',128,4096)}</div><button class="button" data-view="memory">查看 / 人工编辑记忆</button>`;anchor.before(mem);
    const rag=document.createElement('section');rag.className='settings-section card';rag.innerHTML='<h2>知识库 · 独立管理</h2><p class="field-note">资料、处理维护、检索设置、嵌入模型下载已集中到知识库管理区，不在通用设置重复配置。</p><button id="open-rag-settings" class="button">打开知识库设置与检索测试</button>';anchor.before(rag);$('#open-rag-settings').onclick=()=>{ui.kbDetail=null;ui.kbTab='settings';showView('knowledge');};
    const net=document.createElement('section');net.className='settings-section card';net.innerHTML=`<h2>网络开放、密码与共享 API</h2><div class="form-grid"><label>Web 界面监听<select id="setting-web-bind"><option value="127.0.0.1">仅本机 · 默认</option><option value="0.0.0.0">所有网卡 · 允许其他设备访问</option></select><p class="settings-help">改变 Web 监听后需退出并重启软件。不会自动打开 Windows 防火墙或路由器。</p></label>${check('setting-gateway-enabled','启用独立模型共享入口',c.gateway_enabled,'只共享推理，不给外部客户端执行本机文件/代码工具的权限。')}<label>共享入口监听<select id="setting-gateway-bind"><option value="127.0.0.1">仅本机</option><option value="0.0.0.0">所有网卡 · 局域网</option></select><p class="settings-help">自管聊天11437、嵌入11438；默认共享网关11436，调用经过此入口才有来源 IP 日志。</p></label>${field('setting-gateway-port','共享入口端口',c.gateway_port,'默认11436，不能占用Web8765、聊天11437或嵌入11438。',1024,65535)}${check('setting-gateway-key','共享入口要求 Bearer API Key',c.gateway_require_key,'建议始终开启。先生成密钥，再启用网关；明文仅生成时显示一次。')}${field('setting-log-days','访问日志保留 · 天',c.access_retention_days,'仅存来源 IP、接口、模型、状态、耗时和用量，不存正文。',1,90)}${field('setting-log-count','访问日志最多 · 条',c.access_retention_count,'与天数共同限制积累；任务与访问日志页面可分页查看或清空。',100,100000)}</div><p class="notice network-note">共享界面使用同一个工作空间，不是多用户隔离系统。只向可信设备开放，建议设密码；HTTP 本身不加密，公网使用应另配 HTTPS/VPN，不建议直接端口映射。远程权限按宿主机授权；完整控制可操作宿主机文件、COM、代码及管理功能。</p><div class="page-actions"><button id="set-web-password" class="button">${state.auth?.password_set?'修改 / 关闭访问密码':'设置可选访问密码'}</button><button id="generate-gateway-key" class="button">生成 / 更换 API 密钥</button><button class="button" data-view="activity">访问日志</button><button id="logout-device" class="button">退出当前登录</button></div><p class="field-note" id="gateway-status">${state.auth?.gateway?.running?'共享入口正在监听':state.auth?.gateway?.error||'共享入口尚未启动'}。Web 密码与 API Key 相互独立；记住登录最多 30 天。</p>`;anchor.before(net);$('#setting-web-bind').value=c.web_bind;$('#setting-gateway-bind').value=c.gateway_bind;
    $('#set-web-password').onclick=()=>{modal('访问密码',`<p class="field-note">8–256 字符。留空保存表示关闭访问密码；设置会撤销其他设备的登录。HTTP 局域网传输不加密，请只在可信网络操作。</p><input id="new-web-password" type="password" autocomplete="new-password"><label class="check-label"><input id="remember-own-login" type="checkbox">记住本设备登录状态</label><button id="apply-web-password" class="button primary">保存密码设置</button>`);$('#apply-web-password').onclick=handle(async()=>{const password=$('#new-web-password').value,remember=$('#remember-own-login').checked;await api('/api/auth/password','POST',{password});await api('/api/auth/login','POST',{password,remember});state.auth=await api('/api/auth/status');closeModal();renderSettings();toast(password?'密码已设置，本设备已登录':'访问密码已关闭');});};
    $('#generate-gateway-key').onclick=handle(async()=>{const result=await api('/api/auth/gateway-key','POST',{});state.auth.gateway_key_set=true;modal('API 密钥 · 仅本次显示',`<p class="field-note">旧密钥立即失效，请复制并妥善保存。此密钥不是 Web 登录密码。</p><pre>${h(result.key)}</pre><button class="button primary" data-copy-text="${h(result.key)}">复制密钥</button>`);});
    $('#logout-device').onclick=handle(async()=>{await api('/api/auth/logout','POST',{});try{await reconnectSession({manual:true});}catch(error){if(error.status!==401)throw error;}toast(state.auth?.password_set?'已退出当前登录':'未设置访问密码，无需登录');});
    const about=document.createElement('section');about.className='settings-section card';about.innerHTML=`<h2>关于 · 本地智能工作台 ${h(c.version)}</h2><p class="about-author">作者：tl456</p><p class="field-note">自有代码采用 MIT 许可，允许使用、修改和分发，需保留版权及许可声明。模型、Ollama、Python 与其他组件仍遵循各自许可证，不统一改为 MIT。</p><p class="field-note">预置聊天：Qwen3.5 9B + 4B；默认嵌入：Qwen3-Embedding 0.6B。源码包不包含聊天、企业附件、记忆、知识库、授权目录和密钥。</p><button class="button" data-view="guide">软件 / 工具 / 知识库教程</button>`;anchor.before(about);
    anchor.querySelector('.subtle').textContent='正式版升级默认保留资料，同版本重启也保留。程序覆盖与手动初始化分开；需要清空时请先确认资料，再使用出厂设置。手动初始化清除聊天、附件副本、记忆、知识库、密钥、设置、插件和实验历史；9B、4B、0.6B、运行库及外部原文件保留。';
    serviceSettings(anchor);window.WorkbenchV15.settings(anchor);window.WorkbenchV16.settings(anchor);
    for(const id of ['setting-context','setting-output','setting-memory-tokens'])capacityInput(id);
    const save=$('#save-settings'),bar=document.createElement('div');bar.className='settings-savebar';bar.innerHTML='<div><b id="settings-save-state">设置已载入</b><small>保存后新发送使用新参数；已经排队的任务保留原快照。</small></div>';bar.append(save);$('#view-settings').append(bar);ui.settingsDirty=false;
    $('#view-settings').oninput=e=>{if(e.target.matches('input,select,textarea')){ui.settingsDirty=true;$('#settings-save-state').textContent='有未保存的修改';bar.classList.add('dirty');}};
    $('#view-settings').onchange=$('#view-settings').oninput;
  }

  function settingsPatch(){
    return {...window.WorkbenchV16.settingsPatch(),...window.WorkbenchV15.settingsPatch(),gpu_monitor:$('#setting-gpu-monitor').checked,allow_large_context:$('#setting-large-context').checked,queue_limit:Number($('#setting-queue-limit').value),memory_enabled:$('#setting-memory-enabled').checked,memory_ai_write:$('#setting-memory-ai').checked,memory_max_kb:Number($('#setting-memory-kb').value),memory_tokens:tokenValue('#setting-memory-tokens'),ollama_autostart:$('#setting-ollama-autostart').checked,ollama_stop_on_exit:$('#setting-ollama-stop').checked,ollama_management:$('#setting-ollama-management').value,ollama_keep_alive:Number($('#setting-ollama-keepalive').value),web_bind:$('#setting-web-bind').value,gateway_enabled:$('#setting-gateway-enabled').checked,gateway_bind:$('#setting-gateway-bind').value,gateway_port:Number($('#setting-gateway-port').value),gateway_require_key:$('#setting-gateway-key').checked,access_retention_days:Number($('#setting-log-days').value),access_retention_count:Number($('#setting-log-count').value)};
  }

  function capacityInput(id){const input=$('#'+id);if(!input||input.dataset.tokenK==='true')return;const value=Number(input.value);input.dataset.tokenK='true';input.value=String(value/1024);if(input.min)input.min=String(Number(input.min)/1024);if(input.max)input.max=String(Number(input.max)/1024);input.step=String(1/1024);input.title=`精确值 ${value} Token；1K = 1024 Token`;const label=input.closest('label');if(label)for(const node of label.childNodes)if(node.nodeType===3)node.textContent=node.textContent.replace(/Token/g,'K')+(node.textContent.includes('上下文')&&!node.textContent.includes('K')?' · K':'');input.addEventListener('input',()=>{input.title=`精确值 ${Math.round(Number(input.value)*1024)} Token；1K = 1024 Token`;});}
  function settingsSaved(){ui.settingsDirty=false;const note=$('#settings-save-state');if(note)note.textContent='设置已保存';$('.settings-savebar')?.classList.remove('dirty');}

  function serviceSettings(anchor){
    const c=state.cfg,section=document.createElement('section');section.className='settings-section card';section.innerHTML=`<h2>本机 Ollama · 后台与归属</h2><p id="ollama-service-state" class="notice">正在读取服务归属…</p><div class="form-grid"><label>本机连接方式<select id="setting-ollama-management"><option value="managed">由本工作台管理</option><option value="external">连接已有服务</option></select><p class="settings-help">自管使用后台命令行服务，不启动额外官方桌面托盘。若端口已被外部服务占用，仍只连接，不强行接管。</p></label><label>模型保温<select id="setting-ollama-keepalive"><option value="0">回答后尽快释放</option><option value="300">空闲 5 分钟 · 默认</option><option value="900">空闲 15 分钟</option><option value="-1">持续保留 · 增加占用</option></select><p class="settings-help">控制模型在服务内的保留时间，不代表退出或结束服务；仍受资源保护和后端实际调度影响。</p></label>${check('setting-ollama-autostart','随工作台启动自管服务',c.ollama_autostart!==false,'外部连接模式不会因此启动第二个服务。')}${check('setting-ollama-stop','真正退出时停止自管服务',c.ollama_stop_on_exit!==false,'只停止已验证归属的进程。关闭窗口收至托盘不算退出；已有外部服务保留。')}</div><div class="page-actions"><button id="ollama-start" class="button">启动后台服务</button><button id="ollama-stop" class="button danger">停止自管服务</button><button id="ollama-refresh" class="button">刷新归属</button></div><p class="field-note">本机聊天默认11437、嵌入11438、共享网关11436，分别管理。外部官方托盘可由你手动退出，工作台不会静默修改系统启动项。</p>`;anchor.before(section);$('#setting-ollama-management').value=c.ollama_management||'managed';$('#setting-ollama-keepalive').value=String(c.ollama_keep_alive??300);
    const refresh=async()=>{try{const s=await api('/api/ollama/service');if(!$('#ollama-service-state'))return;$('#ollama-service-state').textContent=(s.ownership==='managed'?'本工作台管理':s.ownership==='external'?'连接已有服务 · 不接管':'尚未运行')+' · '+(s.endpoint||'http://127.0.0.1:11437')+(s.pid?' · 进程 '+s.pid:'');$('#ollama-stop').disabled=s.ownership!=='managed';$('#restart-service').disabled=s.ownership!=='managed';$('#ollama-start').disabled=!!s.online||$('#setting-ollama-management').value==='external';}catch(e){if($('#ollama-service-state'))$('#ollama-service-state').textContent='暂时无法读取服务归属：'+e.message;}};
    $('#ollama-start').onclick=handle(async()=>{await saveSettingsForm();await api('/api/service/start','POST',{});await refresh();toast('已请求启动本工作台后台服务');});$('#ollama-stop').onclick=handle(async()=>{if(await confirmDialog('停止自管 Ollama','仅停止本工作台确认管理的服务；有活动任务时不会强制结束。','停止')){await api('/api/ollama/stop','POST',{});await refresh();}});$('#ollama-refresh').onclick=handle(refresh);$('#setting-ollama-management').addEventListener('change',()=>refresh());refresh();
  }

  function provider(id){
    const p=state.cfg.providers.find(p=>p.id===id)||{id:'provider_'+Date.now(),name:'',base_url:'',models:[],context:32768,vision:false,tools:true,kind:'cloud',thinking_mode:''};
    modal(id?'编辑模型连接':'添加模型连接',`<div class="modal-form"><label>连接类型<select id="provider-kind"><option value="cloud">公网 / OpenAI 兼容云模型</option><option value="ollama">局域网 / 其他原生 Ollama</option></select></label><label>名称<input id="provider-name" value="${h(p.name)}" placeholder="例如：车间推理主机 / DeepSeek"></label><label>接口根地址<input id="provider-url" value="${h(p.base_url)}" placeholder="http://192.168.1.20:11434 或 https://api.deepseek.com/v1"></label><p class="settings-help">原生 Ollama 地址不要加 /api 或 /v1；云服务按服务商文档填 接口根地址。局域网连接不经过下载代理。</p><label>API Key（可选）<input id="provider-key" type="password" autocomplete="off" placeholder="${p.has_key?'留空保留已加密密钥':'服务商密钥 / 网关密钥'}"></label><label>备用模型名 · 逗号或换行分隔<textarea id="provider-models">${h((p.models||[]).join('\n'))}</textarea></label><label>云端协议预设<select id="provider-thinking"><option value="">通用 OpenAI 兼容</option><option value="deepseek">DeepSeek · 思考与工具适配</option></select></label><div class="form-grid">${field('provider-context','云模型声明上下文',p.context||32768,'原生 Ollama 从模型元数据读取；云端需按实际服务填写。',2048,2147483647)}${check('provider-vision','云模型支持图片',p.vision,'仅对确实支持图片的服务开启。')}${check('provider-tools','云模型支持原生工具调用',p.tools,'兼容接口不保证全部特性；原生 Ollama 自动读取能力。')}</div><p class="notice">此连接会接收所发送的消息、附件、记忆和所选知识片段。局域网主机也需要可信；HTTP 没有传输加密。密钥以 Windows DPAPI 保存，换电脑需重填。</p><div class="page-actions"><button id="deepseek-preset" class="button">填入 DeepSeek 预设</button><button id="save-provider-v12" class="button primary">保存连接</button></div></div>`);
    $('#provider-kind').value=p.kind||'cloud';$('#provider-thinking').value=p.thinking_mode||'';window.WorkbenchV15.providerForm(p);
    $('#deepseek-preset').onclick=()=>{$('#provider-kind').value='cloud';$('#provider-name').value='DeepSeek';$('#provider-url').value='https://api.deepseek.com/v1';$('#provider-thinking').value='deepseek';$('#provider-thinking').dispatchEvent(new Event('change'));toast('模型名请按账户可用模型列表或官方文档填写，不写死旧型号');};
    $('#save-provider-v12').onclick=handle(async()=>{const value={id:p.id,name:$('#provider-name').value.trim(),kind:$('#provider-kind').value,base_url:$('#provider-url').value.trim(),api_key:$('#provider-key').value,models:$('#provider-models').value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean),thinking_mode:$('#provider-thinking').value,...window.WorkbenchReasoning.providerPatch(),context:Number($('#provider-context').value),vision:$('#provider-vision').checked,tools:$('#provider-tools').checked};if(!value.name)throw new Error('请填写连接名称');state.cfg=await api('/api/settings','PUT',{providers:[...state.cfg.providers.filter(p=>p.id!==value.id),value]});state.modelCaps=null;state.modelCapsFor=null;closeModal();renderSettings();renderControls();toast('连接已保存；可使用连接测试读取模型列表');});
  }

  async function modelDetail(model){
    const info=await api('/api/models/show','POST',{model}),m=state.status?.models?.find(x=>x.name===model),d=info.details?.details||m?.details||{};
    const cell=(title,value)=>`<div class="capability-cell"><span>${title}</span><b>${h(value)}</b></div>`;
    const declared=info.details?.capabilities;
    modal('模型详情',`<div class="model-detail-hero"><img src="/assets/app-icon.svg" alt=""><div><h3>${h(model)}</h3><span class="tag">${info.embedding?'检索嵌入模型':'聊天模型'}</span> <span class="tag">${h(d.family||'架构未声明')}</span></div></div><div class="capability-grid">${cell('磁盘逻辑大小',fmtBytes(m?.size))}${cell('参数量 / 量化',(d.parameter_size||'未知')+' / '+(d.quantization_level||'未知'))}${cell('模型理论上下文',num(info.max_context/1024,1)+'K'+(info.context_verified?'':' · 未声明保守值'))}${cell('当前会话上下文',num(options().context/1024,1)+'K')}${cell('图片输入',declared?(info.vision?'已声明支持':'未声明支持'):'未知')}${cell('原生工具',declared?(info.tools?'已声明支持':'未声明支持'):'未知')}${cell('思考模式',declared?(info.thinking?'已声明支持':'未声明支持'):'未知')}${cell('嵌入用途',info.embedding?'文本向量 / 知识库':'非嵌入模型')}</div><p class="notice">模型声明的能力不等于本机实测通过；理论最大上下文不等于本机可稳定运行容量。更大上下文会增加资源开销，不自动提高准确性。</p>${info.embedding?'<p class="field-note">在知识库配置里使用此模型。更换嵌入模型后需要重建索引，不能直接混用新旧向量。</p>':''}<details><summary>高级 · 完整元数据与参数</summary><pre>${h(JSON.stringify(info.details||info,null,2))}</pre></details>`);
  }

  function attachmentPreview(item,pending){
    const textarea=$('#attachment-text');if(!textarea)return;
    let offset=0;const size=16000,original=textarea.value;
    textarea.insertAdjacentHTML('beforebegin','<div class="preview-toolbar"><button id="preview-read" class="button small active">阅读视图 · MD</button><button id="preview-edit" class="button small">原始文字 / 编辑</button><button id="preview-prev" class="button small">上一段</button><button id="preview-next" class="button small">下一段</button><span id="preview-range" class="preview-range"></span></div><article id="attachment-rendered" class="message-content attachment-rendered"></article>');
    const render=()=>{const text=textarea.value;$('#attachment-rendered').innerHTML=markdown(text.slice(offset,offset+size));$('#preview-range').textContent=`字符 ${Math.min(offset+1,text.length)}–${Math.min(offset+size,text.length)} / ${text.length}`;$('#preview-prev').disabled=offset===0;$('#preview-next').disabled=offset+size>=text.length;addCopyButtons($('#attachment-rendered'));window.enhanceWorkMarkdown?.($('#attachment-rendered'),state.cfg.mermaid_enabled!==false).catch(()=>{});};
    textarea.hidden=true;render();$('#preview-read').onclick=()=>{textarea.hidden=true;$('#attachment-rendered').hidden=false;render();};$('#preview-edit').onclick=()=>{textarea.hidden=false;$('#attachment-rendered').hidden=true;};$('#preview-prev').onclick=()=>{offset=Math.max(0,offset-size);render();};$('#preview-next').onclick=()=>{offset=Math.min(Math.max(0,textarea.value.length-1),offset+size);render();};
  }

  function toolsHelp(){
    return window.WorkbenchV15.toolsHelp();
  }
  function setupSidebar(){
    if(ui.sidebarReady)return;ui.sidebarReady=true;
    const sidebar=$('.sidebar'),nav=$('#nav'),grip=$('#sidebar-resizer');let startY=0,startHeight=0;
    const apply=(value,save=false)=>{const maximum=Math.max(90,Math.floor(sidebar.clientHeight-225)),height=Math.max(90,Math.min(Math.round(value),maximum));nav.style.height=height+'px';nav.style.flexBasis=height+'px';grip.setAttribute('aria-valuemin','90');grip.setAttribute('aria-valuemax',String(maximum));grip.setAttribute('aria-valuenow',String(height));grip.setAttribute('aria-valuetext','导航高度 '+height+' 像素');if(save)localStorage.setItem('workbench-nav-height',String(height));};
    const saved=Number(localStorage.getItem('workbench-nav-height'));apply(saved||nav.getBoundingClientRect().height);
    grip.onpointerdown=e=>{if(e.button!==0)return;startY=e.clientY;startHeight=nav.getBoundingClientRect().height;grip.setPointerCapture(e.pointerId);grip.classList.add('dragging');e.preventDefault();};
    grip.onpointermove=e=>{if(grip.hasPointerCapture?.(e.pointerId))apply(startHeight+e.clientY-startY);};
    grip.onpointerup=e=>{if(grip.hasPointerCapture?.(e.pointerId))grip.releasePointerCapture(e.pointerId);grip.classList.remove('dragging');apply(nav.getBoundingClientRect().height,true);};
    grip.onkeydown=e=>{if(['ArrowUp','ArrowDown','Home'].includes(e.key)){e.preventDefault();apply(e.key==='Home'?300:nav.getBoundingClientRect().height+(e.key==='ArrowDown'?24:-24),true);}};
    window.addEventListener('resize',()=>{if(nav.style.height)apply(parseFloat(nav.style.height));});
  }

  function compactToolToggles(){
    const group=id=>['system_time','ask_user'].includes(id)?'基础':id==='python_run'?'计算':['read_attachment','read_file','list_directory','search_files','file_info','text_create','file_manage'].includes(id)?'文件':['office_operator','http_request'].includes(id)?'Office / 网络':'扩展';
    const container=$('#tool-toggles'),order=['基础','文件','Office / 网络','计算','扩展'];
    container.querySelectorAll('.tool-group-label').forEach(el=>el.remove());
    const buttons=[...container.querySelectorAll('button[data-toggle-tool]')];
    for(const label of order){const matches=buttons.filter(b=>group(b.dataset.toggleTool)===label);if(!matches.length)continue;const tag=document.createElement('span');tag.className='tool-group-label';tag.textContent=label;container.append(tag,...matches);}
  }

  function addMenu(){
    modal('添加附件或使用技能','<div class="add-actions"><button id="add-files-menu" class="button primary">▤ 添加文件或图片</button><button id="add-skill-menu" class="button">◇ 使用技能</button></div><p class="field-note">技能是本次任务的工作规范，不会自动开启 Python 或文件权限。普通聊天无需选择技能。</p>');
    $('#add-files-menu').onclick=()=>{closeModal();$('#file-input').click();};$('#add-skill-menu').onclick=handle(chooseSkills);
  }

  async function chooseSkills(){
    const result=await api('/api/skills');ui.skills=result.items||[];const selected=new Set(options().skill_ids||[]);
    modal('使用技能',`<p class="field-note">选择后仅对后续发送的任务生效；规则随待发任务固定，不修改正在回答的任务。最多 ${result.max_selected||2} 个。</p><div class="skill-picker">${ui.skills.map(s=>`<article class="card"><label class="check-label"><input type="checkbox" data-pick-skill="${h(s.id)}" ${selected.has(s.id)?'checked':''} ${s.available===false?'disabled':''}><b>${h(s.name)}</b></label><p>${h(s.description)}</p><p class="field-note">${h(s.permissions_note||'按需使用已授权工具，不自动扩大权限。')}</p><button class="button small" data-skill-info="${h(s.id)}">查看完整规则</button></article>`).join('')||'<p>暂无可用技能。</p>'}</div><button id="apply-skills" class="button primary">应用所选技能</button>`);
    $('.skill-picker').onchange=e=>{const id=e.target.dataset.pickSkill;if(!id)return;if(e.target.checked&&selected.size>=(result.max_selected||2)){e.target.checked=false;toast('所选技能已达上限');return;}e.target.checked?selected.add(id):selected.delete(id);};
    $('#apply-skills').onclick=handle(async()=>{await saveOptions({skill_ids:[...selected]});closeModal();renderSkills();});
    document.querySelectorAll('[data-skill-info]').forEach(b=>b.onclick=handle(()=>skillInfo(b.dataset.skillInfo)));
  }

  async function skillInfo(id){const skill=await api('/api/skills/'+id);modal(skill.name,`<article class="message-content skill-instructions">${markdown(skill.instructions||skill.description||'')}</article><p class="field-note">版本 ${h(skill.version||'1')} · 规则修订 ${h(skill.revision||'')}。分析方法可以自由选择，模型自报“已核验”不等于程序验证。</p><button id="back-skill-select" class="button">返回技能选择</button>`);addCopyButtons($('#modal-content'));$('#back-skill-select').onclick=handle(chooseSkills);}

  function renderSkills(){
    const tray=$('#skill-tray');if(!tray)return;const ids=options().skill_ids||[],selected=ui.skills.filter(s=>ids.includes(s.id));
    const required=[...new Set(selected.flatMap(s=>s.requires_tools||[]))],missing=required.filter(id=>!(options().tools||[]).includes(id)),unavailable=required.filter(id=>!state.tools.some(t=>t.id===id&&t.enabled));
    tray.innerHTML=selected.map(s=>`<span class="skill-label"><button data-show-skill="${h(s.id)}" title="查看技能规则">◇ ${h(s.name)}</button><button data-remove-skill="${h(s.id)}" aria-label="移除技能 ${h(s.name)}">×</button></span>`).join('')+(selected.length?`<span class="field-note">只装载所选规则；下一条发送生效。</span>${missing.length||options().tool_mode==='off'?'<button id="grant-skill-tools" class="button small">授权所需工具</button>':'<span class="field-note">所需工具已选中</span>'}${unavailable.length?'<span class="error-note">有工具尚未启用，选中不等于已启用。</span><button class="button small" data-view="tools">去工具空间配置</button>':''}`:'');
    tray.onclick=handle(async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.removeSkill){await saveOptions({skill_ids:ids.filter(id=>id!==b.dataset.removeSkill)});}else if(b.dataset.showSkill)await skillInfo(b.dataset.showSkill);else if(b.id==='grant-skill-tools'){
      if(!state.localAccess)throw new Error('共享界面不能授权宿主机文件或代码工具，请在本机操作');
      const names=required.map(id=>state.tools.find(t=>t.id===id)?.name||id).join('、');
      if(await confirmDialog('授权本任务所需工具',`将为后续发送勾选：${names}。Python 以当前用户权限执行正常代码，不是强隔离沙箱；只传入你已授权的附件。取消技能不会静默撤销你已选择的工具。`,'确认授权'))await saveOptions({tools:[...new Set([...(options().tools||[]),...required])],...(options().tool_mode==='off'?{tool_mode:'auto'}:{})});
    }});
  }

  function pendingTurns(id=state.conv?.id){return (ui.turnQueues[id]?.items||[]).filter(t=>['queued','paused','running'].includes(t.status));}

  function renderQueue(){
    const panel=$('#turn-queue');if(!panel)return;
    const queue=ui.turnQueues[state.conv?.id],items=pendingTurns();
    window.WorkbenchInteraction.render(items,()=>loadQueue(state.conv?.id,true));
    panel.hidden=!items.some(t=>t.status!=='running')&&!queue?.paused;
    if(panel.hidden){panel.innerHTML='';return;}
    panel.innerHTML=`<div class="queue-heading"><b>本对话待发队列 · ${items.filter(t=>t.status!=='running').length} 条等待</b><small title="保存知识库选择、模式和取段数；资料与索引使用执行时可用的完整版本，全局关闭即时尊重。">按顺序接上前轮回答；未来消息不会提前送入当前回答。</small>${queue?.paused?'<button id="resume-turns" class="button small primary">继续待发队列</button>':''}</div>${queue?.paused?`<p class="error-note">队列已暂停：${h(queue.paused_reason||'上一轮失败、停止，或程序曾重启。请确认后继续。')}</p>`:''}<div class="queue-items">${items.map((t,i)=>`<div class="queued-turn"><span class="queue-number">${i+1}</span><div><b>${t.status==='running'?'正在处理':t.status==='paused'?'已暂停':'等待发送'}</b><p>${h(t.content||'附带资料任务')}</p><small>${h(t.options?.model||'')} · ${fmtK(t.options?.context)} · ${(t.attachments||[]).length} 份附件${t.options?.skill_ids?.length?' · 含技能':''}</small></div><div class="queue-actions">${t.status!=='running'?`<button class="button small" data-edit-turn="${h(t.id)}">编辑</button>`:''}<button class="button small danger" data-cancel-turn="${h(t.id)}">${t.status==='running'?'停止':'取消'}</button></div></div>`).join('')}</div>`;
    window.WorkbenchInteraction.queueFold(panel);
    panel.onclick=handle(async e=>{const b=e.target.closest('button');if(!b||!state.conv)return;
      const id=state.conv.id;
      if(b.id==='resume-turns'){await api('/api/chat/'+id+'/turns/resume','POST',{});await loadQueue(id,true);}
      else if(b.dataset.cancelTurn){await api('/api/chat/'+id+'/turns/'+b.dataset.cancelTurn+'/cancel','POST',{});await loadQueue(id,true);}
      else if(b.dataset.editTurn){const turn=items.find(t=>t.id===b.dataset.editTurn);if(turn)editTurn(id,turn);}
    });
  }

  function editTurn(id,turn){
    modal('编辑待发消息',`<p class="field-note">只修改这条尚未执行的任务。附件、模型、工具、技能与知识库沿用提交时的快照，不会受聊天顶部后来切换影响。</p><label>消息内容<textarea id="edit-turn-content" rows="6">${h(turn.content)}</textarea></label><p class="field-note">${h(turn.options?.model||'')} · ${fmtK(turn.options?.context)} · ${(turn.attachments||[]).length} 份附件</p><button id="save-turn-edit" class="button primary">保存待发修改</button>`);
    $('#save-turn-edit').onclick=handle(async()=>{await api('/api/chat/'+id+'/turns/'+turn.id,'PATCH',{revision:turn.revision,content:$('#edit-turn-content').value});closeModal();await loadQueue(id,true);toast('待发消息已修改');});
  }

  async function loadQueue(id,force=false){
    if(!id||ui.queueLoading[id])return;
    if(!force&&Date.now()-(ui.queueLoaded[id]||0)<2500)return;
    ui.queueLoading[id]=true;
    try{
      const result=await api('/api/chat/'+id+'/turns?page=1&page_size=50');
      ui.turnQueues[id]=result;ui.queueLoaded[id]=Date.now();
      const active=result.items.find(t=>t.id===result.running_id)||result.items.find(t=>t.status==='running')||(!result.paused&&result.items.find(t=>t.status==='queued'));
      if(active&&!state.streams[id])observeTurn(id,active).catch(()=>{});
      if(state.conv?.id===id){syncCurrent();renderQueue();}
      const keys=Object.keys(ui.turnQueues);for(const key of keys.slice(0,Math.max(0,keys.length-200)))if(!pendingTurns(key).length){delete ui.turnQueues[key];delete ui.queueLoaded[key];}
    }finally{delete ui.queueLoading[id];}
  }

  async function stopCurrent(){
    const id=state.conv?.id;if(!id)return;
    const entry=state.streams[id],turn=entry?.turnId||ui.turnQueues[id]?.running_id;
    if(turn){await api('/api/chat/'+id+'/turns/'+turn+'/cancel','POST',{});toast('正在停止当前回答；后续消息会暂停，确认后可继续');await loadQueue(id,true);}
    else toast('当前没有正在生成的回答');
  }

  async function observeTurn(id,turn){
    if(state.streams[id])return;
    const entry={id,turnId:turn.id,message:turn.live_message||null,phase:turn.user_question?.status==='pending'?'等待你的回答 · 可选择或自定义回复':turn.status==='running'?'正在恢复当前回答':'排队中 · 可继续填写下一条',status:turn.status,controller:new AbortController(),cursor:turn.last_event_id||0};
    state.streams[id]=entry;
    const current=()=>state.conv?.id===id;
    const snapshot=value=>{if(current()&&value){
      const element=$('#messages'),top=element.scrollTop,follow=following(),previous=state.conv;
      if(previous?.id===value.id){
        // Preserve only already-loaded nodes, never fetch the entire history.
        const merged=new Map(previous.messages.map(m=>[m.id,m]));for(const message of value.messages)merged.set(message.id,message);
        const oldCount=path(previous).length;value={...value,messages:[...merged.values()]};
        if(previous.message_page?.around)value.message_page={...value.message_page,window_leaf:previous.message_page.window_leaf,around:previous.message_page.around};
        const loaded=path(value).length;
        if(value.message_page)value.message_page={...value.message_page,next_offset:loaded<value.message_page.total?loaded:null};
        if(state.visibleMessages[id]>=oldCount)state.visibleMessages[id]=loaded;
      }
      state.conv=value;syncCurrent();renderMessages();
      const restore=()=>{if(current())element.scrollTop=follow?element.scrollHeight:top;};restore();requestAnimationFrame(restore);
    }};
    if(current()){
      if(entry.message?.parent&&!state.conv.messages.some(m=>m.id===entry.message.parent)){
        const live=entry.message;entry.message=null;syncCurrent();
        try{const window=await api('/api/conversations/'+id);entry.message=live;snapshot(window);}catch{entry.message=live;}
      }else{syncCurrent();renderMessages();}
    }
    let reader,terminal=false;
    try{
      const response=await window.WorkbenchTransport.request('/api/chat/'+id+'/turns/'+turn.id+'/events?after='+entry.cursor,{signal:entry.controller.signal});
      if(!response.ok){const error=await response.json();throw new Error(error.detail||'队列状态暂时不可用');}
      reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';
      while(true){
        const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let cut;
        while((cut=buffer.indexOf('\n'))>=0){
          const line=buffer.slice(0,cut);buffer=buffer.slice(cut+1);if(!line.trim())continue;const packet=JSON.parse(line);
          if(packet.event_id)entry.cursor=packet.event_id;
          if(packet.type==='resync'){entry.message=packet.live_message||null;snapshot(packet.conversation);}
          else if(packet.type==='accepted'){snapshot(packet.conversation);}
          else if(packet.type==='queued'){entry.jobId=packet.job_id;entry.phase=`后端排队 · 位置 ${packet.position} · 可取消`;if(current())syncCurrent();}
          else if(packet.type==='start'){entry.status='running';entry.phase='正在准备模型与本轮上下文';const value=await api('/api/conversations/'+id);const original=value.messages.find(m=>m.id===packet.message_id);entry.message=original?{...original,content:'',thinking:'',interactions:[]}:null;snapshot(value);}
          else if(packet.type==='delta'&&entry.message){entry.message.content+=packet.text;entry.phase=`${num(packet.chars_per_second)} 字符/s · Token/s 完成后统计`;if(current()){syncCurrent();newContent(id);scheduleDraw();}}
          else if(packet.type==='thinking'&&entry.message){entry.message.thinking=(entry.message.thinking||'')+packet.text;entry.phase='模型正在思考 · 默认折叠';if(current()){syncCurrent();newContent(id);scheduleDraw();}}
          else if(packet.type==='runtime'&&entry.message){entry.message.runtime_state={...packet};delete entry.message.runtime_state.type;if(current())scheduleDraw();}
          else if(packet.type==='user_question'){entry.phase=packet.question?.status==='pending'?'等待你的回答 · 可选择或自定义回复':'已收到回答，正在继续';await loadQueue(id,true);if(current())syncCurrent();}
          else if(packet.type==='steering'){if(entry.message)entry.message.steering=packet.items||[];entry.phase=(packet.items||[]).some(x=>x.status==='pending')?'方向调整已接收 · 下一次模型请求前生效':'方向调整已应用';await loadQueue(id,true);if(current()){syncCurrent();renderMessages();}}
          else if(['tool_status','tool'].includes(packet.type)&&entry.message){window.WorkbenchToolUI.upsert(entry.message,packet);if(current()){if(packet.type==='tool')toast((packet.success?'已完成 ':'调用失败 ')+window.WorkbenchToolUI.name(packet.name,state.tools));scheduleDraw();}}
          else if(packet.type==='phase'){entry.phase=packet.text;if(current()){syncCurrent();$('#context-notice').hidden=false;$('#context-notice').textContent=packet.text;}}
          else if(packet.type==='retrieval'&&entry.message){entry.message.retrieval=packet;if(current())toast(`知识库找到 ${packet.items.length} 个相关片段`);}
          else if(packet.type==='context'){if(entry.message)entry.message.context_report=packet;if(current())showContextReport(packet);}
          else if(packet.type==='retry'&&entry.message){entry.message.content=packet.content;entry.message.thinking=packet.thinking;entry.message.attempts=packet.attempts;if(current())renderMessages();}
          else if(packet.type==='stats'&&entry.message){entry.message.stats=packet;if(current())scheduleDraw();}
          else if(packet.type==='error'){if(entry.message)entry.message.error=packet.message;entry.phase=packet.message;if(current())toast(packet.message);}
          else if(packet.type==='done'){terminal=true;snapshot(packet.conversation);}
          else if(packet.type==='turn_state'){if(['done','failed','cancelled','interrupted'].includes(packet.status||packet.turn?.status))terminal=true;}
        }
      }
    }catch(error){if(error.name!=='AbortError'&&current())toast('显示连接暂时中断，后台任务不因此停止；正在恢复队列状态。');}
    finally{
      if(reader)try{await reader.cancel();}catch{}
      if(state.streams[id]===entry)delete state.streams[id];
      if(current()){
        clearTimeout(drawTimer);drawTimer=null;liveMessage=null;state.busy=false;
        let refreshed=false;try{snapshot(await api('/api/conversations/'+id));refreshed=true;}catch{}
        syncCurrent();if(!refreshed)renderMessages();renderQueue();scrollState();updateBudget();
        const report=[...(state.conv?.messages||[])].reverse().find(m=>m.context_report)?.context_report;
        if(report)showContextReport(report);else $('#context-notice').hidden=true;
      }
      await refreshHistory();delete ui.queueLoaded[id];
      if(terminal)await loadQueue(id,true);
    }
  }

  async function streamChat(regenerate=false){
    if(ui.preparing)return;
    if(regenerate&&pendingTurns().length){toast('此对话还有正在处理或待发的任务，请处理队列后再重新生成，避免切错分支。');return;}
    const draft=$('#prompt').value,content=regenerate?'':draft.trim();
    if(!regenerate&&!content&&!state.attachments.length)return;
    if(state.attachments.some(a=>a.status==='processing')){toast('附件仍在处理，完成后再加入队列');return;}
    const refs=regenerate?[]:state.attachments.filter(a=>a.status==='ready'||a.image).map(a=>({id:a.id,text:a.injectText??a.text??'',mode:a.mode||'both'}));
    const sentIds=new Set(refs.map(a=>a.id));ui.preparing=true;
    $('#send').disabled=true;$('#send').textContent='正在接收…';
    try{
      await ensureConversation();const id=state.conv.id;
      const request={content,regenerate,attachments:refs,options:structuredClone(window.WorkbenchReasoning.requestOptions(options())),cloud_confirmed:options().provider!=='local'};
      const fingerprint=JSON.stringify(request),previous=ui.submitReceipts[id];
      request.client_request_id=previous?.fingerprint===fingerprint?previous.requestId:crypto.randomUUID();
      ui.submitReceipts[id]={fingerprint,requestId:request.client_request_id};
      const result=await api('/api/chat/'+id+'/turns','POST',request);
      delete ui.submitReceipts[id];
      if(state.conv?.id===id){if(!regenerate&&$('#prompt').value===draft)$('#prompt').value='';state.attachments=state.attachments.filter(a=>!sentIds.has(a.id));renderAttachments();ui.follow=true;delete scrollMemory['chat:'+id];}
      if(result.queue)ui.turnQueues[id]=result.queue;
      toast('已接收；按本对话顺序处理，不打断上一轮');await loadQueue(id,true);
    }finally{ui.preparing=false;$('#send').disabled=false;syncCurrent();renderQueue();updateBudget();}
  }

  async function view(name){
    try{if(name==='knowledge')await renderKnowledge();if(name==='memory')await renderMemory();if(name==='activity')await renderActivity();}catch(e){toast(e.message);}
  }

  document.addEventListener('click',handle(async e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;if(d.openKb){ui.kbDetail=d.openKb;ui.docPage=1;await renderKnowledge();}if(d.editKb)await editLibrary(d.editKb);if(d.toggleKb){await api('/api/knowledge/'+d.toggleKb,'PATCH',{enabled:d.enabled!=='1'});await renderKnowledge();}if(d.kbPage){ui.kbPage=Number(d.kbPage);await renderKnowledge();}if(d.docPage){ui.docPage=Number(d.docPage);await renderLibrary(ui.kbDetail);}if(d.deleteDoc){if(await confirmDialog('移除文档','从本知识库移除并更新索引，外部原件保留。','移除')){await api('/api/knowledge/'+d.kb+'/documents/'+d.deleteDoc,'DELETE');await renderLibrary(d.kb);}}if(d.activityTab){ui.activityTab=d.activityTab;ui.activityPage=1;await renderActivity();}if(d.activityPage){ui.activityPage=Number(d.activityPage);await renderActivity();}if(d.cancelJob){await api('/api/jobs/'+d.cancelJob+'/cancel','POST',{});toast('正在取消指定任务');await renderActivity();}}));
  setInterval(async()=>{if(document.hidden||!state.connected||ui.pollPending||ui.uploading)return;ui.pollPending=true;try{if(state.view==='activity'&&!document.activeElement?.matches('#view-activity input,#view-activity select'))await renderActivity();if(state.view==='knowledge'&&!document.activeElement?.matches('input,textarea,select')){if(ui.kbTab==='documents'&&ui.kbDetail)await renderLibrary(ui.kbDetail);else if(['maintenance','models'].includes(ui.kbTab))await renderKnowledgeManagement();}}catch{}finally{ui.pollPending=false;}},6000);
  setInterval(()=>{if(document.hidden||!state.connected)return;const ids=new Set([state.conv?.id,...Object.keys(state.streams),...Object.keys(ui.turnQueues).filter(id=>pendingTurns(id).length)]);for(const id of ids)if(id)loadQueue(id).catch(()=>{});},3000);
  return {boot,login,controls,view,settings,settingsPatch,settingsSaved,provider,modelDetail,attachmentPreview,toolsHelp,syncCurrent,following,newContent,scrollState,renderKnowledge,send:streamChat,stopCurrent,isPreparing:()=>!!ui.preparing};
})();
handle(bootstrap)();
