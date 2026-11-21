/* 与服务端日界、搜索和稳定排序一致的纯视图规则。 */

/* 随机档种子（日界口径与 matchesView 相同的东八区）：默认「每次打开」——
   sessionStorage 记一套（同会话翻页稳定、刷新不跳变，新开标签即新洗法）；
   设置切「每天一换」则用东八区日期当种子，多设备当天同一洗法。
   sessionStorage 不可用（隐私模式/Node 契约测试）时回落日种子：无状态、照样稳定。 */
export function shuffleSeed(prefs = {}) {
  const daily = () => 'd' + new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
  try {
    if (prefs['shuffle-seed'] === 'daily') return daily();
    let s = sessionStorage.getItem('pw_shuffle_seed');
    if (!s) {
      s = crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now();
      sessionStorage.setItem('pw_shuffle_seed', s);
    }
    return s;
  } catch (e) { return daily(); }
}

export function comparePosts(a, b, sort = '') {
  // 点赞序 = 点赞降序 → 时间降序 → sid，与服务端 sort=keep_count 的三键一致
  if (sort === 'keep_count' && (a.keep_count || 1) !== (b.keep_count || 1))
    return (b.keep_count || 1) - (a.keep_count || 1);
  return a.created_at === b.created_at ? (a.sid < b.sid ? -1 : a.sid > b.sid ? 1 : 0)
    : a.created_at > b.created_at ? -1 : 1;
}

/* 点赞徽章：×2 起显示，文字只留 ×N；色阶随点赞升温
   t1=×2 静默灰 → t2=×3-4 主题色 → t3=×5-9 收藏金 → t4=×10+ 墨底反白 */
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function safeUrl(u) {
  // 远端来的链接只放行 http(s)，防 javascript: 之类伪协议进 href
  return /^https?:\/\//i.test(u || '') ? u : '#';
}

export function keepBadge(count) {
  const n = count || 1;
  const tier = n >= 10 ? 4 : n >= 5 ? 3 : n >= 3 ? 2 : 1;
  return { text: `×${n}`, tier, title: `点赞 ${n} 次` };
}
export function avatarSrc(url) {
  // 入库的头像存 PICS_DIR 相对路径（avatars/t_x.jpg）；网页经 /media/* 读盘；外部仅放行 http(s)
  if (!url) return url;
  if (url.startsWith('avatars/')) return '/media/' + url;
  return /^https?:\/\//i.test(url) ? url : '';
}

export function mediaSrc(u) {
  if (!u) return '';
  if (u.startsWith('/media/')) return u;
  if (/^https?:\/\//i.test(u)) return u;
  if (/^[a-z][a-z0-9+.-]*:/i.test(u)) return '';
  return '/media/' + u.replace(/^\/+/, '');
}

/* 与服务端 parse_search_query 同口径：解析搜索范围与前缀（@ 博主 / # 标签 / // 或 c: 评论）。
   'all' 维度按首个前缀切维度并连续剥离该前缀全部字符；显式维度只剥自己的前缀；剥离后再 strip。
   term 保留原大小写，需要小写比较的调用方自行处理。 */
const SEARCH_SCOPES = new Set(['all', 'author', 'tag', 'text', 'comment']);
export function parseSearch(text, scope = 'all') {
  let sc = String(scope || 'all').trim().toLowerCase();
  if (!SEARCH_SCOPES.has(sc)) sc = 'all';
  let t = String(text || '').trim();
  if (!t) return { scope: sc, term: '' };
  if (sc === 'all') {
    if (t.startsWith('@') || t.startsWith('＠')) {
      sc = 'author';
      t = t.replace(/^[@＠]+/, '');
    } else if (t.startsWith('#') || t.startsWith('＃')) {
      sc = 'tag';
      t = t.replace(/^[#＃]+/, '');
    } else if (t.startsWith('//') || t.startsWith('／／') || /^c[:：]/i.test(t)) {
      sc = 'comment';
      t = t.slice(2);
    }
  } else if (sc === 'author') {
    t = t.replace(/^[@＠]+/, '');
  } else if (sc === 'tag') {
    t = t.replace(/^[#＃]+/, '');
  } else if (sc === 'comment' && (t.startsWith('//') || t.startsWith('／／') || /^c[:：]/i.test(t))) {
    t = t.slice(2);
  }
  return { scope: sc, term: t.trim() };
}

export function searchNeedle(term, scope = 'all') {
  return parseSearch(term, scope).term;
}

export function markTerm(text, term = '', scope = 'all') {
  const raw = String(text ?? '');
  const needle = searchNeedle(term, scope);
  if (!needle) return esc(raw);
  const re = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  let out = '';
  let last = 0;
  for (const m of raw.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) out += esc(raw.slice(last, idx));
    out += '<mark>' + esc(m[0]) + '</mark>';
    last = idx + m[0].length;
  }
  if (last < raw.length) out += esc(raw.slice(last));
  return out;
}

export function matchesView(p, s, now = Date.now()) {
  const status = s.status === 'all' || (s.status === 'favorites' ? p.favorite : s.status === p.status);
  const src = s.source === 'x' ? 'x' : 'weibo';
  // 标签筛选：任一命中即入选（OR，与服务端 post_filter 的 tag IN 同口径）；'all'/缺省 = 不筛
  const sel = s.tags && s.tags !== 'all' ? String(s.tags).split(',') : null;
  if (!status || (s.source && s.source !== 'all' && (p.source || 'weibo') !== src) ||
      (s.group !== 'all' && s.group !== p.list_id && !(p.groups || []).includes(s.group)) ||
      (s.author !== 'all' && s.author !== p.uid) ||   // payload 的 uid 已按来源加前缀（author_key），直接相等即可
      // 标签筛选态：payload 未带 tags（旧缓存对象）时按无标签处理，交服务端口径兜底
      (sel && !(p.tags || []).some(t => sel.includes(t)))) return false;
  // 搜索态：根据维度（全部 / 内容 / 评论 / 标签 / 博主）匹配，前缀解析与服务端 parse_search_query 同口径
  if (s.searchText && s.searchText.trim()) {
    const { scope, term: rawTerm } = parseSearch(s.searchText, s.searchScope);
    const term = rawTerm.toLowerCase();
    if (term) {
      const matchText = (p.text || '').toLowerCase().includes(term);
      const matchComment = (p.comment_match || '').toLowerCase().includes(term) ||
        (Array.isArray(p.comments) && p.comments.some(c => (typeof c === 'string' ? c : c.body || c.text || '').toLowerCase().includes(term)));
      const matchTag = (p.tags || []).some(t => (t || '').toLowerCase().includes(term));
      const matchAuthor = (p.author || '').toLowerCase().includes(term);

      let matched = false;
      if (scope === 'text') matched = matchText;
      else if (scope === 'comment') matched = matchComment;
      else if (scope === 'tag') matched = matchTag;
      else if (scope === 'author') matched = matchAuthor;
      else matched = matchText || matchComment || matchTag || matchAuthor;

      if (!matched) return false;
    }
  }
  const day = new Date(now + 8 * 3600e3).toISOString().slice(0, 10);
  const midnight = Date.parse(day + 'T00:00:00+08:00');
  const created = Date.parse(p.created_at);
  if (s.range === 'today') return created >= midnight;
  if (s.range === 'yesterday') return created >= midnight - 86400e3 && created < midnight;
  if (s.range === '7d' || s.range === '30d')
    return created >= midnight - Number(s.range.slice(0,-1)) * 86400e3;
  return true;
}
