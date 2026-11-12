export async function api(path, method = 'GET', body, options = {}) {
  const {timeoutMs = 30000, signal, ...extra} = options;
  const controller = new AbortController();
  const cancel = () => controller.abort(signal.reason);
  if (signal?.aborted) cancel();
  else signal?.addEventListener('abort',cancel,{once:true});
  const timer = setTimeout(() => controller.abort(new Error('请求超时，请刷新核对后重试')),timeoutMs);
  try {
    const res = await fetch(path, {
      ...extra, method, signal:controller.signal,
      headers: {...extra.headers,...(body ? { 'Content-Type': 'application/json' } : {})},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (controller.signal.aborted) throw controller.signal.reason;
    if (!res.ok) {
      // FastAPI 校验错误的 detail 是对象数组，直接进 Error 会显示 [object Object]
      const detail = typeof data.detail === 'string' ? data.detail : '';
      throw new Error(detail || data.msg || `HTTP ${res.status}`);
    }
    return data;
  } catch(e) {
    if (controller.signal.aborted && !signal?.aborted) throw controller.signal.reason;
    throw e;
  } finally { clearTimeout(timer); signal?.removeEventListener('abort',cancel); }
}


export function serialQueue() {
  let tail = Promise.resolve();
  return (action) => { const task = tail.catch(() => {}).then(action); tail = task; return task; };
}
