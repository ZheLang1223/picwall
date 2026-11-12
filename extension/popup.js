/* 浏览器已有登录态 → PWALL；服务端校验成功后替换，不保存浏览器密码。 */
import {destination} from './destination.js';
const $ = (s) => document.querySelector(s);
let BASE = "http://localhost:8081";
let TOKEN = '';

async function allowDestination() {
  BASE = destination($('#addr').value.trim() || BASE);
  const url = new URL(BASE);
  const granted = await chrome.permissions.request({origins:[`${url.protocol}//${url.hostname}/*`]});
  if (!granted) throw new Error('需要允许访问填写的 PWALL 地址');
  const saved = await chrome.storage.local.get(['devices']);
  TOKEN = saved.devices?.[BASE] || '';
}

function status(msg, cls) {
  const el = $("#status");
  el.textContent = msg;
  el.className = cls || "";
}

async function api(path, opts) {
  BASE = destination(BASE);
  const r = await fetch(BASE + path, {...opts,
    headers:{...opts?.headers,...(TOKEN ? {Authorization:'Bearer '+TOKEN} : {})},
    signal:AbortSignal.timeout(20000)});
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

async function sendWeibo() {
  const btn = $("#send-weibo");
  btn.disabled = true;
  status("正在读取微博 Cookie…");
  try {
    await allowDestination();
    if (!TOKEN) throw new Error('请先使用管理配对码连接助手');
    const cookies = await chrome.cookies.getAll({ url: "https://weibo.com/" });
    const fields = new Map();
    // 同名不同 path 的 Cookie 按最具体路径优先，避免混合出重复 SUB。
    cookies.sort((a,b)=>b.path.length-a.path.length).forEach(c=>{if(!fields.has(c.name)) fields.set(c.name,c.value);});
    const str = [...fields].map(([name,value]) => `${name}=${value}`).join("; ");
    if (!fields.get("SUB") || !fields.get("XSRF-TOKEN")) {
      status("bad：微博未登录（没有 SUB），请先在本浏览器登录 weibo.com", "bad");
      return;
    }
    const result = await api("/api/settings/cookie", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ cookie: str }),
    });
    await refresh();
    status(result.msg + "\n" + $("#status").textContent, $("#status").className);
  } catch (e) {
    status("发送失败：" + e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function sendX() {
  const btn = $("#send-x");
  btn.disabled = true;
  status("正在读取 X Cookie…");
  try {
    await allowDestination();
    if (!TOKEN) throw new Error('请先使用管理配对码连接助手');
    let cookies = await chrome.cookies.getAll({url:"https://x.com/"});
    if (!cookies.some(c=>c.name === 'auth_token' && c.value))
      cookies = await chrome.cookies.getAll({url:"https://twitter.com/"});
    const auth = cookies.find((c) => c.name === "auth_token");
    if (!auth) {
      status("X 未登录（没有 auth_token），请先在本浏览器登录 x.com", "bad");
      return;
    }
    const result = await api("/api/settings/x-auth", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ auth_token: auth.value, ct0:cookies.find(c=>c.name==='ct0')?.value || '' }),
    });
    await refresh();
    status(result.msg + "\n" + $("#status").textContent, $("#status").className);
  } catch (e) {
    status("发送失败：" + e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function refresh() {
  try {
    const st = await api("/api/status");
    const lines = [];
    const names = {ok:'连接正常', saved:'已保存，待验证', expired:'登录已失效，请重新发送', error:'暂不可访问，可重新检查', unset:'未连接'};
    const wb = st.connections?.weibo || st.cookie;
    const x = st.connections?.x || {state:st.crawl?.x_auth ? 'saved' : 'unset'};
    lines.push('微博：' + (names[wb?.state] || '未连接'));
    lines.push('X：' + (names[x.state] || '未连接'));
    if (st.crawl && st.crawl.x_last_crawl) lines.push("X 上次抓取 " + st.crawl.x_last_crawl.replace("T", " "));
    status(lines.join("\n"), [wb?.state,x.state].some(s=>s==='expired'||s==='error') ? 'bad' : '');
  } catch (e) {
    status(e.message || "PWALL 未连通：请确认已启动（make start）", "bad");
  }
}

$("#send-weibo").addEventListener("click", sendWeibo);
$("#send-x").addEventListener("click", sendX);
$("#addr").addEventListener("change", async () => {
  try {
    BASE = destination($("#addr").value.trim());
    const saved = await chrome.storage.local.get(['devices']);
    TOKEN = saved.devices?.[BASE] || '';
    await chrome.storage.local.set({ pwall: BASE });
    refresh();
  } catch(e) { TOKEN=''; status(e.message,'bad'); }
});

$('#pair').addEventListener('click', async () => {
  try {
    await allowDestination();
    const result = await api('/api/device/pair',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:$('#pair-code').value,name:'Cookie 助手',bearer:true})});
    TOKEN=result.token; $('#pair-code').value='';
    const saved=await chrome.storage.local.get(['devices']);
    await chrome.storage.local.set({devices:{...saved.devices,[BASE]:TOKEN},pwall:BASE});
    status('助手已配对，可发送已登录的来源 Cookie。','ok');
  } catch(e) { status(e.message,'bad'); }
});
chrome.storage.local.get(["pwall",'devices'], ({ pwall,devices }) => {
  try {
    BASE = destination(pwall || BASE); TOKEN=devices?.[BASE] || '';
    $("#addr").value = BASE;
    if (TOKEN) refresh(); else status('请先使用管理配对码连接助手。');
  } catch(e) { status(e.message,'bad'); }
});
