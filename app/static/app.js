/* Picwall 前端：单页相册。无框架无构建， vanilla JS。 */
import { api } from './requests.js';
import { toast } from './notifications.js';
import { matchesView, keepBadge, avatarSrc, mediaSrc, esc, safeUrl, markTerm, shuffleSeed } from './view-state.js';
import { createPostActions } from './post-actions.js';
import { createFab } from './fab.js';
import { createReview } from './review.js';
import { createSlideshow } from './slideshow.js';
import { createQuad } from './quad.js';
import { createNotes } from './notes.js';
import { initBatch } from './batch-download.js';
import { initSettings } from './settings.js';
import { createLightbox } from './lightbox.js';
import { initDevices } from './devices.js';
import { createMaintenanceJobs } from './maintenance-jobs.js';
import { createTimeline } from './timeline.js';
import { createAuthorView } from './author-view.js';
import { downloadLabel, captureGroupText, captureTitle, emptyState } from './content-state.js';

const $ = (s, el = document) => el.querySelector(s);
const feed = $('#feed');
/* 回顶动作单源：顶栏品牌钮（移动端）与右轨「回顶部」（桌面）共用一份语义 */
function scrollFeedTop() {
  window.scrollTo({top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
}
$('#btn-top').addEventListener('click', scrollFeedTop);

const S = {
  readOnly: document.documentElement.dataset.deviceRole === 'viewer',
  isAdmin: document.documentElement.dataset.deviceRole === 'admin',
  status: 'all', group: 'all', range: 'all', author: 'all',
  source: 'all',       // 'weibo' / 'x' = 只看某一来源（微博图片墙 / X 推文）
  hasCmts: 'all',      // '1' = 只看有评论的帖（评论豁免硬删的检索面，随 URL 同步）
  tags: 'all',         // 'a,b' = 任一标签命中即看（OR）；'all' 不筛（随 URL 同步）
  tagList: [],         // /api/tags 缓存（筛选行 chip 与批量打标选择层共用）
  offset: 0, total: 0, cursor: null, hasMore: true, requestVersion: 0, decisions: new Map(),
  posts: [],      // 已加载的帖子（当前筛选下，created_at 降序，与 feed 子元素顺序一致）
  images: [],     // 扁平化的图列表，供大图浏览
  st: null,       // /api/status 缓存
  pollTimer: null,
  pollFails: 0,
  selectMode: false,
  selected: new Set(),   // 选择模式勾选的 sid
  searchText: '',        // 正文搜索词（视图面板，随 URL 同步）
  searchScope: 'all',    // 搜索维度：all/text/comment/tag/author（随 URL 同步）
  appliedSearchText: '',
  appliedSearchScope: 'all',
  sort: '',              // '' 时间序 / 'keep_count' 点赞序 / 'shuffle' 随机序（随 URL 同步）
};

const maintenanceJobs = createMaintenanceJobs({api,toast,
  onJob: job => {
    if (!S.st) return;
    (S.st.jobs ||= {})[job.kind] = job;
    renderBanner();
  },
  onComplete: async kind => { await loadStatus(); if (kind === 'media') load(true); },
});

function fmtDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  const now = new Date();
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(d, now)) return '今天';
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (sameDay(d, yest)) return '昨天';
  const y = d.getFullYear() === now.getFullYear() ? '' : `${d.getFullYear()}-`;
  return `${y}${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function fmtHM(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtTime(iso) {
  return `${fmtDate(iso)} ${fmtHM(iso)}`.trim();
}

/* 视频时长：毫秒 → m:ss（X 的 dur_ms；缺失/0 不显示徽标时长段） */
function fmtDur(ms) {
  const s = Math.max(0, Math.round((ms || 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/* ---------- 状态、右侧状态栈与工具栏 ---------- */

async function loadStatus() {
  S.st = await api('/api/status');
  railSync();   // 计数/分组口径变了：左右轨立即跟上（徽章/视图计数/树重取），不等兜底轮询
  renderBanner();
  renderToolbar();
  updateMore();
  Object.values(S.st.jobs || {}).forEach(maintenanceJobs.observe);
  return S.st;
}

/* 左右轨同步钩子：rail.js 装载后把 sync 挂到 pwRail 上（事件驱动主通道）；
   未装载时（首屏模块序）为空操作，rail.js 自己的首跑兜底 */
function railSync() { window.pwRail?.railSync?.(); }

function banner(cls, html) {
  $('#banner').innerHTML = `<div class="banner ${cls}">${html}</div>`;
}

function renderBanner() {
  const st = S.st;
  $('#banner').innerHTML = '';
  // 「未填 Cookie」不再挂引导横幅：代抓通道（/api/ingest/harvest）已经覆盖无 Cookie 场景，
  // 这条横幅对老用户是常驻噪音；Cookie 过期/出错仍然要显式提醒。
  if (st.cookie.state === 'expired') {
    banner('bad', '<b>微博登录已失效</b>' + (S.isAdmin ? '到「<a href="#" id="lnk-cookie">来源连接</a>」更新 Cookie 并校验。' : '请用管理设备更新来源连接。'));
  } else if (!st.groups.some((g) => g.active) &&
             (st.cookie.state === 'ok' || Object.values(st.counts || {}).some(n => n > 0))) {
    banner('', '<b>还没有配置分组</b>' + (S.isAdmin ? '到「<a href="#" id="lnk-groups">管理分组</a>」添加分组，再点「立即抓取」。' : '请用管理设备添加分组。'));
  } else if (st.crawl.running) {
    banner('', `<b><span class="spin"></span> 正在抓取…</b>完成后列表会自动刷新。`);
  }
  const issues = [];
  if (st.connections?.x?.state === 'expired') issues.push('X 登录已失效，' + (S.isAdmin ? '请到「<a href="#" id="lnk-x-cookie">来源连接</a>」更新 Cookie' : '请用管理设备更新来源连接'));
  if (st.archive?.missing) issues.push(`${st.archive.missing} 张保留图片尚未归档 ${S.isAdmin ? '<button id="retry-archive">重试归档</button>' : '需管理设备重试'}`);
  if (st.media?.incomplete_posts || st.archive?.missing_originals) issues.push(`${st.media?.incomplete_posts ? st.media.incomplete_posts + ' 帖图片待补齐' : '原图缺失'} ${S.isAdmin ? '<button id="retry-media">补抓图片</button>' : '需管理设备补抓'}`);
  if (st.crawl.error && st.cookie.state !== 'expired') issues.push(`${st.crawl.last_result?.outcome === 'incomplete' ? '采集范围未覆盖完' : '采集未全部完成'}，请到「自动抓取」查看并重试`);
  for (const kind of ['archive','media']) {
    const job = st.jobs?.[kind];
    const name = kind === 'archive' ? '归档检查' : '图片补抓';
    if (['queued','running'].includes(job?.state)) {
      const p = job.progress || {};
      issues.push(p.total && p.done >= p.total ? `${name} · 正在核验结果`
        : `${name}正在进行${p.total ? ` · ${p.done || 0}/${p.total}` : ''}`);
    } else if (['failed','interrupted'].includes(job?.state)) {
      issues.push(`${name}${job.state === 'interrupted' ? '已中断' : '失败'} ${S.isAdmin ? `<button data-retry-job="${kind}">重试</button>` : '需管理设备重试'}`);
    }
  }
  if (issues.length) $('#banner').insertAdjacentHTML('beforeend', `<div class="banner warn">${issues.join('；')}</div>`);
  for (const kind of ['archive','media']) {
    const btn = $('#retry-' + kind);
    if (btn) btn.onclick = async () => {
      btn.disabled = true;
      await maintenanceJobs.start(kind);
      renderBanner();
    };
    if (btn) btn.disabled = ['queued','running'].includes(st.jobs?.[kind]?.state);
  }
  $('#banner').querySelectorAll('[data-retry-job]').forEach(btn => {
    btn.onclick = async () => {
      if (btn.disabled) return;   // 请求中防重复提交
      btn.disabled = true;
      await maintenanceJobs.start(btn.dataset.retryJob);
      renderBanner();
    };
  });
  $('#lnk-cookie') && ($('#lnk-cookie').onclick = (e) => { e.preventDefault(); settings.openConnections('weibo'); });
  $('#lnk-x-cookie') && ($('#lnk-x-cookie').onclick = (e) => { e.preventDefault(); settings.openConnections('x'); });
  $('#lnk-groups') && ($('#lnk-groups').onclick = (e) => { e.preventDefault(); openGroups(); });
}

function renderToolbar() {
  const st = S.st;
  const c = st.counts;
  // 视图按钮：状态名为主文字，其余筛选收进漏斗计数徽章
  const { name, n } = viewState();
  $('#view-label').textContent = name;
  const f = $('#view-filter');
  f.hidden = n === 0;
  $('#view-fcount').textContent = n;
  // 设置窗口「清理空间」分区跟随状态：N 帖已到期待硬删（左栏徽章）
  const badge = $('#nav-purge-badge');
  if (badge && st.purge) {
    badge.hidden = !st.purge.due;
    badge.textContent = st.purge.due ? `${st.purge.due} 到期` : '';
  }
  // 「自动抓取」分区不因抓取中而禁用：它现在是过程面板的入口，抓取中打开正好看进度
}

/* ---------- 列表 ---------- */

/* 瀑布流列栈：CSS 多列（column-fill: balance）在追加新帖时会全量重配列——
   旧卡跳列、阅读位置丢失。固定列栈改为「新卡只进最短列」：已排卡片零位移，
   只有窗口宽度跨档（列数变化）才全量重排。≤640 固定双列，桌面沿用 300px 列宽节奏。 */
let feedCols = [];
/* 列高账本：appendToShortestCol 选最短列只查这份缓存，不再每插一卡就循环读 N 列
   scrollHeight（首屏 50 卡 × 5 列 ≈ 250 次强制同步布局）。契约：任何让列栈换血或
   卡片离场的路径必须把 colHeights 置 null 失效重建——失效点全覆盖在
   ensureFeedCols 各重建分支 / load(reset) / insertCardAt / insertCard 的贴前驱插 /
   下方 MutationObserver（卡片被标记摘除后从列栈移除）。失效漏一处列就会漂移。 */
let colHeights = null;
let colWidth = 0;
let colHeightsDirty = false;
/* 博主沉浸皮肤（胶片/展厅/蓝图）共用的单列标记：author-view.applySkin 挂/摘，
   这里只认标记不认具体皮肤——新皮肤加入 SKINS 后此处零改动 */
function authorStrip() { return feed.classList.contains('author-strip'); }
function feedColCount() {
  // 桌面保留左轨收起态安全边距（纯图模式铺满全幅），右侧 56px 彻底归还瀑布流；
  // 桌面列数保持稳定，左轨悬停浮盖展开零重排；平板（640px~900px）仅扣 32px，≤640 固定双列。
  if (window.innerWidth <= 640) return 2;
  const isPure = document.documentElement.dataset.pureimg === '1';
  const occupied = window.innerWidth >= 900 ? (isPure ? 32 : 116) : 32;
  return Math.min(5, Math.max(1, Math.floor((window.innerWidth - occupied) / 310)));
}
/* 收集当前墙上卡片并按时间线（S.posts）真实原始顺序排列：
   DOM 中的卡片因分布在各列栈内，querySelectorAll 拿到的是列优先序（第0列全集、第1列全集…），
   若直接重排会导致卡片时序被打乱窜位。按 S.posts 时序映射重新收集，保证重排时卡片绝不乱序。 */
function gatherFeedCards() {
  const map = new Map();
  const leftover = [];
  for (const card of feed.querySelectorAll('.card')) {
    const sid = card.dataset.sid;
    if (sid) map.set(sid, card);
    else leftover.push(card);
  }
  const cards = [];
  if (Array.isArray(S.posts)) {
    for (const p of S.posts) {
      const card = map.get(p.sid);
      if (card) {
        cards.push(card);
        map.delete(p.sid);
      }
    }
  }
  for (const card of map.values()) cards.push(card);
  for (const card of leftover) cards.push(card);
  return cards;
}
function estCardHeight(card, w) {
  const r = card._estRatio || 1;
  return Math.round(w / r) + (card._hasTitle ? 42 : 0) + 54;
}
function prepareColMetrics(emptyCols = false) {
  if (!colWidth && feedCols[0]) colWidth = feedCols[0].clientWidth || 300;
  if (!colHeights || colHeights.length !== feedCols.length || colHeightsDirty) {
    colHeights = emptyCols || feedCols.every((c) => c.childElementCount === 0)
      ? new Array(feedCols.length).fill(0)
      : feedCols.map((c) => c.scrollHeight);
    colHeightsDirty = false;
  }
  return colWidth || 300;
}
function appendCardsBatch(cards, emptyCols = false) {
  if (!cards.length) return;
  const w = prepareColMetrics(emptyCols);
  const frags = feedCols.map(() => document.createDocumentFragment());
  for (const card of cards) {
    let best = 0;
    for (let i = 1; i < colHeights.length; i++)
      if (colHeights[i] < colHeights[best]) best = i;
    frags[best].appendChild(card);
    colHeights[best] += estCardHeight(card, w);
  }
  for (let i = 0; i < feedCols.length; i++)
    if (frags[i].hasChildNodes()) feedCols[i].appendChild(frags[i]);
  colHeightsDirty = true;   // 下次跨页追加前仅在读写分离时重校 1 次列实高，当前批零回流
}
function ensureFeedCols() {
  if (timeline.active()) return;   // 编年态是日卷分块，列栈整体让位
  if (authorStrip()) {             // 沉浸皮肤单列条（胶片/展厅/蓝图），列栈退场
    if (!feedCols.length) return;
    const cards = gatherFeedCards();
    feed.innerHTML = ''; feedCols = [];
    colHeights = null; colWidth = 0; colHeightsDirty = false;
    const frag = document.createDocumentFragment();
    for (const c of cards) frag.appendChild(c);   // 平铺回墙：按时间线真实原始顺序
    feed.appendChild(frag);
    return;
  }
  const n = feedColCount();
  if (feedCols.length === n) return;
  const cards = gatherFeedCards();
  feed.innerHTML = '';
  colHeights = null; colWidth = 0; colHeightsDirty = false;
  const colFrag = document.createDocumentFragment();
  feedCols = Array.from({ length: n }, () => {
    const col = document.createElement('div');
    col.className = 'feed-col';
    colFrag.appendChild(col);
    return col;
  });
  feed.appendChild(colFrag);
  appendCardsBatch(cards, true);
}
function appendToShortestCol(card, p) {
  if (timeline.active()) return timeline.appendCard(card, p);   // 编年态按日归桶
  if (authorStrip()) {                                          // 沉浸皮肤直接进单列条
    ensureFeedCols();
    feed.appendChild(card);
    return;
  }
  ensureFeedCols();
  const w = prepareColMetrics();
  let best = 0;
  for (let i = 1; i < colHeights.length; i++)
    if (colHeights[i] < colHeights[best]) best = i;
  feedCols[best].appendChild(card);
  colHeights[best] += estCardHeight(card, w);
}
function insertCardAt(card, ref) {   // 撤销恢复等再插入：跟随参照卡所在列，保持阅读顺序
  if (timeline.active()) return timeline.insertCard(card, ref);   // 编年态按日内时序归位
  if (authorStrip()) {                                           // 沉浸皮肤单列：就地插回
    ensureFeedCols();
    const anchor = ref && ref.parentElement === feed ? ref : null;
    feed.insertBefore(card, anchor);
    return;
  }
  ensureFeedCols();
  const col = ref?.closest('.feed-col');
  if (col) {
    col.insertBefore(card, ref);
    colHeights = null;   // 中途插卡改变所在列高：撤销恢复是低频路径，直接作废账本最稳
  }
  else appendToShortestCol(card);
}
let resizeRaf = 0, resizeTimer = 0;
window.addEventListener('resize', () => {
  if (!resizeRaf) {
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      if (!$('#view-panel').hidden) alignViewPanel();
    });
  }
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    resizeTimer = 0;
    colWidth = 0;
    ensureFeedCols();
  }, 80);
});

let loadController = null;
async function load(reset) {
  if (S.loading && !reset) return;
  if (reset) loadController?.abort();
  loadController = new AbortController();
  const version = ++S.requestVersion;
  const started = Date.now();
  S.loading = true;
  renderViewButton();
  if (!reset) { const more = $('#btn-more'); more.disabled = true; more.textContent = '加载中…'; more.setAttribute('aria-busy', 'true'); }
  if (reset) {
    syncUrl(); S.cursor = null; S.hasMore = true; S.offset = 0;
    S.posts = []; S.images = []; feed.innerHTML = ''; feedCols = [];
    colHeights = null; colWidth = 0; colHeightsDirty = false;
    authorView.sync();   // 博主视图头的显隐跟 S.author；筛选链路的每次换血都从这里过
    renderActiveSearchChip();
  }
  try {
    const q = new URLSearchParams({status:S.status, group:S.group, author:S.author,
      range:S.range, q:S.searchText,
      has_cmts:S.hasCmts === '1' ? '1' : 'all', source:S.source, limit:50});
    if (S.tags !== 'all') q.set('tags', S.tags);
    if (S.searchScope && S.searchScope !== 'all') q.set('q_scope', S.searchScope);
    if (S.sort === 'shuffle') q.set('shuffle', shuffleSeed(window.pwGetPrefs?.()));
    else if (S.sort) q.set('sort', S.sort);
    if (S.cursor) q.set('cursor', S.cursor);
    const data = await api('/api/posts?' + q, 'GET', undefined, {signal:loadController.signal});
    if (version !== S.requestVersion) return;
    S.total = data.total;
    S.cursor = data.next_cursor; S.hasMore = data.has_more;
    const known = new Set(S.posts.map(p => p.sid));
    const added = [];
    for (const row of data.posts) {
      const local = S.decisions.get(row.sid);
      if (local?._settledAt && local._settledAt < started) S.decisions.delete(row.sid);
      const p = S.decisions.get(row.sid) || row;
      if (known.has(p.sid) || !matchesView(p,S)) continue;
      known.add(p.sid); S.posts.push(p);
      const card = renderCard(p);
      added.push({ p, card });
      for (const pc of p.pics) S.images.push({ p, pc, url: `/media/${pc.file}` });
    }
    if (added.length) {
      if (timeline.active()) {
        for (const { card, p } of added) timeline.appendCard(card, p);
      } else if (authorStrip()) {
        ensureFeedCols();
        const frag = document.createDocumentFragment();
        for (const { card } of added) frag.appendChild(card);
        feed.appendChild(frag);
      } else {
        const emptyBefore = !feedCols.length;
        ensureFeedCols();
        appendCardsBatch(added.map((x) => x.card), emptyBefore);
      }
    }
    S.offset = S.posts.length;
    S.loadError = false;
    updateMore();
  } catch (e) {
    if (e.name !== 'AbortError' && version === S.requestVersion) {
      S.loadError = true; toast(e.message); updateMore();
    }
  } finally {
    if (version === S.requestVersion) S.loading = false;
  }
}

function updateMore() {
  const more = $('#btn-more');
  more.disabled = false; more.removeAttribute('aria-busy');
  $('.more').classList.toggle('is-empty', !S.loadError && !S.total);   // 空态给足留白
  if (S.loadError) {
    more.hidden = false;
    more.textContent = '加载失败，点此重试';
    $('#endnote').textContent = '';
    return;
  }
  more.textContent = '加载更多';
  more.hidden = !S.hasMore;
  const endNotes = {
    pending: '到底了',
    kept: '保留的图都翻完了',
    favorites: '收藏的图都翻完了',
    discarded: '删过的图都翻完了',
    all: '到底了',
  };
  let note = '';
  const empty = !S.total ? emptyState(S) : null;
  if (empty) note = empty.text;
  else if (!S.hasMore) note = endNotes[S.status] || '';
  $('#endnote').textContent = note;
  if (empty?.action) {
    const btn = document.createElement('button'); btn.className = 'tbtn';
    btn.textContent = {connection:'连接微博',groups:'管理分组',crawl:'立即抓取'}[empty.action];
    btn.onclick = () => empty.action === 'connection' ? settings.openConnections('weibo')
      : empty.action === 'groups' ? settings.openGroups() : startCrawl();
    $('#endnote').append(document.createTextNode(' '), btn);
  }
}

/* 卡片动作图标（feather 风格描边 SVG，与主题按钮同款语法）；文字说明走 title/aria-label */
const svg = (inner) =>
  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const ICONS = {
  external: svg('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'),
  link: svg('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
  star: svg('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'),
  check: svg('<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>'),
  plus: svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
  minus: svg('<line x1="5" y1="12" x2="19" y2="12"/>'),
  circleX: svg('<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>'),
  x: svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
  restore: svg('<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>'),
  play: svg('<polygon points="6 4 20 12 6 20 6 4"/>'),
  download: svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
  comment: svg('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>'),
  grid: svg('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>'),
  checkSquare: svg('<polyline points="9 11 12 14 22 4"/><path d="M21.12 10.9V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9.71"/>'),
  layers: svg('<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>'),
  image: svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>'),
  heart: svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>'),
};

/* 图片加载失败兜底图：data-URI SVG（320×320 浅灰底 + 断链图标 + 「图片缺失」小字）。
   与 kiosk.js 的 NO IMAGE 占位同语义——/media/ 缩略图 404 或缺失时给一张可辨识的
   占位，不露裸 alt 文本。纯静态串，不拼任何用户数据。 */
const IMG_FALLBACK = 'data:image/svg+xml,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">' +
  '<rect width="320" height="320" fill="#f0f0f2"/>' +
  '<g fill="none" stroke="#9aa0a6" stroke-width="7" stroke-linecap="round">' +
  '<line x1="112" y1="146" x2="146" y2="146"/><line x1="174" y1="146" x2="208" y2="146"/>' +
  '</g>' +
  '<circle cx="160" cy="146" r="6" fill="#9aa0a6"/>' +
  '<text x="160" y="206" text-anchor="middle" font-family="system-ui,sans-serif" ' +
  'font-size="20" fill="#9aa0a6">图片缺失</text></svg>');

/* 头像 = 进博主视图的按钮（墙卡与详情共用）：视觉维持原样，title 说明去处。
   多选模式下点击走勾选分支；样式重置见 style.css 的 button.avatar */
const avatarButton = (p) => {
  const av = avatarSrc(p.avatar);
  return `<button type="button" class="avatar av-link" data-uid="${esc(p.uid)}" title="查看 ${esc(p.author)} 的全部帖子"`
    + ` aria-label="查看 ${esc(p.author)} 的全部帖子">${av
      ? `<img src="${esc(av)}" alt="" loading="lazy" decoding="async">` : esc(p.author[0] || '微')}</button>`;
};

/* 帖子的「今日已赞」态：last_confirm_at（建账/+1 时间戳）落在东八区今天即为真 */
function likedTodayOf(p) {
  if (!p.last_confirm_at) return false;
  const day = (iso) => new Date(Date.parse(iso) + 8 * 3600e3).toISOString().slice(0, 10);
  try { return day(p.last_confirm_at) === day(new Date().toISOString()); }
  catch (e) { return false; }
}

function renderCard(p) {
  const el = document.createElement('article');
  // 收藏走琥珀描边（.card.fav），不再摆收藏 chip；保留态维持 accent 描边
  el.className = 'card' + (p.status === 'kept' ? ' kept' : '') + (p.favorite ? ' fav' : '');
  el.dataset.sid = p.sid;
  el.tabIndex = 0;   // 键盘可聚焦：Tab 到卡片后 Enter 打开（与点击同路径）
  // XHS 式卡：首图原始比例（竖图钳到 3:4 防霸屏、全景钳到 21:9 防细条），其余图进大图/详情看
  const first = (p.pics || [])[0];
  const n = (p.pics || []).length;
  const ratio = first && first.w && first.h
    ? Math.min(Math.max(first.w / first.h, 0.75), 2.33) : 1;
  const pics = first
    ? `<img src="/media/${esc(first.thumb)}" alt="${esc(p.author)} 的微博图" loading="lazy" decoding="async"
         style="aspect-ratio:${ratio.toFixed(4)}" data-file="${esc(first.file)}">` : '';
  // 视频卡：海报静帧上叠 ▶ 徽标 + 时长（判定与点击行为不变，海报即所见）
  const vid = first?.kind === 'video'
    ? `<span class="vid-badge">${ICONS.play}<b>${fmtDur(first.dur_ms)}</b></span>` : '';
  const pill = n > 1 ? `<span class="pics-pill">${n} 图</span>` : '';
  // 待补齐走图面左上角标（与 N 图胶囊同视觉语言）；文本不完整提示归详情正文
  const warn = p.image_complete === false ? '<span class="img-warn">待补齐</span>' : '';
  // 收藏钮：图面右上角，仅桌面悬浮/聚焦出现（触屏收藏走详情，不占常驻视觉）
  const favBtn = S.readOnly ? '' :
    `<button class="fav-btn${p.favorite ? ' on' : ''}" title="${p.favorite ? '取消收藏' : '收藏'}"
       aria-label="${p.favorite ? '取消收藏' : '收藏'}" aria-pressed="${String(!!p.favorite)}">${ICONS.star}</button>`;
  // 来源标识：微博是默认来源不显示，等出现第二个来源才占一处视觉
  const srcChip = p.source && p.source !== 'weibo'
    ? `<span class="src">${p.source === 'x' ? 'X' : esc(p.source)}</span>` : '';
  // 标题行 = 正文开头一行；搜索态优先显示命中的批注并高亮命中词（找得回来看得见为什么）
  const term = (S.searchText || '').trim();
  let titleRow = '';
  if (term && p.comment_match) {
    titleRow = `<p class="title is-match" title="批注命中">${ICONS.comment}<span class="ttext">${markTerm(p.comment_match, term, S.searchScope)}</span></p>`;
  } else if (p.text) {
    titleRow = `<p class="title" title="${esc(p.text)}">${esc(p.text)}</p>`;
  }
  // ♥N = 点赞次数（数字在心左侧）：kept 点心 +1（每日一次），未分拣/不要帖点心 = 保留建账
  const liked = likedTodayOf(p);
  const cnt = p.status === 'kept' ? (p.keep_count || 1) : 0;
  const likeTitle = p.status !== 'kept' ? '点赞保留'
    : liked ? '今天已经赞过了' : '赞一下（点赞 +1）';
  const like = S.readOnly ? '' :
    `<button class="like${p.status === 'kept' ? (liked ? ' done' : ' on') : ''}"
       title="${esc(likeTitle)}" aria-label="${esc(likeTitle)}"
       aria-pressed="${String(p.status === 'kept' && liked)}">${cnt ? `<span class="ln">${cnt}</span>` : ''}${ICONS.heart}</button>`;
  el._estRatio = ratio;
  el._hasTitle = Boolean(titleRow);
  el.innerHTML = `
    <div class="pics${p.source === 'x' ? ' x-nsfw' : ''}">${pics}${vid}${pill}${warn}${favBtn}<span class="pick-dot" aria-hidden="true">✓</span></div>
    ${titleRow}
    <div class="meta">
      ${avatarButton(p)}
      <b>${esc(p.author)}</b>
      ${srcChip}${like}
    </div>`;
  return el;
}

function rebuildImages() {
  S.images = [];
  for (const p of S.posts)
    for (const pc of p.pics)
      S.images.push({ p, pc, url: `/media/${pc.file}` });
}

/* ---------- 博主筛选（视图面板「博主」行） ---------- */

let authorsSeq = 0;
let authorPickerOpen = false;   // 行内选择器展开态（随面板关闭一并归位）
let authorQuery = '';           // 选择器搜索词（展开期间跨重渲保留）

async function refreshAuthors() {
  const seq = ++authorsSeq;   // 连续换筛选时只认最后一次请求的结果
  try {
    const r = await api('/api/authors?group=' + encodeURIComponent(S.group) +
                        '&status=' + encodeURIComponent(S.status) +
                        '&sort=name_asc' +   // 联系人式：拼音顺序 + 字母索引，找人是浏览不是追新
                        '&source=' + encodeURIComponent(S.source));
    if (seq !== authorsSeq) return;
    // 当前分类下 0 帖的博主不显示（正被选中的除外，否则筛选会凭空消失）
    S.authorsList = (r.authors || []).filter((a) => a.n > 0 || a.uid === S.author);
    renderAuthorRow();   // 面板没开时 #f-author 不在渲染树上，重画是幂等空操作
  } catch (e) { /* 博主行失败不阻塞主流程 */ }
}

/* 标签清单（筛选行 + 批量打标选择层共用）：打标/清标后调一次，chip 计数跟上。
   不挂进 loadStatus 轮询——状态轮询高频，标签变更只在用户动作后发生 */
let tagsSeq = 0;
async function refreshTags() {
  const seq = ++tagsSeq;
  try {
    const r = await api('/api/tags');
    if (seq !== tagsSeq) return;
    S.tagList = r.tags || [];
    renderTagRow();   // 面板没开时重画是幂等空操作
    renderTagsChoices();
  } catch (e) { /* 标签行失败不阻塞主流程 */ }
}

const CARET_SVG = '<svg class="caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';
const faAvatar = (a, size = 22) => {
  const av = avatarSrc(a.avatar);
  return av
    ? `<img class="avatar" src="${esc(av)}" alt="" loading="lazy" width="${size}" height="${size}" decoding="async">`
    : `<span class="avatar">${esc((a.author || '微')[0])}</span>`;
};

/* 行首胶囊：未筛选 =「全部博主 ▾」；已筛选 = 头像+名字+✕（✕ 单独成钮清筛选）。
   两种形态都带 data-author-toggle——选中态也要能再点开换人。 */
function faChipHtml() {
  const sel = (S.authorsList || []).find((a) => a.uid === S.author);
  return S.author === 'all'
    ? `<button class="fchip fauthor ${authorPickerOpen ? 'on' : ''}" data-author-toggle
        aria-expanded="${authorPickerOpen}">全部博主<span class="fa-caret">${CARET_SVG}</span></button>`
    : `<span class="fchip fauthor on ${authorPickerOpen ? 'open' : ''}" data-author-toggle
        role="button" tabindex="0" aria-expanded="${authorPickerOpen}"
        aria-label="更换博主筛选">${faAvatar(sel || {}, 20)}
        <span class="fan">${esc(sel?.author || S.author)}</span>
        <button class="fax" data-author-clear title="回全部博主" aria-label="取消博主筛选">✕</button></span>`;
}

/* 选择器列表（联系人式）：搜索词客户端过滤（名字或 uid——X 的 uid 是 x:handle，可直接搜 @名）。
   无搜索词按拼音首字母分组 + 右侧 A–Z/# 索引轨（点字母跳组、按住扫动连续跳）；
   有搜索词退化为扁平结果（搜索接管，联系人应用同款）。 */
const FA_ALPHABET = [...'ABCDEFGHIJKLMNOPQRSTUVWXYZ', '#'];
const faRowHtml = (a) => `
  <button class="fa-row ${S.author === a.uid ? 'on' : ''}" data-uid="${esc(a.uid)}"
    role="option" aria-selected="${S.author === a.uid}"
    title="${esc(a.uid.startsWith('x:') ? '@' + a.uid.slice(2) + ' · ' : '')}${esc(a.author)} · ${a.n} 条">
    ${faAvatar(a)}<span class="fan">${esc(a.author)}</span><span class="an-count">${a.n}</span>
  </button>`;

function faListHtml() {
  const q = authorQuery.trim().toLowerCase();
  const rows = (S.authorsList || []).filter((a) =>
    !q || (a.author || '').toLowerCase().includes(q) || (a.uid || '').toLowerCase().includes(q));
  if (!rows.length)
    return `<div class="fa-empty">${S.authorsList ? '无匹配博主' : '博主列表加载中…'}</div>`;
  if (q) return rows.map(faRowHtml).join('');   // 搜索态：扁平结果不分组
  const byLetter = new Map();
  for (const a of rows) {
    const L = FA_ALPHABET.includes(a.initial) ? a.initial : '#';
    if (!byLetter.has(L)) byLetter.set(L, []);
    byLetter.get(L).push(a);
  }
  const letters = FA_ALPHABET.filter((L) => byLetter.has(L));
  const list = letters.map((L) => `
    <div class="fa-group" data-letter="${L}">
      <div class="fa-letter" aria-hidden="true">${L}</div>
      ${byLetter.get(L).map(faRowHtml).join('')}
    </div>`).join('');
  const rail = letters.map((L) =>
    `<button class="fa-idx" data-letter="${L}" aria-label="跳到 ${L}">${L}</button>`).join('');
  return `<div class="fa-groups">${list}</div><div class="fa-index" aria-label="首字母索引">${rail}</div>`;
}

/* 索引轨跳组：滚到目标组贴顶（字母头 sticky 正好接管顶部） */
function faJump(letter) {
  const list = $('#f-author .fa-list');
  const grp = list?.querySelector(`.fa-group[data-letter="${CSS.escape(letter)}"]`);
  if (list && grp) list.scrollTop = grp.offsetTop;
}

/* 只重画列表区（.fa-groups/.fa-index 都在其内）：整块重渲会弄丢搜索框焦点与光标，
   且不能冲掉 renderAuthorRow 留下的 .fa-list 滚动壳 */
function renderFaList() {
  const el = $('#f-author .fa-list');
  if (el) el.innerHTML = faListHtml();
}

function renderAuthorRow() {
  const box = $('#f-author');
  if (!box) return;
  box.innerHTML = faChipHtml() + (authorPickerOpen
    ? `<div class="fa-picker"><input class="fa-search" type="search"
         placeholder="搜博主名 / @handle…" maxlength="40" value="${esc(authorQuery)}"
         autocomplete="off" spellcheck="false" aria-label="搜索博主">
       <div class="fa-body"><div class="fa-list" role="listbox" aria-label="博主列表">${faListHtml()}</div></div></div>`
    : '');
  if (authorPickerOpen) $('#f-author .fa-search')?.focus();
}

/* 索引轨交互：按下即跳，按住纵向扫动连续跳（capture 后按指下位置找字母） */
$('#f-author')?.addEventListener('pointerdown', (e) => {
  const rail = e.target.closest('.fa-index');
  if (!rail) return;
  e.preventDefault();
  const pick = (y) => {
    const el = document.elementFromPoint(e.clientX, y)?.closest('.fa-idx');
    if (el) faJump(el.dataset.letter);
  };
  pick(e.clientY);
  try { rail.setPointerCapture(e.pointerId); } catch {}
  rail.onpointermove = (ev) => pick(ev.clientY);
  rail.onpointerup = rail.onpointercancel = () => { rail.onpointermove = null; };
});

$('#f-author')?.addEventListener('click', (e) => {
  // 选择/清除会重建 innerHTML 孤儿化 target，冒泡到外层面板关闭器会被误判为点外——
  // 在行内消化掉（与旧排序菜单同款守卫），面板保持打开
  e.stopPropagation();
  if (e.target.closest('[data-author-clear]')) {
    authorPickerOpen = false; authorQuery = '';
    S.author = 'all';   // 与来源/状态同权的筛选维度：只清博主，其余保持
    renderViewPanel(); load(true);
    return;
  }
  const row = e.target.closest('.fa-row');
  if (row) {
    authorPickerOpen = false; authorQuery = '';
    if (row.dataset.uid !== S.author) {
      S.author = row.dataset.uid;
      renderViewPanel(); load(true);
    } else renderAuthorRow();   // 点当前已选博主：只收选择器，不动筛选
    return;
  }
  if (e.target.closest('[data-author-toggle]')) {
    authorPickerOpen = !authorPickerOpen;
    if (!authorPickerOpen) authorQuery = '';
    renderAuthorRow();
  }
});
$('#f-author')?.addEventListener('input', (e) => {
  if (!e.target.classList.contains('fa-search')) return;
  authorQuery = e.target.value;
  renderFaList();
});
// 搜索框里的按键不进全局快捷键；Esc 只收选择器，不惊动面板。
// 选中态胶囊是 span[role=button]：Enter/空格手动补开合（浏览器只替 button 免费做这事）
$('#f-author')?.addEventListener('keydown', (e) => {
  if (e.target.classList.contains('fa-search')) {
    e.stopPropagation();
    if (e.key === 'Escape' && authorPickerOpen) {
      authorPickerOpen = false; authorQuery = '';
      renderAuthorRow();
    }
    return;
  }
  if (e.target.closest('[data-author-toggle]') && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    e.target.closest('[data-author-toggle]').click();
  }
});

/* ---------- 标记（真乐观：同步摘卡零等待；POST 后台串行，失败回滚） ---------- */

let statusRefreshTimer = null;
function queueStatusRefresh() {
  // 快筛连按时聚合计数刷新：500ms 尾触一次，别每张卡都打两遍全表聚合
  clearTimeout(statusRefreshTimer);
  statusRefreshTimer = setTimeout(() => loadStatus().catch(() => {}), 500);
}

const { mark, toggleFavorite, toggleTag, reaffirm, undoLastMark, refreshUndoBtns, enqueueMutation, getLastDrop } = createPostActions({
  S, feed, $, api, toast,
  // 两种插卡模式（syncPost 依位次选择；位次不变走 patchCard 就地修补，不经这里）：
  // ref=后继参照前插；末张无后继 → 贴前驱插（保列保位，不退化追加最短列）；
  // 编年态照走 insertCardAt 的日卷归桶
  insertCard: (p, ref, prev) => {
    const el = renderCard(p);
    // 贴前驱插不经 appendToShortestCol/insertCardAt，列高变了须作废账本（契约见 feedCols 声明旁）
    if (!ref && prev && !timeline.active()) { prev.after(el); colHeights = null; return; }
    insertCardAt(el, ref);
  },
  likedTodayOf, rebuildImages, updateMore, queueStatusRefresh,
  startMaintenance: maintenanceJobs.start,
  renderLB, getLBIndex:() => lightbox.getIndex(), refreshDetail,
  getDetailSid: () => detailSid,   // 标签乐观写失败回滚后，只对前卡刷新详情
});

/* ---------- 四格快筛（桌面专属会话）：键位、簿记与预取都在 quad.js ---------- */
const quad = createQuad({
  S, $, toast, esc, fmtDate, fmtHM, icons: ICONS,
  mark, toggleFavorite, getLastDrop, undoLastMark,
  load, openLB, openLinkForSid, switchStatusView,
  animatedHide, cancelHide,
});

/* ---------- 回看（悬浮球入口）：复用 #detail 覆盖层，导航与动作在下方各函数里走 review 分支 ---------- */
const review = createReview({
  S, $, api, toast, enqueueMutation,
  detailCards, renderDetailInto, resetDetailCard,
  cancelHide, animatedHide, safeUrl, trapTab,
  setDetailSid: (v) => { detailSid = v; },
  showStageImage, openLB,   // ←→/空格 与详情同一规则所需的舞台换图与大图入口
});
const slideshow = createSlideshow({
  S, $, api, toast, enqueueMutation, fmtTime, trapTab, animatedHide, cancelHide,
  commentBodyHTML,
});
const notes = createNotes({
  $, api, toast, esc, commentBodyHTML, fmtCommentTime,
  openDetail: (sid, focus) => openDetail(sid, focus),
  animatedHide, cancelHide,
});
const fab = createFab({
  $,
  items: [
    { icon: ICONS.check,
      label: '快筛',   // 桌面进四格会话，窄屏退回单卡详情流（startQuickTriage 统一分流）
      run: () => startQuickTriage() },
    { icon: ICONS.image,
      label: () => `纯图${window.pwGetPrefs ? (window.pwGetPrefs().pureimg ? ' · 开' : '') : ''}`,
      run: () => {   // 写偏好即改 html[data-pureimg]，墙卡与点击分发即时切换
        const prefs = window.pwGetPrefs ? window.pwGetPrefs() : {};
        window.pwSetPref('pureimg', prefs.pureimg ? '' : '1');
      } },
    { icon: ICONS.checkSquare, label: '多选', run: () => {   // 长按之外的显式入口：多选不再只靠手势偶然发现
        if (S.readOnly) { toast('只读设备不能批量操作'); return; }
        if (!feed.querySelector('.card:not(.fading)')) { toast('当前没有可勾选的卡片'); return; }
        setSelectMode(true);
      } },
    { icon: ICONS.restore, label: '回看', run: () => review.open() },
    { icon: ICONS.play, label: '放映', run: () => slideshow.open() },
    { icon: ICONS.comment, label: '批注', run: () => notes.open() },
  ],
});

/* 图片加载失败兜底：error 事件不冒泡但可在捕获层监听，墙卡/详情/四格三处一并挂住。
   只处理 src 以 /media/ 开头的图（外链头像等不管），换 IMG_FALLBACK 占位；
   dataset.fb 保证兜底只放一次，占位图自身再失败不会死循环。 */
function onImgError(e) {
  const img = e.target;
  if (!(img instanceof HTMLImageElement)) return;
  if (img.dataset.fb || !(img.getAttribute('src') || '').startsWith('/media/')) return;
  img.dataset.fb = '1';
  img.src = IMG_FALLBACK;
}
for (const root of [feed, document.getElementById('detail'), document.getElementById('quad')])
  root.addEventListener('error', onImgError, true);

/* 列高账本的兜底失效：卡片被标记摘除后从列栈移除会改变列高，仅在卡片/列节点移除时
   作废账本（忽略 patchCard 改 .ln 等卡内局部增删）。模块初始化处挂一次。 */
new MutationObserver((muts) => {
  for (const m of muts)
    for (const n of m.removedNodes)
      if (n.nodeType === Node.ELEMENT_NODE
          && (n.classList?.contains('card') || n.classList?.contains('feed-col'))) {
        colHeights = null; return;
      }
}).observe(feed, { childList: true, subtree: true });

feed.addEventListener('click', (e) => {
  if (lpSuppressClick) { lpSuppressClick = false; return; }   // 长按刚进入选择模式：吞掉尾随 click
  if (S.selectMode) {
    const card = e.target.closest('.card');
    if (card) togglePick(card.dataset.sid);
    return;
  }
  // 头像 = 进博主视图（该博主全部内容）：分支须在按钮让位之前，否则被当作普通按钮吞掉
  const av = e.target.closest('.av-link');
  if (av) {
    const card = av.closest('.card');
    authorView.open(av.dataset.uid, S.posts.find((q) => q.sid === card?.dataset.sid));
    return;
  }
  // ♥N = 点赞次数：kept 心 +1（每日一次，当日已赞就地提示）；未分拣/不要帖点心 = 保留建账。
  // 分支须在按钮让位之前；当日已赞不发起请求（服务端同样拦截，双保险）
  const like = e.target.closest('.like');
  if (like) {
    const sid = like.closest('.card').dataset.sid;
    const p = S.posts.find((q) => q.sid === sid);
    if (!p) return;
    if (p.status === 'kept') {
      if (likedTodayOf(p)) { toast('今天已经赞过了，明天再来'); return; }
      reaffirm(sid, 1);
    } else {
      mark(sid, 'kept');
    }
    return;
  }
  // 收藏钮（图面右上悬浮）：乐观收藏，动作与详情 M 键同一函数
  const fav = e.target.closest('.fav-btn');
  if (fav) { toggleFavorite(fav.closest('.card').dataset.sid); return; }
  // 卡内链接/其余普通按钮保持默认行为不劫持
  if (e.target.closest('.card a, .card button')) return;
  // 默认点卡片任意位置 = 进详情；纯图模式下卡片只做图片入口，点图直达大图（←→ 跨帖连看）
  const pics = e.target.closest('.pics');
  if (pics && document.documentElement.dataset.pureimg === '1') {
    const sid = pics.closest('.card').dataset.sid;
    const i = S.images.findIndex((im) => im.p.sid === sid);
    if (i >= 0) { openLB(i, sid); return; }
  }
  const card = e.target.closest('.card');
  if (card) openDetail(card.dataset.sid);
});
// 卡片键盘入口：聚焦卡片本身（不是卡内按钮）时 Enter = 点击卡片，复用点击分支（详情 / 纯图大图 / 多选勾选）
feed.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.repeat) return;
  const card = e.target;
  if (!(card instanceof Element) || !card.matches('#feed .card')) return;
  e.preventDefault();
  (card.querySelector('.pics') || card).click();
});

/* ---------- 大图浏览 ---------- */

const lightbox = createLightbox({S,$,animatedHide,cancelHide,
  resetZoom:() => {gz.zoom=false; gz.px = gz.py = 0;},
  crossPosts: () => document.documentElement.dataset.pureimg === '1'});
function openLB(i,sid) { lightbox.openLB(i,sid); }
function closeLB() { lightbox.closeLB(); }
function renderLB() { lightbox.renderLB(); }
function lbNav(d) { lightbox.lbNav(d); }

/* ---------- 手机手势：大图 + 卡片流（仅触屏生效，桌面不受影响） ---------- */

let gz = { sx: 0, sy: 0, dx: 0, dy: 0, mx: 0, my: 0, px: 0, py: 0,
           lastTap: 0, zoom: false, movedFlag: false };
let lbTapCloseTimer = 0;

/* 双击缩放（中心 2.2×）；缩放态单指平移并钳在图内，双击切回。
   不做触点原点缩放：中心缩放的平移钳制对称，边界不会跟丢 */
function setZoom(on) {
  const img = $('#lb-img');
  gz.zoom = on; gz.px = 0; gz.py = 0;
  if (on) {
    img.style.transformOrigin = '50% 50%';
    img.style.transform = 'scale(2.2)';
  } else {
    img.style.transform = '';
    img.style.transformOrigin = '';
  }
}

const lbStage = $('.lb-stage');
lbStage.addEventListener('touchstart', (e) => {
  if (e.touches.length !== 1) return;
  const t = e.touches[0];
  gz.sx = t.clientX; gz.sy = t.clientY; gz.dx = gz.dy = 0;
  gz.mx = t.clientX; gz.my = t.clientY;
  const now = Date.now();
  const doubleTap = now - gz.lastTap < 300 && !gz.movedFlag;
  if (doubleTap && !lbStage.classList.contains('lb-long')) {
    clearTimeout(lbTapCloseTimer); lbTapCloseTimer = 0;
    setZoom(!gz.zoom);   // 双击缩放/还原（触屏双击语义归缩放，dblfav 不在此生效）；长图档让位原生滚动
  }
  gz.doubleTap = doubleTap;
  gz.lastTap = now; gz.movedFlag = false;
}, { passive: true });

lbStage.addEventListener('touchmove', (e) => {
  gz.movedFlag = true;
  if (e.touches.length !== 1) return;
  const t = e.touches[0];
  const img = $('#lb-img');
  if (gz.zoom) {
    gz.px += t.clientX - gz.mx; gz.py += t.clientY - gz.my;
    gz.mx = t.clientX; gz.my = t.clientY;
    const maxX = Math.max(0, (img.offsetWidth * 2.2 - lbStage.clientWidth) / 2);
    const maxY = Math.max(0, (img.offsetHeight * 2.2 - lbStage.clientHeight) / 2);
    gz.px = Math.max(-maxX, Math.min(maxX, gz.px));
    gz.py = Math.max(-maxY, Math.min(maxY, gz.py));
    img.style.transform = `translate(${gz.px}px, ${gz.py}px) scale(2.2)`;
    return;
  }
  gz.dx = t.clientX - gz.sx; gz.dy = t.clientY - gz.sy;
  if (Math.abs(gz.dx) > Math.abs(gz.dy) && Math.abs(gz.dx) > 20) {
    img.style.transform = `translateX(${gz.dx * 0.5}px)`;
  }
}, { passive: true });

lbStage.addEventListener('touchend', () => {
  const img = $('#lb-img');
  if (gz.zoom) {
    img.style.transform = `translate(${gz.px}px, ${gz.py}px) scale(2.2)`;   // 平移位置保留
    return;
  }
  img.style.transform = '';
  const { dx, dy } = gz;
  if (Math.abs(dx) > 50 && Math.abs(dx) > 2 * Math.abs(dy)) {
    clearTimeout(lbTapCloseTimer); lbTapCloseTimer = 0;
    lbNav(dx < 0 ? 1 : -1);
    return;
  }
  if (gz.doubleTap) { gz.doubleTap = false; return; }
  clearTimeout(lbTapCloseTimer);
  // 欠量滑动（想翻图没翻成）：回弹即止，不当作点击退出
  if (Math.abs(dx) > 8 || Math.abs(dy) > 8) { lbTapCloseTimer = 0; return; }
  lbTapCloseTimer = setTimeout(() => {
    if (!$('#lightbox').hidden) closeLB();
    lbTapCloseTimer = 0;
  }, 320);
}, { passive: true });

/* 卡片流：长按进入选择模式（保留/不要的滑动决定随墙卡纯浏览化退役，分拣走快筛会话） */
let lp = null, lpSuppressClick = false;
feed.addEventListener('pointerdown', (e) => {
  if (S.selectMode || S.readOnly) return;
  if (e.target.closest('a, button')) return;
  const card = e.target.closest('.card');
  if (!card || card.classList.contains('fading')) return;
  lp = { card, x: e.clientX, y: e.clientY, timer: setTimeout(() => {
    lp = null;
    card.classList.remove('pressing');
    lpSuppressClick = true;   // 随后的 click 会落在选择模式勾选上，吞掉
    setSelectMode(true);
    togglePick(card.dataset.sid);
  }, 500) };
  card.classList.add('pressing');
});
function cancelLongPress(e) {
  if (!lp) return;
  if (e && e.type === 'pointermove' &&
      Math.hypot(e.clientX - lp.x, e.clientY - lp.y) < 8) return;   // 轻微抖动不算移动
  clearTimeout(lp.timer);
  lp.card.classList.remove('pressing');
  lp = null;
}
feed.addEventListener('pointermove', cancelLongPress, {passive: true});
feed.addEventListener('pointerup', cancelLongPress, {passive: true});
feed.addEventListener('pointercancel', cancelLongPress, {passive: true});
feed.addEventListener('contextmenu', (e) => {
  if (lp && lp.card.contains(e.target)) e.preventDefault();   // 长按期间压住系统右键/呼出菜单
});

function trapTab(e, root) {
  const els = [...root.querySelectorAll('button, a[href], input, textarea, select')]
    .filter((el) => !el.disabled && el.offsetParent !== null && !el.closest('[inert]'));
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
  else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
}

// 卡片流的「当前卡」：视口里露出面积最大的那张（滚动浏览的落点），空格看它的图
function currentCard() {
  const vh = window.innerHeight;
  let best = null, bestArea = 0;
  const scanList = (els) => {
    if (!els || !els.length) return;
    let lo = 0, hi = els.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (els[mid].getBoundingClientRect().bottom <= 0) lo = mid + 1;
      else hi = mid;
    }
    for (let i = lo; i < els.length; i += 1) {
      const el = els[i];
      const r = el.getBoundingClientRect();
      if (r.top >= vh) break;
      if (!el.classList.contains('card') || el.classList.contains('fading')) continue;
      const area = Math.min(r.bottom, vh) - Math.max(r.top, 0);
      if (r.width > 0 && area > bestArea) { bestArea = area; best = el; }
    }
  };
  if (timeline.active()) {
    for (const sec of feed.querySelectorAll('.tday')) {
      const sr = sec.getBoundingClientRect();
      if (sr.bottom <= 0) continue;
      if (sr.top >= vh) break;
      for (const el of sec.querySelectorAll('.card:not(.fading)')) {
        const r = el.getBoundingClientRect();
        if (r.bottom <= 0 || r.top >= vh) continue;
        const area = Math.min(r.bottom, vh) - Math.max(r.top, 0);
        if (r.width > 0 && area > bestArea) { bestArea = area; best = el; }
      }
    }
  } else if (authorStrip()) {
    scanList(feed.children);
  } else if (feedCols.length) {
    for (const col of feedCols) scanList(col.children);
  } else {
    scanList(feed.querySelectorAll('.card:not(.fading)'));
  }
  return best;
}

document.addEventListener('keydown', (e) => {
  // Cmd+K / Ctrl+K 唤起或切换聚光灯搜索
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    const spModal = $('#modal-spotlight-search');
    if (spModal && !spModal.hidden) closeSpotlightSearch();
    else openSpotlightSearch();
    return;
  }
  // Ctrl/Cmd/Alt 组合键交给浏览器和系统；数字视图键使用未修饰的单键。
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const modal = document.querySelector('.modal:not([hidden])');
  if (modal) {
    if (modal.id === 'modal-settings' && e.key === 'Escape' && settings.mobileBack?.()) {
      e.preventDefault();   // 手机推入面板态：Esc 先返回根菜单，再按才关窗（同 iOS 逐层退）
      return;
    }
    if (e.key === 'Escape' || (modal.id === 'modal-settings' && (e.key === '?' || (e.shiftKey && e.key === '/')))) {
      e.preventDefault();
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
      closeModal(modal.id);
    }
    else if (e.key === 'Tab') trapTab(e, modal);
    return;
  }
  const target = e.target;
  const editing = target instanceof Element && (
    target.isContentEditable || target.matches('input, textarea, select') ||
    !!target.closest('[contenteditable="true"], [contenteditable=""]')
  );
  // ←→ 允许连发（按住连续切图），PageUp/PageDown 允许连发（按住连续翻帖）；
  // ↑↓ 仅在大图打开时允许连发（长图连续滚动阅读）——详情里按住 ↑ 仍不会连发标记
  const lbArrowRepeat = !$('#lightbox').hidden && (e.key === 'ArrowUp' || e.key === 'ArrowDown');
  if (e.repeat && e.key !== 'ArrowLeft' && e.key !== 'ArrowRight'
      && e.key !== 'PageUp' && e.key !== 'PageDown' && !lbArrowRepeat) return;
  if (e.key === 'Escape') {
    let handled = true;
    if (!$('#lightbox').hidden) closeLB();
    else if (!$('#detail').hidden) {
      // 评论输入/行内编辑聚焦时第一下 Esc 只退出编辑，第二下才关浮层
      const ae = document.activeElement;
      if (ae && $('#detail').contains(ae) && ae.matches('.dt-cinput, .c-editbox')) { ae.blur(); return; }
      closeDetail();
    }
    else if (!$('#slideshow').hidden && slideshow.active()) slideshow.close();
    else if (!$('#notes-view').hidden && notes.active()) notes.close();
    else if (!$('#quad').hidden && quad.active()) quad.key(e);   // 四格：Esc 先退细看再退会话
    else if (S.selectMode) setSelectMode(false);
    else if (!$('#view-panel').hidden) { closeViewPanel(); e.stopPropagation(); }   // 视图面板同入逐层退链（此前只认点外收起）
    else handled = false;
    if (handled) {
      e.stopPropagation();
      if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
    }
    return;
  }
  if (editing) return;
  if (!e.repeat && (e.key === '?' || (e.shiftKey && e.key === '/'))) {
    e.preventDefault();
    if (!$('#modal-settings').hidden) closeModal('modal-settings');
    else settings.openSettings('shortcuts');
    return;
  }
  // '/' 呼出聚光灯大搜索栏（非输入态、无修饰键、大图/放映/四格让位）
  if (!e.repeat && e.key === '/' && !e.shiftKey) {
    if ($('#lightbox').hidden && ($('#slideshow').hidden || !slideshow.active()) && ($('#quad').hidden || !quad.active())) {
      e.preventDefault();
      openSpotlightSearch();
      return;
    }
  }
  // Shift+Q / Shift+W 进入快筛（桌面=四格会话）：组合键避开浏览器与既有单键体系；输入态已被上面拦下
  if (e.shiftKey && !e.repeat && (e.key === 'Q' || e.key === 'q' || e.key === 'W' || e.key === 'w')) {
    startQuickTriage();
    return;
  }
  // Shift+F 呼出筛选面板：单键 F 仍归详情/大图的「保留」
  if (e.shiftKey && !e.repeat && (e.key === 'F' || e.key === 'f')) {
    e.preventDefault();
    toggleViewPanel();
    return;
  }
  // 大图开着时筛选拾权一律让位：回看/四格都不吃键（否则大图底下会盲标记），↓↑ 交给大图滚动
  if (!$('#detail').hidden && review.active() && $('#lightbox').hidden
      && !/^[1-4]$/.test(e.key)) { review.key(e); return; }   // 1-4 放行：与普通详情一致，收会话切视图
  if (!$('#slideshow').hidden && slideshow.active()) { slideshow.key(e); return; }   // 放映同理
  if (!$('#quad').hidden && quad.active() && $('#lightbox').hidden) { quad.key(e); return; }   // 四格同理；大图开着时让位大图
  if (!S.selectMode && /^[1-4]$/.test(e.key)) {
    const views = { '1':'kept', '2':'discarded', '3':'favorites', '4':'all' };
    switchStatusView(views[e.key]);
    return;
  }
  const lb = $('#lightbox');
  if (!lb.hidden) {
    if (e.key === 'ArrowLeft') lbNav(-1);
    else if (e.key === 'ArrowRight') lbNav(1);
    else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      // ↑↓ = 浏览：长图档上下滚动阅读（普通图整图已可见，无滚动面则空操作）
      e.preventDefault();
      const stage = $('.lb-stage');
      if (stage.classList.contains('lb-long'))
        stage.scrollBy({ top: e.key === 'ArrowDown' ? stage.clientHeight * 0.8 : -stage.clientHeight * 0.8,
          behavior: e.repeat ? 'auto' : 'smooth' });
    }
    else if (e.key === ' ') { e.preventDefault(); closeLB(); }   // 空格即退出：开图后焦点常落在翻页钮上，不豁免按钮
    else if (e.key === 'l' || e.key === 'L') openLinkForSid(S.images[lightbox.getIndex()]?.p.sid);
    else if (e.key === 'Tab') trapTab(e, lb);
    return;
  }
  const dt = $('#detail');
  if (!dt.hidden) {
    if (e.key === 'ArrowUp') { e.preventDefault(); detailMarkAdvance('kept'); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); detailMarkAdvance('discarded'); }
    else if (e.key === 'PageDown') { e.preventDefault(); detailNav(1); }
    else if (e.key === 'PageUp') { e.preventDefault(); detailNav(-1); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      // 单击切本帖图（边界钳停）；350ms 内同向再击=翻帖。repeat 连发只切图不计双击，
      // 连续翻帖交给 PageUp/PageDown
      e.preventDefault();
      const dir = e.key === 'ArrowRight' ? 1 : -1;
      const now = performance.now();
      const dbl = !e.repeat && dtArrow.key === e.key && now - dtArrow.t < 350;
      dtArrow = dbl || e.repeat ? {key: null, t: 0} : {key: e.key, t: now};
      if (dbl) { detailNav(dir); }
      else {
        const card = $('#detail .detail-card.is-front');
        if (card && card.querySelectorAll('.dt-pics figure').length > 1)
          showStageImage(card, (+card.dataset.img || 0) + dir);
      }
    }
    else if (e.key === 'f' || e.key === 'F') detailMarkAdvance('kept');
    else if (e.key === 'd' || e.key === 'D') detailMarkAdvance('discarded');
    else if (!e.metaKey && !e.ctrlKey && !e.altKey &&
             (e.key === '+' || e.key === '=')) { e.preventDefault(); detailCountDelta(1); }
    else if (!e.metaKey && !e.ctrlKey && !e.altKey &&
             (e.key === '-' || e.key === '_')) { e.preventDefault(); detailCountDelta(-1); }
    else if (e.key === 'm' || e.key === 'M') toggleFavorite(detailSid).then(refreshDetail);
    else if (e.key === 'l' || e.key === 'L') openLinkForSid(detailSid);
    else if (e.key === 't' || e.key === 'T') {
      const inp = $('#detail .detail-card.is-front .dt-cinput');
      if (inp) { e.preventDefault(); inp.focus(); }
    }
    else if (e.key === ' ') {
      // 详情空格：开当前正在看的那张图（圆点/箭头切到第几张就开哪张，不再固定首图；
      // 盖在详情之上，Esc 先退大图再退详情；大图内 ←→ 翻本帖图）
      e.preventDefault();
      const p = S.posts.find((x) => x.sid === detailSid);
      const card = $('#detail .detail-card.is-front');
      const pc = p?.pics?.[Math.max(0, +card?.dataset.img || 0)] || p?.pics?.[0];
      if (pc) {
        const i = S.images.findIndex((im) => im.p.sid === detailSid && im.pc.file === pc.file);
        if (i >= 0) openLB(i, detailSid);
        else toast('该图不在当前筛选列表里，可用下载按钮获取当前图片');
      }
    }
    else if (e.key === 'Tab') trapTab(e, dt);
    return;
  }
  // 卡片流空格：看当前卡的大图（大图里再按空格即退出）。固定语义不豁免按钮焦点——
  // 否则点过任意按钮后空格会变成「激活按钮」，大图打不开；按钮的键盘激活走 Enter。
  // 输入态已被 editing 拦下，多选模式点选优先也不接管
  if (e.key === ' ' && !S.selectMode) {
    e.preventDefault();
    const card = currentCard();
    const i = card ? S.images.findIndex((im) => im.p.sid === card.dataset.sid) : -1;
    if (i >= 0) openLB(i, card.dataset.sid);
  }
});

/* ---------- 详情页（XHS 式双栏单卡：后卡退居隐藏预载，标记后幽灵飞出、类名互换即刻就位） ---------- */

let detailSid = null;        // 前卡 sid
let detailNextSid = null;    // 次位卡 sid
let dtArrow = {key: null, t: 0};   // ←→ 双击翻帖判定：上一次方向键与时刻（帖子一切换即作废）
let triageKept = 0, triageDropped = 0;   // 快筛会话计数（悬浮球入口清零，完成 toast 汇报）

// 快筛进度 pill：详情开着且在新增视图时显示「剩余 N」（N=当前筛选待处理数，标记即减）
function renderTriagePill() {
  const pill = $('#triage-progress');
  const show = !$('#detail').hidden && S.status === 'pending' && !review.active() && !S.readOnly;
  pill.hidden = !show;
  if (show) pill.textContent = `剩余 ${S.total}`;
}

function detailCards() {
  const belt = $('#detail .detail-belt');
  return {
    front: belt.querySelector('.detail-card.is-front'),
    back: belt.querySelector('.detail-card.is-back'),
  };
}

function postInView(x) {
  // favorites 视图里帖子带的是真实三态，整列都是收藏；其余视图按状态过滤
  return S.status === 'all' || S.status === 'favorites' || x.status === S.status;
}

function nextInView(fromIdx) {
  return S.posts.slice(fromIdx + 1).find(postInView) || null;
}

function openLinkForSid(sid) {
  const p = S.posts.find((x) => x.sid === sid);
  const url = safeUrl(p?.weibo_url);
  if (url === '#') { toast('原帖链接不可用'); return; }
  window.open(url, '_blank', 'noopener,noreferrer');
}

let detailRequestVersion = 0;
/* 保留帖的状态依赖键位（F=+1、D=−1、长按「−」删除）只在 title 里——首次见到
   保留帖时 toast 说一次，长按删除不再纯靠偶然发现 */
function keptHintOnce(p) {
  if (S.readOnly || p?.status !== 'kept') return;
  try {
    if (localStorage.getItem('pw_kepthint')) return;
    localStorage.setItem('pw_kepthint', '1');
  } catch (e) { /* 存不了就本次会话不再弹 */ }
  toast('保留帖：F/+ 点赞+1 · D/− 点赞−1 · 长按「−」删除');
}
async function openDetail(sid, focusComments = false) {
  const version = ++detailRequestVersion;
  try {
    const r = await api(`/api/posts/${sid}`);
    if (version !== detailRequestVersion) return;
    renderStackAt(r.post);
    keptHintOnce(r.post);
    resetDetailCard();
    cancelHide($('#detail'));
    $('#detail').hidden = false;
    document.body.classList.add('has-detail');
    $('#detail').scrollTop = 0;
    document.body.style.overflow = 'hidden';
    $('#detail .detail-card.is-front .dt-scroll').scrollTop = 0;
    renderTriagePill();
    // 从墙卡评论角标进来直达评论区；否则焦点落前卡（桌面关闭键已退役，Esc/点卡外关闭）
    const focusEl = focusComments
      ? $('#detail .detail-card.is-front .dt-cinput')
      : $('#detail .detail-card.is-front');
    focusEl.focus({preventScroll:true});
  } catch (e) { toast(e.message); }
}

function closeDetail() {
  if (review.active()) { review.close(); return; }
  ++detailRequestVersion;
  hideQuoteChip();
  $('#triage-progress').hidden = true;
  document.body.classList.remove('has-detail');
  animatedHide($('#detail'));
  detailSid = null;
  detailNextSid = null;
  // 大图还开着（含正在 140ms 淡出）时不能解锁页面滚动，否则背景会跟着滚
  const lb = $('#lightbox');
  if (lb.hidden || lb.classList.contains('closing')) document.body.style.overflow = '';
}

async function refreshDetail() {
  if (!detailSid || $('#detail').hidden) return;
  try {
    const sid = detailSid;
    const r = await api(`/api/posts/${sid}`);
    if (detailSid !== sid || $('#detail').hidden) return;
    const { front } = detailCards();
    if (front && front.classList.contains('is-front')) renderDetailInto(front, r.post, true);
  } catch (e) {
    if (e && e.status === 404) closeDetail();
    else toast('刷新详情失败：' + e.message);
  }
}

// 渲染整带：前卡用传入数据；次位卡从列表缓存取「下一张」，图 eager 立即加载——
// 用户看前卡的整段时间就是次位卡的加载窗口，换位时不再有网络等待。
// 次位卡常态 display:none（.is-back），但 DOM 和预载照常（隐形提速，逻辑不变）
function renderStackAt(frontPost) {
  detailSid = frontPost.sid;
  dtArrow = {key: null, t: 0};   // 新帖从头看：←→ 双击判定作废
  const { front, back } = detailCards();
  front.hidden = false;   // 上一次「到最后一张」可能把前卡置过 hidden
  renderDetailInto(front, frontPost, true);
  const i = S.posts.findIndex((x) => x.sid === frontPost.sid);
  const nxt = i >= 0 ? nextInView(i) : null;
  detailNextSid = nxt ? nxt.sid : null;
  if (nxt) { back.hidden = false; renderDetailInto(back, nxt, true); }
  else back.hidden = true;
}

function renderDetailInto(card, p, eager) {
  card.querySelector('.dt-avatar').innerHTML = avatarButton(p);
  card.querySelector('.dt-author').textContent = p.author;
  // 转发溯源：墙卡不再标 原创/转发，转发者在这里一跳可达
  const via = card.querySelector('.dt-via');
  if (p.via) { via.hidden = false; via.textContent = `@${p.via} 转发`; }
  else { via.hidden = true; via.textContent = ''; }
  // 时间旁的次数小标签（保留态常显，含建账的 ×1——±1 动作要有可见计数）
  const kn = card.querySelector('.dt-keepn');
  if (p.status === 'kept') {
    const kb = keepBadge(p.keep_count || 1);
    kn.hidden = false;
    kn.className = `dt-keepn chip keepn t${kb.tier}`;
    kn.textContent = kb.text;
    kn.title = kb.title;
  } else { kn.hidden = true; kn.textContent = ''; kn.title = ''; }
  card.querySelector('.dt-time').innerHTML =
    `${fmtDate(p.created_at)}<span class="hm"> ${fmtHM(p.created_at)}</span>`;
  card.querySelector('.dt-text').textContent = (p.text || '') +
    (p.text_complete === false ? '\n\n当前文本可能不完整，可打开原博查看全文。' : '');
  // 图区 = 左栏舞台：全部 figure 常驻（预载照常），非当前张隐藏；多图时圆点 + 悬停箭头切换。
  // ←→ 仍是翻帖（快筛节奏优先），图内换图只走圆点/箭头。视频 figure 用 <video>（poster=海报）
  const n = (p.pics || []).length;
  const dp = card.querySelector('.dt-pics');
  dp.className = 'dt-pics';
  dp.innerHTML = (p.pics || []).map((pc, i) => {
    const media = pc.kind === 'video'
      ? `<video class="dt-video" controls playsinline preload="metadata"
           poster="/media/${esc(pc.thumb || '')}" src="/media/${esc(pc.file)}"></video>`
      : `<img src="/media/${esc(pc.thumb)}" alt="${esc(p.author)} 的微博图" loading="${eager ? 'eager' : 'lazy'}" decoding="async" data-file="${esc(pc.file)}">`;
    return `<figure${i ? ' hidden' : ''} data-i="${i}">
      ${media}
      <figcaption><a href="/media/${esc(pc.file)}?dl=1" download title="${esc(downloadLabel(pc).title)}" aria-label="${esc(downloadLabel(pc).title)}">${ICONS.download}</a></figcaption>
    </figure>`;
  }).join('');
  dp.querySelectorAll('img').forEach((img) => img.onclick = () => {
    const i = S.images.findIndex((im) => im.pc.file === img.dataset.file);
    if (i >= 0) openLB(i, p.sid);   // 单卡范围：大图里只能看当前卡片这张帖的图
    else toast('该图不在当前筛选列表里，可用下载按钮获取当前图片');
  });
  const dots = card.querySelector('.dt-dots');
  if (n > 1) {
    dots.hidden = false;
    dots.innerHTML = p.pics.map((_, i) =>
      `<button type="button" class="dt-dot${i ? '' : ' on'}" aria-label="第 ${i + 1} 张" aria-current="${i ? 'false' : 'true'}"></button>`).join('');
  } else { dots.hidden = true; dots.innerHTML = ''; }
  card.querySelector('.dt-ig.prev').hidden = card.querySelector('.dt-ig.next').hidden = n <= 1;
  card.dataset.img = '0';
  const wb = card.querySelector('.dt-weibo');
  wb.href = safeUrl(p.weibo_url);
  wb.innerHTML = ICONS.external;
  // 保留/不要作为详情工具栏图标：保留态换装为点赞 +1/−1（−1 长按=真删除，绑定见底栏事件），
  // 其余状态维持快筛语义（保留/不要）；状态用颜色与 aria-pressed 表达
  const kept = p.status === 'kept';
  const kp = card.querySelector('.dtb.keep'), dpb = card.querySelector('.dtb.drop'), fp = card.querySelector('.dt-fav');
  kp.className = 'dtb keep' + (kept ? ' on' : '');
  kp.innerHTML = kept ? ICONS.plus : ICONS.check;
  kp.title = kept ? '点赞 +1 (F / ↑ / +)' : '保留 (F / ↑)';
  kp.setAttribute('aria-label', kp.title);
  kp.setAttribute('aria-keyshortcuts', 'F ArrowUp +');
  kp.setAttribute('aria-pressed', String(kept));
  dpb.className = 'dtb drop' + (p.status === 'discarded' ? ' on' : '');
  dpb.innerHTML = kept ? ICONS.minus : ICONS.circleX;
  dpb.title = kept ? '点赞 −1 (D / ↓ / -) · 长按删除' : '不要 (D / ↓)';
  dpb.setAttribute('aria-label', dpb.title);
  dpb.setAttribute('aria-keyshortcuts', 'D ArrowDown -');
  dpb.setAttribute('aria-pressed', String(p.status === 'discarded'));
  fp.className = 'dt-fav fav' + (p.favorite ? ' on' : '');   // 保住钩子类：卡节点是常驻复用的
  fp.innerHTML = ICONS.star;
  fp.setAttribute('aria-pressed', String(!!p.favorite));
  fp.title = p.favorite ? '取消收藏 (M)' : '收藏 (M)';
  fp.setAttribute('aria-label', fp.title);
  fp.setAttribute('aria-keyshortcuts', 'M');
  wb.title = '打开原帖 (L)';
  wb.setAttribute('aria-keyshortcuts', 'L');
  renderDetailTags(card, p);
  renderDetailComments(card, p.comments || []);
  hideQuoteChip();
}

/* 详情标签区：chip + ✕ 移除 + ＋ 新增（行内输入，Enter 提交、Esc 取消、失焦兜底）。
   乐观写走 toggleTag；服务端拒绝（空名/超长/超上限）toast 回滚。
   详情卡的 p 可能来自详情端点或 payload 缓存，两路都已带 tags */
function renderDetailTags(card, p) {
  const box = card.querySelector('.dt-tags');
  if (!box) return;
  const tags = p.tags || [];
  const ro = S.readOnly;
  box.innerHTML = tags.map((t) =>
    `<span class="dt-tag">${esc(t)}${ro ? '' :
      `<button class="dt-tag-x" data-tag="${esc(t)}" title="移除标签"
         aria-label="移除标签 ${esc(t)}">✕</button>`}</span>`).join('')
    + (ro ? '' : '<button class="dt-tag-add" title="添加标签（每帖最多 10 个）">＋ 标签</button>');
  box.querySelectorAll('.dt-tag-x').forEach((b) => b.onclick = () => {
    toggleTag(p.sid, [], [b.dataset.tag]).then((r) => { if (r) refreshTags(); });
  });
  const add = box.querySelector('.dt-tag-add');
  if (!add) return;
  add.onclick = () => {
    const inp = document.createElement('input');
    inp.className = 'dt-tag-input';
    inp.maxLength = 24;
    inp.placeholder = '标签名，回车确认';
    inp.setAttribute('aria-label', '添加标签');
    add.replaceWith(inp);
    inp.focus();
    let done = false;   // Enter/blur 双触发防抖：提交过一次就不再提交
    const commit = async () => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      if (!v) { renderDetailTags(card, p); if (document.activeElement === document.body) card.querySelector('.dt-tag-add')?.focus(); return; }   // 空值 = 取消，回原样
      const r = await toggleTag(p.sid, [v], []);
      if (r) refreshTags();
    };
    inp.addEventListener('keydown', (e) => {
      e.stopPropagation();   // 输入键不进全局快捷键；Esc 只收输入框不关详情
      if (e.isComposing || e.keyCode === 229) return;   // IME 组词中：回车只确认候选词，不提交
      if (e.key === 'Enter') commit();
      else if (e.key === 'Escape') { done = true; renderDetailTags(card, p); card.querySelector('.dt-tag-add')?.focus(); }
    });
    inp.addEventListener('blur', () => commit());
  };
}

/* ---------- 详情评论区：旧→新时间线，写/改/删全在右栏 ---------- */

function fmtCommentTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  const y = d.getFullYear() === new Date().getFullYear() ? '' : `${d.getFullYear()}-`;
  return `${y}${p(d.getMonth() + 1)}-${p(d.getDate())} ${fmtHM(iso)}`;
}

function commentEra(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '更早';
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = (day(new Date()) - day(d)) / 86400000;
  if (diff <= 0) return '今天';
  if (diff < 7) return '七天内';
  return '更早';
}

// "> 引用行\n正文" 的纯文本协议 → 引用块渲染；编辑时原样可见；搜索态下命中词高亮
function commentBodyHTML(body, term = '') {
  const lines = String(body).split('\n');
  let i = 0;
  const quote = [];
  while (i < lines.length && lines[i].startsWith('> ')) { quote.push(lines[i].slice(2)); i += 1; }
  const rest = lines.slice(i).join('\n');
  const q = quote.length ? `<span class="c-quote">${markTerm(quote.join('\n'), term, S.searchScope)}</span>` : '';
  return q + (rest ? `<span class="c-text">${markTerm(rest, term, S.searchScope)}</span>` : '');
}

function commentRowHTML(c, term = '') {
  return `<p class="c-body">${commentBodyHTML(c.body, term)}</p>
    <div class="c-meta"><time class="c-time">${fmtCommentTime(c.created_at)}</time>
      <span class="c-acts"><button class="c-edit">编辑</button><button class="c-del">删除</button></span></div>`;
}

function renderDetailComments(card, comments, term = (S.searchText || '').trim()) {
  const list = card.querySelector('.dt-comment-list');
  const head = card.querySelector('.dt-comments-head');
  const empty = card.querySelector('.dt-comment-empty');
  card._comments = comments;   // 写/删后整体重渲的单一数据源
  const eras = [...new Set(comments.map((c) => commentEra(c.created_at)))];
  let era = '';
  list.innerHTML = comments.map((c) => {
    const e = commentEra(c.created_at);
    const h = eras.length > 1 && e !== era ? `<div class="dt-era">${esc(e)}</div>` : '';
    era = e;
    return `${h}<div class="dt-comment" data-cid="${esc(c.id)}">${commentRowHTML(c, term)}</div>`;
  }).join('');
  list.querySelector('.dt-comment:last-child')?.classList.add('now');   // 时间轴的「你在这里」
  head.textContent = `${comments.length} 条评论`;
  head.hidden = comments.length === 0;
  empty.hidden = comments.length > 0;
  list.querySelectorAll('.dt-comment').forEach((row) => bindCommentRow(card, row));
  if (term) {   // 搜索态：滚到第一条命中的批注
    const first = [...list.querySelectorAll('.dt-comment')].find((r) => r.querySelector('mark'));
    first?.scrollIntoView({ block: 'center' });
  }
}

function bindCommentRow(card, row) {
  const cid = Number(row.dataset.cid);
  // 两步删除：第一击武装成「确认删除」（3 秒不点自动复原），第二击才真删
  row.querySelector('.c-del').onclick = async () => {
    const btn = row.querySelector('.c-del');
    if (!btn.classList.contains('arming')) {
      btn.classList.add('arming');
      btn.textContent = '确认删除';
      row.classList.add('arming');   // 武装期悬停态消失也保持可见
      setTimeout(() => {
        btn.classList.remove('arming');
        btn.textContent = '删除';
        row.classList.remove('arming');
      }, 3000);
      return;
    }
    if (btn.disabled) return;   // 请求中防重复提交
    btn.disabled = true;
    try {
      await api(`/api/posts/${detailSid}/comments/${cid}`, 'DELETE');
      card._comments = (card._comments || []).filter((c) => c.id !== cid);
      renderDetailComments(card, card._comments);
      syncCommentCount(card);
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  };
  row.querySelector('.c-edit').onclick = () => {
    const bodyEl = row.querySelector('.c-body');
    if (row.querySelector('.c-editbox')) return;
    const original = bodyEl.textContent;
    const ta = document.createElement('textarea');
    ta.className = 'c-editbox';
    ta.maxLength = 500;
    ta.value = original;
    bodyEl.replaceWith(ta);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;   // replaceWith 会连带 blur，守卫防二次执行
      const text = ta.value.trim();
      if (!save || !text || text === original) { bodyEl.textContent = original; ta.replaceWith(bodyEl); return; }
      try {
        const r = await api(`/api/posts/${detailSid}/comments/${cid}`, 'PUT', { body: text });
        bodyEl.textContent = r.comment.body;
        const cc = (card._comments || []).find((c) => c.id === cid);
        if (cc) cc.body = r.comment.body;   // 保持重渲数据源同步，防后续删除/追加减回旧文
      } catch (e) { toast(e.message); bodyEl.textContent = original; }
      ta.replaceWith(bodyEl);
    };
    ta.addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;   // IME 组词中：回车只确认候选词，不结束编辑
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.stopPropagation(); finish(false); }   // 取消，不让全局 Esc 关浮层
    });
    ta.addEventListener('blur', () => finish(true));
  };
}

async function sendComment(card) {
  if (!detailSid || !card.classList.contains('is-front')) return;
  const input = card.querySelector('.dt-cinput');
  const text = input.value.trim();
  if (!text) { input.focus(); return; }
  const send = card.querySelector('.dt-csend');
  send.disabled = true;
  try {
    const r = await api(`/api/posts/${detailSid}/comments`, 'POST', { body: text });
    card._comments = [...(card._comments || []), r.comment];
    renderDetailComments(card, card._comments);
    syncCommentCount(card);
    input.value = '';
    input.style.height = '';
    input.focus();
    card.querySelector('.dt-scroll').scrollTop = card.querySelector('.dt-scroll').scrollHeight;
  } catch (e) { toast(e.message); }
  finally { send.disabled = false; }
}

function syncCommentCount(card) {
  // 计数以 card._comments（重渲数据源）为准，头部/空态统一由这里收口
  const head = card.querySelector('.dt-comments-head');
  const n = (card._comments || []).length;
  head.textContent = `${n} 条评论`;
  head.hidden = n === 0;
  card.querySelector('.dt-comment-empty').hidden = n > 0;
  const p = S.posts.find((x) => x.sid === detailSid);
  if (p) p.comment_count = n;
}

// 图内换图：显示第 i 张（0 基），同步圆点；范围钳制
function showStageImage(card, i) {
  const figs = card.querySelectorAll('.dt-pics figure');
  if (!figs.length) return;
  const cur = Math.max(0, Math.min(i, figs.length - 1));
  figs.forEach((f, k) => { f.hidden = k !== cur; });
  card.dataset.img = String(cur);
  card.querySelectorAll('.dt-dot').forEach((d, k) => {
    d.classList.toggle('on', k === cur);
    d.setAttribute('aria-current', String(k === cur));
  });
}

/* ---------- 引用批注：选中正文一段 → 浮出「引用批注」→ 输入框带出 > 引用块 ---------- */
let quoteChip = null, quoteText = '', quoteSelRaf = 0;
function hideQuoteChip() {
  quoteChip?.remove();
  quoteChip = null;
  quoteText = '';
}
function syncQuoteChip() {
  quoteSelRaf = 0;
  if (S.readOnly || $('#detail').hidden) { hideQuoteChip(); return; }
  const sel = document.getSelection();
  const textEl = $('#detail .detail-card.is-front .dt-text');
  const text = sel && !sel.isCollapsed && sel.rangeCount && textEl?.contains(sel.anchorNode)
    ? sel.toString().trim() : '';
  if (!text || text.length > 200) { hideQuoteChip(); return; }
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  hideQuoteChip();
  quoteText = text;
  quoteChip = document.createElement('button');
  quoteChip.className = 'quote-chip';
  quoteChip.textContent = '引用批注';
  quoteChip.style.left = `${Math.min(Math.max(rect.left, 12), window.innerWidth - 96)}px`;
  quoteChip.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - 44)}px`;
  quoteChip.addEventListener('mousedown', (e) => e.preventDefault());   // 保住选区不被点没
  quoteChip.addEventListener('click', () => {
    const input = $('#detail .detail-card.is-front .dt-cinput');
    if (input) {
      input.value = `> ${quoteText}\n`;
      input.dispatchEvent(new Event('input'));   // 触发输入框高度自适应
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
    hideQuoteChip();
  });
  document.body.appendChild(quoteChip);
}
document.addEventListener('selectionchange', () => {
  if (S.readOnly || $('#detail').hidden) { hideQuoteChip(); return; }
  if (quoteSelRaf) return;
  quoteSelRaf = typeof requestAnimationFrame === 'function'
    ? requestAnimationFrame(syncQuoteChip)
    : setTimeout(syncQuoteChip, 16);
});

// 两个卡节点常驻（角色互换复用），事件只绑一次；后卡 inert 不可达，绑定无害
document.querySelectorAll('#detail .detail-card').forEach((card) => {
  card.querySelector('.dt-close').innerHTML = ICONS.x;
  card.querySelector('.dt-close').onclick = closeDetail;
  // 详情头像同款入口：先收浮层再进博主视图（回看态复用同一关闭路径）
  card.querySelector('.dt-avatar').addEventListener('click', (e) => {
    const b = e.target.closest('.av-link');
    if (!b || !detailSid) return;
    const p = S.posts.find((q) => q.sid === detailSid);
    const uid = b.dataset.uid;
    closeDetail();
    authorView.open(uid, p);
  });
  // 与键盘 ↑↓ 同一反馈：快筛语义标记后自动进下一张；保留态（±1）不前进
  card.querySelector('.dtb.keep').onclick = () => { if (detailSid) detailMarkAdvance('kept'); };
  // 保留态的「−1」长按 600ms = 真删除（进快筛前进流）；短击仍是 −1，长按后的合成 click 就地吞掉
  const dropBtn = card.querySelector('.dtb.drop');
  let lpTimer = 0, lpFired = false;
  const cancelLP = () => { clearTimeout(lpTimer); lpTimer = 0; };
  dropBtn.addEventListener('pointerdown', () => {
    const p = S.posts.find((x) => x.sid === detailSid);
    if (!p || p.status !== 'kept' || review.active() || S.readOnly) return;
    cancelLP();
    lpTimer = setTimeout(() => { lpTimer = 0; lpFired = true; detailAdvance('discarded'); }, 600);
  });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach((t) => dropBtn.addEventListener(t, cancelLP));
  dropBtn.addEventListener('contextmenu', (e) => e.preventDefault());
  dropBtn.onclick = () => {
    if (lpFired) { lpFired = false; return; }
    if (detailSid) detailMarkAdvance('discarded');
  };
  card.querySelector('.dt-fav').onclick = () => { if (detailSid) toggleFavorite(detailSid).then(refreshDetail); };
  // 图内切换：箭头按当前 index 步进，圆点直接跳（delegation，dots 每次 render 重建）
  card.querySelector('.dt-ig.prev').onclick = () => showStageImage(card, (+card.dataset.img || 0) - 1);
  card.querySelector('.dt-ig.next').onclick = () => showStageImage(card, (+card.dataset.img || 0) + 1);
  card.querySelector('.dt-dots').addEventListener('click', (e) => {
    const d = e.target.closest('.dt-dot');
    if (d) showStageImage(card, [...d.parentNode.children].indexOf(d));
  });
  // 评论输入：Enter 发送、Shift+Enter 换行、高度自适应（上限约四行后内滚）
  const input = card.querySelector('.dt-cinput');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 96)}px`;
  });
  input.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;   // IME 组词中：回车只确认候选词，不发送
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendComment(card); }
  });
  card.querySelector('.dt-csend').onclick = () => sendComment(card);
  card.querySelector('.dt-scroll').addEventListener('scroll', hideQuoteChip, { passive: true });
});
$('#detail').addEventListener('click', (e) => {
  if (e.target === $('#detail')) closeDetail();
});

/* ---------- 详情页快筛：F/↑ 保留，D/↓ 不要，左右浏览 ---------- */

function resetDetailCard(card) {
  const c = card || $('#detail .detail-card.is-front');
  if (!c) return;
  c.style.transition = '';
  c.style.transform = '';
  c.style.opacity = '';
  c.classList.remove('sw-keep', 'sw-drop', 'dragging');
  c.querySelector('.flash-veil.keep').style.opacity = '0';
  c.querySelector('.flash-veil.drop').style.opacity = '0';
}

async function detailNav(d) {
  if (review.active()) { review.nav(d); return; }
  const i = S.posts.findIndex((x) => x.sid === detailSid);
  if (i < 0) { toast('该帖不在当前列表里'); return; }
  const j = i + d;
  if (j < 0) { toast('已经是第一张'); return; }
  if (j >= S.posts.length) {
    // 已翻完已加载部分：原地补载下一页后继续，不再把人赶出详情页
    if (S.hasMore) {
      await load(false);
      if (S.posts[j]) { openDetail(S.posts[j].sid); return; }
    }
    toast('已经是最后一张');
    return;
  }
  openDetail(S.posts[j].sid);
}

// 幽灵飞出：克隆前卡原地飞走（克隆的图全在缓存、瞬时呈现），真身不动画、立即转角色。
// 每次标记各自产生幽灵，连划互不干扰。
function flyOutGhost(card, status) {
  const g = card.cloneNode(true);
  g.classList.remove('is-front', 'is-back');
  g.classList.add('is-ghost', status === 'kept' ? 'fly-keep' : 'fly-drop');
  g.setAttribute('inert', '');
  g.setAttribute('aria-hidden', 'true');
  g.style.width = card.offsetWidth + 'px';    // 钉在前卡原位（detail-belt 左上角）
  g.style.height = card.offsetHeight + 'px';
  card.parentNode.appendChild(g);
  setTimeout(() => g.remove(), 260);
}

/* 详情动作分发：回看走回看；保留态 = 次数 ±1（不前进，真删除走 −1 长按的 detailAdvance）；
   其余为快筛语义：标记并自动进下一张 */
function detailCountDelta(delta) {
  // +/- 纯点赞键：只调整点赞次数（非保留帖不动作，保留/不要请用 F/D）；
  // 回看帖全是保留帖：+/- 与普通同一语义 ±1 不前进。修饰键组合（Ctrl+加号=页面缩放等）不接管。
  if (review.active()) { review.reaffirm(delta); return; }
  const p = S.posts.find((x) => x.sid === detailSid);
  if (!p || p.status !== 'kept') return;
  if (S.readOnly) return;
  dtArrow = {key: null, t: 0};
  const front = detailCards().front;
  const imgIdx = +front.dataset.img || 0;
  reaffirm(detailSid, delta).then((r) => {
    if (r) {
      renderDetailInto(front, r, false);
      showStageImage(front, Math.max(0, Math.min(imgIdx, (r.pics || []).length - 1)));
    }
    resetDetailCard();   // 不前进：就地回弹
  });
}

async function detailMarkAdvance(status) {
  if (review.active()) {
    review.reaffirm(status === 'kept' ? 1 : -1);   // 回看帖全是保留帖：与普通保留帖同语义 ±1 不前进
    resetDetailCard();
    return;
  }
  if (S.readOnly) return;
  const p = S.posts.find((x) => x.sid === detailSid);
  if (p && p.status === 'kept') {
    dtArrow = {key: null, t: 0};
    const front = detailCards().front;
    const imgIdx = +front.dataset.img || 0;
    const r = await reaffirm(detailSid, status === 'kept' ? 1 : -1);
    if (r) {
      renderDetailInto(front, r, false);
      showStageImage(front, Math.max(0, Math.min(imgIdx, (r.pics || []).length - 1)));   // ±1 不打断正在看的图
    }
    resetDetailCard();   // 未前进：拖拽态就地回弹
    return;
  }
  return detailAdvance(status);
}

function detailMatchesPost(a, b) {
  if (!a || !b) return false;
  if (a.text !== b.text || a.text_complete !== b.text_complete || a.via !== b.via
      || a.status !== b.status || (a.keep_count || 1) !== (b.keep_count || 1)
      || Boolean(a.favorite) !== Boolean(b.favorite) || a.author !== b.author
      || a.avatar !== b.avatar) return false;
  const ap = a.pics || [], bp = b.pics || [];
  if (ap.length !== bp.length) return false;
  for (let i = 0; i < ap.length; i += 1)
    if (ap[i].file !== bp[i].file || ap[i].thumb !== bp[i].thumb) return false;
  const at = a.tags || [], bt = b.tags || [];
  if (at.length !== bt.length || at.some((t, i) => t !== bt[i])) return false;
  const ac = a.comments || [], bc = b.comments || [];
  if (ac.length !== bc.length) return false;
  for (let i = 0; i < ac.length; i += 1)
    if (ac[i].id !== bc[i].id || ac[i].body !== bc[i].body) return false;
  return true;
}

async function detailAdvance(status) {
  dtArrow = {key: null, t: 0};   // 标记即换帖：←→ 双击判定作废
  const i = S.posts.findIndex((x) => x.sid === detailSid);
  if (i < 0) return;
  const { front, back } = detailCards();
  // 前进目标在摘除发生前先定好：当前视图内、它之后的第一张。
  // 若等 mark 异步摘除后再取，会取到刚标过的自己——最后一帖就永远停在原地。
  const upNext = nextInView(i);
  const pr = mark(detailSid, status, { fromLB: true });
  if (status === 'kept') triageKept += 1; else triageDropped += 1;   // 会话计数；6 秒撤销不回减（完成 toast 口径为近似值）
  renderTriagePill();
  if (upNext) {
    // 后卡漂移校验（撤销重插/翻页/外部刷新会让缓存位次变化）：不对就先补渲染，
    // 退化为「缓存版先上」的旧行为，只是少见路径
    if (detailNextSid !== upNext.sid) {
      renderDetailInto(back, upNext, true);
      detailNextSid = upNext.sid;
    }
    back.hidden = false;
    flyOutGhost(front, status);
    // 角色互换：次位卡常态 display:none（.is-back 全局隐藏，仅承担预载），
    // 换装即就位，过渡感由幽灵飞出承担——eager 预载照常
    detailSid = upNext.sid;
    back.classList.remove('is-back');
    back.classList.add('is-front');
    back.removeAttribute('inert');
    back.removeAttribute('aria-hidden');
    resetDetailCard(front);
    front.classList.remove('is-front');
    front.classList.add('is-back');
    front.setAttribute('inert', '');
    front.setAttribute('aria-hidden', 'true');
    $('#detail').scrollTop = 0;
    back.querySelector('.dt-scroll').scrollTop = 0;   // 新前卡从顶部读起（评论区也是）
    // 焦点跟随：被降级的卡若带着焦点进了 inert，浏览器会把焦点踢到 body——拉回新前卡
    if (document.activeElement === document.body || front.contains(document.activeElement)) {
      back.focus({preventScroll:true});
    }
    // 补队尾：装「新的下一张」，eager 预载（看新前卡的整段时间就是它的加载窗口）
    const ni = S.posts.findIndex((x) => x.sid === detailSid);
    const nn = ni >= 0 ? nextInView(ni) : null;
    detailNextSid = nn ? nn.sid : null;
    if (nn) { front.hidden = false; renderDetailInto(front, nn, true); }
    else front.hidden = true;
    try {
      const r = await api(`/api/posts/${detailSid}`);
      const { front: f2 } = detailCards();
      if (detailSid === upNext.sid && f2 === back && r?.post && !detailMatchesPost(upNext, r.post)) {
        const prevImg = +f2.dataset.img || 0;
        Object.assign(upNext, r.post);
        renderDetailInto(f2, upNext, true);
        if (prevImg > 0) showStageImage(f2, prevImg);
      }
    } catch (e) { /* 缓存版已在，静默 */ }
    return;
  }
  // 它是本视图最后一张：等摘除落定，服务器还有待处理存货就续一批继续刷
  if (pr?.settled) { try { await pr.settled; } catch { return; } }
  if (S.hasMore) {
    // 服务端还有存货：续一批继续刷。除 all 外各视图整列重取对齐游标；all 不摘卡，
    // 追加下一页后从当前位置继续找
    if (S.status === 'all') await load(false);
    else await load(true);
    const i2 = S.posts.findIndex((x) => x.sid === detailSid);
    const nxt = S.status === 'all' ? nextInView(i2 >= 0 ? i2 : -1)
                                   : S.posts.find(postInView);
    if (nxt) {
      renderStackAt(nxt);
      resetDetailCard();
      $('#detail').scrollTop = 0;
      return;
    }
  }
  closeDetail();
  const undo = getLastDrop();
  const stats = (triageKept || triageDropped) ? ` 保留 ${triageKept} · 不要 ${triageDropped}` : '';
  toast(`快筛完成：这一栏都处理完了 ✓${stats}`, undo ? {label:'撤销',fn:() => {
    if (undo?.target === 'kept' && triageKept) triageKept -= 1;       // 完成时刻的撤销回减计数
    if (undo?.target === 'discarded' && triageDropped) triageDropped -= 1;
    undoLastMark(undo);
  }} : undefined);
  triageKept = 0; triageDropped = 0;   // 会话清账，下一轮从零起
  switchStatusView('kept');   // 筛完直接看成果：进入保留视图（伴随筛选随换视图规则归位）
}

/* 详情卡片拖拽与轻扫：右滑保留、左滑不要——仅触屏（含触控笔）生效；桌面鼠标不参与，
   按住拖动与选词/拖选等鼠标原生用法相悖，桌面上快筛走 F/D 键与动作按钮。
   支持触觉振动反馈；屏幕边缘与输入链接按钮不触发 */
let ds = null, dsSuppressClick = false;
$('#detail').addEventListener('pointerdown', (e) => {
  if (e.pointerType === 'mouse') return;   // 桌面鼠标不发起轻扫，卡片保持纯静态
  const card = e.target.closest('.detail-card');
  if (!card) return;
  if (e.target.closest('a, button, input, textarea, select')) return;
  if (e.clientX < 28 || e.clientX > window.innerWidth - 28) return;
  const inMultiPics = e.target.closest('.dt-pics') && card.querySelectorAll('.dt-pics figure').length > 1;
  if (inMultiPics) return;   // 多图网格内让位原生横滑切图（鼠标已在入口被拦，走到这里必是触屏）
  try { card.setPointerCapture?.(e.pointerId); } catch {}
  ds = { card, pointerId: e.pointerId, x: e.clientX, y: e.clientY, dx: 0, dy: 0, intent: '' };
  dsSuppressClick = false;   // 同 feed 侧：滞留的抑制标志不能吞下一次点按
});
$('#detail').addEventListener('pointermove', (e) => {
  if (!ds) return;
  ds.dx = e.clientX - ds.x; ds.dy = e.clientY - ds.y;
  if (!ds.intent && (Math.abs(ds.dx) > 12 || Math.abs(ds.dy || 0) > 12))
    ds.intent = Math.abs(ds.dx) > Math.abs(ds.dy || 0) ? 'h' : 'v';
  if (ds.intent !== 'h') return;
  ds.card.classList.add('dragging');
  ds.card.style.transform = `translateX(${Math.max(-160, Math.min(160, ds.dx))}px)`;
  ds.card.classList.toggle('sw-keep', ds.dx > 44);
  ds.card.classList.toggle('sw-drop', ds.dx < -44);
  ds.card.querySelector('.flash-veil.keep').style.opacity = String(Math.max(0, Math.min(1, ds.dx / 120)));
  ds.card.querySelector('.flash-veil.drop').style.opacity = String(Math.max(0, Math.min(1, -ds.dx / 120)));
});
function endDetailSwipe(e) {
  if (!ds) return;
  const { card, pointerId, dx, intent } = ds;
  ds = null;
  if (pointerId !== undefined) {
    try { card.releasePointerCapture?.(pointerId); } catch {}
  }
  if (e.type === 'pointercancel') { resetDetailCard(card); return; }
  if (intent === 'h' && Math.abs(dx) > 12) dsSuppressClick = true;
  if (intent === 'h' && Math.abs(dx) > 90) {
    if (typeof navigator !== 'undefined' && navigator.vibrate) {
      try { navigator.vibrate(dx > 0 ? 15 : [10, 30, 10]); } catch {}
    }
    detailMarkAdvance(dx > 0 ? 'kept' : 'discarded');
  } else {
    resetDetailCard();
  }
}
$('#detail').addEventListener('pointerup', endDetailSwipe);
$('#detail').addEventListener('pointercancel', endDetailSwipe);
$('#detail').addEventListener('click', (e) => {
  if (dsSuppressClick) { dsSuppressClick = false; e.stopPropagation(); }
}, true);

$('#dt-prev').onclick = () => detailNav(-1);
$('#dt-next').onclick = () => detailNav(1);

/* ---------- 选择模式与批量下载 ---------- */

function setSelectMode(on) {
  S.selectMode = on;
  fab.setHidden(on);   // 选择模式底栏与悬浮球重叠，整段让位
  S.selected.clear();
  feed.classList.toggle('selecting', on);
  $('#selbar').hidden = !on;
  document.body.classList.toggle('has-selbar', Boolean(on));
  feed.querySelectorAll('.card.picked').forEach((c) => c.classList.remove('picked'));
  updateSelN();
  railSync();
}

function togglePick(sid) {
  const card = feed.querySelector(`.card[data-sid="${CSS.escape(sid)}"]`);
  if (S.selected.has(sid)) { S.selected.delete(sid); card?.classList.remove('picked'); }
  else { S.selected.add(sid); card?.classList.add('picked'); }
  updateSelN();
}

function updateSelN() {
  const n = S.selected.size;
  $('#sel-n').textContent = n;
  // 未勾选时动作钮置灰（全选/退出始终可用）
  ['#sel-keep', '#sel-drop', '#sel-tags', '#sel-download'].forEach((q) => { $(q).disabled = n === 0; });
}

$('#sel-cancel').onclick = () => setSelectMode(false);
$('#sel-all').onclick = () => {
  for (const p of S.posts) S.selected.add(p.sid);
  feed.querySelectorAll('.card').forEach((c) => {
    if (!c.classList.contains('fading') && S.selected.has(c.dataset.sid)) c.classList.add('picked');
  });
  updateSelN();
};
$('#sel-download').onclick = () => {
  if (!S.selected.size) { toast('先勾选要下载的帖'); return; }
  settings.openSettings('batch');   // openBatch 由分区激活钩子接手，带上勾选范围
};

async function bulkStatus(status) {
  const sids = [...S.selected];
  if (!sids.length) { toast('先勾选帖子'); return; }
  try {
    const r = await enqueueMutation(() => api('/api/posts/bulk-status', 'POST', { sids, status }));
    const what = status === 'kept' ? '保留' : '不要';
    const extra = r.failed_count ? `，${r.failed_count} 张归档失败，可在顶部重试` : r.exported ? `，${r.exported} 张已归档` : '';
    toast(`已批量${what} ${r.updated} 帖${extra}`);
    setSelectMode(false);
    await loadStatus();
    load(true);
  } catch (e) { toast(e.message); }
}
$('#sel-keep').onclick = () => bulkStatus('kept');
$('#sel-drop').onclick = () => bulkStatus('discarded');

/* ---------- 批量打标签（选择模式底栏 → 轻量选择层） ----------
   只加不删（逐帖移除走详情标签区）；超单帖上限的帖由服务端跳过并在 toast 里说明 */
let bulkTagPicked = new Set();

function renderTagsChoices() {
  const box = $('#tag-choices');
  if (!box || $('#modal-tags').hidden) return;   // 弹层没开时重画是空操作
  // 本次手输的新标签（不在 tagList 里）也要能显示成选中 chip
  const fresh = [...bulkTagPicked].filter((t) => !(S.tagList || []).some((x) => x.tag === t));
  const chips = (S.tagList || []).map((t) =>
    `<button type="button" class="fchip ${bulkTagPicked.has(t.tag) ? 'on' : ''}" aria-pressed="${bulkTagPicked.has(t.tag)}" data-tag="${esc(t.tag)}">${esc(t.tag)}<span class="fcount">${t.n}</span></button>`)
    .concat(fresh.map((t) => `<button type="button" class="fchip on" aria-pressed="true" data-tag="${esc(t)}">${esc(t)}</button>`));
  box.innerHTML = chips.join('') || '<span class="f-empty">还没有标签——在下方输入一个新标签名</span>';
  box.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
    const t = b.dataset.tag;
    if (bulkTagPicked.has(t)) bulkTagPicked.delete(t); else bulkTagPicked.add(t);
    renderTagsChoices();
    [...box.querySelectorAll('.fchip')].find((x) => x.dataset.tag === t)?.focus();
  });
}

$('#sel-tags').onclick = () => {
  if (S.readOnly) { toast('只读设备不能打标签'); return; }
  if (!S.selected.size) { toast('先勾选帖子'); return; }
  bulkTagPicked = new Set();
  $('#tags-n').textContent = S.selected.size;
  $('#tag-new').value = '';
  renderTagsChoices();
  openModal('modal-tags');
};
$('#tag-add-btn').onclick = () => {
  const v = $('#tag-new').value.trim();
  if (!v) { $('#tag-new').focus(); return; }
  $('#tag-new').value = '';
  bulkTagPicked.add(v);
  renderTagsChoices();
  $('#tag-new').focus();
};
$('#tag-new').addEventListener('keydown', (e) => {
  e.stopPropagation();
  if (e.isComposing || e.keyCode === 229) return;   // IME 组词中：回车只确认候选词，不加标签
  if (e.key === 'Enter') { e.preventDefault(); $('#tag-add-btn').click(); }
});
$('#tags-apply').onclick = async () => {
  const add = [...bulkTagPicked];
  if (!add.length) { toast('先勾选或输入要加的标签'); return; }
  const applyBtn = $('#tags-apply');
  if (applyBtn.disabled) return;
  applyBtn.disabled = true;
  const sids = [...S.selected];
  try {
    const r = await enqueueMutation(() => api('/api/posts/bulk-tags', 'POST', {sids, add}));
    const skipped = r.skipped ? `，${r.skipped} 帖超出上限已跳过` : '';
    toast(`已为 ${r.updated} 帖打上标签${skipped}`);
    closeModal('modal-tags');
    for (const p of S.posts) {
      if (!sids.includes(p.sid)) continue;
      p.tags = [...new Set([...(p.tags || []), ...add])];   // 本地同步，详情立即可见
      if (!matchesView(p, S)) syncPost(p);
    }
    setSelectMode(false);
    refreshTags();
  } catch (e) { toast(e.message); } finally { applyBtn.disabled = false; }
};
/* ---------- 抓取(过程面板挂设置窗口「自动抓取」分区) ---------- */

// 分区激活 = 打开过程面板；空闲且从没抓过（无结果）时自动开始一轮，
// 已有进行中/已完成记录则只展示进度与结果，不重复触发
function crawlPaneEnter() {
  renderCrawlModal();
  clearInterval(S.crawlUiTimer);
  S.crawlUiTimer = setInterval(renderCrawlModal, 1000);
  const p = S.st?.crawl?.progress;
  if (!p?.running && !p?.groups?.length) startCrawl();
}

/* ---------- 清理已删除：预览 + 手动触发（自动清理每天 04:10 另有定时） ---------- */

async function renderPurgeModal() {
  const st = S.st || await loadStatus();
  const p = st.purge || { days: 3, due: 0, discarded: 0 };
  $('#purge-days').textContent = p.days;
  $('#purge-summary').textContent =
    `当前「不要」共 ${p.discarded} 帖，其中 ${p.due} 帖到期会清理；${p.protected || 0} 帖因收藏受保护。取消收藏后重新计时。`;
  $('#purge-n').textContent = p.due;
  $('#btn-purge-run').disabled = !p.due;
  const data = await api('/api/posts?status=discarded&group=all&author=all'
    + '&range=all&offset=0&limit=200');
  const cutoff = Date.now() - p.days * 86400e3;
  const due = data.posts
    .filter((x) => !x.favorite && x.discarded_at && new Date(x.discarded_at).getTime() < cutoff);
  $('#purge-list').innerHTML = due.slice(0, 200).map((x) =>
    `<div class="prow"><span>${esc(x.author)}</span>`
    + `<span class="pwhen">${fmtDate(x.discarded_at)}</span></div>`).join('')
    || '<p class="hint">没有已到期的帖子。</p>';
}

$('#btn-purge-run').onclick = async () => {
  const runBtn = $('#btn-purge-run');
  if (runBtn.disabled) return;
  runBtn.disabled = true;
  const days = S.st?.purge?.days || 3;
  try {
    const r = await api(`/api/purge?days=${days}`, 'POST');
    toast(`已清理 ${r.result.purged_posts} 帖 / ${r.result.removed_files} 图`);
    await loadStatus();
    renderPurgeModal();
    load(true);   // 「不要」视图行数变化
  } catch (e) { runBtn.disabled = false; toast(e.message); }
};

async function startCrawl() {
  try {
    const r = await api('/api/crawl', 'POST');
    if (r && r.ok === false) toast(r.msg || '抓取未能开始');
    renderBanner();
    renderCrawlModal();   // 失败原因由服务端写进 progress，弹窗里能看到
    pollCrawl();
  } catch (e) { toast(e.message); await renderCrawlModal(); }
}

$('#btn-crawl-start').onclick = () => startCrawl();

// 分区内的进度渲染：每秒拉一次状态；分区切走或窗口关闭自动停表
async function renderCrawlModal() {
  if ($('#pane-crawl').hidden || $('#modal-settings').hidden) { clearInterval(S.crawlUiTimer); return; }
  let st;
  try { st = await loadStatus(); } catch (e) { return; }
  $('#crawl-time').textContent = `${st.crawl.time || ''} · 最近成功：${st.crawl.last ? fmtTime(st.crawl.last) : '暂无'} · 最近尝试：${st.crawl.attempt ? fmtTime(st.crawl.attempt) : '暂无'}`;
  const p = st.crawl.progress || {};
  const groups = p.groups || [];
  const btn = $('#btn-crawl-start');
  btn.disabled = !!p.running;
  btn.textContent = p.running ? '正在抓取…' : (groups.length ? '再抓一轮' : '立即抓取');
  const lines = $('#crawl-lines');
  if (!groups.length) {
    lines.innerHTML = p.error
      ? `<div class="crawl-line"><b>未能开始</b><span class="st">${esc(p.error)}</span></div>`
      : '<div class="hint" style="padding:2px">还没有抓取记录。</div>';
    return;
  }
  lines.innerHTML = groups.map((g) => {
    const item = !p.running && ['doing','pending'].includes(g.status) ? {...g,status:'interrupted'} : g;
    let tail = esc(captureGroupText(item));
    if (p.running && g.status === 'doing') tail = `<span class="spin"></span> 第 ${g.page || 1} 页 · ${tail}`;
    return `<div class="crawl-line ${p.running && g.status === 'doing' ? 'doing' : ''}">
      <b>${esc(g.name)}</b><span class="st">${tail}</span></div>`;
  }).join('') +
  (p.running ? '' : p.error
    ? `<div class="crawl-line"><b>${captureTitle(p)}</b><span class="st">${esc(p.error)}</span></div>`
    : `<div class="crawl-line"><b>${captureTitle(p)}</b>
       <span class="st">共 ${groups.length} 组 · 新增 ${p.new_posts || 0} 帖 · ${p.new_pics || 0} 图</span></div>`);
}

function pollCrawl() {
  clearInterval(S.pollTimer);
  S.pollFails = 0;
  S.pollTimer = setInterval(async () => {
    try {
      const st = await loadStatus();
      S.pollFails = 0;
      if (!st.crawl.running) {
        clearInterval(S.pollTimer);
        const lr = st.crawl.last_result;
        if (st.cookie.state === 'expired') toast('微博登录过期，请在设置里重新连接');
        else if (lr?.outcome === 'partial') toast('部分采集未完成，已收到的记录保留，可在自动抓取里查看或补抓图片');
        else if (lr?.outcome === 'incomplete') toast('达到翻页上限，日期范围未覆盖完；请在自动抓取里查看');
        else if (lr?.outcome === 'interrupted') toast('采集中断，已收到的记录保留，可重试');
        else if (lr?.outcome === 'failed') toast('抓取失败，请在自动抓取里查看原因');
        else if (lr) toast(`抓取完成：${lr.new_posts} 帖 / ${lr.new_pics} 图`);
        else if (st.cookie.state === 'expired') toast('Cookie 过期，请重新粘贴');
        else if (st.crawl.error) toast('抓取出错：' + st.crawl.error);
        closeDetailIfOpen();   // 整列重取会让 S.posts 换血，详情继续开着只会拿到死数据
        load(true);
      }
    } catch (e) {
      if (++S.pollFails >= 5) {
        clearInterval(S.pollTimer);
        toast('状态刷新连续失败：' + e.message);
      }
    }
  }, 4000);
}

/* ---------- 弹层 ---------- */

/* 关闭走 140ms 淡出（出比进快）；closing 期间忽略重复关闭，重开则取消定时 */
function animatedHide(el) {
  if (el.hidden || el.classList.contains('closing')) return;
  el.classList.add('closing');
  el._hideTimer = setTimeout(() => {
    el.hidden = true;
    el.classList.remove('closing');
    el._hideTimer = null;
    if (el.id === 'modal-spotlight-search' && typeof railSync === 'function') railSync();
  }, 140);
}
function cancelHide(el) {
  if (el._hideTimer) { clearTimeout(el._hideTimer); el._hideTimer = null; }
  el.classList.remove('closing');
}

let modalReturnFocus = null;
function openModal(id) {
  modalReturnFocus = document.activeElement;
  const m = $('#' + id);
  cancelHide(m);
  m.hidden = false;
  document.body.style.overflow = 'hidden';
  const f = m.querySelector('input, textarea') || m.querySelector('button');
  f && f.focus();
}
function closeModal(id) {
  animatedHide($('#' + id));
  if (id === 'modal-settings') settings.closePane?.();   // 停分区级定时器(如抓取轮询)
  if (id === 'modal-spotlight-search' && typeof railSync === 'function') railSync();
  const anyModalOpen = Array.from(document.querySelectorAll('.modal')).some((m) => !m.hidden && !m.classList.contains('closing') && m.id !== id);
  const lbOpen = !$('#lightbox')?.hidden && !$('#lightbox')?.classList.contains('closing');
  const dtOpen = !$('#detail')?.hidden && !$('#detail')?.classList.contains('closing');
  if (!anyModalOpen && !lbOpen && !dtOpen) {
    document.body.style.overflow = '';
  }
  if (modalReturnFocus) {
    // 防止移动端点击 #f-search 唤起后，关窗 refocus 导致死循环反复打开聚光灯
    if (modalReturnFocus.id !== 'f-search' && !modalReturnFocus.closest?.('#f-search, .searchwrap')) {
      modalReturnFocus.focus();
    }
    modalReturnFocus = null;
  }
}
document.querySelectorAll('.mclose, [data-close]').forEach((b) =>
  b.onclick = () => closeModal(b.dataset.close));
document.querySelectorAll('.modal').forEach((m) =>
  m.addEventListener('click', (e) => { if (e.target === m) closeModal(m.id); }));

const devices = initDevices({$,api,toast});
const settings = initSettings({S,$,api,toast,esc,openModal,loadStatus,toggleFavorite,
  getLBIndex:() => lightbox.getIndex(), getDetailSid:() => detailSid,refreshDetail,pollCrawl,
  hooks: {
    batch:  { onEnter: () => openBatch() },
    crawl:  { onEnter: () => crawlPaneEnter(), onLeave: () => clearInterval(S.crawlUiTimer) },
    purge:  { onEnter: () => renderPurgeModal() },
    devices:{ onEnter: () => devices.render() },
  }});
function openGroups() { return settings.openGroups(); }

/* ---------- 批量下载（设置窗口「批量下载」分区：范围 + 细分勾选） ---------- */

const { openBatch } = initBatch({S, $, api, toast});

/* ---------- 筛选与分页 ---------- */

function closeLBIfOpen() { if (!$('#lightbox').hidden) closeLB(); }
function closeDetailIfOpen() { if (!$('#detail').hidden) closeDetail(); }

/* ---------- 视图按钮 + 筛选面板 ---------- */

const STATUS_NAME = { pending: '新增', kept: '保留', favorites: '收藏',
                      discarded: '已删除', all: '全部' };
const STATUS_VIEW_KEYS = { kept: '1', discarded: '2', favorites: '3', all: '4' };

function switchStatusView(status) {
  closeViewPanel();
  const hadFilters = S.group !== 'all' || S.author !== 'all' || S.source !== 'all'
    || S.range !== 'all' || S.hasCmts !== 'all' || S.tags !== 'all' || !!S.searchText
    || (S.searchScope && S.searchScope !== 'all');
  const needsReload = S.status !== status || hadFilters || !$('#detail').hidden || !$('#lightbox').hidden
    || !$('#quad').hidden || S.selectMode;
  if (!needsReload) return;
  // 换视图=换一摞新墙：伴随筛选全部归位（分组/博主/时间/评论/搜索）。
  // 筛选在生效时先快照：切换后 toast 说明并给「撤销切换」——撤销=整墙还原
  // （视图+筛选一起回切换前，不制造视图与旧筛选错配的中间态）
  const snap = hadFilters ? {status: S.status, group: S.group, author: S.author,
    source: S.source, range: S.range, hasCmts: S.hasCmts, tags: S.tags,
    searchText: S.searchText, searchScope: S.searchScope} : null;
  S.status = status;
  // URL 与顶栏由 load(true) 里的 syncUrl/renderViewButton 重写
  S.group = 'all'; S.author = 'all'; S.range = 'all';
  S.hasCmts = 'all'; S.tags = 'all'; S.searchText = ''; S.searchScope = 'all'; S.source = 'all';
  if (status === 'pending' || status === 'discarded') S.sort = '';
  // 排序段只在 保留/收藏/全部 可见（showSort 同口径）：不可见的视图落回时间序，
  // 新增队列（快筛/四格）尤其不打乱
  $('#f-search').value = '';
  closeLBIfOpen();
  closeDetailIfOpen();
  quad.closeIfOpen();   // 四格绑定新增视图，换视图即收会话（未裁的留在新增里）
  if (S.selectMode) setSelectMode(false);
  renderViewPanel();
  window.scrollTo(0, 0);
  load(true);
  if (snap) toast(`已切到「${STATUS_NAME[status] || status}」，原筛选已重置`, {label: '撤销切换', fn: () => {
    Object.assign(S, snap);
    $('#f-search').value = snap.searchText;
    closeLBIfOpen();
    closeDetailIfOpen();
    quad.closeIfOpen();
    if (S.selectMode) setSelectMode(false);
    renderViewPanel();
    window.scrollTo(0, 0);
    load(true);
  }});
}

/* 快筛会话：悬浮球 / 左轨入口——桌面进四格（键位直裁，一屏四张）；
   窄屏/触屏退回单卡详情流（点卡开详情，底栏按钮或左右滑动裁决，与普通详情同一套交互）。
   完成时刻复用各自的「快筛完成」toast；进度由顶栏视图与待处理数承载。 */
async function startQuickTriage() {
  if (S.readOnly) { toast('只读设备不能筛选'); return; }
  if ((S.st?.counts?.pending || 0) === 0) { toast('新增已筛完 ✓'); return; }
  if (window.matchMedia?.('(min-width: 641px)').matches) { quad.open(); return; }
  triageKept = 0; triageDropped = 0;   // 会话从零起账
  if (S.status !== 'pending') switchStatusView('pending');
  if (!S.posts.length) await load(true);   // 换视图后确保首页就绪
  const first = S.posts[0];
  if (first) openDetail(first.sid);
  else toast('没有待处理的新增');
}

const SORT_CHIPS = [
  { v: '', label: '最新' },
  { v: 'keep_count', label: '点赞' },
  { v: 'shuffle', label: '随机' },
];
const SOURCE_CHIPS = [{ v: 'all', label: '全部' }, { v: 'weibo', label: '微博' }, { v: 'x', label: 'X' }];
const RANGE_CHIPS = [
  { v: 'all', label: '全部' },
  { v: 'today', label: '今天' },
  { v: 'yesterday', label: '昨天' },
  { v: '7d', label: '7天' },
  { v: '30d', label: '30天' },
];
const CMT_CHIPS = [
  { v: 'all', label: '全部' },
  { v: '1', label: '只看有评论' },
];

/* 标签筛选行：多选 chip（OR 语义，任一命中即看）；首枚「全部」清空选择。
   tagList 为空给一行引导文案（没有标签时这一行不是噪音，是入口提示） */
function renderTagRow() {
  const box = $('#f-tags');
  if (!box) return;
  const sel = S.tags === 'all' ? [] : S.tags.split(',');
  if (!(S.tagList || []).length && !sel.length) {
    box.innerHTML = S.readOnly ? '<span class="f-empty">还没有标签</span>'
      : '<span class="f-empty">还没有标签——在详情里给帖子打一个</span>';
    return;
  }
  const chips = [`<button class="fchip ${!sel.length ? 'on' : ''}" data-tag="">全部</button>`]
    .concat((S.tagList || []).map((t) =>
      `<button class="fchip ${sel.includes(t.tag) ? 'on' : ''}" data-tag="${esc(t.tag)}"
         title="${esc(t.tag)} · ${t.n} 帖">${esc(t.tag)}<span class="fcount">${t.n}</span></button>`));
  box.innerHTML = chips.join('');
  box.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
    const t = b.dataset.tag;
    if (!t) { S.tags = 'all'; }
    else {
      const cur = S.tags === 'all' ? [] : S.tags.split(',');
      const i = cur.indexOf(t);
      if (i >= 0) cur.splice(i, 1); else cur.push(t);
      S.tags = cur.length ? cur.join(',') : 'all';
    }
    closeLBIfOpen(); closeDetailIfOpen();
    renderViewPanel();
    load(true);
  });
}

function renderSortRow() {
  const row = $('#f-sort-row');
  const box = $('#f-sort');
  if (!row || !box) return;
  const showSort = ['kept', 'favorites', 'all'].includes(S.status) && !timeline.active();
  row.hidden = !showSort;
  if (!showSort) return;
  box.innerHTML = SORT_CHIPS.map((x) =>
    `<button class="fchip ${S.sort === x.v ? 'on' : ''}" data-v="${x.v}">${x.label}</button>`).join('');
  box.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
    const v = b.dataset.v;
    if (S.sort === v) return;
    S.sort = v;
    closeLBIfOpen(); closeDetailIfOpen();
    renderViewPanel();
    load(true);
  });
}

function renderGroupRow() {
  const row = $('#f-group-row');
  const box = $('#f-group');
  if (!row || !box) return;
  const groups = ((S.st && S.st.groups) || []).filter((g) =>
    g.active && !/^(xu:|wbu:)/.test(g.list_id) && g.list_id !== 'demo');
  if (S.group !== 'all' && !groups.some((g) => g.list_id === S.group)) {
    groups.push({ list_id: S.group, name: S.group, total: 0 });
  }
  row.hidden = !groups.length;
  if (!groups.length) return;
  const chips = [`<button class="fchip ${S.group === 'all' ? 'on' : ''}" data-group="all">全部</button>`]
    .concat(groups.map((g) =>
      `<button class="fchip ${S.group === g.list_id ? 'on' : ''}" data-group="${esc(g.list_id)}"
         title="${esc(g.name)} · ${g.total || 0} 帖">${esc(g.name)}${g.total ? `<span class="fcount">${g.total}</span>` : ''}</button>`));
  box.innerHTML = chips.join('');
  box.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
    const lid = b.dataset.group;
    S.group = S.group === lid && lid !== 'all' ? 'all' : lid;
    if (S.group !== 'all' && S.author !== 'all') S.author = 'all';
    closeLBIfOpen(); closeDetailIfOpen();
    renderViewPanel();
    load(true);
  });
}

function renderViewPanel() {
  renderSortRow();
  renderGroupRow();

  const srcBox = $('#f-source');
  if (srcBox) {
    srcBox.innerHTML = SOURCE_CHIPS.map((x) =>
      `<button class="fchip ${S.source === x.v ? 'on' : ''}" data-v="${x.v}">${x.label}</button>`).join('');
    srcBox.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
      S.source = b.dataset.v;
      S.author = 'all';   // 换来源清作者筛选：上一来源的博主在新来源里不存在
      closeLBIfOpen(); closeDetailIfOpen();
      renderViewPanel();
      load(true);
    });
  }

  // 标签行：多选 chip，数据来自 refreshTags
  renderTagRow();

  // 时间范围行：全部 / 今天 / 昨天 / 7天 / 30天
  const rangeBox = $('#f-range');
  if (rangeBox) {
    rangeBox.innerHTML = RANGE_CHIPS.map((x) =>
      `<button class="fchip ${S.range === x.v ? 'on' : ''}" data-v="${x.v}">${x.label}</button>`).join('');
    rangeBox.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
      S.range = b.dataset.v;
      closeLBIfOpen(); closeDetailIfOpen();
      renderViewPanel();
      load(true);
    });
  }

  // 评论行：全部 / 只看有评论
  const cmtBox = $('#f-has-cmts');
  if (cmtBox) {
    cmtBox.innerHTML = CMT_CHIPS.map((x) =>
      `<button class="fchip ${S.hasCmts === x.v ? 'on' : ''}" data-v="${x.v}">${x.label}</button>`).join('');
    cmtBox.querySelectorAll('.fchip').forEach((b) => b.onclick = () => {
      const v = b.dataset.v;
      S.hasCmts = (v === '1' && S.hasCmts === '1') ? 'all' : v;
      closeLBIfOpen(); closeDetailIfOpen();
      renderViewPanel();
      load(true);
    });
  }

  // 重置筛选：任一伴随筛选偏离默认时出现（状态本身不算筛选）
  const hasFilters = S.group !== 'all' || S.author !== 'all' || S.source !== 'all'
    || S.range !== 'all' || S.hasCmts !== 'all' || S.tags !== 'all' || !!S.searchText
    || (S.searchScope && S.searchScope !== 'all');
  const resetBtn = $('#f-reset');
  if (resetBtn) resetBtn.hidden = !hasFilters;
  railSync();   // 视图/排序/来源/博主的所有变更都汇到这：轨上高亮与 tip 即时跟
}

function resetFilters() {
  S.group = 'all'; S.author = 'all'; S.source = 'all';
  S.range = 'all'; S.hasCmts = 'all'; S.tags = 'all'; S.searchText = '';
  S.searchScope = 'all';
  S.appliedSearchText = '';
  S.appliedSearchScope = 'all';
  if (typeof setSpotlightScope === 'function') setSpotlightScope('all');
  $('#f-search').value = '';
  if ($('#spotlight-input')) {
    $('#spotlight-input').value = '';
    updateSpotlightClearBtn();
  }
  renderViewButton();
  renderViewPanel();
  renderActiveSearchChip();
  load(true);
}

/* 正文搜索：只在回车时应用（输入过程不打扰列表）；清空后回车即重置 */
function applySearch(v, scope) {
  v = (v || '').trim().slice(0, 100);
  const nextScope = !v ? 'all' : (scope !== undefined ? scope : (S.searchScope || 'all'));
  if (v === S.appliedSearchText && nextScope === S.appliedSearchScope) return;
  S.searchText = v;
  S.searchScope = nextScope;
  S.appliedSearchText = S.searchText;
  S.appliedSearchScope = S.searchScope;
  if ($('#f-search')) $('#f-search').value = v;
  if ($('#spotlight-input')) {
    $('#spotlight-input').value = v;
    updateSpotlightClearBtn();
  }
  closeLBIfOpen(); closeDetailIfOpen();
  renderViewButton();
  renderActiveSearchChip();
  railSync();   // 右轨搜索状态即时对齐
  load(true);
}

function renderActiveSearchChip() {
  const chipContainer = $('#feed-search-chip');
  const term = (S.searchText || '').trim();
  const topClear = $('#f-search-clear');
  const searchWrap = $('.searchwrap');

  if (topClear) topClear.hidden = !term;
  if (searchWrap) searchWrap.classList.toggle('has-query', !!term);

  if (!chipContainer) return;
  if (!term) {
    chipContainer.hidden = true;
    chipContainer.innerHTML = '';
    return;
  }

  const scope = S.searchScope || 'all';
  const meta = {
    author: { icon: '👤', label: '博主', cls: 'cat-author' },
    tag: { icon: '🏷️', label: '标签', cls: 'cat-tag' },
    comment: { icon: '💬', label: '评论', cls: 'cat-comment' },
    text: { icon: '📝', label: '内容', cls: 'cat-content' },
    all: { icon: '🔍', label: '全部', cls: 'cat-all' },
  }[scope] || { icon: '🔍', label: '全部', cls: 'cat-all' };

  const displayTerm = scope === 'tag' && !term.startsWith('#') ? `#${term}` : term;

  chipContainer.hidden = false;
  chipContainer.innerHTML = `
    <div class="active-search-chip ${meta.cls}" role="button" tabindex="0" title="点击调整检索">
      <span class="active-search-badge ${meta.cls}">${meta.icon} ${esc(meta.label)}</span>
      <span class="active-search-text">${esc(displayTerm)}</span>
      <button type="button" class="active-search-clear" aria-label="清空当前检索" title="清空检索">✕</button>
    </div>
  `;

  const chipEl = chipContainer.querySelector('.active-search-chip');
  chipEl?.addEventListener('click', (e) => {
    const clearBtn = e.target.closest('.active-search-clear');
    if (clearBtn) {
      e.stopPropagation();
      applySearch('', 'all');
      return;
    }
    openSpotlightSearch(S.searchScope);
  });
  chipEl?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openSpotlightSearch(S.searchScope);
    }
  });
}
if ($('#f-reset')) $('#f-reset').onclick = resetFilters;
$('#f-search').addEventListener('input', () => {
  let val = $('#f-search').value;
  let newScope = null;
  if (val.startsWith('@') || val.startsWith('＠')) {
    newScope = 'author';
    val = val.slice(1);
  } else if (val.startsWith('#') || val.startsWith('＃')) {
    newScope = 'tag';
    val = val.slice(1);
  } else if (val.startsWith('//') || val.startsWith('／／')) {
    newScope = 'comment';
    val = val.slice(2);
  } else if (/^c:/i.test(val) || /^c：/i.test(val)) {
    newScope = 'comment';
    val = val.slice(2);
  }
  if (newScope) {
    $('#f-search').value = val;
    S.searchScope = newScope;
    railSync();
  }
  if (!newScope && !$('#f-search').value && S.searchText) {
    applySearch('', S.searchScope);
  }
});
$('#f-search').addEventListener('keydown', (e) => {
  e.stopPropagation();   // 输入框里的方向键/回车不进全局快捷键
  if (e.isComposing || e.keyCode === 229) return;   // IME 组词中：回车只确认候选词，不搜索
  if (e.key === 'Enter') applySearch($('#f-search').value, S.searchScope);
});
function viewState() {
  // 视图/筛选角标计数：状态名为主文字，其余激活筛选计入徽章
  let n = 0;
  if (S.group !== 'all') n += 1;
  if (S.source !== 'all') n += 1;
  if (S.author !== 'all') n += 1;
  if (S.hasCmts !== 'all') n += 1;
  if (S.range !== 'all') n += 1;
  if (S.tags !== 'all') n += 1;
  if (S.searchText) n += 1;
  return { name: STATUS_NAME[S.status] || '视图', n };
}

function renderViewButton() {
  const { name, n } = viewState();
  $('#view-label').textContent = name;
  const f = $('#view-filter');
  f.hidden = n === 0;
  $('#view-fcount').textContent = n;
}

const viewwrap = $('.viewwrap');
/* 面板开合收敛到这两个入口（顶栏钮、右上筛选钮、Shift+F、点外收起共用）：
   桌面端紧贴右上胶囊 #tt-filter 下沿右对齐；收起时重置筛选浮层状态 */
function alignViewPanel() {
  const panel = $('#view-panel');
  const btn = document.getElementById('tt-filter');
  if (window.innerWidth >= 900 && btn) {
    const rect = btn.getBoundingClientRect();
    panel.style.top = `${Math.round(rect.bottom + 10)}px`;
    panel.style.right = '16px';
    panel.style.left = 'auto';
    panel.style.transform = 'none';
  } else {
    panel.style.top = '';
    panel.style.right = '';
    panel.style.left = '';
    panel.style.transform = '';
  }
}
function closeViewPanel() {
  const panel = $('#view-panel');
  if (panel.hidden) return;
  panel.hidden = true;
  $('#btn-view').setAttribute('aria-expanded', 'false');
  authorPickerOpen = false; authorQuery = '';
  railSync();
}
function toggleViewPanel() {
  const panel = $('#view-panel');
  if (panel.hidden) {
    alignViewPanel();
    panel.hidden = false;
    $('#btn-view').setAttribute('aria-expanded', 'true');
    refreshTags();   // 打开面板时补一次标签清单，chip 计数与新增标签不滞后
    renderViewPanel();
    railSync();
  } else closeViewPanel();
}
$('#btn-view').onclick = (e) => {
  e.stopPropagation();
  toggleViewPanel();
};
document.addEventListener('click', (e) => {
  // 用 composedPath 判界：面板内胶囊点击会同步重渲染 #f-source/#f-tags（innerHTML
  // 换血），到 document 层时 e.target 已脱离 DOM，closest() 会误判「点外」关掉面板；
  // #tt-filter 是桌面打开本面板的右上胶囊钮（rail.js 绑定），点它同样不算「点外」
  const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
  const inside = path.length ? path.some((n) => n instanceof Element
    && (n.id === 'view-panel' || n.classList?.contains('viewwrap') || n.id === 'tt-filter'))
    : !!(e.target instanceof Element && e.target.closest('#view-panel, .viewwrap, #tt-filter'));
  if (!inside) closeViewPanel();
});

$('#btn-more').onclick = () => load(false);

// 底部 sentinel：快滚到底时自动预载下一页；按钮保留作为键盘可达入口
new IntersectionObserver((entries) => {
  if (entries.some((en) => en.isIntersecting) && !S.loadError &&
      S.hasMore && !S.loading)
    load(false);
}, { rootMargin: '600px' }).observe($('.more'));

/* ---------- 启动 ---------- */

function syncUrl() {
  // 筛选状态 → 查询串（只写非默认项）：视图可收藏、可分享；replaceState 不产生历史
  const p = new URLSearchParams();
  if (S.status !== 'all') p.set('status', S.status);
  if (S.group !== 'all') p.set('group', S.group);
  if (S.author !== 'all') p.set('author', S.author);
  if (S.range !== 'all') p.set('range', S.range);
  if (S.hasCmts !== 'all') p.set('has_cmts', S.hasCmts);
  if (S.source !== 'all') p.set('source', S.source);
  if (S.tags !== 'all') p.set('tags', S.tags);
  if (S.searchText) p.set('q', S.searchText);
  if (S.searchScope && S.searchScope !== 'all') p.set('scope', S.searchScope);
  if (S.sort) p.set('sort', S.sort);
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

(function () {   // URL 查询串 → 初始筛选：链接打开即还原视图（可收藏、可分享）
  const p = new URLSearchParams(location.search);
  if (p.has('code')) {
    const clean = new URL(location.href);
    clean.searchParams.delete('code');
    try { history.replaceState(null, '', clean.pathname + (clean.search || '') + clean.hash); } catch (e) {}
  }
  if (['pending', 'kept', 'discarded', 'favorites', 'all'].includes(p.get('status')))
    S.status = p.get('status');
  // 旧链接的 ?fav=1 折叠进收藏视图（同为 favorite=1 口径，结果集等价）
  if (p.get('fav') === '1') S.status = 'favorites';
  if (p.get('group')) S.group = p.get('group');
  if (p.get('author')) S.author = p.get('author');
  // 时间/评论/分组已无面板入口，但旧链接的参数仍认（重置筛选可清）
  if (['today', 'yesterday', '7d', '30d'].includes(p.get('range'))) S.range = p.get('range');
  if (p.get('has_cmts') === '1') S.hasCmts = '1';
  if (['all', 'weibo', 'x'].includes(p.get('source'))) S.source = p.get('source');
  if (p.get('tags')) S.tags = p.get('tags').slice(0, 240);   // 每段标签内容仍由服务端校验
  if (['keep_count', 'shuffle'].includes(p.get('sort'))) S.sort = p.get('sort');
  if (S.status === 'pending' || S.status === 'discarded') S.sort = '';   // 深链不把排序带进排序段不可见的视图
  if (p.get('q')) {
    S.searchText = p.get('q').slice(0, 100);
    $('#f-search').value = S.searchText;
  }
  const scope = p.get('scope');
  if (['all', 'text', 'comment', 'tag', 'author'].includes(scope)) {
    S.searchScope = scope;
  }
  S.appliedSearchText = S.searchText;
  S.appliedSearchScope = S.searchScope;
})();

/* 编年时间线：收卡口挂在 appendToShortestCol/insertCardAt/ensureFeedCols 三处，
   取数、乐观标记、大图/详情照走原链路；初始化只应用记忆布局，首屏 load(true) 自然分流。 */
const timeline = createTimeline({
  S, $, api, toast, renderCard, load,
  renderViewPanel,
  railSync,   // 布局切换（V 键/面板行）后左右轨的编年高亮即时跟
  closeOverlays: () => { closeLBIfOpen(); closeDetailIfOpen(); },
  canSwitch: () => $('#detail').hidden && $('#lightbox').hidden && $('#slideshow').hidden
    && !quad.active() && !review.active() && !S.selectMode,
});
timeline.init();

/* 博主视图：卡片/详情头像进入，头部条显隐跟 S.author，load(true) 里统一 sync */
const authorView = createAuthorView({
  S, $, api, esc, avatarSrc, icons: ICONS, load,
  renderViewPanel,
  closeOverlays: () => { closeLBIfOpen(); closeDetailIfOpen(); },
  chronoActive: () => timeline.active(),
  reflow: () => ensureFeedCols(),
});
authorView.init();

/* ---------- 居中聚光灯大搜索栏（Spotlight Search） ---------- */
const RECENT_SEARCH_KEY = 'pw_recent_searches';

function getRecentSearches() {
  try {
    const raw = localStorage.getItem(RECENT_SEARCH_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) {
    return [];
  }
}

function saveRecentSearch(text, scope = 'all') {
  const q = (text || '').trim().slice(0, 100);
  if (!q) return;
  let list = getRecentSearches();
  list = list.filter((item) => {
    const itemQ = typeof item === 'string' ? item : item.q;
    return itemQ !== q;
  });
  list.unshift({ q, scope: scope || 'all' });
  if (list.length > 8) list = list.slice(0, 8);
  try {
    localStorage.setItem(RECENT_SEARCH_KEY, JSON.stringify(list));
  } catch (e) {}
}

function deleteRecentSearch(q) {
  let list = getRecentSearches();
  list = list.filter((item) => {
    const itemQ = typeof item === 'string' ? item : item.q;
    return itemQ !== q;
  });
  try {
    localStorage.setItem(RECENT_SEARCH_KEY, JSON.stringify(list));
  } catch (e) {}
  renderSpotlightRecents();
}

function clearRecentSearches() {
  try {
    localStorage.removeItem(RECENT_SEARCH_KEY);
  } catch (e) {}
  renderSpotlightRecents();
}

let currentSpotlightScope = 'all';

function setSpotlightScope(scope) {
  currentSpotlightScope = scope || 'all';
  const scopesEl = $('#spotlight-scopes');
  if (scopesEl) {
    scopesEl.querySelectorAll('.s-scope').forEach((b) => {
      b.classList.toggle('on', b.dataset.scope === currentSpotlightScope);
    });
  }
}

function renderSpotlightRecents() {
  const sec = $('#spotlight-recents-section');
  const container = $('#spotlight-recents');
  if (!sec || !container) return;
  const list = getRecentSearches();
  if (!list.length) {
    sec.hidden = true;
    container.innerHTML = '';
    return;
  }
  sec.hidden = false;
  container.innerHTML = list.map((item) => {
    const q = typeof item === 'string' ? item : item.q;
    const scope = typeof item === 'string' ? 'all' : (item.scope || 'all');
    const scopeLabel = { text: '内容', comment: '评论', tag: '标签', author: '博主' }[scope];
    const badge = scopeLabel ? `<span class="spotlight-chip-badge">${esc(scopeLabel)}</span>` : '';
    return `<div class="spotlight-chip" role="button" tabindex="0" data-search-q="${esc(q)}" data-search-scope="${esc(scope)}">`
      + `${badge}<span class="spotlight-chip-text">${esc(q)}</span>`
      + `<button type="button" class="spotlight-chip-del" data-del-q="${esc(q)}" aria-label="删除此条记录">×</button>`
      + `</div>`;
  }).join('');
}

function renderSpotlightTags() {
  const container = $('#spotlight-tags');
  if (!container) return;
  const tags = (S.tagList || []).slice(0, 10);
  if (!tags.length) {
    container.innerHTML = '<span class="hint">暂无标签</span>';
    return;
  }
  container.innerHTML = tags.map((t) => {
    return `<button type="button" class="spotlight-chip spotlight-tag-chip" data-tag-name="${esc(t.tag)}">`
      + `<span class="spotlight-tag-hash">#</span><span class="spotlight-chip-text">${esc(t.tag)}</span>`
      + (t.n ? `<span class="spotlight-chip-count">${t.n}</span>` : '')
      + `</button>`;
  }).join('');
}

function detectSpotlightPrefix() {
  const inp = $('#spotlight-input');
  if (!inp) return null;
  let val = inp.value;
  let newScope = null;
  if (val.startsWith('@') || val.startsWith('＠')) {
    newScope = 'author';
    val = val.slice(1);
  } else if (val.startsWith('#') || val.startsWith('＃')) {
    newScope = 'tag';
    val = val.slice(1);
  } else if (val.startsWith('//') || val.startsWith('／／')) {
    newScope = 'comment';
    val = val.slice(2);
  } else if (/^c:/i.test(val) || /^c：/i.test(val)) {
    newScope = 'comment';
    val = val.slice(2);
  }
  if (newScope) {
    inp.value = val;
    setSpotlightScope(newScope);
    return newScope;
  }
  return null;
}

let liveSearchTimer = null;
let liveSearchController = null;
let liveSearchSeq = 0;
let spotlightNavIndex = -1;

function highlightSnippet(text, query, maxLen = 80) {
  if (!text) return '';
  const cleanQ = (query || '').trim();
  if (!cleanQ) return esc(text.length > maxLen ? text.slice(0, maxLen) + '…' : text);

  const lowerText = text.toLowerCase();
  const lowerQ = cleanQ.toLowerCase();
  const idx = lowerText.indexOf(lowerQ);

  if (idx === -1) {
    return esc(text.length > maxLen ? text.slice(0, maxLen) + '…' : text);
  }

  const matchLen = cleanQ.length;
  let start = Math.max(0, idx - 20);
  let end = Math.min(text.length, idx + matchLen + 40);

  if (end - start < maxLen) {
    if (start === 0) end = Math.min(text.length, maxLen);
    else if (end === text.length) start = Math.max(0, text.length - maxLen);
  }

  const prefix = start > 0 ? '…' : '';
  const suffix = end < text.length ? '…' : '';

  const before = text.slice(start, idx);
  const matched = text.slice(idx, idx + matchLen);
  const after = text.slice(idx + matchLen, end);

  return prefix + esc(before) + '<mark>' + esc(matched) + '</mark>' + esc(after) + suffix;
}

function updateSpotlightNav(items, newIndex) {
  if (!items || !items.length) {
    spotlightNavIndex = -1;
    return;
  }
  items.forEach((item, idx) => {
    const isSel = idx === newIndex;
    item.classList.toggle('selected', isSel);
    item.setAttribute('aria-selected', String(isSel));
    if (isSel) {
      item.scrollIntoView({ block: 'nearest' });
    }
  });
  spotlightNavIndex = newIndex;
}

function renderLiveResults(data, query, scope) {
  const container = $('#spotlight-live-results');
  const defaultBody = $('#spotlight-default-body');
  if (!container) return;

  const q = (query || '').trim();
  if (!q) {
    container.hidden = true;
    container.innerHTML = '';
    if (defaultBody) defaultBody.hidden = false;
    return;
  }

  if (defaultBody) defaultBody.hidden = true;
  container.hidden = false;

  const authors = data?.authors || [];
  const tags = data?.tags || [];
  const content = data?.content || [];
  const comments = data?.comments || [];
  const total = authors.length + tags.length + content.length + comments.length;

  let html = '';

  if (total === 0) {
    html += `<div class="spotlight-empty-state"><span>未找到与 “<b>${esc(q)}</b>” 相关的匹配结果</span><span class="spotlight-empty-sub">尝试按回车在图片墙搜索，或更换关键词</span></div>`;
  } else {
    // 1. 博主分区 (Author Partition)
    if (authors.length > 0) {
      html += `<div class="spotlight-res-group spotlight-partition cat-author" data-group="author">`;
      html += `<div class="spotlight-group-head">`
        + `<div class="spotlight-partition-title">`
        + `<svg class="spotlight-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`
        + `<span>博主</span>`
        + `</div>`
        + `<span class="spotlight-partition-count">${authors.length}</span>`
        + `</div>`;
      html += `<div class="spotlight-partition-list">`;
      html += authors.map((a) => {
        const av = avatarSrc(a.avatar);
        const avHtml = av
          ? `<img class="spotlight-res-avatar" src="${esc(av)}" alt="" decoding="async">`
          : `<span class="spotlight-res-avatar-fallback">${esc((a.author || '微')[0])}</span>`;
        const cleanAuthorQ = q.replace(/^[@＠]/, '');
        return `<div class="spotlight-result-item" data-type="author" data-uid="${esc(a.uid)}" data-name="${esc(a.author)}" data-avatar="${esc(a.avatar)}" role="option" tabindex="0">`
          + avHtml
          + `<div class="spotlight-res-main"><span class="spotlight-res-title">${highlightSnippet(a.author, cleanAuthorQ)}<span class="spotlight-res-sub">UID: ${esc(a.source_uid || a.uid)}</span></span></div>`
          + `<span class="spotlight-res-meta">${a.n} 篇</span>`
          + `</div>`;
      }).join('');
      html += `</div></div>`;
    }

    // 2. 标签分区 (Tag Partition)
    if (tags.length > 0) {
      html += `<div class="spotlight-res-group spotlight-partition cat-tag" data-group="tag">`;
      html += `<div class="spotlight-group-head">`
        + `<div class="spotlight-partition-title">`
        + `<svg class="spotlight-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/></svg>`
        + `<span>标签</span>`
        + `</div>`
        + `<span class="spotlight-partition-count">${tags.length}</span>`
        + `</div>`;
      html += `<div class="spotlight-partition-list">`;
      html += tags.map((t) => {
        const cleanTagQ = q.replace(/^[#＃]/, '');
        return `<div class="spotlight-result-item" data-type="tag" data-tag="${esc(t.tag)}" role="option" tabindex="0">`
          + `<span class="spotlight-tag-icon">#</span>`
          + `<div class="spotlight-res-main"><span class="spotlight-res-title">${highlightSnippet(t.tag, cleanTagQ)}</span></div>`
          + `<span class="spotlight-res-meta">${t.n} 篇</span>`
          + `</div>`;
      }).join('');
      html += `</div></div>`;
    }

    // 3. 内容分区 (Content Partition)
    if (content.length > 0) {
      html += `<div class="spotlight-res-group spotlight-partition cat-content" data-group="content">`;
      html += `<div class="spotlight-group-head">`
        + `<div class="spotlight-partition-title">`
        + `<svg class="spotlight-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`
        + `<span>内容</span>`
        + `</div>`
        + `<span class="spotlight-partition-count">${content.length}</span>`
        + `</div>`;
      html += `<div class="spotlight-partition-list">`;
      html += content.map((c) => {
        const thumb = mediaSrc(c.thumb);
        const thumbHtml = thumb
          ? `<img class="spotlight-res-thumb" src="${esc(thumb)}" alt="" loading="lazy" decoding="async">`
          : `<span class="spotlight-res-thumb-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg></span>`;
        return `<div class="spotlight-result-item" data-type="content" data-sid="${esc(c.sid)}" role="option" tabindex="0">`
          + thumbHtml
          + `<div class="spotlight-res-main"><span class="spotlight-res-snippet">${highlightSnippet(c.text, q, 70)}</span></div>`
          + `<span class="spotlight-res-meta"><span>${esc(c.author || '')}</span>${c.created_at ? `<time class="spotlight-res-date">${fmtDate(c.created_at)}</time>` : ''}</span>`
          + `</div>`;
      }).join('');
      html += `</div></div>`;
    }

    // 4. 评论分区 (Comment Partition)
    if (comments.length > 0) {
      html += `<div class="spotlight-res-group spotlight-partition cat-comment" data-group="comment">`;
      html += `<div class="spotlight-group-head">`
        + `<div class="spotlight-partition-title">`
        + `<svg class="spotlight-group-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`
        + `<span>评论</span>`
        + `</div>`
        + `<span class="spotlight-partition-count">${comments.length}</span>`
        + `</div>`;
      html += `<div class="spotlight-partition-list">`;
      html += comments.map((cm) => {
        const av = avatarSrc(cm.avatar);
        const avHtml = av
          ? `<img class="spotlight-res-avatar" src="${esc(av)}" alt="" decoding="async">`
          : `<span class="spotlight-res-avatar-fallback">${esc((cm.author || '评')[0])}</span>`;
        const cleanCmtQ = q.replace(/^(\/\/|／／|c:|c：)/i, '');
        return `<div class="spotlight-result-item" data-type="comment" data-sid="${esc(cm.sid)}" data-cid="${esc(cm.comment_id || '')}" role="option" tabindex="0">`
          + avHtml
          + `<div class="spotlight-res-main"><span class="spotlight-res-snippet">${highlightSnippet(cm.body, cleanCmtQ, 70)}</span></div>`
          + `<span class="spotlight-res-meta"><span>${esc(cm.author || '')}</span>${cm.created_at ? `<time class="spotlight-res-date">${fmtDate(cm.created_at)}</time>` : ''}</span>`
          + `</div>`;
      }).join('');
      html += `</div></div>`;
    }
  }

  // 底部统一动作：在图片墙查看全部结果 ↵
  html += `<div class="spotlight-result-item spotlight-action-all" data-type="action-all" role="option" tabindex="0">`
    + `<svg class="spotlight-action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`
    + `<div class="spotlight-res-main"><span class="spotlight-res-title">在图片墙查看全部结果 <b>“${esc(q)}”</b></span></div>`
    + `<kbd class="spotlight-res-kbd">↵ Enter</kbd>`
    + `</div>`;

  container.innerHTML = html;
  spotlightNavIndex = -1;
}

function triggerLiveSearch(query, scope) {
  clearTimeout(liveSearchTimer);
  const q = (query || '').trim();
  const defaultBody = $('#spotlight-default-body');
  const liveResults = $('#spotlight-live-results');

  if (!q) {
    liveSearchSeq++;
    liveSearchController?.abort();
    if (liveResults) {
      liveResults.hidden = true;
      liveResults.innerHTML = '';
    }
    if (defaultBody) defaultBody.hidden = false;
    return;
  }

  liveSearchTimer = setTimeout(async () => {
    liveSearchController?.abort();
    liveSearchController = new AbortController();
    const seq = ++liveSearchSeq;
    try {
      const activeScope = scope || currentSpotlightScope || 'all';
      const url = `/api/search/live?q=${encodeURIComponent(q)}&q_scope=${encodeURIComponent(activeScope)}`;
      const data = await api(url, 'GET', undefined, { signal: liveSearchController.signal });
      const curInput = $('#spotlight-input')?.value.trim() || '';
      if (seq === liveSearchSeq && curInput === q && activeScope === currentSpotlightScope) {
        renderLiveResults(data, q, activeScope);
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        // 请求失败静默
      }
    }
  }, 150);
}

function selectSpotlightItem(item) {
  if (!item) return;
  const type = item.dataset.type;
  const inp = $('#spotlight-input');
  const q = inp ? inp.value.trim() : '';

  if (type === 'author') {
    const uid = item.dataset.uid;
    const name = item.dataset.name;
    const avatar = item.dataset.avatar;
    saveRecentSearch(name, 'author');
    closeSpotlightSearch();
    authorView.open(uid, { author: name, avatar });
  } else if (type === 'tag') {
    const tagName = item.dataset.tag;
    setSpotlightScope('tag');
    applySearch(tagName, 'tag');
    saveRecentSearch(tagName, 'tag');
    closeSpotlightSearch();
  } else if (type === 'content') {
    const sid = item.dataset.sid;
    if (q) saveRecentSearch(q, 'text');
    closeSpotlightSearch();
    openDetail(sid);
  } else if (type === 'comment') {
    const sid = item.dataset.sid;
    if (q) saveRecentSearch(q, 'comment');
    closeSpotlightSearch();
    openDetail(sid, true);
  } else if (type === 'action-all') {
    detectSpotlightPrefix();
    applySearch(q, currentSpotlightScope);
    if (q) saveRecentSearch(q, currentSpotlightScope);
    closeSpotlightSearch();
  }
}

function updateSpotlightClearBtn() {
  const inp = $('#spotlight-input');
  const btn = $('#spotlight-clear');
  if (inp && btn) {
    btn.hidden = !inp.value;
  }
}

function openSpotlightSearch(initialScope) {
  const modal = $('#modal-spotlight-search');
  if (!modal) return;
  if (!modal.hidden && !modal.classList.contains('closing')) {
    const targetScope = initialScope || currentSpotlightScope;
    if (targetScope === currentSpotlightScope) {
      $('#spotlight-input')?.focus();
      return;
    }
  }
  // 关闭可能冲突的其他浮层与面板
  document.querySelectorAll('.modal:not([hidden])').forEach((m) => {
    if (m.id !== 'modal-spotlight-search') closeModal(m.id);
  });
  closeViewPanel();
  closeLBIfOpen();
  closeDetailIfOpen();
  setSpotlightScope(initialScope || S.searchScope || 'all');
  const inp = $('#spotlight-input');
  const topVal = $('#f-search')?.value || '';
  const val = topVal || S.searchText || '';
  if (inp) {
    inp.value = val;
    updateSpotlightClearBtn();
  }
  renderSpotlightRecents();
  renderSpotlightTags();
  if (!(S.tagList || []).length) {
    refreshTags().then(renderSpotlightTags);
  }
  if (val.trim()) {
    triggerLiveSearch(val, currentSpotlightScope);
  } else {
    const liveResults = $('#spotlight-live-results');
    const defaultBody = $('#spotlight-default-body');
    if (liveResults) {
      liveResults.hidden = true;
      liveResults.innerHTML = '';
    }
    if (defaultBody) defaultBody.hidden = false;
  }
  openModal('modal-spotlight-search');
  if (inp) {
    inp.focus();
    inp.select();
  }
  railSync();
}

function closeSpotlightSearch() {
  closeModal('modal-spotlight-search');
  railSync();
}

function initSpotlightSearch() {
  const inp = $('#spotlight-input');
  const clearBtn = $('#spotlight-clear');
  const scopesEl = $('#spotlight-scopes');
  const recentsEl = $('#spotlight-recents');
  const clearHistBtn = $('#spotlight-clear-history');
  const tagsEl = $('#spotlight-tags');
  const liveResults = $('#spotlight-live-results');

  if (liveResults) {
    liveResults.addEventListener('click', (e) => {
      const item = e.target.closest('.spotlight-result-item');
      if (item) selectSpotlightItem(item);
    });
  }

  if (inp) {
    inp.addEventListener('input', (e) => {
      if (e.isComposing) return;
      detectSpotlightPrefix();
      updateSpotlightClearBtn();
      triggerLiveSearch(inp.value, currentSpotlightScope);
    });
    inp.addEventListener('compositionend', () => {
      detectSpotlightPrefix();
      updateSpotlightClearBtn();
      triggerLiveSearch(inp.value, currentSpotlightScope);
    });
    inp.addEventListener('search', () => {
      updateSpotlightClearBtn();
      triggerLiveSearch(inp.value, currentSpotlightScope);
    });
    inp.addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      e.stopPropagation();
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        const lr = $('#spotlight-live-results');
        if (lr && !lr.hidden) {
          const items = lr.querySelectorAll('.spotlight-result-item');
          if (items.length) {
            const nextIdx = spotlightNavIndex < 0 ? 0 : (spotlightNavIndex + 1) % items.length;
            updateSpotlightNav(items, nextIdx);
          }
        }
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        const lr = $('#spotlight-live-results');
        if (lr && !lr.hidden) {
          const items = lr.querySelectorAll('.spotlight-result-item');
          if (items.length) {
            const prevIdx = spotlightNavIndex <= 0 ? items.length - 1 : (spotlightNavIndex - 1 + items.length) % items.length;
            updateSpotlightNav(items, prevIdx);
          }
        }
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const lr = $('#spotlight-live-results');
        if (lr && !lr.hidden) {
          const items = lr.querySelectorAll('.spotlight-result-item');
          if (items.length && spotlightNavIndex >= 0 && spotlightNavIndex < items.length) {
            selectSpotlightItem(items[spotlightNavIndex]);
            return;
          }
        }
        detectSpotlightPrefix();
        const v = inp.value.trim();
        applySearch(v, currentSpotlightScope);
        if (v) saveRecentSearch(v, currentSpotlightScope);
        closeSpotlightSearch();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        closeSpotlightSearch();
      } else if (e.key === 'Backspace' && !inp.value && currentSpotlightScope !== 'all') {
        setSpotlightScope('all');
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (inp) {
        inp.value = '';
        updateSpotlightClearBtn();
        triggerLiveSearch('', currentSpotlightScope);
        inp.focus();
      }
    });
  }

  if (scopesEl) {
    scopesEl.addEventListener('click', (e) => {
      const btn = e.target.closest('.s-scope');
      if (!btn) return;
      setSpotlightScope(btn.dataset.scope);
      if (inp) {
        inp.focus();
        if (inp.value.trim()) {
          triggerLiveSearch(inp.value, currentSpotlightScope);
        }
      }
    });
  }

  if (recentsEl) {
    const handleRecent = (target) => {
      const delBtn = target.closest('.spotlight-chip-del');
      if (delBtn) {
        deleteRecentSearch(delBtn.dataset.delQ);
        if (inp) inp.focus();
        return;
      }
      const chip = target.closest('.spotlight-chip');
      if (chip) {
        const q = chip.dataset.searchQ;
        const scope = chip.dataset.searchScope || 'all';
        if (inp) inp.value = q;
        setSpotlightScope(scope);
        applySearch(q, scope);
        saveRecentSearch(q, scope);
        closeSpotlightSearch();
      }
    };
    recentsEl.addEventListener('click', (e) => {
      e.stopPropagation();
      handleRecent(e.target);
    });
    recentsEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        e.stopPropagation();
        handleRecent(e.target);
      }
    });
  }

  if (clearHistBtn) {
    clearHistBtn.addEventListener('click', () => {
      clearRecentSearches();
    });
  }

  if (tagsEl) {
    tagsEl.addEventListener('click', (e) => {
      const chip = e.target.closest('.spotlight-tag-chip');
      if (chip) {
        const tagName = chip.dataset.tagName;
        if (inp) inp.value = tagName;
        setSpotlightScope('tag');
        applySearch(tagName, 'tag');
        saveRecentSearch(tagName, 'tag');
        closeSpotlightSearch();
      }
    });
  }

  document.querySelectorAll('.syntax-hint').forEach((hint) => {
    hint.addEventListener('click', () => {
      const syntax = hint.dataset.syntax;
      if (syntax === '@') setSpotlightScope('author');
      else if (syntax === '#') setSpotlightScope('tag');
      else if (syntax === '//') setSpotlightScope('comment');
      if (inp) {
        inp.focus();
        if (inp.value.trim()) {
          triggerLiveSearch(inp.value, currentSpotlightScope);
        }
      }
    });
  });

  // 顶栏搜索框与聚光灯联动
  $('#f-search')?.addEventListener('focus', (e) => {
    e.target.blur();
    openSpotlightSearch();
  });
  $('#f-search')?.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    e.target.blur();
    openSpotlightSearch();
  });
  $('#f-search')?.addEventListener('input', () => {
    const val = $('#f-search').value;
    openSpotlightSearch();
    const sinp = $('#spotlight-input');
    if (sinp) {
      sinp.value = val;
      triggerLiveSearch(val, currentSpotlightScope);
    }
  });
  $('.searchwrap')?.addEventListener('click', (e) => {
    if (e.target.closest('#f-search-clear')) {
      e.stopPropagation();
      applySearch('', 'all');
      return;
    }
    $('#f-search')?.blur();
    openSpotlightSearch();
  });
  $('#f-search-clear')?.addEventListener('click', (e) => {
    e.stopPropagation();
    applySearch('', 'all');
  });

  // 阻断聚光灯弹窗滚动穿透到后方主页面
  const spModal = $('#modal-spotlight-search');
  if (spModal) {
    spModal.addEventListener('wheel', (e) => {
      const scrollable = spModal.querySelector('.spotlight-body');
      if (!scrollable || !e.target.closest('.spotlight-body')) {
        e.preventDefault();
        return;
      }
      const { scrollTop, scrollHeight, clientHeight } = scrollable;
      const delta = e.deltaY;
      if (delta > 0 && scrollTop + clientHeight >= scrollHeight - 1) {
        e.preventDefault();
      } else if (delta < 0 && scrollTop <= 0) {
        e.preventDefault();
      }
    }, { passive: false });
  }
}

loadStatus().catch((e) => toast('状态加载失败：' + e.message)).then(() => load(true));
refreshTags();   // 标签清单首载（失败静默，筛选行给引导文案）
initSpotlightSearch();
renderActiveSearchChip();

/* ---------- 左右轨与右上胶囊（rail.js）集成钩子 ----------
   轨道只做壳与交互，业务逻辑全部走这里暴露的最小能力面；不复制状态、不合成事件。 */
window.pwRail = {
  S,
  switchStatusView,
  startQuickTriage,
  applySearch: (v, scope) => applySearch(v, scope),
  openSpotlightSearch: (scope) => openSpotlightSearch(scope),
  closeSpotlightSearch: () => closeSpotlightSearch(),
  syncUrl,
  resetFilters,
  renderViewPanel,
  toggleViewPanel,
  viewState,
  feedColCount,        // 列栈公式单源：rail.js 的自愈对照不再复制公式魔数
  relayoutFeed: () => { colWidth = 0; ensureFeedCols(); },
  load: (reset) => load(reset),
  openDetail,
  authorViewOpen: (uid, hint) => authorView.open(uid, hint),
  toast,
  slideshowOpen: () => slideshow.open(),
  notesOpen: () => notes.open(),
  chronoActive: () => timeline.active(),
  setChrono: (on) => timeline.setLayout(on),
  scrollTop: scrollFeedTop,
  openSettings: () => settings.openSettings(),
  setSelectMode: (on) => setSelectMode(on),
};
