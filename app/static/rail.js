/* 左右轨：桌面（≥900px）主导航轨 + 快捷动作轨。
   壳在 index.html，样式在 style.css「桌面左右轨」段；这里只做交互与数据填充。
   能力面 = window.pwRail（app.js 末尾暴露的最小钩子），业务逻辑零复制；
   数据面 = 既有 /api/status（状态计数 + 分组）与 /api/authors（分组作者 / 微博博主）。
   同步走事件驱动：app.js 在状态变更点（loadStatus/renderViewPanel/applySearch/布局切换）
   直呼 R.railSync()；末尾的低频定时器只做列栈自愈与漏事件兜底，不再是主通道。
   移动端 (<900px) 两轨 display:none，行为照跑但触不到，开销可忽略。 */
(function () {
  const R = window.pwRail;
  const rail = document.getElementById('rail');
  if (!R || !rail) return;
  const S = R.S;
  const $ = (s, el) => (el || document).querySelector(s);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const avatarSrc = (u) => (u && u.startsWith('avatars/') ? '/media/' + u : u);
  const CHEV = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>';

  const viewRows = [...document.querySelectorAll('.vw')];   // 轨 + 面板两套
  const chronoRows = [...document.querySelectorAll('.js-chrono')];
  // 右轨排序（自左轨移入）：#ir-sort 抽屉里三档选项（与手机视图面板排序段同全集），tip 文案随 S.sort 刷
  const sortIsl = document.getElementById('ir-sort');
  const sortTip = document.getElementById('ir-sort-tip');
  const sortOpts = sortIsl ? [...sortIsl.querySelectorAll('[data-sort-opt]')] : [];
  // 右轨搜索：图标激活态 = 搜索词在生效（此前生效了也看不出）；tip 显当前词
  const searchBtn = document.getElementById('ir-search');
  const searchTip = document.getElementById('ir-search-tip');
  // 右轨筛选钮：开合态跟视图面板走（aria-expanded 给读屏，样式给 .ibtn.on）
  const filterBtn = document.getElementById('ir-filter');
  const treeEl = document.getElementById('rail-tree');
  const authorsEl = document.getElementById('rail-authors');
  const authorsEntry = document.getElementById('rail-authors-entry');
  const authorsBdg = document.getElementById('rail-authors-bdg');
  // 右轨待筛入口（index.html #ir-triage）：有待筛帖才出现，点击直达快筛
  const irTriage = document.getElementById('ir-triage');
  const irTriageBdg = document.getElementById('ir-triage-bdg');
  // 面板视图行的计数（轨上纯图标，面板行带全量计数）
  const cntEls = {};
  document.querySelectorAll('#panel-views .vw').forEach((b) => {
    cntEls[b.dataset.status] = b.querySelector('.cnt');
  });

  /* ---------- 面板展开 / 收起：#rail-toggle 按钮 + Esc / 点外收起 ----------
     持久化只认 #rail-toggle（「钉住」语义）：它写入 pw_rail_open，刷新后自动展开；
     Esc / 点外 / 穿透导航（选博主）的收起只是会话级，不改偏好——
     钉住的用户点掉面板后，下次进来仍按偏好展开，想退出钉住去点收起钮。 */
  const panel = document.getElementById('rail-panel');
  const toggleBtn = document.getElementById('rail-toggle');
  const toggleTip = document.getElementById('rail-toggle-tip');
  const panelOpen = () => panel.classList.contains('show');
  function syncToggle() {
    const on = panelOpen();
    toggleBtn.classList.toggle('on', on);
    toggleBtn.setAttribute('aria-expanded', String(on));
    toggleBtn.setAttribute('aria-label', on ? '收起导航面板' : '展开导航面板');
    if (toggleTip) toggleTip.textContent = on ? '收起导航' : '展开导航';
  }
  function openPanel() {
    if (panelOpen()) return;
    panel.classList.add('show');
    rail.classList.add('tips-off');
    syncToggle();
  }
  function closePanel() {
    panel.classList.remove('show');
    rail.classList.remove('tips-off');
    syncToggle();
  }
  toggleBtn.addEventListener('click', () => {
    const on = !panelOpen();
    try { localStorage.setItem('pw_rail_open', on ? '1' : ''); } catch (e) { /* 私有模式存不了就本次有效 */ }
    if (on) openPanel(); else closePanel();
  });
  // 上次的展开态跨刷新保留（旧版「钉住」偏好同样视为展开）
  try {
    const pref = localStorage.getItem('pw_rail_open') != null
      ? localStorage.getItem('pw_rail_open') : localStorage.getItem('pw_rail_pin');
    if (pref === '1') {
      // 启动即展开（localStorage 恢复）时面板还在首个样式解析前，直接挂 .show
      // 会把过渡冻在起始帧（实测 Chromium 冻住 transform/opacity）——先关过渡瞬时到位
      panel.style.transition = 'none';
      panel.classList.add('show');
      requestAnimationFrame(() => requestAnimationFrame(() => { panel.style.transition = ''; }));
      rail.classList.add('tips-off');
    }
  } catch (e) { /* 同上 */ }
  syncToggle();

  /* Esc / 点外收起：排在既有浮层链之后——弹层/大图/详情/放映/批注/四格/视图面板
     开着时让位（app.js 的 Escape 链同序处理，一层层退，不双关） */
  const RAIL_OVERLAY_SEL = '.modal:not([hidden]), #lightbox:not([hidden]), #detail:not([hidden]),'
    + ' #slideshow:not([hidden]), #notes-view:not([hidden]), #quad:not([hidden]), #view-panel:not([hidden])';
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (document.querySelector(RAIL_OVERLAY_SEL) || S.selectMode) return;
    const openIsl = document.querySelector('#irail .isl.open');   // 右轨点按展开的抽屉先退
    if (openIsl) { closeDrawers(); return; }
    if (panelOpen()) closePanel();
  });
  document.addEventListener('click', (e) => {
    if (!panelOpen()) return;
    // 用 dispatch 时固化的 composedPath 判断「点在哪」：面板内行点击会在冒泡途中
    // 同步重渲染树（innerHTML 换血），到 document 层时 e.target 已被摘出 DOM、
    // closest() 的祖先链断裂——用它会误判成「点在面板外」把面板关掉
    const path = e.composedPath();
    if (path.some((n) => n && (n.id === 'rail-panel' || n.id === 'rail'))) return;
    closePanel();
  });

  /* ---------- 分组树：X 分组可展开取组内博主，微博博主平铺（本就无分组） ---------- */
  /* 缓存按 key 失效（'lid|status'）：口径变化（换视图 / 分组计数变）只把旧条目标记 stale，
     可见键立即后台重取，其余等下次被看到时重取——重取期间照常渲染旧值，
     不再整树清空导致面板里展开的列表闪「加载中…」。首次失败落位 null（不每秒重试），
     null 条目同样进 stale 名单，换视图 / 计数变化时自动重试。 */
  const authCache = new Map();     // 'lid|status' → authors 数组；null = 首次取失败
  const staleKeys = new Set();     // 可能过期的缓存 key，见到即重取
  const authLoading = new Set();
  const expanded = new Set();
  let lastTreeKey = '';

  const xGroups = () => ((S.st && S.st.groups) || []).filter((g) =>
    g.active && g.list_id !== 'demo' && /^(xg:|x:)/.test(g.list_id));

  async function ensureAuthors(key, params) {
    const ck = key + '|' + S.status;
    if ((authCache.has(ck) && !staleKeys.has(ck)) || authLoading.has(ck)) return;
    authLoading.add(ck);
    const hadValue = authCache.has(ck) && authCache.get(ck) !== null;
    try {
      const q = new URLSearchParams({ group: 'all', source: 'all', status: S.status, sort: 'count_desc', ...params });
      const r = await fetch('/api/authors?' + q.toString());
      if (!r.ok) throw new Error('authors ' + r.status);
      const data = await r.json();
      authCache.set(ck, data.authors || []);
      staleKeys.delete(ck);
    } catch (e) {
      if (!hadValue) authCache.set(ck, null);   // 首次失败落位；已有旧值则留着继续显示，等下次重试
    } finally {
      authLoading.delete(ck);
    }
    renderTree();
  }

  function authorRowHtml(a) {
    const av = a.avatar
      ? `<img class="avatar" src="${esc(avatarSrc(a.avatar))}" alt="" loading="lazy">`
      : `<span class="avatar">${esc((a.author || '微')[0])}</span>`;
    const isNew = S.status === 'pending' && a.n > 0;
    return `<button class="rrow sub${S.author === a.uid ? ' on' : ''}" data-author-pick="${esc(a.uid)}" type="button"
      title="${esc(a.author)} · ${a.n} 帖">${av}<span class="lb">${esc(a.author)}</span>
      <span class="cnt${isNew ? ' new' : ''}">${a.n}</span></button>`;
  }

  function renderTree() {
    if (!S.st) return;
    const cur = S.status;
    // 分组区只列真容器：本地分组（xg:）与 X 列表（x:）；xu: 用户目标是博主，不是分组。
    // 行拆两钮：chev 钮管展开/收起（带 aria-expanded），行体钮管选中过滤——
    // 此前两者耦合在同一个 button 里，展开只有 add 没有删除，树只能张开收不回。
    const groupsHtml = xGroups().map((g) => {
      const open = expanded.has(g.list_id);
      let kids = '';
      if (open) {
        const list = authCache.get(g.list_id + '|' + cur);
        if (list === undefined) kids = '<div class="rload">组员加载中…</div>';
        else if (list === null) kids = '<div class="rload">组员加载失败，换视图重试</div>';
        else kids = list.map((a) => authorRowHtml(a)).join('') || '<div class="rload">组里还没有帖子</div>';
      }
      return `<div class="rrow grp${open ? ' open' : ''}" data-group-expand="${esc(g.list_id)}">
        <button class="gchev" data-group-toggle="${esc(g.list_id)}" type="button" aria-expanded="${open}"
          aria-label="${open ? '收起' : '展开'}${esc(g.name)}的博主列表"><span class="chev">${CHEV}</span></button>
        <button class="gmain" data-group-pick="${esc(g.list_id)}" type="button"
          title="${esc(g.name)} · ${g.total || 0} 帖"><span class="lb">${esc(g.name)}</span>
          <span class="cnt">${g.total || 0}</span></button>
      </div>${kids}`;
    }).join('');
    treeEl.innerHTML = groupsHtml || '<div class="rload">还没有分组（设置里可新建）</div>';
    renderAuthors();
    syncTreeHighlight();
  }

  function renderAuthors() {
    const cur = S.status;
    const list = authCache.get('all|' + cur);
    let html;
    if (list === undefined) html = '<div class="rload">博主加载中…</div>';
    else if (list === null) html = '<div class="rload">博主加载失败，换视图重试</div>';
    else {
      html = list.map((a) => authorRowHtml(a)).join('') || '<div class="rload">还没有博主</div>';
    }
    authorsEl.innerHTML = html;
    syncTreeHighlight();
  }

  function syncTreeHighlight() {
    treeEl.querySelectorAll('[data-group-expand]').forEach((b) =>
      b.classList.toggle('on', S.group === b.dataset.groupExpand));
    treeEl.querySelectorAll('[data-author-pick]').forEach((b) =>
      b.classList.toggle('on', S.author === b.dataset.authorPick));
    authorsEl.querySelectorAll('[data-author-pick]').forEach((b) =>
      b.classList.toggle('on', S.author === b.dataset.authorPick));
  }

  function onTreeDepsChanged() {
    // 口径变了（状态 / 分组计数）：全部标记可能过期。可见键立即后台重取（旧值撑场面不闪加载），
    // 其余留在缓存、标记 stale，下次被看到时重取——旧状态的缓存不再被整树丢弃
    for (const ck of authCache.keys()) staleKeys.add(ck);
    renderTree();
    ensureAuthors('all', {});
    expanded.forEach((lid) => ensureAuthors(lid, { group: lid }));
  }

  /* ---------- 行点击：视图 / 排序 / 编年 / 设置 / 分组 / 博主 ---------- */
  function toggleGroupExpand(lid) {
    if (expanded.has(lid)) expanded.delete(lid);
    else {
      expanded.add(lid);
      ensureAuthors(lid, { group: lid });
    }
    renderTree();
  }
  function pickGroup(lid) {
    const selecting = S.group !== lid;
    S.group = selecting ? lid : 'all';   // 选中组再点 = 回全部；收起交给 chev，选中态不连带收组
    if (selecting) {
      if (S.author !== 'all') S.author = 'all';
      if (!expanded.has(lid)) {          // 选中即展开组员：看见所选范围是分组的本能预期
        expanded.add(lid);
        ensureAuthors(lid, { group: lid });
      }
    }
    R.renderViewPanel();
    R.load(true);
    renderTree();
  }
  /* 博主行 = 进博主页面（跨状态看该博主全部内容，头像/统计条与四款样式都在那），
     不再是状态内过滤——过滤走视图面板（V）的博主行。缓存的行数据作即时身份
     hint，博主条先显名字头像、档案再后台补。 */
  function cachedAuthor(uid) {
    for (const list of authCache.values())
      if (Array.isArray(list)) { const a = list.find((x) => x.uid === uid); if (a) return a; }
    return null;
  }
  function pickAuthor(uid) {
    const a = cachedAuthor(uid);
    closePanel();   // 穿透进新语境：面板收起，博主条与样式切换在墙顶接手
    R.authorViewOpen(uid, a ? { author: a.author, avatar: a.avatar } : null);
  }
  function pickSort(opt) {
    if (!['kept', 'favorites', 'all'].includes(S.status)) { R.toast('该视图按时间序，不参与排序'); return; }
    if (S.sort === opt) { closeDrawers(); return; }
    S.sort = opt;
    R.renderViewPanel();
    R.load(true);
    closeDrawers();   // 点按开合的抽屉选完即收（悬停用户靠移出鼠标收）
  }

  // 导航路由（轨 + 面板共用一份）：功能行/入口行/树行在这里分派
  function navClick(e) {
    // 面板首行收起钮：与轨上 #rail-toggle 同 y 的镜像行，点了收起
    if (e.target.closest('#rail-panel-toggle')) { closePanel(); return; }
    const gt = e.target.closest('[data-group-toggle]');
    if (gt) { toggleGroupExpand(gt.dataset.groupToggle); return; }   // chev 钮：只展开/收起，不动过滤
    const gp = e.target.closest('[data-group-pick]');
    if (gp) { pickGroup(gp.dataset.groupPick); return; }             // 行体钮：只选中过滤，不动展开态
    const gd = e.target.closest('[data-group-expand]');
    if (gd) { pickGroup(gd.dataset.groupExpand); return; }           // 两钮之外的行内空隙：兜底按选中处理
    const ap = e.target.closest('[data-author-pick]');
    if (ap) { pickAuthor(ap.dataset.authorPick); return; }
    const vw = e.target.closest('.vw');
    if (vw) { R.switchStatusView(vw.dataset.status); return; }
    const so = e.target.closest('[data-sort-opt]');
    if (so) { pickSort(so.dataset.sortOpt); return; }
    // 分组/博主入口：同样是显式点击路径——展开面板并滚到对应区
    if (e.target.closest('#rail-groups')) { openPanel(); $('#rail-tree').scrollIntoView({ block: 'nearest' }); return; }
    if (e.target.closest('#rail-authors-entry')) { openPanel(); $('#rail-authors').scrollIntoView({ block: 'nearest' }); return; }
    if (e.target.closest('.js-chrono')) { R.setChrono(!R.chronoActive()); return; }
    if (e.target.closest('.js-settings')) { if (R.openSettings) R.openSettings(); }   // 桌面无顶栏，设置走 pwRail 钩子直开（不再遥控隐藏的 #btn-settings）
  }
  rail.addEventListener('click', navClick);
  panel.addEventListener('click', navClick);

  /* ---------- 状态同步：计数 / 选中态 / 待筛入口 / 搜索镜像，轻量 DOM 写 ---------- */
  function syncSession() {
    // 右轨待筛按钮：有待筛帖才出现（只读设备常隐），筛完即隐；角标 = 待筛数（名条只写「待筛」）
    const pend = ((S.st && S.st.counts) || {}).pending || 0;
    const show = !S.readOnly && pend > 0;
    irTriage.hidden = !show;
    if (!show) return;
    irTriageBdg.hidden = pend === 0;
    const t = pend > 99 ? '99+' : String(pend);
    if (irTriageBdg.textContent !== t) irTriageBdg.textContent = t;
  }

  function sync() {
    // 列栈自愈：内嵌/异构环境里 viewport 可能晚于首屏落位且不发 resize，
    // 列栈停在旧档位。直接对照 app.js 的 feedColCount（经 pwRail 暴露，公式单源），
    // 不符就补发 resize。（编年态没有 .feed-col，让位。）
    const feedEl = document.getElementById('feed');
    if (feedEl && !R.chronoActive() && typeof R.feedColCount === 'function') {
      const actual = feedEl.querySelectorAll(':scope > .feed-col').length;
      if (actual && actual !== R.feedColCount()) window.dispatchEvent(new Event('resize'));
    }
    const st = S.st;
    const c = (st && st.counts) || {};
    viewRows.forEach((b) => b.classList.toggle('on', S.status === b.dataset.status));
    const total = (c.pending || 0) + (c.kept || 0) + (c.discarded || 0);
    for (const [st, el] of Object.entries(cntEls)) {
      if (!el) continue;
      const v = String(st === 'all' ? total : (c[st] || 0));
      if (el.textContent !== v) el.textContent = v;   // 值没变不写 DOM：兜底轮询里零扰动
    }
    const showSort = ['kept', 'favorites', 'all'].includes(S.status);
    const sortText = S.sort === 'keep_count' ? '排序：点赞'
      : S.sort === 'shuffle' ? '排序：随机' : '排序：最新';
    if (sortTip && sortTip.textContent !== sortText) sortTip.textContent = sortText;
    if (sortIsl) sortIsl.classList.toggle('off', !showSort);   // off 态：图标淡显 + 抽屉行点击给 toast
    sortOpts.forEach((b) => b.classList.toggle('on', showSort && S.sort === b.dataset.sortOpt));
    // 搜索激活态：词在生效才点亮（tip 显当前词，空词回落「搜索」）
    const q = S.searchText || '';
    if (searchBtn) searchBtn.classList.toggle('on', !!q);
    if (searchTip) { const t = q ? `搜索：${q}` : '搜索'; if (searchTip.textContent !== t) searchTip.textContent = t; }
    // 筛选钮开合态跟视图面板走（面板开合在 app.js，轨上只镜像 aria）
    if (filterBtn) {
      const vp = document.getElementById('view-panel');
      filterBtn.setAttribute('aria-expanded', String(!!vp && !vp.hidden));
    }
    chronoRows.forEach((b) => b.classList.toggle('on', R.chronoActive()));
    // 入口行选中态与徽章：当前过滤到某组/某博主时对应入口点亮；
    // 新增博主数只在新增视图里点亮（进该视图才取博主名条，徽章是筛补充提醒不是全局哨）
    document.getElementById('rail-groups').classList.toggle('on', S.group !== 'all');
    const pendingList = S.status === 'pending' ? authCache.get('all|pending') : null;
    const newCount = pendingList ? pendingList.filter((a) => a.n > 0).length : 0;
    authorsEntry.classList.toggle('on', S.author !== 'all');
    authorsBdg.hidden = !newCount;
    const newCountText = newCount > 99 ? '99+' : String(newCount);
    if (newCount && authorsBdg.textContent !== newCountText) authorsBdg.textContent = newCountText;
    const key = JSON.stringify([S.status, st ? st.groups.map((g) => g.total).join(',') : '']);
    if (key !== lastTreeKey) {
      lastTreeKey = key;
      onTreeDepsChanged();
    } else {
      syncTreeHighlight();
    }
    syncSession();
    const si = document.getElementById('ir-search-input');
    if (si && document.activeElement !== si && si.value !== (S.searchText || '')) {
      si.value = S.searchText || '';
    }
  }

  /* ---------- 右轨：排序 / 筛选 / 搜索 / 放映 / 随机一张 / 批注 / 待筛 / 回顶部 ---------- */
  // 右轨不在 navClick 监听范围（只绑 #rail/#rail-panel），排序抽屉自绑
  if (sortIsl) sortIsl.addEventListener('click', (e) => {
    const so = e.target.closest('[data-sort-opt]');
    if (!so) return;
    if (e.detail > 0) so.blur();   // 真实点按失焦：焦点留在选项上会被 :focus-within 吊住抽屉不收（键盘激活 detail=0 不动焦点）
    pickSort(so.dataset.sortOpt);
  });
  if (irTriage) irTriage.addEventListener('click', () => R.startQuickTriage());
  const playGo = document.getElementById('ir-play-go');
  if (playGo) playGo.addEventListener('click', () => { closeDrawers(); R.slideshowOpen(); });
  const randomBtn = document.getElementById('ir-random');
  if (randomBtn) {
    randomBtn.addEventListener('click', () => {
      const ps = (S.posts || []).filter((p) => !p.fading);
      if (!ps.length) { R.toast('当前墙上没有卡片'); return; }
      R.openDetail(ps[Math.floor(Math.random() * ps.length)].sid);
    });
  }
  const notesBtn = document.getElementById('ir-notes');
  if (notesBtn) notesBtn.addEventListener('click', () => R.notesOpen());
  const topBtn = document.getElementById('ir-top');
  if (topBtn) topBtn.addEventListener('click', () => R.scrollTop());
  const sInput = document.getElementById('ir-search-input');
  if (sInput) {
    sInput.addEventListener('keydown', (e) => {
      e.stopPropagation();   // 输入框里的按键不进全局快捷键（与 f-search 同规）
      if (e.key === 'Enter') { R.applySearch(sInput.value); closeDrawers(); }
      else if (e.key === 'Escape') {
        // Esc 三段式：草稿未生效→还原成已生效词；已生效词→清除；都没有→失焦（再按 Esc 才收抽屉）
        if (sInput.value !== (S.searchText || '')) sInput.value = S.searchText || '';
        else if (S.searchText) { R.applySearch(''); sInput.value = ''; }
        else sInput.blur();
      }
    });
  }

  /* 右轨筛选钮（const 声明在文件头 sync 段）：桌面打开视图面板的唯一可见入口
     （面板按钮只在移动端顶栏）。不拦冒泡：视图面板的「点外即收」在 app.js 里点名放行 #ir-filter */
  if (filterBtn) filterBtn.addEventListener('click', () => { if (R.toggleViewPanel) R.toggleViewPanel(); });

  /* ---------- 右轨抽屉点按开合：≥900px 的触屏设备（iPad 横屏等）没有 hover，
     悬停展开的抽屉对它不可达；点按切换对所有指针类型都成立，桌面点击同样可用 ---------- */
  const drawerIsls = [...document.querySelectorAll('#irail .isl')]
    .filter((isl) => isl.querySelector('.drawer'));
  function closeDrawers() {
    document.querySelectorAll('#irail .isl.open').forEach((isl) => {
      isl.classList.remove('open');
      const b = isl.querySelector('.ibtn[aria-controls]');
      if (b) b.setAttribute('aria-expanded', 'false');
      const inp = isl.querySelector('.d-search input');
      if (inp && document.activeElement === inp) inp.blur();   // visibility:hidden 不自动失焦，别留隐形焦点吞键盘
    });
  }
  for (const isl of drawerIsls) {
    const btn = isl.querySelector('.ibtn');
    btn.addEventListener('click', () => {
      const wasOpen = isl.classList.contains('open');
      closeDrawers();
      if (wasOpen) return;
      isl.classList.add('open');
      if (btn.hasAttribute('aria-controls')) btn.setAttribute('aria-expanded', 'true');
      const inp = isl.querySelector('.d-search input');
      if (inp) inp.focus();   // 点按开搜索即落焦点：触屏/鼠标少一跳（悬停不打扰焦点）
    });
  }
  document.addEventListener('click', (e) => {
    if (e.target.closest('#irail')) return;   // 抽屉内的点选（排序项等）由各自逻辑收
    closeDrawers();
  });

  /* ---------- 状态同步：事件驱动为主（app.js 状态变更点直呼 R.railSync），
     低频定时器只兜列栈自愈与漏事件 ---------- */
  R.railSync = sync;
  sync();
  setInterval(sync, 1500);
})();
