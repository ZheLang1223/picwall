import {api} from './requests.js';
/* 二维码携带 ?code= 时自动填入（管理设备在「手机扫码连接」弹窗里签发的配对码），随即从地址栏抹除以免留存浏览历史 */
const preUrl = new URL(location.href);
const preCode = preUrl.searchParams.get('code');
if (preCode) {
  document.querySelector('#pair-code').value = preCode.toUpperCase();
  document.querySelector('#pair-name').focus();
  preUrl.searchParams.delete('code');
  try { history.replaceState(null, '', preUrl.pathname + (preUrl.search || '') + preUrl.hash); } catch (e) {}
}
const form = document.querySelector('#pair-form');
form.addEventListener('submit', async e => {
  e.preventDefault();
  const button = form.querySelector('button'), error = document.querySelector('#pair-error');
  button.disabled = true; error.textContent = '正在连接…';
  try {
    await api('/api/device/pair', 'POST', {code:document.querySelector('#pair-code').value,
                                        name:document.querySelector('#pair-name').value});
    location.reload();
  } catch (e) { error.textContent = e.message; }
  finally { button.disabled = false; }
});
