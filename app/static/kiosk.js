/* PWALL 报刊亭：博主杂志墙。
   数据来自 /api/authors/kiosk（纯本地聚合：统计/节奏/封面内页图/缓存签名）；
   内页打开时若签名缺失，再补拉 /api/authors/{key}/bio（服务端带 meta 缓存）。
   刊名/刊色/刊期均为确定性派生（同一书架每天长得一样），不引入任何配置。 */
const S = { shelf: [], open: -1, bio: new Map() };
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)').matches;
const ACCENTS = ['#8a5a44', '#5d6e46', '#b23a24', '#4a6fa0', '#b98a2e', '#46708a',
  '#9a7a2e', '#4e8a72', '#6f5fa8', '#4f7d4a', '#3d7296', '#a85268'];

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/\n/g, '<br>');

/* 刊名：多段名（如 NotValidSky/叶灵/…）优先取带汉字的一段，超长截断 */
function mastName(author) {
  const parts = String(author || '').split('/').map(s => s.trim()).filter(Boolean);
  const cjk = parts.find(p => /[\u4e00-\u9fff\u3040-\u30ff]/.test(p));
  let name = cjk || parts[0] || String(author || '佚名');
  if (!/[\u4e00-\u9fff\u3040-\u30ff]/.test(name)) {
    name = name.split(/\s+/)[0];               // 纯拉丁名只取首词
  }
  if (name.length > 10) {
    // 截断前先试「连续汉字前缀」：摩卡黑狗🔜Furst… → 摩卡黑狗，不带出 emoji/拉丁尾巴
    const run = (name.match(/^[\u4e00-\u9fff\u3040-\u30ff]{2,}/) || [''])[0];
    name = run.length >= 2 ? run : name.slice(0, 9) + '…';
  }
  return name;
}

function identity(d, i) {
  const acc = ACCENTS[i % ACCENTS.length];
  const cjk = /[\u4e00-\u9fff\u3040-\u30ff]/.test(mastName(d.author));
  const font = cjk ? (i % 4 === 3 ? 'hand' : 'serif')
    : (i % 2 ? 'latin' : 'latin-cap');
  const cadence = d.l30 >= 20 ? '日刊連載' : d.l30 >= 10 ? '週刊'
    : d.l30 >= 4 ? '半月刊' : d.l30 >= 1 ? '月刊' : (d.n >= 500 ? '合订本' : '季刊');
  const quote = String(d.text || '').replace(/https?:\/\/\S+/g, '')
    .replace(/\s+/g, ' ').trim().slice(0, 50);
  return { acc, font, cadence, quote };
}

function coverImg(d) {
  if (d.thumbs && d.thumbs[0]) return `<img loading="lazy" src="${esc(d.thumbs[0])}" alt="${esc(mastName(d.author))}最新一期封面">`;
  if (d.avatar) return `<img loading="lazy" src="${esc(d.avatar)}" alt="${esc(mastName(d.author))}头像作封面">`;
  return `<span style="display:flex;align-items:center;justify-content:center;height:100%;color:#7a7263;font-size:11px;letter-spacing:.2em;">NO COVER</span>`;
}

/* 个别缩略图文件可能已缺失（缺图重试机制覆盖前）——加载失败换 NO COVER 占位 */
document.addEventListener('error', (e) => {
  const img = e.target;
  if (img?.tagName === 'IMG' && img.closest('.ks-photo, .ks-grid')) {
    const ph = document.createElement('span');
    ph.className = img.closest('.ks-grid') ? 'ks-ph' : 'ks-photo';
    ph.style.cssText = 'display:flex;align-items:center;justify-content:center;color:#7a7263;font-size:10px;letter-spacing:.2em;width:100%;height:100%;';
    ph.textContent = 'NO IMAGE';
    img.replaceWith(ph);
  }
}, true);

function buildTicker(shelf) {
  const items = [`本期上架 <em>${shelf.length} 刊</em>`];
  const maxN = shelf.reduce((a, b) => (b.n > (a?.n ?? -1) ? b : a), null);
  if (maxN && maxN.n >= 200) {
    items.push(`特大号合订本 <em>${esc(mastName(maxN.author))}</em> 已出至第 <em>${maxN.n}</em> 期`);
  }
  const fresh = shelf.filter(d => Date.now() - Date.parse(d.first_at) < 90 * 864e5);
  if (fresh.length) {
    items.push(`新刊创刊 <em>${fresh.slice(0, 3).map(d => esc(mastName(d.author))).join(' · ')}</em> 創刊號热卖中`);
  }
  const daily = shelf.filter(d => d.l30 >= 20).slice(0, 2);   // 大书架上限两条，跑马灯别刷屏
  for (const d of daily) {
    items.push(`連載日刊 <em>${esc(mastName(d.author))}</em> 近 30 天更新 <em>${d.l30}</em> 篇`);
  }
  const full = shelf.filter(d => d.n >= 20 && d.kept === d.n);
  if (full.length) {
    items.push(`全收藏金印 <em>${full.slice(0, 4).map(d => esc(mastName(d.author))).join(' · ')}</em>`);
  }
  const half = items.map(t => `<span>${t}</span>`).join('');
  $('#ks-ticker').innerHTML = half + half;      // 两份内容做无缝循环
}

function paint() {
  const wall = $('#ks-wall');
  wall.innerHTML = '';
  $('#ks-shelf-meta').textContent =
    `收录 ${S.shelf.length} 位博主 · 按最近一期到货排序`;
  buildTicker(S.shelf);
  if (!S.shelf.length) {
    wall.innerHTML = '<div class="ks-counter-empty" style="grid-column:1/-1;">书架空空——先去抓几期回来</div>';
    return;
  }
  S.shelf.forEach((d, i) => {
    const id = identity(d, i);
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ks-cover';
    b.style.setProperty('--acc', id.acc);
    b.style.setProperty('--rz', (((i * 7) % 5) - 2) * 0.6 + 'deg');
    b.setAttribute('aria-label', `打开杂志：${mastName(d.author)}，第 ${d.n} 期`);
    b.innerHTML = `
      <span class="ks-cover-inner">
        <span class="ks-cover-top">
          <span class="ks-mast" data-font="${id.font}">${esc(mastName(d.author))}</span>
          <span class="ks-mast-sub">${esc((d.handle ? '@' + d.handle : (d.source === 'x' ? 'X · ' : 'WB · ') + d.uid).toUpperCase().slice(0, 26))}</span>
        </span>
        <span class="ks-photo">${coverImg(d)}</span>
        <span class="ks-lines">${esc(id.quote || '本期无文字，只看图。')}</span>
        <span class="ks-cover-foot">
          <span class="ks-vol">VOL.${d.n}</span>
          <span class="ks-bar" aria-hidden="true"></span>
          <span class="ks-stamp">${d.source === 'x' ? 'X PRESS' : 'WB PRESS'}</span>
        </span>
      </span>
      <span class="ks-tag">${id.cadence}</span>
      ${d.n === Math.max(...S.shelf.map(x => x.n)) && d.n >= 200
        ? `<span class="ks-burst">合订本<br>${d.n}期</span>` : ''}`;
    b.addEventListener('click', () => openSpread(i));
    if (!REDUCE) {
      b.addEventListener('pointermove', (e) => {
        const r = b.getBoundingClientRect();
        const px = (e.clientX - r.left) / r.width - 0.5;
        const py = (e.clientY - r.top) / r.height - 0.5;
        b.style.transform = `perspective(900px) rotateY(${(px * 4.8).toFixed(2)}deg) rotateX(${(-py * 3.6).toFixed(2)}deg) rotate(var(--rz))`;
        b.style.setProperty('--shine', (0.35 - Math.abs(px) * 0.3).toFixed(2));
      });
      b.addEventListener('pointerleave', () => {
        b.style.transform = '';
        b.style.removeProperty('--shine');
      });
    }
    wall.appendChild(b);
  });
}

function phImg(d) {
  return d.avatar
    ? `<img loading="lazy" src="${esc(d.avatar)}" alt="头像补位">`
    : `<span class="ks-ph">NO IMAGE</span>`;
}

async function ensureBio(d, slot) {
  if (d.bio || S.bio.has(d.key)) return paintBio(d, slot);
  paintBio(d, slot);                            // 先画占位态，拉到再原地更新
  try {
    const r = await fetch(`/api/authors/${encodeURIComponent(d.key)}/bio`);
    const j = await r.json();
    if (j?.ok && j.bio) {
      S.bio.set(d.key, j.bio);
      if (S.open === slot) paintBio(d, slot);
    }
  } catch (e) { /* 拉不到就保持占位态（未配 Cookie / 限流都常见） */ }
}

function paintBio(d, slot) {
  const box = document.getElementById(`ks-bio-${slot}`);
  if (!box) return;
  const bio = d.bio || S.bio.get(d.key);
  box.innerHTML = bio
    ? `<p>${esc(bio)}</p>`
    : '<p class="empty">「平台简介拉取中——本期专访进行中。」</p>';
}

function openSpread(i) {
  const d = S.shelf[i];
  const id = identity(d, i);
  const rate = d.n ? Math.round(d.kept / d.n * 100) : 0;
  const plat = d.source === 'x' ? 'X' : '微博';
  const imgs = [...(d.thumbs || []).slice(0, 4)];
  while (imgs.length < 4) imgs.push(null);      // 不足四格用头像/占位补齐版面
  S.open = i;
  $('#ks-counter-body').innerHTML = `
    <div class="ks-spread" style="--acc:${id.acc}" role="group" aria-label="${esc(mastName(d.author))} 专栏内页">
      <div class="ks-page left">
        <div class="ks-rhead"><span>PWALL 报刊亭</span><span>专栏 · FEATURED</span><span>第 ${d.n} 期</span></div>
        <div class="ks-hero">
          ${d.avatar || d.thumbs?.[0] ? `<img src="${esc(d.avatar || d.thumbs[0])}" alt="${esc(mastName(d.author))} 头像">` : '<span style="width:64px;height:64px;border-radius:6px;background:#e8e1d2;"></span>'}
          <div>
            <span class="ks-mast-sub">${esc((d.handle ? '@' + d.handle : d.source.toUpperCase() + ' · ' + d.uid).toUpperCase().slice(0, 26))}</span>
            <h3>${esc(mastName(d.author))}</h3>
            <span class="ks-heroline">${plat} 特派记者站 · 驻刊 ${esc(d.first_at.slice(0, 4))} 年起</span>
          </div>
        </div>
        <div class="ks-bio">
          <b>主笔简介 · AUTHOR BIO</b>
          <div id="ks-bio-${i}"></div>
        </div>
        <table class="ks-meta">
          <tr><td>总期数</td><td>第 ${d.n} 期</td></tr>
          <tr><td>本期好评</td><td>${d.kept} / ${d.n}（保留率 ${rate}%）</td></tr>
          <tr><td>近 30 天</td><td>更新 ${d.l30} 篇</td></tr>
          <tr><td>最新到货</td><td>${esc(d.last_at)}</td></tr>
          <tr><td>创刊日期</td><td>${esc(d.first_at)}</td></tr>
        </table>
        <div class="ks-pageno">— 内页 01 —</div>
      </div>
      <div class="ks-page right">
        <div class="ks-rhead"><span>作品版面</span><span>INSIDE PREVIEW</span><span>${esc(d.last_at)}</span></div>
        <div class="ks-grid">
          ${imgs.map((s, j) => s ? `<img loading="lazy" src="${esc(s)}" alt="内页图 ${j + 1}">` : phImg(d)).join('')}
        </div>
        <div class="ks-quote"><b>卷首语</b>${esc(id.quote || '本期无文字，只看图。')}</div>
        <div class="ks-pagefoot"><span>${d.source === 'x' ? 'X PRESS' : 'WB PRESS'}</span><span>第 ${d.n} 期 · ${esc(d.last_at)}</span></div>
        <div class="ks-pageno">— 内页 02 —</div>
      </div>
    </div>
    <div class="ks-close">
      <button type="button" class="ks-btn" id="ks-close-btn">合上杂志 ✕</button>
    </div>`;
  paintBio(d, i);
  if (!d.bio) ensureBio(d, i);
  $('#ks-close-btn').addEventListener('click', closeSpread);
  $('#ks-counter').scrollIntoView({ behavior: REDUCE ? 'auto' : 'smooth', block: 'nearest' });
}

function closeSpread() {
  S.open = -1;
  $('#ks-counter-body').innerHTML =
    '<div class="ks-counter-empty">点击墙上任意一本杂志，翻开它的专栏内页</div>';
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && S.open >= 0) closeSpread();
});

(async () => {
  try {
    const r = await fetch('/api/authors/kiosk');
    const j = await r.json();
    if (!j?.ok) throw new Error('bad payload');
    S.shelf = j.shelf || [];
    paint();
  } catch (e) {
    $('#ks-wall').innerHTML =
      '<div class="ks-counter-empty" style="grid-column:1/-1;">书架暂时取不到（/api/authors/kiosk 失败），稍后刷新再试</div>';
  }
})();
