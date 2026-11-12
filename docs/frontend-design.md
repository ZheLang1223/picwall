---
title: Picwall 前端设计档案
description: 设计令牌/组件/交互模型的 2026-09 快照（对应提交 f914147）；行数与 JS 架构已过时，令牌与设计原则仍有效。
date: 2026-09-19
---

# Picwall 前端设计档案

> 只读审查产物，对应提交 `f914147` 时的代码状态（**2026-09 快照**）。
> 此后实现已演进：JS 从单文件 app.js 拆为 `app/static/` 下多个 ES module，
> 卡片/详情交互多轮改版——本档案的行数、组件清单与 §8 JS 架构不再反映现状，
> 令牌与设计原则仍有效；以 `app/static/` 源码与 [架构总览](architecture.md) 为准。
> **2026-11-06 起墙卡为三段结构（首图 / 标题行 / 作者行+点赞 ♥N），§4.5 的 meta 行
> 构成（原创/转发角标、@转发者、时间、收藏 chip）已整体退役，勿按此节复刻。**
> 源文件：`app/templates/index.html`（222 行）· `app/static/style.css`（572 行）· `app/static/app.js`（约 1290 行）。
> 无框架、无构建：vanilla JS + 单文件 CSS，静态资源以 `?v=<进程启动时间>` 做缓存失效。

---

## 1. 设计原则

1. **单色系**：纸白底 + 墨色字 + 靛蓝点缀，danger 红只用于"不要"语义；靠排版与留白，不靠装饰（图标仅主题切换一枚 SVG）。
2. **图片是主体**：卡片 chrome（meta/动作条）视觉权重恒低于图片；来源/状态标记都是小号 chip。
3. **全站一条缓动**：`--ease: cubic-bezier(0.23, 1, 0.32, 1)`（强 ease-out），所有过渡只声明实际变化的属性；按压统一 `scale(.97)` 下陷。
4. **双主题零组件改动**：所有随主题变化的颜色走令牌，深色只整块覆盖令牌；大图浏览本身是深底，两主题共用一套深色常量。
5. **可达性**：全局唯一 `:focus-visible` 描边、弹层焦点圈闭 + 焦点归还、`prefers-reduced-motion` 全降级、触屏目标 ≥44px、`prefers-reduced-motion` 时动画退化为直现。

---

## 2. 页面骨架

```html
<body>
  <header class="topbar">          <!-- 吸顶 z-30 毛玻璃 -->
    <div class="brand">Picwall</div>
    <nav> 立即抓取(tbtn primary) · 管理分组 · Cookie · 主题切换(#btn-theme) </nav>
  </header>
  <div id="banner"></div>          <!-- Cookie 过期 / 抓取中 / 未配分组 -->
  <div class="toolbar">            <!-- 吸顶 z-20 毛玻璃（桌面） -->
    <div class="seg">待处理·保留·收藏·已删除·全部</div>
    <select 分组> <select 时间> <select 类型>
    选择 · 打包⤓ · <span#counts>
  </div>
  <div class="author-wrap">        <!-- 全部博主▾ + 横向快捷行 + 下拉全量面板 -->
  <main id="feed" class="feed"></main>   <!-- CSS 多列瀑布流 -->
  <div class="more">加载更多 / #endnote</div>
  <div id="selbar">全选本页·✓保留·✕不要·下载所选(n)·退出</div>
  <div id="lightbox">lb-bar / lb-stage(img+hint) / lb-cap / ‹ ›</div>
  <div id="modal-groups">、<div id="modal-cookie">
  <div id="detail">‹ › / detail-card(head·veil×2·meta·text·pics·actions)</div>
  <div id="gesture-hint">、<div id="toast">
</body>
```

z-index 阶梯：topbar 30 < toolbar 20 < author-panel 25 < selbar 45 < modal 50 < detail 55 < lightbox 60 < toast 70 < gesture-hint 80。

---

## 3. 设计令牌（完整代码）

### 3.1 浅色（`:root`，`color-scheme: light`）

```css
:root {
  color-scheme: light;
  --bg: #ffffff;          --ink: #1d1d1f;
  --gray: #6e6e73;        --faint: #6e6e73;   /* 加深过：白底 5.1:1 过 AA */
  --accent: #0066cc;      --surface: #f5f5f7;
  --line: #e5e5ea;        --danger: #c94436;
  --radius: 14px;
  --ease: cubic-bezier(0.23, 1, 0.32, 1);

  --card: #ffffff;        --on-accent: #ffffff;   --on-ink: #ffffff;
  --chrome-bg: rgba(255,255,255,.82);        /* 顶栏毛玻璃 */
  --chrome-bg-strong: rgba(255,255,255,.92); /* 工具栏毛玻璃 */
  --selbar-bg: rgba(255,255,255,.95);
  --warn-bg: #fff8ea;  --warn-line: #ecd9a8;
  --bad-bg: #fdf1ef;   --bad-line: #ecc8c2;
  --accent-line: rgba(0,102,204,.35);   /* chip 描边 */
  --accent-edge: rgba(0,102,204,.45);   /* 保留卡片描边 */
  --accent-wash: rgba(0,102,204,.06);   /* 选中博主水洗底 */
  --ring-keep: rgba(0,102,204,.55);  --ring-drop: rgba(201,68,54,.55);
  --keep-ink: #2e8f57; --keep-line: rgba(63,166,106,.5);
  --danger-line: rgba(201,68,54,.4);
  --veil-keep: #3fa66a; --veil-drop: #e0705f;   /* 快筛闪光幕帘 */
  --toast-link: #7db8ec;
  --scrim: rgba(24,24,26,.38); --scrim-detail: rgba(24,24,26,.45); --scrim-hint: rgba(24,24,26,.55);
  --shadow-hover: 0 10px 28px rgba(29,29,31,.08);
  --shadow-panel: 0 14px 44px rgba(0,0,0,.16);
  --shadow-pop:   0 18px 60px rgba(0,0,0,.25);
}
```

### 3.2 深色（`html[data-theme="dark"]`，只覆盖令牌）

基调：底 `#0d0d0f` · 卡 `#1a1a1e` · 墨字反转 `#e8e8ea` · 次要 `#98989f`（6:1）· accent 提亮 `#4d9fff`（6.4:1）· danger `#e07a6c`（保 4.5:1）。

```css
html[data-theme="dark"] {
  color-scheme: dark;                 /* 原生控件整体转深 */
  --bg:#0d0d0f; --ink:#e8e8ea; --gray:#98989f; --faint:#98989f;
  --accent:#4d9fff; --surface:#232328; --line:#2c2c30; --danger:#e07a6c;
  --card:#1a1a1e; --on-accent:#0d0d0f; --on-ink:#111114;
  --chrome-bg: rgba(13,13,15,.82); --chrome-bg-strong: rgba(13,13,15,.92);
  --selbar-bg: rgba(19,19,22,.95);
  --warn-bg:#2b2416; --warn-line:#57491f;  --bad-bg:#2d1a17; --bad-line:#5e2f27;
  --accent-line: rgba(77,159,255,.4); --accent-edge: rgba(77,159,255,.5);
  --accent-wash: rgba(77,159,255,.14);
  --ring-keep: rgba(77,159,255,.55); --ring-drop: rgba(224,122,108,.55);
  --keep-ink:#4fc489; --keep-line: rgba(79,196,137,.45); --danger-line: rgba(224,122,108,.45);
  --veil-keep:#4cc483; --veil-drop:#ef8574;
  --toast-link:#0066cc;               /* 深色 toast 反转浅底，链接用深蓝 */
  --scrim: rgba(0,0,0,.55); --scrim-detail: rgba(0,0,0,.6); --scrim-hint: rgba(0,0,0,.65);
  --shadow-hover: 0 10px 28px rgba(0,0,0,.5);
  --shadow-panel: 0 14px 44px rgba(0,0,0,.55);
  --shadow-pop:   0 18px 60px rgba(0,0,0,.6);
}
```

**主题初始化**（index.html 内联，必须在 CSS 前执行防闪白）：优先级 `localStorage(pw_theme)` > 系统 `prefers-color-scheme` > 浅色；手动选过以后不再跟系统；`#btn-theme` 浅色显月亮、深色显太阳，点击写回 localStorage。

### 3.3 字体与字号阶梯

- 基准：`15px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro SC", "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", "Microsoft YaHei", sans-serif`，抗锯齿开。
- 字号阶梯（px）：11(kind/chip/src) · 11.5(an-count/lb-count 手机) · 12(via/time/hint/cnt/grow gid) · 12.5(achip/banner code/lbtn 手机) · **13(控件基准：tbtn/seg/select/act/lbtn/counts)** · 13.5(卡片正文/steps) · **14(横幅/pbtn/输入框/gh 正文/toast)** · 14.5(详情正文) · 15(详情作者/输入) · 16(弹层标题/gh 标题) · 19(品牌)。
- 数字场景用 `font-variant-numeric: tabular-nums`（lb-count、dt-progress）。

### 3.4 圆角 / 间距 / 层

- 圆角实际标尺：5(kind/src/code) · 8(按钮/输入/act) · 10(toast/grow/dt-pics img) · 12(横幅/lb-nav/veil/dt-nav) · 14(--radius：卡片/作者面板) · 16(弹窗/详情卡/gh 卡) · 999(胶囊类：tbtn/seg/achip/chip/lb-hint)。
- 栏距：桌面 22px、移动 14px，全站统一；卡片间距 18px；图片网格 gap 2px。
- 阴影三档：hover（最轻）/ panel（作者面板）/ pop（弹窗），深色下加深加黑。

---

## 4. 组件库（CSS 代码 + 描述）

### 4.1 顶栏 `.topbar`

```css
.topbar { position: sticky; top: 0; z-index: 30;
  background: var(--chrome-bg); backdrop-filter: saturate(1.8) blur(16px);
  border-bottom: 1px solid var(--line); padding: 14px 22px; }
.brand { font-size: 19px; font-weight: 700; letter-spacing: -.02em; }
.tbtn { border:1px solid var(--line); background:var(--bg); border-radius:999px;
  padding:6px 14px; font-size:13px; }
.tbtn.primary { background: var(--ink); color: var(--on-ink); border-color: var(--ink); }
```

吸顶毛玻璃；`.tbtn.primary`（立即抓取）用墨底反转——与 `.pbtn`（弹层内 accent 蓝底实底按钮）构成两档 CTA。`#btn-theme` 是唯一 SVG 图标按钮。抓取中 `.tbtn:disabled { opacity:.45 }`。

### 4.2 横幅 `.banner`

```css
.banner { margin:16px 0 0; padding:14px 18px; border-radius:12px;
  background: var(--surface); border:1px solid var(--line); font-size:14px; }
.banner.warn { background:var(--warn-bg); border-color:var(--warn-line); }
.banner.bad  { background:var(--bad-bg);  border-color:var(--bad-line); }
```

三态：默认（抓取中，带 `.spin` 旋转圈）、warn、bad（Cookie 过期红色提醒，含 `<small>` 错误详情与操作按钮）。由 `renderBanner()` 按状态互斥渲染，无内容时清空占位为 0 高。

### 4.3 工具栏 `.toolbar` 与分段控件 `.seg`

```css
.toolbar { position:sticky; top:53px; z-index:20; gap:10px; flex-wrap:wrap;
  background:var(--chrome-bg-strong); backdrop-filter:blur(12px); }
.seg { display:inline-flex; background:var(--surface); border-radius:999px; padding:3px; }
.seg button.on { background:var(--card); color:var(--ink); font-weight:600;
  box-shadow:0 1px 4px rgba(0,0,0,.12); }
.toolbar select { border:1px solid var(--line); border-radius:8px;
  background:var(--card); font-size:13px; }
.counts { margin-left:auto; font-size:13px; color:var(--faint); }
```

五个状态标签（待处理·保留·**收藏**·已删除·全部）胶囊分段；待处理标签动态带计数。三个原生 select（分组/时间/类型），分组下拉里 X 分组显示「X·名称」前缀。`counts` 行汇总 全部计数 + 每日抓取时刻。`top:53px` 是与顶栏高度耦合的魔法数（顶栏实高约 63px，靠自身 padding 兜住）。

### 4.4 作者条 `.author-wrap` / `.achip` / `.author-panel`【已退役 2026-11-07】

> **2026-11-07 筛选中枢改造**：本节的作者条/六态排序/仅头像档位已整体退役，博主筛选移入视图面板「博主」行（`#f-author`：胶囊 + 行内搜索选择器 `.fa-picker`）。段落保留作历史设计记录，与现状不一致处以代码为准。
>
> **2026-11-07 联系人式索引**：选择器改为拼音顺序 + 首字母分组——服务端 `/api/authors` 随行返回 `initial`（与 `name_asc` 同源的拼音首字母，非字母归 `#`）；前端无搜索词时按字母分组渲染（`.fa-letter` 吸顶 + 右缘 `.fa-index` 索引轨，点按/扫动 `faJump` 跳组），有搜索词退化为扁平结果。`.fa-list` 是唯一滚动壳，`renderFaList` 只换其内容以保搜索框焦点。

```css
.author-strip { flex:1; display:flex; gap:8px; overflow-x:auto; scrollbar-width:none; }
.author-panel { position:absolute; left:22px; right:22px; top:calc(100% + 4px); z-index:25;
  max-height:56vh; overflow:auto; background:var(--card); border-radius:14px;
  box-shadow:var(--shadow-panel); }
.author-panel:not([hidden]) { animation:card-in .18s var(--ease); transform-origin:top left; }
.achip { display:inline-flex; gap:6px; border:1px solid var(--line); border-radius:999px;
  padding:3px 12px 3px 4px; font-size:12.5px; }
.achip.on { border-color:var(--accent); background:var(--accent-wash);
  color:var(--accent); font-weight:600; }
```

「全部博主 ▾」固定行首（caret 蓝），点开全量面板（首项"不限博主"+ 全部博主 + 顶部排序三态 seg：名称/帖数多→少/帖数少→多）；快捷行可横滚（滚动条隐藏），行尾一枚 ⇅ 循环切换胶囊；选中胶囊 `scrollIntoView` 式滚进视野；面板从左上角长出（transform-origin）。计数跟随当前标签页口径；面板外点击收起。

### 4.5 卡片 `.card`（核心组件）

```css
.feed { padding:20px 22px 60px; column-gap:18px; column-width:330px; }
.card { break-inside:avoid; margin:0 0 18px; background:var(--card);
  border:1px solid var(--line); border-radius:var(--radius); overflow:hidden;
  position:relative; touch-action:pan-y;
  transition:transform .18s var(--ease), box-shadow .18s var(--ease); }
.card.kept { border-color:var(--accent-edge); }
.card.fading { transition:opacity .35s; opacity:0; pointer-events:none; }
@media (hover:hover) and (pointer:fine) {
  .card:hover { transform:translateY(-2px); box-shadow:var(--shadow-hover); } }
.pics { display:grid; gap:2px; background:var(--surface); cursor:zoom-in; }
.pics.p2 { grid-template-columns:1fr 1fr; }  .pics.p3 { grid-template-columns:1fr 1fr 1fr; }
.pics.p1 img { max-height:540px; object-fit:cover; }   /* p1 内联 aspect-ratio 占位防抖动 */
.pics.p2 img, .pics.p3 img { aspect-ratio:1 / 1; }
.meta { display:flex; align-items:center; gap:8px; padding:10px 14px 0; }
.avatar { width:24px; height:24px; border-radius:50%; background:var(--surface); }
.meta b { font-size:14px; }  .via { font-size:12px; color:var(--faint); }
.text { margin:6px 14px 0; font-size:13.5px;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.actions { display:flex; align-items:center; gap:4px; padding:8px 8px 10px; }
.act { border:0; background:none; color:var(--gray); font-size:13px;
  padding:5px 9px; border-radius:8px; }
.act.keep.on { color:var(--accent); font-weight:600; }
.act.drop.on { color:var(--danger); font-weight:600; }
.act.fav.on { color:var(--accent); font-weight:600; }
.chip { font-size:11px; color:var(--accent); border:1px solid var(--accent-line);
  border-radius:999px; padding:0 7px; }        /* 已保留 / ★收藏 */
.kind, .src { font-size:11px; color:var(--faint); border:1px solid var(--line);
  border-radius:5px; padding:0 5px; }          /* 原创|转发 · 来源(X 才显示) */
```

- 图片网格：1 图自适应高（≤540px，内联 `aspect-ratio:w/h` 占位防 CLS）、2/3 图固定 1:1 网格；全部用 640px 缩略图 + `loading=lazy decoding=async`。
- meta 行：头像（无图回退首字）→ 作者 → 来源 chip（仅非微博）→ 原创/转发 → @转发者 → 状态 chip → ★收藏 chip → 时间（右对齐）。
- 动作行（**全图标化，无文字**）：外链 ↗（原博）· ☆/★（收藏，实心=已收藏）· ✓圈（保留，
  on 态转 accent）· ✕/↺（不要 / 已删态变恢复）·（保留帖）⤓ 下载（当日打包）· 右对齐"N 图"计数。
  图标 = 16px feather 风格描边 SVG（`stroke=currentColor`，与主题按钮同语法），文字语义进
  `title` + `aria-label`（悬停/读屏可辨），状态语义由颜色与星形实心承担，meta 行 chip 仍标状态。
- 状态类：`.kept` 蓝描边；`.fading` 淡出 350ms 后移除；`.sw-keep/.sw-drop` 滑动时 3px 内描边（蓝/红令牌）；`.picked` 选择模式 2px 外描边 + 右上 pick-dot 圆标。
- 事件委托在 `#feed` 上：`.act[data-do]` 分发、点图开大图、点 meta/text 进详情；触屏右滑保留/左滑不要（`touch-action:pan-y` 保竖滚）。

### 4.6 空态 `.more`

空视图给 `9vh` 顶部留白 + 拟人文案（如"这一栏是空的——先「立即抓取」"、"收藏的图都翻完了"）；加载失败时按钮变"加载失败，点此重试"。

### 4.7 大图浏览 `.lightbox`（深底浮层 z-60）

```css
.lightbox { position:fixed; inset:0; background:rgba(24,24,26,.97); }
.lbtn { color:#d7d7db; font-size:13px; border-radius:8px; padding:5px 10px; }
.lbtn:hover { background:rgba(255,255,255,.12); color:#fff; }
.lbtn.keep.on { color:#7db8ec; font-weight:600; }   /* 深底上的浅化 accent */
.lbtn.drop.on { color:#e89a90; font-weight:600; }
.lb-stage img { max-width:100%; max-height:100%; object-fit:contain; }
.lb-nav { width:46px; height:64px; background:rgba(255,255,255,.1);
  font-size:34px; border-radius:12px; }
```

底部说明条：作者 · @转发者 · 正文（60% 宽截断）· 时间。详情快筛按 `F`/`↑` 保留、`D`/`↓` 不要；按 `M` 收藏、`L` 打开原帖。大图中 `M` 收藏、`L` 打开原帖。两侧竖向导航钮；加载中 spinner（`.lb-stage.loading::after` + `@keyframes r`）；手势时中央提示胶囊 `#lb-hint`（↑松手保留/↓松手不要）。相邻图预加载 + `decode()` 预热解码缓存。点空白（backdrop/stage）关闭。

### 4.8 详情页 `.detail`（快筛 z-55）

```css
.detail { background:var(--scrim-detail); padding:5vh 16px 16px; }
.detail-card { width:680px; max-height:88vh; overflow:auto; background:var(--card);
  border-radius:16px; box-shadow:var(--shadow-pop);
  transition:transform .2s, opacity .2s, box-shadow .2s; }
.detail-head { position:sticky; top:-14px; background:var(--card);
  border-bottom:1px solid var(--line); }   /* 分组名 · n/m 进度 · 关闭 */
.flash-veil { font-size:28px; font-weight:800; border:4px solid;
  opacity:0; transform:rotate(∓8deg); }    /* 拖拽时的 保留/不要 印章幕帘 */
.dt-nav { position:absolute; top:50%; width:44px; height:64px;
  background:rgba(24,24,26,.45); font-size:30px; border-radius:12px; }
```

完整正文（`white-space:pre-wrap`）+ 全部图片竖排（`object-fit:contain`，右下角悬浮"下载原图"）+ 底部动作行（原博·保留·不要·收藏·首图⤓）。点卡片 meta/text 进入；←→ 翻帖（到底自动补载下一页继续）；拖拽 >90px 触发标记并飞出（±340px rotate ±6°）；↑↓ 标记并自动前进；进度 `n / total`。

### 4.9 选择模式 `.selecting` 与底栏 `.selbar`

```css
.feed.selecting .card { cursor:pointer; }
.feed.selecting .card .actions { pointer-events:none; opacity:.35; }  /* 动作行降权防误触 */
.pick-dot { position:absolute; top:8px; right:8px; width:26px; height:26px;
  border-radius:50%; border:2px solid #fff; background:rgba(24,24,26,.35); }
.card.picked { outline:2px solid var(--accent); }
.card.picked .pick-dot { background:var(--accent); }
.selbar { position:fixed; bottom:0; z-index:45; background:var(--selbar-bg);
  backdrop-filter:blur(12px); padding-bottom:calc(10px + env(safe-area-inset-bottom)); }
```

底栏五钮：全选本页 · ✓保留 · ✕不要 · 下载所选(n) · 退出；进场 `selbar-in`（自下滑入）。点卡片即勾选，不进大图不进详情。

### 4.10 弹层 `.modal`（管理分组 / Cookie）

```css
.modal { background:var(--scrim); padding:9vh 16px 16px; }
.modal-card { width:620px; max-height:80vh; overflow:auto; background:var(--card);
  border-radius:16px; box-shadow:var(--shadow-pop); }
.pbtn { background:var(--accent); color:var(--on-accent); border-radius:8px;
  padding:8px 16px; font-size:14px; }
.pbtn.ghost { background:var(--card); color:var(--ink); border:1px solid var(--line); }
.grow { border:1px solid var(--line); border-radius:10px; padding:8px 12px; }
```

管理分组：分组行（名称 + X· 前缀 + list_id + 待/留/删统计 + 回补[仅微博] / 移除）、「拉取我的分组」、「手动添加」（ID+备注）；Cookie：三步教程（ol.steps + code）+ textarea（等宽字体）+ 保存/清除。Esc / 点幕帘 / ✕ 三路关闭，均走 140ms 淡出。

### 4.11 Toast `#toast`

```css
#toast { position:fixed; left:50%; bottom:34px; transform:translateX(-50%);
  background:var(--ink); color:var(--on-ink); border-radius:10px; font-size:14px;
  opacity:0; pointer-events:none; transition:opacity .2s; z-index:70; max-width:86vw; }
#toast.show { opacity:.96; pointer-events:auto; }
#toast button { color:var(--toast-link); font-weight:600; }
```

无动作 2.8s、带动作（撤销）6s；撤销链接浅蓝（深色主题 toast 反转浅底、链接深蓝）。用于：归档结果、快筛完成、错误、以及"已保留/已移入不要 → 撤销"。

### 4.12 首用手势提示 `.gesture-hint`

仅触屏设备一次性（localStorage `pw_ghint`）；底部卡片（非居中）列出手势速查 + "滑错了 6 秒内都能撤销"。

---

## 5. 动效系统

```css
@keyframes layer-in { from { opacity:0; } }                       /* 幕帘淡入 .2s/.18s */
@keyframes layer-out { to { opacity:0; } }                        /* 退场 .14s（出比进快） */
@keyframes card-in { from { opacity:0; transform:translateY(10px) scale(.97); } }
@keyframes selbar-in { from { transform:translateY(100%); } }
@keyframes r { to { transform:rotate(360deg); } }                 /* spinner */
.modal.closing, .detail.closing, .lightbox.closing, .gesture-hint.closing {
  animation: layer-out .14s var(--ease) both; pointer-events:none; }
@media (prefers-reduced-motion: reduce) { * { animation-duration:.01ms !important;
  transition-duration:.01ms !important; } }
```

- 卡片淡出 `.fading`：350ms opacity→0 后 `remove()`（JS 定时器与 CSS 时长一致）。
- 大图手势跟手：位移 0.5x 系数、透明度随位移降到 0.45；松手回弹/判定。
- 详情飞出：`translateX(±340px) rotate(±6°)` + 淡出，0.22s。
- JS 侧 `animatedHide/cancelHide`：关闭走 `.closing` 140ms，期间忽略重复关闭，重开取消定时。

---

## 6. 响应式（唯一断点 640px）

| 项 | 桌面 | ≤640px |
|---|---|---|
| 栏距 | 22px | 14px |
| 工具栏 | 吸顶 `top:53px` | 放回流（触屏 44px 化后吸顶太占屏） |
| 触控目标 | 默认 | tbtn/act/achip/select/mclose/grow button/more/lbtn 全部 `min-height:44px` |
| 瀑布流 | `column-width:330px`（自适应列数） | 单列 `column-width:100%` |
| counts | 行尾 | 独占一行 |
| 详情 | 680px 居中卡 | 全屏（`height:100%`、圆角 0、safe-area 底垫） |
| 大图操作栏 | 顶部常态 | 沉到底部拇指区（绝对定位 + safe-area） |
| 品牌 | 仅"Picwall" | 同（小字已删） |

safe-area：selbar / detail-card / lightbox / gesture-hint 四处 `env(safe-area-inset-bottom)`。

---

## 7. 交互模型

### 7.1 键盘

| 浮层 | 键 | 行为 |
|---|---|---|
| 主视图、详情、大图 | 1 / 2 / 3 / 4 | 切换保留 / 已删除 / 收藏 / 全部视图；切换时清空伴随筛选（分组/博主/时间/评论/搜索归位）。详情或大图关闭后回到列表顶部 |
| 全局 | Shift + Q / W | 进入快筛（桌面=四格会话，窄屏=详情单卡流；输入态、弹层内不触发） |
| 详情 | F / ↑ | 标记保留并自动前进 |
| | D / ↓ | 标记不要并自动前进 |
| | ← → | 切换本帖图片（边界钳停）；350ms 内同向双击 = 前后翻帖（到底自动 `load(false)` 补页继续） |
| | PageUp / PageDown | 上一帖 / 下一帖（可按住连发） |
| | M | 收藏翻转（不前进） |
| | L | 打开当前原帖 |
| | T | 聚焦评论输入框 |
| 大图 | ← → | 切换当前帖子图片（到边界停止，相邻已预加载+解码预热） |
| 大图 | ↑ / ↓ | 长图上下滚动阅读（按宽适配档）；普通图无滚动面则空操作 |
| | （筛选键） | 大图打开时 F/D/↑/↓ 的标记语义一律不生效，回看/四格也不吃键 |
| | M | 收藏翻转 |
| | L | 打开当前图片所属原帖 |
| | Esc | 关闭 |
| 弹层 | Esc / Tab | 关闭 / 焦点圈闭 |

数字视图键与详情/大图操作键都只响应未组合按键；输入区域、弹层中不触发视图切换。系统组合键交还浏览器。

### 7.2 触屏手势（pointer/touch 事件，仅触屏路径）

| 场景 | 手势 | 阈值 | 效果 |
|---|---|---|---|
| 大图 | 左右滑 | `|dx|>50` 且 >2×竖向 | 切换图片；纯图模式下一帖到头自动接下一帖首图（跨帖连看） |
| 大图 | 双击 | <300ms | 2.2× 缩放（中心原点）↔ 还原；缩放态单指平移，钳在图内 |
| 卡片流 | 长按 | 500ms（位移 >8px 取消） | 进入多选模式 |

滑动分拣已退役：墙卡与详情卡在触屏上只浏览，保留/不要走详情底栏按钮；
大图不足阈值的滑动回弹不关闭（单击图 320ms 后关闭等双击，点空白立即关）。

### 7.3 鼠标

点卡片任意位置进详情（纯图模式下点图直达大图）、点空白关浮层、卡片悬停轻抬（仅 `hover+fine` 设备）、双击无桌面特化。悬浮球：悬浮展开菜单，「快筛」行悬浮/键盘聚焦浮出「四格」子窗。

---

## 8. JS 架构（app.js）

### 8.1 状态容器

```js
const S = {
  status:'pending', group:'all', range:'all', author:'all', type:'all',
  authorSort:'name', offset:0, total:0,
  posts:[],        // 当前筛选下 created_at 降序，与 feed 子元素顺序一致
  images:[],       // 扁平化图列表（大图流与卡片流共用下标）
  st:null,         // /api/status 缓存
  pollTimer:null, pollFails:0,
  selectMode:false, selected:new Set(),
};
```

### 8.2 渲染函数

`renderCard(p)` / `renderLB()` / `renderDetail(p)` / `renderAuthors(list)` / `renderToolbar()` / `renderBanner()`——全部innerHTML模板 + 事件委托，无每卡监听。`rebuildImages()` 每次数据变动后重建 `S.images` 并 `realignDataI()` 重排 `data-i`（跳过 fading 卡，保证两条动线下标一致）。

### 8.3 标记 = 真乐观 + 串行 POST + 失败回滚

```js
function doMark(sid, status, opts) {
  // 1) 同步：本地状态翻转 → viewHides 判定 → 摘卡(fading/fromLB 直接 remove)
  //    → S.total/-1, S.offset/-1（游标回收防跳帖）→ rebuildImages → updateMore
  //    → offerUndo（6s 撤销）
  // 2) 网络：postChain = postChain.catch(()=>{}).then(() => api(POST status))
  //    —— catch 回写防"一次失败毒死整条链"；连按同键按入队顺序落库
  // 3) 成功：服务端 kept_at 纠正乐观期日期 + 归档 toast；「全部」视图卡片重绘
  // 4) 失败：按 created_at 降序找位重插 + 计数回滚 + 错误 toast
}
```

收藏 `toggleFavorite` 独立：乐观翻转立即重绘（大图同步按钮态），POST 失败回翻；「收藏」页里取消收藏走与标记同款的摘除收口；成功后 `queueStatusRefresh()`（500ms 尾触防抖聚合计数刷新）。

### 8.4 分页与轮询

- 翻页：`IntersectionObserver(rootMargin:600px)` 观察 `.more`，守卫 `S.loading / S.loadError / offset<total`；`load(reset)` 期间再触发 reset 会记 `needReload` 事后补一次；"加载更多"按钮保留为键盘可达入口。
- 抓取轮询：`pollCrawl()` 4s 间隔 `/api/status`，完成/连续 5 次失败即停；完成弹结果 toast 并 `load(true)`。

### 8.5 弹层与焦点

`openModal/closeModal/openLB/closeLB/openDetail/closeDetail` 均记录并归还焦点、锁 `body.overflow`（详情关闭时会检查大图是否仍在 140ms 淡出窗口）；`trapTab` 做焦点圈闭。

---

## 9. 无障碍清单

- `:focus-visible` 全局 2px accent 描边；弹层 Tab 圈闭 + 关闭后焦点归还触发元素。
- 大图 `alt="作者 的微博图"`；toast `role="status" aria-live="polite"`；浮层 `role="dialog" aria-modal`；导航钮有 aria-label。
- `prefers-reduced-motion` 全局降级；`prefers-color-scheme` 跟随（手动选择后以用户为准）。
- 触屏目标 ≥44px；`color-scheme` 令牌让原生控件随主题。
- 已知弱点：动作图标钮无 `aria-pressed`（状态由 on 类的颜色/实心星表达，文字语义在 aria-label）；正文截断仅 `title` 提示。

---

## 10. 已知设计债（诚实清单，未修）

1. 字号阶梯 11 档且含 4 个 `.5` 半档，相邻档区分度低。
2. `--radius` 只服务卡片，实际圆角 5/8/10/12/14/16/999 七档硬编码，无标尺。
3. 双主按钮语言并存：`.tbtn.primary`（墨底胶囊，顶栏/下载所选）vs `.pbtn`（蓝底直角，弹层内），无成文使用规则。
4. 工具栏吸顶 `top:53px` 魔法数（顶栏实高 ~63px，靠 padding 兜住）。
5. 大图深底色（`#d7d7db`/`#7db8ec` 等）为字面量常量而非令牌（大图浮层本就恒深，影响可控）。
6. 成功色三处表达（veil 绿 / keep-ink 绿 / accent 蓝），语义上"保留"用 accent、"收藏"复用 accent 靠 ★ 前缀区分。
7. 长.Session DOM 无上界（筛图单调递减 + 分页 50/页，当前量级可控；虚拟化已评估暂缓）。
