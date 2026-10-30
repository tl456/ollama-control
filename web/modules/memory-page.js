"use strict";
// The memory page owns presentation only. All writes use the backend memory service.
window.WorkbenchMemoryPage = (() => {
  let renderSequence=0;
  async function render({state, api, $, h, markdown, fmtK, toast, handle, confirmDialog, restoreScroll}) {
    const page = $("#view-memory");
    if (!state.localAccess) { page.innerHTML = '<p class="readonly-shared">记忆管理仅限宿主机。</p>'; return; }
    const sequence=++renderSequence;
    page.innerHTML='<div class="inline-empty">正在读取长期记忆与Token用量…</div>';
    const value = await api("/api/memory");
    if(sequence!==renderSequence)return;
    const limit=value.token_limit??value.token_budget,used=value.used_tokens,over=value.over_limit||value.over_budget;
    const countingLabel=({'conservative_estimate':'保守估算','qwen3.5_tokenizer':'本地 Qwen 分词'})[value.counting_method]||'本地分词计数';
    page.innerHTML = `<div class="page-heading"><div><div class="eyebrow">固定能力 · 一份 Markdown</div><h1>长期记忆，由 AI 自由维护。</h1><p>AI 可新增、修改、合并、删除、重排或重写全文；人工编辑作为辅助。</p></div><div class="page-actions"><button id="save-memory" class="button primary">保存修改</button><button id="clear-memory" class="button danger">清空记忆</button></div></div><div class="notice"><div class="memory-budget ${over?'over':''}"><span>已保存记忆用量</span><b>${fmtK(used)} / ${fmtK(limit)}</b><span>${used??'—'} / ${limit} Token（含注入包装）</span></div><p>当前${value.enabled ? "启用" : "关闭"} · AI ${value.ai_write ? "可自由完整编辑" : "不能修改"} · ${h(countingLabel)} · 文件 ${value.bytes} / ${value.max_kb * 1024} 字节。</p><p>${over?h(value.warning||'现有记忆超过新阈值，请精简后保存，原文件保留。'):'保存时按最终全文检查Token硬上限，超额不会覆盖原文件。'}阈值在设置中可修改，默认1K。云模型分词可能不同；记忆会随当前连接发送。</p></div><p id="memory-write-status" class="field-note" aria-live="polite"></p><div class="memory-layout"><div class="card"><h2>编辑这一份 Markdown</h2><textarea id="memory-editor" class="memory-editor" aria-label="长期记忆编辑">${h(value.text)}</textarea><p class="field-note">不要求版本指纹；完整覆盖按最后一次成功保存生效。AI 尽量增量维护只是建议，并发全文覆盖仍可能覆盖其他对话的同期修改。AI 只可操作这一份指定记忆文件。</p><p class="field-note">不保存密钥、凭据、附件全文或猜测。模型声称“已记住”不等于保存成功，请以工具记录或这里的实际内容为准。</p></div><div class="card"><h2>阅读预览</h2><article id="memory-rendered" class="message-content memory-preview">${markdown(value.text || '暂无记忆。你可以对模型说：“以后请使用毫米和牛顿。”')}</article></div></div>`;
    let previewTimer;
    $("#memory-editor").oninput = () => {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(() => {
        const preview = $("#memory-rendered"), editor = $("#memory-editor");
        if (!preview || !editor) return;
        preview.innerHTML = markdown(editor.value);
        $("#memory-write-status").textContent="正文有未保存修改；保存时由服务端重新统计Token，超额保留原记忆。";
        window.enhanceWorkMarkdown?.(preview, state.cfg.mermaid_enabled !== false).catch(() => {});
      }, 250);
    };
    const save = async text => {
      const saveButton = $("#save-memory"), clearButton = $("#clear-memory");
      if (saveButton.disabled) return;
      saveButton.disabled = clearButton.disabled = true;
      saveButton.textContent = "正在保存…";
      try {
        const result = await api("/api/memory", "PUT", {text, mode:"replace"});
        if (result.ok === false) throw new Error(result.error || "记忆未保存");
        toast(result.note || "记忆已保存");
        await render({state, api, $, h, markdown, fmtK, toast, handle, confirmDialog, restoreScroll});
      } catch(error) {
        const note=$("#memory-write-status");if(note){note.className='error-note';note.textContent=error.message||'保存失败，原记忆保留。';}throw error;
      } finally {
        saveButton.disabled = clearButton.disabled = false;
        saveButton.textContent = "保存修改";
      }
    };
    $("#save-memory").onclick = handle(() => save($("#memory-editor").value));
    $("#clear-memory").onclick = handle(async () => {
      if (await confirmDialog("清空长期记忆", "仅清这一份记忆，不删除对话、知识库或模型。", "清空")) await save("");
    });
    restoreScroll();
  }
  return Object.freeze({render});
})();
