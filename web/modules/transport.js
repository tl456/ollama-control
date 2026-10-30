"use strict";
// UI authentication is the browser's ordinary same-origin login Cookie.
// A failed/interrupted operation is never automatically replayed.
window.WorkbenchTransport = (() => {
  let hooks = {}, statusPromise = null, loginNotified = false;
  const notify = phase => { try { hooks.onState?.(phase); } catch {} };
  const errorFor = (status, data, fallback) => {
    const detail = data?.detail ?? data?.error;
    const error = new Error(typeof detail === 'string' ? detail : fallback || '请求失败 HTTP ' + status);
    error.status = status;
    if (typeof data?.code === 'string') error.code = data.code;
    return error;
  };
  function requireLogin() {
    notify('login-required');
    if (loginNotified) return;
    loginNotified = true;
    try { Promise.resolve(hooks.onLoginRequired?.()).catch(() => {}); } catch {}
  }
  function acceptBootstrap(value) {
    if (!value || typeof value !== 'object') throw errorFor(0, null, '未取得界面状态，请稍后刷新');
    hooks.applyConnection(value);
    loginNotified = false;
    notify('connected');
    return value;
  }
  async function request(url, options = {}) {
    const address = new URL(String(url), location.href);
    const sameOrigin = address.origin === location.origin;
    const response = await fetch(url, {...options,
      ...(sameOrigin ? {credentials: 'same-origin'} : {})});
    if (sameOrigin && address.pathname.startsWith('/api/') && address.pathname !== '/api/auth/login' && response.status === 401)
      requireLogin();
    return response; // No retry on 401/403, network errors, or answer-stream failure.
  }
  function reconnect({manual = false} = {}) {
    if (statusPromise) return statusPromise;
    if (manual) loginNotified = false;
    notify('checking');
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    const operation = (async () => {
      try {
        const response = await request('/api/auth/status', {cache: 'no-store', signal: controller.signal});
        let value;
        try { value = await response.json(); } catch { value = null; }
        if (!response.ok) throw errorFor(response.status, value);
        if (!value || typeof value !== 'object') throw errorFor(0, null, '暂未取得登录状态');
        if(value.password_set && value.authenticated !== true){requireLogin();throw errorFor(401,{detail:'请先登录',code:'login_required'});}
        return acceptBootstrap({auth: value, ...(typeof value.can_manage === 'boolean' ? {can_manage: value.can_manage} : {}),
          ...(typeof value.local === 'boolean' ? {actual_local: value.local} : {})});
      } catch (error) {
        if (error.status !== 401) notify('error');
        throw error.name === 'AbortError' ? errorFor(0, null, '检查连接超时，请稍后再试') : error;
      } finally { clearTimeout(timer); }
    })();
    statusPromise = operation;
    operation.finally(() => { if (statusPromise === operation) statusPromise = null; }).catch(() => {});
    return operation;
  }
  return Object.freeze({configure(value) { hooks = value; }, request, reconnect, acceptBootstrap, errorFor});
})();
