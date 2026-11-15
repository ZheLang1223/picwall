"""一次性迁移脚本：合并导航栏（PWALL 单栏 + 视图筛选面板 + 设置右移）。
跑完即完成使命，保留在仓库里作为这次改版的记录。"""


def main() -> None:
    p = 'app/templates/index.html'
    with open(p) as f:
        s = f.read()

    # ---------- 1) 顶栏整体替换 ----------
    gear = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06'
            'a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21'
            'a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06'
            'a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3'
            'a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06'
            'a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3'
            'a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06'
            'a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21'
            'a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>')

    qr_ic = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
             '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
             '<rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3z"/>'
             '<path d="M21 14v3M14 21h3M21 21h.01"/></svg>')
    theme_ic = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>')
    groups_ic = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                 '<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/></svg>')
    crawl_ic = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>')
    batch_ic = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/>'
                '<line x1="12" y1="15" x2="12" y2="3"/></svg>')
    cookie_ic = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                 '<circle cx="12" cy="12" r="9"/><path d="M8.5 8.5h.01M15.5 8.5h.01M12 12h.01'
                 'M8.5 15h.01M15 14.5h.01M12 16.5h.01"/></svg>')

    new_header = f'''<header class="topbar">
  <div class="brand">PWALL</div>
  <div class="nav-tools">
    <div class="viewwrap">
      <button id="btn-view" class="viewbtn" aria-expanded="false" aria-controls="view-panel">
        <span id="view-label">视图</span>
        <span id="view-badge" class="vbadge" hidden></span>
        <svg class="caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
    </div>
    <button id="btn-select" class="tbtn">选择</button>
    <button id="btn-batch" class="tbtn" title="选择范围打包下载">打包</button>
  </div>
  <div class="settings-wrap">
    <button id="btn-settings" class="tbtn ic" title="设置" aria-label="设置">
      {gear}
    </button>
    <div id="settings-card" class="settings-card" hidden>
      <div class="modal-head" style="margin-bottom:6px"><b>设置</b></div>
      <button class="setrow" id="set-qr">{qr_ic}<span>手机扫码连接</span></button>
      <button class="setrow" id="set-theme">{theme_ic}<span>深色 / 浅色模式</span></button>
      <button class="setrow" id="set-groups">{groups_ic}<span>管理分组</span></button>
      <button class="setrow" id="set-crawl">{crawl_ic}<span>自动抓取</span></button>
      <button class="setrow" id="set-batch">{batch_ic}<span>批量下载</span></button>
      <button class="setrow" id="set-cookie">{cookie_ic}<span>Cookie</span></button>
    </div>
  </div>
</header>

<div id="view-panel" class="view-panel" hidden>
  <div class="f-row"><span class="f-label">状态</span><div class="f-chips" id="f-status"></div></div>
  <div class="f-row"><span class="f-label">分组</span><div class="f-chips" id="f-group"></div></div>
  <div class="f-row"><span class="f-label">时间</span><div class="f-chips" id="f-range">
    <button class="fchip" data-v="all">全部</button>
    <button class="fchip" data-v="today">今天</button>
    <button class="fchip" data-v="yesterday">昨天</button>
    <button class="fchip" data-v="7d">近 7 天</button>
  </div></div>
  <div class="f-row"><span class="f-label">类型</span><div class="f-chips" id="f-type">
    <button class="fchip" data-v="all">全部</button>
    <button class="fchip" data-v="orig">原创</button>
    <button class="fchip" data-v="rt">转发</button>
  </div></div>
  <p class="f-hint">「收藏」是独立标记：勾选后表示在当前状态里只看收藏的。</p>
</div>

<div id="banner"></div>'''

    # ---------- 1) 顶栏整体替换 ----------
    a = s.index('<header class="topbar">')
    b = s.index('</header>') + len('</header>')
    s = s[:a] + new_header + s[b:]

    # ---------- 2) 删除旧 toolbar 块（配对 div 层级） ----------
    t = s.index('<div class="toolbar">')
    depth = 0
    i = t
    while i < len(s):
        mo = s.find('<div', i)
        mc = s.find('</div>', i)
        if mc == -1 and mo == -1:
            break
        if mo != -1 and (mc == -1 or mo < mc):
            depth += 1
            i = mo + 4
        else:
            depth -= 1
            i = mc + 6
            if depth == 0:
                break
    s = s[:t] + s[i:]

    # ---------- 3) 词表：待处理 → 新增（按钮标签在 HTML 已删，这里只留 JS 侧） ----------
    with open(p, 'w') as f:
        f.write(s)
    print('index.html 顶栏合并 + 旧工具栏移除 ✓')


if __name__ == "__main__":
    main()
