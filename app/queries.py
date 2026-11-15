"""列表与导出的共同筛选语义。"""

from datetime import datetime, timedelta

from . import config

_SEARCH_SCOPES = frozenset({"all", "author", "tag", "text", "comment"})


def parse_search_query(q: str, q_scope: str = "all") -> tuple[str, str]:
    """解析搜索范围与前缀（@ 博主 / # 标签 / // 或 c: 评论），剥离前缀后再 strip。"""
    scope = (q_scope or "all").strip().lower()
    if scope not in _SEARCH_SCOPES:
        scope = "all"
    text = (q or "").strip()
    if not text:
        return scope, ""
    if scope == "all":
        if text.startswith(("@", "＠")):
            scope = "author"
            text = text.lstrip("@＠")
        elif text.startswith(("#", "＃")):
            scope = "tag"
            text = text.lstrip("#＃")
        elif text.startswith(("//", "／／")) or text.lower().startswith(("c:", "c：")):
            scope = "comment"
            text = text[2:]
    elif scope == "author":
        text = text.lstrip("@＠")
    elif scope == "tag":
        text = text.lstrip("#＃")
    elif scope == "comment" and (text.startswith(("//", "／／")) or text.lower().startswith(("c:", "c："))):
        text = text[2:]
    return scope, text.strip()[:500]


def like_contains(term: str) -> str:
    """构造转义 LIKE 通配符后的子串匹配模式（配合 ESCAPE '\\'）。"""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def can_use_trigram(term: str) -> bool:
    """FTS5 trigram 仅支持长度 ≥3 且不含 NUL 的精确子串匹配；短词回落 LIKE。"""
    return bool(term) and len(term) >= 3 and "\x00" not in term


def fts_phrase(term: str) -> str:
    """将搜索词包成 FTS5 双引号字面短语（对内嵌引号双写转义）。"""
    return '"' + term.replace('"', '""') + '"'


def post_filter(status: str, group: str, author: str, range: str,
                has_cmts: str = "all", source: str = "all", tags: str = "all",
                q: str = "", q_scope: str = "all"):
    """列表与批量打包共用的筛选条件构造（列名都带 p. 前缀）。

    status=favorites 是收藏筛选（favorite=1，任意 decision），与三态正交。
    has_cmts='1' = 只看写过评论的帖（评论豁免硬删的检索面）。
    source='x' / 'weibo' = 只看某一来源（微博/X，p.source 空串视为微博）。
    tags='a,b' = 任一标签命中即入选（OR，与前端 matchesView 同口径）；'all' = 不筛。
    q: 搜索词；q_scope: 搜索维度（all/text/comment/tag/author）。
    """
    where, args = ["1=1"], []
    if status == "favorites":
        where.append("p.favorite = 1")
    elif status != "all":
        where.append("p.status = ?")
        args.append(status)
    if has_cmts == "1":
        where.append("EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid = p.sid)")
    if tags and tags != "all":
        wanted = [t.strip() for t in tags.split(",") if t.strip()][:20]
        if wanted:
            qmarks = ",".join("?" * len(wanted))
            where.append(f"EXISTS(SELECT 1 FROM post_tags pt WHERE pt.sid = p.sid "
                         f"AND pt.tag IN ({qmarks}))")
            args += wanted
    if source in ("weibo", "x"):
        where.append("COALESCE(NULLIF(p.source,''),'weibo') = ?")
        args.append(source)
    if group != "all":
        where.append("EXISTS(SELECT 1 FROM post_groups pg WHERE pg.sid=p.sid AND pg.list_id=?)")
        args.append(group)
    if author != "all":
        source, uid = author.split(":", 1) if ":" in author else ("weibo", author)
        where.append("p.uid = ? AND COALESCE(NULLIF(p.source,''),'weibo') = ?")
        args += [uid, source]
    if range != "all":
        day = datetime.now(config.NOW_TZ)
        if range == "yesterday":
            # 上界用次日零点的开区间：23:59:59 会漏掉亚秒戳（客户端 matchesView 同口径）
            day -= timedelta(days=1)
            next_day = day + timedelta(days=1)
            where.append("p.created_at >= ? AND p.created_at < ?")
            args += [
                day.strftime("%Y-%m-%dT00:00:00+08:00"),
                next_day.strftime("%Y-%m-%dT00:00:00+08:00"),
            ]
        else:
            days = {"7d": 7, "30d": 30}.get(range)
            if days:
                day -= timedelta(days=days)
            where.append("p.created_at >= ?")
            args.append(day.strftime("%Y-%m-%dT00:00:00+08:00"))
    if q and q.strip():
        scope, term = parse_search_query(q, q_scope)
        if not term:
            # 只有前缀字符（如 "@"、"#"、"//"）时视作空关键词，避免退化为 LIKE '%%' 全表扫描
            where.append("1=0")
            return " AND ".join(where), args

        like = like_contains(term)
        use_fts = can_use_trigram(term)
        phrase = fts_phrase(term) if use_fts else ""
        if scope == "text":
            if use_fts:
                where.append("p.rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?)")
                args.append(f"text : {phrase}")
            else:
                where.append("p.text LIKE ? ESCAPE '\\'")
                args.append(like)
        elif scope == "comment":
            if use_fts:
                where.append(
                    "EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid = p.sid "
                    "AND pc.id IN (SELECT rowid FROM post_comments_fts WHERE post_comments_fts MATCH ?))"
                )
                args.append(f"body : {phrase}")
            else:
                where.append("EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid = p.sid AND pc.body LIKE ? ESCAPE '\\')")
                args.append(like)
        elif scope == "tag":
            where.append("EXISTS(SELECT 1 FROM post_tags pt WHERE pt.sid = p.sid AND pt.tag LIKE ? ESCAPE '\\')")
            args.append(like)
        elif scope == "author":
            if use_fts:
                where.append("p.rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?)")
                args.append(f"author : {phrase}")
            else:
                where.append("p.author LIKE ? ESCAPE '\\'")
                args.append(like)
        else:
            if use_fts:
                where.append(
                    "(p.rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?) "
                    "OR EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid = p.sid "
                    "AND pc.id IN (SELECT rowid FROM post_comments_fts WHERE post_comments_fts MATCH ?)) "
                    "OR EXISTS(SELECT 1 FROM post_tags pt WHERE pt.sid = p.sid AND pt.tag LIKE ? ESCAPE '\\'))"
                )
                args.extend([f"{{text author}} : {phrase}", f"body : {phrase}", like])
            else:
                where.append("(p.text LIKE ? ESCAPE '\\' "
                             "OR EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid = p.sid AND pc.body LIKE ? ESCAPE '\\') "
                             "OR EXISTS(SELECT 1 FROM post_tags pt WHERE pt.sid = p.sid AND pt.tag LIKE ? ESCAPE '\\') "
                             "OR p.author LIKE ? ESCAPE '\\')")
                args.extend([like, like, like, like])
    return " AND ".join(where), args
