/* 回看：服务端选组（次数≥2 优先 + 7 天内看过的不重复 + 固定数量），
   复用 #detail 覆盖层浏览这一组。卡片交互与普通详情**完全同一套**——
   回看帖全是保留帖，故 F/↑ 与 D/↓ = 点赞 ±1（不前进，相向按键互为撤销，
   与普通保留帖 ±1 同语义）；←→ 单击切本帖图 / 350ms 双击翻帖、
   PageUp/PageDown 翻帖、空格开当前图大图、M/L/T、Esc 关闭。
   组读到底自动取下一组续刷（与墙流自动补页同一心智），真没有才提示停留。
   1-4 视图键不在此吃键：与普通详情一致，按了即收会话切视图（app.js 分发放行）。 */
import { keepBadge } from './view-state.js';

export function createReview({ S, $, api, toast, enqueueMutation,
                               detailCards, renderDetailInto, resetDetailCard,
                               cancelHide, animatedHide,
                               safeUrl, trapTab, setDetailSid,
                               showStageImage, openLB }) {
  const R = { active: false, posts: [], idx: 0, stashImages: null, loadingMore: false, opening: false,
              arrow: {key: null, t: 0} };

  // 回看帖不在当前 feed 列表里时，直接改 feed 卡片的徽章；在列表里则同步数据对象
  function syncBadge(p) {
    const inFeed = S.posts.find(x => x.sid === p.sid);
    if (inFeed) inFeed.keep_count = p.keep_count;
    const card = document.querySelector(`#feed .card[data-sid="${CSS.escape(p.sid)}"]`);
    if (!card) return;
    let chip = card.querySelector('.keepn');
    if (p.keep_count >= 2) {
      const kb = keepBadge(p.keep_count);
      if (!chip) {
        chip = document.createElement('span');
        chip.className = 'chip keepn';
        card.querySelector('.meta time')?.before(chip);
      }
      chip.className = `chip keepn t${kb.tier}`;
      chip.title = kb.title;
      chip.textContent = kb.text;
    } else chip?.remove();
  }

  function reviewN() {
    const prefs = window.pwGetPrefs ? window.pwGetPrefs() : {};
    const n = parseInt(prefs.review, 10);
    return [10, 30, 100].includes(n) ? n : 30;
  }

  function render() {
    const { front, back } = detailCards();
    const p = R.posts[R.idx], nxt = R.posts[R.idx + 1] || null;
    front.hidden = false;
    renderDetailInto(front, p, true);
    setDetailSid(p.sid);
    R.arrow = {key: null, t: 0};   // 换帖即作废：←→ 双击判定与详情同款
    if (nxt) { back.hidden = false; renderDetailInto(back, nxt, true); }
    else back.hidden = true;
    resetDetailCard(front);
    const pill = $('#review-progress');
    pill.hidden = false;
    pill.textContent = `${R.idx + 1} / ${R.posts.length}`;
  }

  async function open() {
    if (R.active || R.opening || S.readOnly) return;
    R.opening = true;   // 请求在途时挡住连点；close() 清掉即视为取消
    const pill = $('#review-progress');   // 等待反馈：请求回来前先亮进度 pill，成功后由 render() 接管文案
    pill.hidden = false;
    pill.textContent = '回看加载中…';
    let data;
    try { data = await api('/api/review/next?limit=' + reviewN()); }
    catch (e) { if (R.opening) { pill.hidden = true; toast(e.message); } R.opening = false; return; }
    if (!R.opening || R.active) { R.opening = false; pill.hidden = true; return; }   // 加载中已被关闭：取消打开
    R.opening = false;
    const posts = data.posts || [];
    if (!posts.length) { pill.hidden = true; toast('还没有保留的图'); return; }
    R.active = true; R.posts = posts; R.idx = 0;
    // 大图浏览换到本组范围（关闭时还原 feed 的图清单）
    R.stashImages = S.images;
    S.images = posts.flatMap(p => (p.pics || []).map(pc => ({ p, pc, url: '/media/' + pc.file })));
    $('#detail').dataset.mode = 'review';
    render();
    cancelHide($('#detail'));
    $('#detail').hidden = false;
    document.body.classList.add('has-detail');
    $('#detail').scrollTop = 0;
    document.body.style.overflow = 'hidden';
    $('#detail .detail-card.is-front').focus({ preventScroll: true });
    try {   // F 不再前进：键位虽已对齐普通，但老肌肉记忆值得一次性提示
      if (!localStorage.getItem('pw_reviewhint')) {
        localStorage.setItem('pw_reviewhint', '1');
        toast('回看与普通浏览同一套键位：F/D 点赞±1 · ←→ 双击 / PgUp·Dn 翻帖 · Esc 退出');
      }
    } catch (e) { /* 存不了就不再弹 */ }
  }

  function close() {
    if (R.opening && !R.active) {   // 打开请求尚在途：取消
      R.opening = false;
      $('#review-progress').hidden = true;
      return;
    }
    if (!R.active) return;
    R.active = false;
    R.posts = [];
    R.loadingMore = false;
    S.images = R.stashImages || [];
    delete $('#detail').dataset.mode;
    $('#review-progress').hidden = true;
    document.body.classList.remove('has-detail');
    animatedHide($('#detail'));
    setDetailSid(null);
    const lb = $('#lightbox');
    if (lb.hidden || lb.classList.contains('closing')) document.body.style.overflow = '';
  }

  /* 点赞 ±1：与普通保留帖同语义——乐观改账、串行落账、失败按修订号回滚、
     不前进（就地刷新 ×N 徽章）；± 相向按键即互为撤销，不设 6 秒撤销 toast。
     每帖每天 +1 一次由服务端统一拦截（409 → 回滚 + toast）。 */
  async function reaffirm(delta) {
    const p = R.posts[R.idx];
    if (!p) return;
    if (delta < 0 && (p.keep_count || 1) <= 1) { toast('只赞过一次'); return; }
    const revision = (p._reaffirmRevision || 0) + 1;
    p._reaffirmRevision = revision;
    const previous = { count: p.keep_count || 1, last: p.last_confirm_at };
    p.keep_count = previous.count + delta;
    if (delta > 0) p.last_confirm_at = new Date().toISOString();   // 乐观盖当日账（回滚时还原）
    R.arrow = {key: null, t: 0};   // 与普通 ±1 同款：双击判定作废
    syncBadge(p);
    const { front } = detailCards();
    let imgIdx = 0;
    const inFront = R.posts[R.idx]?.sid === p.sid && !!front;
    if (inFront) {
      p.comments = front._comments || p.comments || [];   // 保住刚写的批注（卡内实时列表回写数据源）
      imgIdx = +front.dataset.img || 0;
    }
    const repaint = () => {   // ±1 不前进：就地刷新 ×N 徽章，不打断正在看的图
      front.hidden = false;
      renderDetailInto(front, p, false);
      showStageImage(front, Math.max(0, Math.min(imgIdx, (p.pics || []).length - 1)));
      resetDetailCard(front);
    };
    if (inFront) repaint();
    try {
      await enqueueMutation(() => api(`/api/posts/${p.sid}/reaffirm`, delta > 0 ? 'POST' : 'DELETE'));
    } catch (e) {
      if (p._reaffirmRevision === revision) {
        p.keep_count = previous.count;
        p.last_confirm_at = previous.last;
        syncBadge(p);
        if (R.active && inFront) repaint();
      }
      toast(e.message);
    }
  }

  /* 组读到底自动取下一组续刷（与普通浏览自动补页同一心智）；真没有 → 提示停留。
     同组 sid 去重：服务端 7 天去重之外再防一批内的重复入选。 */
  async function fetchMore() {
    if (R.loadingMore) return false;
    R.loadingMore = true;
    try {
      const data = await api('/api/review/next?limit=' + reviewN());
      const known = new Set(R.posts.map(x => x.sid));
      const fresh = (data.posts || []).filter(x => !known.has(x.sid));
      if (!fresh.length) return false;
      R.posts.push(...fresh);
      S.images.push(...fresh.flatMap(p => (p.pics || []).map(pc => ({ p, pc, url: '/media/' + pc.file }))));
      return true;
    } catch (e) { toast(e.message); return null; }   // null = load failed (already toasted); false = really no more
    finally { R.loadingMore = false; }
  }

  async function nav(d) {
    if (!R.active) return;
    const j = R.idx + d;
    if (j < 0) { toast('已经是第一张'); return; }
    if (j >= R.posts.length) {
      if (R.loadingMore) return;   // 下一组还在加载：静默等待，不误报到底
      const added = await fetchMore();
      if (!R.active) return;   // 加载期间已退出回看
      if (added === null) return;   // load failed: error toast already shown
      if (!added) { toast('已经是最后一张'); return; }
    }
    R.idx = j;
    render();
  }

  async function favorite() {
    const p = R.posts[R.idx];
    if (!p) return;
    const target = !p.favorite;
    p.favorite = target;
    render();
    try { await enqueueMutation(() => api(`/api/posts/${p.sid}/favorite`, 'POST', { favorite: target })); }
    catch (e) { p.favorite = !target; render(); toast(e.message); }
  }

  function openLink() {
    const url = safeUrl(R.posts[R.idx]?.weibo_url);
    if (url === '#') { toast('原帖链接不可用'); return; }
    window.open(url, '_blank', 'noopener,noreferrer');
  }

  // 键位与普通详情一致（1-4 视图键由 app.js 分发放行给全局，不在此吃键）
  function key(e) {
    switch (e.key) {
      case 'ArrowUp': e.preventDefault(); reaffirm(1); break;
      case 'f': case 'F': reaffirm(1); break;
      case 'ArrowDown': e.preventDefault(); reaffirm(-1); break;
      case 'd': case 'D': reaffirm(-1); break;
      case 'PageUp': e.preventDefault(); nav(-1); break;
      case 'PageDown': e.preventDefault(); nav(1); break;
      case 'ArrowLeft': case 'ArrowRight': {
        // 与详情同一规则：单击切本帖图（边界钳停），350ms 内同向双击翻帖
        e.preventDefault();
        const dir = e.key === 'ArrowRight' ? 1 : -1;
        const now = performance.now();
        const dbl = !e.repeat && R.arrow.key === e.key && now - R.arrow.t < 350;
        R.arrow = dbl || e.repeat ? {key: null, t: 0} : {key: e.key, t: now};
        if (dbl) { nav(dir); break; }
        const card = detailCards().front;
        if (card && card.querySelectorAll('.dt-pics figure').length > 1)
          showStageImage(card, (+card.dataset.img || 0) + dir);
        break;
      }
      case ' ': {
        // 与详情同一语义：开当前正在看的那张图的大图（盖在回看之上）
        e.preventDefault();
        const p = R.posts[R.idx];
        const card = detailCards().front;
        const pc = p?.pics?.[Math.max(0, +card?.dataset.img || 0)] || p?.pics?.[0];
        if (!pc) break;
        const i = S.images.findIndex((im) => im.p.sid === p.sid && im.pc.file === pc.file);
        if (i >= 0) openLB(i, p.sid);
        break;
      }
      case 'm': case 'M': favorite(); break;
      case 'l': case 'L': openLink(); break;
      case 't': case 'T': {
        const inp = $('#detail .detail-card.is-front .dt-cinput');   // 回看里也能随手批注
        if (inp) inp.focus();
        break;
      }
      case 'Tab': trapTab(e, $('#detail')); break;
    }
  }

  return { open, close, active: () => R.active, reaffirm, nav, key };
}
