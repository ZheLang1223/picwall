/* 悬浮球功能入口：菜单项由注册表驱动，新功能 = 注册表加一行。
   桌面鼠标悬浮展开（60ms 意图延迟开 / 180ms 缓冲收），单击开合对任何指针一致
   （悬浮已开时单击即收），键盘 Enter/Space 走合成 click 同样可开。 */
export function createFab({ $, items }) {
  const fab = $('#fab'), btn = $('#fab-btn'), menu = $('#fab-menu');
  const hoverable = () => window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  let openTimer = 0, closeTimer = 0;

  function open() {
    clearTimeout(closeTimer);
    if (!menu.hidden) return;
    menu.innerHTML = '';
    for (const item of items) {
      const b = document.createElement('button');
      b.className = 'setrow';
      const label = typeof item.label === 'function' ? item.label() : item.label;
      b.innerHTML = item.icon || '';
      const span = document.createElement('span');
      span.textContent = label ?? '';
      b.appendChild(span);
      b.onclick = (e) => { e.stopPropagation(); close(); item.run(); };
      menu.appendChild(b);
    }
    setMenuSide();
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  }
  function close() {
    clearTimeout(openTimer);
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  }

  // 菜单是球的孩子：指针在球与菜单之间移动不触发 leave，180ms 缓冲只兜真正的离开
  fab.addEventListener('mouseenter', () => {
    if (!hoverable()) return;
    clearTimeout(closeTimer);
    openTimer = setTimeout(open, 60);
  });
  fab.addEventListener('mouseleave', () => {
    if (!hoverable()) return;
    clearTimeout(openTimer);
    closeTimer = setTimeout(close, 180);
  });
  /* 拖动：pointer 事件统一鼠标/触屏，位移 >6px 算拖动并吞掉尾随 click（点按开合不受影响）。
     位置记 localStorage（pw_fab_pos），窗口缩放时夹回屏内；球在上半屏时菜单向下展开。 */
  const POS_KEY = 'pw_fab_pos';
  let drag = null, suppressClick = false, resizeRaf = 0;
  const clamp = (x, y, fw = fab.offsetWidth, fh = fab.offsetHeight) => {
    const maxX = window.innerWidth - fw, maxY = window.innerHeight - fh;
    return { x: Math.min(Math.max(x, 8), Math.max(8, maxX - 8)),
             y: Math.min(Math.max(y, 8), Math.max(8, maxY - 8)) };
  };
  const applyPos = ({ x, y }) => {
    fab.style.left = `${x}px`; fab.style.top = `${y}px`;
    fab.style.right = 'auto'; fab.style.bottom = 'auto';
  };
  function setMenuSide() {
    const nearTop = fab.getBoundingClientRect().top < window.innerHeight / 2;
    menu.classList.toggle('menu-down', nearTop);
  }
  try {   // 恢复上次拖动的位置（越界夹回）
    const p = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
    if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) applyPos(clamp(p.x, p.y));
  } catch {}
  window.addEventListener('resize', () => {
    if (!fab.style.left || resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      const fr = fab.getBoundingClientRect();
      applyPos(clamp(fr.left, fr.top, fr.width || fab.offsetWidth, fr.height || fab.offsetHeight));
      if (!menu.hidden) setMenuSide();
    });
  });
  btn.addEventListener('pointerdown', (e) => {
    suppressClick = false;   // 兜底重置：上一轮遗留的吞击标记（如无尾随 click 的异常路径）绝不吞本次真实点按
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const fr = fab.getBoundingClientRect();
    drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY,
             ox: e.clientX - fr.left, oy: e.clientY - fr.top,
             fw: fab.offsetWidth, fh: fab.offsetHeight, moved: false };
    try { btn.setPointerCapture(e.pointerId); } catch {}   // 非活跃 pointer（如合成事件）无捕获也可拖
  });
  btn.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 6) return;
    drag.moved = true;
    close();   // 拖动即收菜单，悬浮重开计时也一并清掉
    e.preventDefault();
    applyPos(clamp(e.clientX - drag.ox, e.clientY - drag.oy, drag.fw, drag.fh));
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const moved = drag.moved;
    drag = null;
    if (!moved) return;
    if (e.type !== 'pointercancel') {   // 真拖完才吞尾随 click；click 同步跟在 pointerup 后，下一宏任务自动解除武装
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
    }   // pointercancel（系统手势/触点被接管）没有尾随 click：照旧存位置+重摆菜单朝向，但绝不武装——否则吞掉下一次真实点按
    const fr = fab.getBoundingClientRect();
    try { localStorage.setItem(POS_KEY, JSON.stringify({ x: fr.left, y: fr.top })); } catch {}
    setMenuSide();
  };
  btn.addEventListener('pointerup', endDrag);
  btn.addEventListener('pointercancel', endDrag);
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (suppressClick) { suppressClick = false; return; }   // 刚拖完：这不是点按
    if (!menu.hidden) { close(); return; }
    open();   // 点击=显式开合：任何指针一致（此前鼠标点击被悬浮路径吞掉，点了没反应）；键盘合成 click 同样走这里
  });
  menu.addEventListener('click', (e) => e.stopPropagation());
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !e.target.closest('#fab')) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) { e.stopPropagation(); close(); }
  });

  return { setHidden: (v) => { fab.classList.toggle('fab-off', v); if (v) close(); } };
}
