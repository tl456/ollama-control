"use strict";
window.WorkbenchResourceUI = (() => {
  const labels = Object.freeze({waiting:"等待首次查询",ready:"30 秒缓存监测",backoff:"查询失败 · 退避 5 分钟",
    paused:"本次运行监测已暂停",unavailable:"NVIDIA 监测接口不可用",disabled:"实时监测关闭",
    disabled_environment:"当前运行环境已暂停主动监测"});
  function monitorText(value = {}) {
    const key = value.gpu_monitor_status || value.status ||
      (value.gpu_monitor_paused || value.paused ? "paused" : value.gpu_monitor_enabled === false ? "disabled" : "waiting");
    return labels[key] || "监测状态待确认";
  }
  function settings(panel, {api, $, state, toast, handle}) {
    panel.insertAdjacentHTML("beforeend", '<div class="page-actions"><button id="monitor-resume" class="button small">恢复已暂停的监测</button><span id="monitor-status" class="field-note" aria-live="polite"></span></div>');
    const button = $("#monitor-resume"), line = $("#monitor-status");
    const show = value => { line.textContent = monitorText(value); button.disabled = !$("#setting-gpu-monitor").checked; };
    show({gpu_monitor_enabled:state.cfg.gpu_monitor});
    api("/api/runtime").then(value => show(value.hardware || value)).catch(() => { line.textContent="暂未取得监测状态，可稍后刷新页面"; });
    $("#setting-gpu-monitor").addEventListener("change", () => { button.disabled = !$("#setting-gpu-monitor").checked; });
    button.onclick = handle(async () => {
      const result = await api("/api/runtime/monitor-resume", "POST", {});
      show(result);
      toast("已重置监测暂停状态；不会立即强制查询，也不改变已保存开关");
    });
  }
  return Object.freeze({monitorText, settings});
})();
