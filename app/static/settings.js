/* 统一设置窗口:齿轮直达 modal-settings,左栏分区导航、右栏内容面板按需使用。
   各分区的数据渲染保留原弹窗逻辑;窗口生命周期(Esc/点外关闭/焦点返回)由主页面
   openModal/closeModal 统一管理,分区级定时器(抓取轮询)经 onLeave/closePane 归还。 */
export function initSettings({S,$,api,toast,esc,openModal,loadStatus,toggleFavorite,
                              getLBIndex,getDetailSid,refreshDetail,pollCrawl,hooks={}}) {
/* ---------- 个性化（pw_prefs → data-* 属性 → CSS 档位；写入走头部 pwSetPref） ---------- */
const getPrefs = () => (window.pwGetPrefs ? window.pwGetPrefs() : {});
function renderPrefs() {
  const p = getPrefs();
  const set = (id, val) => document.querySelectorAll('#' + id + ' button')
    .forEach((b) => {
      const on = b.dataset.v === val;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', String(on));
    });
  set('pref-theme', p.theme || 'system');
  set('pref-accent', p.accent || 'indigo');
  set('pref-bg', p.bg || 'cool');
  set('pref-font', p.font || 'std');
  set('pref-skin', ['swiss', 'brut', 'photo'].includes(p.skin) ? p.skin : 'classic');
  set('pref-x-blur', p['x-blur'] === 'off' ? 'off' : 'on');   // X 卡片默认模糊
  const sw = document.getElementById('pref-dblfav-sw');
  if (sw) sw.checked = !!p.dblfav;
  // 背景色调仅浅色生效：深色（含跟随系统的深色）下整行禁用并给提示；
  // 皮肤档位自带底色/强调色时接管对应行（photo 保留用户强调色，不禁）
  const darkNow = p.theme === 'dark' ||
    ((!p.theme || p.theme === 'system') && window.matchMedia('(prefers-color-scheme: dark)').matches);
  const skin = ['swiss', 'brut', 'photo'].includes(p.skin) ? p.skin : 'classic';
  const bgRow = document.getElementById('pref-bg-row');
  if (bgRow) bgRow.classList.toggle('disabled', darkNow || skin !== 'classic');
  const bgHint = document.getElementById('pref-bg-hint');
  if (bgHint) {
    bgHint.hidden = !(darkNow || skin !== 'classic');
    bgHint.textContent = skin !== 'classic' ? '当前皮肤自带背景色调' : '仅浅色模式生效';
  }
  const skinOwnsAccent = skin === 'swiss' || skin === 'brut';
  const accentRow = document.getElementById('pref-accent-row');
  if (accentRow) accentRow.classList.toggle('disabled', skinOwnsAccent);
  const accentHint = document.getElementById('pref-accent-hint');
  if (accentHint) accentHint.hidden = !skinOwnsAccent;
  set('pref-review', ['10', '30', '100'].includes(p.review) ? p.review : '30');
  set('pref-ss-interval', ['3', '5', '8'].includes(p['ss-interval']) ? p['ss-interval'] : '5');
  set('pref-ss-order', p['ss-order'] === 'rand' ? 'rand' : 'seq');
  set('pref-ss-comments', p['ss-comments'] === '0' ? 'off' : 'on');
  set('pref-shuffle-seed', p['shuffle-seed'] === 'daily' ? 'daily' : 'session');   // 随机档种子周期：每次打开(会话) / 每天一换
}
$('#spanes').addEventListener('click', (e) => {
  const b = e.target.closest('.seg button');
  if (!b || !b.parentElement.id.startsWith('pref-')) return;
  const key = b.parentElement.id.replace('pref-', '');
  // 放映评论默认开，关时存 '0'（ssPrefs 以 !== '0' 判定）
  const val = key === 'ss-comments' ? (b.dataset.v === 'on' ? '1' : '0')
    : b.dataset.v;
  window.pwSetPref(key, val);
  renderPrefs();
});
$('#pref-dblfav-sw').addEventListener('change', (e) => {
  window.pwSetPref('dblfav', e.target.checked ? '1' : '');
});

/* 双击图片收藏（个性化开关，默认关）：看大图的场景直接标收藏，不必瞄准小星星。
   生效范围 = 详情图阵 + 灯箱（feed 缩略图上双击难以瞄准，且单击已占用为开详情） */
document.getElementById('lightbox').addEventListener('dblclick', (e) => {
  if (!getPrefs().dblfav || !e.target.closest('.lb-stage')) return;
  if (!matchMedia('(hover: hover) and (pointer: fine)').matches) return;   // 触屏双击归缩放
  const c = S.images[getLBIndex()];
  if (c) toggleFavorite(c.p.sid);   // toggleFavorite 自带灯箱按钮/卡片同步
});
document.getElementById('detail').addEventListener('dblclick', (e) => {
  if (!getPrefs().dblfav || !e.target.closest('.dt-pics')) return;
  if (getDetailSid()) toggleFavorite(getDetailSid()).then(refreshDetail);
});

/* ---------- 分区导航：注册表 + 激活生命周期 ---------- */

const panes = {
  groups:     { onEnter: () => { renderMyGroups(); } },
  conn:       { onEnter: onConnEnter },
  crawl:      { onEnter: hooks.crawl?.onEnter, onLeave: hooks.crawl?.onLeave },
  batch:      { onEnter: hooks.batch?.onEnter },
  purge:      { onEnter: hooks.purge?.onEnter },
  appearance: { onEnter: renderPrefs },
  cards:      { onEnter: renderPrefs },
  slideshow:  { onEnter: renderPrefs },
  qr:         { onEnter: onQrEnter },
  devices:    { onEnter: hooks.devices?.onEnter },
  shortcuts:  {},
  about:      { onEnter: renderAbout },
};
let activePane = null;
/* 手机（≤640px）为推入式导航：根菜单 → 点行滑入面板 → ‹ 返回；桌面左栏导航不变 */
const isMobile = () => matchMedia('(max-width: 640px)').matches;
const shell = document.querySelector('.settings-shell');
function setHeadTitle(name) {   // 手机推入面板时标题栏显示分区名，其余场景恒为「设置」
  $('#settings-title').textContent = name || '设置';
}
function firstVisiblePane() {
  for (const b of document.querySelectorAll('.snav-item[data-pane]'))
    if (b.offsetParent !== null) return b.dataset.pane;   // 被设备权限 display:none 的项量不到位置
  return 'appearance';
}
function goRoot() {   // 返回根菜单：面板收起、分区态清空（含分区级定时器）
  panes[activePane]?.onLeave?.();
  document.querySelectorAll('.spane').forEach((s) => { s.hidden = true; });
  document.querySelectorAll('.snav-item[data-pane]').forEach((b) => {
    b.classList.remove('on'); b.removeAttribute('aria-current');
  });
  activePane = null;
  shell.classList.remove('in-pane');
  setHeadTitle();
}
function activate(id) {
  const item = document.querySelector(`.snav-item[data-pane="${id}"]`);
  if (!item || item.offsetParent === null) {   // 无此分区或被设备权限隐藏：落到首个可见分区
    const fallback = firstVisiblePane();
    if (id !== fallback) return activate(fallback);
    return;
  }
  panes[activePane]?.onLeave?.();
  if (activePane !== id) {
    document.querySelectorAll('.spane').forEach((s) => { s.hidden = s.id !== 'pane-' + id; });
    document.querySelectorAll('.snav-item[data-pane]').forEach((b) => {
      const on = b.dataset.pane === id;
      b.classList.toggle('on', on);
      if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');
    });
    activePane = id;
    try { localStorage.setItem('pw_settings_pane', id); } catch {}
  }
  if (isMobile()) {   // 手机：进入推入态，标题栏换分区名（桌面标题恒为「设置」）
    shell.classList.add('in-pane');
    setHeadTitle(item.querySelector('span')?.textContent);
  }
  panes[id]?.onEnter?.();   // 重复点同一分区也重跑：数据保持新鲜
}
function openSettings(id) {
  openModal('modal-settings');
  if (!id && isMobile()) {   // 手机：齿轮直达先看菜单，不自动跳上次分区；深链（带 id）仍直落面板
    goRoot();
    $('#snav .snav-item')?.focus();
    return;
  }
  let last = null;
  try { last = localStorage.getItem('pw_settings_pane'); } catch {}
  activate(id || last || firstVisiblePane());
  (document.querySelector('.snav-item.on') || $('#snav .snav-item'))?.focus();
}
$('#btn-settings').onclick = () => openSettings();
$('#settings-back').onclick = () => {   // 返回后焦点落回来源行，读屏上下文不丢
  const from = activePane;
  goRoot();
  (from && document.querySelector(`.snav-item[data-pane="${from}"]`))?.focus();
};
$('#snav').addEventListener('click', (e) => {
  const b = e.target.closest('.snav-item[data-pane]');
  if (b) activate(b.dataset.pane);
});

/* ---------- 分组管理（原「管理分组」弹窗） ---------- */

/* 展开态记忆：本次会话内重渲不收起（组行内嵌成员管理区） */
const openXGroups = new Set();

async function renderMyGroups() {
  const [st, mm] = await Promise.all([
    loadStatus(),
    api('/api/group-members').catch(() => ({ by_group: {}, members: [] })),
  ]);
  const byGroup = mm.by_group || {};
  const groupsOf = (memberId) => (mm.members || [])
    .filter((m) => m.member_id === memberId)
    .map((m) => m.list_id);
  const box = $('#my-groups');
  box.innerHTML = st.groups.map((g) => {
    const isXg = g.list_id.startsWith('xg:');
    const prefix = g.list_id.startsWith('x:') || g.list_id.startsWith('xu:') || isXg
      ? (isXg ? '组·' : 'X·') : g.list_id.startsWith('wbu:') ? 'WB·' : '';
    // xu: 用户行尾带所属组徽章（组名小字，点徽章跳到该组行）
    const badges = g.list_id.startsWith('xu:')
      ? groupsOf(g.list_id).map((gid) => {
          const grp = st.groups.find((x) => x.list_id === gid);
          return `<button class="gm-badge" data-goto="${esc(gid)}" title="分组：${esc(grp?.name || gid)}">${esc(grp?.name || gid)}</button>`;
        }).join('') : '';
    const membersRow = isXg && openXGroups.has(g.list_id)
      ? (() => {
          const list = byGroup[g.list_id] || [];
          const chips = list.map((m) =>
            `<span class="gm-chip">${esc(m.name)}<button data-gmrm="${esc(g.list_id)}|${esc(m.member_id)}" aria-label="移出 ${esc(m.name)}">✕</button></span>`).join('');
          return `<div class="gm-row">
            <div class="gm-chips">${chips || '<span class="hint">还没有成员，输入 @handle 添加</span>'}</div>
            <div class="gm-add"><input placeholder="添加成员：@handle" maxlength="16" autocomplete="off" spellcheck="false"
              data-gmadd="${esc(g.list_id)}"><button data-gmok="${esc(g.list_id)}">加入</button></div>
            <div class="hint gm-hint">成员须是上方已添加的 X 用户；加入即回填历史帖。</div>
          </div>`;
        })() : '';
    return `
    <div class="grow${isXg ? ' is-xg' : ''}" data-id="${g.list_id}">
      <span class="gname">${prefix}${esc(g.name)}${badges}</span>
      <span class="gstat">${g.active
        ? `待 ${g.pending || 0} · 留 ${g.kept || 0} · 删 ${g.discarded || 0}`
        : '已移除'}</span>
      ${g.active ? `${isXg ? `<button data-gmtoggle="${g.list_id}" aria-expanded="${openXGroups.has(g.list_id)}">${openXGroups.has(g.list_id) ? '收起' : '成员'}</button>` : ''}<button data-bf="${g.list_id}" title="回补近期内容">回补</button><button data-rm="${g.list_id}">移除</button>` : ''}
    </div>${membersRow}`;
  }).join('') ||
    '<div class="hint" style="padding:6px 2px">还没有分组，点下方「拉取分组」从微博账号导入。</div>';
  box.querySelectorAll('[data-gmtoggle]').forEach((b) => b.onclick = () => {
    const id = b.dataset.gmtoggle;
    openXGroups.has(id) ? openXGroups.delete(id) : openXGroups.add(id);
    renderMyGroups();
  });
  box.querySelectorAll('[data-goto]').forEach((b) => b.onclick = () => {
    const row = box.querySelector(`.grow[data-id="${CSS.escape(b.dataset.goto)}"]`);
    openXGroups.add(b.dataset.goto);
    renderMyGroups().then(() => box.querySelector(
      `.grow[data-id="${CSS.escape(b.dataset.goto)}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }));
  });
  box.querySelectorAll('[data-gmrm]').forEach((b) => b.onclick = async () => {
    const [gid, mid] = b.dataset.gmrm.split('|');
    try {
      const r = await api(`/api/groups/${encodeURIComponent(gid)}/members/${encodeURIComponent(mid)}`, 'DELETE');
      toast(r.msg || '已移出');
      renderMyGroups();
    } catch (e) { toast(e.message); }
  });
  const addMember = async (gid) => {
    const inp = box.querySelector(`[data-gmadd="${CSS.escape(gid)}"]`);
    const v = inp.value.trim().replace(/^@/, '');
    if (!v) { inp.focus(); return; }
    try {
      const r = await api(`/api/groups/${encodeURIComponent(gid)}/members`, 'POST', { member: v });
      toast(r.msg || '已加入');
      renderMyGroups();
    } catch (e) { toast(e.message); }
  };
  box.querySelectorAll('[data-gmok]').forEach((b) => b.onclick = () => addMember(b.dataset.gmok));
  box.querySelectorAll('[data-gmadd]').forEach((inp) => {
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); addMember(inp.dataset.gmadd); }
    });
  });
  box.querySelectorAll('[data-bf]').forEach((b) => b.onclick = async () => {
    try {
      b.disabled = true;
      const r = await api('/api/groups/' + encodeURIComponent(b.dataset.bf) + '/backfill', 'POST');
      toast(r.msg || '回补已开始');
      if (typeof pollCrawl === 'function') pollCrawl();
    } catch (e) { toast(e.message); }
    finally { b.disabled = false; }
  });
  box.querySelectorAll('[data-rm]').forEach((b) => b.onclick = async () => {
    try {
      await api('/api/groups/' + b.dataset.rm, 'DELETE');
      openXGroups.delete(b.dataset.rm);
      toast('已移除（已抓的图保留在库里）');
      renderMyGroups();
    } catch (e) { toast(e.message); }
  });
}

// 新建本地分组：只起名字，slug 服务端生成；建好自动展开成员管理
$('#btn-add-xgroup').onclick = async () => {
  const inp = $('#xgroup-input');
  const name = inp.value.trim();
  if (!name) { toast('请输入分组名'); return; }
  const btn = $('#btn-add-xgroup');
  btn.disabled = true;
  try {
    const r = await api('/api/groups', 'POST', { list_id: 'xg:', name });
    toast(r.msg || '已建分组');
    if (r.list_id) openXGroups.add(r.list_id);
    inp.value = '';
    renderMyGroups();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; }
};
$('#xgroup-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('#btn-add-xgroup').click(); }
});

// 添加微博博主：输入 UID、链接或 wbu: 前缀，服务端校验博主存在并获取昵称建组
$('#btn-add-wbuser').onclick = async () => {
  const inp = $('#wbuser-input');
  const val = inp.value.trim();
  if (!val) {
    toast('请输入微博博主 UID 或主页链接');
    return;
  }
  const btn = $('#btn-add-wbuser');
  btn.disabled = true; btn.textContent = '校验中…';
  try {
    const r = await api('/api/groups', 'POST', { list_id: val.startsWith('wbu:') || val.includes('weibo.com') ? val : 'wbu:' + val });
    toast(r.msg || '已添加微博博主');
    inp.value = '';
    await renderMyGroups();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = '添加微博博主'; }
};

// 添加 X 用户：输入 @handle（大小写不敏感），服务端先实连校验用户存在再建组
$('#btn-add-xuser').onclick = async () => {
  const inp = $('#xuser-input');
  const h = inp.value.trim().replace(/^@/, '');
  if (!/^[A-Za-z0-9_]{1,15}$/.test(h)) {
    toast('X 用户名只能是字母、数字、下划线（≤15 位）');
    return;
  }
  const btn = $('#btn-add-xuser');
  btn.disabled = true; btn.textContent = '校验中…';
  try {
    const r = await api('/api/groups', 'POST', { list_id: 'xu:' + h.toLowerCase(), name: '@' + h });
    toast(r.msg || `已添加 @${h}`);
    inp.value = '';
    await renderMyGroups();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = '添加 X 用户'; }
};

// 一键拉取：后台拉微博全部分组并自动加入，过程列表与按钮不可点，无需手动查 ID
$('#btn-discover').onclick = async () => {
  const btn = $('#btn-discover');
  const list = $('#my-groups');
  btn.disabled = true;
  btn.textContent = '正在拉取…';
  list.classList.add('off');
  try {
    const r = await api('/api/groups/discover', 'POST');
    let added = 0;
    for (const c of r.candidates) {
      if (c.added) continue;
      await api('/api/groups', 'POST', { list_id: c.list_id, name: c.name });
      added++;
    }
    toast(added ? `已添加 ${added} 个分组，首次抓取会自动回补近 30 天` : '没有新分组可加');
    await renderMyGroups();
  } catch (err) {
    list.innerHTML = `<div class="hint" style="padding:6px 2px">${esc(err.message)}</div>`;
  }
  btn.disabled = false;
  btn.textContent = '拉取分组';
  list.classList.remove('off');
};

/* ---------- 连接状态（原 Cookie 弹窗：微博 / X 双平台凭据） ---------- */

let cookieSource = 'weibo', cookieBusy = false;
const cookieStates = {unset:'未连接', saved:'已保存 · 待验证', ok:'连接正常',
  expired:'登录已失效 · 请更新 Cookie', error:'暂时无法访问 · 可重新检查'};
function renderConnection() {
  const c = S.st?.connections?.[cookieSource] || (cookieSource === 'weibo' ? S.st?.cookie : null) || {state:'unset'};
  $('#cookie-connection-state').textContent = cookieStates[c.state] || cookieStates.unset;
  const date = c.checked_at || c.updated_at;
  const stamp = date ? new Date(date).toLocaleString('zh-CN', {hour12:false}) : '';
  $('#cookie-connection-meta').textContent = [c.source === 'env' ? '使用环境配置' : c.source === 'web' ? '使用本机保存的凭据' : '',
    stamp ? `${c.checked_at ? '最近检查' : '最近更新'} ${stamp}` : '', c.error].filter(Boolean).join(' · ');
  $('#cookie-source').querySelectorAll('button').forEach(b => {
    const on = b.dataset.source === cookieSource;
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); b.disabled = cookieBusy;
  });
  const wb = cookieSource === 'weibo';
  $('#cookie-input-label').textContent = `手动更新${wb ? '微博' : ' X'} Cookie`;
  $('#cookie-input-guide').textContent = wb
    ? '登录 weibo.com，在开发者工具 Network 中复制请求 Cookie，需包含 SUB 与 XSRF-TOKEN。'
    : '登录 x.com，可粘贴完整 Cookie（含 auth_token 和 ct0），也可只粘贴 auth_token 值。添加 X 列表或博主后即可校验。';
  $('#cookie-input').placeholder = wb ? 'SUB=…; XSRF-TOKEN=…; …' : 'auth_token=…; ct0=…';
  $('#cookie-input').disabled = cookieBusy;
  $('#btn-save-cookie').disabled = cookieBusy;
  $('#btn-save-cookie').textContent = cookieBusy ? '处理中…' : '保存并校验';
  $('#btn-check-cookie').disabled = cookieBusy || c.state === 'unset';
  $('#btn-clear-cookie').disabled = cookieBusy || c.state === 'unset';
  $('#btn-clear-cookie').textContent = c.source === 'env' ? '环境配置生效中' : '清除';
  if (c.source === 'env') $('#btn-clear-cookie').disabled = true;
}
function onConnEnter() {
  $('#cookie-input').value = ''; $('#cookie-result').textContent = '';
  renderConnection();
  loadStatus().then(renderConnection)
    .catch((e) => { $('#cookie-result').textContent = e.message; });
}
function openConnections(source) {   // banner / 空态深链入口，可指定落点平台
  if (source && !cookieBusy) cookieSource = source;
  openSettings('conn');
}
$('#cookie-source').onclick = e => {
  const b = e.target.closest('button[data-source]');
  if (!b || cookieBusy) return;
  cookieSource = b.dataset.source; $('#cookie-input').value = ''; $('#cookie-result').textContent = '';
  renderConnection();
};
async function cookieAction(action) {
  if (cookieBusy) return;
  const v = $('#cookie-input').value.trim();
  if (action === 'save' && !v) { $('#cookie-result').textContent = '请先粘贴 Cookie'; return; }
  cookieBusy = true; renderConnection(); $('#cookie-result').textContent = '正在处理…';
  try {
    const base = cookieSource === 'weibo' ? '/api/settings/cookie' : '/api/settings/x-auth';
    const r = action === 'check' ? await api(`/api/settings/${cookieSource}/check`, 'POST')
      : await api(base, action === 'clear' ? 'DELETE' : 'POST',
        action === 'save' ? (cookieSource === 'weibo' ? {cookie:v} : {auth_token:v}) : undefined);
    if (action !== 'check') $('#cookie-input').value = '';
    $('#cookie-result').textContent = r.msg;
  } catch (e) { $('#cookie-result').textContent = e.message; }
  finally {
    try { await loadStatus(); } catch (e) { /* 操作结果保留，下一次打开再更新状态 */ }
    cookieBusy = false; renderConnection();
  }
}
$('#btn-save-cookie').onclick = () => cookieAction('save');
$('#btn-check-cookie').onclick = () => cookieAction('check');
$('#btn-clear-cookie').onclick = () => cookieAction('clear');

/* ---------- 手机扫码连接（配对码签发仅管理设备；非管理端整块隐藏） ---------- */

/* 配对码签发：生成后二维码携带 ?code=，新设备扫码即自动填入；10 分钟后自动失效归位。 */
let pairCode = '', pairCodeResetTimer = 0;
function qrText() {
  const base = $('#qr-url').value.trim() || location.origin;
  return pairCode ? `${base}${base.includes('?') ? '&' : '?'}code=${encodeURIComponent(pairCode)}` : base;
}
function refreshQr() {
  $('#qr-img').src = '/qr.svg?text=' + encodeURIComponent(qrText());
}
function resetPairCode() {
  clearTimeout(pairCodeResetTimer); pairCodeResetTimer = 0;
  pairCode = '';
  $('#pair-code').textContent = '···· ···· ····';
  $('#pair-code-gen').textContent = '生成配对码';
  refreshQr();
}
$('#pair-role').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-v]');
  if (!b) return;
  $('#pair-role').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
});
$('#pair-code-gen').onclick = async () => {
  if (!S.isAdmin) return;
  const btn = $('#pair-code-gen');
  const role = $('#pair-role .on')?.dataset.v || 'editor';
  btn.disabled = true;
  try {
    const r = await api('/api/device/code', 'POST', { role });
    pairCode = r.code;
    $('#pair-code').textContent = r.code;
    btn.textContent = '重新生成';
    $('#pair-code-hint').textContent =
      `「${{editor:'日常',viewer:'只读',admin:'管理'}[r.role]}」设备码 · ${Math.round(r.ttl / 60)} 分钟内有效、仅可使用一次；扫码自动填入。`;
    clearTimeout(pairCodeResetTimer);
    pairCodeResetTimer = setTimeout(resetPairCode, r.ttl * 1000);
    refreshQr();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; }
};

async function onQrEnter() {
  const host = location.hostname;
  let url;
  if (host !== 'localhost' && host !== '127.0.0.1') {
    url = location.origin;   // 本来就是用 IP/.local 打开的，这就是当前有效地址
  } else {
    // 用 localhost 打开时，问服务端要启动时注入的宿主局域网 IP
    try {
      const lan = await api('/api/lan');
      if (lan.ip) url = `http://${lan.ip}:${lan.port}`;
    } catch (e) { /* 网络问题走兜底 */ }
    if (!url) url = localStorage.getItem('pw_qrurl') || location.origin;
  }
  $('#qr-url').value = url;
  localStorage.setItem('pw_qrurl', url);
  refreshQr();
}
$('#qr-url').oninput = (() => { let t; return () => { clearTimeout(t); t = setTimeout(refreshQr, 350); }; })();
$('#qr-refresh').onclick = refreshQr;
$('#qr-url').addEventListener('change', () => {
  const v = $('#qr-url').value.trim();
  if (v) localStorage.setItem('pw_qrurl', v);
});

/* ---------- 关于：版本 / 结构 / 设备身份 ---------- */

function renderAbout() {
  const st = S.st || {};
  const role = document.documentElement.dataset.deviceRole;
  const localOwner = document.documentElement.dataset.localOwner === 'true';
  $('#about-version').textContent = st.version || '—';
  $('#about-schema').textContent = st.schema_version || '—';
  $('#about-role').textContent = localOwner ? '本机（管理）'
    : ({admin:'管理', editor:'分拣', viewer:'只读', collector:'采集'}[role] || '已配对');
}

return {
  openSettings,
  openGroups: () => openSettings('groups'),
  openConnections,
  // 手机推入面板态：Esc/返回手势先回根菜单（返回 true），已在根或桌面返回 false 由调用方关窗
  mobileBack: () => { if (!isMobile() || !activePane) return false; goRoot(); return true; },
  // 窗口被关闭（Esc/点外/✕）时停掉分区级定时器（如抓取进度轮询）
  closePane: () => panes[activePane]?.onLeave?.(),
};
}
