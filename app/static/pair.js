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
let pairing = false;
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (pairing) return;   // 请求在途或已连接待跳转：忽略重复提交（禁用按钮拦不住输入框回车）
  pairing = true;
  const button = form.querySelector('button'), error = document.querySelector('#pair-error');
  button.disabled = true; error.textContent = '正在连接…';
  try {
    await api('/api/device/pair', 'POST', {code:document.querySelector('#pair-code').value,
                                        name:document.querySelector('#pair-name').value});
    /* 成功：保持禁用直到 reload 生效（原来的 finally 会在跳转前重新点亮按钮，可被再次提交） */
    error.textContent = '已连接，正在进入…';
    location.reload();
  } catch (err) {
    /* 失败：恢复按钮并展示原因，焦点与全选放回配对码输入框，改完即可直接重试 */
    error.textContent = err.message;
    button.disabled = false;
    const code = document.querySelector('#pair-code');
    code.focus(); code.select();
    pairing = false;
  }
});
