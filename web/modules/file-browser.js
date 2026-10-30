"use strict";
window.WorkbenchFileBrowser = (() => {
  function toolsHelp(){
    if(document.querySelector('#tool-design-help'))return;
    document.querySelector('#view-tools').insertAdjacentHTML('beforeend',`<section id="tool-design-help" class="card page-section"><h2>工具使用与开发入门</h2><p class="field-note">工具先在工作空间测试，再在对话中勾选。文件工具支持当前Windows账户能读取的完整路径，不要求目录白名单。读取结果可能含敏感资料，选择云模型前请核对。Python和导入插件按可信代码执行，不是权限沙箱。</p><details><summary>只读文件读取 · 全文 / 分段 / 按行</summary><pre>${h(JSON.stringify({path:'C:\\示例资料\\sample.dat',mode:'lines',start_line:1,line_count:100},null,2))}</pre><p class="field-note">搜索深度、分页、超时和返回容量保护性能。未知格式先验证纯文本，分段读取查看next_line/next_offset，部分结果不能当全文。</p></details><details><summary>文件编辑、Office和网络工具</summary><p class="field-note">新建文本只新增TXT/MD，重名自动编号。文件操作器可复制、重命名、编辑文本和移入回收站；已有文件编辑前保存快照。Office操作器先inspect读取结构，再edit修改，支持插图和preview排版预览；字体可用fonts查询。网络工具可访问网页、本机和局域网HTTP API。配置在各工具卡片中，所有模型使用同一套功能。</p></details><details><summary>开发一个独立工具</summary><p class="field-note">每个工具有独立目录：manifest.json声明ID、说明、参数与配置；tool.py实现run(arguments, context)；README.md提供中文教程、参数、返回值和故障示例。下载时间工具示例可直接学习。各工具共用文档提取等基础服务，不彼此调用业务。</p><p class="field-note">修改后用合成资料测试，返回JSON可序列化对象；错误保留真实原因，设置有界超时，不把密钥或企业资料写入代码。插件权限声明不等于操作系统隔离。</p></details><button class="button" data-view="guide">完整软件与工具教程</button></section>`);
    const notices=document.querySelectorAll('#view-tools>.notice');
    if(notices[0])notices[0].textContent='目录、属性、搜索和读取工具保持只读。新建文本、文件操作器与Office提供独立写入能力，卡片蓝色标签说明用途；工具执行记录在任务栏可查。所有路径属于宿主机。';
    if(notices[1])notices[1].textContent='目录、属性、搜索和正文读取各自独立配置。搜索默认当前目录及下一级，可调整深度；Office COM/Python读取方式在文件读取工具配置。';
  }
  async function open(tool='list_directory',initial={}){
    const conf=await api('/api/tools/'+tool+'/settings');
    const current={path:initial.path||conf.values?.start_directory||state.cfg.root||'C:\\',offset:initial.offset||0};let items=[];
    modal('只读文件浏览 · 当前账户可访问路径',`<label>宿主机完整路径<input id="browser-path" value="${h(current.path)}" placeholder="例如C:\\资料；远程仍读取宿主机"></label><div class="page-actions"><button id="browser-up" class="button small">↑ 上一级</button><button id="browser-open" class="button small primary">打开目录</button>${state.actualLocal!==false?'<button id="browser-pick" class="button small">选择文件夹</button>':''}<input id="browser-query" placeholder="按文件名搜索，支持*.txt"><button id="browser-search" class="button small">搜索</button></div><p class="notice">不限制目录范围，仍受Windows账户权限和只读工具能力约束。搜索默认下属${conf.values?.search_depth??state.cfg.search_depth??1}级，分页和返回上限防止卡顿。路径属于宿主机。</p><div id="browser-result"></div><div id="browser-pagination" class="sidebar-pagination"></div>`);
    const join=file=>file.path||(/^[a-z]:[\\/]|^\\\\/i.test(file.relative_path||'')?file.relative_path:current.path.replace(/[\\/]$/,'')+'\\'+(file.relative_path||file.name));
    const refresh=async(search=false)=>{
      current.path=document.querySelector('#browser-path').value.trim();if(!current.path)throw new Error('请输入宿主机完整目录路径');
      const result=await api('/api/tools/test','POST',{name:search?'search_files':'list_directory',arguments:{path:current.path,offset:current.offset,...(search?{query:document.querySelector('#browser-query').value}:{})}});items=result.items||[];
      const node=document.querySelector('#browser-result');if(!node)return;
      node.innerHTML=`<div class="table-wrap"><table class="data-table"><thead><tr><th>名称</th><th>类型 / 大小</th><th>操作</th></tr></thead><tbody>${items.map((f,i)=>`<tr><td><button data-browser-enter="${i}">${f.type==='directory'?'▸':'▤'} ${h(f.relative_path||f.name||f.path)}</button></td><td>${f.type==='directory'?'目录':fmtBytes(f.size)}</td><td><button class="button small" data-browser-properties="${i}">属性</button>${f.type!=='directory'?`<button class="button small" data-browser-text="${i}">读取</button>`:''}</td></tr>`).join('')||'<tr><td colspan="3">没有匹配条目</td></tr>'}</tbody></table></div>`;
      document.querySelector('#browser-pagination').innerHTML=search?`<span>${items.length}项${result.truncated?' · 已达到返回上限':''}</span>`:`<button id="browser-prev" ${current.offset?'':'disabled'}>上一页</button><span>${current.offset+1}–${current.offset+items.length} / ${result.count??items.length}</span><button id="browser-next" ${result.next_offset!=null?'':'disabled'}>下一页</button>`;
      const prev=document.querySelector('#browser-prev'),next=document.querySelector('#browser-next');if(prev)prev.onclick=handle(async()=>{current.offset=Math.max(0,current.offset-200);await refresh();});if(next)next.onclick=handle(async()=>{current.offset=result.next_offset;await refresh();});
    };
    document.querySelector('#browser-result').onclick=handle(async e=>{
      const b=e.target.closest('button');if(!b)return;const index=b.dataset.browserEnter??b.dataset.browserProperties??b.dataset.browserText,item=items[Number(index)];if(!item)return;
      if(b.dataset.browserEnter!=null&&item.type==='directory'){document.querySelector('#browser-path').value=join(item);current.offset=0;await refresh();return;}
      const reading=b.dataset.browserText!=null,r=await api('/api/tools/test','POST',{name:reading?'read_file':'file_info',arguments:{path:join(item)}});
      modal(reading?'只读提取结果':'文件属性',`<p class="field-note">${h(r.method||'属性')} · ${h(join(item))}</p><pre>${h(r.text??JSON.stringify(r,null,2))}</pre><button id="browser-return" class="button">返回目录</button><button id="browser-copy" class="button">复制结果</button>`);
      document.querySelector('#browser-return').onclick=handle(()=>open(tool,current));document.querySelector('#browser-copy').onclick=()=>copy(r.text??JSON.stringify(r,null,2));
    });
    document.querySelector('#browser-open').onclick=handle(async()=>{current.offset=0;await refresh();});
    document.querySelector('#browser-search').onclick=handle(async()=>{if(!document.querySelector('#browser-query').value.trim())throw new Error('请输入文件名或通配符');current.offset=0;await refresh(true);});
    document.querySelector('#browser-up').onclick=handle(async()=>{const p=document.querySelector('#browser-path').value.replace(/\//g,'\\').replace(/\\+$/,'');const root=p.match(/^[a-z]:/i)?.[0],cut=p.lastIndexOf('\\');document.querySelector('#browser-path').value=cut>=0?p.slice(0,cut)+(cut===2?'\\':''):root?root+'\\':p;current.offset=0;await refresh();});
    if(document.querySelector('#browser-pick'))document.querySelector('#browser-pick').onclick=handle(async()=>{const r=await api('/api/ui/pick-directory','POST',{});if(r.path){document.querySelector('#browser-path').value=r.path;current.offset=0;await refresh();}});
    await refresh();
  }
  function settings(){
    const input=document.querySelector('#tool-setting-start_directory');if(!input)return;
    input.insertAdjacentHTML('afterend','<p class="field-note">此处仅设置浏览起点，不是访问白名单；实际操作可填写其他当前账户可访问的完整路径。</p>');
    if(state.actualLocal!==false){const button=document.createElement('button');button.className='button small';button.type='button';button.textContent='选择浏览起点';input.after(button);button.onclick=handle(async()=>{button.disabled=true;try{const value=await api('/api/ui/pick-directory','POST',{});if(value.path)input.value=value.path;}finally{button.disabled=false;}});}
  }
  return Object.freeze({open,toolsHelp,settings});
})();
