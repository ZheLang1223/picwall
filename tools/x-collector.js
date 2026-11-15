/* Picwall · X（Twitter）采集助手 —— 浏览器代抓通道的采集端
 *
 * 用法（需已登录 x.com 的桌面浏览器）：
 *   1. 打开 x.com 并进入你的 List 页面；
 *   2. make picwall-collector-token 获取仅可导入的令牌，填 DEVICE_TOKEN；
 *      F12 → Console，粘贴本脚本并回车（先改 LIST_ID / LIST_NAME）；
 *   3. 像平时一样往下滚动列表——脚本被动拦截页面自己发出的时间线请求，
 *      抽出带图推文，凑满 30 条自动打包 POST 给 picwall；
 *   4. 随时调用 pwXFlush() 手动清空剩余；调用 pwXStop() 停止采集。
 *
 * 设计要点：
 *   - 不硬编码 GraphQL queryId：只拦页面自己发的 XHR/fetch 响应，queryId 漂移与本脚本无关；
 *   - 只读：不点任何按钮、不发任何写请求；滚动节奏 = 人工浏览节奏（自限频）；
 *   - 图片 URL 原样交给服务端（pbs.twimg.com 公网可取），服务端负责 orig/large 与落盘去重。
 */

(() => {
  const LIST_ID = '';      // ← 必填：X 列表 id（URL x.com/i/lists/1234567890 里的数字）
  const LIST_NAME = '';    // ← 建议填：在 picwall 里显示的分组名
  const PICWALL = 'http://localhost:8081';
  const DEVICE_TOKEN = ''; // ← 必填：make picwall-collector-token；不得使用管理令牌
  const FLUSH_AT = 30;
  const MAX_SEEN = 5000;
  const TIMELINE_RE = /\/graphql\/.*\/(ListLatestTweetsTimeline|ListTimeline)/;

  if (!LIST_ID || !DEVICE_TOKEN) { console.error('[pwX] 先填 LIST_ID 与 DEVICE_TOKEN'); return; }

  const seen = new Set();
  const buffer = [];
  let stopped = false, posted = 0;
  let sending = false;
  let autoFlushBlocked = false;

  const rememberId = (id) => {
    if (seen.has(id)) return false;
    if (seen.size >= MAX_SEEN) {
      const oldest = seen.values().next().value;
      if (oldest !== undefined) seen.delete(oldest);
    }
    seen.add(id);
    return true;
  };

  const iso = (s) => { const d = new Date(s); return isNaN(d) ? '' : d.toISOString(); };

  // GraphQL 的 user 字段形态随版本漂移，多路取值；取不到的推文会被服务端契约校验安全跳过
  const handleTweet = (r) => {
    if (!r || r.__typename === 'TweetTombstone') return;
    const legacy = r.legacy || {};
    const rtResult = legacy.retweeted_status_result?.result;
    const rt = rtResult?.legacy || legacy.retweeted_status;
    const src = rt || legacy;
    const media = (src.extended_entities || src.entities || {}).media || [];
    const images = media.filter(m => m.type === 'photo').map(m => m.media_url_https);
    // 视频/GIF：取最高码率 mp4 变体 + 海报静帧；与 xapi 服务端抓取同契约（服务端再过白名单）
    const videos = media.filter(m => m.type === 'video' || m.type === 'animated_gif')
      .map(m => {
        const vs = (m.video_info?.variants || []).filter(v => v.content_type === 'video/mp4' && v.url);
        if (!vs.length || !m.media_url_https) return null;
        const best = vs.reduce((a, b) => (b.bitrate || 0) > (a.bitrate || 0) ? b : a);
        return { poster: m.media_url_https, url: best.url, variants: vs,
                 aspect_ratio: m.video_info?.aspect_ratio || [1, 1],
                 duration_millis: m.video_info?.duration_millis || 0 };
      }).filter(Boolean);
    if (!images.length && !videos.length) return;     // 无图无视频帖不进图片墙
    const id = (rt && src.id_str) || r.rest_id || legacy.id_str;
    if (!id || !rememberId(id)) return;
    const user = (rtResult || r).core?.user_results?.result || {};
    const u = {...(user.legacy || {}), ...(user.core || {}), ...(src.user || {})};
    const item = {
      id,
      url: `https://x.com/${u.screen_name || 'i'}/status/${id}`,
      author: { id: String(user.rest_id || u.id_str || ''), uid: u.screen_name || '', handle:u.screen_name || '', name: u.name || '', avatar: u.profile_image_url_https || '' },
      text: (src.full_text || '').replace(/https:\/\/t\.co\/\w+$/g, '').trim(),
      created_at: iso(src.created_at || ''),
      via: rt ? (legacy.user?.name || legacy.user?.screen_name || '') : '',
      images,
    };
    if (videos.length) item.videos = videos;
    buffer.push(item);
  };

  const walkEntry = (e) => {
    const t = e?.content?.itemContent?.tweet_results?.result
      || e?.item?.content?.itemContent?.tweet_results?.result;
    if (!t) return;
    handleTweet(t.tweet || t);                         // 新版包 {tweet: …}，旧版平铺
  };

  const walkInstructions = (instructions) => {
    for (const ins of instructions || []) {
      for (const e of ins.entries || []) {
        if (e.entryId?.includes('tweet')) walkEntry(e);
        for (const it of e.content?.items || []) walkEntry(it);   // conversation module
      }
    }
  };

  const eatGraphQL = (url, json) => {
    if (stopped || !TIMELINE_RE.test(url)) return;
    try {
      const d = json?.data;
      walkInstructions(d?.list?.tweets_timeline?.timeline?.instructions
        || d?.tweet_list?.timeline_response?.timeline?.instructions
        || d?.user?.result?.timeline?.timeline?.instructions || []);
      console.log(`[pwX] 累计采集 ${buffer.length}（去重后），已推送 ${posted}`);
      if (buffer.length >= FLUSH_AT && !autoFlushBlocked) flush(false);
    } catch (err) { console.warn('[pwX] 解析失败（结构又变了，忽略该响应）', err); }
  };

  const _fetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await _fetch.apply(this, args);
    try {
      const u = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
      if (!stopped && TIMELINE_RE.test(u)) {
        const clone = res.clone();
        clone.json().then(j => eatGraphQL(u, j)).catch(() => {});
      }
    } catch (e) {}
    return res;
  };
  const _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    const url = String(u);
    if (!stopped && TIMELINE_RE.test(url)) {
      this.addEventListener('load', function () {
        try {
          if (!stopped && (this.responseType === 'json' || !this.responseType)) {
            eatGraphQL(url, typeof this.response === 'string'
              ? JSON.parse(this.response) : this.response);
          }
        } catch (e) {}
      });
    }
    return _open.call(this, m, u, ...rest);
  };

  async function flush(manual = false) {
    if (manual) autoFlushBlocked = false;
    if (sending) return;
    if (!buffer.length) { console.log('[pwX] 没有待推送的推文'); return; }
    const batch = buffer.splice(0, 100);
    sending = true;
    try {
      const r = await fetch(`${PICWALL}/api/ingest/x`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + DEVICE_TOKEN },
        signal: AbortSignal.timeout(120000),
        body: JSON.stringify({ list_id: LIST_ID, name: LIST_NAME, tweets: batch }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.detail || j.msg || `HTTP ${r.status}`);
      autoFlushBlocked = false;
      posted += j.new_posts || 0;
      console.log(`[pwX] 推送 ${batch.length} → 新入 ${j.new_posts} 帖 / ${j.new_pics} 图（累计新入 ${posted}）`);
      if (buffer.length) console.log(`[pwX] 余 ${buffer.length} 条，再调一次 pwXFlush()`);
    } catch (e) {
      buffer.unshift(...batch);
      autoFlushBlocked = true;
      console.error('[pwX] 推送未确认，已暂停自动推送并保留待发内容；恢复后请手动调用 pwXFlush()', e.message);
    } finally { sending = false; }
  }
  function stop() {
    stopped = true;
    window.fetch = _fetch;
    XMLHttpRequest.prototype.open = _open;
    try { delete window.pwXFlush; delete window.pwXStop; } catch (e) {}
    console.log('[pwX] 已停止并卸载拦截钩子');
  }
  Object.defineProperty(window, 'pwXFlush', { value: () => flush(true), configurable: true });
  Object.defineProperty(window, 'pwXStop', { value: stop, configurable: true });
  console.log('[pwX] 采集已挂载：滚动列表即可，pwXFlush() 手动推送 / pwXStop() 停止');
})();
