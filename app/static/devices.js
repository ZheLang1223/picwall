/* 设备管理(设置窗口「设备管理」分区):settings.js 的分区激活钩子调 render();
   左栏副标随设备身份展示当前角色。
   esc 为本文件自带：模块化拆分后各文件不共享 app.js 的模块内函数，这里单独定义一份。 */
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export function initDevices({$,api,toast}) {
  const role = document.documentElement.dataset.deviceRole;
  const localOwner = document.documentElement.dataset.localOwner === 'true';
  const sub = $('#nav-devices-sub');
  if (sub) sub.textContent = localOwner ? '本机' : ({admin:'管理',editor:'分拣',viewer:'只读'}[role] || '已配对');
  async function render() {
    const box = $('#device-list');
    box.textContent = role === 'admin' ? '正在读取…' : '当前设备可以' + (role === 'viewer' ? '浏览和下载。' : '分拣、浏览和下载。');
    if (role !== 'admin') return;
    try {
      const [result, current] = await Promise.all([api('/api/devices'),api('/api/device/session')]);
      box.innerHTML = (localOwner ? '<p class="hint">当前为运行 PWALL 的本机，无需配对。</p>' : '') + result.devices.map(d => `<div class="grow"><span>${esc(d.name)} · ${esc({admin:'管理',editor:'分拣',viewer:'只读',collector:'采集'}[d.role])}${d.id === current.id ? '（当前）' : ''}</span><button type="button" data-device="${esc(d.id)}">取消配对</button></div>`).join('');
      box.querySelectorAll('[data-device]').forEach(b => b.onclick = async () => {
        if (b.disabled) return; b.disabled = true;
        try {
          await api('/api/devices/' + encodeURIComponent(b.dataset.device), 'DELETE');
          if (b.dataset.device === current.id) location.reload();
          else b.closest('.grow').remove();
        } catch(e) { b.disabled = false; toast(e.message); }
      });
    } catch(e) { console.error('设备列表渲染失败:', e); box.textContent = '设备列表加载失败：' + e.message; }
  }
  const logout = $('#device-logout');
  if (logout) logout.onclick = async () => {
    if (logout.disabled) return; logout.disabled = true;
    try { await api('/api/device/session','DELETE'); location.reload(); }
    catch(e) { logout.disabled = false; toast(e.message); }
  };
  return { render };
}
