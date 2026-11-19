/* 幻灯片放映：跟随当前视图的筛选结果（/api/posts 游标分页，边放边取），
   全屏自动翻页。纯欣赏模式——无保留/不要动作，与回看的分拣心智分开：
   单击/空格暂停继续，←→ 手动翻，M 收藏，Esc 或右上 ✕ 退出。
   片单上限取个性化「回看数量」档，装满停预取；保留帖被有效看过
   （停留满 2 秒，或暂停/收藏时当场结算）静默 +1 点赞喂「按点赞排序」；
   鼠标静止 2.5 秒隐匿光标与关闭钮，底部计时线随放映间隔走。 */
import { shuffleSeed } from './view-state.js';

function shuffle(list) {   // 只在帖子级洗牌：套图/条漫展开后仍按 1→N 连播
  for (let i = list.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [list[i], list[j]] = [list[j], list[i]];
  }
  return list;
}

export function ssPrefs(prefs = {}) {
  const n = parseInt(prefs['ss-interval'], 10);
  const review = parseInt(prefs.review, 10);   // 回看数量：与回看选组共用同一档位
  return { interval: [3, 5, 8].includes(n) ? n : 5,
           limit: [10, 30, 100].includes(review) ? review : 30,
           random: prefs['ss-order'] === 'rand',
           comments: prefs['ss-comments'] !== '0' };   // 字幕默认显示，个性化可关
}

/* 帖子数组 → 放映片单：随机档先洗 posts 再展开（严禁拆成单图后打乱，
   否则套图会被打散）；每张带 slideIndex/slideTotal 供字幕显示组内进度。
   视频行取海报静帧当幻灯片（放映是自动连播的纯欣赏流，不做内嵌播放器）。 */
export function expandPosts(posts, prefs = {}) {
  if (prefs.random) shuffle(posts);
  return posts.flatMap((p) => {
    const pics = (p.pics || []).filter((pc) => pc.file);
    return pics.map((pc, i) => ({p, pc, url: '/media/' + (pc.kind === 'video' ? (pc.thumb || pc.file) : pc.file),
                                 slideIndex: i + 1, slideTotal: pics.length}));
  });
}

export function createSlideshow({ S, $, api, toast, enqueueMutation, fmtTime, trapTab,
                                  animatedHide, cancelHide, commentBodyHTML }) {
  const SL = { active: false, images: [], idx: 0, cursor: null, hasMore: true,
               loading: false, timer: 0, paused: false,
               limit: 30, viewed: new Set(), dwell: 0, idle: 0 };
  const LOOKAHEAD = 6;   // 剩余不足此数就预取下一批
  let ssFront = 0, ssToken = 0;   // 交叉淡入淡出：可见层指针 + 过期渲染令牌
  /* 打开放映的那个控件：关闭后把焦点还给它（role=dialog 焦点进出） */
  let ssOpener = null;

  async function fetchBatch() {
    if (SL.loading || !SL.hasMore) return;
    SL.loading = true;
    try {
      const q = new URLSearchParams({status: S.status, group: S.group, author: S.author,
        range: S.range, q: S.searchText,
        has_cmts: S.hasCmts === '1' ? '1' : 'all', limit: 50});
      if (S.searchScope && S.searchScope !== 'all') q.set('q_scope', S.searchScope);
      if (ssPrefs(window.pwGetPrefs ? window.pwGetPrefs() : {}).comments) q.set('cmts', '1');
      if (S.sort === 'shuffle') q.set('shuffle', shuffleSeed(window.pwGetPrefs ? window.pwGetPrefs() : {}));
      else if (S.sort) q.set('sort', S.sort);
      if (SL.cursor) q.set('cursor', SL.cursor);
      const r = await api('/api/posts?' + q);
      const batch = expandPosts(r.posts || [],
        ssPrefs(window.pwGetPrefs ? window.pwGetPrefs() : {}));
      const room = Math.max(0, SL.limit - SL.images.length);
      SL.images.push(...batch.slice(0, room));
      if (batch.length > room) SL.hasMore = false;   // 片单装满即封顶：停预取，播完收场
      else { SL.cursor = r.next_cursor || null; SL.hasMore = !!r.has_more; }
    } catch (e) { toast(e.message); }
    finally { SL.loading = false; }
  }

  function maybeFetch() {
    if (SL.hasMore && !SL.loading && SL.images.length - SL.idx <= LOOKAHEAD) fetchBatch();
  }

  function render() {
    const img = SL.images[SL.idx];
    if (!img) return;
    // 双层交叉淡入淡出：图载入隐藏层，onload 后互换可见性（过期渲染用令牌丢弃）
    const token = ++ssToken;
    const [layerA, layerB] = [$('#ss-layer-a'), $('#ss-layer-b')];
    const visible = ssFront ? layerB : layerA;
    const hidden = ssFront ? layerA : layerB;
    const im = new Image();
    im.onload = () => {
      if (token !== ssToken || !SL.active) return;
      hidden.querySelector('img').src = img.url;
      hidden.classList.add('ss-show');
      visible.classList.remove('ss-show');
      ssFront = 1 - ssFront;
      armDwell(img.p);   // 切入激活层才开始算有效停留
    };
    im.src = img.url;
    renderCaption(img);
    // 评论字幕：你写的批注是重温的一部分；个性化可关，无评论不留空
    const notes = $('#ss-notes');
    const cmts = ssPrefs(window.pwGetPrefs ? window.pwGetPrefs() : {}).comments
      ? (img.p.comments || []) : [];
    if (cmts.length) {
      notes.hidden = false;
      notes.innerHTML = cmts.map((c) => `<p class="ss-note">${commentBodyHTML(c.body)}</p>`).join('');
    } else { notes.hidden = true; notes.innerHTML = ''; }
    const nxt = SL.images[SL.idx + 1];
    if (nxt) new Image().src = nxt.url;   // 预载下一张，自动翻页不闪等
  }

  /* 左下角字幕：作者·时间 + 套图组内进度 + 收藏星标（M 键切换的即时可视） */
  function renderCaption(img) {
    const cap = $('#ss-caption');
    const meta = document.createElement('span');
    meta.textContent = `${img.p.author} · ${fmtTime(img.p.created_at)}`;
    cap.replaceChildren(meta);
    if (img.slideTotal > 1) {
      const step = document.createElement('span');
      step.className = 'ss-step';
      step.textContent = `${img.slideIndex}/${img.slideTotal}`;
      cap.append(step);
    }
    const star = document.createElement('span');
    star.className = 'ss-fav-star' + (img.p.favorite ? ' on' : '');
    star.textContent = '★';
    cap.append(star);
  }

  /* 有效回看计次：翻走即作废，暂停/收藏时当场结算；只对保留帖记账
     （keep_count 的语义边界），一场放映每帖至多一次，静默上报不弹错 */
  function armDwell(p) {
    clearTimeout(SL.dwell);
    SL.dwell = setTimeout(() => { SL.dwell = 0; countView(p); }, 2000);
  }
  function countView(p) {
    clearTimeout(SL.dwell); SL.dwell = 0;
    if (!p || SL.viewed.has(p.sid) || p.status !== 'kept') return;
    SL.viewed.add(p.sid);
    enqueueMutation(() => api(`/api/posts/${p.sid}/reaffirm`, 'POST')).catch(() => {});
  }

  function schedule() {
    clearTimeout(SL.timer);
    const bar = $('#ss-progress');
    bar.classList.remove('run', 'frozen');
    if (!SL.active || SL.paused) return;
    const ms = ssPrefs(window.pwGetPrefs ? window.pwGetPrefs() : {}).interval;
    $('#slideshow').style.setProperty('--ss-dur', ms + 's');   // 计时线与呼吸缩放共用时长
    void bar.offsetWidth;   // 重排后再起步：手动翻页/续播都从 0 重画
    bar.classList.add('run');
    SL.timer = setTimeout(() => { SL.timer = 0; advance(1); }, ms * 1000);
  }

  async function advance(d) {
    if (!SL.active) return;
    clearTimeout(SL.dwell); SL.dwell = 0;   // 2 秒内翻走不计数
    const j = SL.idx + d;
    if (j < 0) return;
    if (j >= SL.images.length) {
      if (!SL.hasMore) { finish(); return; }
      await fetchBatch();
      if (j >= SL.images.length) { finish(); return; }
    }
    SL.idx = j;
    render();
    schedule();   // 手动翻页重置计时；暂停态下 schedule 自守卫
    maybeFetch();
  }

  function togglePause() {
    if (!SL.active) return;
    SL.paused = !SL.paused;
    $('#ss-state').hidden = !SL.paused;
    if (SL.paused) {
      clearTimeout(SL.timer); SL.timer = 0;
      $('#ss-progress').classList.add('frozen');   // 计时线冻结在当前宽度
      countView(SL.images[SL.idx]?.p);
    }
    else schedule();
  }

  /* 静止隐匿：2.5 秒无鼠标/按键 → 藏光标与关闭钮，任何动静立即唤醒 */
  function wake() {
    if (!SL.active) return;
    $('#slideshow').classList.remove('is-idle');
    clearTimeout(SL.idle);
    SL.idle = setTimeout(() => {
      if (SL.active) $('#slideshow').classList.add('is-idle');
    }, 2500);
  }

  async function open() {
    const ae = (typeof document !== 'undefined') ? document.activeElement : null;
    if (SL.active || S.readOnly) return;
    SL.limit = ssPrefs(window.pwGetPrefs ? window.pwGetPrefs() : {}).limit;
    SL.viewed = new Set();
    await fetchBatch();
    if (!SL.images.length) { toast('当前筛选没有图片'); return; }
    SL.active = true; SL.idx = 0; SL.paused = false;
    ssOpener = (ae && typeof Element !== 'undefined' && ae instanceof Element
                && ae !== document.body) ? ae : null;
    $('#ss-state').hidden = true;
    cancelHide($('#slideshow'));
    $('#slideshow').hidden = false;
    if (typeof document !== 'undefined' && document.body) document.body.style.overflow = 'hidden';
    render();
    schedule();
    wake();
    $('#ss-close')?.focus?.({ preventScroll: true });
    try {   // 首次放映说一次计次规则：纯欣赏模式里的隐式写账不该全靠猜
      if (!localStorage.getItem('pw_sshint')) {
        localStorage.setItem('pw_sshint', '1');
        toast('停留满 2 秒（或暂停/收藏）的保留帖会自动计一次「点赞」');
      }
    } catch (e) { /* 存不了就不再弹 */ }
  }

  function close() {
    if (!SL.active) return;
    SL.active = false;
    clearTimeout(SL.timer); SL.timer = 0;
    clearTimeout(SL.dwell); SL.dwell = 0;
    clearTimeout(SL.idle); SL.idle = 0;
    SL.images = []; SL.idx = 0; SL.cursor = null; SL.hasMore = true; SL.paused = false;
    SL.viewed = new Set();
    $('#slideshow').classList.remove('is-idle');
    $('#ss-progress').classList.remove('run', 'frozen');
    animatedHide($('#slideshow'));
    if (typeof document !== 'undefined' && ssOpener && ssOpener.isConnected) {
      const cur = document.activeElement;
      const ss = $('#slideshow');
      if (!cur || cur === document.body || (ss && ss.contains(cur))) {
        ssOpener.focus?.({ preventScroll: true });
      }
    }
    ssOpener = null;
    const lb = $('#lightbox');
    if (lb.hidden || lb.classList.contains('closing')) {
      if (typeof document !== 'undefined' && document.body) document.body.style.overflow = '';
    }
  }

  function finish() {
    toast(`放映结束 · 共 ${SL.images.length} 张`);
    close();
  }

  // 收藏顺手标记心头好：回看帖不在当前 feed 时直接同步数据对象（墙卡已无星标按钮）
  function syncFav(p) {
    const inFeed = S.posts.find(x => x.sid === p.sid);
    if (inFeed) inFeed.favorite = p.favorite;
  }
  async function favorite() {
    const img = SL.images[SL.idx];
    if (!img) return;
    const p = img.p;
    const target = !p.favorite;
    p.favorite = target;
    syncFav(p);
    countView(p);   // 主动收藏即视为有效回看
    const star = $('.ss-fav-star');
    if (star) {
      star.classList.toggle('on', target);
      star.classList.remove('pop'); void star.offsetWidth; star.classList.add('pop');
    }
    try { await enqueueMutation(() => api(`/api/posts/${p.sid}/favorite`, 'POST', {favorite: target})); }
    catch (e) {
      p.favorite = !target; syncFav(p);
      if (star) star.classList.toggle('on', p.favorite);
      toast(e.message);
    }
  }

  // 放映是模态流：吃掉全部单键（含 1-5 换视图），已知的给出动作，其余忽略
  function key(e) {
    wake();
    switch (e.key) {
      case ' ': e.preventDefault(); togglePause(); break;
      case 'ArrowLeft': advance(-1); break;
      case 'ArrowRight': advance(1); break;
      case 'm': case 'M': favorite(); break;
      case 'Tab': trapTab(e, $('#slideshow')); break;
    }
  }

  $('#slideshow').addEventListener('mousemove', wake);
  $('#slideshow').addEventListener('click', (e) => {
    wake();
    if (e.target.closest('.ss-nav') || e.target.closest('#ss-close')) return;
    togglePause();
  });
  $('#ss-prev').onclick = () => advance(-1);
  $('#ss-next').onclick = () => advance(1);
  $('#ss-close').onclick = () => close();

  return { open, close, active: () => SL.active, key };
}
