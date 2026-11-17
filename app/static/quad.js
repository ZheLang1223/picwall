/* 四格快筛（桌面专属会话）：一屏四张固定卡位、键盘逐格直裁、裁一补一。
   鼠标零绑定——裁决只走键盘：↑/F 保留、↓/D 不要、Z 撤销上一笔；
   ←→ 移焦（细看态翻图）、空格大图、回车细看、M/L 收藏/原帖、Esc 分层退出。
   队列从 S.posts(pending) 派生不另立账本：mark() 摘除即出队，撤销回插即回场；
   卡面用原图档（每屏仅 4 张 + 预取，本机盘不构成压力），槽位节点常驻只换内容。 */
import { keepBadge, avatarSrc } from './view-state.js';

/* 纯簿记（可单测）：取下一帖——补位栈优先，其次按当前列表找未入槽的待处理帖 */
export function pickNext(stash, posts, usedSids) {
  if (stash.length) return stash.shift();
  return posts.find(p => p.status === 'pending' && !usedSids.has(p.sid)) || null;
}

/* 纯簿记（可单测）：焦点按阅读序前进，跳过空槽；全空返回 -1（会话收尾） */
export function nextFocus(slots, from) {
  for (let k = 1; k <= 4; k++) {
    const j = (from + k) % 4;
    if (slots[j]) return j;
  }
  return -1;
}

export function createQuad({ S, $, toast, esc, fmtDate, fmtHM, icons,
                             mark, toggleFavorite, getLastDrop, undoLastMark,
                             load, openLB, openLinkForSid, switchStatusView,
                             animatedHide, cancelHide }) {
  const Q = { active: false, slots: [null, null, null, null], focus: 0,
              stash: [], lastDecided: null, zoom: false, zoomSlot: 0, zoomIdx: 0,
              prefetched: new Set(), inflight: 0,
              picIdx: [0, 0, 0, 0], lastRotate: [0, 0, 0, 0], rotateTimer: 0 };
  /* 打开四格的那个控件：关闭后把焦点还给它（role=dialog 焦点进出） */
  let quadOpener = null;

  const slotEl = (i) => $('#quad .q-slot[data-slot="' + i + '"]');
  const zoomEl = () => $('#quad .q-zoom');
  const usedSids = () => new Set(Q.slots.filter(Boolean).map(p => p.sid));

  /* ✕ 关闭钮：会话里鼠标唯一可达的出口（裁决仍只走键盘）——
     误入会话不再只能靠 Esc 逃生，也给读屏/键盘一个显式关闭控件 */
  const closeBtn = $('#quad .q-close');
  if (closeBtn) {
    closeBtn.innerHTML = icons.x || '';
    closeBtn.onclick = () => close();
  }

  /* ---------- 卡面：renderCard 的四格变体（原图直出、无勾选点、无评论预览行） ---------- */
  /* 比例感知三档：正常图整图 contain（一眼看全）；竖长图 cover 顶部锚定（长图内容自上而下，
     首屏辨识度最高）；横超宽 cover 居中。非 contain 档带角标明示「没看全」，深入走空格/回车。
     阈值与库况口径一致（w/h 均为入库元数据，无需等解码）。 */
  function fitInfo(pc) {
    const w = pc?.w || 0, h = pc?.h || 0;
    if (w && h && h / w > 2) return { cls: 'q-fit-top', tag: '长图' };
    if (w && h && w / h > 2.2) return { cls: 'q-fit-center', tag: '全景' };
    return { cls: 'q-fit-contain', tag: '' };
  }
  function applyQuadPic(i, pc) {
    const fit = fitInfo(pc);
    const el = slotEl(i);
    const img = el?.querySelector('.pics img');
    if (img) { img.src = '/media/' + (pc.kind === 'video' ? (pc.thumb || pc.file) : pc.file); img.className = fit.cls; }
    const hint = el?.querySelector('.pics-hint');
    if (hint) { hint.textContent = pc.kind === 'video' ? '视频' : fit.tag; hint.hidden = !(fit.tag || pc.kind === 'video'); }
    const p = Q.slots[i];
    const n = (p?.pics || []).length;
    const pill = el?.querySelector('.pics-pill');
    if (pill && n > 1) {
      pill.textContent = `${(Q.picIdx[i] || 0) + 1}/${n} 图`;
    }
    const bar = el?.querySelector('.q-progress-bar');
    if (bar && i === Q.focus) {
      bar.style.animation = 'none';
      void bar.offsetHeight;
      bar.style.animation = 'qProgress 3s linear';
    }
  }
  function quadCardHTML(p) {
    const first = (p.pics || [])[0];
    const n = (p.pics || []).length;
    const isVid = first?.kind === 'video';
    const fit = fitInfo(first);
    const hint = (fit.tag || isVid) ? `<span class="pics-hint">${isVid ? '视频' : fit.tag}</span>` : '';
    const pics = first
      ? `<img class="${esc(fit.cls)}" src="/media/${esc(isVid ? (first.thumb || first.file) : first.file)}" alt="${esc(p.author)} 的微博图" decoding="async" data-file="${esc(first.file)}">${hint}`
      : '';
    const pill = n > 1 ? `<span class="pics-pill">1/${n} 图</span>` : '';
    const progress = n > 1 ? '<div class="q-progress-track"><div class="q-progress-bar"></div></div>' : '';
    const srcChip = p.source && p.source !== 'weibo'
      ? `<span class="src">${p.source === 'x' ? 'X' : esc(p.source)}</span>` : '';
    const kb = p.keep_count >= 2 ? keepBadge(p.keep_count) : null;
    const keepn = kb ? `<span class="chip keepn t${esc(kb.tier)}" title="${esc(kb.title)}">${esc(kb.text)}</span>` : '';
    const cmCount = Number(p.comment_count) || 0;
    const cmc = cmCount
      ? `<span class="chip cmc" title="有 ${cmCount} 条评论">${icons.comment}${cmCount}</span>` : '';
    // 收藏不再摆 chip：琥珀描边由 .q-slot .card.fav 表达（与墙卡一致）
    const badges = [keepn, cmc,
      p.image_complete === false ? '<span class="kind">图片待补齐</span>' : '',
    ].filter(Boolean).join('');
    const av = avatarSrc(p.avatar);
    return `
      <div class="pics">${pics}${pill}${progress}</div>
      <div class="meta">
        ${av ? `<img class="avatar" src="${esc(av)}" alt="" decoding="async">`
             : `<span class="avatar">${esc((p.author || '微')[0])}</span>`}
        <b>${esc(p.author)}</b>
        ${srcChip}
      </div>
      ${badges ? `<div class="badges">${badges}</div>` : ''}`;
  }

  function renderSlot(i, post) {
    Q.slots[i] = post;
    Q.picIdx[i] = 0; Q.lastRotate[i] = Date.now();   // 补位新帖从头轮起，且赋予完整初始观察周期
    const el = slotEl(i);
    if (!el) return;
    el.classList.toggle('empty', !post);
    el.innerHTML = post
      ? `<article class="card q-card${post.favorite ? ' fav' : ''}" data-sid="${esc(post.sid)}">${quadCardHTML(post)}</article>`
      : '';
  }

  function takeNext() {
    return pickNext(Q.stash, S.posts, usedSids());
  }

  function renderAll() {
    for (let i = 0; i < 4; i++) renderSlot(i, takeNext());
    applyFocus();
    prefetch();
  }

  function applyFocus() {
    Q.lastRotate[Q.focus] = Date.now();   // 移焦时对齐 3s 倒计时时钟窗口，杜绝动画掐断
    for (let i = 0; i < 4; i++) {
      const isF = i === Q.focus;
      const el = slotEl(i);
      el?.classList.toggle('focused', isF);
      const bar = el?.querySelector('.q-progress-bar');
      if (bar) {
        bar.style.animation = 'none';
        void bar.offsetHeight;
        bar.style.animation = isF ? 'qProgress 3s linear' : 'none';
      }
    }
  }

  /* 补位原图预取：在途封顶 4，候选 = 列表里未入槽未预取的待处理帖前几张 */
  function prefetch() {
    if (typeof Image === 'undefined') return;   // 测试环境无 Image
    let budget = 4 - Q.inflight;
    const used = usedSids();
    for (const p of S.posts) {
      if (budget <= 0) break;
      if (p.status !== 'pending' || used.has(p.sid) || Q.prefetched.has(p.sid)) continue;
      const pc = (p.pics || [])[0];
      if (!pc) continue;
      Q.prefetched.add(p.sid);
      Q.inflight += 1;
      const im = new Image();
      im.onload = im.onerror = () => { Q.inflight = Math.max(0, Q.inflight - 1); };
      im.src = '/media/' + (pc.kind === 'video' ? (pc.thumb || pc.file) : pc.file);
      budget -= 1;
    }
  }

  /* ---------- 会话 ---------- */
  async function open() {
    const ae = (typeof document !== 'undefined') ? document.activeElement : null;
    if (Q.active || S.readOnly) return;
    if ((S.st?.counts?.pending || 0) === 0) { toast('新增已筛完 ✓'); return; }
    if (S.status !== 'pending') { switchStatusView('pending'); await load(true); }
    else if (!S.posts.length) await load(true);
    if (!S.posts.some(p => p.status === 'pending')) { toast('没有待处理的新增'); return; }
    Q.active = true;
    quadOpener = (ae && typeof Element !== 'undefined' && ae instanceof Element
                  && ae !== document.body) ? ae : null;
    quadKept = 0; quadDropped = 0;
    Q.slots = [null, null, null, null];
    Q.focus = 0; Q.stash = []; Q.lastDecided = null;
    Q.zoom = false; zoomEl().hidden = true;
    Q.prefetched = new Set(); Q.inflight = 0;
    if (typeof document !== 'undefined' && document.body) document.body.style.overflow = 'hidden';
    const quad = $('#quad');
    cancelHide(quad);
    quad.hidden = false;
    renderAll();
    $('#quad .q-close')?.focus?.({ preventScroll: true });
    startRotate();
    hintOnce();
  }

  /* 多图槽位自动轮换：聚焦格 3s/步（裁决要盯着看），非聚焦格 1.2s/步（预览要快）；
     仅轮四格卡面，细看态让位手动 ←→。tick 500ms，按各槽周期到点才换。 */
  function startRotate() {
    stopRotate();
    const t = setInterval(() => {
      if (Q.zoom) return;
      const now = Date.now();
      for (let i = 0; i < 4; i++) {
        const p = Q.slots[i];
        const pics = p?.pics || [];
        if (pics.length < 2) continue;
        const period = i === Q.focus ? 3000 : 1200;
        if (now - (Q.lastRotate[i] || 0) < period) continue;
        Q.lastRotate[i] = now;
        Q.picIdx[i] = (Q.picIdx[i] + 1) % pics.length;
        applyQuadPic(i, pics[Q.picIdx[i]]);
      }
    }, 500);
    t.unref?.();   // 测试进程不因轮换定时器挂住
    Q.rotateTimer = t;
  }
  function stopRotate() {
    if (Q.rotateTimer) { clearInterval(Q.rotateTimer); Q.rotateTimer = 0; }
  }

  function close() {
    if (!Q.active) return;
    Q.active = false;
    stopRotate();
    Q.zoom = false; zoomEl().hidden = true;
    Q.slots = [null, null, null, null];
    Q.lastDecided = null;
    animatedHide($('#quad'));
    if (typeof document !== 'undefined' && quadOpener && quadOpener.isConnected) {
      const cur = document.activeElement;
      const q = $('#quad');
      if (!cur || cur === document.body || (q && q.contains(cur))) {
        quadOpener.focus?.({ preventScroll: true });
      }
    }
    quadOpener = null;
    const lb = $('#lightbox');
    if (lb.hidden || lb.classList.contains('closing')) {
      if (typeof document !== 'undefined' && document.body) document.body.style.overflow = '';
    }
  }

  let quadKept = 0, quadDropped = 0;
  function finish() {
    const undo = getLastDrop();
    const stats = (quadKept || quadDropped) ? ` 保留 ${quadKept} · 不要 ${quadDropped}` : '';
    toast(`四格快筛完成：新增已清完 ✓${stats}`, undo ? {
      label: '撤销', fn: () => undoLastMark(undo)
    } : undefined);
    quadKept = 0; quadDropped = 0;
    close();
    switchStatusView('kept');   // 筛完直接看成果：进入保留视图
  }

  function hintOnce() {
    try {
      if (localStorage.getItem('pw_quadhint')) return;
      localStorage.setItem('pw_quadhint', '1');
    } catch (e) { /* 存不了就用着 */ }
    toast('↑/F 保留 · ↓/D 不要 · Z 撤销 · 空格 大图 · 回车 细看 · Esc 退出');
  }

  /* ---------- 裁决与补位 ---------- */
  async function decide(dir) {
    if (S.readOnly) return;
    const idx = Q.zoom ? Q.zoomSlot : Q.focus;
    const p = Q.slots[idx];
    if (!p) { if (!Q.zoom) advanceFocus(); return; }
    const r = mark(p.sid, dir);
    if (!r) return;   // 连按竞速时帖已被前一笔摘走
    if (dir === 'kept') quadKept += 1; else quadDropped += 1;
    Q.lastDecided = { slot: idx, sid: p.sid, dir };
    ghost(idx, dir);
    if (Q.zoom) closeZoom();
    let np = takeNext();
    if (!np && S.hasMore) { await load(false); np = takeNext(); }
    renderSlot(idx, np);
    advanceFocus();
    prefetch();
  }

  function advanceFocus() {
    const j = nextFocus(Q.slots, Q.focus);
    if (j < 0) { finish(); return; }
    Q.focus = j;
    applyFocus();
  }

  function moveFocus(d) {
    Q.focus = (Q.focus + d + 4) % 4;
    applyFocus();
  }

  function ghost(i, dir) {
    const slot = slotEl(i);
    const card = slot?.querySelector?.('.card');
    if (!card || !card.cloneNode) return;
    const g = card.cloneNode(true);
    g.classList.remove('focused');
    g.classList.add('q-ghost', dir === 'kept' ? 'fly-keep' : 'fly-drop');
    slot.appendChild(g);
    setTimeout(() => g.remove(), 240);
  }

  /* ---------- Z 撤销上一笔：原帖回格、占位帖退回队首、焦点退回该格 ---------- */
  async function undoLast() {
    const op = getLastDrop();
    const rec = Q.lastDecided;
    if (!op || !rec || op.p.sid !== rec.sid) { toast('没有可撤销的裁决'); return; }
    if (Date.now() > op.expiresAt) { toast('撤销已过期，可在对应视图里恢复'); return; }
    const displaced = Q.slots[rec.slot];
    const restored = await undoLastMark(op);
    if (!restored || !Q.active) return;
    if (op?.target === 'kept' && quadKept > 0) quadKept -= 1;
    else if (op?.target === 'discarded' && quadDropped > 0) quadDropped -= 1;
    Q.lastDecided = null;
    if (displaced && displaced.sid !== restored.sid) Q.stash.unshift(displaced);
    renderSlot(rec.slot, restored);
    Q.focus = rec.slot;
    applyFocus();
    prefetch();
  }

  /* ---------- 细看单张（轻量层，不复用详情传送带） ---------- */
  function openZoom() {
    const p = Q.slots[Q.focus];
    if (!p) return;
    Q.zoom = true; Q.zoomSlot = Q.focus; Q.zoomIdx = 0;
    renderZoom();
    zoomEl().hidden = false;
  }

  function renderZoom() {
    const p = Q.slots[Q.zoomSlot];
    if (!p) return;
    const pics = p.pics || [];
    Q.zoomIdx = Math.max(0, Math.min(Q.zoomIdx, pics.length - 1));
    const pc = pics[Q.zoomIdx];
    const z = zoomEl();
    const img = z.querySelector?.('.qz-img');
    if (img && pc) {
      const src = '/media/' + (pc.kind === 'video' ? (pc.thumb || pc.file) : pc.file);
      if (img.getAttribute('src') !== src) img.setAttribute('src', src);
    }
    const cnt = z.querySelector?.('.qz-count');
    if (cnt) {
      cnt.hidden = pics.length <= 1;
      cnt.textContent = pics.length > 1 ? `${Q.zoomIdx + 1} / ${pics.length}` : '';
    }
    const av = z.querySelector?.('.qz-avatar');
    const avUrl = avatarSrc(p.avatar);
    if (av) av.innerHTML = avUrl
      ? `<img class="avatar" src="${esc(avUrl)}" alt="" decoding="async">`
      : `<span class="avatar">${esc((p.author || '微')[0])}</span>`;
    const set = (sel, fn) => { const el = z.querySelector?.(sel); if (el) fn(el); };
    set('.qz-author', el => { el.textContent = p.author; });
    set('.qz-time', el => { el.innerHTML = `${fmtDate(p.created_at)}<span class="hm"> ${fmtHM(p.created_at)}</span>`; });
    set('.qz-text', el => {
      el.textContent = (p.text || '') +
        (p.text_complete === false ? '\n\n当前文本可能不完整，可打开原博查看全文。' : '');
    });
  }

  function zoomNav(d) {
    const n = (Q.slots[Q.zoomSlot]?.pics || []).length;
    if (n <= 1) return;
    Q.zoomIdx = (Q.zoomIdx + d + n) % n;
    renderZoom();
  }

  function closeZoom() {
    Q.zoom = false;
    zoomEl().hidden = true;
  }

  /* ---------- 大图（沿用墙卡语义：只翻本帖的图；锚定正在看的那张） ---------- */
  function openPostLightbox(p) {
    if (!p) return;
    const pics = p.pics || [];
    // 卡面在自动轮换（或细看态翻页）——开用户此刻看到的那张，不是固定首图
    const cur = pics[Q.zoom ? Q.zoomIdx : (Q.picIdx[Q.focus] || 0)] || pics[0];
    if (!cur) return;
    const i = S.images.findIndex(im => im.p.sid === p.sid && im.pc.file === cur.file);
    if (i >= 0) openLB(i, p.sid);
  }

  /* ---------- 键位（模态流：吃掉全部单键；返回 promise 便于测试衔接） ---------- */
  function key(e) {
    switch (e.key) {
      case 'ArrowUp': e.preventDefault(); return decide('kept');
      case 'f': case 'F': return decide('kept');
      case 'ArrowDown': e.preventDefault(); return decide('discarded');
      case 'd': case 'D': return decide('discarded');
      case 'ArrowLeft': Q.zoom ? zoomNav(-1) : moveFocus(-1); return undefined;
      case 'ArrowRight': Q.zoom ? zoomNav(1) : moveFocus(1); return undefined;
      case ' ': e.preventDefault(); openPostLightbox(Q.zoom ? Q.slots[Q.zoomSlot] : Q.slots[Q.focus]); return undefined;
      case 'Enter': e.preventDefault(); openZoom(); return undefined;
      case 'm': case 'M': { const p = Q.slots[Q.zoom ? Q.zoomSlot : Q.focus]; if (p) toggleFavorite(p.sid); return undefined; }
      case 'l': case 'L': { const p = Q.slots[Q.zoom ? Q.zoomSlot : Q.focus]; if (p) openLinkForSid(p.sid); return undefined; }
      case 'z': case 'Z': return undoLast();
      case 'Escape': if (Q.zoom) closeZoom(); else close(); return undefined;
    }
    return undefined;
  }

  return { open, close, key, active: () => Q.active, closeIfOpen: () => { if (Q.active) close(); } };
}
