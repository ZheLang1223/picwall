import test from 'node:test';
import assert from 'node:assert/strict';
import {matchesView, comparePosts, keepBadge, avatarSrc, mediaSrc, esc, safeUrl, markTerm, shuffleSeed} from '../app/static/view-state.js';
import {esc as kioskEsc, nl2br as kioskNl2br, safeMedia as kioskSafeMedia} from '../app/static/kiosk.js';
import {filterWeiboCookies, isInsecureLanHttp, originPatternFor} from '../extension/cookies.js';
import {ssPrefs, expandPosts} from '../app/static/slideshow.js';
import {serialQueue} from '../app/static/requests.js';
import {createPostActions} from '../app/static/post-actions.js';
import {createMaintenanceJobs, maintenanceResult} from '../app/static/maintenance-jobs.js';
import {captureGroupText, captureTitle, emptyState} from '../app/static/content-state.js';
import {createQuad, pickNext, nextFocus} from '../app/static/quad.js';
import {createTimeline, dayOf, dayLabel, monthLabel, planRows} from '../app/static/timeline.js';
import {createLightbox} from '../app/static/lightbox.js';
import {enterFilters, statsLine, profileSpan, profileUrl, skinFromPrefs, SKINS} from '../app/static/author-view.js';
const s={status:'all',group:'all',author:'all',range:'all',source:'all',hasCmts:'all',searchText:''};
const p={sid:'a',uid:'u1',list_id:'g1',status:'kept',favorite:true,text:'hello',via:'',created_at:'2026-10-01T00:00:00+08:00'};
test('部分采集和翻页上限均不被表达为全部成功',()=>{
 const group={status:'done',received:10,new_posts:2,new_pics:3,skipped:7,failed:1,coverage:'page_limit'};
 assert.match(captureGroupText(group),/1 条图片未齐/);
 assert.match(captureGroupText(group),/日期范围未覆盖完/);
 assert.equal(captureTitle({outcome:'incomplete'}),'范围未覆盖完');
 assert.equal(captureTitle({outcome:'partial'}),'部分完成');
 assert.equal(captureTitle({error:'legacy error'}),'未完成');
});
test('空筛选和已有相册均不误导用户重新连接或采集',()=>{
 const existing={...s,status:'pending',isAdmin:true,st:{counts:{kept:5},cookie:{state:'unset'}}};
 assert.match(emptyState(existing).text,/查看已有图片/);
 assert.equal(emptyState(existing).action,undefined);
 assert.match(emptyState({...existing,searchText:'none'}).text,/清空筛选条件/);
 const fresh={...s,status:'pending',isAdmin:true,st:{counts:{},cookie:{state:'unset'},groups:[]}};
 assert.equal(emptyState(fresh).action,'connection');
 assert.equal(emptyState({...fresh,isAdmin:false}).action,undefined);
});
test('归档任务完成仍准确说明缺失原图和历史副本',()=>{
 const msg=maintenanceResult('archive',{current_complete:false,history:{missing:2,missing_originals:1,historical_missing:3,historical_corrupt:1}});
 assert.match(msg,/2 张保留图片未归档/);
 assert.match(msg,/先补抓/);
 assert.match(msg,/历史归档：3 条缺失、1 条校验失败/);
 assert.doesNotMatch(msg,/当前保留图片已归档/);
});
test('后台任务运行期间不报完成，重复观察只保留一条查询链',async()=>{
 const notices=[], seen=[];
 let finish, complete;
 const wait=new Promise(resolve=>{finish=resolve;});
 const done=new Promise(resolve=>{complete=resolve;});
 let calls=0;
 const job={kind:'archive',id:'one',state:'running'};
 const monitor=createMaintenanceJobs({
  api:async()=>{calls++; await wait; return {job:{...job,state:'complete',result:{current_complete:true,history:{historical_missing:1}}}};},
  toast:msg=>notices.push(msg),onJob:r=>seen.push(r),onComplete:()=>complete(),pause:async()=>{},
 });
 monitor.observe(job); monitor.observe(job);
 assert.equal(calls,1); assert.equal(notices.length,0);
 finish(); await done;
 assert.equal(calls,1); assert.equal(seen.at(-1).state,'complete');
 assert.match(notices.at(-1),/当前保留图片已归档；历史归档：1 条缺失/);
});
test('任务查询连续失败结束等待且保留明确提示，不伪报成功',async()=>{
 let calls=0, finish;
 const done=new Promise(resolve=>{finish=resolve;});
 const notices=[];
 const monitor=createMaintenanceJobs({api:async()=>{calls++; throw new Error('offline');},
  toast:(msg,action,opts)=>{notices.push({msg,opts});finish();},onJob:()=>{},
  onComplete:()=>assert.fail('未读到结果不能完成'),pause:async()=>{},
 });
 monitor.observe({kind:'archive',id:'one',state:'running'}); await done;
 assert.equal(calls,3); assert.equal(notices.length,1);
 assert.match(notices[0].msg,/后台任务可能仍在进行/);
 assert.equal(notices[0].opts.persistent,true);
});
test('同帖多分组在撤销回插时仍匹配当前分组',()=>{
 assert.equal(matchesView({...p,groups:['g1','g2']},{...s,group:'g2'}),true);
 assert.equal(matchesView({...p,groups:['g1','g2']},{...s,group:'g3'}),false);
});
test('标签筛选任一命中即入选（OR），无标签帖出局，缺省不筛',()=>{
 assert.equal(matchesView({...p,tags:['风景','猫']},{...s,tags:'猫'}),true);
 assert.equal(matchesView({...p,tags:['风景','猫']},{...s,tags:'风景,装机'}),true);
 assert.equal(matchesView({...p,tags:['风景']},{...s,tags:'装机'}),false);
 assert.equal(matchesView({...p,tags:[]},{...s,tags:'风景'}),false);
 assert.equal(matchesView({...p,tags:['风景']},{...s,tags:'all'}),true);
 assert.equal(matchesView(p,{...s,tags:'风景'}),false);   // 无标签的帖与被标签筛选出局（与服务端 EXISTS 同口径）
 assert.equal(matchesView(p,s),true);
});
test('回插统一检查作者、搜索、收藏与状态',()=>{
 assert.equal(matchesView(p,{...s,author:'u1'}),true);
 assert.equal(matchesView(p,{...s,author:'u2'}),false);
 assert.equal(matchesView(p,{...s,status:'favorites'}),true);
 assert.equal(matchesView(p,{...s,status:'pending'}),false);
 assert.equal(matchesView(p,{...s,searchText:'missing'}),false);
 assert.equal(matchesView(p,{...s,searchText:'HELLO'}),true);
});
test('搜索纯评论命中不被前端丢弃：comment_match 与正文命中同权',()=>{
 assert.equal(matchesView({...p,comment_match:'已查到机位：日出前半小时到'},{...s,searchText:'日出'}),true);
 assert.equal(matchesView({...p,comment_match:'无关批注'},{...s,searchText:'日出'}),false);
 assert.equal(matchesView({...p,text:'山间日出'},{...s,searchText:'日出'}),true);
});
test('头像相对路径映射到媒体路由',()=>{
 assert.equal(avatarSrc('avatars/t_ab.jpg'),'/media/avatars/t_ab.jpg');
 assert.equal(avatarSrc('https://pbs.twimg.com/profile_images/a_normal.jpg'),'https://pbs.twimg.com/profile_images/a_normal.jpg');
 assert.equal(avatarSrc(''), '');
 assert.equal(avatarSrc(undefined), undefined);
});
test('X 博主筛选值与 payload 的带前缀 uid 直接相等匹配',()=>{
 assert.equal(matchesView({...p,uid:'x:luoluokun_',source:'x'},{...s,author:'x:luoluokun_'}),true);
 assert.equal(matchesView({...p,uid:'x:luoluokun_',source:'x'},{...s,author:'x:别人'}),false);
 assert.equal(matchesView({...p,uid:'x:luoluokun_',source:'x'},{...s,author:'luoluokun_'}),false);
 assert.equal(matchesView({...p,uid:'u1'},{...s,author:'x:u1'}),false);
});
test('东八区跨日时间窗口与服务端一致',()=>{
 const now=Date.parse('2026-10-02T01:00:00+08:00');
 assert.equal(matchesView(p,{...s,range:'today'},now),false);
 assert.equal(matchesView(p,{...s,range:'yesterday'},now),true);
 assert.equal(matchesView({...p,created_at:'2026-10-02T00:00:00+08:00'}, {...s,range:'yesterday'},now),false);
});
test('同秒发帖按 sid 稳定排序',()=>{
 assert.deepEqual([{...p,sid:'b'},{...p,sid:'a'}].sort(comparePosts).map(x=>x.sid),['a','b']);
});
test('随机档种子：Node 无 sessionStorage 回落日种子，daily 档固定用东八区日期',()=>{
 const d='d'+new Date(Date.now()+8*3600e3).toISOString().slice(0,10);
 assert.equal(shuffleSeed({}),d);   // 本文件跑在 Node：无 sessionStorage，走日种子回落
 assert.equal(shuffleSeed({'shuffle-seed':'daily'}),d);
 assert.equal(shuffleSeed({'shuffle-seed':'daily'}),shuffleSeed({'shuffle-seed':'daily'}));
});
test('连按操作按顺序发送，失败不阻断后续撤销',async()=>{
 const enqueue=serialQueue(),seen=[];
 const first=enqueue(async()=>{seen.push('keep');throw new Error('offline');});
 const second=enqueue(async()=>seen.push('discard'));
 const third=enqueue(async()=>seen.push('undo'));
 await assert.rejects(first);await second;await third;
 assert.deepEqual(seen,['keep','discard','undo']);
});

test('标记失败后立即撤销，断网时恢复原状态',async()=>{
 const oldDocument=globalThis.document,oldCSS=globalThis.CSS;
 globalThis.document={querySelectorAll:()=>[]};globalThis.CSS={escape:x=>x};
 try {
  const post={...p,status:'pending',kept_at:null,discarded_at:null};
  const state={...s,posts:[post],images:[],decisions:new Map(),total:1};
  const messages=[];
  const actions=createPostActions({S:state,feed:{querySelectorAll:()=>[],querySelector:()=>null,insertBefore:()=>{}},
   $:()=>({hidden:true}),api:async()=>{throw new Error('offline');},toast:x=>messages.push(x),
   insertCard:()=>({}),rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
   renderLB:()=>{},getLBIndex:()=>0,refreshDetail:()=>{}});
  actions.mark(post.sid,'discarded');
  await actions.undoLastMark(actions.getLastDrop());
  assert.equal(post.status,'pending');assert.equal(post.discarded_at,null);
  assert.equal(state.posts.length,1);assert.equal(post._pendingWrites,0);
  assert.ok(messages.includes('offline'));
 } finally {globalThis.document=oldDocument;globalThis.CSS=oldCSS;}
});

test('墙卡滑动决定退役：滑动标记与方向戳不再存在，分拣统一走快筛会话入口', async()=>{
 const fs=await import('node:fs/promises');
 const source=await fs.readFile(new URL('../app/static/app.js',import.meta.url),'utf8');
 assert.ok(!source.includes('swipeSuppressClick'), '滑动抑制标志应已删除');
 assert.ok(!source.includes("content: '✓ 保留'"), '墙面方向大字戳应已删除');
 assert.ok(!source.includes("content: '✕ 不要'"), '墙面方向大字戳应已删除');
 assert.ok(source.includes('startQuickTriage'), '快筛会话入口应存在');
 // 快筛单入口：桌面分流四格会话（不再有双卡/四格偏好漂移）
 assert.ok(source.includes('quad.open(); return;'), '桌面快筛应直达四格会话');
 assert.ok(source.includes("label: '多选'"), '悬浮球应有多选显式入口（长按之外的发现路径）');
 assert.ok(source.includes("['#sel-keep', '#sel-drop', '#sel-tags', '#sel-download'].forEach") && source.includes('$(q).disabled = n === 0'),
   '选择模式未勾选时底栏动作钮应置灰');
});

test('详情键位：←→ 切图 + 同向双击翻帖 + PageUp/Down 翻帖，批注聚焦改 T', async()=>{
 const fs=await import('node:fs/promises');
 const source=await fs.readFile(new URL('../app/static/app.js',import.meta.url),'utf8');
 assert.ok(source.includes("'PageDown'") && source.includes("'PageUp'"), 'PageUp/PageDown 翻帖应存在');
 assert.ok(source.includes('dtArrow'), '←→ 双击翻帖计时器应存在');
 assert.ok(source.includes('showStageImage'), '←→ 应复用图舞台切图函数');
 assert.ok(source.includes("e.key === 't' || e.key === 'T'"), '批注聚焦应改 T');
 assert.ok(!source.includes("e.key === 'n' || e.key === 'N'"), '旧 N 键应已退役');
 const review=await fs.readFile(new URL('../app/static/review.js',import.meta.url),'utf8');
 assert.ok(review.includes("case 't': case 'T':"), '回看批注聚焦应改 T');
 assert.ok(!review.includes("case 'n'"), '回看旧 N 键应已退役');
});

test('卡片模式一致性：回看←→对齐详情、四格有可见出口、换视图重置可撤销', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const app=await fs.readFile(new URL('app.js',root),'utf8');
 const review=await fs.readFile(new URL('review.js',root),'utf8');
 const quad=await fs.readFile(new URL('quad.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 const html=await fs.readFile(new URL('../templates/index.html',root),'utf8');
 // 回看 ←→ 与详情同一规则：单击切本帖图、350ms 双击翻帖、PageUp/PageDown 直接翻帖
 assert.ok(review.includes('R.arrow'), '回看应有与详情同款的 ←→ 双击判定计时');
 assert.ok(review.includes("case 'PageUp'") && review.includes("case 'PageDown'"), '回看应支持 PageUp/PageDown 翻帖');
 assert.ok(review.includes("case ' '"), '回看应支持空格开当前图大图');
 // 回看交互并入普通语义：F/D = 点赞 ±1 不前进（相向按键互为撤销），组到底自动续刷
 assert.ok(review.includes('reaffirm(1)') && review.includes('reaffirm(-1)'), '回看 F/D 应为点赞 ±1');
 assert.ok(!review.includes("advance('keep')") && !review.includes('flyOutGhost'), '回看不应再有前进流与幽灵飞出（普通保留帖 ±1 不前进）');
 assert.ok(review.includes('fetchMore'), '回看组到底应自动取下一组续刷');
 assert.ok(app.includes("&& !/^[1-4]$/.test(e.key)"), '回看里 1-4 应放行给视图切换（与普通详情一致）');
 assert.ok(app.includes("review.reaffirm(status === 'kept' ? 1 : -1)"), '回看拖拽/标记应走 ±1 不前进');
 // 四格会话有可见出口与常驻键位提示（键盘流不再只靠一次性 toast 与 Esc 逃生）
 assert.ok(quad.includes('closeBtn.onclick'), '四格应绑定 ✕ 关闭钮');
 assert.ok(html.includes('class="q-hint"'), '四格会话应有常驻键位提示条');
 assert.ok(css.includes('.q-close') && css.includes('.q-hint'), '四格出口与提示样式应存在');
 assert.ok(quad.includes('pics[Q.zoom ? Q.zoomIdx : (Q.picIdx[Q.focus] || 0)]'),
   '四格空格应锚定正在轮换/细看的那张图，不是固定首图');
 // 换视图重置筛选可感知、可撤销
 assert.ok(app.includes('const snap = hadFilters'), '换视图应在伴随筛选生效时快照');
 assert.ok(app.includes('撤销切换'), '换视图重置筛选应给出可撤销 toast');
 // 编年 V 键被浮层阻塞时不再静默
 const tl=await fs.readFile(new URL('timeline.js',root),'utf8');
 assert.ok(tl.includes('先退出浮层，再切换布局'), 'V 键被浮层阻塞应提示而非静默');
});

test('编年跳转遇到加载失败应停止重试', async()=>{
 const fs=await import('node:fs/promises');
 const tl=await fs.readFile(new URL('../app/static/timeline.js',import.meta.url),'utf8');
 assert.ok(tl.includes("if (S.loadError) { toast('装载失败，请稍后重试'); return; }"),
   'gotoDay 翻页遇加载失败应立即停止并提示，不再重试 60 次刷屏');
});

test('交互五项：点卡开详情、纯图点图跨帖看图、悬浮球纯图入口与四格子浮窗、换视图清筛选', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const app=await fs.readFile(new URL('app.js',root),'utf8');
 const lb=await fs.readFile(new URL('lightbox.js',root),'utf8');
 const fabSrc=await fs.readFile(new URL('fab.js',root),'utf8');
 const feedClick=app.match(/feed\.addEventListener\('click'[\s\S]*?\n\}\);/);
 assert.ok(feedClick, '墙卡点击分发应存在');
 assert.ok(feedClick[0].includes("dataset.pureimg === '1'"), '点图开大图应仅限纯图模式');
 assert.ok(app.includes('crossPosts'), 'app 应向大图注入跨帖开关');
 assert.ok(lb.includes('crossPosts') && lb.includes('scopeSid=next.p.sid'), '纯图大图应跨帖连看');
 assert.ok(app.includes('label: () => `纯图'), '悬浮球应有纯图入口');
 const settingsJs=await fs.readFile(new URL('../app/static/settings.js',import.meta.url),'utf8');
 assert.ok(!settingsJs.includes('pureimg'), 'settings.js 不应再有纯图逻辑（防止残留引用在启动期炸掉整个初始化）');
 const settingsHtml=await fs.readFile(new URL('../app/templates/index.html',import.meta.url),'utf8');
 assert.ok(!settingsHtml.includes('set-pureimg'), '设置卡不应再有「纯图模式」行（入口只在悬浮球）');
 assert.ok(!fabSrc.includes('fab-flyout'), '快筛单入口后悬停子浮窗应已退役');
 assert.ok(app.includes("S.hasCmts = 'all'; S.tags = 'all'; S.searchText = '';"), '换视图应清空伴随筛选（含标签）');
 assert.ok(!app.includes('favOnly') && !app.includes("fav:S."), '幽灵 favOnly 状态应已收编（fav=1 折叠进收藏视图）');
 assert.ok(app.includes("p.get('fav') === '1') S.status = 'favorites'"), '旧链接 ?fav=1 应折叠为收藏视图');
});

test('视图面板：屏幕居中、「链接」行退役、搜索只在回车应用', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const app=await fs.readFile(new URL('app.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 const html=await fs.readFile(new URL('../app/templates/index.html',import.meta.url),'utf8');
 assert.ok(!app.includes('f-copylink'), '复制视图链接处理器应已退役');
 assert.ok(!html.includes('f-copylink'), '模板不应再有「链接」行');
 assert.ok(!css.includes('#f-copylink'), '样式不应再有链接行规则');
 assert.ok(!app.includes('searchTimer'), '搜索防抖应已退役（只在回车应用）');
 assert.ok(css.includes('translateX(-50%)'), '视图面板应顶栏下方水平居中');
 // 视图面板聚焦于辅助属性筛选（来源/标签/时间/评论），状态/排序/布局均已从面板退役收敛至双轨
 assert.ok(!html.includes('id="f-sort-ctl"') && !html.includes('id="f-layout"'), '排序与布局已从面板退役并收敛至双轨');
 assert.ok(html.includes('id="f-range"') && html.includes('id="f-has-cmts"'), '面板应包含时间范围与评论筛选');
 // 类型筛选已删除：面板无行、状态无字段、URL 不再读写
 assert.ok(!html.includes('f-type') && !app.includes('TYPE_CHIPS'), '类型筛选行应已删除');
 assert.ok(!app.includes("p.set('type'") && !app.includes('type:S.type'), '筛选请求不应再带 type 参数');
});

test('快筛只留四格：数字键 1-4、Shift+Q/W 进快筛、双卡与其设置已退役', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const app=await fs.readFile(new URL('app.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 const html=await fs.readFile(new URL('../templates/index.html',root),'utf8');
 const settingsJs=await fs.readFile(new URL('settings.js',root),'utf8');
 assert.ok(app.includes("kept: '1', discarded: '2', favorites: '3', all: '4'"), '数字键应重编 1-4');
 assert.ok(app.includes('/^[1-4]$/') && !app.includes('/^[1-5]$/'), '数字视图键应为 1-4');
 // 快筛入口：Shift+Q/W 同走 startQuickTriage，桌面分流四格；窄屏退回单卡详情流
 assert.ok(app.includes("(e.key === 'Q' || e.key === 'q' || e.key === 'W' || e.key === 'w')"), 'Shift+Q/W 进快筛应存在');
 assert.ok(app.includes('quad.open(); return;'), '桌面快筛应直达四格会话');
 // 双卡快筛整族退役：入口参数、belt 类、样式与「快筛默认形态」设置一并清除
 assert.ok(!app.includes('forceBelt') && !app.includes("classList.add('belt')"), '双卡快筛逻辑应已删除');
 assert.ok(!css.includes('#detail.belt'), '双卡样式应已删除');
 assert.ok(!html.includes('pref-triage-mode'), '「快筛默认形态」设置行应已删除');
 assert.ok(!settingsJs.includes('triage-mode'), 'settings.js 不应再读写 triage-mode');
 assert.ok((app.match(/switchStatusView\('kept'\)/g) || []).length >= 1, '快筛筛完应进保留视图');
 const quadFinish=await fs.readFile(new URL('quad.js',root),'utf8');
 assert.ok(quadFinish.includes("switchStatusView('kept')"), '四格筛完应进保留视图');
});

test('待筛入口：并入左悬浮胶囊且保持常规单色导航条目样式（非实心主色按钮）', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const html=await fs.readFile(new URL('../templates/index.html',root),'utf8');
 const railSrc=await fs.readFile(new URL('rail.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 // 左悬浮胶囊待筛按钮：默认 hidden，rail.js 按 pending 数显隐 + 计数，点击直达快筛
 assert.ok(html.includes('id="rail-triage"'), '左悬浮胶囊应有待筛入口按钮');
 assert.ok(html.includes('id="rail-triage-bdg"') && html.includes('id="rail-triage-cnt"'), '待筛按钮应同时支持收起态角标与展开态计数');
 assert.ok(railSrc.includes('triageBtn.hidden = !showTriage'), '待筛按钮应由 syncSession 控制显隐');
 assert.ok(railSrc.includes('R.startQuickTriage()'), '待筛按钮点击应直达快筛');
 assert.ok(css.includes('#rail .bdg'), '待筛角标样式应存在');
 assert.ok(!css.includes('#rail-triage { background: var(--accent)'), '待筛入口不得做成高亮实心按钮');
 // 旧右轨与旧会话卡整族退役
 assert.ok(!html.includes('id="irail"') && !html.includes('id="ir-triage"'), '旧 100vh 右轨应已解散');
 assert.ok(!html.includes('rail-session'), '左轨会话卡应已删除');
 assert.ok(!railSrc.includes('ss-go') && !railSrc.includes('pw_triage_skip'), '会话卡逻辑应已删除');
 assert.ok(!css.includes('.rail-panel .session'), '会话卡样式应已删除');
});

test('左囊直通博主大厅、排序与分组并入右上筛选面板', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const html=await fs.readFile(new URL('../templates/index.html',root),'utf8');
 const railSrc=await fs.readFile(new URL('rail.js',root),'utf8');
 const appSrc=await fs.readFile(new URL('app.js',root),'utf8');
 // 左囊仅保留极简 4 区，博主入口直链 /kiosk 管理大厅，不再在主页左侧嵌长树/分组表
 assert.ok(html.includes('所有博主') && html.includes('href="/kiosk"'), '左囊入口应直通 /kiosk 所有博主大厅');
 assert.ok(!html.includes('id="rail-tree"') && !html.includes('id="rail-authors"')
   && !html.includes('id="rail-groups"') && !html.includes('id="rail-sort"'),
   '左囊不应再内嵌长分组树、博主列表或独立排序项');
 // 排序三档与分组过滤合并进右上 #view-panel，由 #tt-filter 触发并汇总徽章
 assert.ok(html.includes('id="tt-filter"') && html.includes('id="tt-filter-bdg"'), '右上工具条应提供筛选与排序合并入口');
 assert.ok(html.includes('id="f-sort-row"') && html.includes('id="f-sort"'), '#view-panel 应内含排序段');
 assert.ok(html.includes('id="f-group-row"') && html.includes('id="f-group"'), '#view-panel 应内含分组过滤段');
 assert.ok(appSrc.includes('const SORT_CHIPS =') && appSrc.includes("v: 'shuffle', label: '随机'"), 'SORT_CHIPS 应含随机档（三档齐全）');
 assert.ok(appSrc.includes('function renderSortRow()') && appSrc.includes('function renderGroupRow()'), 'app.js 应统一渲染面板排序与分组段');
 assert.ok(railSrc.includes("const sortLabel = S.sort === 'keep_count' ? '点赞序' : S.sort === 'shuffle' ? '随机序' : '';"), '右上筛选按钮 tip 应同步当前排序档');
 assert.ok(appSrc.includes('authorViewOpen:') && appSrc.includes('viewState'), 'pwRail 应暴露 authorViewOpen 与 viewState 钩子');
});

test('悬浮胶囊交互：左囊 120ms hover-intent 无重排覆盖层、右上更多菜单开合、设置/搜索/回顶直连', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const html=await fs.readFile(new URL('../templates/index.html',root),'utf8');
 const railSrc=await fs.readFile(new URL('rail.js',root),'utf8');
 const appSrc=await fs.readFile(new URL('app.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 // 左囊纯 Overlay + 120ms hover-intent，移除钉住/重排推挤逻辑
 assert.ok(railSrc.includes('setPanelOpen(true), 120') && railSrc.includes('setPanelOpen(false), 120'), '左囊应具备 120ms hover-intent 防误触延时');
 assert.ok(!railSrc.includes('pw_rail_open') && !railSrc.includes('pw_rail_pin')
   && !html.includes('id="rail-toggle"') && !css.includes('body.rail-open'),
   '左囊不应残留钉住持久化或 body.rail-open 重排推挤');
 // 右上更多菜单 ⋯：aria-expanded 同步 + 点击/移出收起
 assert.ok(html.includes('aria-controls="tt-more-menu"') && html.includes('id="tt-more-menu"'), '右上更多钮应关联 #tt-more-menu');
 assert.ok(html.includes('aria-expanded="false"') && railSrc.includes('function closeMore()'), '更多菜单应具备受控开合状态机');
 // 搜索：图标激活态 = 词在生效；tip 显当前词；点击直通聚光灯大搜索栏
 assert.ok(railSrc.includes("searchBtn.classList.toggle('on', !!q)"), '搜索词在生效应点亮右上搜索图标');
 assert.ok(railSrc.includes("const t = q ? `搜索：${q}` : '搜索'"), '搜索 tip 应显当前搜索词');
 assert.ok(railSrc.includes("searchBtn.addEventListener('click'") && railSrc.includes('R.openSpotlightSearch?.()'), '右上搜索钮应唤起聚光灯搜索');
 // 设置/回顶部直连：不再遥控桌面隐藏的顶栏元素
 assert.ok(appSrc.includes('scrollTop: scrollFeedTop'), 'pwRail 回顶应直连动作本身');
 assert.ok(appSrc.includes('openSettings: () => settings.openSettings()'), 'pwRail 应暴露 openSettings 钩子');
 assert.ok(!railSrc.includes("getElementById('btn-settings')"), '设置行不应遥控隐藏的 #btn-settings');
 assert.ok(!railSrc.includes("getElementById('btn-top')"), '回顶不应遥控隐藏的 #btn-top');
});

test('悬浮球可拖动：位移阈值区分拖/点、位置持久化、近顶菜单向下', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const fabSrc=await fs.readFile(new URL('fab.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 assert.ok(fabSrc.includes('pointerdown') && fabSrc.includes('pointermove'), '应走 pointer 拖动');
 assert.ok(fabSrc.includes('pw_fab_pos'), '拖动位置应持久化');
 assert.ok(fabSrc.includes('suppressClick'), '拖动后的 click 应被吞掉');
 assert.ok(fabSrc.includes('menu-down'), '近顶应切菜单向下展开');
 assert.ok(css.includes('.fab-menu.menu-down'), '菜单向下展开样式应存在');
 assert.ok(css.includes('touch-action: none'), '球体拖动不应触发页面滚动');
 assert.ok(!fabSrc.includes('fab-flyout') && !css.includes('.fab-flyout'), '子浮窗（双卡/四格子菜单唯一消费者）应随双卡退役');
});

test('瀑布流列栈：CSS 多列退役，追加只进最短列（旧卡零位移）', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const app=await fs.readFile(new URL('app.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 assert.ok(app.includes('function appendToShortestCol'), '最短列追加应存在');
 assert.ok(app.includes('function ensureFeedCols'), '列数变更全量重排应存在');
 // 插卡两模式：后继参照前插 / 末张贴前驱（收藏点赞跳位同源修复）；
 // 位次不变走 patchCard 就地修补（不经插卡，零重建不闪）
 assert.ok(app.includes('insertCard: (p, ref, prev)'), '插卡应支持前驱模式');
 assert.ok(app.includes('prev.after(el)'), '末张无后继应贴前驱插（保列保位）');
 assert.ok(app.includes('likedTodayOf, rebuildImages'), '接线应注入日界判断（就地修补需要）');
 const postActions=await fs.readFile(new URL('post-actions.js',root),'utf8');
 assert.ok(postActions.includes('function patchCard') && postActions.includes('patchCard(oldEl, p)'),
   '位次不变应走 patchCard 就地修补（不重建节点，避免 <img> 重解码闪帧）');
 assert.ok(!css.includes('column-width'), 'CSS 多列应已退役（balance 重配列是排版混乱根源）');
 assert.ok(!css.includes('column-count'), '不应残留 column-count');
 assert.ok(css.includes('.feed-col'), '列栈样式应存在');
});

test('四格：空格大图在最顶层、聚焦强化、多图自动轮换', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const quadSrc=await fs.readFile(new URL('quad.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 assert.ok(css.match(/\.lightbox\s*{[^}]*z-index:\s*70/), '大图应在四格（z 65）之上');
 assert.ok(quadSrc.includes('startRotate') && quadSrc.includes('setInterval'), '多图槽位应自动轮换');
 assert.ok(quadSrc.includes('lastRotate') && quadSrc.includes('i === Q.focus ? 3000 : 1200'), '聚焦慢轮、非聚焦快轮应分周期');
 assert.ok(quadSrc.includes('unref'), '轮换定时器不应挂住测试进程');
 assert.ok(css.includes('.q-slot.focused::after'), '聚焦格应有「裁决中」标识');
 assert.ok(css.includes('.q-slot:not(.focused):not(.empty) { opacity'), '非聚焦格应压暗');
});

test('长图适配：四格三档分派（contain/顶部/居中+角标）、大图按宽滚动', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const quadSrc=await fs.readFile(new URL('quad.js',root),'utf8');
 const lbSrc=await fs.readFile(new URL('lightbox.js',root),'utf8');
 const css=await fs.readFile(new URL('style.css',root),'utf8');
 assert.ok(quadSrc.includes('function fitInfo') && quadSrc.includes('q-fit-top'), '四格三档分档应存在');
 assert.ok(quadSrc.includes('pics-hint') && quadSrc.includes('applyQuadPic'), '未全显角标与轮换复用分档应存在');
 assert.ok(lbSrc.includes('lb-long') && lbSrc.includes('scrollTop = 0'), '大图长图档与切图回顶应存在');
 assert.ok(css.includes('.lb-stage.lb-long'), '长图按宽滚动样式应存在');
 assert.ok(css.includes('.q-fit-contain'), 'contain 档样式应存在');
});

test('大图键位：筛选键（F/D/↑/↓ 标记）禁用、↑↓ 改为长图滚动', async()=>{
 const fs=await import('node:fs/promises');
 const root=new URL('../app/static/',import.meta.url);
 const app=await fs.readFile(new URL('app.js',root),'utf8');
 assert.ok(app.includes("review.active() && $('#lightbox').hidden"), '回看不应在大图打开时吃键（盲标记泄漏面）');
 assert.ok(app.includes('lbArrowRepeat'), '↑↓ 连发应仅在大图内放行');
 assert.ok(app.includes("stage.clientHeight * 0.8"), '↑↓ 长图滚动应存在');
 assert.ok(!app.includes("closest('button, a[href]')"), '卡片流空格不应被按钮焦点豁免（点过按钮后也要能开大图）');
 assert.ok(app.includes('详情空格：开当前正在看的那张图'), '详情空格应绑定开当前卡大图');
 assert.ok(app.includes('p?.pics?.[Math.max(0, +card?.dataset.img || 0)]'),
   '详情空格应锚定舞台当前图（圆点/箭头切到第几张就开哪张），不是固定首图');
 const lbBlock=app.match(/const lb = \$\('#lightbox'\);[\s\S]*?\n  \}\n  const dt/);
 assert.ok(lbBlock, '大图键位块应存在');
 assert.ok(!/detailMarkAdvance|mark\(/.test(lbBlock[0]), '大图块内不得出现筛选动作');
});

test('入口导入均存在，全部 JS 模块语法可解析',async()=>{
 const fs=await import('node:fs/promises');
 const cp=await import('node:child_process');
 const root=new URL('../app/static/',import.meta.url);
 const source=await fs.readFile(new URL('app.js',root),'utf8');
 for (const [,names,file] of source.matchAll(/import \{([^}]+)\} from '(\.[^']+)'/g)) {
   const module=await import(new URL(file,root));
   for (const name of names.split(',').map(x=>x.trim())) assert.ok(name in module,`${file}: ${name}`);
 }
 for (const file of (await fs.readdir(root)).filter(x=>x.endsWith('.js'))) {
   const {fileURLToPath}=await import('node:url');
   const check=cp.spawnSync(process.execPath,['--check',fileURLToPath(new URL(file,root))],{encoding:'utf8'});
   assert.equal(check.status,0,check.stderr);
 }
});


test('请求超时会结束等待，外部取消可以中止旧筛选请求',async()=>{
 const {api}=await import('../app/static/requests.js');
 const oldFetch=globalThis.fetch;
 globalThis.fetch=async (path,{signal})=>new Promise((resolve,reject)=>{
   signal.addEventListener('abort',()=>reject(signal.reason),{once:true});
   if(signal.aborted) reject(signal.reason);
 });
 try {
   await assert.rejects(api('/api/posts','GET',undefined,{timeoutMs:10}),/请求超时/);
   const cancel=new AbortController();
   const pending=api('/api/posts','GET',undefined,{signal:cancel.signal});
   cancel.abort(new Error('筛选已切换'));
   await assert.rejects(pending,/筛选已切换/);
 } finally {globalThis.fetch=oldFetch;}
});

test('助手拒绝公网上传地址、内网探针地址与带凭据的地址',async()=>{
 const {destination}=await import('../extension/destination.js');
 for (const value of ['https://example.com','http://169.254.169.254','file:///tmp',
  'http://user:pass@localhost:8081','http://localhost:8081/?secret=x','http://localhost.evil.test']) {
   assert.throws(()=>destination(value),undefined,value);
 }
 assert.equal(destination('http://192.168.1.10:8081/'),'http://192.168.1.10:8081');
 assert.equal(destination('http://localhost:8081'),'http://localhost:8081');
 const fs=await import('node:fs/promises');
 const manifest=JSON.parse(await fs.readFile(new URL('../extension/manifest.json',import.meta.url),'utf8'));
 assert.ok(!manifest.host_permissions.includes('http://*/*'));
 assert.ok(!manifest.host_permissions.includes('https://*/*'));
});

test('只读设备按快捷键也不产生乐观状态修改或写请求',async()=>{
 const post={...p,status:'pending'},state={...s,readOnly:true,posts:[post]};
 let writes=0;
 const actions=createPostActions({S:state,api:()=>{writes++;}});
 assert.equal(actions.mark(post.sid,'kept'),null);
 await actions.toggleFavorite(post.sid);
 assert.equal(post.status,'pending');assert.equal(post.favorite,true);assert.equal(writes,0);
});

test('点赞次数 ±1：乐观改账、服务端口径回落、×1 减不动、非保留帖拒绝',async()=>{
 const post={...p,keep_count:2},state={...s,posts:[post],decisions:new Map()};
 const writes=[];const oldCSS=globalThis.CSS;
 globalThis.CSS={escape:(x)=>x};   // Node 无 CSS：syncPost 的选择器转义 mock 掉
 const actions=createPostActions({
   S:state,toast:()=>{},
   api:(path,method)=>{writes.push(method+' '+path);return Promise.resolve({keep_count:post.keep_count});},
   feed:{querySelectorAll:()=>[],querySelector:()=>null},insertCard:()=>{},rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
 });
 try {
   await actions.reaffirm(post.sid,1);
   assert.equal(post.keep_count,3);
   assert.deepEqual(writes,['POST /api/posts/a/reaffirm']);
   await actions.reaffirm(post.sid,-1);
   assert.equal(post.keep_count,2);
   assert.deepEqual(writes,['POST /api/posts/a/reaffirm','DELETE /api/posts/a/reaffirm']);
   await actions.reaffirm(post.sid,-1);
   assert.equal(post.keep_count,1,'×2 再减是合法的，落到建账起点');
   assert.equal(writes.length,3);
   await actions.reaffirm(post.sid,-1);
   assert.equal(post.keep_count,1,'×1 是建账起点，客户端直接拦下');
   assert.equal(writes.length,3);
   const deleted={...p,sid:'b',status:'discarded',keep_count:5};
   state.posts.push(deleted);
   await actions.reaffirm(deleted.sid,-1);
   assert.equal(deleted.keep_count,5,'非保留帖拒绝');
   assert.equal(writes.length,3);
 } finally { if (oldCSS===undefined) delete globalThis.CSS; else globalThis.CSS=oldCSS; }
});

test('收藏翻转不挪卡：普通视图就地翻描边（末张不再退化追加最短列），收藏页取消才摘卡',async()=>{
 const post={...p,favorite:false,status:'kept'},state={...s,posts:[post],decisions:new Map()};
 const oldCSS=globalThis.CSS;globalThis.CSS={escape:(x)=>x};
 // $ 须提供（成功路径探测大图/详情开着没有）：返回 hidden 桩即可
 const $=()=>({hidden:true});
 let inserted=0;let favSet='unset';
 const mk=(sink)=>({querySelectorAll:()=>[{remove(){},classList:{toggle(c,v){sink.set(v);}}}],querySelector:()=>null});
 const writes=[];
 const actions=createPostActions({
   S:state,$,toast:()=>{},api:(path,method)=>{writes.push(method+' '+path);return Promise.resolve({});},
   feed:mk({set:(v)=>{favSet=v;}}),insertCard:()=>{inserted++;},rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
 });
 try {
   await actions.toggleFavorite(post.sid);
   assert.equal(favSet,true,'乐观翻描边');
   assert.equal(inserted,0,'普通视图不得摘卡重插（末张无后继参照会退化追加最短列，卡片跳位）');
   assert.deepEqual(writes,['POST /api/posts/a/favorite']);
   const rolled={v:null,set(v){this.v=v;}};
   const failing=createPostActions({
     S:{...s,posts:[{...post,favorite:false}],decisions:new Map()},$,toast:()=>{},api:()=>Promise.reject(new Error('offline')),
     feed:mk(rolled),insertCard:()=>{inserted++;},rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
   });
   await failing.toggleFavorite(post.sid);
   assert.equal(rolled.v,false,'失败回滚就地翻回，同样不挪卡');
   assert.equal(inserted,0);
   const inFav=createPostActions({
     S:{...s,status:'favorites',posts:[{...post,favorite:true}],decisions:new Map()},$,toast:()=>{},api:()=>Promise.resolve({}),
     feed:mk({set(){}}),insertCard:()=>{inserted++;},rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
   });
   await inFav.toggleFavorite(post.sid);
   assert.equal(inserted,0,'收藏页取消收藏只摘卡，不重插');
 } finally { if(oldCSS===undefined) delete globalThis.CSS; else globalThis.CSS=oldCSS; }
});

test('点赞不挪卡不闪：时间序就地修补（零重建），次数序末张贴前驱插',async()=>{
 const oldCSS=globalThis.CSS;globalThis.CSS={escape:(x)=>x};
 try {
  // 时间序（默认）：kept 帖 +1 → 就地修补数字与按钮态，insertCard 零调用
  const post={...p,status:'kept',keep_count:2},state={...s,posts:[post],decisions:new Map()};
  let inserted=0;const ln={textContent:'2'};
  const likeEl={className:'',title:'',setAttribute(){},querySelector:(sel)=>sel==='.ln'?ln:null};
  const cls=new Set();
  const oldEl={classList:{toggle(c,v){if(v===undefined)v=!cls.has(c);v?cls.add(c):cls.delete(c);},contains:c=>cls.has(c)},
               querySelector:(sel)=>sel==='.like'?likeEl:null,remove(){}};
  const actions=createPostActions({
    S:state,toast:()=>{},api:()=>Promise.resolve({keep_count:3}),
    feed:{querySelector:(sel)=>sel.includes(`"${post.sid}"`)?oldEl:null,querySelectorAll:()=>[]},
    insertCard:()=>{inserted++;},likedTodayOf:()=>false,
    rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
  });
  await actions.reaffirm(post.sid,1);
  assert.equal(post.keep_count,3);
  assert.equal(inserted,0,'位次不变不得重建卡片节点（重建会换掉 <img> 重解码一帧，图面闪一下）');
  assert.equal(String(ln.textContent),'3','点赞数就地更新');
  assert.equal(likeEl.className,'like on','按钮态就地更新（未到当日上限仍可再赞）');
  assert.ok(cls.has('kept'),'kept 描边就地同步');
  // 次数序：末张 +1 需要重排，但无后继 → 贴前驱插（第三参），不落最短列
  const prev={...p,sid:'prev',keep_count:5},main={...p,sid:'main',keep_count:1};
  const cstate={...s,sort:'keep_count',posts:[prev,main],decisions:new Map()};
  let insertCalls=[];
  const prevEl={remove(){},after(){}};
  const cActions=createPostActions({
    S:cstate,toast:()=>{},api:()=>Promise.resolve({keep_count:2}),
    feed:{querySelector:(sel)=>sel.includes('"prev"')?prevEl:null,querySelectorAll:()=>[]},
    insertCard:(pp,ref,pr)=>{insertCalls.push([ref,pr]);},rebuildImages:()=>{},updateMore:()=>{},queueStatusRefresh:()=>{},
  });
  await cActions.reaffirm(main.sid,1);
  assert.equal(insertCalls.length,2,'乐观与回落各一次');
  assert.ok(insertCalls.every(([ref,pr]) => ref===null && pr===prevEl),
    '次数序末张应贴前驱插（ref=null, prev=前驱元素），不落最短列');
 } finally { if(oldCSS===undefined) delete globalThis.CSS; else globalThis.CSS=oldCSS; }
});

test('收藏改走琥珀描边、详情时间行是次数标签：模板与渲染契约',async()=>{
 const fs=await import('node:fs/promises');
 const read=(x)=>fs.readFile(new URL(x,import.meta.url),'utf8');
 const [app,html,css,quad]=await Promise.all([
   read('../app/static/app.js'),read('../app/templates/index.html'),
   read('../app/static/style.css'),read('../app/static/quad.js')]);
 assert.ok(!html.includes('data-v="amber"'),'强调色不应再有琥珀档');
 assert.ok(!css.includes('data-accent="amber"'),'琥珀强调色令牌应已删除');
 assert.ok(!app.includes('★ 收藏') && !quad.includes('★ 收藏'),'卡片不应再渲染收藏 chip');
 assert.ok(app.includes("p.favorite ? ' fav'") && quad.includes("post.favorite ? ' fav'"),'收藏帖卡片应挂 fav 类走琥珀描边');
 assert.ok(html.includes('dt-keepn'),'详情时间行应有次数标签槽位');
 assert.ok(html.includes('dt-via'),'详情应有 @转发 溯源槽位（墙卡不再标类型，转发者详情一跳可达）');
 assert.ok(css.includes('.card.fav') && css.includes('.q-slot .card.fav'),'墙卡与四格卡都应有琥珀描边规则');
});

test('次数徽章：×2 起显示，色阶随次数升温',()=>{
 assert.deepEqual(keepBadge(1), {text:'×1',tier:1,title:'点赞 1 次'});
 assert.equal(keepBadge(2).tier, 1);
 assert.equal(keepBadge(3).tier, 2);
 assert.equal(keepBadge(4).tier, 2);
 assert.equal(keepBadge(5).tier, 3);
 assert.equal(keepBadge(9).tier, 3);
 assert.equal(keepBadge(10).tier, 4);
 assert.equal(keepBadge(2).text, '×2');
});

test('放映偏好：间隔/回看数量/随机/评论字幕，非法值回退默认',()=>{
 assert.deepEqual(ssPrefs({}), {interval:5, limit:30, random:false, comments:true});
 assert.deepEqual(ssPrefs({'ss-interval':'8','ss-order':'rand'}), {interval:8, limit:30, random:true, comments:true});
 assert.deepEqual(ssPrefs({'ss-interval':'3','review':'100'}), {interval:3, limit:100, random:false, comments:true});
 assert.deepEqual(ssPrefs({'ss-interval':'7','review':'77'}), {interval:5, limit:30, random:false, comments:true});
 assert.equal(ssPrefs({'ss-comments':'0'}).comments, false);
});

test('放映片单展开：套图保序成组、组内进度元数据，随机只在帖子级洗牌',()=>{
 const posts=[{sid:'a',pics:[{file:'a1'},{file:'a2'},{file:'a3'}]},
              {sid:'b',pics:[{file:'b1'}]},
              {sid:'c',pics:[{file:'c1'},{file:'c2'}]}];
 const seq=expandPosts(posts);
 assert.deepEqual(seq.map(i=>i.url),
   ['/media/a1','/media/a2','/media/a3','/media/b1','/media/c1','/media/c2']);
 assert.deepEqual(seq.map(i=>i.slideIndex),[1,2,3,1,1,2]);
 assert.deepEqual(seq.map(i=>i.slideTotal),[3,3,3,1,2,2]);
 // 随机档：帖子顺序可变，但每帖内部 slideIndex 仍严格 1→N 连续（套图不散）
 const rand=expandPosts(posts,{random:true});
 let prevSid=null, prevIdx=0;
 for (const it of rand) {
   if (it.p.sid === prevSid) assert.equal(it.slideIndex, prevIdx + 1);
   else { assert.equal(it.slideIndex, 1); prevSid = it.p.sid; }
   prevIdx = it.slideIndex;
 }
 // 无文件的图位不进片单
 assert.deepEqual(expandPosts([{sid:'x',pics:[{},{file:'x1'},{file:'x2'}]}]).map(i=>i.url),
   ['/media/x1','/media/x2']);
});

/* ---------- 四格快筛（quad.js） ---------- */

test('四格取帖：补位栈优先，跳过已占位与非待处理帖',()=>{
 const posts=[{sid:'a',status:'pending'},{sid:'b',status:'pending'},
              {sid:'c',status:'kept'},{sid:'d',status:'pending'}];
 assert.equal(pickNext([{sid:'s',status:'pending'}],posts,new Set()).sid,'s');
 assert.equal(pickNext([],posts,new Set(['a','b'])).sid,'d');
 assert.equal(pickNext([],posts,new Set(['a','b','d'])),null);
 assert.equal(pickNext([],[],new Set()),null);
});

test('四格焦点：阅读序前进、跳过空槽、全空即收尾',()=>{
 const full=[{},{},{},{}];
 assert.equal(nextFocus(full,0),1);
 assert.equal(nextFocus(full,3),0);
 const tail=[{},{},null,null];
 assert.equal(nextFocus(tail,1),0);
 assert.equal(nextFocus(tail,3),0);
 assert.equal(nextFocus([null,null,null,null],2),-1);
});

test('四格会话：键盘直裁即补位，Z 撤销原帖回格、占位帖退回队首',async()=>{
 const els={};
 const makeEl=()=>({hidden:false,innerHTML:'',textContent:'',style:{},dataset:{},
  classList:{_s:new Set(),add(...c){for(const x of c)this._s.add(x);},
             remove(...c){for(const x of c)this._s.delete(x);},
             toggle(c,v){if(v===undefined)v=!this._s.has(c);if(v)this._s.add(c);else this._s.delete(c);return v;},
             contains(c){return this._s.has(c);}},
  appendChild(){},remove(){},querySelector:()=>null,querySelectorAll:()=>[],
  focus(){},setAttribute(){},getAttribute:()=>null});
 const oldDocument=globalThis.document,oldCSS=globalThis.CSS,oldLS=globalThis.localStorage;
 globalThis.document={querySelectorAll:()=>[],createElement:()=>makeEl(),body:makeEl()};
 globalThis.CSS={escape:x=>x};
 delete globalThis.localStorage;
 try {
  const posts='abcdef'.split('').map(x=>({sid:x,status:'pending',pics:[{file:x+'.jpg'}],author:'甲'}));
  const state={...s,status:'pending',readOnly:false,posts,
    images:posts.map(pp=>({p:pp,pc:pp.pics[0],url:'/media/'+pp.pics[0].file})),
    hasMore:false,decisions:new Map(),st:{counts:{pending:6}}};
  const marked=[],lbCalls=[],msgs=[],ops=[];
  let lastOp=null;
  const mark=(sidx,dir)=>{
    const i=state.posts.findIndex(x=>x.sid===sidx);
    if(i<0)return null;
    const post=state.posts[i];
    post.status=dir;state.posts.splice(i,1);marked.push([sidx,dir]);
    lastOp={p:post,expiresAt:Date.now()+6000};ops.push(lastOp);
    return {post,gone:true};
  };
  const undoLastMark=async(op)=>{
    if(lastOp!==op||Date.now()>op.expiresAt)return null;
    lastOp=null;
    const back={...op.p,status:'pending'};
    state.posts.push(back);
    return back;
  };
  const quad=createQuad({S:state,$:(sel)=>els[sel]||(els[sel]=makeEl()),
    toast:m=>msgs.push(m),esc:x=>x,fmtDate:()=>'10-11',fmtHM:()=>'12:00',
    icons:{comment:'',star:''},mark,toggleFavorite:async()=>{},
    getLastDrop:()=>lastOp,undoLastMark,
    load:async()=>{},openLB:(i,sidx)=>lbCalls.push([i,sidx]),openLinkForSid:()=>{},
    switchStatusView:()=>{},animatedHide:()=>{},cancelHide:()=>{}});
  const slot=(i)=>els[`#quad .q-slot[data-slot="${i}"]`];
  await quad.open();
  assert.equal(quad.active(),true);
  assert.equal(state.posts.length,6,'入槽不摘列表：会话队列由待处理视图派生');
  assert.match(slot(0).innerHTML,/a\.jpg/);
  assert.ok(slot(0).classList.contains('focused'),'初始焦点在 0 格');
  // ↓ 直裁 0 格：幽灵位补 e，焦点前进到 1
  await quad.key({key:'ArrowDown',preventDefault(){}});
  assert.deepEqual(marked,[['a','discarded']]);
  assert.match(slot(0).innerHTML,/e\.jpg/);
  assert.ok(slot(1).classList.contains('focused'));
  // F 保留 1 格
  await quad.key({key:'f'});
  assert.deepEqual(marked,[['a','discarded'],['b','kept']]);
  // 空格开当前格（1 格=c）大图
  await quad.key({key:' ',preventDefault(){}});
  assert.deepEqual(lbCalls,[[2,'c']]);
  // Esc 退出会话
  quad.key({key:'Escape'});
  assert.equal(quad.active(),false);
 } finally {globalThis.document=oldDocument;globalThis.CSS=oldCSS;
   if(oldLS===undefined)delete globalThis.localStorage;else globalThis.localStorage=oldLS;}
});

test('四格 Z 撤销：原帖回到原格，被补位的新帖退回队首优先补位',async()=>{
 const els={};
 const makeEl=()=>({hidden:false,innerHTML:'',textContent:'',style:{},dataset:{},
  classList:{_s:new Set(),add(...c){for(const x of c)this._s.add(x);},
             remove(...c){for(const x of c)this._s.delete(x);},
             toggle(c,v){if(v===undefined)v=!this._s.has(c);if(v)this._s.add(c);else this._s.delete(c);return v;},
             contains(c){return this._s.has(c);}},
  appendChild(){},remove(){},querySelector:()=>null,querySelectorAll:()=>[],
  focus(){},setAttribute(){},getAttribute:()=>null});
 const oldDocument=globalThis.document,oldCSS=globalThis.CSS,oldLS=globalThis.localStorage;
 globalThis.document={querySelectorAll:()=>[],createElement:()=>makeEl(),body:makeEl()};
 globalThis.CSS={escape:x=>x};
 delete globalThis.localStorage;
 try {
  const posts='abcdef'.split('').map(x=>({sid:x,status:'pending',pics:[{file:x+'.jpg'}],author:'甲'}));
  const state={...s,status:'pending',readOnly:false,posts,images:[],hasMore:false,
    decisions:new Map(),st:{counts:{pending:6}}};
  let lastOp=null;
  const mark=(sid,dir)=>{
    const i=state.posts.findIndex(x=>x.sid===sid);
    const post=state.posts[i];post.status=dir;state.posts.splice(i,1);
    lastOp={p:post,expiresAt:Date.now()+6000};
    return {post,gone:true};
  };
  const undoLastMark=async(op)=>{
    if(lastOp!==op)return null;
    lastOp=null;
    const back={...op.p,status:'pending'};state.posts.push(back);
    return back;
  };
  const quad=createQuad({S:state,$:(sel)=>els[sel]||(els[sel]=makeEl()),
    toast:()=>{},esc:x=>x,fmtDate:()=>'',fmtHM:()=>'',icons:{comment:'',star:''},
    mark,toggleFavorite:async()=>{},getLastDrop:()=>lastOp,undoLastMark,
    load:async()=>{},openLB:()=>{},openLinkForSid:()=>{},
    switchStatusView:()=>{},animatedHide:()=>{},cancelHide:()=>{}});
  const slot=(i)=>els[`#quad .q-slot[data-slot="${i}"]`];
  await quad.open();                        // 槽 [a,b,c,d]，列表余 [e,f]
  await quad.key({key:'ArrowDown',preventDefault(){}});   // 裁 a → 0 格补 e，焦点到 1
  assert.match(slot(0).innerHTML,/e\.jpg/);
  assert.ok(quad.active(),'裁 1 补 1，会话继续');
  await quad.key({key:'z'});                // Z 撤销：a 回 0 格，e 退回队首，焦点回 0
  assert.match(slot(0).innerHTML,/a\.jpg/);
  assert.ok(slot(0).classList.contains('focused'));
  assert.equal(state.posts.filter(x=>x.sid==='a').length,1,'a 已回到列表');
  await quad.key({key:'ArrowDown',preventDefault(){}});   // 再裁 a：这次 e 从队首补回 0 格
  assert.match(slot(0).innerHTML,/e\.jpg/,'被退回队首的 e 优先补位');
  await quad.key({key:'z'});                // 再撤：a 回格、e 回队首
  assert.match(slot(0).innerHTML,/a\.jpg/);
  quad.key({key:'Escape'});
  assert.equal(quad.active(),false);
 } finally {globalThis.document=oldDocument;globalThis.CSS=oldCSS;
   if(oldLS===undefined)delete globalThis.localStorage;else globalThis.localStorage=oldLS;}
});

test('四格快筛：只读设备不进入，新增为零直接提示',async()=>{
 const els={};
 const makeEl=()=>({hidden:true,innerHTML:'',textContent:'',style:{},dataset:{},
  classList:{_s:new Set(),add(){},remove(){},toggle(){},contains:()=>false},
  appendChild(){},remove(){},querySelector:()=>null,querySelectorAll:()=>[],
  focus(){},setAttribute(){},getAttribute:()=>null});
 const oldDocument=globalThis.document,oldCSS=globalThis.CSS,oldLS=globalThis.localStorage;
 globalThis.document={querySelectorAll:()=>[],createElement:()=>makeEl(),body:makeEl()};
 globalThis.CSS={escape:x=>x};
 delete globalThis.localStorage;
 try {
  const msgs=[];
  const readOnlyState={...s,status:'pending',readOnly:true,posts:[{sid:'a',status:'pending'}],
    images:[],hasMore:false,decisions:new Map(),st:{counts:{pending:1}}};
  let writes=0;
  const ro=createQuad({S:readOnlyState,$:(sel)=>els[sel]||(els[sel]=makeEl()),
    toast:m=>msgs.push(m),esc:x=>x,fmtDate:()=>'',fmtHM:()=>'',icons:{comment:'',star:''},
    mark:()=>{writes++;return null;},toggleFavorite:async()=>{},
    getLastDrop:()=>null,undoLastMark:async()=>null,
    load:async()=>{},openLB:()=>{},openLinkForSid:()=>{},
    switchStatusView:()=>{},animatedHide:()=>{},cancelHide:()=>{}});
  await ro.open();
  assert.equal(ro.active(),false,'只读不进入');
  const emptyState2={...s,status:'pending',readOnly:false,posts:[],images:[],hasMore:false,
    decisions:new Map(),st:{counts:{pending:0}}};
  const q2=createQuad({S:emptyState2,$:(sel)=>els[sel]||(els[sel]=makeEl()),
    toast:m=>msgs.push(m),esc:x=>x,fmtDate:()=>'',fmtHM:()=>'',icons:{comment:'',star:''},
    mark:()=>null,toggleFavorite:async()=>{},getLastDrop:()=>null,undoLastMark:async()=>null,
    load:async()=>{},openLB:()=>{},openLinkForSid:()=>{},
    switchStatusView:()=>{},animatedHide:()=>{},cancelHide:()=>{}});
  await q2.open();
  assert.equal(q2.active(),false);
  assert.match(msgs.at(-1),/新增已筛完/);
  assert.equal(writes,0);
 } finally {globalThis.document=oldDocument;globalThis.CSS=oldCSS;
   if(oldLS===undefined)delete globalThis.localStorage;else globalThis.localStorage=oldLS;}
});

/* ---------- 编年时间线（timeline.js） ---------- */
test('编年 dayOf 与标签：东八区日期截取、中文月日与跨年月份',()=>{
 assert.equal(dayOf({created_at:'2026-10-14T09:30:00+08:00'}),'2026-10-14');
 assert.equal(dayOf({created_at:''}),'');
 assert.equal(dayLabel('2026-10-04'),'10月4日');
 assert.equal(monthLabel('2026-10-14','2026'),'10月');
 assert.equal(monthLabel('2025-12-31','2026'),'2025年12月');
});
test('编年尺几何：月界插标签行、高度开方钳位、保留占比与收藏标记',()=>{
 const days=[
  {d:'2026-10-02',total:100,kept:50,discarded:20,fav:2,cmts:0},
  {d:'2026-10-01',total:9,kept:0,discarded:0,fav:0,cmts:1},
  {d:'2025-09-30',total:0,kept:0,discarded:0,fav:0,cmts:0}];
 const rows=planRows(days);
 assert.deepEqual(rows.map(r=>r.kind),['month','day','day','month','day'],'月界插标签行');
 assert.equal(rows[0].label,'10月');
 assert.equal(rows[1].h,16,'√(total)×2 钳到 16px 上限');
 assert.equal(rows[1].w,100,'峰值日满宽');
 assert.equal(rows[1].keptW,50,'保留占比');
 assert.equal(rows[1].fav,true,'收藏日金点');
 assert.equal(rows[3].label,'2025年9月','跨年月界带年份');
 assert.equal(rows[2].h,2+Math.round(Math.sqrt(9)*2),'9 帖=8px');
 assert.equal(rows[4].w,20,'零帖日保底宽');
 assert.equal(rows[4].fav,false);
});
test('createTimeline 返回编年控制器 API 且不在导入期触碰 DOM',()=>{
 const tl=createTimeline({$:()=>({classList:{contains:()=>false,toggle:()=>{}}}),
  S:{loading:false,hasMore:true},api:async()=>({days:[]}),toast:()=>{},load:async()=>{},
  renderViewPanel:()=>{},closeOverlays:()=>{},canSwitch:()=>true});
 for (const k of ['init','active','appendCard','insertCard','setLayout','gotoDay','refreshOutline'])
  assert.equal(typeof tl[k],'function');
});
test('博主视图进入即归位：作者钉住，状态与伴随筛选全部清零',()=>{
 assert.deepEqual(enterFilters('x:luoluokun_'),
  {author:'x:luoluokun_',status:'all',group:'all',source:'all',
   range:'all',hasCmts:'all',searchText:'',sort:''});
 assert.equal(enterFilters('all').author,'all','无快照退出的默认落点也是同一套归位');
});
test('头部统计文案只报有内容的维度，跨度同年同月只出现一次',()=>{
 assert.equal(statsLine({n:12,kept:8,favorites:2,comments:0,
   first_at:'2024-03-02T10:00:00+08:00',last_at:'2026-10-01T09:00:00+08:00'}),
  '12 帖 · 保留 8 · 收藏 2 · 2024-03 – 2026-10');
 assert.equal(statsLine({n:3,kept:0,favorites:0,comments:1,
   first_at:'2026-10-01T00:00:00+08:00',last_at:'2026-10-02T00:00:00+08:00'}),
  '3 帖 · 注 1 · 2026-10');
 assert.equal(statsLine({n:1,kept:0,favorites:0,comments:0,first_at:'',last_at:''}),'1 帖');
});
test('原主页链接：X 只认 handle，微博用 uid，缺据不给链接',()=>{
 assert.equal(profileUrl({source:'x',handle:'luoluokun_',uid:'x'}),'https://x.com/luoluokun_');
 assert.equal(profileUrl({source:'x',handle:'',uid:'99'}),'', '数字 rest_id 拼 x.com 会被当 handle 打不开，宁缺毋滥');
 assert.equal(profileUrl({source:'weibo',uid:'1234567'}),'https://weibo.com/u/1234567');
 assert.equal(profileUrl({source:'weibo',uid:''}),'');
 assert.equal(profileUrl(null),'');
 assert.equal(profileSpan('',''),'', '缺一端不给跨度');
});
test('博主皮肤偏好：author-skin 多值优先，老档 author-film 折算，未知值回卡片',()=>{
 assert.deepEqual(SKINS,['film','gallery','blueprint'],'皮肤清单即档位枚举');
 assert.equal(skinFromPrefs({}),'','无偏好 = 卡片默认');
 assert.equal(skinFromPrefs(undefined),'','prefs 拿不到也回默认');
 for (const k of SKINS) assert.equal(skinFromPrefs({'author-skin':k}),k);
 assert.equal(skinFromPrefs({'author-silm':'typo'}),'','拼错键无效');
 assert.equal(skinFromPrefs({'author-skin':'neon'}),'','未知档位回卡片');
 assert.equal(skinFromPrefs({'author-film':'1'}),'film','老档布尔折算胶片');
 assert.equal(skinFromPrefs({'author-film':''}),'','老档关闭即卡片');
 assert.equal(skinFromPrefs({'author-film':'1','author-skin':'blueprint'}),'blueprint','新键优先于老档');
});

test('优化闭环：快捷键速查、批量下载智能分卷、四格轮播进度与触屏快筛契约', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');
  const [html, css, app, batch, quad] = await Promise.all([
    read('../app/templates/index.html'),
    read('../app/static/style.css'),
    read('../app/static/app.js'),
    read('../app/static/batch-download.js'),
    read('../app/static/quad.js')
  ]);
  // 1. 快捷键速查：收进统一设置窗口的分区；? 键呼出且再次敲击可关闭；data-close 按钮可关闭
  assert.ok(html.includes('id="modal-settings"') && html.includes('data-pane="shortcuts"'), '模板应包含统一设置窗口与快捷键分区');
  assert.ok(css.includes('.settings-shell'), '统一设置窗口应有独立外壳样式');
  assert.ok(app.includes("modal.id === 'modal-settings' && (e.key === '?'"), '顶部模态拦截应放行设置窗口的 ? 键以支持二次关闭');
  assert.ok(app.includes("document.querySelectorAll('.mclose, [data-close]')"), '应统一绑定 [data-close] 保证模态内动作按钮有效');

  // 1.5 手机端设置推入式导航：根菜单 → 滑入面板 → ‹ 返回；Esc 逐层退
  const settingsJs = await read('../app/static/settings.js');
  assert.ok(html.includes('id="settings-back"') && html.includes('id="settings-title"'), '设置窗口头应有返回键与可变标题');
  assert.ok(css.includes('.settings-shell.in-pane .spanes') && css.includes('translateX(100%)'), '手机端面板应从右滑入（in-pane 态位移归零）');
  assert.ok(css.includes('.snav-item::after') && css.includes('.snav-title { display: block'), '手机根菜单应保留分组标题并给整行按钮加行尾箭头');
  assert.ok(settingsJs.includes('function goRoot') && settingsJs.includes("matchMedia('(max-width: 640px)')"), 'settings.js 应具备返回根菜单能力并以 640px 判定手机态');
  assert.ok(settingsJs.includes('mobileBack'), 'settings.js 应导出 mobileBack 供 Esc 逐层返回');
  assert.ok(app.includes("settings.mobileBack?.()"), 'Esc 拦截应在关窗前先尝试手机端返回根菜单');

  // 2. 批量下载智能分卷：超限规划、已选隔离与单项超限防护
  assert.ok(html.includes('id="b-chunks"'), '批量下载模板应有分卷容器');
  assert.ok(batch.includes('renderChunks') && batch.includes('b-chunk-btn'), '批量下载脚本应具备智能分卷规划与下载');
  assert.ok(batch.includes('B.checked.has') && batch.includes('overSingle'), '分卷应隔离已选范围并对单项超限实施安全阻断');

  // 3. 四格轮播进度与时钟同步
  assert.ok(quad.includes('q-progress-track') && quad.includes('q-progress-bar'), '四格卡片应包含微进度条');
  assert.ok(quad.includes('Q.lastRotate[i] = Date.now()'), '新帖入槽 lastRotate 应为当前时间杜绝抢跑闪退');
  assert.ok(quad.includes('Q.lastRotate[Q.focus] = Date.now()'), '移焦时应重置聚焦槽时间戳以对齐 3s 倒计时');
  assert.ok(css.includes('@keyframes qProgress'), '样式应声明四格进度条 3s 动画');

  // 4. 详情触屏滑动与触感
  assert.ok(app.includes('navigator.vibrate'), '滑动手势完成应支持触感振动反馈');
  assert.ok(app.includes('card.setPointerCapture') && app.includes('card.releasePointerCapture'), '触屏滑动应支持 pointer capture 防脱手');
  assert.ok(!app.includes("if (e.pointerType !== 'mouse') return;"), '全屏详情滑动不应将触屏设备直接拒之门外');
  assert.ok(css.includes('.topbar .viewbtn') && css.includes('var(--bg-track)'), '顶栏视图按钮应有背景与轮廓以强化可点击感知');
});

test('微博博主目标与管理分组契约：WB·前缀、博主入口与分组过滤', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');
  const [settings, app, html] = await Promise.all([
    read('../app/static/settings.js'),
    read('../app/static/app.js'),
    read('../app/templates/index.html')
  ]);

  // 1. 模板包含微博博主添加表单
  assert.ok(html.includes('id="wbuser-input"') && html.includes('id="btn-add-wbuser"'), '管理分组弹窗应提供微博博主添加输入框与按钮');

  // 2. settings.js 支持添加微博博主并给 wbu: 加上 WB· 标签
  assert.ok(settings.includes('#btn-add-wbuser'), 'settings.js 应绑定添加微博博主事件');
  assert.ok(settings.includes("g.list_id.startsWith('wbu:') ? 'WB·' : ''"), '管理分组应给 wbu: 目标渲染 WB· 前缀');

  // 3. 分组筛选并入右上筛选面板（博主目标不进分组芯片，看博主走左囊「所有博主」/kiosk 或博主视图）
  assert.ok(html.includes('id="f-group"') && app.includes("!/^(xu:|wbu:)/.test(g.list_id)"), '分组筛选应并入 #view-panel 且排除单人博主目标');
});

test('筛选中枢：博主条退役、博主筛选并入视图面板、Shift+F 呼出', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');
  const [html, app, css, settings] = await Promise.all([
    read('../app/templates/index.html'),
    read('../app/static/app.js'),
    read('../app/static/style.css'),
    read('../app/static/settings.js')
  ]);

  // 1. 博主筛选条与六态排序菜单整体退役
  assert.ok(!html.includes('author-strip') && !html.includes('sort-menu'), '博主条/排序菜单 markup 应已删除');
  assert.ok(!css.includes('.author-wrap') && !css.includes('.achip'), '博主条样式应已删除');
  assert.ok(!app.includes('SORT_METRICS') && !app.includes('pw_asort'), '六态博主排序应已退役');

  // 2. 「博主展示 → 仅头像」偏好随条退役
  assert.ok(!html.includes('pref-author') && !settings.includes('pref-author'), '博主展示偏好应已删除');
  assert.ok(!css.includes('data-author='), '仅头像 CSS 档位应已删除');

  // 3. 视图面板精简：博主与状态行退役收敛至侧栏，时间与评论筛选进驻面板
  assert.ok(!html.includes('id="f-author"') && !html.includes('id="f-status"'), '博主与状态行已从面板退役收敛至侧栏');
  assert.ok(html.includes('id="f-range"') && html.includes('id="f-has-cmts"'), '时间与评论筛选应进驻面板');
  assert.ok(app.includes('renderAuthorRow') && app.includes('fa-picker'), '博主行应有行内选择器');
  assert.ok(app.includes('sort=name_asc'), '博主列表默认按拼音顺序（联系人式）');
  assert.ok(app.includes('FA_ALPHABET') && app.includes('fa-index'), '博主选择器应有字母分组与索引轨');
  assert.ok(app.includes('function faJump') && app.includes('data-letter'), '索引轨应支持点字母跳组');
  assert.ok(app.includes('a.initial'), '博主行应消费服务端 initial 首字母');
  assert.ok(css.includes('.fa-letter') && css.includes('.fa-idx'), '字母头与索引轨应有样式');
  assert.ok(app.includes("if (S.author !== 'all') n += 1;"), '漏斗徽章应计入博主筛选');

  // 4. Shift+F 呼出面板；快捷键速查同步
  assert.ok(app.includes("e.key === 'F' || e.key === 'f'") && app.includes('toggleViewPanel'), 'Shift+F 应开关筛选面板');
  assert.ok(html.includes('<kbd>Shift</kbd>+<kbd>F</kbd>'), '快捷键速查应含 Shift+F');

  // 5. 旧深链参数仍被解析（无 UI 入口，重置筛选可清）
  assert.ok(app.includes("['today', 'yesterday', '7d', '30d'].includes(p.get('range'))"), '时间深链仍应生效');

  // 6. 图标几何统一：箭头与 ✕ 共用 18px 槽位，字重不随开合跳变
  assert.ok(app.includes('fa-caret') && css.includes('.fa-caret'), '展开箭头应套 18px 图标槽');
  assert.ok(css.includes('.fa-caret, .fauthor .fax'), '箭头与 ✕ 应共用同一槽位规则');
  assert.ok(css.includes('.fchip.fauthor.on { font-weight: 400; }'), '选中/展开态不应加粗（宽度不跳变）');

  // 7. 分组管理成员开合：绑定与渲染属性必须同名（曾因 bmtoggle/gmtoggle 拼写错失效）
  assert.ok(settings.includes("querySelectorAll('[data-gmtoggle]')") && settings.includes('data-gmtoggle='),
    '成员开合按钮应绑定 data-gmtoggle');
  assert.ok(!settings.includes('bmtoggle'), '不应残留 bmtoggle 拼写');
});

test('桌面右轨与侧栏修复：Esc冒泡阻断、抽屉焦点/点按收起、编年排序保护、放映直连、纯图多选入口与几何对齐', async () => {
  const fs = await import('node:fs/promises');
  const root = new URL('../app/static/', import.meta.url);
  const html = await fs.readFile(new URL('../templates/index.html', root), 'utf8');
  const railSrc = await fs.readFile(new URL('rail.js', root), 'utf8');
  const appSrc = await fs.readFile(new URL('app.js', root), 'utf8');
  const css = await fs.readFile(new URL('style.css', root), 'utf8');

  // 1. Esc 冒泡阻断：关闭视图面板时必须 stopPropagation 与 stopImmediatePropagation，避免同级 document 监听器误关左侧栏
  assert.ok(appSrc.includes("closeViewPanel(); e.stopPropagation();"), '关闭视图面板时应阻断 Esc 冒泡');
  assert.ok(appSrc.includes('e.stopImmediatePropagation()'), '关闭视图面板与浮层时应调用 stopImmediatePropagation 阻断同级 document 监听器');

  // 验证真实的事件派发：当 stopImmediatePropagation 被调用时，document 上的后续 keydown 监听器不应被触发
  {
    const target = new EventTarget();
    let secondListenerFired = false;
    target.addEventListener('keydown', (e) => {
      // 模拟 app.js 行为
      e.stopPropagation();
      e.stopImmediatePropagation();
    });
    target.addEventListener('keydown', (e) => {
      // 模拟 rail.js 行为
      secondListenerFired = true;
    });
    target.dispatchEvent(new Event('keydown'));
    assert.equal(secondListenerFired, false, 'stopImmediatePropagation 应彻底阻断同级监听器执行');
  }

  // 2. 右上工具条与纯图模式联动、时间线右缘退让
  assert.ok(html.includes('id="top-tools"') && html.includes('id="corner-tools"'),
    '桌面应存在右上 #top-tools 与右下 #corner-tools 悬浮胶囊');
  assert.ok(appSrc.includes("inp.addEventListener('search'") && appSrc.includes("inp.addEventListener('input'"),
    '搜索输入框应监听 search 与 input 事件以支持 WebKit 原生清除与实时前缀');

  // 3. 纯图模式下贴边布局与悬浮胶囊半隐恢复
  assert.ok(css.includes('html[data-pureimg="1"] body { padding-left: 0; }'),
    '纯图模式下桌面应释放左留白贴边铺满');
  assert.ok(css.includes('html[data-pureimg="1"] #rail,')
    && css.includes('html[data-pureimg="1"] #corner-tools { opacity: .22; }')
    && css.includes('html[data-pureimg="1"] #top-tools.panel-open,')
    && css.includes('html[data-pureimg="1"] #corner-tools:focus-within { opacity: 1; }'),
    '纯图模式下悬浮胶囊应默认半隐并在悬停/聚焦/开窗时回显');
  assert.ok(css.includes('#truler { right: 0; top: max(64px, var(--tl-top, 64px)); bottom: 64px;')
    && css.includes('body.has-selbar #corner-tools { bottom: 68px; }'),
    '编年密度尺应避让右上胶囊，多选底栏应抬起右下回顶胶囊');

  // 4. 三套皮肤适配胶囊（Swiss 零玻璃 / Brut 硬阴影撞色 / Photo 扁平）
  assert.ok(css.includes('html[data-skin="swiss"] #rail,')
    && css.includes('html[data-skin="brut"] #rail,')
    && css.includes('box-shadow: 4px 4px 0 var(--skin-edge);')
    && css.includes('html[data-skin="photo"] #rail,'),
    'Swiss / Brut / Photo 三套皮肤应各自覆盖悬浮胶囊样式');

  // 5. 更多菜单直连动作与快捷入口
  assert.ok(html.includes('id="tt-play"') && html.includes('幻灯片放映')
    && html.includes('id="tt-random"') && html.includes('id="tt-notes"')
    && html.includes('id="tt-top"'),
    '右上更多菜单应包含放映/随机/批注，右下胶囊提供回顶');
  assert.ok(railSrc.includes("document.getElementById('tt-play')?.addEventListener('click'")
    && railSrc.includes('R.slideshowOpen()'),
    '点击放映应直达 R.slideshowOpen()');

  // 6. 桌面隐藏 FAB，右上工具条集成纯图与多选
  assert.ok(css.includes('@media (min-width: 900px)') && css.includes('.fab { display: none; }'),
    '桌面端 (>=900px) 应隐藏悬浮球 FAB');
  assert.ok(html.includes('id="tt-pureimg"') && html.includes('id="tt-select"'),
    '右上悬浮胶囊应提供纯图与多选入口');
  assert.ok(railSrc.includes('pureimgBtn.addEventListener') && railSrc.includes('selectBtn.addEventListener'),
    '纯图与多选按钮应绑定点击切换');
  assert.ok(appSrc.includes('setSelectMode: (on) => setSelectMode(on)'),
    'pwRail 应暴露 setSelectMode 钩子');
  assert.ok(appSrc.includes('updateSelN();\n  railSync();'),
    'setSelectMode 应即时触发 railSync 同步多选状态');

  // 7. 左侧悬浮胶囊与布局修复（高亮条、分组过滤、换视图清筛选、title、authorView 贴顶、列数、边框对比度）
  const authorSrc = await fs.readFile(new URL('author-view.js', root), 'utf8');

  // 高亮指示条锚定在 left: 0
  assert.ok(css.includes('#rail .rrow.on::before {'), '高亮指示条应置于 left: 0 渲染安全区');

  // 面板分组过滤保留纯数字微博分组，排除博主单人目标与 demo
  assert.ok(appSrc.includes("!/^(xu:|wbu:)/.test(g.list_id) && g.list_id !== 'demo'"),
    '面板分组过滤应排除博主目标与 demo，保留纯数字微博分组');

  // 换全部视图时若带筛选（含博主）不应早退死锁
  assert.ok(appSrc.includes('const needsReload = S.status !== status || hadFilters'),
    'switchStatusView 应将 hadFilters 纳入 needsReload 条件');

  // 左囊所有条目均带原生 title 属性（收起态悬停可读）
  assert.ok(html.includes('id="rail-brand"') && html.includes('title="PWALL · 回到顶部"')
    && html.includes('id="rail-authors-entry"') && html.includes('title="所有博主"')
    && html.includes('id="rail-triage"') && html.includes('title="待筛"')
    && html.includes('id="rail-chrono"') && html.includes('title="编年视图"')
    && html.includes('id="rail-stats-link"') && html.includes('title="回看统计"')
    && html.includes('id="rail-settings"') && html.includes('title="设置"'),
    '左悬浮胶囊所有条目均应具备原生 title 属性');

  // 桌面端吸顶博主栏贴顶 8px 修复 + timeline 顶栏探测
  assert.ok(authorSrc.includes("const hasTopbar = tb && getComputedStyle(tb).display !== 'none';")
    && authorSrc.includes("const base = hasTopbar ? tb.offsetHeight + 6 : 8;"),
    'authorView syncSticky 应正确识别桌面顶栏隐藏并贴顶 8px');

  // 左囊胶囊收起(56px)/展开(216px) 平滑覆盖与文字隐藏
  assert.ok(css.includes('width: 56px;') && css.includes('#rail.open {') && css.includes('width: 216px;')
    && css.includes('#rail:not(.open) .lb { opacity: 0; max-width: 0; pointer-events: none;'),
    '左悬浮胶囊应从 56px 覆盖展开至 216px，收起态文字滑隐不占位');

  // 桌面（含纯图态）与平板 (640px~900px) 列数 occupied 计算及零递归自愈
  assert.ok(appSrc.includes('const occupied = window.innerWidth >= 900 ? (isPure ? 32 : 116) : 32;')
    && appSrc.includes('relayoutFeed: () => { colWidth = 0; ensureFeedCols(); }')
    && !railSrc.includes("window.dispatchEvent(new Event('resize'))"),
    'feedColCount 在桌面纯图态扣除 32px、常规态扣除 116px，列栈自愈应直接调用 relayoutFeed 而非同步派发 resize 事件');

  // 胶囊描边对比度使用 border-subtle
  assert.ok(css.includes('#rail {') && css.includes('#top-tools {') && css.includes('border: 1px solid var(--border-subtle);'),
    '悬浮胶囊描边应使用 border-subtle 强化对比度');
});

test('多维搜索契约与筛选面板精简：搜索维度匹配、快捷前缀、胶囊同步与面板去重', async () => {
  const fs = await import('node:fs/promises');
  const root = new URL('../app/static/', import.meta.url);
  const html = await fs.readFile(new URL('../app/templates/index.html', import.meta.url), 'utf8');
  const railSrc = await fs.readFile(new URL('rail.js', root), 'utf8');
  const appSrc = await fs.readFile(new URL('app.js', root), 'utf8');
  const css = await fs.readFile(new URL('style.css', root), 'utf8');

  // 1. matchesView 多维搜索口径契约
  const basePost = { sid: 's1', status: 'kept', favorite: 0, text: '阳光下的海边', author: '摄影师阿张', tags: ['风景', '胶片'], comment_match: '' };
  const baseState = { status: 'kept', group: 'all', author: 'all', source: 'all', range: 'all', hasCmts: 'all', tags: 'all', searchText: '', searchScope: 'all' };

  // scope: 'all' 维度：任一命中（文本、作者、标签、批注）
  assert.equal(matchesView(basePost, { ...baseState, searchText: '海边', searchScope: 'all' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '阿张', searchScope: 'all' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '胶片', searchScope: 'all' }), true);
  assert.equal(matchesView({ ...basePost, comment_match: '机位绝佳' }, { ...baseState, searchText: '机位', searchScope: 'all' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '不存在', searchScope: 'all' }), false);

  // scope: 'text' 维度：仅正文
  assert.equal(matchesView(basePost, { ...baseState, searchText: '海边', searchScope: 'text' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '阿张', searchScope: 'text' }), false);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '胶片', searchScope: 'text' }), false);

  // scope: 'author' 维度：仅博主
  assert.equal(matchesView(basePost, { ...baseState, searchText: '阿张', searchScope: 'author' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '海边', searchScope: 'author' }), false);

  // scope: 'tag' 维度：仅标签
  assert.equal(matchesView(basePost, { ...baseState, searchText: '胶片', searchScope: 'tag' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '海边', searchScope: 'tag' }), false);

  // scope: 'comment' 维度：仅评论/批注
  assert.equal(matchesView({ ...basePost, comment_match: '机位绝佳' }, { ...baseState, searchText: '机位', searchScope: 'comment' }), true);
  assert.equal(matchesView(basePost, { ...baseState, searchText: '海边', searchScope: 'comment' }), false);

  // 2. 居中聚光灯大搜索栏 UI 与胶囊 markup
  assert.ok(html.includes('id="modal-spotlight-search"') && html.includes('id="spotlight-scopes"') && html.includes('class="search-scopes"'), '聚光灯搜索应包含模态弹窗与维度胶囊容器');
  assert.ok(html.includes('data-scope="all"') && html.includes('data-scope="text"')
    && html.includes('data-scope="comment"') && html.includes('data-scope="tag"')
    && html.includes('data-scope="author"'), '搜索维度应包含全部/内容/评论/标签/博主');

  // 3. 前缀快捷键识别 (@xxx, #xxx, //xxx, c:xxx)
  assert.ok(appSrc.includes("val.startsWith('@')") && appSrc.includes("newScope = 'author'"), '前缀 @ 应自动切博主维度');
  assert.ok(appSrc.includes("val.startsWith('#')") && appSrc.includes("newScope = 'tag'"), '前缀 # 应自动切标签维度');
  assert.ok(appSrc.includes("val.startsWith('//')") && appSrc.includes("newScope = 'comment'"), '前缀 // 应自动切评论维度');
  assert.ok(appSrc.includes('/^c:/i.test(val)') && appSrc.includes("newScope = 'comment'"), '前缀 c: 应自动切评论维度');

  // 4. 聚光灯搜索交互与状态同步
  assert.ok(appSrc.includes("scopesEl.addEventListener('click'") && appSrc.includes("setSpotlightScope"), '点击维度胶囊应即时切换维度');
  assert.ok(appSrc.includes("scopesEl.querySelectorAll('.s-scope').forEach"), 'sync 应同步胶囊高亮态');
  assert.ok(appSrc.includes("p.set('scope', S.searchScope)"), 'syncUrl 应持久化 scope');
  assert.ok(appSrc.includes("q.set('q_scope', S.searchScope)"), 'load 应发送 q_scope');

  // 5. 视图面板精简去重与右轨贴靠 popover 对齐
  assert.ok(!html.includes('id="f-status"'), '#view-panel 应已移除状态行');
  assert.ok(!html.includes('id="f-author"'), '#view-panel 应已移除博主行');
  assert.ok(!html.includes('id="f-sort-ctl"'), '#view-panel 应已移除排序控制');
  assert.ok(!html.includes('id="f-layout"'), '#view-panel 应已移除布局控制');
  assert.ok(html.includes('id="f-source"') && html.includes('id="f-tags"')
    && html.includes('id="f-range"') && html.includes('id="f-has-cmts"')
    && html.includes('id="f-reset"'), '#view-panel 应保留辅助属性筛选');
  assert.ok(css.includes('.view-panel') && css.includes('320px'), '桌面筛选面板应为 320px 贴靠浮窗');
  assert.ok(css.includes('top: 64px;') && css.includes('right: 16px;'), '桌面筛选面板与通知栈应紧贴右上工具条下沿');

  // 6. 样式规格契约
  assert.ok(css.includes('.search-scopes') && css.includes('.s-scope'), '应定义维度胶囊样式');
  assert.ok(css.includes('.spotlight-modal') && css.includes('backdrop-filter: blur(8px)'), '聚光灯弹窗应定义毛玻璃背景');
  assert.ok(css.includes('.f-foot') && css.includes('.f-reset'), '筛选重置应有独立底部行样式');
  assert.ok(css.includes('.f-foot:has(#f-reset[hidden])'), '筛选重置无按钮时应收起空底栏');

  // 7. matchesView 边界情况：纯空格搜索词不产生错误过滤
  assert.equal(matchesView(basePost, { ...baseState, searchText: '   ', searchScope: 'text' }), true);

  // 8. 维度胶囊与 applySearch 联动：同词换维度必须触发重新加载（不能被误判为未变更而早退）
  assert.ok(appSrc.includes('appliedSearchScope') && appSrc.includes('appliedSearchText'), 'app.js 应维护已应用的搜索词与维度，防止换维度早退');
  assert.ok(appSrc.includes("applySearch(v, currentSpotlightScope)"), '聚光灯搜索应保证当前维度正确传入 applySearch');
  assert.ok(appSrc.includes('pw_recent_searches'), '最近搜索应持久化至 localStorage');
  assert.ok(html.includes('spotlight-syntax-hints') && html.includes('@昵称') && html.includes('//内容'), '聚光灯应包含快捷语法提示 (@昵称 搜博主, #标签 搜标签, //内容 搜评论)');
  assert.ok(html.includes('spotlight-footer') && html.includes('检索') && html.includes('ESC'), '聚光灯应包含底部按键提示');

  // 9. 健壮性与可访问性契约：全角斜杠支持、退格切回全部、最近搜索键盘可达与移动端回焦防死循环
  assert.ok(appSrc.includes("val.startsWith('／／')"), '应支持全角斜杠前缀识别');
  assert.ok(appSrc.includes("e.key === 'Backspace' && !inp.value && currentSpotlightScope !== 'all'"), '空输入框按 Backspace 应切回全部维度');
  assert.ok(appSrc.includes('modalReturnFocus.id !== \'f-search\''), '关窗时不应死循环回焦移动端搜索输入框');
  assert.ok(appSrc.includes('setSpotlightScope(\'all\')'), 'resetFilters 应同步重置聚光灯搜索维度');
  assert.ok(appSrc.includes('role="button"') && appSrc.includes('tabindex="0"'), '最近搜索气泡应具备键盘访问语义');
});

test('实时检索与分类UI区分契约：聚光灯实时联想、四维视觉区分、键盘导航与页面活跃检索胶囊', async () => {
  const fs = await import('node:fs/promises');
  const root = new URL('../app/static/', import.meta.url);
  const html = await fs.readFile(new URL('../app/templates/index.html', import.meta.url), 'utf8');
  const css = await fs.readFile(new URL('style.css', root), 'utf8');
  const appSrc = await fs.readFile(new URL('app.js', root), 'utf8');

  // 1. 结构契约：聚光灯实时结果容器与图片墙活跃检索胶囊
  assert.ok(html.includes('id="spotlight-live-results"'), '聚光灯模态中应包含实时检索结果容器 #spotlight-live-results');
  assert.ok(html.includes('id="spotlight-default-body"'), '聚光灯模态中应包含默认空白态容器 #spotlight-default-body');
  assert.ok(html.includes('id="feed-search-chip"'), '图片墙顶部应包含活跃检索胶囊容器 #feed-search-chip');
  assert.ok(html.includes('id="f-search-clear"'), '顶栏搜索框应包含一键清除按钮 #f-search-clear');

  // 2. 实时检索、防抖与竞态保护契约
  assert.ok(appSrc.includes('/api/search/live?q='), 'app.js 应请求 /api/search/live 实时检索接口');
  assert.ok(appSrc.includes('triggerLiveSearch'), 'app.js 应具备实时搜索触发器');
  assert.ok(appSrc.includes('150'), '实时搜索应具备 ~150ms 的防抖机制');
  assert.ok(appSrc.includes('AbortController'), '实时搜索防抖应使用 AbortController 取消前序未完成请求');
  assert.ok(appSrc.includes('liveSearchSeq'), '实时搜索应使用序列号严格防止网络回包乱序覆盖');

  // 3. 四维类别UI区分契约 (Author/Tag/Content/Comment)
  assert.ok(css.includes('.cat-author') && css.includes('.cat-tag') && css.includes('.cat-content') && css.includes('.cat-comment'), 'CSS 应为博主/标签/内容/评论定义四维类别色阶样式');
  assert.ok(appSrc.includes('data-type="author"') && appSrc.includes('cat-author'), '博主分类应具备专属徽章与类型标识');
  assert.ok(appSrc.includes('data-type="tag"') && appSrc.includes('cat-tag'), '标签分类应具备专属徽章与类型标识');
  assert.ok(appSrc.includes('data-type="content"') && appSrc.includes('cat-content'), '内容分类应具备专属徽章与类型标识');
  assert.ok(appSrc.includes('data-type="comment"') && appSrc.includes('cat-comment'), '评论分类应具备专属徽章与类型标识');
  assert.ok(appSrc.includes('openDetail(sid, true)'), '评论结果点击应直达详情卡片并定位评论区');

  // 4. 关键词高亮、缩略图路径与日期展示
  assert.ok(appSrc.includes('highlightSnippet') && appSrc.includes('<mark>'), '实时结果应使用 <mark> 高亮匹配关键词');
  assert.ok(css.includes('.spotlight-res-snippet mark'), 'CSS 应定义高亮标签样式');
  assert.ok(appSrc.includes('spotlight-res-avatar') && appSrc.includes('spotlight-res-thumb'), '实时结果应支持头像与正文缩略图渲染');
  assert.ok(appSrc.includes('mediaSrc(c.thumb)'), '正文缩略图应通过 mediaSrc 映射正确媒体相对路径');
  assert.ok(appSrc.includes('spotlight-res-date') && appSrc.includes('fmtDate(c.created_at)'), '正文与评论实时结果应显示发布日期');

  // 5. 键盘导航契约 (↑/↓ 键位、中文输入法保护与 Enter 确认选择)
  assert.ok(appSrc.includes("e.key === 'ArrowDown'") && appSrc.includes("e.key === 'ArrowUp'"), '聚光灯搜索应支持上下方向键导航');
  assert.ok(appSrc.includes('updateSpotlightNav'), '应维护当前键盘高亮项');
  assert.ok(css.includes('.spotlight-result-item.selected'), 'CSS 应定义键盘选中态样式');
  assert.ok(appSrc.includes('selectSpotlightItem'), '按回车或点击应选择当前高亮结果');
  assert.ok(appSrc.includes('isComposing') && appSrc.includes('compositionend'), '输入框应具备中文输入法（IME）合成事件保护');

  // 6. 底部全部结果动作
  assert.ok(appSrc.includes('在图片墙查看全部结果'), '实时检索底部应提供在图片墙查看全部结果的统一动作');
  assert.ok(css.includes('.spotlight-action-all'), '全部结果动作应有明显分隔与样式');

  // 7. 图片墙活跃检索胶囊 (Feed Active Search Chip) 契约
  assert.ok(appSrc.includes('renderActiveSearchChip'), 'app.js 应维护 renderActiveSearchChip 渲染函数');
  assert.ok(appSrc.includes('active-search-chip'), '活跃检索胶囊应使用 .active-search-chip 结构');
  assert.ok(appSrc.includes('active-search-clear'), '活跃检索胶囊应支持一键清除');
  assert.ok(css.includes('.feed-search-chip') && css.includes('.active-search-chip'), 'CSS 应定义图片墙顶部活跃检索胶囊样式');
  assert.ok(css.includes('.active-search-badge.cat-author') && css.includes('.active-search-badge.cat-tag'), '活跃检索徽章应具备各分类专属背景与文本颜色');
});

test('Wave1 安全加固：settings 组 ID 转义、kiosk/view-state 转义与协议约束、markTerm 防实体破坏', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');

  // 1. settings.js：g.list_id 全量 esc()，删除接口使用 encodeURIComponent
  const settingsSrc = await read('../app/static/settings.js');
  assert.ok(settingsSrc.includes('data-id="${esc(g.list_id)}"'));
  assert.ok(settingsSrc.includes('data-gmtoggle="${esc(g.list_id)}"'));
  assert.ok(settingsSrc.includes('data-bf="${esc(g.list_id)}"'));
  assert.ok(settingsSrc.includes('data-rm="${esc(g.list_id)}"'));
  assert.ok(settingsSrc.includes("api('/api/groups/' + encodeURIComponent(b.dataset.rm), 'DELETE')"));

  // 2. kiosk.js：单引号转义、属性中换行不变成 <br>、仅 nl2br 换 <br>、safeMedia 协议白名单
  assert.equal(kioskEsc(`a'b"c\n<d>&`), 'a&#39;b&quot;c\n&lt;d&gt;&amp;');
  assert.equal(kioskNl2br('a<b\nc&d'), 'a&lt;b<br>c&amp;d');
  assert.equal(kioskSafeMedia('avatars/u.jpg'), '/media/avatars/u.jpg');
  assert.equal(kioskSafeMedia('/media/x/y.jpg'), '/media/x/y.jpg');
  assert.equal(kioskSafeMedia('https://cdn.example.com/a.jpg'), 'https://cdn.example.com/a.jpg');
  assert.equal(kioskSafeMedia('javascript:alert(1)'), '');
  assert.equal(kioskSafeMedia('data:text/html,<script>1</script>'), '');
  assert.equal(kioskSafeMedia('//evil.example/a.jpg'), '');

  // 3. view-state.js：mediaSrc/avatarSrc/safeUrl 协议收紧，markTerm 不损坏 HTML 实体且按 scope 去前缀
  assert.equal(avatarSrc('javascript:alert(1)'), '');
  assert.equal(avatarSrc('data:image/svg+xml,<svg/>'), '');
  assert.equal(avatarSrc('httpanything:foo'), '');
  assert.equal(mediaSrc('/media/a.jpg'), '/media/a.jpg');
  assert.equal(mediaSrc('https://pbs.twimg.com/a.jpg'), 'https://pbs.twimg.com/a.jpg');
  assert.equal(mediaSrc('a/b.jpg'), '/media/a/b.jpg');
  assert.equal(mediaSrc('data:text/html,xss'), '');
  assert.equal(mediaSrc('javascript:alert(1)'), '');
  assert.equal(mediaSrc('httpfoo:bar'), '');
  assert.equal(safeUrl('javascript:alert(1)'), '#');
  assert.equal(safeUrl('https://weibo.com/1/2'), 'https://weibo.com/1/2');
  assert.equal(esc(`'<>\"&`), '&#39;&lt;&gt;&quot;&amp;');
  assert.equal(markTerm('<script & alert>', 'lt'), '&lt;script &amp; alert&gt;');
  assert.equal(markTerm('<script & alert>', 'amp'), '&lt;script &amp; alert&gt;');
  assert.equal(markTerm('see <tag> now', '<tag>'), 'see <mark>&lt;tag&gt;</mark> now');
  assert.equal(markTerm('note: hello', '//hello', 'all'), 'note: <mark>hello</mark>');
  assert.equal(markTerm('literal //hello', '//hello', 'text'), 'literal <mark>//hello</mark>');

  // 4. devices.js / pair.js / quad.js / lightbox.js / fab.js / style.css / app.js 契约
  const [devicesSrc, pairSrc, quadSrc, lbSrc, fabSrc, cssSrc, appSrc] = await Promise.all([
    read('../app/static/devices.js'),
    read('../app/static/pair.js'),
    read('../app/static/quad.js'),
    read('../app/static/lightbox.js'),
    read('../app/static/fab.js'),
    read('../app/static/style.css'),
    read('../app/static/app.js'),
  ]);
  assert.ok(devicesSrc.includes("encodeURIComponent(b.dataset.device)"));
  assert.ok(pairSrc.includes('history.replaceState') && pairSrc.includes("searchParams.delete('code')"));
  assert.ok(appSrc.includes("clean.searchParams.delete('code')") && appSrc.includes('S.readOnly || $(\'#detail\').hidden'));
  assert.ok(quadSrc.includes('avatarSrc(p.avatar)') && quadSrc.includes('src="/media/${esc(isVid'));
  assert.ok(lbSrc.includes('src="${esc(src)}"') && lbSrc.includes('data-i="${esc(gi)}"'));
  assert.ok(!fabSrc.includes('<span>${label}</span>') && fabSrc.includes('span.textContent ='));
  for (const sel of ['#sel-tags', '#rail-triage', '#tt-select', '#tt-play']) {
    assert.ok(cssSrc.includes(`html[data-device-role="viewer"]`) && cssSrc.includes(sel), `viewer 应隐藏 ${sel}`);
  }
});

test('Wave1 安全加固：模板 CSP 就绪（无内联脚本/样式与事件属性）、外链 noreferrer、扩展过滤与采集器边界', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');

  // 1. 模板无内联 <script>/<style> 与 on*= 事件属性，外链带 noreferrer
  for (const name of ['index.html', 'kiosk.html', 'pair.html', 'stats.html']) {
    const html = await read(`../app/templates/${name}`);
    assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), `${name} 不应含内联 <script>`);
    assert.ok(!/<style[\s>]/i.test(html), `${name} 不应含内联 <style>`);
    assert.ok(!/\son[a-z]+\s*=/i.test(html), `${name} 不应含内联 on* 事件属性`);
  }
  const indexHtml = await read('../app/templates/index.html');
  assert.ok(indexHtml.includes('<script src="/static/boot.js?v={{ v }}"></script>'));
  assert.ok(!indexHtml.includes('rel="noopener" '), '外部原博链接必须同时带 noreferrer');
  assert.ok(indexHtml.includes('rel="noopener noreferrer"'));

  // 2. 扩展 Cookie 白名单、非回环明文 HTTP 告警、#addr 变更先鉴权与旧权限清理
  const { fields, cookieHeader } = filterWeiboCookies([
    { name: 'SUB', value: 'rootSub', path: '/' },
    { name: 'SUB', value: 'deepSub', path: '/ajax/' },
    { name: 'XSRF-TOKEN', value: 'xsrf123', path: '/' },
    { name: 'SCF', value: 'scf1', path: '/' },
    { name: '_s_tentry', value: 'track', path: '/' },
    { name: 'SESSION_OTHER', value: 'dropme', path: '/' },
  ]);
  assert.equal(fields.get('SUB'), 'deepSub');
  assert.equal(fields.get('XSRF-TOKEN'), 'xsrf123');
  assert.equal(fields.get('SCF'), 'scf1');
  assert.equal(fields.has('_s_tentry'), false);
  assert.equal(fields.has('SESSION_OTHER'), false);
  assert.ok(!cookieHeader.includes('dropme') && !cookieHeader.includes('_s_tentry'));

  assert.equal(isInsecureLanHttp('http://192.168.1.20:8081'), true);
  assert.equal(isInsecureLanHttp('http://10.0.0.5:8081'), true);
  assert.equal(isInsecureLanHttp('http://localhost:8081'), false);
  assert.equal(isInsecureLanHttp('http://127.0.0.1:8081'), false);
  assert.equal(isInsecureLanHttp('http://[::1]:8081'), false);
  assert.equal(isInsecureLanHttp('https://192.168.1.20:8443'), false);
  assert.equal(originPatternFor('http://192.168.1.20:8081'), 'http://192.168.1.20/*');

  const [popupHtml, popupJs, collectorJs] = await Promise.all([
    read('../extension/popup.html'),
    read('../extension/popup.js'),
    read('../tools/x-collector.js'),
  ]);
  assert.ok(popupHtml.includes('id="lan-warn"'));
  assert.ok(popupJs.includes('filterWeiboCookies') && popupJs.includes('revokeUnusedOrigins'));
  assert.match(popupJs, /#addr"\)\.addEventListener\("change"[\s\S]*await allowDestination\(\)[\s\S]*refresh\(\)/);

  // 3. tools/x-collector.js：GraphQL 匹配前置、seen 有界、出错暂停自动重试、stop 卸载钩子
  assert.ok(collectorJs.includes('TIMELINE_RE.test(u)') && collectorJs.includes('MAX_SEEN = 5000'));
  assert.ok(collectorJs.includes('autoFlushBlocked') && collectorJs.includes('window.fetch = _fetch'));
});

test('Wave2 性能优化 F-P1：Lightbox 胶片窗口化渲染（最多 41 张）、原地切换高亮与取消过期预解码', async () => {
  const prevDoc = globalThis.document;
  const prevImg = globalThis.Image;
  const makeCls = (initial = '') => {
    const set = new Set(initial.split(/\s+/).filter(Boolean));
    return {
      add: (...xs) => xs.forEach((x) => set.add(x)),
      remove: (...xs) => xs.forEach((x) => set.delete(x)),
      contains: (x) => set.has(x),
      toggle: (x, f) => { if (f === undefined ? !set.has(x) : f) set.add(x); else set.delete(x); },
    };
  };
  let filmHtmlWrites = 0;
  let filmChildren = [];
  const makeEl = (tag = 'div') => {
    const attrs = new Map();
    const listeners = new Map();
    return {
      tagName: tag.toUpperCase(),
      hidden: true,
      disabled: false,
      style: {},
      classList: makeCls(),
      children: [],
      setAttribute: (k, v) => attrs.set(k, String(v)),
      getAttribute: (k) => attrs.get(k) ?? null,
      addEventListener: (t, fn) => listeners.set(t, fn),
      focus: () => {},
      scrollIntoView: () => {},
      scrollTop: 0,
    };
  };
  const film = makeEl('div');
  Object.defineProperty(film, 'innerHTML', {
    get() { return ''; },
    set(v) {
      filmHtmlWrites += 1;
      filmChildren = [];
      if (!v) { film.children = filmChildren; return; }
      for (const m of v.matchAll(/<button class="([^"]*)" data-i="([^"]*)" data-pos="([^"]*)" role="tab" aria-selected="([^"]*)"/g)) {
        const btn = makeEl('button');
        btn.classList = makeCls(m[1]);
        btn.setAttribute('data-i', m[2]);
        btn.setAttribute('data-pos', m[3]);
        btn.setAttribute('aria-selected', m[4]);
        btn.dataset = { i: m[2], pos: m[3] };
        filmChildren.push(btn);
      }
      film.children = filmChildren;
    },
  });
  const els = {
    '#lightbox': makeEl('div'),
    '#detail': makeEl('div'),
    '#lb-film': film,
    '#lb-img': makeEl('img'),
    '.lb-stage': makeEl('div'),
    '#lb-video': null,
    '#lb-prev': makeEl('button'),
    '#lb-next': makeEl('button'),
  };
  const createdPreloads = [];
  let decodeCalls = 0;
  globalThis.document = { activeElement: null, body: { style: {} } };
  globalThis.Image = class FakeImage {
    constructor() { this.complete = false; this._src = ''; this.onload = null; this.onerror = null; createdPreloads.push(this); }
    set src(v) { this._src = v; }
    get src() { return this._src; }
    decode() { decodeCalls += 1; return Promise.resolve(); }
  };
  let lb;
  try {
    const images = Array.from({ length: 100 }, (_, i) => ({
      p: { sid: `s${i}`, author: `a${i}` },
      pc: { file: `f${i}.jpg`, kind: 'img', w: 100, h: 100 },
      url: `/media/f${i}.jpg`,
    }));
    lb = createLightbox({
      S: { images },
      $: (sel) => els[sel] ?? null,
      animatedHide: (el) => { el.hidden = true; },
      cancelHide: () => {},
      resetZoom: () => {},
      crossPosts: () => true,
    });
    lb.openLB(50);
    assert.equal(filmHtmlWrites, 1);
    assert.equal(filmChildren.length, 41, '窗口化胶片只挂载 pos ± 20（41 个节点）');
    assert.equal(filmChildren[0].dataset.pos, '30');
    assert.equal(filmChildren[40].dataset.pos, '70');
    assert.equal(filmChildren[20].classList.contains('on'), true);
    assert.equal(filmChildren[20].getAttribute('aria-selected'), 'true');

    // 在 40ms 预载窗口前快速切走：旧 40ms timer 应被取消且不会解码
    lb.lbNav(1);
    assert.equal(filmHtmlWrites, 1, '窗口内步进不得重建 #lb-film DOM');
    assert.equal(filmChildren[20].classList.contains('on'), false);
    assert.equal(filmChildren[20].getAttribute('aria-selected'), 'false');
    assert.equal(filmChildren[21].classList.contains('on'), true);
    assert.equal(filmChildren[21].getAttribute('aria-selected'), 'true');

    await new Promise((r) => setTimeout(r, 65));
    assert.equal(createdPreloads.length, 2, '仅最新 pos 的左右邻图发起预载');
    const staleImg = createdPreloads[0];
    // 滑出左缓冲边界（pos=34 < 30+5）触发窗口滑动：[14, 54]（41 张），随后停在 pos=23 仍在窗口内
    for (let k = 0; k < 28; k += 1) lb.lbNav(-1);
    assert.equal(filmHtmlWrites, 2, '触达边缘缓冲应滑动重渲染新窗口');
    assert.equal(filmChildren[0].dataset.pos, '14');
    assert.equal(filmChildren[40].dataset.pos, '54');
    assert.equal(staleImg.src, '', '切图/关闭时未完成预载应清空 src 中止');
    staleImg.onload?.();
    assert.equal(decodeCalls, 0, '过期 seq 的预载回调不得触发 decode()');
    lb.closeLB();
  } finally {
    lb?.closeLB?.();
    globalThis.document = prevDoc;
    globalThis.Image = prevImg;
  }
});

test('Wave2 性能优化 F-P3：mark 与 keep_count 重排增量维护 S.images，不触发整墙 rebuildImages', async () => {
  const prevCss = globalThis.CSS;
  const prevDoc = globalThis.document;
  globalThis.CSS = { escape: (s) => String(s) };
  globalThis.document = { querySelectorAll: () => [] };
  try {
    let rebuildCalls = 0;
    const cards = new Map();
    const makeCard = (sid) => ({
      dataset: { sid },
      classes: new Set(),
      classList: {
        toggle(c, f) { if (f === undefined ? !this.has(c) : f) cards.get(sid).classes.add(c); else cards.get(sid).classes.delete(c); },
        contains(c) { return cards.get(sid).classes.has(c); },
      },
      querySelector: () => null,
      remove() { cards.delete(sid); },
    });
    const p1 = { sid: 'p1', status: 'pending', author: 'a1', uid: 'u1', list_id: 'g1', created_at: '2026-10-02T00:00:00+08:00', keep_count: 1, pics: [{ file: 'p1a.jpg' }] };
    const p2 = { sid: 'p2', status: 'pending', author: 'a2', uid: 'u2', list_id: 'g1', created_at: '2026-10-01T00:00:00+08:00', keep_count: 1, pics: [{ file: 'p2a.jpg' }, { file: 'p2b.jpg' }] };
    cards.set('p1', makeCard('p1'));
    cards.set('p2', makeCard('p2'));
    const S = {
      status: 'pending', group: 'all', author: 'all', range: 'all', source: 'all', hasCmts: 'all', tags: 'all',
      searchText: '', searchScope: 'all', sort: '', readOnly: false, total: 2,
      posts: [p1, p2],
      images: [
        { p: p1, pc: p1.pics[0], url: '/media/p1a.jpg' },
        { p: p2, pc: p2.pics[0], url: '/media/p2a.jpg' },
        { p: p2, pc: p2.pics[1], url: '/media/p2b.jpg' },
      ],
      decisions: new Map(),
    };
    const feed = {
      querySelector: (sel) => {
        const m = sel.match(/data-sid="([^"]*)"/);
        return m ? cards.get(m[1]) || null : null;
      },
    };
    const acts = createPostActions({
      S, feed,
      $: () => ({ hidden: true }),
      api: async (url, _m, body) => {
        if (url.endsWith('/status')) return { decision: { status: body.status } };
        if (url.endsWith('/reaffirm')) return { ok: true, keep_count: 3, last_confirm_at: '2026-10-03T00:00:00+08:00' };
        return { ok: true };
      },
      toast: () => {},
      insertCard: (p) => { cards.set(p.sid, makeCard(p.sid)); },
      rebuildImages: () => { rebuildCalls += 1; },
      updateMore: () => {},
      queueStatusRefresh: () => {},
      renderLB: () => {},
      getLBIndex: () => -1,
      refreshDetail: () => {},
      getDetailSid: () => null,
      startMaintenance: () => {},
      likedTodayOf: () => false,
    });
    const res = acts.mark('p2', 'discarded');
    await res.settled;
    assert.equal(rebuildCalls, 0, 'mark 摘卡不得触发全墙 rebuildImages');
    assert.deepEqual(S.images.map((x) => x.pc.file), ['p1a.jpg']);

    // 切到 keep_count 排序并赞 p2 使之超过 p1：应增量重插 S.images 且不调用 rebuildImages
    S.status = 'kept';
    S.sort = 'keep_count';
    p1.status = 'kept'; p1.keep_count = 2; p1.kept_at = '2026-10-01T00:00:00+08:00';
    p2.status = 'kept'; p2.keep_count = 1; p2.kept_at = '2026-10-02T00:00:00+08:00';
    S.posts = [p1, p2];
    S.images = [
      { p: p1, pc: p1.pics[0], url: '/media/p1a.jpg' },
      { p: p2, pc: p2.pics[0], url: '/media/p2a.jpg' },
      { p: p2, pc: p2.pics[1], url: '/media/p2b.jpg' },
    ];
    cards.set('p2', makeCard('p2'));
    await acts.reaffirm('p2', 1);
    assert.equal(rebuildCalls, 0, 'keep_count 重排不得触发全墙 rebuildImages');
    assert.deepEqual(S.posts.map((x) => x.sid), ['p2', 'p1']);
    assert.deepEqual(S.images.map((x) => x.pc.file), ['p2a.jpg', 'p2b.jpg', 'p1a.jpg']);
  } finally {
    globalThis.CSS = prevCss;
    globalThis.document = prevDoc;
  }
});

test('Wave2 性能优化 F-P5：Timeline 仅在编年态激活 MutationObserver，退出立即 disconnect', () => {
  const prevDoc = globalThis.document;
  const prevWin = globalThis.window;
  const prevMO = globalThis.MutationObserver;
  const prevMM = globalThis.matchMedia;
  let observeCalls = 0, disconnectCalls = 0;
  const makeEl = () => {
    const el = {
      hidden: false, dataset: {}, style: {}, innerHTML: '', scrollTop: 0,
      classList: { set: new Set(), toggle(c, f) { const on = f === undefined ? !this.set.has(c) : f; if (on) this.set.add(c); else this.set.delete(c); return on; }, contains(c) { return this.set.has(c); } },
      addEventListener: () => {},
      querySelector: () => makeEl(),
      querySelectorAll: () => [],
      appendChild: () => {},
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 20, height: 400 }),
      setPointerCapture: () => {},
    };
    return el;
  };
  const feedEl = makeEl();
  const rulerEl = makeEl();
  const els = new Map([['#feed', feedEl]]);
  let createdRuler = null;
  globalThis.MutationObserver = class FakeMO {
    observe() { observeCalls += 1; }
    disconnect() { disconnectCalls += 1; }
  };
  globalThis.matchMedia = () => ({ matches: false });
  globalThis.window = {
    pwGetPrefs: () => ({ layout: '' }),
    pwSetPref: () => {},
    addEventListener: () => {},
    scrollY: 0,
    innerHeight: 800,
  };
  globalThis.document = {
    documentElement: { dataset: {}, style: { setProperty: () => {} } },
    body: { appendChild: (el) => { createdRuler = el; } },
    getElementById: () => null,
    querySelector: (s) => (s === '.topbar' ? { offsetHeight: 40 } : rulerEl),
    querySelectorAll: () => [],
    createElement: () => makeEl(),
    addEventListener: () => {},
  };
  try {
    const tl = createTimeline({
      S: { sort: '', loading: false, status: 'all', group: 'all', author: 'all', range: 'all', searchText: '', hasCmts: 'all', source: 'all' },
      $: (s) => els.get(s) || null,
      api: async () => ({ days: [] }),
      toast: () => {},
      load: () => {},
    });
    tl.init();
    assert.ok(createdRuler, '密度尺应挂载');
    assert.equal(observeCalls, 0, '非编年态初始化不得挂载 MutationObserver');
    tl.setLayout(true);
    assert.equal(observeCalls, 1, '切入编年态后应挂载 MutationObserver');
    tl.setLayout(false);
    assert.equal(disconnectCalls, 1, '退出编年态必须 disconnect() 并释放观察者');
  } finally {
    globalThis.document = prevDoc;
    globalThis.window = prevWin;
    globalThis.MutationObserver = prevMO;
    globalThis.matchMedia = prevMM;
  }
});

test('Wave2 性能优化契约 F-P2/F-P4/F-P6/F-P7/F-P8：热路径、事件委托、异步解码与 CSS 选择器边界', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');
  const [appSrc, reviewSrc, railSrc, settingsSrc, kioskSrc, fabSrc, notesSrc, statsSrc, authorSrc, cssSrc] = await Promise.all([
    read('../app/static/app.js'),
    read('../app/static/review.js'),
    read('../app/static/rail.js'),
    read('../app/static/settings.js'),
    read('../app/static/kiosk.js'),
    read('../app/static/fab.js'),
    read('../app/static/notes.js'),
    read('../app/static/stats.js'),
    read('../app/static/author-view.js'),
    read('../app/static/style.css'),
  ]);

  // F-P2: 无每卡 scrollHeight 重排，使用 DocumentFragment 批插 + 防抖/RAF resize + 非卡结构变更不清空 colHeights
  assert.ok(appSrc.includes('function estCardHeight') && appSrc.includes('function appendCardsBatch') && appSrc.includes('createDocumentFragment'));
  assert.ok(appSrc.includes("window.addEventListener('resize'") && appSrc.includes('requestAnimationFrame') && appSrc.includes('colWidth = 0'));
  assert.ok(!appSrc.includes('col.scrollHeight'));

  // F-P3: load() 增量追加 S.images；sel-all 单次遍历；currentCard 二分查找
  assert.ok(!appSrc.includes('realignDataI') && appSrc.includes('for (const pc of p.pics) S.images.push'));
  assert.ok(!appSrc.includes("querySelectorAll('.card').forEach((c) => { c.style.transform = '';"));
  assert.ok(appSrc.includes('const scanList = (els) =>'));

  // F-P4: 删除 load(true) 无效 refreshAuthors()；spotlight 去重 + stopPropagation；detailAdvance 智能复用；rail 不可见与窄屏不轮询；settings 成员抽屉原位展开
  assert.ok(!appSrc.includes('if (reset) refreshAuthors();'));
  assert.ok(appSrc.includes('function detailMatchesPost') && appSrc.includes("!modal.hidden && !modal.classList.contains('closing')"));
  assert.ok(appSrc.includes("$('#f-search')?.addEventListener('click', (e) => {\n    e.preventDefault();\n    e.stopPropagation();"));
  assert.ok(railSrc.includes('document.hidden') && railSrc.includes("matchMedia('(min-width: 900px)')"));
  assert.ok(settingsSrc.includes('function setXGroupOpen') && settingsSrc.includes('function bindMembersRow'));

  // F-P6: kiosk 委托事件与预计算极值；author-view / fab / selectionchange 节流与零热路径测量
  assert.ok(kioskSrc.includes('wall.addEventListener(\'pointermove\'') && kioskSrc.includes('maxCover'));
  assert.ok(authorSrc.includes('stickyRaf = requestAnimationFrame(() => { stickyRaf = 0; syncSticky(); })'));
  assert.ok(fabSrc.includes('fw: fab.offsetWidth, fh: fab.offsetHeight') && fabSrc.includes('requestAnimationFrame'));
  assert.ok(appSrc.includes('quoteSelRaf'));

  // F-P7 & F-P8: CSS 优化 + class 替代 body:has + 去 offsetParent + decoding="async"
  assert.ok(cssSrc.includes('html:not([data-skin="brut"]) #feed:not(.author-film):not(.author-gallery):not(.author-blueprint) .card'));
  assert.ok(!cssSrc.includes('* { border-radius: 0 !important; }'));
  assert.ok(!cssSrc.includes('blur(18px)'));
  assert.ok(!cssSrc.includes('body:has(#selbar') && cssSrc.includes('body.has-selbar #toast') && cssSrc.includes('body.has-detail #toast'));
  assert.ok(appSrc.includes("classList.toggle('has-detail'") || appSrc.includes("classList.add('has-detail')"));
  assert.ok(appSrc.includes("classList.toggle('has-selbar'") && reviewSrc.includes("classList.add('has-detail')"));
  assert.ok(!settingsSrc.includes('offsetParent') && settingsSrc.includes('ADMIN_ONLY_PANES'));
  assert.ok(!railSrc.includes('<img'), 'rail.js 左悬浮胶囊已精简为纯图标/文字导航，不再渲染博主头像 <img>');
  for (const [name, src] of [['kiosk', kioskSrc], ['notes', notesSrc], ['stats', statsSrc], ['author-view', authorSrc]]) {
    assert.ok(src.includes('decoding="async"'), `${name}.js 图片应带 decoding="async"`);
  }
});

test('博主管理大厅（/kiosk）：拼音搜索、A–Z/# 索引、分组过滤、2 步取消关注与 xg: 调整契约', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');
  const [kHtml, kCss, kJs] = await Promise.all([
    read('../app/templates/kiosk.html'),
    read('../app/static/kiosk.css'),
    read('../app/static/kiosk.js'),
  ]);

  assert.ok(kHtml.includes('id="ks-search"') && kHtml.includes('id="ks-sort"')
    && kHtml.includes('id="ks-groups"') && kHtml.includes('id="ks-alpha"')
    && kHtml.includes('id="ks-newgrp-form"') && kHtml.includes('id="ks-toast"'),
    '/kiosk 模板应包含检索、排序、分组、首字母索引与新建分组表单');
  assert.ok(kCss.includes('.ks-controls') && kCss.includes('.ks-spread-actions')
    && kCss.includes('.ks-gchip') && kCss.includes('.ks-toast'),
    'kiosk.css 应定义控制条、详情操作与分组芯片样式');
  assert.ok(kJs.includes("d.pinyin") && kJs.includes("b.dataset.idx = String(i)")
    && kJs.includes('confirmUnfollowKey') && kJs.includes('再次点击确认取消关注')
    && kJs.includes("S.role === 'admin'") && kJs.includes("list_id: 'xg:'")
    && kJs.includes("await ksApi('/api/groups/' + encodeURIComponent(d.follow_id), 'DELETE')")
    && kJs.includes('safeMedia(') && kJs.includes('esc('),
    'kiosk.js 应完整实现筛选映射、admin 鉴权开关、2 步取消关注、xg: 成员增删与输出转义');
});

test('远端修复整合契约：失败重试、防死循环、防连点 busy 态与无障碍属性', async () => {
  const fs = await import('node:fs/promises');
  const read = (p) => fs.readFile(new URL(p, import.meta.url), 'utf8');
  const [indexHtml, appSrc, kioskSrc, statsSrc, notesSrc, reviewSrc, devicesSrc, settingsSrc] = await Promise.all([
    read('../app/templates/index.html'),
    read('../app/static/app.js'),
    read('../app/static/kiosk.js'),
    read('../app/static/stats.js'),
    read('../app/static/notes.js'),
    read('../app/static/review.js'),
    read('../app/static/devices.js'),
    read('../app/static/settings.js'),
  ]);

  assert.ok(appSrc.includes("loadStatus().catch((e) => toast('状态加载失败：' + e.message)).then(() => load(true))"),
    'status 加载失败时仍应继续加载主墙');
  assert.ok(appSrc.includes("more.setAttribute('aria-busy', 'true')") && appSrc.includes("more.removeAttribute('aria-busy')"),
    '加载更多按钮请求期间应进入 aria-busy 禁用态');
  assert.ok(kioskSrc.includes('async function loadShelf()') && kioskSrc.includes('ks-retry'),
    'kiosk 书架加载失败应提供重试按钮');
  assert.ok(statsSrc.includes('st-retry') && notesSrc.includes('note-retry'),
    'stats 与 notes 加载失败应提供重试按钮');
  assert.ok(reviewSrc.includes('if (added === null) return;'),
    'review 加载失败不应误报已到列表末尾');
  assert.ok(devicesSrc.includes('logout.disabled = true') && settingsSrc.includes('if (b.disabled) return; b.disabled = true;'),
    '设备解绑与分组操作按钮请求期间应禁用防连点');
  assert.ok(indexHtml.includes('id="ss-state" class="ss-state" role="status" aria-live="polite"')
    && indexHtml.includes('aria-label="微博博主 UID 或主页链接"'),
    '放映状态与设置输入框应具备无障碍角色与标签');
});

test('筛选面板关闭回归焦点：hadFocus 捕获 + 焦点还给可见打开按钮', async () => {
  const fs = await import('node:fs/promises');
  const source = await fs.readFile(new URL('../app/static/app.js', import.meta.url), 'utf8');
  const start = source.indexOf('function closeViewPanel()');
  const end = source.indexOf('function toggleViewPanel()');
  assert.ok(start !== -1 && end !== -1 && start < end, 'app.js 应存在 closeViewPanel 与 toggleViewPanel');
  const body = source.slice(start, end);
  assert.ok(body.includes('panel.contains(document.activeElement)'), '关面板前应捕获焦点是否在面板内');
  assert.ok(body.includes('focus({preventScroll:true})'), '面板关闭后应把焦点还给实际可见的打开按钮');
});
