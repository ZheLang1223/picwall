/* 标记、收藏与撤销共用串行写队列和视图同步。 */
import { matchesView, comparePosts } from './view-state.js';
import { serialQueue } from './requests.js';
export function createPostActions({S, feed, $, api, toast, insertCard, rebuildImages,
                                  updateMore, queueStatusRefresh, renderLB, getLBIndex,
                                  refreshDetail, getDetailSid, startMaintenance, likedTodayOf}) {
  const enqueue = serialQueue();
  let lastDrop = null;
  let undoTimer = null;
  const readOnly = () => !!S.readOnly;
  function beginWrite(p) {
    p._pendingWrites=(p._pendingWrites || 0)+1; p._settledAt=null; S.decisions.set(p.sid,p);
  }
  function finishWrite(p) {
    p._pendingWrites=Math.max(0,(p._pendingWrites || 0)-1);
    if (!p._pendingWrites) p._settledAt=Date.now();
  }
  function refreshUndoBtns() {
    document.querySelectorAll('#detail .dt-undo').forEach(b => { b.disabled = !lastDrop; });
  }
  /* 位次不变时的就地修补：只改点赞数与状态类，不重建卡片节点。
     连节点都不能换——replaceWith(renderCard) 会换掉 <img>，重新解码一帧
     造成图面快速闪一下（2026-11-07 用户实测）。墙卡上随状态变化的字段
     只有 kept/fav 描边与点赞按钮，逐项补丁即可与重渲等效。 */
  function patchCard(el, p) {
    el.classList.toggle('kept', p.status === 'kept');
    el.classList.toggle('fav', !!p.favorite);
    const like = el.querySelector('.like');
    if (!like || !p.status) return;
    const liked = likedTodayOf(p);
    const cnt = p.status === 'kept' ? (p.keep_count || 1) : 0;
    like.className = `like${p.status === 'kept' ? (liked ? ' done' : ' on') : ''}`;
    like.title = p.status !== 'kept' ? '点赞保留'
      : liked ? '今天已经赞过了' : '赞一下（点赞 +1）';
    like.setAttribute('aria-label', like.title);
    like.setAttribute('aria-pressed', String(p.status === 'kept' && liked));
    let ln = like.querySelector('.ln');
    if (cnt) {
      if (!ln) { ln = document.createElement('span'); ln.className = 'ln'; like.prepend(ln); }
      ln.textContent = cnt;
    } else ln?.remove();
  }

  function removePostImages(sid) {
    if (!Array.isArray(S.images) || !S.images.length) return;
    const start = S.images.findIndex(x => x.p.sid === sid);
    if (start < 0) return;
    let end = start + 1;
    while (end < S.images.length && S.images[end].p.sid === sid) end += 1;
    S.images.splice(start, end - start);
    if (S.images.some(x => x.p.sid === sid)) {
      S.images = S.images.filter(x => x.p.sid !== sid);
    }
  }

  function insertPostImages(p, postIdx) {
    if (!Array.isArray(S.images)) S.images = [];
    removePostImages(p.sid);
    const pics = p.pics || [];
    if (!pics.length) return;
    let imgAt = -1;
    const nextP = S.posts[postIdx + 1];
    if (nextP) imgAt = S.images.findIndex(x => x.p.sid === nextP.sid);
    if (imgAt < 0) {
      imgAt = 0;
      for (let k = 0; k < postIdx; k += 1) imgAt += (S.posts[k].pics || []).length;
      imgAt = Math.min(imgAt, S.images.length);
    }
    const entries = pics.map(pc => ({ p, pc, url: `/media/${pc.file}` }));
    S.images.splice(imgAt, 0, ...entries);
  }

  function syncPost(p) {
    const i = S.posts.findIndex(x => x.sid === p.sid);
    const oldEl = feed.querySelector(`.card[data-sid="${CSS.escape(p.sid)}"]`);
    if (!matchesView(p, S)) {
      oldEl?.remove();
      if (i >= 0) { S.posts.splice(i, 1); S.total = Math.max(0, S.total - 1); }
      removePostImages(p.sid);
    } else if (oldEl && i >= 0 && S.sort !== 'keep_count') {
      // 视图归属与排序位次都不变（时间序/随机序下的点赞 ±1 / 状态翻转）：就地修补，零重建
      patchCard(oldEl, p);
    } else {
      oldEl?.remove();   // 重排路径先摘旧卡：新卡按新位次另插（in-place 分支已由 replaceWith 原位换）
      if (i >= 0) S.posts[i] = p;
      else { S.posts.push(p); S.total += 1; }
      // 随机序是服务端按种子定的虚拟全序，S.posts 的装载次序即它——时间重排会把它洗回去；
      // 回插一律按数组邻位（新入列的帖落在末位，下页自然续在其后）
      if (S.sort !== 'shuffle') S.posts.sort((x, y) => comparePosts(x, y, S.sort));
      const at = S.posts.findIndex(x => x.sid === p.sid);
      // 后继参照定插入点；末张没有后继 → 贴前驱插（保列保位），两条参照都没有才落最短列
      insertCard(p,
        feed.querySelector(`.card[data-sid="${CSS.escape(S.posts[at+1]?.sid || '')}"]`),
        feed.querySelector(`.card[data-sid="${CSS.escape(S.posts[at-1]?.sid || '')}"]`));
      insertPostImages(p, at);
    }
    updateMore();
  }
  function mark(sid, status, opts = {}) {
    if (readOnly()) return null;
    const p = S.posts.find(x => x.sid === sid);
    if (!p) return null;
    const previous = {status:p.status, kept_at:p.kept_at, discarded_at:p.discarded_at,
                      last_confirm_at:p.last_confirm_at};
    const target = opts.toggle && p.status === status ? 'pending' : status;
    if (target === p.status) return {p, gone:false};
    const op = {p, previous, target, expiresAt:Date.now()+6000};
    beginWrite(p); p._operation = op;
    p.status = target;
    p.kept_at = target === 'kept' ? new Date().toISOString() : null;
    p.discarded_at = target === 'discarded' ? new Date().toISOString() : null;
    // 转入 kept 即建账点赞：乐观盖当日账（服务端 mark_snapshot 同步盖章；撤销回快照不清账）
    if (target === 'kept') p.last_confirm_at = new Date().toISOString();
    const gone = !matchesView(p, S);
    syncPost(p);
    lastDrop = op;
    clearTimeout(undoTimer);
    undoTimer = setTimeout(() => {
      if (lastDrop === op) { lastDrop = null; refreshUndoBtns(); }
    },6000);
    undoTimer?.unref?.();
    refreshUndoBtns();
    toast(`「${p.author}」${target === 'kept' ? '已保留' : target === 'discarded' ? '已移入不要' : '已恢复新增'}`,
          {label:'撤销', fn:() => undoLastMark(op)});
    // 入队同步发生；即使连续快筛/立即撤销，服务端顺序仍与操作顺序一致。
    op.request = enqueue(() => api(`/api/posts/${sid}/status`, 'POST', {status:target}));
    op.request.then(r => {
      op.response = r;
      if (p._operation === op) {
        Object.assign(p, r.decision);
        if (S.posts.some(x => x.sid === sid)) syncPost(p);
      }
      queueStatusRefresh();
      if (r.failed_count) toast(`已保留，${r.failed_count} 张归档失败，可重试`,
        {label:'重试归档', fn:() => startMaintenance('archive')});
    }).catch(e => {
      if (p._operation === op) { Object.assign(p, previous); syncPost(p); }
      if (lastDrop === op) { lastDrop = null; refreshUndoBtns(); }
      queueStatusRefresh(); toast(e.message,{label:'刷新核对',fn:()=>location.reload()},{persistent:true});
    }).finally(() => finishWrite(p));
    return {p, gone, settled:op.request};
  }
  async function undoLastMark(op) {
    if (!op || lastDrop !== op || Date.now() > op.expiresAt) { toast('撤销已过期，可在对应视图里恢复'); return null; }
    clearTimeout(undoTimer); lastDrop = null; refreshUndoBtns();
    const marker={}; op.p._operation=marker; beginWrite(op.p);
    try {
      // 先占据队列的位置，避免等待响应期间其他标记插队。
      const r = await enqueue(async () => {
        const marked = await op.request;
        return api(`/api/posts/${op.p.sid}/restore`, 'POST',
                   {previous:marked.previous, expected:marked.decision});
      });
      if (op.p._operation===marker) { Object.assign(op.p, r.decision); syncPost(op.p); }
      queueStatusRefresh();
      if (!$('#detail').hidden) refreshDetail();
      toast(`「${op.p.author}」已撤销`);
      return op.p;   // 四格快筛的 Z 撤销靠返回值判断成败
    } catch (e) {
      // 标记失败或撤销冲突时，恢复服务端事实；断网则回退到最近已确认的状态。
      let decision = op.response?.decision || op.previous;
      try { decision = (await api(`/api/posts/${op.p.sid}`)).post; } catch { /* 保留已确认状态 */ }
      if (op.p._operation === marker) { Object.assign(op.p, decision); syncPost(op.p); }
      if (!$('#detail').hidden) refreshDetail();
      toast(e.message); queueStatusRefresh();
      return null;
    }
    finally { finishWrite(op.p); }
  }
  /* 点赞次数 ±1（保留态专属）：乐观改账、串行落账、失败回滚；
     墙卡点赞心与详情 ±1 同走这里；+1 每帖每天一次（服务端统一拦截，超限 toast 回滚）；
     ×1 是建账起点，−1 到 1 为止（再减走长按删除） */
  async function reaffirm(sid, delta) {
    if (readOnly()) return null;
    const p = S.posts.find(x => x.sid === sid);
    if (!p || p.status !== 'kept') return null;
    if (delta < 0 && (p.keep_count || 1) <= 1) { toast('只赞过一次，长按「−」才是删除'); return null; }
    const previous = {count: p.keep_count || 1, last: p.last_confirm_at};
    const revision = (p._reaffirmRevision || 0) + 1;
    p._reaffirmRevision = revision;
    beginWrite(p);
    p.keep_count = previous.count + delta;
    if (delta > 0) p.last_confirm_at = new Date().toISOString();   // 乐观盖当日账（回滚时还原）
    syncPost(p);
    try {
      const r = await enqueue(() => api(`/api/posts/${sid}/reaffirm`, delta > 0 ? 'POST' : 'DELETE'));
      if (p._reaffirmRevision === revision) { p.keep_count = r.keep_count; syncPost(p); }
      queueStatusRefresh();
      return p;
    } catch (e) {
      if (p._reaffirmRevision === revision) {
        p.keep_count = previous.count;
        p.last_confirm_at = previous.last;
        syncPost(p);
      }
      toast(e.message);
      return null;
    } finally { finishWrite(p); }
  }
  /* 收藏翻转不改排序键（created_at/keep_count），除收藏页外也不改视图归属：
     就地翻卡片琥珀描边即可，不走 syncPost 摘卡重插——列表末张没有后继参照，
     重插会退化成「追加最短列」，卡片跳位（懒加载下列高本就不真实平衡） */
  function applyFavDom(sid, fav) {
    feed.querySelectorAll(`.card[data-sid="${CSS.escape(sid)}"]`)
      .forEach(el => el.classList.toggle('fav', fav));
  }
  async function toggleFavorite(sid) {
    if (readOnly()) return;
    const p = S.posts.find(x => x.sid === sid);
    if (!p) return;
    const target = !p.favorite;
    const revision = (p._favoriteRevision || 0) + 1;
    p._favoriteRevision = revision;
    beginWrite(p); p.favorite = target;
    if (matchesView(p, S)) applyFavDom(sid, target);   // 仍在视图：只换描边不挪卡
    else syncPost(p);                                  // 收藏页取消收藏：摘卡出列
    try {
      await enqueue(() => api(`/api/posts/${sid}/favorite`, 'POST', {favorite:target}));
      queueStatusRefresh();
      if (!$('#lightbox').hidden && S.images[getLBIndex()]?.p.sid === sid) renderLB();
      if (!$('#detail').hidden) refreshDetail();
    } catch (e) {
      if (p._favoriteRevision === revision) {
        p.favorite = !target;
        if (matchesView(p, S)) applyFavDom(sid, p.favorite);
        else syncPost(p);
      }
      queueStatusRefresh(); toast(e.message);
    } finally { finishWrite(p); }
  }
  /* 标签增删：与收藏同构的乐观写（revision 防竞态、失败回滚）。
     卡面不显示标签，唯一视觉后果是标签筛选生效时帖子的视图归属变化——
     matchesView 不再成立才走 syncPost 摘卡，否则只刷新详情里的标签区 */
  async function toggleTag(sid, add = [], remove = []) {
    if (readOnly()) return;
    const p = S.posts.find(x => x.sid === sid);
    if (!p) return null;
    const revision = (p._tagsRevision || 0) + 1;
    p._tagsRevision = revision;
    const previous = p.tags || [];
    const optimistic = [...previous];
    for (const t of remove || []) { const i = optimistic.indexOf(t); if (i >= 0) optimistic.splice(i, 1); }
    for (const t of add || []) if (!optimistic.includes(t)) optimistic.push(t);
    beginWrite(p); p.tags = optimistic;
    if (!matchesView(p, S)) syncPost(p);
    else if (!$('#detail').hidden && getDetailSid?.() === sid) refreshDetail();
    try {
      const r = await enqueue(() => api(`/api/posts/${sid}/tags`, 'POST', {add, remove}));
      if (p._tagsRevision === revision) p.tags = r.tags || optimistic;
      if (!$('#detail').hidden) refreshDetail();   // 服务端返回定序后的标签：详情标签区跟上
      return p;
    } catch (e) {
      if (p._tagsRevision === revision) {
        p.tags = previous;
        if (!matchesView(p, S)) syncPost(p);
      }
      toast(e.message);
      return null;
    } finally { finishWrite(p); }
  }
  return {mark, toggleFavorite, toggleTag, reaffirm, undoLastMark, refreshUndoBtns, enqueueMutation:enqueue, getLastDrop:() => lastDrop};
}
