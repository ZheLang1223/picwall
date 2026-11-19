/* PWALL 统计室：fetch /api/stats → 纯 DOM 渲染。
   独立页不引共享模块（模式同 kiosk.js）：esc 全转义、空态与失败态明确。 */
const $ = (s, el = document) => (el || document).querySelector(s);

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* 头像路径映射：与相册 avatarSrc 同规则（avatars/ 相对路径走 /media/，外部仅放行 http(s)） */
const avatarSrc = (url) => {
  if (!url) return url;
  if (url.startsWith('avatars/')) return '/media/' + url;
  return /^https?:\/\//i.test(url) ? url : '';
};

function tile(n, label, cls = '') {
  return `<div class="st-tile ${cls}"><b>${esc(n)}</b><span>${esc(label)}</span></div>`;
}

function paintOverview(o) {
  $('#st-overview').innerHTML = [
    tile(o.total, '入库帖'),
    tile(o.pending, '待处理', 'hot'),
    tile(o.kept, '保留'),
    tile(o.discarded, '已删除'),
    tile(o.favorites, '收藏', 'gold'),
    tile(o.pics, '图片'),
    tile(o.comments, '批注'),
    tile(o.tags, '标签'),
  ].join('');
}

/* 近 30 天双柱：入库（蓝）为主柱，回看点赞（金）按同尺缩放叠在下方。
   高度按两系列各自最大值归一（两把尺，避免点赞被入库量淹没）；空日给占位底线 */
function paintChart(daily) {
  const wrap = $('#st-chart-wrap');
  if (!daily.length) return;   // 模板自带空态
  const days = [];
  const fmt = (d) => `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
  const today = new Date(Date.now() + 8 * 3600e3);
  for (let i = 29; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86400e3);
    days.push({ d: fmt(d), n: 0, likes: 0 });
  }
  const map = new Map(daily.map((x) => [x.d, x]));
  for (const slot of days) Object.assign(slot, map.get(slot.d) || {});
  const maxN = Math.max(1, ...days.map((x) => x.n));
  const maxK = Math.max(1, ...days.map((x) => x.likes));
  const cols = days.map((x) => {
    const hn = Math.round((x.n / maxN) * 78);
    const hk = x.likes ? Math.max(2, Math.round((x.likes / maxK) * 26)) : 0;
    const tip = `${x.d} · 入库 ${x.n} · 点赞 ${x.likes}`;
    return `<div class="st-col" title="${esc(tip)}"><span class="tip">${esc(tip)}</span>` +
      `<i class="bar-n" style="height:${hn}%"></i>` +
      (hk ? `<i class="bar-k" style="height:${hk}%"></i>` : '') + `</div>`;
  }).join('');
  wrap.innerHTML = `<div class="st-chart"${REDUCE ? '' : ' aria-hidden="true"'}>${cols}</div>` +
    `<div class="st-legend"><span><i style="background:#7db8ec"></i>入库（峰值 ${maxN}）</span>` +
    `<span><i style="background:rgba(232,198,114,.75)"></i>回看点赞（峰值 ${maxK}）</span></div>`;
}

function paintAuthors(rows) {
  const box = $('#st-authors');
  if (!rows.length) { box.innerHTML = '<div class="st-empty" style="grid-column:1/-1">还没有保留记录。</div>'; return; }
  box.innerHTML = rows.map((a, i) => {
    const avUrl = avatarSrc(a.avatar);
    const av = avUrl
      ? `<img src="${esc(avUrl)}" alt="" loading="lazy" decoding="async">`
      : `<span class="av-ph">${esc((a.author || '微')[0])}</span>`;
    return `<div class="st-author"><span class="rank">${i + 1}</span>${av}` +
      `<span class="nm"><b>${esc(a.author)}</b>` +
      `<span>保留 ${esc(a.kept)} · 收藏 ${esc(a.favorites)} · 共 ${esc(a.n)} 帖</span></span></div>`;
  }).join('');
}

function paintTop(rows) {
  const box = $('#st-top');
  if (!rows.length) { box.innerHTML = '<div class="st-empty" style="grid-column:1/-1">还没有点赞记录。</div>'; return; }
  box.innerHTML = rows.map((p) => {
    const media = p.thumb ? `<img src="/media/${esc(p.thumb)}" alt="" loading="lazy" decoding="async">`
      : `<div class="ph">无图</div>`;
    return `<div class="st-top">${media}<span class="kc">×${esc(p.keep_count || 1)}</span>` +
      `<span class="who">${esc(p.author)}</span></div>`;
  }).join('');
}

function paintReview(r) {
  $('#st-review').innerHTML = [
    tile(r.reaffirmed, '回看点赞', 'gold'),
    tile(r.shown, '回看出场'),
    tile(r.active_days, '活跃天数'),
  ].join('');
}

/* 加载/失败态：进 fetch 前五区先给「加载中…」（重试同样生效）；失败时总账给重试钮、
   其余四区降级「暂不可用」。stLoading 在途旗 + 重试钮点击即禁用，连点不叠加请求 */
const ST_WAIT = '<div class="st-empty" style="grid-column:1/-1">加载中…</div>';
const ST_NA = '<div class="st-empty" style="grid-column:1/-1">暂不可用</div>';
const chartEmpty = $('#st-chart-wrap').innerHTML;   // 模板自带空态：paintChart 空日早退依赖它

let stLoading = false;
async function loadStats() {
  if (stLoading) return;   // 在途忽略重复触发
  stLoading = true;
  for (const id of ['st-overview', 'st-chart-wrap', 'st-authors', 'st-top', 'st-review']) $('#' + id).innerHTML = ST_WAIT;
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    if (!j?.ok) throw new Error('bad payload');
    const daily = j.daily || [];
    if (!daily.length) $('#st-chart-wrap').innerHTML = chartEmpty;   // 归还模板空态，paintChart 空日早退后不留「加载中」
    paintOverview(j.overview);
    paintChart(daily);
    paintAuthors(j.top_authors || []);
    paintTop(j.top_posts || []);
    paintReview(j.review);
  } catch (e) {
    $('#st-overview').innerHTML = '<div class="st-empty" style="grid-column:1/-1">统计暂时取不到，稍后再试。 <button type="button" class="st-retry">重试</button></div>';
    for (const id of ['st-chart-wrap', 'st-authors', 'st-top', 'st-review']) $('#' + id).innerHTML = ST_NA;
    $('#st-overview .st-retry').onclick = (ev) => { ev.currentTarget.disabled = true; loadStats(); };
  } finally {
    stLoading = false;
  }
}

loadStats();
