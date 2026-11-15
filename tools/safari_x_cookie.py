"""从本机 Safari Cookie 库提取 x.com 登录态（auth_token/ct0），发给图片墙并当场验证。

Safari 的 Cookie 库受 macOS TCC 保护：跑本脚本的终端需要「完全磁盘访问」权限
（系统设置 → 隐私与安全性 → 完全磁盘访问 → 勾选终端，重开终端再跑）。
脚本不回显 Cookie 明文（--show 除外），提取后 POST 给本机图片墙的
/api/settings/x-auth——服务端保存时会向 X 发起一次验证，Safari 里那个会话
是活是死当场见分晓（过期时旧凭据保留、连接状态记为 expired）。

记录格式（2026-10 在 macOS 15.6 实测定型）：文件以 cook 魔数 + 页数 + 页大小表开头，
但每条 Cookie 是「域名\0 名字\0 路径\0 值\0」四连 C 字符串池（域名前带一个 'A'
类型标记字节），与页框架无关——全文扫描即可；老系统的经典页目录解析保留作兜底。

仓库根目录用法：
    make picwall-safari-cookie                  # 提取并保存（顺带验证）
    make picwall-safari-cookie ARGS=--dry-run   # 只看 Safari 里有没有，不发送
    make picwall-safari-cookie ARGS="--dry-run --debug"   # 取不到时看结构诊断
"""
import argparse
import ipaddress
import json
import re
import struct
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

SAFARI_COOKIES = (Path.home() / "Library/Containers/com.apple.Safari"
                  / "Data/Library/Cookies/Cookies.binarycookies")
PICWALL_URL = "http://localhost:8081"
# 域名按「本域或子域」匹配；x.com 之外留 twitter.com 旧档兼容
X_DOMAINS = (".x.com", ".twitter.com")
MAX_COOKIE_FILE_BYTES = 32 * 1024 * 1024
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# 四连字符串池：域名\0 名字\0 路径(/)\0 值\0，全部可打印 ASCII
_POOL = re.compile(rb"([\x20-\x7e]{2,253})\x00([\x20-\x7e]{1,64})\x00/\x00([\x20-\x7e]{1,4600}?)\x00")


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _str_at(rec: bytes, off: int) -> str:
    end = rec.index(b"\x00", off)
    return rec[off:end].decode("utf-8", "replace")


def _scan_pools(buf: bytes) -> list[dict]:
    """全文扫描四连字符串池；域名前的 'A' 是类型标记字节，剥掉。

    过期与否不在此判断：Safari 会把过期 Cookie 清出文件，死活交给
    图片墙保存时的实连验证。
    """
    out: list[dict] = []
    for m in _POOL.finditer(buf):
        dom = m.group(1).decode("ascii", "replace")
        if dom.startswith("A") and len(dom) > 2 and (dom[1] == "." or dom[1].islower()):
            dom = dom[1:]
        out.append({"domain": dom, "name": m.group(2).decode("ascii", "replace"),
                    "path": "/", "value": m.group(3).decode("ascii", "replace")})
    return out


def _detect_candidates(buf: bytes) -> dict[str, list[int]]:
    """经典格式的页目录有两种排布，返回各自通过自检的页数候选。

    split：N 个 size 连排，后跟 N 个 offset 连排；pairs：N 组 (size, offset) 对。
    自检条件：每页 ≥12 字节（页头+页脚）、页起点在目录区之后、页不越过文件尾。
    """
    out: dict[str, list[int]] = {"split": [], "pairs": []}
    total_len = len(buf)
    cap = max(0, (total_len - 8) // 8)

    # pairs 布局各槽位只依赖 k，仅 o >= 8 + 8*n 随 n 收缩，可单遍 O(N) 自检
    pairs: list[int] = []
    min_off = total_len + 1
    for k in range(cap):
        s = _u32(buf, 8 + 8 * k)
        o = _u32(buf, 8 + 8 * k + 4)
        if s < 12 or o + s > total_len:
            break
        min_off = min(min_off, o)
        n = k + 1
        if min_off < 8 + 8 * n:
            break
        pairs.append(n)
    pairs.reverse()
    out["pairs"] = pairs

    # split 布局先用前缀 s >= 12 截断最大可能页数，再降序验证（_parse_classic 只取前 4）
    split_cap = 0
    while split_cap < cap:
        s = _u32(buf, 8 + 4 * split_cap)
        if s < 12 or s > total_len:
            break
        split_cap += 1
    probes = 0
    for n in range(split_cap, 0, -1):
        if len(out["split"]) >= 16 or probes >= 200_000:
            break
        if _u32(buf, 8 + 4 * n) < 8 + 8 * n or _u32(buf, 8 + 4 * (2 * n - 1)) < 8 + 8 * n:
            probes += 1
            continue
        ok = True
        for k in range(n):
            probes += 1
            s = _u32(buf, 8 + 4 * k)
            o = _u32(buf, 8 + 4 * (n + k))
            if o < 8 + 8 * n or o + s > total_len:
                ok = False
                break
        if ok:
            out["split"].append(n)
    return out


def _parse_page(page: bytes) -> list[dict]:
    """经典格式单页 → [{domain, name, path, value, expires}]；损坏页与杂项记录静默跳过。"""
    out: list[dict] = []
    if len(page) < 12:
        return out
    count = _u32(page, 4)
    if count > max(0, (len(page) - 12) // 44):      # 每条记录至少 44 字节
        return out
    ro_min = 12 + 4 * count                         # 记录紧跟偏移表与页脚之后
    now = datetime.now(timezone.utc)
    for i in range(count):
        if 8 + i * 4 + 4 > len(page):
            break
        ro = _u32(page, 8 + i * 4)
        if not ro_min <= ro < len(page):
            continue
        rec = page[ro:]
        try:
            (exp,) = struct.unpack_from(">Q", rec, 28)
            if exp and _MAC_EPOCH + timedelta(seconds=exp) <= now:
                continue
            out.append({
                "domain": _str_at(rec, _u32(rec, 12)),
                "name": _str_at(rec, _u32(rec, 16)),
                "path": _str_at(rec, _u32(rec, 20)),
                "value": _str_at(rec, _u32(rec, 24)),
                "expires": exp,
            })
        except (struct.error, ValueError, OverflowError):
            continue    # 截断/越界/垃圾时间的杂项记录：跳过
    return out


def _parse_classic(buf: bytes, debug: bool = False) -> list[dict]:
    """老系统经典格式：页目录排布与页数由文件自证，全部候选解析取产出最多的一组。"""
    cands = _detect_candidates(buf)
    if debug:
        print(f"经典格式自检候选 {cands}", file=sys.stderr)
    best: list[dict] = []
    for layout in ("split", "pairs"):
        for n in sorted(cands[layout], reverse=True)[:4]:
            cookies: list[dict] = []
            for k in range(n):
                s = _u32(buf, 8 + 4 * k if layout == "split" else 8 + 8 * k)
                o = (_u32(buf, 8 + 4 * (n + k)) if layout == "split"
                     else _u32(buf, 8 + 8 * k + 4))
                cookies += _parse_page(buf[o:o + s])
            if debug:
                print(f"候选 {layout} n={n}: 解析 {len(cookies)} 条", file=sys.stderr)
            if len(cookies) > len(best):
                best = cookies
    return best


def _inspect_dump(buf: bytes) -> None:
    """所有解析路径落空时的结构体检：只输出格式线索，不含 Cookie 值。"""
    print("—— 结构体检（无 Cookie 值）——", file=sys.stderr)
    print("前 64 字节：" + buf[:64].hex(), file=sys.stderr)
    for sig, name in {b"SQLite format 3": "SQLite", b"bplist00": "bplist",
                      b"bvx-": "lzfse", b"bvx2": "lzfse", b"\x78\x9c": "zlib",
                      b"PK\x03\x04": "zip", b"CREATE TABLE": "SQL"}.items():
        pos = buf.find(sig)
        if pos >= 0:
            print(f"签名 {name} @ 偏移 {pos}", file=sys.stderr)


def read_safari_cookies(path: Path, debug: bool = False) -> list[dict]:
    """解析 Safari Cookie 库 → [{domain, name, path, value}]。

    新格式四连字符串池全文扫描为主路径；老系统经典页目录解析兜底；
    两者都落空时（--debug）输出结构体检供人工定格式。
    """
    p = Path(path)
    with p.open("rb") as f:
        buf = f.read(MAX_COOKIE_FILE_BYTES + 1)
    if len(buf) > MAX_COOKIE_FILE_BYTES:
        raise ValueError(f"{path} 超过最大读取上限（{MAX_COOKIE_FILE_BYTES} 字节）")
    if buf[:4] != b"cook":
        raise ValueError(f"{path} 不是 Safari Cookie 库（文件头不符）")
    cookies = _scan_pools(buf)
    if cookies:
        if debug:
            print(f"新格式字符串池扫描：{len(cookies)} 条", file=sys.stderr)
        return cookies
    cookies = _parse_classic(buf, debug)
    if not cookies and debug:
        _inspect_dump(buf)
    return cookies


def _domain_priority(domain: str) -> int:
    d = domain.lower()
    if d == "x.com" or d == ".x.com" or d.endswith(".x.com"):
        return 2
    if d == "twitter.com" or d == ".twitter.com" or d.endswith(".twitter.com"):
        return 1
    return 0


def _is_x_domain(domain: str) -> bool:
    return _domain_priority(domain) > 0


def extract_x(cookies: list[dict]) -> dict:
    """挑出 X 域下的 auth_token 与 ct0（优先 .x.com 及较晚过期时间）；缺失字段为空串。"""
    best: dict[str, tuple[tuple[int, int, int], str]] = {}
    for idx, c in enumerate(cookies):
        name = c.get("name", "")
        val = c.get("value", "")
        if name not in ("auth_token", "ct0") or not val:
            continue
        prio = _domain_priority(c.get("domain", ""))
        if prio == 0:
            continue
        exp = int(c.get("expires") or 0)
        rank = (prio, exp, idx)
        if name not in best or rank > best[name][0]:
            best[name] = (rank, val)
    return {
        "auth_token": best["auth_token"][1] if "auth_token" in best else "",
        "ct0": best["ct0"][1] if "ct0" in best else "",
    }


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_picwall_url(url: str, allow_insecure_lan: bool = False, warn: bool = True) -> str:
    """校验目标服务 URL：只放行 https:// 或本机回环 http://；非回环 http:// 需显式授权。"""
    base = url.strip().rstrip("/")
    sp = urlsplit(base)
    host = sp.hostname or ""
    if sp.scheme not in ("http", "https") or not sp.netloc or not host:
        raise ValueError(f"非法的目标服务地址：{url!r}（仅支持 https:// 或 http://localhost）")
    if sp.scheme == "http" and not _is_loopback_host(host):
        if not allow_insecure_lan:
            raise ValueError(
                f"拒绝通过明文 HTTP 向非回环主机发送 X 会话凭据：{base}；"
                "请改用 https:// 或显式加 --allow-insecure-lan"
            )
        if warn:
            print(f"警告：正通过未加密 HTTP 向局域网主机 {base} 发送 X 会话凭据", file=sys.stderr)
    return base


def send(picwall: str, auth_token: str, ct0: str, allow_insecure_lan: bool = False) -> dict:
    """POST /api/settings/x-auth；带同源 Origin 头过门禁（本机回环即 admin）。"""
    base = validate_picwall_url(picwall, allow_insecure_lan=allow_insecure_lan)
    sp = urlsplit(base)
    body = json.dumps({"auth_token": auth_token, "ct0": ct0}).encode()
    req = urllib.request.Request(
        base + "/api/settings/x-auth", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Origin": f"{sp.scheme}://{sp.netloc}"})
    with urllib.request.urlopen(req, timeout=90) as r:   # 服务端 verify 会实连 X，放宽些
        return json.loads(r.read())


def _mask(v: str, show: bool) -> str:
    return v if show else "…" + v[-4:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从 Safari 提取 x.com 登录态发给图片墙")
    ap.add_argument("--url", default=PICWALL_URL, help="图片墙本机入口（默认 %(default)s）")
    ap.add_argument("--file", type=Path, default=SAFARI_COOKIES,
                    help="Cookie 库路径（默认 Safari；测试可指向合成文件）")
    ap.add_argument("--dry-run", action="store_true", help="只解析不发送")
    ap.add_argument("--show", action="store_true", help="打印明文（默认只显示尾 4 位）")
    ap.add_argument("--debug", action="store_true", help="输出解析诊断（stderr）")
    ap.add_argument("--allow-insecure-lan", action="store_true",
                    help="允许通过明文 HTTP 向非回环地址发送（有窃听风险）")
    args = ap.parse_args(argv)

    if not args.dry_run:
        try:
            validate_picwall_url(args.url, allow_insecure_lan=args.allow_insecure_lan, warn=False)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1

    try:
        cookies = read_safari_cookies(args.file, debug=args.debug)
    except PermissionError:
        print("读不了 Safari Cookie 库（macOS 隐私保护拦截）。", file=sys.stderr)
        print("系统设置 → 隐私与安全性 → 完全磁盘访问 → 勾选你正在用的终端，重开终端再跑。",
              file=sys.stderr)
        return 2
    found = extract_x(cookies)
    if not found["auth_token"]:
        print("Safari Cookie 库里没有 x.com 的 auth_token——Safari 当前没有 X 登录会话。",
              file=sys.stderr)
        print("在 Safari 登录 x.com 后重跑；若会话确实死了，改走第三方数据 API 路线。",
              file=sys.stderr)
        return 3
    print(f"共解析 {len(cookies)} 条 Cookie；"
          f"auth_token {_mask(found['auth_token'], args.show)}"
          f"　ct0 {'有' if found['ct0'] else '无'}")
    if args.dry_run:
        print("dry-run：未发送。")
        return 0
    try:
        r = send(args.url, **found, allow_insecure_lan=args.allow_insecure_lan)
    except (urllib.error.URLError, OSError) as e:
        print(f"连不上图片墙（{args.url}）：{e}", file=sys.stderr)
        print("先 make picwall 拉起服务；或 --show 取明文后从网页「Cookie」入口手动粘贴。",
              file=sys.stderr)
        return 4
    print(r.get("msg") or ("已保存" if r.get("ok") else "失败"))
    return 0 if r.get("ok") and r.get("state") == "ok" else 5


if __name__ == "__main__":
    sys.exit(main())
