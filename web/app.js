"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const h = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  connected: false,
  cfg: {},
  tools: [],
  conversations: [],
  conv: null,
  view: "chat",
  trash: false,
  busy: false,
  attachments: [],
  models: [],
  status: null,
  opts: {},
  controller: null,
  historyPage: 1,
  historyTotal: 0,
  historyPageSize: 20,
  visibleMessages: {},
  lastStats: {},
  streams: {},
};
let scrollMemory = {};
try {
  scrollMemory = JSON.parse(localStorage.getItem("localai-scroll-v1") || "{}");
} catch {}
function scrollKey() {
  return state.view === "chat"
    ? "chat:" + (state.conv?.id || "new")
    : state.view;
}
function saveScroll() {
  const el = state.view === "chat" ? $("#messages") : $("#view-" + state.view);
  if (el) scrollMemory[scrollKey()] = el.scrollTop;
  const keys=Object.keys(scrollMemory);if(keys.length>200)for(const key of keys.slice(0,keys.length-200))delete scrollMemory[key];
  localStorage.setItem("localai-scroll-v1", JSON.stringify(scrollMemory));
}
function restoreScroll() {
  const key = scrollKey();
  const el = state.view === "chat" ? $("#messages") : $("#view-" + state.view);
  if (el && key in scrollMemory)
    requestAnimationFrame(() => {
      el.scrollTop = scrollMemory[key];
    });
}
const modeNames = {
  auto: "自动",
  native: "原生工具调用",
  legacy: "传统 JSON",
  off: "关闭工具",
};
let toastTimer,
  drawTimer,
  liveMessage = null;
let budgetTimer,budgetSequence=0;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const fmtBytes = (n) =>
  n == null
    ? "—"
    : n >= 1073741824
      ? (n / 1073741824).toFixed(1) + " GiB"
      : n >= 1048576
        ? (n / 1048576).toFixed(1) + " MiB"
        : n >= 1024
          ? (n / 1024).toFixed(1) + " KiB"
          : n + " B";
const num = (n, d = 1) =>
  typeof n === "number" && Number.isFinite(n) ? n.toFixed(d) : "—";
// Capacity is always 1K = 1024 tokens; keep exact integers in API/storage.
const fmtK = value => typeof value === 'number' && Number.isFinite(value) ? (value / 1024).toFixed(value < 1024 ? 2 : value % 1024 ? 2 : 0) + 'K' : '—';
const tokenValue = selector => { const el=$(selector); return Math.round(Number(el.value) * (el.dataset.tokenK === 'true' ? 1024 : 1)); };
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 4500);
}
async function api(url, method = "GET", body, raw = false) {
  const options = { method, headers: {} };
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await window.WorkbenchTransport.request(url, options);
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    throw window.WorkbenchTransport.errorFor(response.status, data);
  }
  return raw ? response : response.json();
}
function applyConnection(value) {
  state.connected = true;
  if (typeof value.can_manage === 'boolean') state.canManage = value.can_manage;
  else if (typeof value.local_access === 'boolean') state.canManage = value.local_access;
  if (typeof state.canManage === 'boolean') state.localAccess = state.canManage;
  const actual = value.actual_local ?? value.is_local;
  if (typeof actual === 'boolean') state.actualLocal = actual;
  if (value.auth && typeof value.auth === 'object') state.auth = value.auth;
}
let connectionNoticeTimer;
window.WorkbenchTransport.configure({
  applyConnection,
  onLoginRequired: () => {
    const resume=typeof state.cfg.provider==='string';
    state.connected=false; // Pause read polling until the normal Cookie login succeeds.
    return window.Workbench?.login({resume});
  },
  onState: phase => {
    const note = $('#connection-state'), button = $('#refresh');
    if (!note || !button) return;
    clearTimeout(connectionNoticeTimer);
    note.hidden = false;
    note.textContent = ({checking: '正在检查连接…', connected: '连接正常', error: '连接中断 · 可点刷新', 'login-required': '请重新登录'})[phase] || '';
    button.setAttribute('aria-busy', String(phase === 'checking'));
    if (phase === 'connected') connectionNoticeTimer = setTimeout(() => { note.hidden = true; }, 3500);
  },
});
const reconnectSession = (options = {}) => window.WorkbenchTransport.reconnect(options);
function handle(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (e) {
      toast(e.message || "操作未完成");
      console.error(e);
    }
  };
}
function markdown(text) {
  if(window.workMarkdown)return window.workMarkdown(text);
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(
      marked.parse(text || "", { breaks: true, gfm: true }),
      {
        FORBID_TAGS: ["img", "style", "script", "iframe", "input", "form"],
        FORBID_ATTR: ["style"],
      },
    );
  }
  return "<p>" + h(text).replace(/\n/g, "<br>") + "</p>";
}
function addCopyButtons(root) {
  root.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(".code-copy")) return;
    const b = document.createElement("button");
    b.className = "code-copy";
    b.textContent = "复制代码";
    b.onclick = () =>
      copy(
        pre.querySelector("code")?.textContent ||
          pre.textContent.replace("复制代码", ""),
      );
    pre.prepend(b);
  });
  root.querySelectorAll("a").forEach((a) => {
    if(!a.getAttribute('href')?.startsWith('#'))a.target = "_blank";
    a.rel = "noreferrer noopener";
  });
  root.querySelectorAll('table').forEach(table=>{if(table.parentElement.classList.contains('md-table-wrap'))return;const wrapper=document.createElement('div');wrapper.className='md-table-wrap';table.before(wrapper);wrapper.append(table);const button=document.createElement('button');button.className='button small table-copy';button.textContent='复制表格';button.onclick=()=>copy([...table.rows].map(row=>[...row.cells].map(cell=>cell.textContent).join('\t')).join('\n'));wrapper.prepend(button);});
  if(!state.busy)window.enhanceWorkMarkdown?.(root,state.cfg.mermaid_enabled!==false);
}
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch {
    const t = document.createElement("textarea");
    t.value = text;
    document.body.append(t);
    t.select();
    document.execCommand("copy");
    t.remove();
    toast("已复制");
  }
}
function modal(title, content) {
  $("#modal-title").textContent = title;
  $("#modal-content").innerHTML = content;
  if (!$("#modal").open) $("#modal").showModal();
}
function closeModal() {
  $("#modal").close();
}
function confirmDialog(title, text, button = "确认") {
  return new Promise((resolve) => {
    modal(
      title,
      `<p>${h(text)}</p><div class="flex-row"><button class="button" id="confirm-no">取消</button><button class="button primary" id="confirm-yes">${h(button)}</button></div>`,
    );
    let done = false;
    const settle = (value) => {
      if (done) return;
      done = true;
      $("#modal").removeEventListener("close", cancel);
      closeModal();
      resolve(value);
    };
    const cancel = () => settle(false);
    $("#confirm-yes").onclick = () => settle(true);
    $("#confirm-no").onclick = () => settle(false);
    $("#modal").addEventListener("close", cancel, { once: true });
  });
}
function options() {
  return state.conv?.options || state.opts;
}
function path(conv) {
  if (!conv) return [];
  const by = new Map(conv.messages.map((m) => [m.id, m]));
  const result = [];
  let id = conv.message_page?.window_leaf || conv.leaf;
  while (id && by.has(id)) {
    const m = by.get(id);
    result.push(m);
    id = m.parent;
    if (result.length > 5000) break;
  }
  return result.reverse();
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
}
async function bootstrap() {
  const response = await fetch("/api/bootstrap");
  if(!response.ok)throw new Error('启动读取失败 HTTP '+response.status);
  const result = await response.json();
  if(result.login_required || result.authenticated === false){
    state.auth=result.auth||{};state.canManage=false;state.localAccess=false;
    await window.Workbench.login({resume:state.connected});return;
  }
  window.WorkbenchTransport.acceptBootstrap(result);
  state.cfg = result.settings;
  if(localStorage.getItem('localai-version')!==result.version){scrollMemory={};localStorage.removeItem('localai-scroll-v1');localStorage.setItem('localai-version',result.version);}
  state.tools = result.tools;
  state.canManage=result.can_manage??(result.local_access!==false);
  state.localAccess=state.canManage;state.actualLocal=(result.actual_local??result.is_local??result.local_access)!==false;state.auth=result.auth||{};
  state.opts = {
    provider: state.cfg.provider,
    model: state.cfg.default_model,
    context: state.cfg.context,
    tool_mode: state.cfg.tool_mode,
    tools: [...state.cfg.tools],
    context_policy:state.cfg.context_policy, analysis_mode:false,
    think: state.cfg.think,
    rag_enabled:false,rag_ids:[],rag_mode:'original',skill_ids:[],
  };
  setTheme(state.cfg.theme);
  await refreshHistory();
  await refreshModels();
  renderControls();
  renderMessages();
  await refreshStatus();
  renderApi();
  await window.Workbench?.boot(result);
}
async function refreshHistory() {
  const old = $("#history-list").scrollTop;
  const p = new URLSearchParams({
    page: state.historyPage,
    page_size: state.historyPageSize,
    search: $("#history-search").value,
    trash: state.trash,
  });
  const result = await api("/api/conversations?" + p);
  state.conversations = result.items;
  state.historyPage = result.page;
  state.historyTotal = result.total;
  renderHistory();
  $("#history-list").scrollTop = old;
}
function renderHistory() {
  const rows = state.conversations;
  $("#history-list").innerHTML = rows.length
    ? rows
        .map(
          (c) =>
            `<div class="history-row ${state.conv?.id === c.id ? "selected" : ""}"><button data-open-conv="${h(c.id)}" title="${h(c.title)}">${c.pinned ? "⌑ " : ""}${h(c.title)}</button><button class="icon-btn" data-menu-conv="${h(c.id)}" aria-label="对话菜单">⋯</button></div>`,
        )
        .join("")
    : `<div class="empty-history">${state.trash ? "回收站为空" : "想法从这里开始<br>创建你的第一段对话"}</div>`;
  $("#trash-toggle").style.color = state.trash ? "var(--accent)" : "";
  const pages = Math.max(
    1,
    Math.ceil(state.historyTotal / state.historyPageSize),
  );
  $("#history-pagination").innerHTML =
    `<button data-history-page="${Math.max(1, state.historyPage - 1)}" ${state.historyPage <= 1 ? "disabled" : ""} aria-label="上一页对话">‹</button><span>${state.historyPage} / ${pages} · ${state.historyTotal} 条</span><button data-history-page="${state.historyPage + 1}" ${state.historyPage >= pages ? "disabled" : ""} aria-label="下一页对话">›</button>`;
}
async function openConversation(id) {
  if(window.Workbench?.isPreparing()){toast('正在提交，请稍候');return;}
  saveScroll();
  state.conv = await api("/api/conversations/" + id);
  window.Workbench?.syncCurrent();
  state.attachments = [];
  state.view = "chat";
  showView("chat");
  await refreshModels();
  renderControls();
  renderMessages();
  renderHistory();
  renderAttachments();
}
async function newChat() {
  if(window.Workbench?.isPreparing()){toast('正在提交，请稍候');return;}
  saveScroll();
  state.conv = null;
  state.busy=false;liveMessage=null;
  state.trash = false;
  state.historyPage = 1;
  state.attachments = [];
  state.opts = { ...state.opts,tools:[...state.cfg.tools],rag_enabled:false,rag_ids:[],rag_mode:'original',skill_ids:[] };
  showView("chat");
  renderControls();
  renderMessages();
  await refreshHistory();
  renderAttachments();
  $("#prompt").focus();
}
async function ensureConversation() {
  if (!state.conv) {
    state.conv = await api("/api/conversations", "POST");
    state.conv = await api("/api/conversations/" + state.conv.id, "PATCH", {
      options: state.opts,
    });
    await refreshHistory();
  }
  return state.conv;
}
async function saveOptions(patch) {
  // Running turns keep an immutable server snapshot; changes affect future sends only.
  if (state.conv) {
    state.conv = await api("/api/conversations/" + state.conv.id, "PATCH", {
      options: patch,
    });
  } else Object.assign(state.opts, patch);
  renderControls();
}
async function refreshModels() {
  const id = options().provider || "local";
  try {
    state.models = (
      await api("/api/models?provider=" + encodeURIComponent(id))
    ).models;
  } catch {
    const p = state.cfg.providers.find((p) => p.id === id);
    state.models = (
      id === "local" ? [state.cfg.default_model] : p?.models || []
    ).map((name) => ({ name }));
  }
  const model=options().model;
  try{const caps=await api('/api/models/show','POST',{provider:id,model});if(options().provider===id&&options().model===model){state.modelCaps=caps;state.modelCapsFor={provider:id,model};}}catch{if(options().provider===id&&options().model===model){state.modelCaps=null;state.modelCapsFor=null;}}
  renderControls();
}
function renderControls() {
  const o = options();
  $("#provider-select").innerHTML =
    '<option value="local">本地 Ollama</option>' +
    state.cfg.providers
      .map((p) => `<option value="${h(p.id)}">☁ ${h(p.name)}</option>`)
      .join("");
  $("#provider-select").value = o.provider;
  const modelNames = [
    ...new Set([o.model, ...state.models.map((m) => m.name).filter(name=>!name.startsWith('qwen3-embedding:'))].filter(Boolean)),
  ];
  $("#model-select").innerHTML = modelNames
    .map((m) => `<option value="${h(m)}">${h(m)}</option>`)
    .join("");
  $("#model-select").value = o.model;
  $("#context-select").value = o.context;
  if(![...$('#context-select').options].some(v=>v.value===String(o.context))){const item=new Option(`${num(o.context/1024,2)}K · 自定义`,String(o.context));$('#context-select').add(item);$('#context-select').value=o.context;}
  $('#analysis-toggle').classList.toggle('active',!!o.analysis_mode);
  for(const item of $('#context-select').options){item.disabled=false;item.title=state.modelCaps&&Number(item.value)>state.modelCaps.max_context?'超过模型声明值；按本次设置尝试，实际由服务端决定':'';}
  $("#mode-select").value = o.tool_mode;
  $("#think-toggle").classList.toggle("active", !!o.think);
  $("#privacy-badge").textContent =
    o.provider === "local"
      ? "● 本地运行 · 数据留在本机"
      : "☁ 云端 " +
        (state.cfg.providers.find((p) => p.id === o.provider)?.name ||
          o.provider) +
        " · 消息/附件将外发";
  $("#privacy-badge").classList.toggle("cloud", o.provider !== "local");
  $("#tool-toggles").innerHTML = state.tools
    .filter((t) => t.enabled)
    .map(
      (t) =>
        `<button class="chip ${(o.tools || []).includes(t.id) && o.tool_mode !== "off" ? "active" : ""}" data-toggle-tool="${h(t.id)}" title="${h(t.description)}">${(o.tools || []).includes(t.id) ? "✓" : "＋"} ${h(t.name)}</button>`,
    )
    .join("");
  $("#context-info").textContent =
    `${o.context / 1024}K 上下文 · ${state.cfg.file_read_mode === "office" ? "Office COM 兼容读取" : "Python 直接读取"}`;
  updateBudget();
  window.Workbench?.controls();
}
function showView(view) {
  if (view !== state.view) saveScroll();
  state.view = view;
  $$(".view").forEach((el) => (el.hidden = el.id !== "view-" + view));
  $$(".nav-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.view === view),
  );
  const names = {
    chat: "聊天空间",
    models: "模型中心",
    tools: "工具工作空间",
    api: "API 接入教程",
    settings: "设置",
    guide: "软件教程",
    skills:'技能中心',knowledge:'知识库 RAG',memory:'长期记忆',activity:'任务与访问日志',
  };
  $("#view-title").textContent = names[view];
  $("#view-kicker").textContent = view === "chat" ? "工作空间" : "本地控制台";
  if (view === "models") renderModels();
  if (view === "tools") renderTools();
  if (view === "settings") renderSettings();
  if (view === "guide") renderGuide();
  window.Workbench?.view(view);
  window.WorkbenchV16?.view(view);
  restoreScroll();
}
function renderMessages() {
  window.WorkbenchDetails?.capture($("#messages"));
  window.WorkbenchV16?.outline();
  const messages = path(state.conv);
  if (!messages.length) {
    $("#messages").innerHTML =
      `<div class="welcome"><div class="welcome-orbit">✦</div><div class="eyebrow">本机优先 · 资料与工具</div><h1>本地智能工作台，<span>从一个问题开始。</span></h1><p>直接聊天、添加资料、调用小工具。默认模型已为你选好。</p><div class="suggestions"><button class="suggestion" data-suggest="请用简短的 Markdown 介绍你能帮助我完成哪些简单任务，以及你的局限。"><span class="s-icon">✧</span><strong>试着聊一聊</strong><small>问问题、改写文字、整理思路</small></button><button class="suggestion" data-attach="true"><span class="s-icon">▤</span><strong>添加一份资料</strong><small>文档提取文字，图片可识字和读图</small></button><button class="suggestion" data-suggest="请查询系统时间，告诉我今天的日期和时间。"><span class="s-icon">◷</span><strong>试用一个工具</strong><small>查时间，或查看宿主机目录清单</small></button></div><div class="welcome-note"><i></i>${options().provider === "local" ? "本地运行 · 对话与附件保存在这台电脑" : "云端运行 · 消息与所选附件将发送给当前服务商"}</div></div>`;
    return;
  }
  const count = state.visibleMessages[state.conv.id] || 50;
  const shown = messages.slice(-count);
  $("#messages").innerHTML =
    (state.conv.message_page?.next_offset!=null
      ? `<div class="flex-row" style="justify-content:center"><button class="button small" data-load-older="true">加载更早消息（已载入 ${messages.length}/${state.conv.message_page.total}）</button></div>`
      : "") + shown.map(messageHtml).join("");
  addCopyButtons($("#messages"));
  window.WorkbenchDetails?.apply($("#messages"),state.conv.id);
  const key = "chat:" + state.conv.id;
  if (key in scrollMemory) restoreScroll();
  else $("#messages").scrollTop = $("#messages").scrollHeight;
}
function statsHtml(s) {
  if (!s || !Object.keys(s).length) return "";
  const cps = s.chars_per_second || 0;
  const local = s.provider === "local" || s.source === "Ollama API";
  return `<details data-detail-kind="stats"><summary>${s.complete ? "速度与用量" : "统计不完整"} · ${num(s.tokens_per_second)} Token/s · ${num(s.seconds)}s</summary><div class="stats-grid"><span>输出生成<b>${num(s.tokens_per_second)} Token/s</b></span><span>输入处理<b>${num(s.prompt_tokens_per_second)} Token/s</b></span><span title="精确输入 ${h(s.input_tokens??'未知')} / 输出 ${h(s.output_tokens??'未知')} Token">输入 / 输出容量<b>${fmtK(s.input_tokens)} / ${fmtK(s.output_tokens)}</b></span><span>缓存命中 / 未命中<b>${fmtK(s.cached_input_tokens)} / ${fmtK(s.cache_miss_tokens)}</b></span><span>工具调用<b>${h(s.tool_calls??"—")} / ${h(s.tool_limit??state.cfg.max_tool_calls??50)}</b></span><span>本轮模型请求<b>${h(s.model_requests??"—")}</b></span><span>首段 / 首个正文<b>${num(s.first_chunk)}s / ${num(s.first_text)}s</b></span><span>正文显示速度<b>${num(cps)} 字符/s</b></span><span>模型加载 / 整轮耗时<b>${num(s.load_seconds)}s / ${num(s.seconds)}s</b></span><span>相对手写参考<b>${num((cps * 60) / state.cfg.handwriting_cpm)} ×</b></span><span>相对打字参考<b>${num((cps * 60) / state.cfg.typing_cpm)} ×</b></span><span>统计来源<b>${h(s.source || "未完成")}</b></span></div><p class="field-note">${local ? "生成 Token 可能包括模型思考；字符速度仅描述正文显示节奏。" : "云端 Token/s 使用 usage 与本机计时估算，包含网络/服务端等待和思考。"}手写 ${state.cfg.handwriting_cpm}、打字 ${state.cfg.typing_cpm} 字/分钟为可修改的比较刻度，不表示人群平均值或思考速度。</p></details>`;
}
function toolFilesHtml(event){
  return window.WorkbenchToolUI.filesHtml(event);
}
function baseMessageHtml(m) {
  const toolHtml = window.WorkbenchToolUI.eventsHtml(m.interactions, state.tools, modeNames);
  return `<article class="message ${m.role === "user" ? "user" : ""} ${m.excluded ? "excluded" : ""}" data-message="${h(m.id)}"><div class="avatar ${m.role === "assistant" ? "ai" : ""}">${m.role === "user" ? "你" : "✦"}</div><div><div class="message-label">${m.role === "user" ? "你" : "工作台助手"}<small>${h(m.model || "")}${m.mode ? " · " + h(modeNames[m.mode] || m.mode) : ""}${m.excluded ? " · 已排除后续上下文" : ""}</small></div>${m.thinking ? `<details data-detail-kind="thinking"><summary>思考过程</summary><div class="thinking">${h(m.thinking)}</div></details>` : ""}<div class="message-runtime">${window.WorkbenchToolUI.runtimeHtml(m.runtime_state)}</div><div class="message-tools">${toolHtml}</div><div class="message-body"><div class="message-content">${markdown(m.content)}${state.busy && liveMessage?.id === m.id ? '<i class="cursor"></i>' : ""}</div></div>${m.error ? `<div class="error-note">${h(m.error)}</div>` : ""}<div class="message-attachments">${(m.attachments || []).map((a) => `<button class="chip" data-view-attachment="${h(a.id)}">▤ ${h(a.name)} · ${a.mode === "image" ? "原图" : a.mode === "text" ? "文字" : "图+文"}</button>`).join("")}</div>${statsHtml(m.stats)}<div class="message-actions"><button data-msg-action="copy" data-id="${m.id}">复制</button>${m.role === "user" ? `<button data-msg-action="edit" data-id="${m.id}">编辑并分支</button>` : `<button data-msg-action="regenerate" data-id="${m.id}">重新生成</button>`}<button data-msg-action="exclude" data-id="${m.id}">${m.excluded ? "恢复上下文" : "排除上下文"}</button><button data-msg-action="truncate" data-id="${m.id}">删除至此分支</button></div></div></article>`;
}
function renderLive() {
  if (!liveMessage || state.conv?.message_page?.around) return;
  const old = $(`[data-message="${liveMessage.id}"]`);
  if (old) {
    window.WorkbenchDetails?.capture(old);
    const bottom = window.Workbench ? window.Workbench.following() :
      $("#messages").scrollHeight -
        $("#messages").scrollTop -
        $("#messages").clientHeight <
      150;
    const content=old.querySelector('.message-content');
    if(!content.querySelector('[data-stream-prefix]'))content.innerHTML='<div data-stream-prefix></div><div data-stream-tail></div><i class="cursor"></i>';
    const text=liveMessage.content||'';
    let boundary=text.lastIndexOf('\n\n');
    const before=boundary<0?'':text.slice(0,boundary);
    if((before.match(/^```/gm)||[]).length%2)boundary=-1;
    const prefix=boundary<0?'':text.slice(0,boundary+2),tail=boundary<0?text:text.slice(boundary+2);
    const prefixEl=content.querySelector('[data-stream-prefix]');
    if(prefixEl.dataset.length!==String(prefix.length)){prefixEl.innerHTML=markdown(prefix);prefixEl.dataset.length=String(prefix.length);addCopyButtons(prefixEl);}
    content.querySelector('[data-stream-tail]').innerHTML=markdown(tail);
    if(liveMessage.thinking){let thinking=old.querySelector('.thinking');if(!thinking){const details=document.createElement('details');details.dataset.detailKind='thinking';details.innerHTML='<summary>思考过程</summary><div class="thinking"></div>';content.closest('.message-body').before(details);thinking=details.querySelector('.thinking');}thinking.textContent=liveMessage.thinking;}
    const runtime=old.querySelector('.message-runtime');
    if(runtime)runtime.innerHTML=window.WorkbenchToolUI.runtimeHtml(liveMessage.runtime_state);
    const events=old.querySelector('.message-tools');
    if(events)window.WorkbenchToolUI.sync(events,liveMessage.interactions,state.tools,modeNames);
    window.WorkbenchDetails?.apply(old,state.conv?.id);
    if (bottom) $("#messages").scrollTop = $("#messages").scrollHeight;
  }
}
function scheduleDraw() {
  if (!drawTimer)
    drawTimer = setTimeout(() => {
      drawTimer = null;
      renderLive();
    }, 180);
}
async function cloudConsent() {
  // The user requested a persistent, low-key disclosure rather than a blocking dialog.
  renderControls();
  return true;
}
async function send(regenerate = false) {
  return window.Workbench.send(regenerate);
}
async function stop() {
  if(window.Workbench?.stopCurrent)return window.Workbench.stopCurrent();
  if (state.conv) {
    const entry=state.streams[state.conv.id];
    if(entry?.status==='submitting')entry.controller.abort();
    await api("/api/chat/" + state.conv.id + "/stop", "POST");
    toast("正在停止生成…");
  }
}
async function uploadFiles(files) {
  const limit = state.cfg.max_attachments || 5;
  for (const file of files) {
    if (state.attachments.length >= limit) {
      toast("每条消息最多 " + limit + " 个附件");
      break;
    }
    const form = new FormData();
    form.append("file", file);
    const item = await api(
      "/api/attachments?read_mode=" +
        encodeURIComponent(state.cfg.file_read_mode || "native"),
      "POST",
      form,
    );
    item.mode = item.image ? "both" : "text";
    state.attachments.push(item);
    renderAttachments();
    pollAttachment(item.id);
  }
}
async function pollAttachment(id) {
  for (let i = 0; i < 180; i++) {
    await wait(700);
    const index = state.attachments.findIndex((a) => a.id === id);
    if (index < 0) return;
    const item = await api("/api/attachments/" + id);
    Object.assign(state.attachments[index], item);
    renderAttachments();
    if (item.status !== "processing") {
      if (item.error) toast(item.error);
      return;
    }
  }
  toast("附件处理超时，可查看状态或移除后重试");
}
function renderAttachments() {
  $("#attachment-tray").innerHTML = state.attachments
    .map(
      (a) =>
        `<div class="attachment-card ${a.status === "error" ? "error" : ""}"><button data-view-attachment="${h(a.id)}">${a.status === "processing" ? '<span class="spinner"></span>' : a.image ? "▧" : "▤"}</button><button data-view-attachment="${h(a.id)}"><b>${h(a.name)}</b><small>${a.status === "processing" ? "自动处理中…" : a.status === "error" ? "读取失败，查看原因" : (a.injectText ?? a.text ?? "").length + " 字符 · " + (a.image ? (a.mode === "text" ? "仅文字" : a.mode === "image" ? "仅原图" : "原图 + OCR") : "文字已附加")}</small></button><button class="icon-btn" data-remove-attachment="${h(a.id)}" aria-label="移除附件">×</button></div>`,
    )
    .join("");
  updateBudget();
}
function updateBudget() {
  if(!state.connected)return;
  clearTimeout(budgetTimer);
  budgetTimer=setTimeout(async()=>{try{const sequence=++budgetSequence;const report=await api('/api/context/preview','POST',{conversation_id:state.conv?.id,options:options(),content:$('#prompt').value,attachments:state.attachments.filter(a=>a.status==='ready').map(a=>({id:a.id,text:a.injectText??a.text,mode:a.mode||'both'}))});if(sequence!==budgetSequence)return;showContextReport(report);}catch{}},300);
}

function showContextReport(report){
  $('#context-info').textContent=`预计输入 ${fmtK(report.input_tokens)} / ${fmtK(report.context)} · 回答预留 ${fmtK(report.output_tokens)} · 安全余量 ${fmtK(report.safety_tokens)}`;
  $('#context-info').title=`1K = 1024 Token；精确输入 ${report.input_tokens}，容量 ${report.context}，回答 ${report.output_tokens}，余量 ${report.safety_tokens}。${report.source||'统一保守估算'}；待发消息按当前已完成历史预估，实际执行时会重新计算。`;
  $('.composer-meta').classList.toggle('warning',report.total>report.context*.8);
  const notes=(report.attachments||[]).filter(a=>a.truncated).map(a=>`${a.name}：本次送入约 ${Math.round(a.ratio*100)}%，其余未参与回答`);
  if(report.state?.covered_messages)notes.unshift(`${report.state.policy==='summary'?'已摘要':'已截断'}旧历史 ${report.state.covered_messages} 条，原记录保留`);
  else if(report.total>report.context*.8)notes.unshift(report.policy==='manual'?'接近容量，请手动整理':`接近容量，将自动${report.policy==='truncate'?'截断':'摘要'}旧历史`);
  $('#context-notice').hidden=!notes.length;$('#context-notice').textContent=notes.join('；');
}

function messageHtml(message){
  let extra='';const report=message.context_report;
  if(message.steering?.length)extra+=`<details class="context-event" data-detail-kind="steering"><summary>本轮方向调整 · ${message.steering.length} 条</summary>${message.steering.map(item=>`<p><b>${item.status==='applied'?'已应用':'等待应用'}</b> · ${h(item.content)}</p>`).join('')}</details>`;
  if(message.retrieval)extra+=window.WorkbenchRetrieval.card(message.retrieval,message.id);
  if(report?.state?.covered_messages)extra+=`<details class="context-event" data-detail-kind="context"><summary>${report.state.policy==='summary'?'历史已压缩为摘要':'旧历史已截断'} · ${report.state.covered_messages} 条 · 原记录保留</summary><pre>${h(report.state.summary||'本次只带近期历史，未删除原记录。')}</pre></details>`;
  const refs=(message.attachments||[]).filter(a=>a.injection?.truncated);
  if(refs.length)extra+=`<div class="context-notice">${refs.map(a=>`${h(a.name)}：本次送入 ${Math.round(a.injection.ratio*100)}%，未送入部分未参与回答`).join('<br>')}</div>`;
  if(message.attempts?.length)extra+=`<details class="context-event" data-detail-kind="recovery"><summary>故障恢复记录 · ${message.attempts.length} 次</summary>${message.attempts.map(a=>`<pre>${h(a.content||a.note)}</pre>`).join('')}</details>`;
  return baseMessageHtml(message).replace('<div class="message-body">',extra+'<div class="message-body">').replaceAll('data-view-attachment=',`data-message-ref="${h(message.id)}" data-view-attachment=`);
}
async function viewAttachment(id, messageId) {
  const pending = state.attachments.find((a) => a.id === id);
  const item = pending || (await api(messageId&&state.conv?`/api/conversations/${state.conv.id}/messages/${messageId}/attachments/${id}`:"/api/attachments/" + id));
  modal(
    item.name,
    `${item.image ? '<div id="image-preview"></div>' : ""}<p class="field-note">${h(item.method || "处理中")} · ${fmtBytes(item.size)} ${item.confidence ? " · OCR 置信度 " + num(item.confidence * 100) + "%" : ""}</p>${item.error ? `<div class="error-note">${h(item.error)}</div>` : ""}${item.warning ? `<div class="notice">${h(item.warning)}</div>` : ""}${pending && item.image ? `<label>发送方式<select id="attachment-mode"><option value="both">原图 + OCR 文字</option><option value="image">仅原图读图</option><option value="text">仅 OCR 文字</option></select></label>` : ""}<textarea id="attachment-text" ${pending ? "" : "readonly"} aria-label="附件提取文字">${h(item.injectText ?? item.text ?? "")}</textarea><p class="field-note">可编辑或选取要注入的文字；原文件保留。OCR 文字可能存在识别误差。</p><div class="flex-row">${pending ? '<button id="summarize-attachment" class="button">全文分段摘要</button><button id="save-attachment" class="button primary">保存附加内容</button>' : '<button class="button" data-close-modal>关闭</button>'}</div>`,
  );
  if (item.image) {
    try {
      const r = await api(
        "/api/attachments/" + id + "/image",
        "GET",
        undefined,
        true,
      );
      const url = URL.createObjectURL(await r.blob());
      const image = document.createElement("img");
      image.src = url;
      $("#image-preview").append(image);
      image.onload = () => URL.revokeObjectURL(url);
    } catch {}
  }
  if (pending) {
    if (item.image) $("#attachment-mode").value = pending.mode || "both";
    $("#save-attachment").onclick = () => {
      pending.injectText = $("#attachment-text").value;
      if (item.image) pending.mode = $("#attachment-mode").value;
      renderAttachments();
      closeModal();
    };
    $("#summarize-attachment").onclick = handle(async () => {
      if (!(await cloudConsent())) return;
      const button=$("#summarize-attachment"),editor=$("#attachment-text");
      button.disabled=true;
      try{
        const result=await api("/api/attachments/"+id+"/summary","POST",{provider:options().provider,model:options().model,cloud_confirmed:options().provider!=="local"});
        editor.value="[模型摘要 · "+result.coverage+" · "+result.model+"]\n"+result.text;
        toast("摘要完成，点击保存后注入");
      }finally{button.disabled=false;}
    });
  }
  window.Workbench?.attachmentPreview(item,pending);
}
async function messageAction(action, id) {
  if (state.busy) {
    toast("请先停止生成");
    return;
  }
  const message = state.conv.messages.find((m) => m.id === id);
  if (action === "copy") {
    await copy(message.content);
    return;
  }
  if (action === "edit") {
    modal(
      "编辑消息 · 创建新分支",
      `<p class="subtle">原消息与后续回答保留，可从会话菜单切换历史分支。</p><textarea id="edit-message">${h(message.content)}</textarea><div class="flex-row"><button class="button primary" id="apply-edit">保存并重新回答</button></div>`,
    );
    $("#apply-edit").onclick = handle(async () => {
      state.conv = await api(
        "/api/conversations/" + state.conv.id + "/message-action",
        "POST",
        { action, message_id: id, content: $("#edit-message").value },
      );
      closeModal();
      renderMessages();
      await send(true);
    });
    return;
  }
  if (
    action === "truncate" &&
    !(await confirmDialog(
      "删除当前后续分支",
      "当前视图将退回到这条消息之前，旧分支保留，可在会话菜单恢复。",
      "退回到此处",
    ))
  )
    return;
  state.conv = await api(
    "/api/conversations/" + state.conv.id + "/message-action",
    "POST",
    { action, message_id: id },
  );
  renderMessages();
  if (action === "regenerate") await send(true);
}
async function conversationMenu(id) {
  const conv = await api("/api/conversations/" + id);
  const branchPage=await api('/api/conversations/'+id+'/branches');
  const branches=branchPage.items;
  modal(
    "对话设置",
    `<label>对话名称<input id="conv-name" value="${h(conv.title)}"></label><div class="flex-row"><button class="button" id="rename-conv">保存名称</button><button class="button" id="pin-conv">${conv.pinned ? "取消置顶" : "置顶对话"}</button><button class="button" id="export-md">导出 MD</button><button class="button" id="export-json">导出 JSON</button></div>${branches.length ? `<label style="margin-top:20px">历史消息 / 分支<select id="branch-select">${branches.map((m) => `<option value="${m.id}" ${conv.leaf === m.id ? "selected" : ""}>${m.role === "user" ? "你" : "助手"} · ${h(m.content.slice(0, 48) || "工具/思考回复")}</option>`).join("")}</select></label><div class="flex-row"><button id="switch-branch" class="button">切换到选定分支</button></div>` : ""}<div class="flex-row"><button class="button danger" id="trash-conv">${conv.deleted ? "恢复对话" : "移至回收站"}</button>${conv.deleted ? '<button class="button danger" id="purge-conv">永久删除</button>' : ""}</div>`,
  );
  $("#rename-conv").onclick = handle(async () => {
    await api("/api/conversations/" + id, "PATCH", {
      title: $("#conv-name").value || "未命名对话",
    });
    closeModal();
    await refreshHistory();
  });
  $("#pin-conv").onclick = handle(async () => {
    await api("/api/conversations/" + id, "PATCH", { pinned: !conv.pinned });
    closeModal();
    await refreshHistory();
  });
  $("#trash-conv").onclick = handle(async () => {
    await api("/api/conversations/" + id, "PATCH", { deleted: !conv.deleted });
    if (state.conv?.id === id) state.conv = null;
    closeModal();
    await refreshHistory();
    renderMessages();
  });
  if ($("#purge-conv"))
    $("#purge-conv").onclick = handle(async () => {
      if (
        !(await confirmDialog(
          "永久删除",
          "永久移除此对话；已导出的文件不会删除。",
          "删除",
        ))
      )
        return;
      await api("/api/conversations/" + id, "DELETE");
      if (state.conv?.id === id) state.conv = null;
      closeModal();
      await refreshHistory();
      renderMessages();
    });
  $("#export-md").onclick = handle(() =>
    download(
      "/api/conversations/" + id + "/export?format=md",
      conv.title + ".md",
    ),
  );
  $("#export-json").onclick = handle(() =>
    download(
      "/api/conversations/" + id + "/export?format=json",
      conv.title + ".json",
    ),
  );
  if ($("#switch-branch"))
    $("#switch-branch").onclick = handle(async () => {
      const leaf = $("#branch-select").value;
      await api("/api/conversations/" + id, "PATCH", { leaf });
      closeModal();
      await openConversation(id);
    });
}
async function download(url, name) {
  const response = await api(url, "GET", undefined, true);
  const object = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = object;
  a.download = name.replace(/[\\/:*?"<>|]/g, "_");
  a.click();
  setTimeout(() => URL.revokeObjectURL(object), 1000);
}

async function refreshStatus() {
  if(!state.connected)return;
  try {
    state.status = await api("/api/status");
    $("#status-dot").classList.toggle("online", state.status.online);
    $("#status-label").textContent = state.status.online
      ? "Ollama " + state.status.version + " · 已连接"
      : "Ollama 未连接";
    if (state.view === "models") renderModels();
  } catch {
    $("#status-label").textContent = "服务连接中断";
  }
}
function hardwareCards() {
  const hw = state.status?.hardware || {};
  const gpu = hw.gpus?.[0];
  const loaded = state.status?.loaded || [];
  return `<div class="metrics"><div class="metric-card"><label>图形处理器</label><strong style="font-size:16px">${h(gpu?.name || "未获取安装信息")}</strong><small>${gpu ? `专用总显存 ${num(gpu.total_mb / 1024)} GiB · ${gpu.free_mb==null?'实时监测默认关闭':`可用 ${num(gpu.free_mb/1024)} GiB · 利用率 ${num(gpu.utilization,0)}%`}` : "GPU 运行情况以实际模型加载为准"}</small></div><div class="metric-card"><label>系统物理内存 / 可用</label><strong>${fmtBytes(hw.ram_total)} <span class="subtle">/ ${fmtBytes(hw.ram_available)}</span></strong><small>CPU ${num(hw.cpu_percent,0)}% · 磁盘可用 ${fmtBytes(hw.disk_free)}</small></div><div class="metric-card"><label>系统已提交 / 提交上限</label><strong>${fmtBytes(hw.commit_used)} <span class="subtle">/ ${fmtBytes(hw.commit_limit)}</span></strong><small>剩余提交额度 ${fmtBytes(hw.commit_available)}；不是物理内存占用</small></div><div class="metric-card"><label>已加载聊天模型</label><strong>${loaded.length} <span class="subtle">个模型</span></strong><small>${loaded.map(m=>`${h(m.name)} · ${m.size_vram?Math.min(100,Math.round(m.size_vram/m.size*100))+'% GPU 加载':'CPU 加载'}`).join('<br>')||'按需加载，未使用时可卸载'}</small></div></div>`;
}
function renderBaseModels() {
  const installed = state.status?.models || [];
  const presets = [
    {
      n: "2b",
      size: "2.7–3.1 GB",
      desc: "例：把一小段文件清单按类型列成表",
      vram: 6,
      can: "把给出的 10 个文件名分成“图片/文档”；把一段说明改写得更简短。",
      limit:
        "内容一多就可能漏项；不适合长资料、多步骤比较或稳定的连续工具调用。",
    },
    {
      n: "4b",
      size: "3.3–4.0 GB",
      desc: "例：找文件、比较两段短文的不同",
      vram: 8,
      can: "借助只读工具找含指定名称的文件，按日期列清单；比较两段短说明的不同，或读一张截图。",
      limit:
        "多份长资料、复杂计算、深层原因判断容易遗漏或猜测；工具结果也要复核。",
    },
    {
      n: "9b",
      size: "6.6–7.6 GB",
      desc: "例：对照几份资料，列出修改点",
      vram: 12,
      can: "对比几份较短说明，整理新增/删除/变化；结合目录属性归纳文件版本线索。",
      limit:
        "文件名和时间只能提供线索，无法据此确认正文版本；复杂跨文档推理仍可能编造。",
    },
    {
      n: "27b",
      size: "17–20 GB",
      desc: "例：多步骤分析、比较方案与约束",
      vram: 28,
      can: "尝试综合更多资料，分步骤比较方案，指出差异、依据和待确认条件；也可解释较复杂代码。",
      limit:
        "仍会算错、漏条件或编造事实；不能替代专业审核，普通电脑运行可能较慢。",
    },
  ];
  $("#view-models").innerHTML =
    `<div class="page-heading"><div><div class="eyebrow">模型与资源</div><h1>给想法，选一个合适的大脑。</h1><p>管理本地模型与资源，了解你的电脑适合运行什么。</p></div><a class="button" href="https://ollama.com/search" target="_blank" rel="noreferrer">官方模型库 ↗</a></div>${hardwareCards()}<h2 class="section-title">Qwen 3.5 精选 <span>文本 + 视觉 · 四档预设</span></h2><div class="cards">${presets
      .map((p) => {
        const name = "qwen3.5:" + p.n;
        const ready = installed.some((m) => m.name === name);
        const gpu = state.status?.hardware.gpus?.[0];
        return `<div class="card model-card ${p.n === "4b" ? "featured" : ""}"><div class="flex-row"><span class="eyebrow">QWEN 3.5</span>${p.n === "4b" ? '<span class="tag accent">默认体验</span>' : ""}</div><div class="model-number">${p.n.toUpperCase().replace("B", "")}<small> B</small></div><p>${p.desc}</p><span class="subtle">下载参考 ${p.size}<br>8K 上下文显存建议 ≥ ${p.vram} GiB</span><span class="tag ${ready ? "success" : ""}">${ready ? "已安装" : gpu && gpu.free_mb / 1024 >= p.vram ? "预计适合 GPU" : "可能需混合/CPU · 待实测"}</span><button class="button ${p.n === "4b" ? "primary" : ""}" ${ready ? "data-use-model" : "data-pull-model"}="${name}">${ready ? "开始对话" : "下载模型 ↓"}</button></div>`;
      })
      .join(
        "",
      )}</div><div class="notice">显存建议是保守参考，实际结果由模型、上下文、图像与驱动决定。GPU 加载比例与 GPU 利用率是不同指标；速度/质量标签为使用建议。</div><h2 class="section-title">下载模型 <span>支持官方名称、命令和模型页面链接</span></h2><div class="download-input"><input id="model-download-name" placeholder="qwen3.5:4b 或 https://ollama.com/library/…"><button class="button primary" id="download-model">开始下载</button></div><div id="download-jobs"></div><h2 class="section-title">已安装模型 <span>${installed.length} 个</span></h2><div class="table-wrap"><table class="data-table"><thead><tr><th>模型名称</th><th>磁盘逻辑大小</th><th>量化 / 参数</th><th>管理</th></tr></thead><tbody>${installed.length ? installed.map((m) => `<tr><td>${h(m.name)}</td><td>${fmtBytes(m.size)}</td><td>${h(m.details?.quantization_level || "—")} / ${h(m.details?.parameter_size || "—")}</td><td><button class="button small" data-model-detail="${h(m.name)}">详情</button> <button class="button small" data-unload-model="${h(m.name)}">卸载</button> <button class="button small danger" data-delete-model="${h(m.name)}">删除</button></td></tr>`).join("") : '<tr><td colspan="4" class="inline-empty">暂无本地模型，预下载完成后可直接体验。</td></tr>'}</tbody></table></div><div class="notice">模型目录：${h(state.cfg.root)}/models<br>卸载释放内存/显存；删除移除磁盘模型，共享层可能使实际释放空间小于逻辑大小。</div><button class="button" id="start-service">检测 / 启动 Ollama 服务</button>`;
  $("#download-model").onclick = handle(() =>
    pullModel($("#model-download-name").value),
  );
  $("#start-service").onclick = handle(async () => {
    await api("/api/service/start", "POST");
    toast("已请求启动，稍后刷新");
    setTimeout(refreshStatus, 2000);
  });
  refreshDownloads();
  $("#view-models h1").textContent = "选择模型，先看它能帮你做什么。";
  $$("#view-models .model-card").forEach((card, index) => {
    const p = presets[index];
    const section = document.createElement("div");
    section.className = "model-task-info";
    section.innerHTML = `<b>可以这样试</b><p>${h(p.can)}</p><b>容易出错的地方</b><p>${h(p.limit)}</p>`;
    card.querySelector(".subtle").before(section);
  });
  const note = document.createElement("p");
  note.className = "field-note";
  note.textContent =
    "以上是任务难度的使用参考，未作为正式能力跑分。目录整理仅生成清单；当前工具不会移动、重命名或修改文件。模型不能根据清单得知文件正文。";
  $("#view-models .cards").after(note);
  restoreScroll();
  if (state.status?.online && state.status.service?.managed === false) {
    const warning = document.createElement("div");
    warning.className = "notice";
    warning.textContent =
      "当前连接的是外部启动的 Ollama，无法确认它的模型存储目录。若预置 4B 未出现，请从 Ollama 托盘退出后，再点本页“检测 / 启动”。本工作台不会擅自终止或覆盖外部服务。";
    $("#view-models .page-heading").after(warning);
  }
  if (
    state.status?.service?.unicode_runtime_path &&
    state.status.version === "0.40.8"
  ) {
    const warning = document.createElement("div");
    warning.className = "notice";
    warning.textContent =
      "Ollama 本体位于含中文/非英文的程序路径。此版本在本次实测中可能无法加载 GPU 运行库而改用 CPU，建议将程序安装到英文目录，例如 D:\\LocalAIStudio。模型与资料文件名仍可使用中文。";
    $("#view-models .page-heading").after(warning);
  }
}
async function pullModel(model) {
  await api("/api/downloads", "POST", { model });
  toast("下载任务已创建");
  refreshDownloads();
}
function renderModels(){
  renderBaseModels();
  for(const card of $$('#view-models .model-card')){
    const button=card.querySelector('[data-use-model],[data-pull-model]');const model=button?.dataset.useModel||button?.dataset.pullModel;
    const featured=model===state.cfg.default_model;card.classList.toggle('featured',featured);button?.classList.toggle('primary',featured);
    const label=card.querySelector('.tag.accent');if(label)label.remove();
    if(featured){const tag=document.createElement('span');tag.className='tag accent';tag.textContent='当前默认 · 首次硬件选型';card.querySelector('.flex-row').append(tag);}
  }
  const note=document.createElement('div');note.className='notice';note.textContent='离线包保留聊天 9B 与 4B，另含检索用 0.6B 嵌入模型。首次：单张独显专用总显存 >10 GiB 优先 9B，否则 4B；共享显存不计，不进行启动试载。'+(state.cfg.first_start?.reason||'等待安装信息判断。');$('#view-models .page-heading').after(note);
}

async function renderTools(){
  await renderBaseTools();
  window.WorkbenchV16?.tools();
  const notice=$('#view-tools .notice');if(notice)notice.textContent='工具在当前Windows账户权限下执行。只读、新建、编辑、回收站与网络能力在卡片标明；配置集中于各工具。所有模型使用统一工具和额度，执行记录可在任务栏查看。';
  for(const card of $$('#view-tools .tool-card')){
    const id=card.querySelector('[data-test-tool]')?.dataset.testTool;if(id==='python_run'){
      card.classList.add('python-tool-card');const tag=card.querySelector('.tag');tag.textContent=state.tools.find(t=>t.id===id)?.enabled?'可信执行 · 已启用':'可信执行 · 默认关闭';
    }
  }
  const grid=$('#view-tools .tools-grid');if(grid){const cards=[...grid.querySelectorAll('.tool-card')],groups=[['基础信息',['system_time']],['文件与附件',['read_attachment','list_directory','search_files','file_info','read_file','text_create','file_manage']],['Office 与网络',['office_operator','http_request']],['计算与编程',['python_run']],['扩展插件',[]]];const used=new Set();for(const [name,ids] of groups){const matches=cards.filter(card=>{const id=card.querySelector('[data-test-tool]')?.dataset.testTool;return !used.has(card)&&(ids.length?ids.includes(id):true);});if(!matches.length)continue;const heading=document.createElement('h3');heading.className='tool-workspace-group';heading.textContent=name;grid.append(heading);for(const card of matches){grid.append(card);used.add(card);}}}
}

async function pythonLabDialog(page=1){
  const conf=await api('/api/tools/python_run/settings');const history=await api('/api/python/runs?page='+page);
  state.labDraft??='import statistics\nvalues = [10.02, 9.98, 10.00]\nprint("平均值", statistics.mean(values))\nprint("极差", round(max(values) - min(values), 3))';
  modal('Python 小实验 · 受信任模式',`<div class="notice">正常 Python，不是安全沙箱。代码以当前 Windows 用户权限运行。仅输入明确授权的数据；本次工作目录独立，不会自动传入工程资料或 API Key。${conf.values.enabled?'已启用，可被聊天调用。':'当前未启用，请先进入工具配置了解并启用。'}</div><button id="lab-config" class="button small">工具配置 / 启用</button><label>代码<textarea id="lab-code" class="code-editor" spellcheck="false">${h(state.labDraft)}</textarea></label><label>标准输入（供 input() 逐行读取）<textarea id="lab-stdin" rows="2">${h(state.labInput||'')}</textarea></label><div class="flex-row"><button id="lab-run" class="button primary" ${conf.values.enabled?'':'disabled'}>运行代码</button><button id="lab-stop" class="button danger">停止实验</button><span class="subtle">默认超时 ${conf.values.timeout} 秒 · 输出 ${conf.values.output_kb} KiB</span></div><pre id="lab-result">输出和错误会显示在这里。</pre><h3>最近实验 · 自动清理 / 分页</h3><div id="lab-history">${history.items.map(r=>`<button class="button small" data-lab-id="${r.id}">${r.status==='finished'?'✓':'!'} ${h(new Date(r.created*1000).toLocaleString())} · ${r.seconds??'运行中'}s</button>`).join(' ')||'<p class="subtle">暂无实验记录</p>'}</div><div class="flex-row"><button id="lab-prev" class="button small" ${page>1?'':'disabled'}>上一页</button><span>${history.page} / ${Math.max(1,Math.ceil(history.total/history.page_size))} · 共 ${history.total} 次</span><button id="lab-next" class="button small" ${page*history.page_size<history.total?'':'disabled'}>下一页</button></div><div id="lab-files"></div>`);
  $('#lab-code').oninput=()=>{state.labDraft=$('#lab-code').value;};$('#lab-stdin').oninput=()=>{state.labInput=$('#lab-stdin').value;};
  $('#lab-config').onclick=()=>toolSettingsDialog('python_run');
  const show=result=>{if(!$('#lab-result'))return;$('#lab-result').textContent=JSON.stringify({status:result.status,stdout:result.stdout,stderr:result.stderr,exit_code:result.exit_code,seconds:result.seconds,note:result.note},null,2);$('#lab-files').innerHTML=(result.files||[]).map(f=>`<button class="button small" data-lab-file="${h(f.name)}">保存 ${h(f.name)} · ${fmtBytes(f.bytes)}</button>`).join('');$('#lab-files').onclick=handle(async event=>{const b=event.target.closest('[data-lab-file]');if(b)await download('/api/python/runs/'+result.id+'/file?name='+encodeURIComponent(b.dataset.labFile),b.dataset.labFile);});};
  $('#lab-run').onclick=handle(async()=>{const button=$('#lab-run');button.disabled=true;$('#lab-result').textContent='正在执行…';try{const result=await api('/api/tools/test','POST',{name:'python_run',arguments:{code:$('#lab-code').value,stdin:$('#lab-stdin').value}});show(result);}finally{button.disabled=false;}});
  $('#lab-stop').onclick=handle(async()=>{await api('/api/python/stop','POST',{});toast('已请求停止实验');});
  $('#lab-history').onclick=event=>{const id=event.target.closest('[data-lab-id]')?.dataset.labId;const record=history.items.find(r=>r.id===id);if(record){state.labDraft=record.code;state.labInput=record.stdin;$('#lab-code').value=record.code;$('#lab-stdin').value=record.stdin;show(record);}};
  $('#lab-prev').onclick=()=>pythonLabDialog(page-1);$('#lab-next').onclick=()=>pythonLabDialog(page+1);
}
async function refreshDownloads() {
  if (state.view !== "models") return;
  try {
    const jobs = await api("/api/downloads");
    const el = $("#download-jobs");
    if (!el) return;
    el.innerHTML = window.WorkbenchDownloads.render(jobs);
  } catch {}
}
async function modelDetail(model) {
  if(window.Workbench)return window.Workbench.modelDetail(model);
  const info = await api("/api/models/show", "POST", { model });
  modal(
    model,
    `<div class="notice">模型声明容量：${info.max_context/1024}K${info.context_verified===false?'（未声明，保守限制）':''}；当前设置 ${options().context/1024}K。模型支持上限不等于本机稳定容量。9B/4B 官方原生上限 256K，不自动启用扩展容量。</div><details><summary>完整元数据与参数</summary><pre>${h(JSON.stringify(info.details || info, null, 2))}</pre></details>`,
  );
}
async function renderBaseTools() {
  state.tools = await api("/api/tools");
  $("#view-tools").innerHTML =
    `<div class="page-heading"><div><div class="eyebrow">工具工作空间</div><h1>小工具，让对话多一点能力。</h1><p>查询时间、查找文件、读取资料；按需选择交给模型的工具。</p></div><div class="flex-row"><button id="example-plugin" class="button">下载时间工具示例</button><button id="import-plugin" class="button primary">＋ 导入工具 ZIP</button></div></div><div class="notice">内置工具均为只读，不会移动、修改或重命名文件。目录工具默认没有访问权限；先点工具卡片的“配置”，选择允许读取的文件夹。导入的 Python 插件不是权限沙箱，仅启用已核对的可信代码。</div><div class="tools-grid">${state.tools.map((t) => `<div class="card tool-card"><div class="tool-icon">${t.id.includes("time") ? "◷" : "◇"}</div><div class="flex-row" style="justify-content:space-between"><h2>${h(t.name)}</h2><span class="tag ${t.enabled ? "success" : ""}">${t.builtin ? "内置工具" : t.enabled ? "已启用" : "已导入 · 未启用"}</span></div><p>${h(t.description)}</p><div class="subtle" style="margin-top:8px">${h(t.id)} · ${h(t.version || "1.0")}<br>权限：${h((t.permissions || []).join(", "))}</div><div class="flex-row"><button class="button small" data-test-tool="${h(t.id)}">${t.id === "system_time" ? "测试" : "打开 / 测试"}</button><button class="button small" data-tool-settings="${h(t.id)}">配置</button>${!t.builtin ? `<button class="button small" data-source-tool="${h(t.id)}">查看源码</button><button class="button small ${t.enabled ? "" : "primary"}" data-enable-plugin="${h(t.id)}">${t.enabled ? "停用" : "启用"}</button><button class="button small danger" data-remove-plugin="${h(t.id)}">移除</button>` : ""}</div></div>`).join("")}</div><div class="notice">目录清单、文件属性、文件名搜索和文件读取共用允许目录。搜索默认限于当前目录及下一级；搜索深度在“搜索文件名 → 配置”中修改。Office COM / Python 读取方式在“只读文件读取 → 配置”中切换。</div>`;
  $("#example-plugin").onclick = handle(() =>
    download("/api/example-plugin", "time_demo.zip"),
  );
  $("#import-plugin").onclick = () => $("#plugin-input").click();
  renderControls();
  restoreScroll();
  window.Workbench?.toolsHelp();
}
async function testTool(name) {
  if(name==='python_run'){await pythonLabDialog();return;}
  if(name==='read_attachment'){modal('读取本次附件', '<p>此工具只读取用户在当前对话中已授权发送的附件，不接受任意本机文件路径。</p><p>在聊天中添加 DAT、ZIP 或文档，勾选“读取本次附件”后发送。模型可列出附件、查看 ZIP 条目并分段读取；精确计算和生成表格可再勾选 Python。</p><p class="field-note">没有会话授权的全局测试不会获得附件权限。未发送的草稿也不会被读取。</p>');return;}
  if (
    ["list_directory", "search_files", "file_info", "read_file"].includes(name)
  ) {
    await directoryBrowser(name);
    return;
  }
  if(name!=='system_time'){await window.WorkbenchV16.testTool(name);return;}
  const result=await api('/api/tools/test','POST',{name,arguments:{}});
  modal('工具测试 · '+name,`<pre>${h(JSON.stringify(result,null,2))}</pre>`);
}
async function togglePlugin(id) {
  const enabled = state.cfg.plugins.includes(id);
  if (!enabled) {
    const t = state.tools.find((t) => t.id === id);
    if (
      !(await confirmDialog(
        "启用可信本地插件",
        `${t.name} 请求权限：${t.permissions.join(", ")}。Python 插件可访问本机资源，仅启用已核对的可信代码。`,
        "启用",
      ))
    )
      return;
  }
  state.cfg = await api("/api/settings", "PUT", {
    plugins: enabled
      ? state.cfg.plugins.filter((p) => p !== id)
      : [...state.cfg.plugins, id],
  });
  await renderTools();
}
function contextTable() {
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>上下文</th><th>直观参考</th></tr></thead><tbody>${[
    ["2K", "一张交接班便签；一条设备故障报修信息"],
    ["4K", "一份设备维护通知；一道简单工序的操作说明"],
    ["8K（初始默认）", "一次故障经过记录；一份简短试验说明"],
    ["16K", "一份含表格的试验大纲；生产会议纪要与行动清单"],
    ["32K", "同一零件的几份工艺说明、检验要求和网页摘录"],
    ["64K", "一本设备手册的主要文字；工装改进需求资料"],
    ["100K", "一个设备维护专题的资料集合：手册摘录、故障记录与交接说明"],
    ["128K", "一个零件试制专题的主要资料：需求、试验记录、会议纪要"],
    ["200K", "一组相关零件的试制资料合集；多阶段工艺与试验记录"],
    ["256K", "小型制造专题资料集合：工艺文件、检验记录与参考网页"],
  ]
    .map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`)
    .join(
      "",
    )}</tbody></table></div><p class="field-note">仅为容量级别类比。真实 Token 数取决于语言、分词、图像、代码，材料可用容量还需扣除对话历史、工具和回答预留。</p>`;
}
function renderApi() {
  const py = `from openai import OpenAI\n\nclient = OpenAI(\n    base_url="http://127.0.0.1:11437/v1",\n    api_key="ollama",\n)\nreply = client.chat.completions.create(\n    model="qwen3.5:4b",\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(reply.choices[0].message.content)`;
  const ps = `$body = @{\n    model = 'qwen3.5:4b'\n    messages = @(@{ role = 'user'; content = '你好' })\n    stream = $false\n    options = @{ num_ctx = 8192 }\n} | ConvertTo-Json -Depth 5\n\nInvoke-RestMethod -Uri 'http://127.0.0.1:11437/api/chat' \u0060\n  -Method Post -ContentType 'application/json; charset=utf-8' \u0060\n  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))`;
  $("#view-api").innerHTML =
    `<div class="api-docs"><div class="eyebrow">接口接入与开发</div><h1>让本地模型，接入你的工作流。</h1><p>连接本机 Ollama 的客户端可独立工作，控制台无需充当云端转发服务。</p><div class="endpoint"><span class="tag">OLLAMA</span><code>http://127.0.0.1:11437</code><button class="button small" data-copy-text="http://127.0.0.1:11437">复制</button></div><div class="endpoint"><span class="tag">OPENAI</span><code>http://127.0.0.1:11437/v1</code><button class="button small" data-copy-text="http://127.0.0.1:11437/v1">复制</button></div><h2>01 · OpenAI SDK / 标准客户端</h2><p>先安装 openai Python 包。本地 API Key 可填 ollama，它不是鉴权密钥。云服务使用各自的真实 API Key。</p><pre><code>${h(py)}</code></pre><h2>02 · 原生 Ollama API / PowerShell</h2><pre><code>${h(ps)}</code></pre><div class="table-wrap"><table class="data-table"><thead><tr><th>原生 API</th><th>用途</th></tr></thead><tbody>${[
      ["GET /api/version", "版本"],
      ["GET /api/tags", "本地模型"],
      ["POST /api/show", "模型详情与能力"],
      ["POST /api/pull", "下载与流式进度"],
      ["DELETE /api/delete", "删除模型"],
      ["GET /api/ps", "模型加载分配与上下文"],
      ["POST /api/chat", "对话、视觉、工具调用"],
      ["POST /api/generate", "生成 / keep_alive=0 卸载"],
    ]
      .map(([a, b]) => `<tr><td><code>${a}</code></td><td>${b}</td></tr>`)
      .join(
        "",
      )}</tbody></table></div><h2>03 · Open WebUI</h2><p>原生运行的 Open WebUI → 设置 / 连接 / Ollama → 填入 <code>http://127.0.0.1:11437</code>。选择 OpenAI 兼容连接时使用带 <code>/v1</code> 的地址。</p><p>Docker 内通常使用 <code>http://host.docker.internal:11437</code>；宿主机仅监听回环地址时不保证容器可达。容器接入需额外配置监听与防火墙；当前默认仅供本机访问。</p><h2>04 · DeepSeek 等云模型接入本控制台</h2><p>进入设置 → 云模型连接，填服务商名称、完整 接口根地址（通常以 /v1 结尾）、API Key 与模型名，然后在聊天顶部切换服务商。模型列表可连接测试获取，也可手动输入。视觉、工具和上下文能力按服务商实际支持填写。</p><div class="notice">云端聊天时，消息、附加资料与工具结果会发送至所选服务商。顶部保持常驻小提示。API Key 用 Windows DPAPI 加密保存，不随源码迁移。</div><h2>05 · 工具接口与插件</h2><p>原生工具通过 <code>tools → tool_calls → role=tool</code> 循环；传统模式先进行 JSON 工具决策，再返回普通 Markdown 回答。关闭工具时保持原始聊天。两种模式共用同一插件入口。</p><p>外部 Open WebUI 直接连 Ollama，不会自动获得本控制台的 TIME / 目录插件；工具在 Python 应用层执行。此版本支持 Open WebUI 风格的导入体验，采用自己的 manifest + tool.py 接口。</p><h2>06 · 上下文容量参考</h2>${contextTable()}<p><a href="https://docs.ollama.com/api/introduction" target="_blank" rel="noreferrer">官方 API 文档 ↗</a> · <a href="https://docs.ollama.com/api/openai-compatibility" target="_blank" rel="noreferrer">OpenAI 兼容范围 ↗</a></p><p>Embedding 需支持嵌入的专用模型，预装 4B 不作为嵌入模型；兼容接口不等于支持所有 OpenAI 特性。</p></div>`;
  addCopyButtons($("#view-api"));
}

function renderBaseSettings() {
  const c = state.cfg;
  $("#view-settings").innerHTML =
    `<div class="page-heading"><div><div class="eyebrow">工作台设置</div><h1>让工作台，适合你的习惯。</h1><p>配置保存在本机。模型与业务数据集中管理，随时可以初始化。</p></div><button id="save-settings" class="button primary">保存设置</button></div><div class="settings-section card"><h2>外观与生成</h2><div class="form-grid"><label>主题<select id="setting-theme"><option value="light">浅色 · 清透白</option><option value="dark">深色 · 夜间工作台</option></select></label><label>默认上下文<select id="setting-context">${[2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144].map((n) => `<option value="${n}">${n / 1024}K</option>`).join("")}</select></label><label>默认模型<input id="setting-model" value="${h(c.default_model)}"></label><label>工具调用方式<select id="setting-tool-mode">${Object.entries(
      modeNames,
    )
      .map(([k, v]) => `<option value="${k}">${v}</option>`)
      .join(
        "",
      )}</select></label><label>回答预留 Token<input id="setting-output" type="number" min="128" value="${c.output_tokens}"></label><label>温度<input id="setting-temperature" type="number" min="0" max="2" step="0.1" value="${c.temperature}"></label><label>手写比较基准 · 字/分钟<input id="setting-handwriting" type="number" min="1" value="${c.handwriting_cpm}"></label><label>打字比较基准 · 字/分钟<input id="setting-typing" type="number" min="1" value="${c.typing_cpm}"></label><label class="full">助手提示词<textarea id="setting-system" rows="3">${h(c.system_prompt)}</textarea></label></div><p class="field-note" style="margin-top:15px">速度比较基准可填写你的实际速度；它们是文字输出节奏的刻度，不代表人类平均或思考能力。</p></div><div class="settings-section card"><h2>文件与目录</h2><div class="form-grid"><label>默认读取方式<select id="setting-read-mode"><option value="native">原始模式 · Python 直接读取</option><option value="office">兼容模式 · MS Office COM 只读</option></select></label><label>附件大小上限 · MiB<input id="setting-upload" type="number" min="1" max="100" value="${c.upload_mb}"></label><label>每条消息附件数量<input id="setting-attachments" type="number" min="1" max="10" value="${c.max_attachments}"></label></div><p class="field-note">支持 TXT、MD、Word、Excel 和 PPT 文字提取。Python 直接解析默认启用；COM 需要本机安装相应 Office 并拥有文件权限，可适配 Office 正常可读的企业透明加密文件。只读、禁宏、不保存原件；实际兼容性由企业策略决定。</p><p class="field-note">文件工具的起始目录、搜索深度与读取方式统一放在工具工作空间；可读取当前Windows账户有权限的路径。附件原件、提取文本和对话都属于运行数据，不进入源码迁移包。</p></div><div class="settings-section card"><h2>下载网络与代理</h2><div class="form-grid"><label>代理策略<select id="setting-proxy-mode"><option value="system">跟随系统静态代理 / HTTPS_PROXY</option><option value="direct">直连</option><option value="manual">手动代理</option></select></label><label>手动 HTTPS 代理地址<input id="setting-proxy-url" value="${h(c.proxy_url)}" placeholder="http://127.0.0.1:7890"></label></div><p class="field-note">云请求与受本程序管理的 Ollama 下载跟随此设置；本机11437/嵌入11438连接不走代理。PAC 自动脚本请填实际代理地址。更改后，已运行的 Ollama 需重启生效。</p><button id="restart-service" class="button small">应用代理并重启受管理的 Ollama</button></div><div class="settings-section card"><div class="flex-row" style="justify-content:space-between;margin-bottom:17px"><h2 style="margin:0">OpenAI 兼容云模型</h2><button id="add-provider" class="button small">＋ 添加服务商</button></div>${c.providers.length ? c.providers.map((p) => `<div class="download-job"><div class="flex-row"><b>${h(p.name)}</b><span class="tag">${h(p.id)}</span>${p.has_key ? '<span class="key-set">密钥已加密保存</span>' : ""}<button class="button small" data-edit-provider="${h(p.id)}">编辑</button><button class="button small" data-test-provider="${h(p.id)}">连接测试</button><button class="button small danger" data-delete-provider="${h(p.id)}">移除</button></div><p class="subtle">${h(p.base_url)}<br>${h((p.models || []).join(" · "))}</p></div>`).join("") : '<p class="subtle">尚未配置云服务。可接入 DeepSeek 等 OpenAI 标准格式服务。</p>'}<div class="notice">选择云模型后，聊天顶部常驻提示消息与附件将外发。Office COM 提取的企业资料也可能随对话发送。API Key 使用本机 Windows DPAPI 加密保存，换电脑后需重新填写。</div></div><div class="settings-section card"><h2>集中目录</h2><code style="font-size:12px">${h(c.root)}</code><p class="field-note" style="margin-top:14px">models/ 模型 · data/ 聊天与附件 · config/ 设置与密钥 · runtime/ Python 与 OCR · plugins/ 工具<br>完整离线包内置 Ollama 与独立 Python；WebView2 使用系统组件。软件不阻止官方更新。</p></div><div class="settings-section card danger-zone"><h2>恢复出厂设置 / 初始化</h2><p class="subtle">初始化清除本软件管理数据，不创建备份；模型、运行库和外部原文件保留。</p><button id="factory-reset" class="button danger">恢复出厂设置…</button></div>`;
  $("#setting-theme").value = c.theme;
  $("#setting-context").value = c.context;
  $("#setting-tool-mode").value = c.tool_mode;
  $("#setting-read-mode").value = c.file_read_mode || "native";
  $("#setting-proxy-mode").value = c.proxy_mode;
  // Tool-specific options are rendered from tool schemas inside the workspace.
  const fileCard = $("#setting-read-mode").closest(".card");
  const link = document.createElement("div");
  link.className = "notice";
  link.innerHTML =
    '起始目录、搜索深度与只读文件工具的读取方式，请在 <button class="button small" data-view="tools">工具工作空间 → 配置</button> 中调整。这里仅设置手动上传的附件。';
  fileCard.append(link);
  addSettingHelp();
  $("#save-settings").onclick = handle(saveSettingsForm);
  $("#add-provider").onclick = () => editProvider();
  $("#factory-reset").onclick = factoryResetDialog;
  $("#restart-service").onclick = handle(async () => {
    await saveSettingsForm();
    await api("/api/service/restart", "POST");
    toast("已应用代理并重启 Ollama");
    setTimeout(refreshStatus, 1500);
  });
  $("#setting-theme").onchange = () => setTheme($("#setting-theme").value);
}
function renderSettings(){
  renderBaseSettings();const c=state.cfg;
  const field=(id,title,value,note,min='',max='',step='1')=>`<label>${title}<input id="${id}" type="number" value="${value}" min="${min}" max="${max}" step="${step}"><p class="settings-help">${note}</p></label>`;
  const card=document.createElement('div');card.className='settings-section card';
  card.innerHTML=`<h2>上下文与长资料</h2><div class="form-grid"><label>旧历史如何处理<select id="setting-policy"><option value="summary">自动摘要（推荐）</option><option value="truncate">仅保留近期 / 截断</option><option value="manual">手动整理</option></select><p class="settings-help">只精简发给模型的内容，不删除聊天原记录。摘要可能丢失细节，工程数据请回查原文。</p></label>${field('setting-trigger','自动整理触发比例 · %',c.compression_trigger*100,'预计输入、回答预留和余量达到此比例后整理旧历史。',50,95)}${field('setting-recent','近期原文目标 · %',c.recent_ratio*100,'按 Token 而非消息条数保留，完整问答在预算允许时保留。',2,30)}${field('setting-attachment-ratio','附件使用剩余预算 · %',c.attachment_ratio*100,'100% 仍已扣除回答、安全余量与必要提示，不是把总容量全部塞满。超长会显示实际送入比例。',10,100)}</div><p class="field-note">“单次资料分析”少带旧历史，为当前附件腾出空间；仍超长时点附件进行分段摘要，不宣称一次读完全文。</p></div>`;
  $('#view-settings .danger-zone').before(card);$('#setting-policy').value=c.context_policy;
  const sampling=document.createElement('div');sampling.className='settings-section card';
  sampling.innerHTML=`<h2>生成参数 · 进阶</h2><label>默认参数来源<select id="setting-sampling-mode"><option value="recommended">按模型与思考模式的官方推荐</option><option value="model">沿用模型原始默认</option><option value="custom">自定义参数</option></select><p class="settings-help">Qwen3.5 非思考一般任务：温度 0.7、Top P 0.8、Top K 20；思考模式一般任务：1.0、0.95、20。自定义温度在上方“外观与生成”设置。</p></label><div class="form-grid">${field('setting-top-p','Top P',c.top_p,'控制采样候选的累计概率；并非准确率。',0,1,.05)}${field('setting-top-k','Top K',c.top_k,'候选数量；云接口通常不接受此字段，不强行发送。',0,200)}${field('setting-min-p','Min P',c.min_p,'低概率候选过滤；默认 0。',0,1,.01)}${field('setting-repeat-penalty','重复惩罚',c.repeat_penalty,'1 表示不额外惩罚重复；过高可能破坏术语和格式。',0,2,.1)}${field('setting-presence-penalty','主题重复惩罚',c.presence_penalty,'影响重复主题倾向；支持范围依服务商而异。',-2,2,.1)}${field('setting-seed','随机种子',c.seed,'-1 不固定；固定种子也不保证跨版本/设备完全一致。',-1,2147483647)}</div><label class="check-label"><input id="setting-recovery" type="checkbox" ${c.auto_recovery?'checked':''}>尚未输出且连接失败时，原配置重连一次（默认关闭）</label><p class="settings-help">资源/驱动错误不重试，不切 CPU/GPU、不重放工具。检查系统已提交额度。</p></div>`;
  card.after(sampling);$('#setting-sampling-mode').value=c.sampling_mode;
  const housekeeping=document.createElement('div');housekeeping.className='settings-section card';
  housekeeping.innerHTML=`<h2>后台与自动清理</h2><div class="form-grid"><label class="check-label"><input id="setting-tray" type="checkbox" ${c.close_to_tray?'checked':''}>关闭内嵌窗口后收至托盘</label><label class="check-label"><input id="setting-cleanup" type="checkbox" ${c.cleanup_enabled?'checked':''}>自动清理过期管理数据</label>${field('setting-run-days','实验记录最多保留 · 天',c.run_retention_days,'只清本软件任务目录，不清原始外部文件。',1,90)}${field('setting-run-count','实验记录最多保留 · 次',c.run_retention_count,'同时满足时间/数量规则；正在运行的不清理。',1,500)}${field('setting-trash-days','回收站保留 · 天',c.trash_retention_days,'超期永久删除回收站对话；正常未删除的聊天不会按时间自动删除。',1,365)}</div><label class="check-label"><input id="setting-mermaid" type="checkbox" ${c.mermaid_enabled?'checked':''}>显示离线 Mermaid 流程图</label><p class="settings-help">图表失败显示源码；无外部图片自动请求。无引用附件宽限 7 天；日志轮转；列表与实验记录分页。</p><button id="cleanup-now" class="button">清理当前可回收内容</button>`;
  sampling.after(housekeeping);
  $('#cleanup-now').onclick=handle(async()=>{await saveSettingsForm();const r=await api('/api/maintenance/cleanup','POST',{});toast(`已清理 ${r.removed} 项管理数据；模型和原始资料未动`);});
  $('#view-settings .danger-zone .subtle').textContent='正式版升级默认保留资料；需要清空时请手动初始化。同一版本正常重启保留数据。手动初始化清聊天、附件副本、云密钥、设置、插件与实验历史，不备份；模型与运行库保留。';
  window.Workbench?.settings();
}

async function saveSettingsForm() {
  state.cfg = await api("/api/settings", "PUT", {
    theme: $("#setting-theme").value,
    context: tokenValue("#setting-context"),
    default_model: $("#setting-model").value.trim(),
    tool_mode: $("#setting-tool-mode").value,
    output_tokens: tokenValue("#setting-output"),
    temperature: Number($("#setting-temperature").value),
    context_policy:$('#setting-policy').value,compression_trigger:Number($('#setting-trigger').value)/100,recent_ratio:Number($('#setting-recent').value)/100,attachment_ratio:Number($('#setting-attachment-ratio').value)/100,
    sampling_mode:$('#setting-sampling-mode').value,top_p:Number($('#setting-top-p').value),top_k:Number($('#setting-top-k').value),min_p:Number($('#setting-min-p').value),repeat_penalty:Number($('#setting-repeat-penalty').value),presence_penalty:Number($('#setting-presence-penalty').value),seed:Number($('#setting-seed').value),
    auto_recovery:$('#setting-recovery').checked,close_to_tray:$('#setting-tray').checked,cleanup_enabled:$('#setting-cleanup').checked,run_retention_days:Number($('#setting-run-days').value),run_retention_count:Number($('#setting-run-count').value),trash_retention_days:Number($('#setting-trash-days').value),mermaid_enabled:$('#setting-mermaid').checked,
    handwriting_cpm: Number($("#setting-handwriting").value),
    typing_cpm: Number($("#setting-typing").value),
    system_prompt: $("#setting-system").value,
    file_read_mode: $("#setting-read-mode").value,
    upload_mb: Number($("#setting-upload").value),
    max_attachments: Number($("#setting-attachments").value),
    proxy_mode: $("#setting-proxy-mode").value,
    proxy_url: $("#setting-proxy-url").value.trim(),
    ...(window.Workbench?.settingsPatch()||{}),
  });
  setTheme(state.cfg.theme);
  renderControls();
  toast("设置已保存；新会话使用默认参数");
  window.Workbench?.settingsSaved();
  if(window.Workbench)state.auth=await api('/api/auth/status');
}
function editProvider(id) {
  if(window.Workbench)return window.Workbench.provider(id);
  const p = state.cfg.providers.find((p) => p.id === id) || {
    id: "provider_" + Date.now(),
    name: "",
    base_url: "",
    models: [],
    context: 32768,
    vision: false,
    tools: true,
  };
  modal(
    id ? "编辑云模型连接" : "添加 OpenAI 兼容连接",
    `<div class="modal-form"><label>服务商名称<input id="provider-name" value="${h(p.name)}" placeholder="例如 DeepSeek"></label><label>接口根地址<input id="provider-url" value="${h(p.base_url)}" placeholder="https://…/v1"></label><label>API Key<input id="provider-key" type="password" autocomplete="off" placeholder="${p.has_key ? "已加密保存，留空保留" : "填入服务商 API Key"}"></label><label>模型名 · 用逗号或换行分隔<textarea id="provider-models" style="min-height:65px">${h(p.models.join("\n"))}</textarea></label><div class="form-grid"><label>上下文上限<input id="provider-context" type="number" value="${p.context || 32768}"></label><label class="check-label"><input id="provider-vision" type="checkbox" ${p.vision ? "checked" : ""}>支持图片输入</label><label class="check-label"><input id="provider-tools" type="checkbox" ${p.tools ? "checked" : ""}>支持原生工具调用</label></div><p class="field-note">按实际模型能力设置，OpenAI 兼容不保证全部特性。云消息与所选附件将发送到该服务商，聊天顶部持续提示。</p><button class="button primary" id="save-provider">保存连接</button></div>`,
  );
  $("#save-provider").onclick = handle(async () => {
    const value = {
      id: p.id,
      name: $("#provider-name").value.trim(),
      base_url: $("#provider-url").value.trim(),
      api_key: $("#provider-key").value,
      models: $("#provider-models")
        .value.split(/[,，\n]/)
        .map((v) => v.trim())
        .filter(Boolean),
      context: Number($("#provider-context").value),
      vision: $("#provider-vision").checked,
      tools: $("#provider-tools").checked,
    };
    if (!value.name || !value.base_url)
      throw new Error("请填写名称和 接口根地址");
    state.cfg = await api("/api/settings", "PUT", {
      providers: [...state.cfg.providers.filter((x) => x.id !== p.id), value],
    });
    closeModal();
    renderSettings();
    renderControls();
    toast("连接已保存");
  });
}
function factoryResetDialog() {
  modal(
    "恢复出厂设置 / 初始化",
    `<p>直接清除本软件的对话、附件副本、云密钥、设置、导入插件和实验历史。<b>不创建备份，无法恢复。</b>原始外部资料、9B/4B 与运行库保留。同版本正常重启不会初始化。</p><label>输入“初始化”确认<input id="reset-confirm" autocomplete="off" placeholder="初始化"></label><div class="flex-row"><button class="button danger" id="run-reset">直接初始化，不备份</button></div>`,
  );
  $("#run-reset").onclick = handle(async () => {
    const result = await api("/api/factory-reset", "POST", {
      confirm: $("#reset-confirm").value,
    });
    state.conv = null;
    state.attachments = [];
    closeModal();
    await bootstrap();
    showView("settings");
    scrollMemory={};localStorage.removeItem('localai-scroll-v1');
    toast("已直接初始化，未创建备份；模型与运行库保留");
  });
}
function chatOptionsDialog() {
  const o = options();
  modal(
    "当前会话",
    `<p class="subtle">${h(o.model)} · ${o.context / 1024}K · ${h(modeNames[o.tool_mode])}</p><div class="flex-row"><button id="save-model-preset" class="button">保存当前设置为模型预设</button>${state.conv ? '<button id="show-conv-options" class="button">对话名称 / 分支 / 导出</button>' : ""}</div><h3 style="margin-top:24px">上下文容量参考</h3>${contextTable()}`,
  );
  $("#save-model-preset").onclick = handle(async () => {
    const key = o.provider + ":" + o.model;
    state.cfg = await api("/api/settings", "PUT", {
      model_presets: {
        ...state.cfg.model_presets,
        [key]: {
          context: o.context,
          tool_mode: o.tool_mode,
          tools: o.tools,
          think: o.think,
        },
      },
    });
    toast("已保存该模型预设");
  });
  if ($("#show-conv-options"))
    $("#show-conv-options").onclick = () => conversationMenu(state.conv.id);
}

$("#modal-close").onclick = closeModal;
document.addEventListener(
  "click",
  handle(async (event) => {
    const b = event.target.closest("button,a");
    if (!b) return;
    const d = b.dataset;
    if (d.view) {
      showView(d.view);
      return;
    }
    if(d.toolFile){await download(d.toolFile,d.toolFilename||'生成文件');return;}
    if (d.suggest) {
      $("#prompt").value = d.suggest;
      updateBudget();
      $("#prompt").focus();
      return;
    }
    if (d.attach) {
      $("#file-input").click();
      return;
    }
    if (d.openConv) {
      await openConversation(d.openConv);
      return;
    }
    if (d.menuConv) {
      await conversationMenu(d.menuConv);
      return;
    }
    if (d.toggleTool) {
      const current = options().tools || [];
      await saveOptions({
        tools: current.includes(d.toggleTool)
          ? current.filter((t) => t !== d.toggleTool)
          : [...current, d.toggleTool],
      });
      return;
    }
    if (d.viewAttachment) {
      await viewAttachment(d.viewAttachment,d.messageRef);
      return;
    }
    if (d.removeAttachment) {
      state.attachments = state.attachments.filter(
        (a) => a.id !== d.removeAttachment,
      );
      renderAttachments();
      return;
    }
    if (d.msgAction) {
      await messageAction(d.msgAction, d.id);
      return;
    }
    if (d.closeModal !== undefined) {
      closeModal();
      return;
    }
    if (d.copyText) {
      await copy(d.copyText);
      return;
    }
    if (d.pullModel) {
      await pullModel(d.pullModel);
      return;
    }
    if (d.useModel) {
      state.conv = null;
      await saveOptions({ provider: "local", model: d.useModel });
      await refreshModels();
      showView("chat");
      renderMessages();
      return;
    }
    if (d.modelDetail) {
      await modelDetail(d.modelDetail);
      return;
    }
    if (d.unloadModel) {
      await api("/api/models/unload", "POST", { model: d.unloadModel });
      toast("模型已卸载，磁盘文件保留");
      await refreshStatus();
      return;
    }
    if (d.deleteModel) {
      if (
        await confirmDialog(
          "删除模型",
          `删除 ${d.deleteModel} 的模型数据。共享层的实际磁盘释放量可能不同，后续可重新下载。`,
          "删除模型",
        )
      ) {
        await api("/api/models", "DELETE", {
          model: d.deleteModel,
          confirm: d.deleteModel,
        });
        await refreshStatus();
        await refreshModels();
      }
      return;
    }
    if (d.cancelDownload) {
      await api("/api/downloads/" + d.cancelDownload + "/cancel", "POST");
      await refreshDownloads();
      return;
    }
    if (d.testTool) {
      await testTool(d.testTool);
      return;
    }
    if (d.sourceTool) {
      const source = await api("/api/tools/" + d.sourceTool + "/source");
      modal(
        "工具源码 · " + d.sourceTool,
        `<pre>${h(JSON.stringify(source.manifest, null, 2))}</pre><pre>${h(source.source)}</pre><details><summary>文件校验值</summary><pre>${h(JSON.stringify(source.hashes, null, 2))}</pre></details>`,
      );
      return;
    }
    if (d.enablePlugin) {
      await togglePlugin(d.enablePlugin);
      return;
    }
    if (d.removePlugin) {
      if (
        await confirmDialog(
          "移除工具",
          "停用后移除该插件，文件将归档到 data/backups。",
          "移除",
        )
      ) {
        await api("/api/tools/" + d.removePlugin, "DELETE");
        await renderTools();
      }
      return;
    }
    if (d.editProvider) {
      editProvider(d.editProvider);
      return;
    }
    if (d.testProvider) {
      const result = await api(
        "/api/models?provider=" + encodeURIComponent(d.testProvider),
      );
      modal(
        "连接测试成功",
        `<p>已连接，返回 ${result.models.length} 个模型。</p><pre>${h(result.models.map((m) => m.name).join("\n"))}</pre>`,
      );
      return;
    }
    if (d.deleteProvider) {
      if (
        await confirmDialog(
          "移除云连接",
          "移除此服务商配置及其加密密钥。已有会话内容保留。",
          "移除连接",
        )
      ) {
        state.cfg = await api("/api/settings", "PUT", {
          providers: state.cfg.providers.filter(
            (p) => p.id !== d.deleteProvider,
          ),
        });
        if (options().provider === d.deleteProvider) {
          state.conv = null;
          state.opts.provider = "local";
          state.opts.model = state.cfg.default_model;
        }
        renderSettings();
        renderControls();
      }
      return;
    }
  }),
);
$("#beginner-help").onclick = () =>
  modal(
    "第一次使用，从这里开始",
    `<p><b>1. 直接聊天：</b>保留默认本地模型，输入问题后按 Enter。第一次回答需要加载模型，稍等片刻即可。</p><p><b>2. 带着资料问：</b>点输入框左下角的 ＋，或拖入文档/图片。看到“文字已附加”后发送。图片默认同时提供原图和识别文字，点附件可查看、修改。</p><p><b>3. 想让它查信息：</b>开启“系统时间”工具，就能问现在几点。目录工具可按完整路径读取当前Windows账户有权访问的目录，请注意敏感信息。</p><p><b>4. 上下文是什么：</b>模型一次能参考的聊天和资料容量。默认 8K 适合入门，资料太长时可增大或选择片段；越大越占内存。</p><p><b>5. 两种工具方式：</b>保持“自动”即可。原生方式让模型直接发出工具请求；传统 JSON 是兼容方式。</p><p><b>6. 本地还是云端：</b>本地在你的电脑运行。云端需要你配置服务商，会发送对话和所选资料，顶部有持续提示。</p><p><b>7. 速度怎么看：</b>Token/s 越高，通常回答输出越快；第一次加载与模型思考会增加等待。不要把它等同于答案质量。</p>`,
  );
$("#new-chat").onclick = handle(newChat);
$("#history-search").oninput = renderHistory;
$("#trash-toggle").onclick = () => {
  state.trash = !state.trash;
  renderHistory();
};
$("#theme-toggle").onclick = handle(async () => {
  const theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  setTheme(theme);
  state.cfg = await api("/api/settings", "PUT", { theme });
  if (state.view === "settings") renderSettings();
});
$("#refresh").onclick = handle(async () => {
  await reconnectSession({manual: true});
  await refreshStatus();
  await refreshModels();
  toast("状态已刷新");
});
$("#provider-select").onchange = handle(async () => {
  const provider = $("#provider-select").value;
  const p = state.cfg.providers.find((p) => p.id === provider);
  await saveOptions({
    provider,
    model: provider === "local" ? state.cfg.default_model : p?.models[0] || "",
  });
  await refreshModels();
  renderMessages();
});
$("#model-select").onchange = handle(async () => {
  const model = $("#model-select").value;
  const preset =
    state.cfg.model_presets[options().provider + ":" + model] || {};
  await saveOptions({ model, ...preset });
});
$("#context-select").onchange = handle(() =>
  saveOptions({ context: Number($("#context-select").value) }),
);
$("#mode-select").onchange = handle(() =>
  saveOptions({ tool_mode: $("#mode-select").value }),
);
$("#think-toggle").onclick = handle(() =>
  saveOptions({ think: !options().think }),
);
$("#chat-options").onclick = chatOptionsDialog;
$('#analysis-toggle').onclick=handle(async()=>{await saveOptions({analysis_mode:!options().analysis_mode});updateBudget();});
$("#attach-button").onclick = () => $("#file-input").click();
$("#file-input").onchange = handle(async (e) => {
  await uploadFiles([...e.target.files]);
  e.target.value = "";
});
$("#plugin-input").onchange = handle(async (e) => {
  if (!e.target.files[0]) return;
  const f = new FormData();
  f.append("file", e.target.files[0]);
  await api("/api/tools/import", "POST", f);
  e.target.value = "";
  toast("已导入，默认未启用");
  await renderTools();
});
$("#send").onclick = handle(() => send());
$("#prompt").onkeydown = handle(async (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    await send();
  }
});
$("#prompt").oninput = () => {
  const t = $("#prompt");
  t.style.height = "auto";
  t.style.height = Math.min(200, t.scrollHeight) + "px";
  updateBudget();
};
$("#prompt").onpaste = handle(async (e) => {
  const files = [...e.clipboardData.files];
  if (files.length) {
    e.preventDefault();
    await uploadFiles(files);
  }
});
$("#drop-zone").ondragover = (e) => {
  e.preventDefault();
  $("#drop-zone").classList.add("dragging");
};
$("#drop-zone").ondragleave = () =>
  $("#drop-zone").classList.remove("dragging");
$("#drop-zone").ondrop = handle(async (e) => {
  e.preventDefault();
  $("#drop-zone").classList.remove("dragging");
  await uploadFiles([...e.dataTransfer.files]);
});
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    handle(newChat)();
  }
});
function addSettingHelp() {
  const help = {
    "setting-theme":
      "切换明暗外观。默认深色；保存后下次启动继续使用，出厂初始化恢复深色。",
    "setting-context":
      "模型一次能参考的聊天和资料容量。默认 8K；材料较长可提高，越大通常越占内存。下面有容量参考表。",
    "setting-model":
      "完整包预装 9B 和 4B。首次按专用显存 >10 GiB 选 9B，否则 4B；以后尊重手动选择，不每次重启改回。",
    "setting-tool-mode":
      "保持“自动”即可。原生是标准工具调用；传统 JSON 是兼容方式；关闭普通工具时，记忆仍独立受设置控制。",
    "setting-output":
      "给本次回答预留的空间。默认 2K；设大可写更长回答，但留给历史和附件的空间会减少。",
    "setting-temperature":
      "仅自定义参数模式使用此值。控制表达变化，不是智力开关；降低温度不保证尺寸、计算或事实正确。官方推荐模式随思考开关选择参数。",
    "setting-handwriting":
      "仅用于速度参照。填入你一分钟大约手写多少字，不影响模型速度或智能。",
    "setting-typing":
      "仅用于速度参照。填入你一分钟大约输入多少字，可与回答的可见文字输出速度比较。",
    "setting-system":
      "每次对话会附带给模型的角色和回答要求。新手可保留默认，不建议放 API Key 或机密信息。",
    "setting-read-mode":
      "手动上传文件的读取方式。默认 Python 直接解析；若 Office 文档受透明加密保护，可改 COM，由已授权 Office 读取。",
    "setting-upload":
      "单个上传文件的大小限制，默认 20 MiB。小文件处理更快，提取文字仍受上下文容量限制。",
    "setting-attachments":
      "一次发送可以带多少份资料，默认最多 5 份。多份附件会一起占用上下文。",
    "setting-proxy-mode":
      "模型下载与云接口使用的网络路径。默认跟随系统静态代理；本机 Ollama API 始终直连。",
    "setting-proxy-url":
      "仅“手动代理”模式使用。填写代理软件的 HTTP/HTTPS 代理入口，不是模型下载地址。",
  };
  for (const [id, text] of Object.entries(help)) {
    const field = $("#" + id);
    if (field) {
      const note = document.createElement("p");
      note.className = "settings-help";
      note.textContent = text;
      field.parentElement.append(note);
    }
  }
  const reference = document.createElement("details");
  reference.innerHTML =
    "<summary>上下文参考：2K、4K、8K 各能容纳什么？</summary>" + contextTable();
  $("#setting-context").parentElement.append(reference);
}

async function toolSettingsDialog(id) {
  const conf = await api("/api/tools/" + id + "/settings");
  const entries = Object.entries(conf.schema.properties || {});
  modal(
    "工具配置 · " + (state.tools.find((t) => t.id === id)?.name || id),
    `<div class="modal-form">${entries
      .map(([key, spec]) => {
        const value = conf.values[key] ?? spec.default ?? "";
        let control;
        if (spec.format === "root-list")
          control = `<textarea id="tool-setting-${h(key)}" style="min-height:100px" placeholder="可点下方按钮选择文件夹">${h((value || []).map((r) => r.id + "=" + r.path).join("\n"))}</textarea><button class="button small" id="pick-tool-root">＋ 选择文件夹</button>`;
        else if (spec.enum)
          control = `<select id="tool-setting-${h(key)}">${spec.enum.map((v) => `<option value="${h(v)}" ${v === value ? "selected" : ""}>${v === "office" ? "Office COM（默认兼容）" : v === "native" ? "Python 直接读取" : h(v)}</option>`).join("")}</select>`;
        else if (spec.type === "boolean")
          control = `<input id="tool-setting-${h(key)}" type="checkbox" ${value ? "checked" : ""}>`;
        else if (["object", "array"].includes(spec.type))
          control = `<textarea id="tool-setting-${h(key)}">${h(JSON.stringify(value, null, 2))}</textarea>`;
        else
          control = `<input id="tool-setting-${h(key)}" type="${spec.type === "integer" || spec.type === "number" ? "number" : "text"}" value="${h(value)}" ${spec.minimum != null ? 'min="' + spec.minimum + '"' : ""} ${spec.maximum != null ? 'max="' + spec.maximum + '"' : ""}>`;
        return `<label>${h(spec.title || key)}${control}<p class="settings-help">${h(spec.description || "此配置由工具声明。")}</p></label>`;
      })
      .join(
        "",
      )}${!entries.length ? '<p class="subtle">此工具不需要额外配置。</p>' : ""}<div class="flex-row"><button id="save-tool-config" class="button primary">保存工具配置</button></div></div>`,
  );
  window.WorkbenchFileBrowser.settings();
  if ($("#pick-tool-root"))
    $("#pick-tool-root").onclick = handle(async () => {
      const button = $("#pick-tool-root");
      button.disabled = true;
      button.textContent = "请在 Windows 窗口中选择…";
      try {
        const selected = await api("/api/ui/pick-directory", "POST");
        const area = $("#tool-setting-roots");
        if (!selected.path || !area) return;
        const lines = area.value.split("\n").filter((l) => l.trim());
        const normalized = (p) =>
          p.trim().replace(/\\/g, "/").replace(/\/$/, "").toLowerCase();
        if (
          lines.some(
            (l) =>
              normalized(l.slice(l.indexOf("=") + 1)) ===
              normalized(selected.path),
          )
        ) {
          toast("该文件夹已添加");
          return;
        }
        let i = 1;
        while (
          lines.some((l) => l.slice(0, l.indexOf("=")).trim() === "folder_" + i)
        )
          i++;
        lines.push("folder_" + i + "=" + selected.path);
        area.value = lines.join("\n");
        toast("路径已填入；可修改左侧别名，保存后生效");
      } finally {
        button.disabled = false;
        button.textContent = "＋ 选择文件夹";
      }
    });
  $("#save-tool-config").onclick = handle(async () => {
    const values = {};
    for (const [key, spec] of entries) {
      const field = $("#tool-setting-" + key);
      if (spec.format === "root-list") {
        values[key] = field.value
          .split("\n")
          .filter((l) => l.trim())
          .map((l) => {
            const i = l.indexOf("=");
            if (i < 1)
              throw new Error(
                "目录格式为 简称=路径，可使用“选择文件夹”自动填写",
              );
            return { id: l.slice(0, i).trim(), path: l.slice(i + 1).trim() };
          });
      } else if (spec.type === "boolean") values[key] = field.checked;
      else if (["integer", "number"].includes(spec.type))
        values[key] = Number(field.value);
      else if (["array", "object"].includes(spec.type))
        values[key] = JSON.parse(field.value);
      else values[key] = field.value;
    }
    await api("/api/tools/" + id + "/settings", "PUT", values);
    state.cfg = await api("/api/settings");
    closeModal();
    await renderTools();
    toast("工具配置已保存");
  });
}

async function directoryBrowser(tool = "list_directory", initial = {}) {
  return window.WorkbenchV15.directoryBrowser(tool,initial);
}
let runtimePending = false;
async function refreshRuntime() {
  if (!state.connected || runtimePending || state.view !== "chat" || document.hidden) return;
  runtimePending = true;
  try {
    const o = options();
    const el = $("#model-runtime");
    if (o.provider !== "local"&&state.cfg.providers.find(p=>p.id===o.provider)?.kind!=='ollama') {
      el.innerHTML = `<span class="runtime-state"><i></i><b>云模型</b></span><span>${h(state.cfg.providers.find((p) => p.id === o.provider)?.name || o.provider)}</span><span class="runtime-sep">|</span><span>服务端资源占用不可见</span><span class="speed-text">${state.busy ? h($("#live-speed").textContent) : "等待发送"}</span>`;
      return;
    }
    const value = await api("/api/runtime?provider="+encodeURIComponent(o.provider));
    const loaded = value.loaded.find(
      (m) => m.name === o.model || m.model === o.model,
    );
    const gpu = value.hardware.gpus?.[0];
    const last = [...path(state.conv)]
      .reverse()
      .find((m) => m.role === "assistant" && m.stats?.tokens_per_second)?.stats;
    el.innerHTML = `<span class="runtime-state"><i style="background:${value.online?'var(--green)':'var(--danger)'}"></i><b>${value.remote?'局域网主机 · ':''}${state.busy?h($('#live-speed').textContent):loaded?'模型已加载':'按需加载'}</b></span><span class="runtime-sep">|</span><span>${loaded?'模型占用 '+fmtBytes(loaded.size)+(loaded.size_vram?' · GPU 分配 '+Math.min(100,Math.round(loaded.size_vram/loaded.size*100))+'%':' · CPU'):'发送问题时按需加载'}</span>${gpu?`<span class="runtime-sep">|</span><span>${gpu.used_mb==null?'设备总显存 '+num(gpu.total_mb/1024)+' GiB · '+h(window.WorkbenchResourceUI.monitorText(value.hardware)):'设备显存 '+num(gpu.used_mb/1024)+'/'+num(gpu.total_mb/1024)+' GiB · GPU '+num(gpu.utilization,0)+'% · '+h(window.WorkbenchResourceUI.monitorText(value.hardware))}</span>`:''}${!gpu&&value.hardware.gpu_monitor_status?`<span class="runtime-sep">|</span><span>NVIDIA · ${h(window.WorkbenchResourceUI.monitorText(value.hardware))}</span>`:''}${value.hardware.commit_available!=null?`<span class="runtime-sep">|</span><span>剩余提交额度 ${fmtBytes(value.hardware.commit_available)}</span>`:''}<span class="runtime-sep">|</span><span class="speed-text">${state.busy?h($('#live-speed').textContent):last?'上次 '+num(last.tokens_per_second)+' Token/s':'速度将在回答时显示'}</span>`;
  } catch {
  } finally {
    runtimePending = false;
  }
}

async function renderGuide() {
  const response = await api("/api/guide");
  state.softwareGuide = response.markdown;
  $("#view-guide").innerHTML =
    `<div class="page-heading"><div><div class="eyebrow">软件教程与操作说明</div><h1>软件教程</h1><p>从日常操作到给其他 AI 的完整软件说明。</p></div><div class="flex-row"><button id="copy-software-guide" class="button primary">复制软件说明给 AI</button><button id="download-software-guide" class="button">导出教程 MD</button></div></div><article class="api-docs message-content">${markdown(response.markdown)}</article>`;
  $("#copy-software-guide").onclick = () => copy(state.softwareGuide);
  $("#download-software-guide").onclick = handle(() =>
    download("/api/guide?format=md", "本地智能工作台-软件教程.md"),
  );
  addCopyButtons($("#view-guide"));
  restoreScroll();
}

document.addEventListener(
  "click",
  handle(async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.historyPage) {
      state.historyPage = Number(b.dataset.historyPage);
      await refreshHistory();
      $("#history-list").scrollTop = 0;
    }
    if (b.dataset.loadOlder) {
      const before = $("#messages").scrollHeight;
      const top = $("#messages").scrollTop;
      const offset=state.conv.message_page?.next_offset;if(offset==null)return;
      const page=await api('/api/conversations/'+state.conv.id+'?offset='+offset);
      const seen=new Set(state.conv.messages.map(m=>m.id));
      state.conv.messages=[...page.messages.filter(m=>!seen.has(m.id)),...state.conv.messages];
      state.conv.message_page={...page.message_page,window_leaf:state.conv.message_page?.window_leaf||state.conv.leaf,around:state.conv.message_page?.around||null};
      state.visibleMessages[state.conv.id]=state.conv.messages.length;
      delete scrollMemory[scrollKey()];
      renderMessages();
      requestAnimationFrame(
        () =>
          ($("#messages").scrollTop =
            $("#messages").scrollHeight - before + top),
      );
    }
    if (b.dataset.toolSettings) {
      await toolSettingsDialog(b.dataset.toolSettings);
    }
  }),
);
let searchTimer;
$("#history-search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(
    handle(async () => {
      state.historyPage = 1;
      await refreshHistory();
    }),
    250,
  );
};
$("#trash-toggle").onclick = handle(async () => {
  state.trash = !state.trash;
  state.historyPage = 1;
  await refreshHistory();
});
let scrollTimer;
for (const el of [$("#messages"), ...$$(".page-view")])
  el.addEventListener(
    "scroll",
    () => {
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(saveScroll, 180);
    },
    { passive: true },
  );
window.addEventListener("beforeunload", saveScroll);
setInterval(() => {
  if (state.connected && !state.busy && !document.hidden) refreshStatus();
}, 20000);
setInterval(()=>{if(!document.hidden&&state.view==='models')refreshDownloads();},4000);
setInterval(refreshRuntime, 10000);
