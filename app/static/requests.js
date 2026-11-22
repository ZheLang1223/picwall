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
      // FastAPI 校验错误的 detail 是对象数组，直接进 Error 会显示 [object Object]，取首项 msg
      const raw = data.detail;
      const detail = typeof raw === 'string' ? raw
        : Array.isArray(raw) && typeof raw[0]?.msg === 'string' ? raw[0].msg : '';
      const err = new Error(detail || data.msg || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  } catch(e) {
    if (controller.signal.aborted && !signal?.aborted) throw controller.signal.reason;
    if (e instanceof TypeError && !controller.signal.aborted && !signal?.aborted) {
      // 断网/服务器不可达时 fetch 抛的 TypeError 文案各浏览器不同且是英文，翻译成人话
      const err = new Error('网络连接失败，请检查网络后重试');
      err.network = true;
      err.cause = e;
      throw err;
    }
    throw e;
  } finally { clearTimeout(timer); signal?.removeEventListener('abort',cancel); }
}


export function serialQueue() {
  let tail = Promise.resolve();
  return (action) => { const task = tail.catch(() => {}).then(action); tail = task; return task; };
}
