/* 最近批注流：全库批注按时间倒排——「我这周在想什么图」。
   轻全屏层（悬浮球「批注」入口），点行 = 打开该帖详情并聚焦评论区；滚动到底自动续页。 */
export function createNotes({ $, api, toast, esc, commentBodyHTML, fmtCommentTime,
                              openDetail, animatedHide, cancelHide }) {
  const N = { active: false, cursor: null, hasMore: true, loading: false };
  let returnFocus = null;   // dialog 焦点还原：打开时记来处，关闭时还回去

  async function fetchBatch(reset) {
    if (N.loading) return;
    if (!reset && !N.hasMore) return;
    N.loading = true;
    try {
      const q = new URLSearchParams({ limit: 100 });
      if (!reset && N.cursor) q.set('cursor', N.cursor);
      const r = await api('/api/comments?' + q);
      const list = $('#notes-list');
      if (reset) list.innerHTML = '';
      for (const it of r.items || []) {
        const row = document.createElement('button');
        row.className = 'note-row';
        row.innerHTML = `
          ${it.thumb ? `<img class="note-thumb" src="/media/${esc(it.thumb)}" alt="" loading="lazy" decoding="async">`
                     : '<span class="note-thumb"></span>'}
          <span class="note-main"><span class="note-body">${commentBodyHTML(it.body)}</span>
            <span class="note-meta">${esc(it.author)} · ${fmtCommentTime(it.created_at)}</span></span>`;
        row.onclick = () => { close(); openDetail(it.sid, true); };
        list.appendChild(row);
      }
      N.cursor = r.next_cursor || null;
      N.hasMore = !!r.has_more;
      if (reset && !(r.items || []).length)
        list.innerHTML = '<p class="note-empty">还没有批注——在详情里给图写第一条。</p>';
    } catch (e) {
      toast(e.message);
      if (reset)   // 首屏读取失败：等待态换成错误态（翻页失败不动已有行，仅 toast）
        $('#notes-list').innerHTML = `<div class="note-empty">批注读取失败：${esc(e.message)}</div>`;
      if (!reset) {
        N.hasMore = false;
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'note-empty note-retry';
        retry.textContent = '加载失败，点此重试';
        retry.onclick = () => { retry.remove(); N.hasMore = true; fetchBatch(false); };
        $('#notes-list').appendChild(retry);
      }
    }
    finally { N.loading = false; }
  }

  async function open() {
    if (N.active) return;
    N.active = true;
    N.cursor = null; N.hasMore = true;
    cancelHide($('#notes-view'));
    $('#notes-view').hidden = false;
    document.body.style.overflow = 'hidden';
    returnFocus = document.activeElement;   // 先记来处再移焦点（dialog 语义入口）
    $('#notes-close').focus();
    $('#notes-list').innerHTML = '<div class="note-empty">正在读取批注…</div>';
    await fetchBatch(true);
  }

  function close() {
    if (!N.active) return;
    N.active = false;
    animatedHide($('#notes-view'));
    returnFocus?.focus?.();   // 还原来处焦点；元素可能已移除，可选链保护
    const lb = $('#lightbox');
    if ($('#detail').hidden && lb.hidden) document.body.style.overflow = '';
  }

  $('#notes-close').onclick = close;
  $('#notes-view').addEventListener('click', (e) => {
    if (e.target === $('#notes-view')) close();
  });
  $('#notes-list').addEventListener('scroll', () => {
    const el = $('#notes-list');
    if (N.active && N.hasMore && !N.loading && el.scrollTop + el.clientHeight > el.scrollHeight - 200)
      fetchBatch(false);
  }, { passive: true });

  return { open, close, active: () => N.active };
}
