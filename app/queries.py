"""列表与导出的共同筛选语义。"""

from datetime import datetime, timedelta

from . import config


def post_filter(status: str, group: str, author: str, range: str,
                has_cmts: str = "all", source: str = "all", tags: str = "all"):
    """列表与批量打包共用的筛选条件构造（列名都带 p. 前缀）。

    status=favorites 是收藏筛选（favorite=1，任意 decision），与三态正交。
    has_cmts='1' = 只看写过评论的帖（评论豁免硬删的检索面）。
    source='x' / 'weibo' = 只看某一来源（微博/X，p.source 空串视为微博）。
    tags='a,b' = 任一标签命中即入选（OR，与前端 matchesView 同口径）；'all' = 不筛。
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
        where.append("(p.list_id = ? OR EXISTS(SELECT 1 FROM post_groups pg WHERE pg.sid=p.sid AND pg.list_id=?))")
        args += [group, group]
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
    return " AND ".join(where), args
