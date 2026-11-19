/* PWALL 报刊亭 · 博主管理大厅：
   数据来自 /api/authors/kiosk（统计/节奏/封面内页图/缓存签名/拼音索引/分组/关注状态）；
   内页打开时若签名缺失，再补拉 /api/authors/{key}/bio（服务端带 meta 缓存）；
   支持拼音与关键词检索、A–Z/# 首字母快跳、按来源/分组筛选、排序、取消关注与 xg: 本地分组调整。 */
const S = {
  shelf: [], groups: [], role: 'viewer', open: -1, bio: new Map(),
  query: '', alpha: 'all', group: 'all', sort: 'recent', confirmUnfollowKey: '',
};
let toastTimer = 0;
let unfollowTimer = 0;
const ALPHA_KEYS = ['all', ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ', '#'];
const REDUCE = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
const ACCENTS = ['#8a5a44', '#5d6e46', '#b23a24', '#4a6fa0', '#b98a2e', '#46708a',
  '#9a7a2e', '#4e8a72', '#6f5fa8', '#4f7d4a', '#3d7296', '#a85268'];

const $ = (sel) => document.querySelector(sel);
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const nl2br = (s) => esc(s).replace(/\n/g, '<br>');
export const safeMedia = (u) => {
  const s = String(u ?? '').trim();
  if (!s) return '';
  if (s.startsWith('avatars/')) return '/media/' + s;
  return /^(\/(?!\/)|https?:\/\/)/i.test(s) ? s : '';
};

function ksToast(msg) {
  const el = $('#ks-toast');
  if (!el) return;
  clearTimeout(toastTimer);
  el.textContent = msg;
  el.hidden = false;
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

async function ksApi(url, method = 'GET', body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.detail || j.msg || `请求失败 (${r.status})`);
  return j;
}

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
  const thumb = safeMedia(d.thumbs && d.thumbs[0]);
  if (thumb) return `<img loading="lazy" decoding="async" src="${esc(thumb)}" alt="${esc(mastName(d.author))}最新一期封面">`;
  const av = safeMedia(d.avatar);
  if (av) return `<img loading="lazy" decoding="async" src="${esc(av)}" alt="${esc(mastName(d.author))}头像作封面">`;
  return `<span style="display:flex;align-items:center;justify-content:center;height:100%;color:#7a7263;font-size:11px;letter-spacing:.2em;">NO COVER</span>`;
}

/* 个别缩略图文件可能已缺失（缺图重试机制覆盖前）——加载失败换 NO COVER 占位 */
if (typeof document !== 'undefined') {
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
}

function buildTicker(shelf, maxCover) {
  const items = [`本期上架 <em>${shelf.length} 刊</em>`];
  const maxN = maxCover || shelf.reduce((a, b) => ((Number(b.n) || 0) > (Number(a?.n) || -1) ? b : a), null);
  if (maxN && maxN.n >= 200) {
    items.push(`特大号合订本 <em>${esc(mastName(maxN.author))}</em> 已出至第 <em>${esc(maxN.n)}</em> 期`);
  }
  const now = Date.now();
  const fresh = shelf.filter(d => {
    const ts = d.first_at ? Date.parse(d.first_at) : NaN;
    return Number.isFinite(ts) && now - ts < 90 * 864e5;
  });
  if (fresh.length) {
    items.push(`新刊创刊 <em>${fresh.slice(0, 3).map(d => esc(mastName(d.author))).join(' · ')}</em> 創刊號热卖中`);
  }
  const daily = shelf.filter(d => d.l30 >= 20).slice(0, 2);   // 大书架上限两条，跑马灯别刷屏
  for (const d of daily) {
    items.push(`連載日刊 <em>${esc(mastName(d.author))}</em> 近 30 天更新 <em>${esc(d.l30)}</em> 篇`);
  }
  const full = shelf.filter(d => d.n >= 20 && d.kept === d.n);
  if (full.length) {
    items.push(`全收藏金印 <em>${full.slice(0, 4).map(d => esc(mastName(d.author))).join(' · ')}</em>`);
  }
  const half = items.map(t => `<span>${t}</span>`).join('');
  $('#ks-ticker').innerHTML = half + half;      // 两份内容做无缝循环
}

let wallTiltBound = false;
function bindWallEvents(wall) {
  if (wallTiltBound || !wall) return;
  wallTiltBound = true;
  wall.addEventListener('click', (e) => {
    const b = e.target.closest('.ks-cover');
    if (b && b.dataset.idx != null) openSpread(Number(b.dataset.idx));
  });
  if (REDUCE) return;
  let tiltRaf = 0, activeBtn = null, lastX = 0, lastY = 0;
  const resetTilt = (b) => {
    if (!b) return;
    b.style.transform = '';
    b.style.removeProperty('--shine');
  };
  wall.addEventListener('pointermove', (e) => {
    const b = e.target.closest('.ks-cover');
    if (activeBtn && activeBtn !== b) resetTilt(activeBtn);
    activeBtn = b;
    if (!b) return;
    lastX = e.clientX;
    lastY = e.clientY;
    if (tiltRaf) return;
    tiltRaf = requestAnimationFrame(() => {
      tiltRaf = 0;
      if (!activeBtn) return;
      const r = activeBtn.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const px = (lastX - r.left) / r.width - 0.5;
      const py = (lastY - r.top) / r.height - 0.5;
      activeBtn.style.transform = `perspective(900px) rotateY(${(px * 4.8).toFixed(2)}deg) rotateX(${(-py * 3.6).toFixed(2)}deg) rotate(var(--rz))`;
      activeBtn.style.setProperty('--shine', (0.35 - Math.abs(px) * 0.3).toFixed(2));
    });
  });
  wall.addEventListener('pointerout', (e) => {
    const b = e.target.closest('.ks-cover');
    if (b && !b.contains(e.relatedTarget)) {
      if (activeBtn === b) activeBtn = null;
      resetTilt(b);
    }
  });
}

function inGroupFilter(d, grp) {
  if (!grp || grp === 'all') return true;
  if (grp === 'weibo' || grp === 'x') return d.source === grp;
  if (grp.startsWith('xg:')) {
    return (d.xg_groups || []).includes(grp) || (!d.followed && (d.groups || []).includes(grp));
  }
  return (d.groups || []).includes(grp);
}

function matchesAuthor(d, ignoreAlpha = false) {
  if (!inGroupFilter(d, S.group)) return false;
  if (!ignoreAlpha && S.alpha !== 'all' && (d.initial || '#') !== S.alpha) return false;
  const q = S.query.trim().toLowerCase();
  if (!q) return true;
  return (d.author || '').toLowerCase().includes(q)
    || (d.pinyin || '').toLowerCase().includes(q)
    || (d.handle || '').toLowerCase().includes(q)
    || String(d.uid || '').toLowerCase().includes(q);
}

function sortedItems(items) {
  const arr = [...items];
  if (S.sort === 'pinyin') {
    arr.sort((a, b) => String(a.pinyin || a.author).localeCompare(String(b.pinyin || b.author))
      || String(a.key).localeCompare(String(b.key)));
  } else if (S.sort === 'count') {
    arr.sort((a, b) => (Number(b.n) || 0) - (Number(a.n) || 0)
      || String(b.last_at || '').localeCompare(String(a.last_at || '')));
  } else if (S.sort === 'rate') {
    const rate = (x) => (x.n ? (x.kept || 0) / x.n : 0);
    arr.sort((a, b) => rate(b) - rate(a) || (Number(b.n) || 0) - (Number(a.n) || 0));
  } else {
    arr.sort((a, b) => String(b.last_at || '').localeCompare(String(a.last_at || '')));
  }
  return arr;
}

function renderGroupsBar() {
  const el = $('#ks-groups');
  if (!el) return;
  const chips = [
    { id: 'all', name: '全部', n: S.shelf.length },
    { id: 'weibo', name: '微博', n: S.shelf.filter((d) => d.source === 'weibo').length },
    { id: 'x', name: 'X', n: S.shelf.filter((d) => d.source === 'x').length },
    ...S.groups.map((g) => ({
      id: g.list_id,
      name: g.is_local ? `组·${g.name}` : g.name,
      n: S.shelf.filter((d) => inGroupFilter(d, g.list_id)).length,
    })),
  ];
  el.innerHTML = chips.map((c) =>
    `<button type="button" class="ks-pill${S.group === c.id ? ' on' : ''}" data-ks-group="${esc(c.id)}">${esc(c.name)}<span class="cnt">${c.n}</span></button>`
  ).join('');
}

function renderAlphaBar() {
  const el = $('#ks-alpha');
  if (!el) return;
  const avail = new Set(S.shelf.filter((d) => matchesAuthor(d, true)).map((d) => d.initial || '#'));
  el.innerHTML = ALPHA_KEYS.map((k) => {
    const label = k === 'all' ? '全部' : k;
    const disabled = k !== 'all' && !avail.has(k);
    return `<button type="button" class="ks-pill${S.alpha === k ? ' on' : ''}" data-ks-alpha="${esc(k)}"${disabled ? ' disabled' : ''}>${esc(label)}</button>`;
  }).join('');
}

function paint() {
  const wall = $('#ks-wall');
  bindWallEvents(wall);
  renderGroupsBar();
  renderAlphaBar();
  const sortEl = $('#ks-sort');
  if (sortEl) {
    sortEl.querySelectorAll('[data-ks-sort]').forEach((b) => b.classList.toggle('on', b.dataset.ksSort === S.sort));
  }
  const newGrpForm = $('#ks-newgrp-form');
  if (newGrpForm) newGrpForm.hidden = S.role !== 'admin';

  wall.innerHTML = '';
  const idxMap = new Map(S.shelf.map((d, idx) => [d.key, idx]));
  const visible = sortedItems(S.shelf.filter((d) => matchesAuthor(d)));
  const isFiltered = visible.length !== S.shelf.length;
  const sortNames = { recent: '按最近到货排序', pinyin: '按拼音 A–Z 排序', count: '按总期数排序', rate: '按保留率排序' };
  $('#ks-shelf-meta').textContent = isFiltered
    ? `显示 ${visible.length} / ${S.shelf.length} 位博主 · ${sortNames[S.sort] || ''}`
    : `收录 ${S.shelf.length} 位博主 · ${sortNames[S.sort] || '按最近一期到货排序'}`;
  const maxCover = S.shelf.reduce((a, b) => ((Number(b.n) || 0) > (Number(a?.n) || -1) ? b : a), null);
  const maxN = Number(maxCover?.n) || 0;
  buildTicker(S.shelf, maxCover);
  if (!S.shelf.length) {
    wall.innerHTML = '<div class="ks-counter-empty" style="grid-column:1/-1;">书架空空——先去抓几期回来</div>';
    return;
  }
  if (!visible.length) {
    wall.innerHTML = '<div class="ks-counter-empty" style="grid-column:1/-1;">没有符合条件的博主——换个关键词或分组试试</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  visible.forEach((d) => {
    const i = idxMap.get(d.key) ?? 0;
    const id = identity(d, i);
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `ks-cover${!d.followed && d.follow_id ? ' is-unfollowed' : ''}`;
    b.dataset.idx = String(i);
    b.style.setProperty('--acc', id.acc);
    b.style.setProperty('--rz', (((i * 7) % 5) - 2) * 0.6 + 'deg');
    b.setAttribute('aria-label', `打开杂志：${mastName(d.author)}，第 ${d.n} 期`);
    b.innerHTML = `
      <span class="ks-cover-inner">
        <span class="ks-cover-top">
          <span class="ks-mast" data-font="${esc(id.font)}">${esc(mastName(d.author))}</span>
          <span class="ks-mast-sub">${esc((d.handle ? '@' + d.handle : (d.source === 'x' ? 'X · ' : 'WB · ') + d.uid).toUpperCase().slice(0, 26))}</span>
        </span>
        <span class="ks-photo">${coverImg(d)}</span>
        <span class="ks-lines">${esc(id.quote || '本期无文字，只看图。')}</span>
        <span class="ks-cover-foot">
          <span class="ks-vol">VOL.${esc(d.n)}</span>
          <span class="ks-bar" aria-hidden="true"></span>
          <span class="ks-stamp">${d.source === 'x' ? 'X PRESS' : 'WB PRESS'}</span>
        </span>
      </span>
      <span class="ks-tag">${esc(id.cadence)}</span>
      ${!d.followed && d.follow_id ? '<span class="ks-cover-badge">未单独关注</span>' : ''}
      ${d.n === maxN && d.n >= 200
        ? `<span class="ks-burst">合订本<br>${esc(d.n)}期</span>` : ''}`;
    frag.appendChild(b);
  });
  wall.appendChild(frag);
}

function phImg(d) {
  const av = safeMedia(d.avatar);
  return av
    ? `<img loading="lazy" decoding="async" src="${esc(av)}" alt="头像补位">`
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
    ? `<p>${nl2br(bio)}</p>`
    : '<p class="empty">「平台简介拉取中——本期专访进行中。」</p>';
}

function spreadManageHtml(d) {
  const isAdmin = S.role === 'admin';
  const groupNames = new Map(S.groups.map((g) => [g.list_id, g]));
  const upstreamBadges = (d.groups || [])
    .map((lid) => groupNames.get(lid))
    .filter((g) => g && !g.is_local)
    .map((g) => `<a class="ks-gchip readonly" href="/?group=${encodeURIComponent(g.list_id)}" title="去主相册看该列表">${esc(g.name)} ↗</a>`)
    .join('');

  const localGroups = S.groups.filter((g) => g.is_local);
  const canEditXg = isAdmin && d.follow_id?.startsWith('xu:') && d.followed;
  const xgChips = localGroups.map((g) => {
    const on = (d.xg_groups || []).includes(g.list_id);
    if (!canEditXg) {
      return on ? `<a class="ks-gchip on" href="/?group=${encodeURIComponent(g.list_id)}">${esc(g.name)} ↗</a>` : '';
    }
    return `<button type="button" class="ks-gchip${on ? ' on' : ''}" data-ks-toggle-xg="${esc(g.list_id)}" aria-pressed="${on}">${on ? '✓ ' : '+ '}${esc(g.name)}</button>`;
  }).filter(Boolean).join('');

  let followBtn = '';
  if (isAdmin && d.follow_id) {
    if (d.followed) {
      const confirming = S.confirmUnfollowKey === d.key;
      followBtn = `<button type="button" class="ks-link-btn danger${confirming ? ' confirming' : ''}" id="ks-unfollow-btn">${confirming ? '再次点击确认取消关注（已存图保留）' : '取消关注'}</button>`;
    } else {
      followBtn = `<button type="button" class="ks-link-btn" id="ks-refollow-btn">+ 单独关注此博主</button>`;
    }
  }
  const origUrl = d.source === 'x'
    ? (d.handle ? `https://x.com/${encodeURIComponent(d.handle)}` : '')
    : (d.uid ? `https://weibo.com/u/${encodeURIComponent(d.uid)}` : '');

  return `
    <div class="ks-spread-actions">
      <a class="ks-link-btn primary" href="/?author=${encodeURIComponent(d.key)}">在相册查看全部帖 ↗</a>
      ${origUrl ? `<a class="ks-link-btn" href="${esc(origUrl)}" target="_blank" rel="noopener noreferrer">原站主页 ↗</a>` : ''}
      ${followBtn}
    </div>
    <div class="ks-manage">
      <div class="ks-manage-head">
        <b>所属分组 · GROUPS</b>
        <span>${d.followed ? '已单独关注' : (upstreamBadges ? '来自列表采集' : '仅存量记录')}</span>
      </div>
      <div class="ks-grp-pills">
        ${upstreamBadges}
        ${xgChips || (!upstreamBadges ? '<span style="color:#7a7263;">未归入任何分组</span>' : '')}
      </div>
      ${canEditXg ? `<form class="ks-inline-add" id="ks-spread-addgrp">
        <input id="ks-spread-grpname" type="text" placeholder="新建分组并加入…" maxlength="24" autocomplete="off">
        <button type="submit" class="ks-link-btn">+ 新建并加入</button>
      </form>` : (isAdmin && d.follow_id?.startsWith('xu:') && !d.followed
        ? '<div style="margin-top:6px;color:#7a7263;">点上方「单独关注此博主」后可加入本地分组</div>' : '')}
    </div>`;
}

function bindSpreadActions(d, slot) {
  const unfollowBtn = $('#ks-unfollow-btn');
  if (unfollowBtn) {
    unfollowBtn.addEventListener('click', async () => {
      if (S.confirmUnfollowKey !== d.key) {
        S.confirmUnfollowKey = d.key;
        clearTimeout(unfollowTimer);
        unfollowTimer = setTimeout(() => {
          if (S.confirmUnfollowKey === d.key) {
            S.confirmUnfollowKey = '';
            if (S.open === slot) openSpread(slot);
          }
        }, 4000);
        openSpread(slot);
        return;
      }
      S.confirmUnfollowKey = '';
      clearTimeout(unfollowTimer);
      unfollowBtn.disabled = true;
      try {
        await ksApi('/api/groups/' + encodeURIComponent(d.follow_id), 'DELETE');
        d.followed = false;
        ksToast(`已取消关注 ${mastName(d.author)}（已抓图片保留在相册中）`);
        paint();
        openSpread(slot);
      } catch (e) { ksToast(e.message); unfollowBtn.disabled = false; }
    });
  }
  const refollowBtn = $('#ks-refollow-btn');
  if (refollowBtn) {
    refollowBtn.addEventListener('click', async () => {
      refollowBtn.disabled = true;
      refollowBtn.textContent = '校验并关注中…';
      try {
        const label = d.source === 'x' && d.handle ? '@' + d.handle : (d.author || d.uid);
        const r = await ksApi('/api/groups', 'POST', { list_id: d.follow_id, name: label });
        d.followed = true;
        ksToast(r.msg || `已重新关注 ${mastName(d.author)}`);
        paint();
        openSpread(slot);
      } catch (e) { ksToast(e.message); refollowBtn.disabled = false; refollowBtn.textContent = '+ 单独关注此博主'; }
    });
  }
  const pills = document.querySelectorAll('#ks-counter-body [data-ks-toggle-xg]');
  pills.forEach((btn) => btn.addEventListener('click', async () => {
    const gid = btn.dataset.ksToggleXg;
    const on = (d.xg_groups || []).includes(gid);
    btn.disabled = true;
    try {
      if (on) {
        await ksApi(`/api/groups/${encodeURIComponent(gid)}/members/${encodeURIComponent(d.follow_id)}`, 'DELETE');
        d.xg_groups = (d.xg_groups || []).filter((x) => x !== gid);
        ksToast('已移出分组');
      } else {
        await ksApi(`/api/groups/${encodeURIComponent(gid)}/members`, 'POST', { member: d.follow_id });
        d.xg_groups = [...new Set([...(d.xg_groups || []), gid])];
        d.groups = [...new Set([...(d.groups || []), gid])];
        ksToast('已加入分组（历史帖已归组）');
      }
      paint();
      openSpread(slot);
    } catch (e) { ksToast(e.message); btn.disabled = false; }
  }));
  const addGrpForm = $('#ks-spread-addgrp');
  if (addGrpForm) {
    addGrpForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const inp = $('#ks-spread-grpname');
      const name = inp?.value.trim();
      if (!name) { inp?.focus(); return; }
      try {
        const r = await ksApi('/api/groups', 'POST', { list_id: 'xg:', name });
        const lid = r.list_id;
        if (lid) {
          S.groups.push({ list_id: lid, name, source: 'x', is_local: true });
          await ksApi(`/api/groups/${encodeURIComponent(lid)}/members`, 'POST', { member: d.follow_id });
          d.xg_groups = [...new Set([...(d.xg_groups || []), lid])];
          d.groups = [...new Set([...(d.groups || []), lid])];
        }
        ksToast(`已新建分组「${name}」并归入`);
        paint();
        openSpread(slot);
      } catch (err) { ksToast(err.message); }
    });
  }
}

function openSpread(i) {
  const d = S.shelf[i];
  if (!d) return;
  const id = identity(d, i);
  const rate = d.n ? Math.round(d.kept / d.n * 100) : 0;
  const plat = d.source === 'x' ? 'X' : '微博';
  const imgs = [...(d.thumbs || []).slice(0, 4)];
  while (imgs.length < 4) imgs.push(null);      // 不足四格用头像/占位补齐版面
  const heroSrc = safeMedia(d.avatar || d.thumbs?.[0]);
  const safeUid = (d.source || 'weibo').toUpperCase() + ' · ' + (d.uid ?? '');
  S.open = i;
  $('#ks-counter-body').innerHTML = `
    <div class="ks-spread" style="--acc:${esc(id.acc)}" role="group" aria-label="${esc(mastName(d.author))} 专栏内页">
      <div class="ks-page left">
        <div class="ks-rhead"><span>PWALL 报刊亭</span><span>专栏 · FEATURED</span><span>第 ${esc(d.n)} 期</span></div>
        <div class="ks-hero">
          ${heroSrc ? `<img decoding="async" src="${esc(heroSrc)}" alt="${esc(mastName(d.author))} 头像">` : '<span style="width:64px;height:64px;border-radius:6px;background:#e8e1d2;"></span>'}
          <div>
            <span class="ks-mast-sub">${esc((d.handle ? '@' + d.handle : safeUid).toUpperCase().slice(0, 26))}</span>
            <h3>${esc(mastName(d.author))}</h3>
            <span class="ks-heroline">${esc(plat)} 特派记者站 · 驻刊 ${esc((d.first_at || '').slice(0, 4) || '—')} 年起</span>
          </div>
        </div>
        <div class="ks-bio">
          <b>主笔简介 · AUTHOR BIO</b>
          <div id="ks-bio-${esc(i)}"></div>
        </div>
        <table class="ks-meta">
          <tr><td>总期数</td><td>第 ${esc(d.n)} 期</td></tr>
          <tr><td>本期好评</td><td>${esc(d.kept)} / ${esc(d.n)}（保留率 ${esc(rate)}%）</td></tr>
          <tr><td>近 30 天</td><td>更新 ${esc(d.l30)} 篇</td></tr>
          <tr><td>最新到货</td><td>${esc(d.last_at)}</td></tr>
          <tr><td>创刊日期</td><td>${esc(d.first_at)}</td></tr>
        </table>
        ${spreadManageHtml(d)}
        <div class="ks-pageno">— 内页 01 —</div>
      </div>
      <div class="ks-page right">
        <div class="ks-rhead"><span>作品版面</span><span>INSIDE PREVIEW</span><span>${esc(d.last_at)}</span></div>
        <div class="ks-grid">
          ${imgs.map((s, j) => {
            const safe = safeMedia(s);
            return safe ? `<img loading="lazy" decoding="async" src="${esc(safe)}" alt="内页图 ${j + 1}">` : phImg(d);
          }).join('')}
        </div>
        <div class="ks-quote"><b>卷首语</b>${esc(id.quote || '本期无文字，只看图。')}</div>
        <div class="ks-pagefoot"><span>${d.source === 'x' ? 'X PRESS' : 'WB PRESS'}</span><span>第 ${esc(d.n)} 期 · ${esc(d.last_at)}</span></div>
        <div class="ks-pageno">— 内页 02 —</div>
      </div>
    </div>
    <div class="ks-close">
      <button type="button" class="ks-btn" id="ks-close-btn">合上杂志 ✕</button>
    </div>`;
  paintBio(d, i);
  if (!d.bio) ensureBio(d, i);
  $('#ks-close-btn').addEventListener('click', closeSpread);
  bindSpreadActions(d, i);
  $('#ks-counter').scrollIntoView({ behavior: REDUCE ? 'auto' : 'smooth', block: 'nearest' });
}

function closeSpread() {
  S.open = -1;
  S.confirmUnfollowKey = '';
  $('#ks-counter-body').innerHTML =
    '<div class="ks-counter-empty">点击墙上任意一本杂志，翻开它的专栏内页与管理</div>';
}

if (typeof document !== 'undefined') {
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S.open >= 0) closeSpread();
  });

  const searchInp = $('#ks-search');
  const clearBtn = $('#ks-search-clear');
  if (searchInp) {
    const onSearch = () => {
      S.query = searchInp.value;
      if (clearBtn) clearBtn.hidden = !S.query;
      paint();
    };
    searchInp.addEventListener('input', onSearch);
    searchInp.addEventListener('search', onSearch);
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      S.query = '';
      if (searchInp) { searchInp.value = ''; searchInp.focus(); }
      clearBtn.hidden = true;
      paint();
    });
  }
  $('#ks-groups')?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-ks-group]');
    if (!b) return;
    S.group = b.dataset.ksGroup;
    S.alpha = 'all';
    paint();
  });
  $('#ks-alpha')?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-ks-alpha]');
    if (!b || b.disabled) return;
    const k = b.dataset.ksAlpha;
    S.alpha = (k !== 'all' && S.alpha === k) ? 'all' : k;
    paint();
  });
  $('#ks-sort')?.addEventListener('click', (e) => {
    const b = e.target.closest('[data-ks-sort]');
    if (!b) return;
    S.sort = b.dataset.ksSort;
    paint();
  });
  $('#ks-newgrp-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const inp = $('#ks-newgrp-input');
    const name = inp?.value.trim();
    if (!name) { inp?.focus(); return; }
    try {
      const r = await ksApi('/api/groups', 'POST', { list_id: 'xg:', name });
      if (r.list_id) S.groups.push({ list_id: r.list_id, name, source: 'x', is_local: true });
      inp.value = '';
      ksToast(`已新建分组「${name}」`);
      paint();
      if (S.open >= 0) openSpread(S.open);
    } catch (err) { ksToast(err.message); }
  });

  let shelfLoading = false;
  async function loadShelf() {
    if (shelfLoading) return;
    shelfLoading = true;
    $('#ks-wall').innerHTML = '<div class="ks-counter-empty" style="grid-column:1/-1;">书架加载中…</div>';
    try {
      const r = await fetch('/api/authors/kiosk');
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      if (!j?.ok) throw new Error('bad payload');
      S.shelf = j.shelf || [];
      S.groups = j.groups || [];
      S.role = j.role || 'viewer';
      paint();
    } catch (e) {
      $('#ks-wall').innerHTML = '<div class="ks-counter-empty" style="grid-column:1/-1;">书架暂时取不到，稍后再试。 <button type="button" class="ks-retry">重试</button></div>';
      $('#ks-wall .ks-retry').onclick = (ev) => { ev.currentTarget.disabled = true; loadShelf(); };
    } finally {
      shelfLoading = false;
    }
  }
  loadShelf();
}
