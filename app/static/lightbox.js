/* 大图浏览：默认只看当前帖（锚定 sid/file，列表重排后也不会串到其他帖）；
   crossPosts() 为真（纯图模式）时按全局图流跨帖——本帖端走到头即接下一帖首图/上一帖末图。 */
export function createLightbox({S,$,animatedHide,cancelHide,resetZoom,crossPosts}) {
  let anchor = null, scopeSid = null, returnFocus = null, imageClickTimer = 0, lastTouchAt = 0;
  const crossing = () => !!(crossPosts && crossPosts());
  function getIndex() {
    return anchor ? S.images.findIndex(x => x.p.sid === anchor.sid && x.pc.file === anchor.file) : -1;
  }
  function openLB(i,sid) {
    const image=S.images[i]; if (!image) return;
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
    clearTimeout(imageClickTimer); imageClickTimer = 0;
    animatedHide($('#lightbox'));
    document.body.style.overflow=$('#detail').hidden ? '' : 'hidden';
    scopeSid=null;anchor=null;filmKey=null;
    returnFocus?.focus();returnFocus=null;
  }
  function renderLB() {
    const i=getIndex(); if (i<0) {closeLB();return;}
    const {p,pc}=S.images[i]; const img=$('#lb-img'),stage=$('.lb-stage');
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
    if (isVideo) {
      const pool0 = crossing() ? S.images : S.images.filter(x=>x.p.sid===scopeSid);
      const pos0 = crossing() ? i : pool0.findIndex(x=>x.pc.file===pc.file);
      $('#lb-prev').disabled=pos0<=0;$('#lb-next').disabled=pos0>=pool0.length-1;
      renderFilm(pool0,pos0);
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
      img.onload = () => { stage.classList.remove('loading');
        stage.classList.toggle('lb-long',
          img.naturalHeight / Math.max(1, img.naturalWidth) > 2); };
      img.onerror = () => stage.classList.remove('loading');
      img.src = src;
    }
    const pool = crossing() ? S.images : S.images.filter(x=>x.p.sid===scopeSid);
    const pos = crossing() ? i : pool.findIndex(x=>x.pc.file===pc.file);
    $('#lb-prev').disabled=pos<=0;$('#lb-next').disabled=pos>=pool.length-1;
    renderFilm(pool,pos);
    for (const delta of [-1,1]) {
      const n=pool[pos+delta];if (!n) continue;
      const preload=new Image();
      preload.src = n.pc.kind === 'video' ? '/media/' + (n.pc.thumb || n.pc.file) : n.url;
      preload.decode?.().catch(()=>{});
    }
  }

  /* 底部胶片缩略图轨道：池变（开图/跨帖/切作用域）才重建，其余只更新高亮与滚动位 */
  let filmKey = null;
  function renderFilm(pool,pos) {
    const film=$('#lb-film');
    const key=pool.length+'|'+pool.map(x=>x.pc.file).join('|');
    if (key!==filmKey) {
      filmKey=key;
      film.hidden=pool.length<=1;
      const gmap=new Map(S.images.map((x,gi)=>[x.pc.file,gi]));
      film.innerHTML=pool.map((x,k)=>{
        const src='/media/'+(x.pc.thumb||x.pc.file);
        return `<button class="lb-thumb${k===pos?' on':''}" data-i="${gmap.get(x.pc.file)}"`
          + ` role="tab" aria-label="第 ${k+1} 张"><img src="${src}" alt="" loading="lazy" decoding="async"></button>`;
      }).join('');
    }
    film.querySelectorAll('.lb-thumb').forEach((b,k)=>{
      b.classList.toggle('on',k===pos);
      b.setAttribute('aria-selected',k===pos?'true':'false');
    });
    film.querySelector('.lb-thumb.on')?.scrollIntoView({block:'nearest',inline:'center',behavior:'smooth'});
  }
  function lbNav(delta) {
    const i=getIndex();if (i<0) return;
    const pool = crossing() ? S.images : S.images.filter(x=>x.p.sid===scopeSid);
    const pos = crossing() ? i : pool.findIndex(x=>x.pc.file===S.images[i].pc.file);
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
