/* 编年时间线：右缘时间密度尺 + 按日分块的内容流（v1 桌面端）。
   内容流不另起取数链路：app.js 的 load() 在编年态把新卡交给 appendCard（按日归桶），
   卡片渲染、乐观标记、大图/详情、多选全部复用墙的既有机制。
   尺子数据来自 /api/timeline/outline（按日聚合，筛选口径与 /api/posts 一致）；
   布局记忆在 pw_prefs.layout，`V` 键或视图面板「布局」行切换。
   手机端（≤640px 或触屏）隐藏尺子与切换入口，布局退化可用但不做适配——本轮范围只在桌面。 */

const MONTH_H = 14;   // 尺子月份标签行高（px）

/* ---------- 纯函数（Node 契约测试直接覆盖） ---------- */

export function dayOf(p) {
  /* 库内 created_at 统一东八区 ISO8601（微博原样、X 经 normalize_created_at 归一），
     截前 10 位即当地日，与服务端 outline 的 substr 口径一致。 */
  return (p.created_at || '').slice(0, 10);
}

export function dayLabel(dayStr) {
  const [, m, d] = dayStr.split('-');
  return `${+m}月${+d}日`;
}

export function monthLabel(dayStr, newestYear) {
  const [y, m] = dayStr.split('-');
  return y === newestYear ? `${+m}月` : `${y}年${+m}月`;
}

/* outline days（降序）→ 尺子行几何：月界插标签行；日行高度按帖数开方钳位，
   宽度按当日帖数占峰值比例，保留占比用 --kept 交给 CSS 上色。 */
export function planRows(days) {
  const maxTotal = Math.max(1, ...days.map((d) => d.total));
  const newestYear = (days[0]?.d || '').slice(0, 4);
  const rows = [];
  let prevMonth = '';
  for (const d of days) {
    const month = d.d.slice(0, 7);
    if (month !== prevMonth) {
      rows.push({ kind: 'month', label: monthLabel(d.d, newestYear), day: d.d });
      prevMonth = month;
    }
    const total = d.total || 0;
    rows.push({
      kind: 'day', day: d.d,
      h: Math.min(16, 2 + Math.round(Math.sqrt(total) * 2)),
      w: Math.max(20, Math.round(total / maxTotal * 100)),
      keptW: Math.round((d.kept || 0) / Math.max(1, total) * 100),
      fav: (d.fav || 0) > 0,
    });
  }
  return rows;
}

/* ---------- 运行时（DOM 逻辑全部从 createTimeline/deps 进入） ---------- */

export function createTimeline(deps) {
  const { S, $, api, toast, load, renderViewPanel, railSync, closeOverlays, canSwitch } = deps;
  const feed = $('#feed');
  const sections = new Map();      // day → <section.tday>（插入序 = 内容流序，新→旧）
  let outline = new Map();         // day → {total,kept,discarded,fav,cmts}
  let geo = [];                    // 尺子行几何 [{top,h,day}]
  let geoMap = new Map();          // day → geo 条目 O(1) 索引
  let totalH = 0;
  let outlineSeq = 0;
  let needOutline = false;
  let syncTimer = 0;
  let ruler = null;
  let feedObs = null;
  let lastCurTop = -1, lastCurH = -1;

  const active = () => feed.classList.contains('chrono');
  const motionOk = () => !matchMedia('(prefers-reduced-motion: reduce)').matches;
  const settle = () => new Promise((res) => {
    (function chk() { S.loading ? setTimeout(chk, 50) : res(); })();
  });

  /* ---------- 日卷 ---------- */

  function paintHead(head, day) {
    const o = outline.get(day) || {};
    const bits = [];
    if (o.total != null) {
      bits.push(`${o.total} 帖`);
      if (o.kept) bits.push(`留 ${o.kept}`);
      if (o.fav) bits.push(`★ ${o.fav}`);
      if (o.cmts) bits.push(`注 ${o.cmts}`);
    }
    const d = new Date(`${day}T12:00:00+08:00`);
    const today = new Date(); const yest = new Date(Date.now() - 86400000);
    const rel = d.toDateString() === today.toDateString() ? '今天'
      : d.toDateString() === yest.toDateString() ? '昨天' : '';
    const wd = `周${'日一二三四五六'[d.getDay()]}`;
    head.innerHTML = `<b>${rel ? `${rel} · ` : ''}${dayLabel(day)}</b>`
      + `<span class="tl-wd">${wd}</span>`
      + (bits.length ? `<span class="tday-stats">${bits.join(' · ')}</span>` : '');
  }

  function buildSection(day) {
    const sec = document.createElement('section');
    sec.className = 'tday';
    sec.dataset.day = day;
    const head = document.createElement('header');
    head.className = 'tday-head';
    paintHead(head, day);
    sec.appendChild(head);
    sec.appendChild(Object.assign(document.createElement('div'), { className: 'tday-cols' }));
    // 按日降序就位：新日通常更老，直接落到末尾；乱序到达时找第一个更老日插到它前面
    let anchor = null;
    for (const el of feed.querySelectorAll('.tday'))
      if (el.dataset.day < day) { anchor = el; break; }
    feed.insertBefore(sec, anchor);
    return sec;
  }

  function sectionFor(p) {
    const day = dayOf(p);
    let sec = sections.get(day);
    if (!sec || !sec.isConnected) {
      sec = buildSection(day);
      sections.set(day, sec);
    }
    return sec;
  }

  /* app.js load() 在编年态的收卡口（替代最短列栈）；p 缺席时按 sid 回查主列表 */
  function appendCard(card, p) {
    const post = p || S.posts.find((q) => q.sid === card.dataset.sid);
    if (!post) return;
    sectionFor(post).querySelector('.tday-cols').appendChild(card);
  }

  /* 撤销恢复的再插入口：日内按 S.posts 全序（created_at 降序）归位 */
  function insertCard(card, ref) {
    const posts = S.posts || [];
    const idxMap = new Map();
    for (let i = 0; i < posts.length; i += 1) idxMap.set(posts[i].sid, i);
    const idx = idxMap.get(card.dataset.sid);
    if (idx === undefined) return;
    const cols = sectionFor(posts[idx]).querySelector('.tday-cols');
    for (const el of cols.children) {
      if (el === card) continue;
      const other = idxMap.get(el.dataset.sid);
      if (other !== undefined && other > idx) { cols.insertBefore(card, el); return; }
    }
    cols.appendChild(card);
  }

  /* ---------- 密度尺 ---------- */

  function mountRuler() {
    ruler = document.createElement('div');
    ruler.id = 'truler';
    ruler.hidden = true;
    ruler.innerHTML = '<div class="tl-track"><div class="tl-inner"><div class="tl-cursor"></div></div></div>'
      + '<div class="tl-bubble" hidden></div>';
    document.body.appendChild(ruler);
    const track = ruler.querySelector('.tl-track');

    let scrubbing = false, pointerId = null;
    const dayAt = (clientY, rect) => {
      const r = rect || track.getBoundingClientRect();
      const y = clientY - r.top + track.scrollTop;
      if (!geo.length) return null;
      for (const g of geo)
        if (y >= g.top && y < g.top + g.h) return g.day;
      return y < geo[0].top ? geo[0].day : geo[geo.length - 1].day;
    };
    const showBubble = (e) => {
      const tr = track.getBoundingClientRect();
      const day = dayAt(e.clientY, tr);
      const bubble = ruler.querySelector('.tl-bubble');
      if (!day) { bubble.hidden = true; return; }
      const o = outline.get(day) || {};
      const bits = [`${o.total ?? 0} 帖`];
      if (o.kept) bits.push(`留 ${o.kept}`);
      if (o.cmts) bits.push(`注 ${o.cmts}`);
      bubble.textContent = `${dayLabel(day)} · ${bits.join(' · ')}`;
      bubble.style.top = `${Math.min(Math.max(e.clientY, 48), window.innerHeight - 24)}px`;
      bubble.style.right = `${window.innerWidth - tr.left + 10}px`;
      bubble.hidden = false;
    };
    track.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      scrubbing = true; pointerId = e.pointerId;
      track.setPointerCapture(e.pointerId);
      showBubble(e);
      e.preventDefault();
    });
    track.addEventListener('pointermove', (e) => {
      if (!scrubbing || e.pointerId !== pointerId) return;
      showBubble(e);
    });
    const endScrub = (e) => {
      if (!scrubbing || e.pointerId !== pointerId) return;
      scrubbing = false;
      ruler.querySelector('.tl-bubble').hidden = true;
      const day = dayAt(e.clientY);
      if (day) gotoDay(day);   // 点按与拖动松手同语义：都是「去那天」
    };
    track.addEventListener('pointerup', endScrub);
    track.addEventListener('pointercancel', () => {
      scrubbing = false;
      ruler.querySelector('.tl-bubble').hidden = true;
    });
  }

  function renderRuler() {
    const inner = ruler.querySelector('.tl-inner');
    const track = ruler.querySelector('.tl-track');
    const keepScroll = track.scrollTop;
    const trackH = track.clientHeight;
    inner.querySelectorAll('.tl-row').forEach((el) => el.remove());
    geo = []; geoMap = new Map(); totalH = 0; lastCurTop = -1; lastCurH = -1;
    const frag = document.createDocumentFragment();
    for (const row of planRows([...outline.values()])) {
      const el = document.createElement('div');
      el.className = `tl-row ${row.kind === 'month' ? 'tl-month' : 'tl-day'}`;
      el.dataset.day = row.day;
      let entry;
      if (row.kind === 'month') {
        el.textContent = row.label;
        entry = { top: totalH, h: MONTH_H, day: row.day };
        totalH += MONTH_H;
      } else {
        el.style.height = `${row.h}px`;
        if (row.fav) el.classList.add('fav');
        const bar = document.createElement('i');
        bar.className = 'tl-bar';
        bar.style.width = `${row.w}%`;
        bar.style.setProperty('--kept', `${row.keptW}%`);
        el.appendChild(bar);
        entry = { top: totalH, h: row.h, day: row.day };
        totalH += row.h;
      }
      geo.push(entry);
      if (!geoMap.has(row.day)) geoMap.set(row.day, entry);
      frag.appendChild(el);
    }
    inner.appendChild(frag);
    track.scrollTop = Math.min(keepScroll, Math.max(0, totalH - trackH));
  }

  const geoByDay = (day) => geoMap.get(day) || geo[geo.length - 1];

  function updateCursor() {
    if (!ruler || !active()) return;
    const cur = ruler.querySelector('.tl-cursor');
    if (!geo.length) {
      if (lastCurH !== 0) { cur.style.height = '0px'; lastCurH = 0; }
      return;
    }
    // 读写分离：先一次性读取视口、轨道和日卷矩形，后统一写入样式与滚动位
    const track = ruler.querySelector('.tl-track');
    const trTop = track.scrollTop;
    const trH = track.clientHeight;
    const vh = window.innerHeight;
    const scrollY = window.scrollY;
    const topEdge = 48;
    let first = null, last = null;
    const measured = [];
    for (const [day, sec] of sections) {
      const r = sec.getBoundingClientRect();
      measured.push({ day, top: r.top, bottom: r.bottom });
      if (r.bottom < topEdge || r.top > vh) continue;
      if (first == null) first = day;
      last = day;
    }
    if (first == null) {   // 视口落在区块间隙/末尾：复用刚才量过的 top，不二次读布局
      const mid = scrollY + vh / 2;
      let best = null, bestDist = Infinity;
      for (const m of measured) {
        const dist = Math.abs(m.top + scrollY - mid);
        if (dist < bestDist) { bestDist = dist; best = m.day; }
      }
      first = last = best;
    }
    if (first == null) {
      if (lastCurH !== 0) { cur.style.height = '0px'; lastCurH = 0; }
      return;
    }
    const g1 = geoByDay(first); const g2 = geoByDay(last);
    const top = g1.top; const height = Math.max(4, g2.top + g2.h - g1.top);
    const bottom = top + height;
    if (top !== lastCurTop) { cur.style.top = `${top}px`; lastCurTop = top; }
    if (height !== lastCurH) { cur.style.height = `${height}px`; lastCurH = height; }
    if (top < trTop + 4) track.scrollTop = Math.max(0, top - 12);
    else if (bottom > trTop + trH - 4)
      track.scrollTop = Math.min(Math.max(0, totalH - trH), bottom - trH + 12);
  }

  async function gotoDay(day) {
    if (!active()) return;
    let sec = sections.get(day);
    if (!sec) {
      if (!S.hasMore) { toast('已经翻到最早了'); return; }
      toast(`正在装载 ${dayLabel(day)}…`);
      let guard = 0;
      while (!sections.has(day) && S.hasMore && guard++ < 60) {
        await settle();
        if (!S.hasMore) break;
        load(false);
        await settle();
      }
      sec = sections.get(day);
      if (!sec) { toast(`${dayLabel(day)} 在当前筛选下没有帖子`); return; }
    }
    const topbar = document.querySelector('.topbar');
    const y = sec.getBoundingClientRect().top + window.scrollY
      - (topbar?.offsetHeight || 41) - 6;
    window.scrollTo({ top: Math.max(0, y), behavior: motionOk() ? 'smooth' : 'auto' });
  }

  /* ---------- 数据与联动 ---------- */

  async function refreshOutline() {
    if (!active()) return;
    const seq = ++outlineSeq;
    const q = new URLSearchParams({ status: S.status, group: S.group, author: S.author,
      range: S.range, q: S.searchText,
      has_cmts: S.hasCmts === '1' ? '1' : 'all', source: S.source });
    if (S.tags && S.tags !== 'all') q.set('tags', S.tags);
    if (S.searchScope && S.searchScope !== 'all') q.set('q_scope', S.searchScope);
    try {
      const data = await api('/api/timeline/outline?' + q);
      if (seq !== outlineSeq || !active()) return;
      outline = new Map(data.days.map((d) => [d.d, d]));
      renderRuler();
      for (const [day, sec] of sections) {
        const head = sec.querySelector('.tday-head');
        if (head) paintHead(head, day);
      }
      updateCursor();
    } catch (e) { toast(e.message); }
  }

  /* feed 的任何结构变化（翻页追加、标记摘卡、筛选重载）统一在这里收口：
     仅在编年态激活时挂载观察者，退出编年态立即 disconnect，避免普通瀑布流空转。 */
  function connectObserver() {
    if (!feedObs && typeof MutationObserver === 'function') {
      feedObs = new MutationObserver((records) => {
        if (!active()) return;
        for (const r of records)
          for (const n of r.removedNodes)
            if (n.nodeType === 1 && (n.classList.contains('card') || n.classList.contains('tday')))
              needOutline = true;
        clearTimeout(syncTimer);
        syncTimer = setTimeout(() => {
          syncTimer = 0;
          if (!active()) return;
          for (const [day, sec] of [...sections]) {
            if (!sec.isConnected) { sections.delete(day); continue; }
            if (!sec.querySelector('.card')) { sec.remove(); sections.delete(day); needOutline = true; }
          }
          if (needOutline) { needOutline = false; refreshOutline(); }
          updateCursor();
        }, 120);
      });
    }
    if (feedObs && feed) {
      try { feedObs.observe(feed, { childList: true, subtree: true }); } catch (e) {}
    }
  }
  function disconnectObserver() {
    feedObs?.disconnect();
    clearTimeout(syncTimer);
    syncTimer = 0;
    needOutline = false;
  }

  let scrollRaf = 0;
  function bindScroll() {
    const onScroll = () => {
      if (!active() || scrollRaf) return;
      scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; if (active()) updateCursor(); });
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
  }

  /* ---------- 布局开关 ---------- */

  function applyLayoutDom(on) {
    if (on) document.documentElement.dataset.layout = 'chrono';
    else delete document.documentElement.dataset.layout;
    feed.classList.toggle('chrono', on);
    if (ruler) ruler.hidden = !on;
    if (on) {   // 日卷粘滞头吸附位：移动端=顶栏实高，桌面端顶栏隐藏贴顶 8px（#truler 由 CSS max(64px, --tl-top) 避让右上胶囊）
      const tb = document.querySelector('.topbar');
      const hasTopbar = tb && (typeof getComputedStyle !== 'function' || getComputedStyle(tb).display !== 'none');
      document.documentElement.style.setProperty('--tl-top', hasTopbar ? `${tb.offsetHeight + 6}px` : '8px');
    }
  }

  function syncToggle() {
    document.querySelectorAll('#f-layout .segctl > button, #f-layout button').forEach((b) =>
      b.classList.toggle('on', (b.dataset.v === 'chrono') === active()));
  }

  function setLayout(on) {
    if (on === active()) return;
    if (on && canSwitch && !canSwitch()) { toast('先退出浮层，再切换布局'); return; }
    if (on && S.sort) {   // 点赞序不打时间顺序，编年态强制时间序（面板行由 CSS 隐藏）
      S.sort = '';
      renderViewPanel?.();
    }
    applyLayoutDom(on);
    try {
      if (window.pwSetPref) window.pwSetPref('layout', on ? 'chrono' : '');
    } catch (e) { /* 存不了就本次会话有效 */ }
    railSync?.();   // 左轨编年行高亮即时跟（V 键路径不经 renderViewPanel）
    closeOverlays?.();
    sections.clear();
    if (on) {
      connectObserver();
      refreshOutline();
    } else {
      disconnectObserver();
      ++outlineSeq;
      geo = []; geoMap = new Map(); outline = new Map(); totalH = 0;
      lastCurTop = -1; lastCurH = -1;
    }
    load(true);
    syncToggle();
  }

  function mountToggle() {
    // 布局段已在视图面板「视图」行静态声明（与排序合并单行），这里只绑事件
    const ctl = document.getElementById('f-layout');
    if (!ctl) return;
    ctl.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-v]');
      if (b) setLayout(b.dataset.v === 'chrono');
    });
  }

  function bindKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'v' && e.key !== 'V') return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (t && t.closest && t.closest('input, textarea, select, [contenteditable]')) return;
      setLayout(!active());
    });
  }

  function init() {
    mountToggle();
    mountRuler();
    bindScroll();
    bindKeys();
    const saved = (() => {
      try { return window.pwGetPrefs?.().layout === 'chrono'; } catch (e) { return false; }
    })();
    if (saved) {   // 静默应用：不触发 load，启动首屏的 load(true) 自然走编年收卡口
      if (S.sort) S.sort = '';   // 点赞序不打时间序：URL 带 ?sort=keep_count 也不带进编年态
      applyLayoutDom(true);
      connectObserver();
      refreshOutline();
    }
    syncToggle();
  }

  return { init, active, appendCard, insertCard, setLayout, gotoDay, refreshOutline };
}
