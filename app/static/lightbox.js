import { esc } from './view-state.js';

/* 大图浏览：默认只看当前帖（锚定 sid/file，列表重排后也不会串到其他帖）；
   crossPosts() 为真（纯图模式）时按全局图流跨帖——本帖端走到头即接下一帖首图/上一帖末图。 */
export function createLightbox({S,$,animatedHide,cancelHide,resetZoom,crossPosts}) {
  const FILM_RADIUS = 20, FILM_EDGE = 5;
  let anchor = null, scopeSid = null, returnFocus = null, imageClickTimer = 0, lastTouchAt = 0;
  let lbSeq = 0, preloadTimer = 0, activePreloads = [];
  let filmKey = null, filmStart = 0, filmEnd = 0, activeThumb = null;
  const crossing = () => !!(crossPosts && crossPosts());
  function cancelPreloads() {
    ++lbSeq;
    if (preloadTimer) { clearTimeout(preloadTimer); preloadTimer = 0; }
    for (const im of activePreloads) {
      im.onload = im.onerror = null;
      if (!im.complete) im.src = '';
    }
    activePreloads = [];
  }
  function getIndex() {
    return anchor ? S.images.findIndex(x => x.p.sid === anchor.sid && x.pc.file === anchor.file) : -1;
  }
  function buildPool(currIndex, target) {
    if (crossing()) return { pool: S.images, pos: currIndex, indices: null };
    const pool = [], indices = [];
    let pos = -1;
    const curFile = target?.pc.file ?? S.images[currIndex]?.pc.file;
    for (let idx = 0; idx < S.images.length; idx++) {
      const item = S.images[idx];
      if (item.p.sid === scopeSid) {
        if (pos < 0 && item.pc.file === curFile && item.p.sid === anchor.sid) pos = pool.length;
        pool.push(item);
        indices.push(idx);
      }
    }
    return { pool, pos, indices };
  }
  function openLB(i,sid) {
    const image=S.images[i]; if (!image) return;
    cancelPreloads();
    scopeSid=sid || image.p.sid;
    anchor={sid:image.p.sid,file:image.pc.file};
    returnFocus=document.activeElement;
    clearTimeout(imageClickTimer); imageClickTimer = 0;
    cancelHide($('#lightbox'));$('#lightbox').hidden=false;
    document.body.style.overflow='hidden';renderLB();
    const target = !$('#lb-next').disabled ? $('#lb-next') : $('#lightbox');
    target.setAttribute('tabindex','-1');target.focus();
  }
  function closeLB() {
    cancelPreloads();
    clearTimeout(imageClickTimer); imageClickTimer = 0;
    animatedHide($('#lightbox'));
    document.body.style.overflow=$('#detail').hidden ? '' : 'hidden';
    scopeSid=null;anchor=null;filmKey=null;filmStart=0;filmEnd=0;activeThumb=null;
    returnFocus?.focus();returnFocus=null;
  }
  function renderLB() {
    const i=getIndex(); if (i<0) {closeLB();return;}
    cancelPreloads();
    const seq = lbSeq;
    const item = S.images[i];
    const {p,pc}=item; const img=$('#lb-img'),stage=$('.lb-stage');
    const vid=$('#lb-video'), isVideo = pc.kind === 'video';
    resetZoom();img.style.transform='';img.style.transformOrigin='';img.style.opacity='';
    img.alt=`${p.author} 的图片`;
    // 视频分支：<video> 接管舞台（禁用长图滚动与缩放），海报作封面；翻走时停播清源
    if (vid) {
      if (isVideo) {
        img.hidden = true; vid.hidden = false;
        const vsrc = '/media/' + pc.file;
        if (vid.getAttribute('src') !== vsrc) {
          vid.poster = pc.thumb ? '/media/' + pc.thumb : '';
          vid.src = vsrc;
        }
        stage.classList.remove('lb-long', 'loading');
      } else {
        vid.hidden = true;
        if (vid.getAttribute('src')) { vid.pause?.(); vid.removeAttribute('src'); vid.load?.(); }
        vid.poster = '';
        img.hidden = false;
      }
    }
    const { pool, pos, indices } = buildPool(i, item);
    if (isVideo) {
      $('#lb-prev').disabled=pos<=0;$('#lb-next').disabled=pos>=pool.length-1;
      renderFilm(pool,pos,indices);
      return;
    }
    // 长图（h/w>2）按宽适配 + 舞台原生竖滚：contain 下长图只是一条细线，读不了。
    // 元数据预判立即生效；缺元数据的旧行在解码后按真实比例校正。切图即回顶。
    const long = !!pc.h && !!pc.w && pc.h / pc.w > 2;
    stage.classList.toggle('lb-long', long);
    if (long) stage.scrollTop = 0;
    const src='/media/'+pc.file;
    if (img.getAttribute('src')!==src) {
      stage.classList.add('loading');
      img.onload = () => { if (seq !== lbSeq) return;
        stage.classList.remove('loading');
        stage.classList.toggle('lb-long',
          img.naturalHeight / Math.max(1, img.naturalWidth) > 2); };
      img.onerror = () => { if (seq === lbSeq) stage.classList.remove('loading'); };
      img.src = src;
    }
    $('#lb-prev').disabled=pos<=0;$('#lb-next').disabled=pos>=pool.length-1;
    renderFilm(pool,pos,indices);
    const urls = [];
    for (const delta of [-1,1]) {
      const n=pool[pos+delta]; if (!n) continue;
      urls.push(n.pc.kind === 'video' ? '/media/' + (n.pc.thumb || n.pc.file) : n.url);
    }
    if (urls.length && typeof Image === 'function') {
      preloadTimer = setTimeout(() => {
        preloadTimer = 0;
        if (seq !== lbSeq) return;
        for (const u of urls) {
          const preload = new Image();
          activePreloads.push(preload);
          preload.onload = () => {
            if (seq !== lbSeq) return;
            preload.decode?.().catch(() => {});
          };
          preload.onerror = () => {};
          preload.src = u;
          if (preload.complete && seq === lbSeq) preload.decode?.().catch(() => {});
        }
      }, 40);
    }
  }

  /* 底部胶片缩略图轨道：仅渲染 pos ± 20 窗口（最多 41 张），步进仅直改前后两颗高亮 */
  function filmSignature(pool, start, end) {
    let sig = (crossing() ? 'cross' : scopeSid) + '|' + pool.length + '|' + start + '|' + end + '|'
      + (pool[0]?.pc.file || '') + '|' + (pool[pool.length - 1]?.pc.file || '');
    for (let k = start; k < end; k++) sig += '|' + pool[k].pc.file;
    return sig;
  }
  function getThumbAt(film, pos) {
    return film.children?.[pos - filmStart] || film.querySelector?.(`.lb-thumb[data-pos="${pos}"]`) || null;
  }
  function renderFilm(pool,pos,indices) {
    const film=$('#lb-film');
    if (pool.length <= 1 || pos < 0) {
      film.hidden = true;
      if (filmKey !== null) {
        filmKey = null; filmStart = 0; filmEnd = 0; activeThumb = null;
        film.innerHTML = '';
      }
      return;
    }
    film.hidden = false;
    const nearLeft = filmStart > 0 && pos < filmStart + FILM_EDGE;
    const nearRight = filmEnd < pool.length && pos >= filmEnd - FILM_EDGE;
    const outOfWindow = pos < filmStart || pos >= filmEnd;
    let nextStart = filmStart, nextEnd = filmEnd;
    if (filmKey === null || outOfWindow || nearLeft || nearRight) {
      nextStart = Math.max(0, pos - FILM_RADIUS);
      nextEnd = Math.min(pool.length, pos + FILM_RADIUS + 1);
    }
    const sig = filmSignature(pool, nextStart, nextEnd);
    if (sig !== filmKey) {
      filmKey = sig;
      filmStart = nextStart;
      filmEnd = nextEnd;
      let html = '';
      for (let k = filmStart; k < filmEnd; k++) {
        const x = pool[k];
        const gi = indices ? indices[k] : k;
        const src = '/media/' + (x.pc.thumb || x.pc.file);
        const on = k === pos;
        html += `<button class="lb-thumb${on ? ' on' : ''}" data-i="${esc(gi)}" data-pos="${k}"`
          + ` role="tab" aria-selected="${on ? 'true' : 'false'}" aria-label="第 ${k + 1} 张">`
          + `<img src="${esc(src)}" alt="" loading="lazy" decoding="async"></button>`;
      }
      film.innerHTML = html;
      activeThumb = getThumbAt(film, pos);
      activeThumb?.scrollIntoView?.({block:'nearest',inline:'center',behavior:'smooth'});
      return;
    }
    const nextThumb = getThumbAt(film, pos);
    if (activeThumb && activeThumb !== nextThumb) {
      activeThumb.classList?.remove('on');
      activeThumb.setAttribute?.('aria-selected', 'false');
    }
    if (nextThumb) {
      nextThumb.classList?.add('on');
      nextThumb.setAttribute?.('aria-selected', 'true');
      nextThumb.scrollIntoView?.({block:'nearest',inline:'center',behavior:'smooth'});
    }
    activeThumb = nextThumb;
  }
  function lbNav(delta) {
    const i=getIndex();if (i<0) return;
    const { pool, pos } = buildPool(i, S.images[i]);
    const next=pool[pos+delta]; if (!next) return;
    anchor={sid:next.p.sid,file:next.pc.file};
    if (crossing()) scopeSid=next.p.sid;   // 跨帖后作用域跟帖走，中途关掉跨帖开关也不串帖
    renderLB();
  }
  $('#lb-prev').onclick=()=>lbNav(-1);$('#lb-next').onclick=()=>lbNav(1);
  $('#lb-film').addEventListener('click',e=>{
    const b=e.target.closest('.lb-thumb'); if (!b) return;
    const img=S.images[Number(b.dataset.i)]; if (!img) return;
    anchor={sid:img.p.sid,file:img.pc.file};
    if (crossing()) scopeSid=img.p.sid;
    renderLB();
  });
  $('#lightbox').addEventListener('touchstart',()=>{lastTouchAt=Date.now();},
    {capture:true,passive:true});
  $('#lightbox').addEventListener('click',e=>{
    if (e.target===$('#lightbox') || e.target.classList.contains('lb-stage')) { closeLB(); return; }
    if (e.target !== $('#lb-img')) return;
    // 手机双击仍用于缩放/收藏；单击图片稍作等待，让双击的第二次触点取消退出动作。
    if (Date.now() - lastTouchAt < 800) return;
    clearTimeout(imageClickTimer);
    if (e.detail > 1) { imageClickTimer = 0; return; }
    imageClickTimer = setTimeout(closeLB, 320);
  });
  return {openLB,closeLB,renderLB,lbNav,getIndex};
}
