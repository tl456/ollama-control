"use strict";
window.WorkbenchV16=(()=>{
  const portSources=new WeakMap();
  const ui={skillPage:1,skillSearch:"",outlineKey:"",outlinePending:false,auditTool:"",auditModel:"",auditStatus:""};
  const pager=(p,total,size,attr)=>'<div class="pager"><button class="button" data-'+attr+'="'+Math.max(1,p-1)+'" '+(p<=1?'disabled':'')+'>上一页</button><span>'+p+' / '+Math.max(1,Math.ceil(total/size))+' · '+total+' 项</span><button class="button" data-'+attr+'="'+(p+1)+'" '+(p*size>=total?'disabled':'')+'>下一页</button></div>';
  function wide(){const d=$('#modal');d.classList.add('wide-dialog');d.addEventListener('close',()=>d.classList.remove('wide-dialog'),{once:true});}
  function boot(result){
    if(result?.service_ports)state.servicePorts=result.service_ports;
    if(!$('#conversation-outline'))$('#messages').after(Object.assign(document.createElement('nav'),{id:'conversation-outline',className:'conversation-outline'}));
    $('#conversation-outline').setAttribute('aria-label','本对话提问导航');
    const bottom=$('#jump-bottom');bottom.onclick=handle(async()=>{
      if(state.conv?.message_page?.around){const id=state.conv.id;const fresh=await api('/api/conversations/'+id);if(state.conv?.id!==id)return;state.conv=fresh;window.Workbench.syncCurrent();delete scrollMemory['chat:'+id];renderMessages();}
      $('#messages').scrollTop=$('#messages').scrollHeight;window.Workbench.scrollState();
    });
    for(const name of ['skills'])$('[data-view="'+name+'"]')?.toggleAttribute('hidden',!state.localAccess);
    ports();outline();
    const changes=state.servicePorts?.changes||[],key=JSON.stringify(changes);if(changes.length&&sessionStorage.getItem('port-notice')!==key){sessionStorage.setItem('port-notice',key);modal('已自动调整服务端口','<p>原端口已被占用，工作台已使用以下可用端口：</p><ul>'+changes.map(x=>'<li>'+h(({web:'工作台界面',gateway:'模型共享网关',chat:'聊天Ollama',embedding:'嵌入Ollama'}[x.role])||x.role)+'：'+h(x.requested)+' → '+h(x.actual)+'</li>').join('')+'</ul><p>接口教程已更新为实际地址。</p><button class="button primary" data-close-modal>知道了</button>');}
  }
  function settings(anchor){
    const c=state.cfg,el=document.createElement('section');el.className='settings-section card';
    el.innerHTML='<h2>启动与工具执行</h2><div class="form-grid"><label>打开工作台的方式<select id="setting-launch-mode"><option value="app">浏览器应用窗口 · 默认</option><option value="webview2">WebView2 内嵌窗口</option><option value="default">普通浏览器标签页</option></select><p class="settings-help">托盘、快捷方式和开机启动采用同一选择。应用窗口隐藏地址栏；首选不可用时回退到浏览器。保存后下次打开生效。</p></label><label>每轮工具调用总额度<input id="setting-max-tool-calls" type="number" min="1" max="500" value="'+h(c.max_tool_calls??50)+'"><p class="settings-help">每条用户消息默认最多累计50次，包括失败尝试；所有模型、原生与传统调用使用相同额度。自动时间注入不计入，达到额度后保留已有结果。</p></label></div>';
    anchor.before(el);$('#setting-launch-mode').value=c.launch_mode||'app';ports();
  }
  function ports(){
    const p=state.servicePorts?.ports;if(!p)return;const map={'8765':p.web,'11436':p.gateway,'11437':p.chat,'11438':p.embedding};
    for(const root of [$('#view-api')].filter(Boolean)){const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let node;while(node=walker.nextNode()){if(!portSources.has(node))portSources.set(node,node.textContent);node.textContent=portSources.get(node).replace(/\b(8765|11436|11437|11438)\b/g,x=>String(map[x]||x));}for(const b of root.querySelectorAll('[data-copy-text]')){if(!portSources.has(b))portSources.set(b,b.dataset.copyText);b.dataset.copyText=portSources.get(b).replace(/\b(8765|11436|11437|11438)\b/g,x=>String(map[x]||x));}}
  }
  function settingsPatch(){return {launch_mode:$('#setting-launch-mode').value,max_tool_calls:Number($('#setting-max-tool-calls').value)};}
  async function documentPreview(kb,doc){
    const value=await api('/api/knowledge/'+encodeURIComponent(kb)+'/documents/'+encodeURIComponent(doc)+'/source');
    modal(value.name||'知识库文档', '<div class="document-preview-toolbar"><span>当前文档内容</span><button class="button" id="copy-kb-source">复制正文</button><button class="button" data-close-modal>关闭</button></div><article class="message-content document-reading">'+markdown(value.text||'')+'</article>');
    wide();$('#copy-kb-source').onclick=handle(()=>copy(value.text||''));addCopyButtons($('#modal-content'));window.enhanceWorkMarkdown?.($('#modal-content'),state.cfg.mermaid_enabled!==false).catch(()=>{});
  }
  async function renderSkills(){
    const result=await api('/api/skills?'+new URLSearchParams({page:ui.skillPage,search:ui.skillSearch}));if(state.view!=='skills')return;ui.skillPage=result.page;
    $('#view-skills').innerHTML='<div class="page-heading"><div><div class="eyebrow">方法与规则</div><h1>技能中心</h1><p>技能描述如何完成任务；工具负责实际执行。在聊天输入框“＋”中选择技能，后续发送时生效。</p></div><div class="page-actions"><button class="button" id="import-skill">导入 MD / ZIP</button><button class="button primary" id="new-skill">＋ 新建技能</button><input type="file" id="skill-import-file" accept=".md,.markdown,.zip" hidden></div></div><div class="filter-row"><input id="skill-search" placeholder="搜索名称或简介" value="'+h(ui.skillSearch)+'"></div><div class="skills-grid">'+result.items.map(s=>'<article class="card"><span class="tag">'+(s.builtin?'示例预设':'我的技能')+'</span><h2>'+h(s.name)+'</h2><p>'+h(s.description)+'</p><p class="field-note">建议工具：'+h((s.requires_tools||[]).join('、')||'按任务选择')+' · 版本 '+h(s.version)+'</p><div class="page-actions"><button class="button small" data-skill-read="'+h(s.id)+'">阅览</button><button class="button small" data-skill-edit="'+h(s.id)+'">'+(s.builtin?'查看示例':'编辑')+'</button><button class="button small" data-skill-copy="'+h(s.id)+'">复制</button><button class="button small" data-skill-export="'+h(s.id)+'">导出 ZIP</button><button class="button small" data-skill-md="'+h(s.id)+'">导出 MD</button>'+(!s.builtin?'<button class="button small danger" data-skill-delete="'+h(s.id)+'">删除</button>':'')+'</div></article>').join('')+'</div>'+pager(result.page,result.total,result.page_size,'skill-page')+'<details class="card skill-help"><summary>怎样编写和使用技能</summary><p>写清目标、输入、工作步骤和输出要求。提示模型核对资料，不编造缺失数据。需要运行工具时说明用途，用户仍需在对话中勾选对应工具。</p><p>ZIP 包包含 manifest.json、SKILL.md 和说明，可导出后编辑再导入为新副本；MD 仅含规则正文。技能不会自动安装程序或执行代码，排队中的任务保留发送时的规则快照。</p></details>';
    $('#new-skill').onclick=handle(()=>skillEditor());$('#import-skill').onclick=()=>$('#skill-import-file').click();
    $('#skill-import-file').onchange=handle(async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;const data=new FormData();data.append('file',f);await api('/api/skills/import','POST',data);toast('技能已导入为独立副本');await renderSkills();});
    let timer;$('#skill-search').oninput=e=>{ui.skillSearch=e.target.value;clearTimeout(timer);timer=setTimeout(handle(async()=>{ui.skillPage=1;await renderSkills();}),300);};restoreScroll();
  }
  async function skillEditor(id){
    const s=id?await api('/api/skills/'+id):{name:'新技能',description:'',instructions:'# 目标\n\n# 工作步骤\n\n# 输出要求\n',version:'1.0',requires_tools:[]},read=!!s.builtin;
    modal(read?'内置示例 · 可复制后编辑':id?'编辑技能':'新建技能','<div class="modal-form"><label>名称<input id="skill-name" maxlength="100" value="'+h(s.name)+'" '+(read?'readonly':'')+'></label><label>简介<input id="skill-description" maxlength="1000" value="'+h(s.description)+'" '+(read?'readonly':'')+'></label><div class="form-grid"><label>版本<input id="skill-version" maxlength="32" value="'+h(s.version)+'" '+(read?'readonly':'')+'></label><label>建议工具编号 · 逗号分隔<input id="skill-tools" value="'+h((s.requires_tools||[]).join(', '))+'" '+(read?'readonly':'')+'></label></div><div class="md-editor-columns"><label>Markdown 规则<textarea id="skill-instructions" spellcheck="false" '+(read?'readonly':'')+'>'+h(s.instructions)+'</textarea><span id="skill-byte-count"></span></label><section><h3>阅读预览</h3><article id="skill-render" class="message-content"></article></section></div><div class="page-actions">'+(read?'<button class="button primary" data-skill-copy="'+h(id)+'">复制后编辑</button>':'<button class="button primary" id="save-skill">保存技能</button>')+'<button class="button" data-close-modal>关闭</button></div></div>');wide();
    const editor=$('#skill-instructions'),render=()=>{$('#skill-render').innerHTML=markdown(editor.value);$('#skill-byte-count').textContent=new TextEncoder().encode(editor.value).length+' / 12000 字节';};render();let timer;editor.oninput=()=>{clearTimeout(timer);timer=setTimeout(render,120);};
    if(!read)$('#save-skill').onclick=handle(async()=>{const b=$('#save-skill');b.disabled=true;try{await api('/api/skills'+(id?'/'+id:''),id?'PUT':'POST',{name:$('#skill-name').value,description:$('#skill-description').value,version:$('#skill-version').value,instructions:editor.value,requires_tools:$('#skill-tools').value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean)});closeModal();toast('技能已保存，后续发送使用新规则');await renderSkills();}finally{b.disabled=false;}});
  }
  async function outline(){
    const nav=$('#conversation-outline');if(!nav)return;const c=state.conv;if(!c){nav.replaceChildren();ui.outlineKey='';return;}
    const key=c.id+':'+c.leaf;if(ui.outlineKey===key||ui.outlinePending)return;ui.outlinePending=true;
    try{const result=await api('/api/conversations/'+c.id+'/outline');if(state.conv?.id!==c.id)return;ui.outlineKey=key;
      nav.innerHTML=(result.items||[]).map((m,i)=>'<button data-jump-message="'+h(m.id)+'" title="'+h(m.summary)+'" aria-label="跳转到第'+(i+1)+'次提问：'+h(m.summary)+'"><span class="outline-tick"></span><span class="outline-preview">'+h(m.summary||'附加资料')+'</span></button>').join('');
    }catch(error){ui.outlineKey=key;nav.title='对话导航暂未加载，可刷新后重试';}finally{ui.outlinePending=false;if(state.conv?.id&&state.conv.id!==c.id)outline();}
  }
  async function jumpMessage(id){
    const convId=state.conv?.id;if(!convId)return;
    let node=$('[data-message="'+CSS.escape(id)+'"]');
    if(!node){const page=await api('/api/conversations/'+convId+'?around='+encodeURIComponent(id));if(state.conv?.id!==convId)return;state.conv=page;state.visibleMessages[convId]=page.messages.length;delete scrollMemory['chat:'+convId];renderMessages();node=$('[data-message="'+CSS.escape(id)+'"]');}
    node?.scrollIntoView({block:'center',behavior:'smooth'});node?.classList.add('located-message');setTimeout(()=>node?.classList.remove('located-message'),2500);$('#jump-bottom').hidden=false;window.Workbench.scrollState();
  }
  function tools(){
    const imports=$('#import-plugin');if(imports&&!$('#interface-example')){imports.insertAdjacentHTML('beforebegin','<button id="interface-example" class="button">下载接口测试示例</button>');$('#interface-example').onclick=handle(()=>download('/api/tools/example-interface','tool_interface_demo.zip'));}
    for(const card of $$('#view-tools .tool-card')){
      const id=card.querySelector('[data-test-tool]')?.dataset.testTool,t=state.tools.find(t=>t.id===id);if(!t)continue;
      const labels={create:'会创建文件',edit:'可修改文件',recycle:'可移入回收站',network:'会向目标地址发送请求'};
      const tags=[...(t.capabilities||[]).filter(x=>labels[x]).map(x=>labels[x]),...(t.capability_labels||[])];if(tags.length)card.insertAdjacentHTML('beforeend','<div class="capability-labels">'+tags.map(x=>'<span class="capability-label">'+h(x)+'</span>').join('')+'</div>');
      if(t.exportable!==false)card.querySelector('[data-test-tool]')?.parentElement.insertAdjacentHTML('beforeend','<button class="button small" data-export-tool="'+h(id)+'">导出工具</button>');
      else card.insertAdjacentHTML('beforeend','<p class="field-note">此工具依赖聊天交互通道；实现及教程已包含在完整源码包。</p>');
    }
  }
  async function testTool(name){
    const t=state.tools.find(t=>t.id===name);if(!t)throw new Error('工具不存在');
    if(name==='ask_user'){modal('提问用户 · 在聊天中体验','<p>此工具已默认勾选。新建对话后，可以发送“请先询问我想要的输出形式，给两个选项，得到回答后再继续”。</p><p>问题会显示在聊天输入区上方，支持选择、自定义回答和跳过；普通发送仍排队，“调整当前任务”用于中途改变方向。模型是否调用由实际响应决定。</p>');return;}
    const example=t.example||(name==='office_operator'?{action:'fonts'}:Object.fromEntries(Object.entries(t.parameters?.properties||{}).filter(([key,v])=>t.parameters?.required?.includes(key)||v.default!==undefined).map(([key,v])=>[key,v.default??v.enum?.[0]??(v.type==='object'?{}:v.type==='array'?[]:v.type==='number'||v.type==='integer'?0:'')])));
    modal('工具测试 · '+t.name,'<p>'+h(t.description)+'</p><label>调用参数 JSON<textarea id="tool-test-json" spellcheck="false">'+h(JSON.stringify(example,null,2))+'</textarea></label><p class="field-note">点击执行会真正调用工具；请将示例路径和地址改为你的目标。编辑、联网等能力与聊天调用相同。</p><details><summary>参数说明</summary><pre>'+h(JSON.stringify(t.parameters,null,2))+'</pre></details><button id="execute-tool-test" class="button primary">执行一次</button><div id="tool-test-result" aria-live="polite"></div>');
    $('#execute-tool-test').onclick=handle(async()=>{const button=$('#execute-tool-test'),area=$('#tool-test-result');button.disabled=true;area.textContent='正在执行…';try{let args;try{args=JSON.parse($('#tool-test-json').value);}catch{throw new Error('参数JSON格式不正确');}const r=await api('/api/tools/test','POST',{name,arguments:args});area.innerHTML=window.WorkbenchToolUI.eventHtml({name,result:r,success:r.ok!==false,status:r.ok===false?'failed':'success'},state.tools,modeNames);}catch(error){area.textContent=error.message;throw error;}finally{button.disabled=false;}});
  }
  async function activity(tab,page=1){
    const q=new URLSearchParams({page});if(tab==='audit'){q.set('tool',ui.auditTool);q.set('model',ui.auditModel);q.set('status',ui.auditStatus);}
    const r=await api('/api/'+tab+'?'+q);if(state.view!=='activity')return;
    const filters=tab==='audit'?'<div class="filter-row"><input id="audit-tool" placeholder="工具编号" value="'+h(ui.auditTool)+'"><input id="audit-model" placeholder="模型名称" value="'+h(ui.auditModel)+'"><select id="audit-status"><option value="">全部结果</option><option value="success">成功</option><option value="failed">失败</option><option value="error">执行异常</option><option value="unknown">结果未确认</option><option value="http_error">HTTP错误</option><option value="cancelled">取消</option><option value="running">执行中</option></select><button id="filter-audit" class="button">筛选</button></div>':'';
    const rows=tab==='audit'?r.items.map(x=>'<tr><td>'+h(new Date(x.created_at).toLocaleString())+'</td><td>'+h(x.model)+'<br><small>'+h(x.provider)+'</small></td><td>'+h(x.tool)+'<br>'+h(x.action)+'</td><td>'+h(({success:'成功',failed:'失败',error:'执行异常',unknown:'结果未确认',http_error:'HTTP错误',running:'执行中',cancelled:'取消'}[x.status])||x.status)+' · '+num(x.duration)+'s</td><td>'+h(x.target)+'<br>'+h(x.error_type||'')+'</td></tr>').join(''):r.items.map(x=>'<tr><td>'+h(new Date(x.created_at).toLocaleString())+'</td><td class="wrap-path">'+h(x.path)+'</td><td>'+h(x.action)+'</td><td>'+fmtBytes(x.bytes)+'</td><td><button class="button small" data-restore-snapshot="'+h(x.id)+'">恢复此快照</button></td></tr>').join('');
    $('#view-activity').innerHTML='<div class="page-heading"><div><div class="eyebrow">任务与有界日志</div><h1>'+(tab==='audit'?'工具审计':'文件快照')+'</h1><p>'+(tab==='audit'?'模型与工具的基本执行记录，不保存聊天正文或密钥。':'按编辑时间查看修改前版本。恢复前会先保存文件当前状态。')+'</p></div></div><div class="tab-row">'+[['queue','推理队列'],['builds','知识库处理'],['access','API 来源 IP 日志'],['audit','工具审计'],['snapshots','文件快照']].map(([id,name])=>'<button class="button '+(id===tab?'active':'')+'" data-activity-tab="'+id+'">'+name+'</button>').join('')+'</div>'+filters+'<div class="table-wrap"><table class="data-table"><thead><tr>'+(tab==='audit'?['时间','模型 / 连接','工具 / 动作','结果 / 耗时','目标 / 错误']:['时间','文件','动作','大小','操作']).map(t=>'<th>'+t+'</th>').join('')+'</tr></thead><tbody>'+(rows||'<tr><td colspan="5">暂无记录。</td></tr>')+'</tbody></table></div>'+pager(r.page,r.total,r.page_size,'activity-page')+(tab==='audit'?'<p class="field-note">当前 '+fmtBytes(r.bytes)+' · 最长 '+h(r.retention_days)+' 天 / '+fmtBytes(r.max_bytes)+(r.compression?' · GZIP已归档 '+h(r.compression.archived_records)+' 条，近期 '+h(r.compression.hot_records)+' 条实时记录':'')+'</p>':'');
    if(tab==='audit'){$('#audit-status').value=ui.auditStatus;$('#filter-audit').onclick=handle(async()=>{ui.auditTool=$('#audit-tool').value;ui.auditModel=$('#audit-model').value;ui.auditStatus=$('#audit-status').value;await activity('audit',1);});}restoreScroll();
  }
  async function officePreview(id){
    const result=await api('/api/office/previews/'+encodeURIComponent(id));let page=0,scale=1;
    modal(result.source_name||'Office 排版预览','<div class="office-preview-controls"><button id="preview-prev" class="button">上一页</button><span id="preview-position"></span><button id="preview-next" class="button">下一页</button><button id="preview-smaller" class="button">缩小</button><button id="preview-fit" class="button">适应宽度</button><button id="preview-larger" class="button">放大</button><button id="preview-download" class="button">下载此页</button></div><div class="office-preview-canvas"><img id="office-preview-image" alt="Office实际渲染页面"></div><p class="field-note">本机 Office 导出的排版效果。前端查看不代表模型已接收或检查过图片。</p>');wide();
    const note=$('#modal-content .field-note');if(note)note.textContent=(result.method||'Office实际渲染')+' · '+(result.range||'返回范围')+'。前端查看不代表模型已接收或检查图片。';
    const items=result.items||[];
    const render=()=>{const item=items[page];$('#preview-position').textContent=items.length?(page+1)+' / '+items.length:'暂无可预览页面';$('#preview-prev').disabled=page<=0;$('#preview-next').disabled=page>=items.length-1;$('#preview-download').disabled=!item;const img=$('#office-preview-image');img.hidden=!item;if(item){if(!/^\/api\/office\/previews\/[a-zA-Z0-9_-]+\//.test(item.url))throw new Error('预览地址无效');img.src=item.url;img.style.width=(scale*100)+'%';}};
    $('#preview-prev').onclick=()=>{page=Math.max(0,page-1);render();};$('#preview-next').onclick=()=>{page=Math.min(items.length-1,page+1);render();};$('#preview-smaller').onclick=()=>{scale=Math.max(.25,scale-.25);render();};$('#preview-larger').onclick=()=>{scale=Math.min(3,scale+.25);render();};$('#preview-fit').onclick=()=>{scale=1;render();};$('#preview-download').onclick=handle(()=>download(items[page].url,items[page].name||'page.png'));render();
  }
  function summary(){
    const c=state.conv,report=[...(c?.messages||[])].reverse().find(m=>m.context_report?.state?.summary)?.context_report,s=c?.context_state||report?.state;
    modal('上下文与历史摘要','<div class="diagnostic-grid"><div><span>本次设置</span><b>'+fmtK(options().context)+'</b></div><div><span>模型声明</span><b>'+fmtK(state.modelCaps?.max_context)+'</b></div></div><p class="field-note">允许超过模型声明值。服务端实际能力与主机内存决定是否可用；原始聊天记录保留。</p>'+(s?.summary?'<p>已摘要 '+h(s.covered_messages)+' 条旧消息，保留 '+h(s.recent_messages)+' 条近期消息。</p><article class="message-content">'+markdown(s.summary)+'</article>':'<p class="inline-empty">当前尚无历史摘要。达到设置的触发比例后自动整理。</p>'));wide();
  }
  function controls(){
    if(!$('#context-summary-button'))$('#chat-options').before(Object.assign(document.createElement('button'),{id:'context-summary-button',className:'chip',textContent:'上下文 / 摘要'}));
    $('#context-summary-button').onclick=summary;
    const over=state.modelCaps?.max_context&&options().context>state.modelCaps.max_context;$('#context-summary-button').title=over?'已超过模型声明值，按本次设置尝试':'查看容量与压缩摘要';
    $('#context-summary-button').classList.toggle('context-over-declared',!!over);
  }
  async function view(name){ports();if(name==='skills')try{await renderSkills();}catch(e){toast(e.message);}}
  document.addEventListener('click',e=>handle(async e=>{
    const b=e.target.closest('button');if(!b)return;const d=b.dataset;
    if(d.viewKbDoc)await documentPreview(d.sourceKb,d.viewKbDoc);
    if(d.exportTool)await download('/api/tools/'+encodeURIComponent(d.exportTool)+'/export',d.exportTool+'.zip');
    if(d.skillPage){ui.skillPage=Number(d.skillPage);await renderSkills();}
    if(d.skillRead){const s=await api('/api/skills/'+d.skillRead);modal(s.name,'<article class="message-content document-reading">'+markdown(s.instructions)+'</article>');wide();}
    if(d.skillEdit)await skillEditor(d.skillEdit);
    if(d.skillCopy){const s=await api('/api/skills/'+d.skillCopy+'/copy','POST',{});closeModal();await renderSkills();await skillEditor(s.id);}
    if(d.skillDelete&&await confirmDialog('删除技能','删除此技能，不改变已提交任务的规则快照。','删除')){await api('/api/skills/'+d.skillDelete,'DELETE');await renderSkills();}
    if(d.skillExport)await download('/api/skills/'+d.skillExport+'/export',d.skillExport+'.zip');
    if(d.skillMd)await download('/api/skills/'+d.skillMd+'/export?format=md',d.skillMd+'.md');
    if(d.jumpMessage)await jumpMessage(d.jumpMessage);
    if(d.officePreview)await officePreview(d.officePreview);
    if(d.restoreSnapshot&&await confirmDialog('恢复文件快照','当前文件会先保存一份快照，再恢复到所选版本。','恢复')){await api('/api/snapshots/'+d.restoreSnapshot+'/restore','POST',{});toast('文件已恢复，当前状态已保留快照');await activity('snapshots');}
  })(e));
  document.addEventListener('change',e=>{if(e.target.matches('[data-font-demo]')){const demo=e.target.closest('.office-result-card').querySelector('.font-demo');demo.style.fontFamily=JSON.stringify(e.target.value);}});
  return {boot,settings,settingsPatch,view,tools,activity,outline,controls,officePreview,testTool};
})();
