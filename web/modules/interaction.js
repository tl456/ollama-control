"use strict";
// Conversation interaction UI. Future queued turns stay separate from live steering.
window.WorkbenchInteraction = (() => {
  let current = null, key = "", refresh = null;
  const receipts = new Map();
  function boot() {
    if (!document.querySelector('#user-question'))
      document.querySelector('.composer-area').insertAdjacentHTML('afterbegin','<section id="user-question" class="user-question" aria-live="polite" hidden></section>');
    if (!document.querySelector('#steer-current'))
      document.querySelector('#send').insertAdjacentHTML('beforebegin','<button id="steer-current" class="button steer-current" hidden title="把输入补充到当前任务；在下一次模型请求前生效">↗ 调整当前任务</button>');
    document.querySelector('#steer-current').onclick = e => handle(steer)(e);
  }
  function queueFold(panel) {
    const heading=panel.querySelector('.queue-heading');if(!heading)return;
    const details=document.createElement('details'),summary=document.createElement('summary');
    try{details.open=localStorage.getItem('workbench-queue-expanded')==='true';}catch{}
    summary.className=heading.className;while(heading.firstChild)summary.append(heading.firstChild);heading.remove();
    details.append(summary);while(panel.firstChild)details.append(panel.firstChild);panel.append(details);
    details.addEventListener('toggle',()=>{try{localStorage.setItem('workbench-queue-expanded',String(details.open));}catch{}});
  }
  function render(items = [], reload) {
    boot(); refresh = reload;
    current = items.find(t => t.status === 'running') || null;
    const button = document.querySelector('#steer-current');
    button.hidden = !current;
    const panel = document.querySelector('#user-question'), q = current?.user_question;
    const nextKey = (state.conv?.id || '') + ':' + (q?.id || '') + ':' + (q?.status || '');
    if (key === nextKey) return;
    key = nextKey;
    panel.hidden = !(current && q?.status === 'pending');
    if (panel.hidden) {panel.replaceChildren(); return;}
    const turn = current, conv = state.conv.id;
    panel.innerHTML = '<details open><summary>需要你的补充 · 回答后继续</summary><p class="question-title">'+h(q.question)+'</p><div class="question-options">'+(q.options||[]).map((value,i)=>'<button class="button" data-answer-choice="'+i+'">'+h(value)+'</button>').join('')+'</div><label>自定义回答<textarea id="question-answer" maxlength="8000" rows="2" placeholder="输入你的选择、补充条件或新的要求…"></textarea></label><div class="page-actions"><button class="button primary" id="submit-question-answer">回答并继续</button><button class="button" id="skip-question-answer">跳过此问题</button><small>下方“发送”仍加入待发队列；“调整当前任务”用于改变当前任务方向。</small></div><p id="question-answer-error" class="error-note" hidden></p></details>';
    const submit = async (answer, skip = false) => {
      if (!skip && !answer.trim()) throw new Error('请选择一个选项或填写回答');
      const buttons = [...panel.querySelectorAll('button')]; buttons.forEach(b=>b.disabled=true);
      const error = panel.querySelector('#question-answer-error'); error.hidden=true;
      try {
        await api('/api/chat/'+encodeURIComponent(conv)+'/turns/'+encodeURIComponent(turn.id)+'/answer','POST',{question_id:q.id,answer:answer.trim(),skip});
        if (state.conv?.id===conv) {panel.hidden=true; key='';}
        toast(skip?'已跳过，模型将收到你没有补充的说明':'回答已提交，当前任务将继续');
        await refresh?.();
      } catch (failure) {error.textContent=failure.message;error.hidden=false;}
      finally {buttons.forEach(b=>b.disabled=false);}
    };
    panel.querySelectorAll('[data-answer-choice]').forEach(b=>b.onclick=()=>{panel.querySelector('#question-answer').value=q.options[Number(b.dataset.answerChoice)];panel.querySelector('#question-answer').focus();});
    panel.querySelector('#submit-question-answer').onclick=e=>handle(()=>submit(panel.querySelector('#question-answer').value))(e);
    panel.querySelector('#skip-question-answer').onclick=e=>handle(()=>submit('',true))(e);
  }
  async function steer() {
    const turn = current, conv = state.conv?.id, input = document.querySelector('#prompt'), draft = input.value, content = draft.trim();
    if (!turn || !conv) throw new Error('当前没有正在处理的任务；可以直接发送新消息');
    if (!content) {input.focus();throw new Error('先在输入框写下要调整的方向');}
    const button = document.querySelector('#steer-current'); button.disabled=true;
    const receiptKey=turn.id+':'+content;
    if(!receipts.has(receiptKey))receipts.set(receiptKey,crypto.randomUUID());
    while(receipts.size>32)receipts.delete(receipts.keys().next().value);
    try {
      await api('/api/chat/'+encodeURIComponent(conv)+'/turns/'+encodeURIComponent(turn.id)+'/steer','POST',{content,client_request_id:receipts.get(receiptKey)});
      receipts.delete(receiptKey);
      if(state.conv?.id===conv && input.value===draft)input.value='';
      toast('已提交方向调整；当前文件操作会先完成，再按新要求继续'+(state.attachments.length?'。附件保留在待发区':''));
      await refresh?.(); updateBudget();
    } finally {button.disabled=false;}
  }
  return Object.freeze({boot,render,queueFold});
})();
