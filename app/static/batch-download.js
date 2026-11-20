/* 下载预览与打包发送同一筛选参数；异步返回只更新当前选择。
   面板常驻设置窗口的「批量下载」分区,openBatch 由分区激活钩子调用,只重置状态并刷新。 */
export function initBatch({S, $, api, toast}) {
  const B = {scope:'kept',sub:'all',checked:new Set(),data:null,version:0,select:'',packing:0,refreshing:false};
  function params(filtered=true) {
    const q = new URLSearchParams({scope:B.scope, thumb:$('#b-thumb').checked ? 1 : 0,
      subfolder:$('#b-subfolder').checked ? 1 : 0});
    if (B.scope === 'selected') q.set('select', B.select);
    if (filtered && B.checked.size) q.set(B.sub === 'author' ? 'authors' : 'dates', [...B.checked].join(','));
    return q;
  }
  /* 打包改走任务框架：POST 登记 job → 轮询 /api/jobs/download → complete 后 iframe 领 zip。
     go 为触发按钮（打包期间与 #b-go 一起禁用）；v 锁定面板版本，refresh 重算期间不再抢写状态文案。
     packing/refreshing 双闸：任一打包在途或当前版本重算未定时 #b-go 保持禁用，收尾互不抢写按钮态。 */
  async function zipViaJob(q, label, btn) {
    const go = $('#b-go');
    const v = B.version;
    go.disabled = true;
    if (btn) btn.disabled = true;
    B.packing++;
    $('#b-total').textContent = label || '正在打包…';
    try {
      const job = (await api('/api/download/zip?' + q.toString(), 'POST')).job;
      for (;;) {
        await new Promise(r => setTimeout(r, 1000));
        const cur = (await api('/api/jobs/download')).job;
        if (cur.id !== job.id) return;   // 面板已被更新的打包任务接管，这里不再重复交付
        if (cur.state === 'queued' || cur.state === 'running') {
          if (v === B.version) {
            const p = cur.progress || {};
            $('#b-total').textContent = p.total ? `打包中 ${p.done}/${p.total}` : '打包中…';
          }
          continue;
        }
        if (cur.state === 'complete') {
          if (v === B.version) $('#b-total').textContent = `共 ${cur.result?.entries ?? 0} 张 · 已开始下载`;
          // 隐藏下载 frame 领取 zip，浏览器下载条接管；主页面与筛选状态保持可用。
          $('#download-frame').src = '/api/download/zip-file?job=' + job.id;
        } else {
          const msg = cur.error || '打包失败,请重试';
          toast(msg);
          if (v === B.version) $('#b-total').textContent = msg;
        }
        return;
      }
    } catch (e) {
      if (v === B.version) $('#b-total').textContent = e.message;
      toast(e.message);
    } finally {
      B.packing--;
      if (btn) btn.disabled = false;
      go.disabled = B.packing > 0 || B.refreshing || !B.data?.pics || !!B.data?.over_limit;
    }
  }
  function renderChunks(rawList, isAuthor) {
    const chunksBox = $('#b-chunks');
    if (!chunksBox) return;
    if (!B.data?.over_limit || !rawList || !rawList.length) {
      chunksBox.innerHTML = '';
      chunksBox.hidden = true;
      return;
    }
    // 若用户勾选了细分项，分卷仅在勾选项中切分，绝不违背预期回退全库
    let list = B.checked.size
      ? rawList.filter(item => B.checked.has(isAuthor ? item.uid : item.date))
      : [...rawList];
    if (!list.length) {
      chunksBox.innerHTML = '';
      chunksBox.hidden = true;
      return;
    }
    if (!isAuthor) {
      list.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
    }
    const chunks = [];
    let cur = { keys: [], n: 0, label: '', last: '', overSingle: false };
    for (const item of list) {
      const n = item.n || 0;
      const key = isAuthor ? item.uid : item.date;
      if (n > 500) {
        if (cur.keys.length > 0) { chunks.push(cur); cur = { keys: [], n: 0, label: '', last: '', overSingle: false }; }
        chunks.push({
          keys: [key], n, label: isAuthor ? (item.author || item.uid) : item.date,
          last: isAuthor ? (item.author || item.uid) : item.date, overSingle: true
        });
        continue;
      }
      if (cur.keys.length > 0 && cur.n + n > 500) {
        chunks.push(cur);
        cur = { keys: [], n: 0, label: '', last: '', overSingle: false };
      }
      cur.keys.push(key);
      cur.n += n;
      if (!cur.label) cur.label = isAuthor ? (item.author || item.uid) : item.date;
      cur.last = isAuthor ? (item.author || item.uid) : item.date;
    }
    if (cur.keys.length > 0) chunks.push(cur);
    if (chunks.length === 0 || (chunks.length === 1 && !chunks[0].overSingle)) {
      chunksBox.innerHTML = '';
      chunksBox.hidden = true;
      return;
    }
    chunksBox.hidden = false;
    chunksBox.innerHTML = '<div class="b-chunks-title"><span class="hint">超出 500 帖单包限制，已为您规划分卷：</span></div>';
    const row = document.createElement('div');
    row.className = 'b-chunks-row';
    chunks.forEach((chunk, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pbtn ghost b-chunk-btn';
      const rangeText = chunk.label === chunk.last ? chunk.label : `${chunk.label} ~ ${chunk.last}`;
      if (chunk.overSingle) {
        btn.disabled = true;
        btn.textContent = `分卷 ${idx + 1} (${chunk.n} 帖 · ${rangeText}) · 单项超 500 帖需再细分`;
      } else {
        btn.textContent = `分卷 ${idx + 1} (${chunk.n} 帖 · ${rangeText}) ⤓`;
        btn.onclick = () => {
          const q = new URLSearchParams({
            scope: B.scope,
            thumb: $('#b-thumb').checked ? 1 : 0,
            subfolder: $('#b-subfolder').checked ? 1 : 0
          });
          if (B.scope === 'selected') q.set('select', B.select);
          q.set(isAuthor ? 'authors' : 'dates', chunk.keys.join(','));
          zipViaJob(q, `正在打包分卷 ${idx + 1}…`, btn);
        };
      }
      row.append(btn);
    });
    chunksBox.append(row);
  }

  async function refresh(rebuild=false) {
    const v = ++B.version;
    B.refreshing = true;
    $('#b-go').disabled = true;
    $('#b-total').textContent = '正在计算可下载的图片…';
    const chunksBox = $('#b-chunks');
    if (chunksBox) { chunksBox.innerHTML = ''; chunksBox.hidden = true; }
    try {
      const data = await api('/api/download/options?' + params());
      if (v !== B.version) return;
      B.data = data;
      $('#b-total').textContent = (data.over_limit ? `当前范围 ${data.posts} 帖` : `将下载 ${data.posts} 帖 · ${data.pics} 张去重图片`) +
        (data.quality?.mid ? `；${data.quality.mid} 张为中图，原图仍待补抓` : '') +
        (data.quality?.unknown ? `；${data.quality.unknown} 张旧记录画质未核验` : '') +
        (data.quality?.thumbnail ? '；下载缩略图' : '') +
        (data.missing ? `；${data.missing} 张图片文件缺失，请先补抓` : '') +
        (data.over_limit ? '。单包最多 500 帖，请按博主或日期分批选择。' : '');
      $('#b-go').disabled = B.packing > 0 || !data.pics || data.over_limit;
      if (rebuild || data.over_limit) {
        const all = await api('/api/download/options?' + params(false));
        if (v !== B.version) return;
        const list = B.sub === 'author' ? all.authors : B.sub === 'date' ? all.dates : [];
        if (rebuild) {
          const box = $('#b-buckets'); box.innerHTML = '';
          for (const b of list) {
            const label = document.createElement('label');
            const cb = document.createElement('input'); cb.type = 'checkbox';
            const key = B.sub === 'author' ? b.uid : b.date;
            cb.checked = B.checked.has(key);
            cb.onchange = () => { cb.checked ? B.checked.add(key) : B.checked.delete(key); refresh(); };
            label.append(cb, document.createTextNode(`${b.author || b.date} · ${b.n} 帖` + (b.pics === null ? '' : ` · ${b.pics} 图`)));
            box.append(label);
          }
        }
        if (data.over_limit) {
          const chunkList = B.sub === 'author' ? all.authors : all.dates;
          renderChunks(chunkList, B.sub === 'author');
        }
      }
    } catch (e) { if (v === B.version) { $('#b-total').textContent = e.message; toast(e.message); } }
    finally { if (v === B.version) B.refreshing = false; }
  }
  function openBatch() {
    B.scope = S.selected.size ? 'selected' : S.status === 'favorites' ? 'favorites' : 'kept';
    B.select = [...S.selected].join(','); B.sub = 'all'; B.checked.clear();
    setSeg('#b-scope', B.scope);
    setSeg('#b-sub', B.sub);
    refresh(true);
  }
  /* 范围/细分为设置窗统一分段控件：.on 高亮 + aria-pressed 同步 */
  function setSeg(sel, val) {
    document.querySelectorAll(sel + ' button').forEach(b => {
      const on = b.dataset.v === val;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', String(on));
    });
  }
  $('#pane-batch').addEventListener('change', e => {
    if (['b-thumb','b-subfolder'].includes(e.target.id)) refresh();
  });
  $('#pane-batch').addEventListener('click', e => {
    const b = e.target.closest('#b-scope button, #b-sub button');
    if (!b || b.classList.contains('on')) return;
    if (b.parentElement.id === 'b-scope') B.scope = b.dataset.v;
    else B.sub = b.dataset.v;
    B.checked.clear();
    setSeg('#' + b.parentElement.id, b.dataset.v);
    refresh(true);
  });
  $('#b-go').onclick = () => {
    if ($('#b-go').disabled || B.packing) return;
    zipViaJob(params(), '正在打包…');
  };
  return {openBatch};
}
