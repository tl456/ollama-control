"use strict";
// Pure, shared presentation for persisted and streamed tool execution records.
// This module never calls a tool, invents an execution, or reads the browser clock.
window.WorkbenchToolUI = (() => {
  const names = Object.freeze({
    system_time: "系统时间 TIME", python_run: "Python 辅助", ask_user: "提问用户",
    office_operator:"Office 操作器",text_create:"新建文本文件",file_manage:"文件操作器",http_request:"网络工具",
    memory_read: "读取长期记忆", memory_update: "维护长期记忆",
    knowledge_search: "检索知识库", knowledge_read: "读取知识片段",
    read_attachment: "读取本对话附件", read_file: "只读文件读取",
    list_directory: "只读目录清单", search_files: "搜索文件名", file_info: "查看文件属性",
  });
  const labels = Object.freeze({queued: "等待执行", running: "执行中", success: "成功", failed: "失败", outcome_unknown: "结果未确认 · 已停止", interrupted: "已中断 · 结果未确认"});
  const markupCache = new WeakMap();
  const escape = value => String(value ?? "").replace(/[&<>"']/g,
    char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
  const compact = value => String(value ?? "").replace(/\s+/g, " ").trim().slice(0, 180);
  const bytes = size => Number.isFinite(size) ? size >= 1048576 ? (size/1048576).toFixed(1)+" MiB" : size >= 1024 ? (size/1024).toFixed(1)+" KiB" : size+" B" : "—";
  function name(id, catalogue = []) { return names[id] || catalogue.find(tool => tool.id === id)?.name || id || "未知工具"; }
  function status(event) {
    if (event.status === "outcome_unknown" || event.result?.outcome_unknown) return "outcome_unknown";
    if (event.status === "interrupted") return "interrupted";
    if (event.success === false) return "failed";
    if (["queued", "running", "success", "failed"].includes(event.status)) return event.status;
    return typeof event.success === "boolean" ? event.success ? "success" : "failed" : "running";
  }
  function duration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "";
    if (seconds < .001) return "<1 毫秒";
    if (seconds < .1) return Math.round(seconds * 1000) + " 毫秒";
    return seconds.toFixed(1) + " 秒";
  }
  function summary(event) {
    if (event.reused) return "复用本轮已有结果 · " + summary({...event, reused:false});
    const state = status(event), result = event.result || {};
    if (state === "queued") return "调用已进入执行队列，尚未取得结果";
    if (state === "running") return event.name === "ask_user" ? "等待用户选择或补充回答" : "正在执行此工具，结果返回后更新";
    if (state === "outcome_unknown") return compact(result.next_step || "操作可能已产生修改，请检查实际结果；本轮不会自动重试");
    if (state === "interrupted") return compact(result.error || "调用已中断，不能确认执行结果；未当作成功或未执行处理");
    if (state === "failed") return compact(result.error || result.message || event.error || "工具未成功；展开查看原因");
    if (event.name === "ask_user") return compact(result.answer || (result.status === "skipped" ? "用户跳过此问题" : "已收到用户反馈"));
    if (event.name === "system_time") return compact(result.iso8601 || [result.date, result.time, result.timezone].filter(Boolean).join(" ") || "系统时间已读取");
    if (event.name === "memory_read") return `已读取记忆 · ${bytes(result.bytes)}${result.text ? " · " + compact(result.text).slice(0, 85) : " · 当前为空"}`;
    if (event.name === "memory_update") return compact(result.note || "记忆已保存") + (Number.isFinite(result.bytes) ? " · " + bytes(result.bytes) : "");
    if (event.name === "python_run") return compact(result.stdout || result.stderr || "Python 已执行") + (Array.isArray(result.files) && result.files.length ? ` · ${result.files.length} 个输出文件` : "");
    if (Array.isArray(result.items)) return `返回 ${result.items.length} 项${result.complete === false || result.truncated ? " · 内容有截取" : ""}`;
    return compact(result.note || result.text || result.message || "执行完成；展开查看返回数据");
  }
  function filesHtml(event) {
    if (event.name === "office_operator" || event.result?.preview_id || Array.isArray(event.result?.fonts)) {
      const result=event.result||{},id=result.preview_id||result.preview?.id;
      const button=id&&/^[a-zA-Z0-9_-]+$/.test(id)?'<button class="button small" data-office-preview="'+escape(id)+'">▧ 查看排版效果</button>':'';
      const fonts=Array.isArray(result.fonts)?result.fonts.filter(x=>typeof x==='string').slice(0,1000):[];
      return '<div class="office-result-card">'+button+(result.preview_note?'<p class="field-note">'+escape(result.preview_note)+'</p>':'')+(result.preview_error?'<p class="notice">预览未生成：'+escape(result.preview_error)+'</p>':'')+(fonts.length?'<label>字体演示<select data-font-demo>'+fonts.map(f=>'<option>'+escape(f)+'</option>').join('')+'</select></label><div class="font-demo">设备维护报告 · 示例文字 ABC 123</div>':'')+'</div>';
    }
    if (status(event) !== "success") return "";
    const files = (event.result?.files || []).slice(0, 50).filter(file =>
      /^\/api\/python\/runs\/[a-f0-9]{32}\/file\?name=/.test(file.url || ""));
    return files.length ? `<div class="tool-output-files">${files.map(file =>
      `<button class="button small" data-tool-file="${escape(file.url)}" data-tool-filename="${escape(file.name)}" title="SHA-256 ${escape(file.sha256 || "未提供")}">↓ 下载 ${escape(file.name)} · ${bytes(file.bytes)}</button>`).join("")}</div>` : "";
  }
  function eventKey(event, index = 0) { return event.call_id || event.tool_call_id || event.id || "tool-" + index; }
  function eventHtml(event, catalogue = [], modes = {}, index = 0) {
    const state = status(event), mark = state === "success" ? "✓" : state === "failed" ? "!" : ["interrupted","outcome_unknown"].includes(state) ? "?" : state === "queued" ? "◷" : "◌";
    const timing = duration(event.seconds);
    return `<details class="tool-event tool-${state}" data-tool-key="${escape(eventKey(event, index))}" data-tool-status="${state}"><summary><span class="tool-event-mark" aria-hidden="true">${mark}</span><span class="tool-event-title">${escape(name(event.name, catalogue))}</span><span class="tool-status-label">${labels[state]}</span><span class="tool-event-meta">${escape(modes[event.mode] || event.mode || "")}${timing ? " · " + timing : ""}</span><span class="tool-result-inline">${escape(summary(event))}</span></summary><pre>${escape(JSON.stringify({arguments:event.arguments || {},result:event.result ?? null}, null, 2))}</pre>${filesHtml(event)}</details>`;
  }
  function eventsHtml(events, catalogue, modes) { return (events || []).map((event, index) => eventHtml(event, catalogue, modes, index)).join(""); }
  function sync(container, events, catalogue, modes) {
    const previous = new Map([...container.children].map(node => [node.dataset.toolKey, node]));
    const keep = new Set();
    (events || []).forEach((event, index) => {
      const key = String(eventKey(event, index)), html = eventHtml(event, catalogue, modes, index), old = previous.get(key);
      keep.add(key);
      if (old && markupCache.get(old) === html) return;
      const wrapper = document.createElement("div"); wrapper.innerHTML = html;
      const node = wrapper.firstElementChild; markupCache.set(node, html);
      if (old) { node.open = old.open; old.replaceWith(node); } else container.append(node);
    });
    for (const [key, node] of previous) if (!keep.has(key)) node.remove();
  }
  function upsert(message, event) {
    message.interactions ??= [];
    const key = event.call_id || event.tool_call_id;
    const index = key ? message.interactions.findIndex(previous => (previous.call_id || previous.tool_call_id) === key) : -1;
    if (index >= 0) message.interactions[index] = {...message.interactions[index], ...event};
    else message.interactions.push(event);
  }
  function runtimeHtml(runtime) {
    if (!runtime || runtime.source !== "server" || typeof runtime.local_time !== "string" ||
        !/^\d{4}-\d{2}-\d{2}T/.test(runtime.local_time) || !Number.isFinite(Date.parse(runtime.local_time))) return "";
    const text = runtime.local_time.replace("T", " ");
    return `<div class="turn-runtime-note" title="后端本轮实际注入的时间快照；不是浏览器时钟，也不表示执行过 TIME 工具"><span aria-hidden="true">◷</span><span>本轮系统时间已同步</span><time datetime="${escape(runtime.local_time)}">${escape(text)}</time><span class="turn-runtime-mode">${escape(runtime.mode || "")}</span></div>`;
  }
  return Object.freeze({name, status, duration, summary, filesHtml, eventHtml, eventsHtml, sync, upsert, runtimeHtml});
})();
