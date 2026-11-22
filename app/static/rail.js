/* 桌面悬浮胶囊导航与快捷工具栏（≥900px）：
   - 左悬浮胶囊 #rail：纯 Overlay 无重排，120ms hover-intent 悬停自动展开，移出自动收起，无钉住模式；
     4 区条目：视图（全部/保留/收藏）· 所有博主（/kiosk）与待筛（N）· 编年/统计 · 设置。
   - 右上悬浮胶囊 #top-tools：搜索 / 筛选与排序（合并）/ 纯图 / 多选 / 更多⋯（放映·随机·批注）。
   - 右下悬浮胶囊 #corner-tools：回到顶部。
   同步走事件驱动（app.js 直呼 R.railSync）；末尾低频定时器只在桌面端且页面可见时做列栈自愈与兜底。 */
(function () {
  const R = window.pwRail;
  const rail = document.getElementById('rail');
  if (!R || !rail) return;
  const S = R.S;

  const topTools = document.getElementById('top-tools');
  const viewRows = [...document.querySelectorAll('#rail-views .vw')];
  const chronoBtn = document.getElementById('rail-chrono');
  const triageBtn = document.getElementById('rail-triage');
  const triageBdg = document.getElementById('rail-triage-bdg');
  const triageCnt = document.getElementById('rail-triage-cnt');

  const searchBtn = document.getElementById('tt-search');
  const filterBtn = document.getElementById('tt-filter');
  const filterBdg = document.getElementById('tt-filter-bdg');
  const pureimgBtn = document.getElementById('tt-pureimg');
  const selectBtn = document.getElementById('tt-select');
  const moreWrap = document.getElementById('tt-more');
  const moreBtn = document.getElementById('tt-more-btn');
  const moreMenu = document.getElementById('tt-more-menu');
  const topBtn = document.getElementById('tt-top');

  const cntEls = {};
  document.querySelectorAll('#rail-views .vw').forEach((b) => {
    cntEls[b.dataset.status] = b.querySelector('.cnt');
  });

  const hoverable = () => window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  /* ---------- 左悬浮胶囊：120ms hover-intent 展开，移出 120ms 收起，无钉住态 ---------- */
  let railOpenTimer = 0;
  let railCloseTimer = 0;
  const panelOpen = () => rail.classList.contains('open');

  function setPanelOpen(on) {
    clearTimeout(railOpenTimer);
    clearTimeout(railCloseTimer);
    rail.classList.toggle('open', Boolean(on));
  }

  rail.addEventListener('mouseenter', () => {
    if (!hoverable()) return;
    clearTimeout(railCloseTimer);
    railOpenTimer = setTimeout(() => setPanelOpen(true), 120);
  });
  rail.addEventListener('mouseleave', () => {
    if (!hoverable()) return;
    clearTimeout(railOpenTimer);
    railCloseTimer = setTimeout(() => setPanelOpen(false), 120);
  });
  rail.addEventListener('focusin', () => {
    clearTimeout(railCloseTimer);
    setPanelOpen(true);
  });
  rail.addEventListener('focusout', (e) => {
    if (!rail.contains(e.relatedTarget)) {
      clearTimeout(railOpenTimer);
      setPanelOpen(false);
    }
  });

  /* ---------- 右上「更多 ⋯」菜单：悬停/点按开合 ---------- */
  let moreOpenTimer = 0;
  let moreCloseTimer = 0;
  const isMoreOpen = () => !!(moreMenu && !moreMenu.hidden);

  function setMoreOpen(on) {
    clearTimeout(moreOpenTimer);
    clearTimeout(moreCloseTimer);
    if (!moreMenu || !moreBtn) return;
    moreMenu.hidden = !on;
    moreBtn.setAttribute('aria-expanded', String(Boolean(on)));
    moreBtn.classList.toggle('on', Boolean(on));
    topTools?.classList.toggle('has-open', Boolean(on));
  }
  function closeMore() { setMoreOpen(false); }

  if (moreWrap && moreBtn && moreMenu) {
    moreWrap.addEventListener('mouseenter', () => {
      if (!hoverable()) return;
      clearTimeout(moreCloseTimer);
      moreOpenTimer = setTimeout(() => setMoreOpen(true), 80);
    });
    moreWrap.addEventListener('mouseleave', () => {
      if (!hoverable()) return;
      clearTimeout(moreOpenTimer);
      moreCloseTimer = setTimeout(() => setMoreOpen(false), 160);
    });
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setMoreOpen(!isMoreOpen());
    });
  }

  const RAIL_OVERLAY_SEL = '.modal:not([hidden]), #lightbox:not([hidden]), #detail:not([hidden]),'
    + ' #slideshow:not([hidden]), #notes-view:not([hidden]), #quad:not([hidden]), #view-panel:not([hidden])';

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if (document.querySelector(RAIL_OVERLAY_SEL) || S.selectMode) return;
    if (isMoreOpen()) { closeMore(); return; }
    if (panelOpen()) setPanelOpen(false);
  });
  document.addEventListener('click', (e) => {
    if (isMoreOpen() && !e.target.closest('#tt-more')) closeMore();
    if (panelOpen() && !e.composedPath().some((n) => n && n.id === 'rail')) {
      setPanelOpen(false);
    }
  });

  /* ---------- 左轨点击路由 ---------- */
  rail.addEventListener('click', (e) => {
    if (e.target.closest('#rail-brand')) { R.scrollTop?.(); return; }
    const vw = e.target.closest('.vw');
    if (vw) { R.switchStatusView(vw.dataset.status); return; }
    if (e.target.closest('#rail-triage')) { setPanelOpen(false); R.startQuickTriage(); return; }
    if (e.target.closest('.js-chrono')) { R.setChrono(!R.chronoActive()); return; }
    if (e.target.closest('.js-settings')) { setPanelOpen(false); R.openSettings?.(); }
  });

  /* ---------- 右上与右下悬浮工具行为 ---------- */
  if (searchBtn) {
    searchBtn.addEventListener('click', () => {
      closeMore();
      R.openSpotlightSearch?.();
    });
  }
  if (filterBtn) {
    filterBtn.addEventListener('click', () => {
      closeMore();
      R.toggleViewPanel?.();
    });
  }
  if (pureimgBtn) {
    pureimgBtn.addEventListener('click', () => {
      closeMore();
      const prefs = window.pwGetPrefs ? window.pwGetPrefs() : {};
      window.pwSetPref('pureimg', prefs.pureimg ? '' : '1');
      sync();
    });
  }
  if (selectBtn) {
    selectBtn.addEventListener('click', () => {
      closeMore();
      if (S.readOnly) { R.toast('只读设备不能批量操作'); return; }
      if (!S.selectMode && !document.querySelector('#feed .card:not(.fading)')) {
        R.toast('当前没有可勾选的卡片');
        return;
      }
      R.setSelectMode(!S.selectMode);
      sync();
    });
  }
  document.getElementById('tt-play')?.addEventListener('click', () => {
    closeMore();
    R.slideshowOpen();
  });
  document.getElementById('tt-random')?.addEventListener('click', () => {
    closeMore();
    const ps = (S.posts || []).filter((p) => !p.fading);
    if (!ps.length) { R.toast('当前墙上没有卡片'); return; }
    R.openDetail(ps[Math.floor(Math.random() * ps.length)].sid);
  });
  document.getElementById('tt-notes')?.addEventListener('click', () => {
    closeMore();
    R.notesOpen();
  });
  if (topBtn) {
    topBtn.addEventListener('click', () => {
      closeMore();
      R.scrollTop?.();
    });
  }

  /* ---------- 状态同步：事件驱动为主 ---------- */
  function syncSession() {
    const counts = (S.st && S.st.counts) || {};
    const total = (counts.pending || 0) + (counts.kept || 0) + (counts.discarded || 0);
    const vwTitles = {
      all: `全部 · ${total || 0} 帖`,
      kept: `保留 · ${counts.kept || 0} 帖`,
      favorites: `收藏 · ${counts.favorites || 0} 帖`,
    };
    document.querySelectorAll('#rail-views .vw').forEach((b) => {
      const t = vwTitles[b.dataset.status];
      if (t && b.title !== t) b.title = t;
    });
    if (chronoBtn) {
      const chrTitle = R.chronoActive() ? '编年视图 · 已开启' : '编年视图';
      if (chronoBtn.title !== chrTitle) chronoBtn.title = chrTitle;
    }

    const pend = counts.pending || 0;
    const showTriage = !S.readOnly && pend > 0;
    if (triageBtn) triageBtn.hidden = !showTriage;
    const bdgText = pend > 99 ? '99+' : String(pend);
    if (triageBdg) {
      triageBdg.hidden = !showTriage;
      if (showTriage && triageBdg.textContent !== bdgText) triageBdg.textContent = bdgText;
    }
    if (triageCnt) {
      const cntText = showTriage ? String(pend) : '';
      if (triageCnt.textContent !== cntText) triageCnt.textContent = cntText;
    }
    if (triageBtn && showTriage) {
      const tt = `待筛 · ${pend} 帖`;
      if (triageBtn.title !== tt) triageBtn.title = tt;
    }

    const scopesEl = document.getElementById('spotlight-scopes');
    if (scopesEl) {
      const cur = S.searchScope || 'all';
      scopesEl.querySelectorAll('.s-scope').forEach((b) => {
        b.classList.toggle('on', b.dataset.scope === cur);
      });
    }
  }

  function sync() {
    const feedEl = document.getElementById('feed');
    if (feedEl && !R.chronoActive() && typeof R.feedColCount === 'function' && typeof R.relayoutFeed === 'function') {
      const actual = feedEl.querySelectorAll(':scope > .feed-col').length;
      if (actual && actual !== R.feedColCount()) R.relayoutFeed();
    }
    const c = (S.st && S.st.counts) || {};
    viewRows.forEach((b) => {
      const on = S.status === b.dataset.status;
      b.classList.toggle('on', on);
      // 读屏可感知当前视图：当前行标 aria-current="page"，其余行彻底移除；值不变不写 DOM（sync 每 1.5s 兜底）
      if (on) {
        if (b.getAttribute('aria-current') !== 'page') b.setAttribute('aria-current', 'page');
      } else if (b.hasAttribute('aria-current')) {
        b.removeAttribute('aria-current');
      }
    });
    const total = (c.pending || 0) + (c.kept || 0) + (c.discarded || 0);
    for (const [st, el] of Object.entries(cntEls)) {
      if (!el) continue;
      const v = String(st === 'all' ? total : (c[st] || 0));
      if (el.textContent !== v) el.textContent = v;
    }

    const q = S.searchText || '';
    if (searchBtn) {
      searchBtn.classList.toggle('on', !!q);
      const spModal = document.getElementById('modal-spotlight-search');
      const isOpen = !!spModal && !spModal.hidden && !spModal.classList.contains('closing');
      searchBtn.setAttribute('aria-expanded', String(isOpen));
      const t = q ? `搜索：${q}` : '搜索';
      if (searchBtn.title !== t) searchBtn.title = t;
    }

    if (filterBtn) {
      const vp = document.getElementById('view-panel');
      const vpOpen = !!vp && !vp.hidden;
      const activeFilters = typeof R.viewState === 'function' ? R.viewState().n : 0;
      const sortLabel = S.sort === 'keep_count' ? '点赞序' : S.sort === 'shuffle' ? '随机序' : '';
      filterBtn.setAttribute('aria-expanded', String(vpOpen));
      filterBtn.classList.toggle('on', vpOpen || activeFilters > 0 || !!S.sort);
      topTools?.classList.toggle('panel-open', vpOpen);
      if (filterBdg) {
        const badgeCount = activeFilters || (S.sort ? 1 : 0);
        filterBdg.hidden = !badgeCount;
        const bt = badgeCount > 9 ? '9+' : (activeFilters ? String(activeFilters) : '·');
        if (badgeCount && filterBdg.textContent !== bt) filterBdg.textContent = bt;
      }
      const ft = sortLabel ? `筛选与排序 · ${sortLabel}` : '筛选与排序';
      if (filterBtn.title !== ft) filterBtn.title = ft;
    }

    if (pureimgBtn) {
      const isPure = document.documentElement.dataset.pureimg === '1';
      pureimgBtn.classList.toggle('on', isPure);
      const t = isPure ? '纯图模式 · 开' : '纯图模式';
      if (pureimgBtn.title !== t) pureimgBtn.title = t;
    }
    if (selectBtn) {
      selectBtn.classList.toggle('on', !!S.selectMode);
      const t = S.selectMode ? '退出多选' : '多选模式';
      if (selectBtn.title !== t) selectBtn.title = t;
    }

    if (chronoBtn) chronoBtn.classList.toggle('on', R.chronoActive());
    syncSession();
  }

  R.railSync = sync;
  const desktopMq = window.matchMedia('(min-width: 900px)');
  let pollTimer = 0;
  const canPoll = () => !document.hidden && desktopMq.matches;
  function updatePolling(runNow = false) {
    if (canPoll()) {
      if (!pollTimer) pollTimer = setInterval(() => { if (canPoll()) sync(); }, 1500);
      if (runNow) sync();
    } else if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = 0;
    }
  }
  document.addEventListener('visibilitychange', () => updatePolling(!document.hidden));
  desktopMq.addEventListener?.('change', () => updatePolling(desktopMq.matches));
  window.addEventListener('resize', () => {
    if (canPoll() && !pollTimer) updatePolling(true);
    else if (!canPoll() && pollTimer) updatePolling(false);
  });
  updatePolling(true);
  try {
    new MutationObserver(() => sync()).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-pureimg'],
    });
  } catch (e) {}
})();
