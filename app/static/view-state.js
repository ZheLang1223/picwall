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
export function keepBadge(count) {
  const n = count || 1;
  const tier = n >= 10 ? 4 : n >= 5 ? 3 : n >= 3 ? 2 : 1;
  return { text: `×${n}`, tier, title: `点赞 ${n} 次` };
}
export function avatarSrc(url) {
  // 入库的头像存 PICS_DIR 相对路径（avatars/t_x.jpg）；网页经 /media/* 读盘
  return url && url.startsWith('avatars/') ? '/media/' + url : url;
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
      (sel && !(p.tags || []).some(t => sel.includes(t))) ||
      // 搜索态：正文与评论命中同权（comment_match = 服务端给的最早命中批注），
      // 缺它则纯评论命中的帖被服务端返回后又被这里整卡丢弃
      (s.searchText && !((p.text || '').toLowerCase().includes(s.searchText.trim().toLowerCase()) ||
        (p.comment_match || '').toLowerCase().includes(s.searchText.trim().toLowerCase())))) return false;
  const day = new Date(now + 8 * 3600e3).toISOString().slice(0, 10);
  const midnight = Date.parse(day + 'T00:00:00+08:00');
  const created = Date.parse(p.created_at);
  if (s.range === 'today') return created >= midnight;
  if (s.range === 'yesterday') return created >= midnight - 86400e3 && created < midnight;
  if (s.range === '7d' || s.range === '30d')
    return created >= midnight - Number(s.range.slice(0,-1)) * 86400e3;
  return true;
}
