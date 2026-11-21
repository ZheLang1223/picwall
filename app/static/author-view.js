/* 博主视图：卡片/详情头像进入的「该博主全部内容」筛选语境。
   视图模型 = S.author 钉住 + 状态与伴随筛选归位（enterFilters），墙，分页、
   标记、大图、多选全部复用既有链路；头部条 #author-view 的显隐单一事实源是
   S.author，load(true) 与进出动作统一汇到 sync()。
   头部统计来自 /api/authors/{key}/profile（本地聚合），微博签名沿用
   /api/authors/{uid}/bio；外链 X 拼 handle、微博拼 uid。
   右侧样式组四选一：卡片（默认）/ 胶片 / 展厅 / 蓝图——沉浸皮肤全是单列细看
   （feed 挂 author-strip 供 app.js 列栈退场），偏好键 author-skin 多值记忆。
   编年态下同一批卡片走同一入口，得到该博主的编年流（日卷/尺子照常）。 */

/* 进入博主视图的状态迁移：author 钉住，其余全部归位——「所有内容」的语义。
   sort 归空 = 时间序；source 归 all（post_filter 的 author 分支自带来源限定）。
   uid 传 'all' 即退出且无快照时的默认落点。 */
export function enterFilters(uid) {
  return { author: uid, status: 'all', group: 'all', source: 'all',
           range: 'all', hasCmts: 'all', tags: 'all', searchText: '', sort: '' };
}

/* 时间跨度（年-月）：同月只出现一次；缺据不给 */
export function profileSpan(first, last) {
  if (!first || !last) return '';
  const a = first.slice(0, 7), b = last.slice(0, 7);
  return a === b ? a : `${a} – ${b}`;
}

/* 头部统计文案：N 帖 · 保留 x · 收藏 y · 注 z · 跨度；为 0 的维度不出现 */
export function statsLine(p) {
  const bits = [`${p.n} 帖`];
  if (p.kept) bits.push(`保留 ${p.kept}`);
  if (p.favorites) bits.push(`收藏 ${p.favorites}`);
  if (p.comments) bits.push(`注 ${p.comments}`);
  const span = profileSpan(p.first_at, p.last_at);
  if (span) bits.push(span);
  return bits.join(' · ');
}

/* 作者原主页：X 只认 handle（x.com 按 handle 解析；老数据 uid 是数字 rest_id，
   拼进链接会被当 handle 打不开——宁缺毋滥不给链接）；微博用 uid。 */
export function profileUrl(p) {
  if (!p) return '';
  if (p.source === 'x') {
    return p.handle ? `https://x.com/${encodeURIComponent(p.handle)}` : '';
  }
  return p.uid ? `https://weibo.com/u/${encodeURIComponent(p.uid)}` : '';
}

/* 沉浸皮肤档位（'' = 卡片墙默认）：胶片 / 展厅 / 蓝图，全部是单列细看布局 */
export const SKINS = ['film', 'gallery', 'blueprint'];

/* 偏好 → 皮肤：author-skin 多值优先；未知档位回卡片；老档案只有
   author-film 布尔（2026-11-11 前的唯一皮肤），按 '1' 折算成 film。 */
export function skinFromPrefs(prefs) {
  const v = prefs?.['author-skin'];
  if (SKINS.includes(v)) return v;
  return prefs?.['author-film'] === '1' ? 'film' : '';
}

export function createAuthorView(deps) {
  const { S, $, api, esc, avatarSrc, icons, load, closeOverlays,
          renderViewPanel, chronoActive, reflow } = deps;
  const el = $('#author-view');
  let snapshot = null;        // 进入前的筛选域（退出时还原；视图内换人不覆盖）
  let hints = new Map();      // key → {author,avatar}（卡片带过来的即时身份）
  let cache = new Map();      // key → profile 全量档案（即时渲染，后台总再刷一遍）
  let seq = 0;
  let currentKey = '';

  /* 沉浸皮肤（pref author-skin）：博主视图专属换皮，卡片 DOM 不动、行为全保留。
     三个皮肤都是单列条（app.js 列栈按 author-strip 判定退场）；编年态的优先级
     用 CSS :not(.chrono) 解决，类本身只在博主语境下挂。 */
  function skinNow() {
    try { return skinFromPrefs(window.pwGetPrefs?.()); } catch (e) { return ''; }
  }
  function applySkin() {
    const feed = $('#feed');
    if (!feed) return;
    const on = S.author !== 'all' ? skinNow() : '';
    for (const k of SKINS) feed.classList.toggle(`author-${k}`, k === on);
    feed.classList.toggle('author-strip', SKINS.includes(on));
  }

  /* 吸顶偏移：自身贴顶栏下沿；桌面隐藏顶栏时贴顶 (8px)；编年态把日卷粘滞头与尺子的 --tl-top 顶到自己下方 */
  function syncSticky() {
    const tb = document.querySelector('.topbar');
    const hasTopbar = tb && getComputedStyle(tb).display !== 'none';
    const base = hasTopbar ? tb.offsetHeight + 6 : 8;
    el.style.top = `${base}px`;
    if (chronoActive?.()) {
      const h = el.hidden ? 0 : el.offsetHeight;
      document.documentElement.style.setProperty('--tl-top', `${base + h}px`);
    }
  }

  function paint() {
    applySkin();
    if (S.author === 'all') {
      $('#feed')?.removeAttribute('data-skin-author');
      el.hidden = true;
      syncSticky();
      return;
    }
    const prof = cache.get(S.author);
    const hint = hints.get(S.author);
    const name = prof?.author || hint?.author
      || S.posts.find((q) => q.uid === S.author)?.author || S.author;
    const avatar = avatarSrc(prof?.avatar || hint?.avatar || '');
    const ext = profileUrl(prof);
    const skin = skinNow();
    const stBtn = (v, label, title) =>
      `<button type="button" class="av-stbtn${skin === v ? ' on' : ''}" data-st="${v}"
                aria-pressed="${String(skin === v)}" title="${title}">${label}</button>`;
    $('#feed')?.setAttribute('data-skin-author', name);   // 皮肤墙头（片头/展头/图签）用
    el.innerHTML = `
      ${avatar ? `<img class="avatar" src="${esc(avatar)}" alt="" decoding="async">`
               : `<span class="avatar">${esc(name[0] || '微')}</span>`}
      <div class="av-id"><b>${esc(name)}</b>
        <span class="av-sub">${prof ? esc(statsLine(prof)) : ''}</span></div>
      <div class="av-style" role="group" aria-label="博主视图样式">
        ${stBtn('', '卡片', '卡片墙样式')}
        ${stBtn('film', '胶片', '胶片印样样式')}
        ${stBtn('gallery', '展厅', '美术馆展厅样式')}
        ${stBtn('blueprint', '蓝图', '蓝图图纸样式')}
      </div>
      ${ext ? `<a class="av-ext" href="${esc(ext)}" target="_blank" rel="noopener noreferrer"
                  title="打开原主页" aria-label="打开原主页">${icons.external}</a>` : ''}
      <button class="av-close" title="退出博主视图" aria-label="退出博主视图">${icons.x}</button>`;
    el.hidden = false;
    syncSticky();
  }

  async function fetchProfile() {
    const key = S.author;
    const my = ++seq;
    try {
      const r = await api(`/api/authors/${encodeURIComponent(key)}/profile`);
      if (my !== seq || S.author !== key) return;
      cache.set(key, r);
      paint();
    } catch (e) { /* 拿不到统计就只显示名字（404 = 帖已全清），不打断浏览 */ }
    if (!key.includes(':')) {   // 微博作者补签名：进 title，与墙头胶囊同款待遇
      try {
        const r = await api(`/api/authors/${encodeURIComponent(key)}/bio`);
        if (my !== seq || S.author !== key || !r?.bio) return;
        el.querySelector('.av-id')
          ?.setAttribute('title', `${cache.get(key)?.author || key} · ${r.bio}`);
      } catch (e) { /* 同上，静默降级 */ }
    }
  }

  /* 显隐跟 S.author；换人才重取档案，同人只重画（load(true) 每次筛选变更都会进来） */
  function sync() {
    if (S.author === 'all') snapshot = null;
    if (S.author === currentKey) { paint(); return; }
    currentKey = S.author;
    paint();
    if (S.author !== 'all') fetchProfile();
  }

  function open(uid, hint) {
    if (!uid || uid === 'all') return;
    closeOverlays?.();
    if (S.author === 'all')
      snapshot = { status: S.status, group: S.group, source: S.source,
                   range: S.range, hasCmts: S.hasCmts, tags: S.tags,
                   searchText: S.searchText, sort: S.sort,
                   searchScope: S.searchScope };
    if (hint) hints.set(uid, { author: hint.author, avatar: hint.avatar });
    Object.assign(S, enterFilters(uid), { searchScope: 'all', appliedSearchScope: 'all', appliedSearchText: '' });
    $('#f-search').value = '';
    renderViewPanel?.();
    window.scrollTo(0, 0);
    sync();
    load(true);
  }

  function close() {
    if (S.author === 'all') return;
    const prev = snapshot;
    snapshot = null;
    if (prev) {   // 回到进入时的那面墙（保留视图进来就回保留视图）
      Object.assign(S, prev, { author: 'all' });
      S.appliedSearchText = S.searchText || '';
      S.appliedSearchScope = S.searchScope || 'all';
      $('#f-search').value = S.searchText || '';
    } else {      // 直开 URL（?author=…）没有快照：回默认全部视图
      Object.assign(S, enterFilters('all'), { searchScope: 'all', appliedSearchScope: 'all', appliedSearchText: '' });
      $('#f-search').value = '';
    }
    renderViewPanel?.();
    load(true);
  }

  function init() {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.av-close')) return close();
      const st = e.target.closest('.av-stbtn');
      if (!st) return;
      const v = st.dataset.st;                     // '' = 卡片（偏好键整个清掉）
      if (skinNow() === v) return;
      try { window.pwSetPref?.('author-skin', v); } catch (err) {}
      applySkin();
      reflow?.();          // 列栈 ↔ 单列平铺的重排收口在 app.js
      paint();             // 重画按压态
    });
    let stickyRaf = 0;
    window.addEventListener('resize', () => {
      if (stickyRaf) return;
      stickyRaf = requestAnimationFrame(() => { stickyRaf = 0; syncSticky(); });
    });   // 顶栏高度随宽度档变化
    sync();
  }

  return { init, sync, open, close };
}
