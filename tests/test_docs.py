"""文档健康检查：死链与锚点、frontmatter 完整性、环境变量契约。

把 README §8 更新规则的机器可查部分变成 make picwall-test 的强制约束：
- 规则 7（退役/移动文档必须回改引用）→ 死链检查（2026-10-13 曾留下两处死链），
  含 `文件.md#节标题` 锚链接的锚点存在性（锚 slug 按 GitHub 约定计算）；
- 规则 3（配置面变更同步文档）→ 环境变量契约：compose environment 是白名单，
  漏声明则容器看不见、配了等于没配（2026-10-16 实锤过），文档与声明必须同步。
"""
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# 容器内生效的环境变量全集（README §3 配置表与 docker-compose.yml 必须与这里一致；
# 宿主侧的 PICWALL_BLOB_STORE 不在 compose，另行要求 README 记载）
COMPOSE_VARS = {
    "TZ", "WB_COOKIE", "CRAWL_TIME", "CRAWL_MAX_PAGES", "CRAWL_BACKFILL_DAYS",
    "CRAWL_BACKFILL_MAX_PAGES", "PURGE_DAYS", "X_CRAWL_INTERVAL_HOURS",
    "X_PROXY_URL", "PICWALL_LAN_IP", "PICWALL_ALLOWED_HOSTS",
    "PWALL_PAIR_CODE", "PWALL_PAIR_ROLE", "PICWALL_MAX_IMAGE_PIXELS",
    "PICWALL_MAX_VIDEO_BYTES",
}
HOST_VARS = {"PICWALL_BLOB_STORE"}


class DocLinksTest(unittest.TestCase):
    @staticmethod
    def _github_slug(text: str) -> str:
        # GitHub 锚约定：小写、去标点（CJK 保留）、空格转连字符（如「2. 分拣（快筛）」→ 2-分拣快筛）
        s = "".join(ch for ch in text.strip().lower() if ch.isalnum() or ch in " -_")
        return s.replace(" ", "-")

    def _link_problems(self, md: Path, url: str):
        file_part, _, anchor = url.partition("#")
        if not file_part:
            return None
        file_part = re.sub(r":\d+$", "", file_part)  # 剥行号后缀（db.py:209、/abs/Makefile:93）
        path = Path(file_part)
        resolved = path.resolve() if path.is_absolute() else (md.parent / path).resolve()
        if not resolved.exists():
            return f"{md.relative_to(PROJECT_ROOT)} -> {url}（文件不存在）"
        if anchor and resolved.suffix == ".md":
            headings = {self._github_slug(m.group(2))
                        for m in HEADING_RE.finditer(resolved.read_text(encoding="utf-8"))}
            if anchor not in headings:
                return f"{md.relative_to(PROJECT_ROOT)} -> {url}（锚点不存在）"
        return None

    def test_relative_links_resolve(self):
        broken = []
        for md in sorted(PROJECT_ROOT.rglob("*.md")):
            for target in LINK_RE.findall(md.read_text(encoding="utf-8")):
                url = target.strip()
                if url.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                problem = self._link_problems(md, url)
                if problem:
                    broken.append(problem)
        self.assertEqual(
            broken, [],
            "死链/坏锚点（退役/移动文档后未回改引用）：\n" + "\n".join(broken),
        )


class DocFrontmatterTest(unittest.TestCase):
    """docs/ 正式文档统一 frontmatter 三件套（与项目文档惯例同构）。"""

    def test_docs_have_title_description_date(self):
        bad = []
        for md in sorted(DOCS_DIR.glob("*.md")):
            fields = dict(re.findall(r"^(title|description|date):\s*(.+)$",
                                     md.read_text(encoding="utf-8"), re.MULTILINE))
            for field in ("title", "description", "date"):
                if not fields.get(field, "").strip():
                    bad.append(f"{md.name}: 缺 {field}")
            if fields.get("date") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["date"].strip()):
                bad.append(f"{md.name}: date 非 YYYY-MM-DD")
        self.assertEqual(bad, [], "docs/*.md frontmatter 不合规：\n" + "\n".join(bad))


class EnvContractTest(unittest.TestCase):
    """README §3 配置表 ↔ docker-compose environment 白名单，双向钉死。"""

    def test_compose_declares_exactly_the_contract(self):
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_block = compose.split("environment:", 1)[1].split("volumes:", 1)[0]
        declared = set(re.findall(r"^\s+([A-Z][A-Z0-9_]*):", env_block, re.MULTILINE))
        self.assertEqual(
            declared, COMPOSE_VARS,
            "compose environment 与契约不符（多声明的删掉、新变量必须声明，否则 .env 配置看不见）",
        )

    def test_readme_documents_all_env_vars(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        missing = [v for v in sorted(COMPOSE_VARS | HOST_VARS) if f"`{v}`" not in readme]
        self.assertEqual(missing, [], "README 未记载的环境变量：\n" + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
