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
  if (logout) {
    /* 两步确认（同取消配对/清空的武装式防误触）：首击只武装不改状态，再击才真退登；
       失焦、Esc 或 3 秒超时自动解除，避免误触把已配对设备踢下线。 */
    let armTimer = 0;
    const disarm = () => {
      clearTimeout(armTimer);
      logout.classList.remove('arming');
      logout.removeAttribute('aria-label');
      logout.textContent = '断开当前设备';
    };
    logout.addEventListener('blur', disarm);
    logout.addEventListener('keydown', (e) => { if (e.key === 'Escape') disarm(); });
    logout.onclick = async () => {
      if (logout.disabled) return;
      if (!logout.classList.contains('arming')) {
        logout.classList.add('arming');
        logout.setAttribute('aria-label', '再点一次确认断开当前设备（需重新配对才能再登录）');
        logout.textContent = '确认断开并退出';
        armTimer = setTimeout(disarm, 3000);
        return;
      }
      disarm(); logout.disabled = true;
      try { await api('/api/device/session','DELETE'); location.reload(); }
      catch(e) { logout.disabled = false; toast(e.message); }
    };
  }
  return { render };
}
