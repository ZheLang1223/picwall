export function destination(value) {
  const url = new URL(value);
  const host = url.hostname.toLowerCase();
  const octets = host.split('.').map(Number);
  const v4 = octets.length === 4 && octets.every(n => Number.isInteger(n) && n >= 0 && n < 256);
  const privateV4 = v4 && (octets[0] === 127 || octets[0] === 10 ||
    (octets[0] === 192 && octets[1] === 168) || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31));
  const privateV6 = host === '[::1]' || /^\[(fc|fd)[0-9a-f]{2}:/.test(host);
  if (!['http:','https:'].includes(url.protocol) || url.username || url.password ||
      url.pathname !== '/' || url.search || url.hash ||
      !(host === 'localhost' || privateV4 || privateV6)) {
    throw new Error('请填写 localhost 或私网 IP 的 PWALL 地址');
  }
  return url.origin;
}
