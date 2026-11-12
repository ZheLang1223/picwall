"""Safari Cookie 库解析与 X 凭据提取（tools/safari_x_cookie）。"""
import struct
import tempfile
import unittest
from pathlib import Path

from tools.safari_x_cookie import _parse_classic, extract_x, read_safari_cookies

# Mac 纪元（2001-01-01）起算的秒数：1e9 ≈ 2032-09，作「未过期」样本
_FUTURE = 1_000_000_000


def _record(domain: str, name: str, value: str, path: str = "/",
            expiry: int = _FUTURE) -> bytes:
    d, n, p, v = domain.encode(), name.encode(), path.encode(), value.encode()
    d_off = 44                      # 记录头 7×uint32 + 2×uint64
    n_off = d_off + len(d) + 1
    p_off = n_off + len(n) + 1
    v_off = p_off + len(p) + 1
    pool = d + b"\x00" + n + b"\x00" + p + b"\x00" + v + b"\x00"
    head = struct.pack(">IIIIIIIQQ", v_off + len(v) + 1, 0, 0,
                       d_off, n_off, p_off, v_off, expiry, expiry)
    return head + pool


def _page(records: list[bytes]) -> bytes:
    head_len = 8 + 4 * len(records) + 4
    offs, blob = [], b""
    for r in records:
        offs.append(head_len + len(blob))
        blob += r
    return (struct.pack(">II", 0, len(records))
            + b"".join(struct.pack(">I", o) for o in offs)
            + struct.pack(">I", 0) + blob)


def _file(pages: list[bytes]) -> bytes:
    out = b"cook" + struct.pack(">I", len(pages))
    off = 8 + len(pages) * 8
    for p in pages:
        out += struct.pack(">II", len(p), off)
        off += len(p)
    return out + b"".join(pages)


def _file_split(pages: list[bytes]) -> bytes:
    """分列排布：N 个 size 连排 + N 个 offset 连排（老版 Safari 文件的常见形态）。"""
    n = len(pages)
    out = b"cook" + struct.pack(">I", n)
    out += b"".join(struct.pack(">I", len(p)) for p in pages)
    offs, o = [], 8 + 8 * n
    for p in pages:
        offs.append(o)
        o += len(p)
    return out + b"".join(struct.pack(">I", x) for x in offs) + b"".join(pages)


class SafariCookieTest(unittest.TestCase):
    def _write(self, blob: bytes) -> Path:
        f = Path(tempfile.mkdtemp()) / "Cookies.binarycookies"
        f.write_bytes(blob)
        return f

    def test_new_format_pool_scan(self):
        """新格式（macOS 15）：四连字符串池与页框架无关，全文扫描直接命中。"""
        blob = (b"cook" + struct.pack(">I", 98) + b"\x00" * 392
                + b"junk\x00" + b"A.x.com\x00auth_token\x00/\x00tok1234567890\x00"
                + b"A.x.com\x00ct0\x00/\x00csrfabcdef\x00")
        cookies = read_safari_cookies(self._write(blob))
        self.assertEqual(extract_x(cookies),
                         {"auth_token": "tok1234567890", "ct0": "csrfabcdef"})

    def test_reads_pages_and_picks_x_credentials(self):
        f = self._write(_file([
            _page([_record(".x.com", "auth_token", "tok1234567890"),
                   _record(".x.com", "ct0", "csrfabcdef"),
                   _record(".google.com", "NID", "noisy")]),
            _page([_record(".twitter.com", "auth_token", "legacytok")]),
        ]))
        cookies = read_safari_cookies(f)
        self.assertEqual({c["name"] for c in cookies}, {"auth_token", "ct0", "NID"})
        self.assertEqual(extract_x(cookies),
                         {"auth_token": "tok1234567890", "ct0": "csrfabcdef"})

    def test_classic_expired_dropped_and_session_cookie_kept(self):
        """经典解析按到期时间过滤；池扫描不做此判断（死活交给图片墙实连验证）。"""
        f = self._write(_file([_page([
            _record(".x.com", "auth_token", "old", expiry=1),   # 2001 年初，已过期
            _record(".x.com", "ct0", "sess", expiry=0),         # 会话 Cookie（exp=0），保留
        ])]))
        self.assertEqual(extract_x(_parse_classic(f.read_bytes())),
                         {"auth_token": "", "ct0": "sess"})

    def test_no_x_session_returns_empty(self):
        f = self._write(_file([_page([_record(".google.com", "NID", "x")])]))
        self.assertEqual(extract_x(read_safari_cookies(f)),
                         {"auth_token": "", "ct0": ""})

    def test_directory_offsets_honored_with_gap(self):
        """真实文件的页体与目录之间可能有缝隙：必须按目录绝对偏移切页。"""
        p0 = _page([_record(".x.com", "auth_token", "tok1234567890")])
        p1 = _page([_record(".x.com", "ct0", "csrf")])
        gap = b"\x00" * 4
        base = 8 + 16 + len(gap)
        out = (b"cook" + struct.pack(">I", 2)
               + struct.pack(">II", len(p0), base)
               + struct.pack(">II", len(p1), base + len(p0))
               + gap + p0 + p1)
        cookies = read_safari_cookies(self._write(out))
        self.assertEqual(extract_x(cookies),
                         {"auth_token": "tok1234567890", "ct0": "csrf"})

    def test_truncated_record_skipped(self):
        """截断/越界的杂项记录跳过不炸，同页的正常记录照常解析。"""
        junk = struct.pack(">I", 6) + b"garbage"      # 声称 6 字节、实际不足记录头 44
        ok = _record(".x.com", "auth_token", "tok1234567890")
        page = _page([junk, ok])
        cookies = read_safari_cookies(self._write(_file([page])))
        self.assertEqual([c["name"] for c in cookies], ["auth_token"])

    def test_corrupt_page_skipped_next_page_parsed(self):
        """声称条数超出页容量的损坏页整页跳过，后续正常页照常解析。"""
        bad = struct.pack(">II", 0, 9999) + struct.pack(">I", 0)   # 声明 9999 条的空壳页
        good = _page([_record(".x.com", "auth_token", "tok1234567890")])
        cookies = read_safari_cookies(self._write(_file([bad, good])))
        self.assertEqual([c["name"] for c in cookies], ["auth_token"])

    def test_split_layout_detected(self):
        """分列排布（size 连排 + offset 连排）由自检识别并正确解析。"""
        f = self._write(_file_split([
            _page([_record(".x.com", "auth_token", "tok1234567890")]),
            _page([_record(".x.com", "ct0", "csrfabcdef")]),
        ]))
        self.assertEqual(extract_x(read_safari_cookies(f)),
                         {"auth_token": "tok1234567890", "ct0": "csrfabcdef"})


if __name__ == "__main__":
    unittest.main()
