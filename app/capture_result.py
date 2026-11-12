"""采集结果口径：每条收到的记录属于完成、跳过、未完成或尚未处理。"""
from . import db

COUNTERS = ("received", "normalized", "attempted", "accepted", "skipped", "failed",
            "partial", "unprocessed", "new_posts", "new_pics")


def counts(received=0):
    return {key: received if key == "received" else 0 for key in COUNTERS}


def stored(result, sid, existed, saved):
    result["attempted"] += 1
    post = db.has_post(sid)
    result["new_posts"] += int(not existed and post)
    result["new_pics"] += saved
    if db.has_tombstone(sid):
        result["skipped"] += 1
        return
    with db.conn() as c:
        unfinished = c.execute("SELECT 1 FROM image_jobs WHERE sid=?", (sid,)).fetchone() is not None
    if post and not unfinished:
        result["accepted"] += 1
    else:
        result["failed"] += 1
        result["partial"] += int(post)


def outcome(groups):
    errors = [g for g in groups if g.get("error")]
    failed = sum(g.get("failed", 0) + g.get("unprocessed", 0) for g in groups)
    available = sum(g.get("accepted", 0) + g.get("partial", 0) + g.get("new_posts", 0) for g in groups)
    if errors or failed:
        completed_groups = any(not g.get("error") and not g.get("failed") and not g.get("unprocessed") for g in groups)
        return "partial" if available or completed_groups else "failed"
    if any(g.get("coverage") == "page_limit" for g in groups):
        return "incomplete"
    return "success"


def aggregate(groups):
    return {key: sum(g.get(key, 0) for g in groups) for key in COUNTERS}
