/* PWALL 统计室：fetch /api/stats → 纯 DOM 渲染。
   独立页不引共享模块（模式同 kiosk.js）：esc 全转义、空态与失败态明确。 */
const $ = (s, el = document) => (el || document).querySelector(s);

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* 头像路径映射：与相册 avatarSrc 同规则（avatars/ 相对路径走 /media/） */
const avatarSrc = (url) => url && url.startsWith('avatars/') ? '/media/' + url : url;

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
    const av = a.avatar && avatarSrc(a.avatar)
      ? `<img src="${esc(avatarSrc(a.avatar))}" alt="" loading="lazy">`
      : `<span class="av-ph">${esc((a.author || '微')[0])}</span>`;
    return `<div class="st-author"><span class="rank">${i + 1}</span>${av}` +
      `<span class="nm"><b>${esc(a.author)}</b>` +
      `<span>保留 ${a.kept} · 收藏 ${a.favorites} · 共 ${a.n} 帖</span></span></div>`;
  }).join('');
}

function paintTop(rows) {
  const box = $('#st-top');
  if (!rows.length) { box.innerHTML = '<div class="st-empty" style="grid-column:1/-1">还没有点赞记录。</div>'; return; }
  box.innerHTML = rows.map((p) => {
    const media = p.thumb ? `<img src="/media/${esc(p.thumb)}" alt="" loading="lazy">`
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

(async () => {
  try {
    const r = await fetch('/api/stats');
    const j = await r.json();
    if (!j?.ok) throw new Error('bad payload');
    paintOverview(j.overview);
    paintChart(j.daily || []);
    paintAuthors(j.top_authors || []);
    paintTop(j.top_posts || []);
    paintReview(j.review);
  } catch (e) {
    $('#st-overview').innerHTML = '<div class="st-empty" style="grid-column:1/-1">统计暂时取不到，稍后再试。</div>';
  }
})();
