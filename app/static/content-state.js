/* 用户可见的内容范围与画质；不将“处理结束”当作“全部齐全”。 */
export function downloadLabel(pic) {
  if (pic.quality === 'full') return {text:'下载原图', title:'下载原图'};
  if (pic.quality === 'mid') return {text:'下载中图', title:'下载中图；原图仍待补抓'};
  return {text:'下载图片', title:'下载当前图片；旧记录的原图画质未核验'};
}

export function captureGroupText(g) {
  if (g.status === 'pending') return '排队中';
  let text = g.received === undefined ? '' : `接收 ${g.received} 条 · `;
  if (g.accepted !== undefined) text += `处理完成 ${g.accepted} 条 · `;
  text += `新增 ${g.new_posts || 0} 帖 / ${g.new_pics || 0} 图`;
  if (g.skipped) text += ` · 跳过 ${g.skipped} 条`;
  if (g.failed) text += ` · ${g.failed} 条图片未齐`;
  if (g.unprocessed) text += ` · ${g.unprocessed} 条待处理`;
  if (g.status === 'interrupted') text += ' · 已中断，可重试';
  else if (g.error) text += ' · ' + g.error;
  else if (g.status === 'error') text += ' · 失败，请检查登录或网络后重试';
  if (g.coverage === 'page_limit') text += ' · 达到翻页上限，日期范围未覆盖完';
  if (g.coverage === 'browser_batch') text += ' · 仅核对此次导入批次';
  return text;
}

export function captureTitle(result) {
  return {success:'完成',incomplete:'范围未覆盖完',partial:'部分完成',failed:'未完成',interrupted:'已中断'}[result.outcome]
    || (result.error ? '未完成' : '完成');
}

export function emptyState(s) {
  const filtered = s.group !== 'all' || s.range !== 'all' || s.author !== 'all' ||
                   s.source !== 'all' || s.hasCmts !== 'all' || !!(s.tags && s.tags !== 'all') ||
                   !!s.searchText;
  if (filtered) return {text:'没有符合这些条件的图片，可调整或清空筛选条件。'};
  if (s.status === 'favorites') return {text:'还没有收藏，看图时点「收藏」或按 M。'};
  if (s.status === 'kept') return {text:'还没有保留的图片。'};
  if (s.status === 'discarded') return {text:'没有移入「不要」的图片。'};
  const count = s.st?.counts || {};
  if (count.kept || count.discarded) return {text:'新增已筛完；悬浮球「快筛」或 Shift+Q 继续分拣，也可切到「保留」「全部」查看已有图片。'};
  if (!s.st) return {text:'当前没有图片。'};
  if (s.st.cookie?.state === 'unset' || s.st.cookie?.state === 'expired')
    return {text:s.isAdmin ? '先连接微博并校验，再添加分组开始采集。' : '请用管理设备连接微博并添加分组。',action:s.isAdmin ? 'connection' : undefined};
  if (s.st.cookie?.state !== 'ok')
    return {text:s.isAdmin ? '先校验微博连接，再开始采集。' : '请用管理设备校验微博连接。',action:s.isAdmin ? 'connection' : undefined};
  if (!s.st.groups?.some(g => g.active))
    return {text:s.isAdmin ? '微博已连接，添加一个分组开始采集。' : '请用管理设备添加分组。',action:s.isAdmin ? 'groups' : undefined};
  return {text:'还没有采集到图片。',action:s.isAdmin ? 'crawl' : undefined};
}
