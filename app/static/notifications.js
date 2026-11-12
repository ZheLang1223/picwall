const $ = (s) => document.querySelector(s);
/* 栈式 toast：#toast 是容器，条目（.toast-item）独立计时、互不顶替——
   快筛连裁时带「撤销」的条目在其 6s 反悔窗口内不会被后续状态消息顶掉。
   同屏上限 3 条，超容先逐出最旧的**无动作**条目（全是带动作的才逐最旧）。
   persistent 条目带 ✕ 手动关闭（无动作也能关掉）；悬停暂停计时、移出续跑。 */
const MAX_ITEMS = 3;

function makeItem(msg, action, options) {
  const item = document.createElement('div');
  item.className = 'toast-item';
  const span = document.createElement('span');
  span.textContent = msg;   // textContent：调用方可能拼接远端错误消息
  item.appendChild(span);
  const acts = action ? (Array.isArray(action) ? action : [action]) : [];
  let hasAction = false;
  for (const act of acts) {
    if (!act || !act.label) continue;
    hasAction = true;
    const b = document.createElement('button');
    b.textContent = act.label;
    b.onclick = () => { removeItem(item); act.fn(); };
    item.appendChild(b);
  }
  if (options.persistent) {   // 常驻提示必须有显式出口
    const x = document.createElement('button');
    x.className = 't-x';
    x.textContent = '✕';
    x.setAttribute('aria-label', '关闭提示');
    x.onclick = () => removeItem(item);
    item.appendChild(x);
  }
  item._hasAction = hasAction;
  // 带撤销的 toast 停留更久（Gmail 式：给足反悔时间）
  item._delay = options.duration || (hasAction ? 6000 : 2800);
  item.addEventListener('mouseenter', () => pauseItem(item));
  item.addEventListener('mouseleave', () => armItem(item));
  return item;
}

function removeItem(item) {
  clearTimeout(item._timer);
  item._timer = 0;
  item.remove();
}

function armItem(item) {   // 起跑/续跑倒计时
  clearTimeout(item._timer);
  item._deadline = Date.now() + item._delay;
  item._timer = setTimeout(() => removeItem(item), item._delay);
}

function pauseItem(item) {   // 悬停暂停：记住剩余时长，移出后续跑
  if (!item._timer) return;
  clearTimeout(item._timer);
  item._timer = 0;
  item._delay = Math.max(800, (item._deadline || 0) - Date.now());
}

function evictOverflow(container) {   // 超容逐出：优先无动作的最旧条目
  while (container.children.length > MAX_ITEMS) {
    const items = [...container.children];
    const victim = items.find((el) => !el._hasAction) || items[0];
    removeItem(victim);
  }
}

export function toast(msg, action, options = {}) {
  const t = $('#toast');
  const item = makeItem(msg, action, options);
  t.appendChild(item);
  evictOverflow(t);
  requestAnimationFrame(() => item.classList.add('show'));   // 下一帧再亮，保 opacity 过渡
  if (!options.persistent) armItem(item);
}
