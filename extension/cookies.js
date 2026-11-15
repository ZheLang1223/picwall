/* 纯函数辅助（供 popup.js 与单元测试共用）：Weibo Cookie 白名单过滤、明文 LAN 告警判定与权限源格式化。 */
export const WEIBO_COOKIE_ALLOWLIST = new Set([
  'SUB',
  'SUBP',
  'XSRF-TOKEN',
  'SSOLoginState',
  'ALF',
  'SCF',
  'WBPSESS',
]);

export function filterWeiboCookies(cookies = []) {
  const fields = new Map();
  // 同名不同 path 的 Cookie 按最具体路径优先，避免混合出重复 SUB，且只采集白名单鉴权字段
  const sorted = [...cookies].sort((a, b) => (b?.path?.length || 0) - (a?.path?.length || 0));
  for (const c of sorted) {
    if (!c || !WEIBO_COOKIE_ALLOWLIST.has(c.name) || fields.has(c.name)) continue;
    fields.set(c.name, c.value ?? '');
  }
  const cookieHeader = [...fields].map(([name, value]) => `${name}=${value}`).join('; ');
  return { fields, cookieHeader };
}

export function isLoopbackHost(hostname) {
  const host = String(hostname || '').toLowerCase();
  if (host === 'localhost' || host === '[::1]') return true;
  const octets = host.split('.').map(Number);
  return octets.length === 4 && octets.every((n) => Number.isInteger(n) && n >= 0 && n < 256) && octets[0] === 127;
}

export function isInsecureLanHttp(origin) {
  try {
    const url = new URL(origin);
    return url.protocol === 'http:' && !isLoopbackHost(url.hostname);
  } catch (e) {
    return false;
  }
}

export function originPatternFor(origin) {
  const url = new URL(origin);
  return `${url.protocol}//${url.hostname}/*`;
}
