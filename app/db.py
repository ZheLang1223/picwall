"""SQLite 存取：帖子、媒体、收录、恢复任务、归档与设备权限。

posts.status: pending（未处理）/ kept（保留）/ discarded（不要）
favorite 是独立布尔标记，与 status 正交：删除不清除收藏，「收藏」筛选 = favorite=1 任意状态。
source 是内容来源标识（weibo/x/…）；跨来源 ID 防撞策略 = 新来源 sid 带前缀（x:<tweet_id>），
微博存量 sid 不动，以 source 列为准。
pics.file/thumb 是相对 PICS_DIR 的路径；file 为空表示下载原图失败。
discarded_at 是「不要」的时间戳，自动清理按它计龄。
keep_count 是「点赞次数」：首次保留 =1 建账，此后每帖每天最多 +1 一次（卡上点赞语义，
服务层统一限流，回看点赞 / 放映有效回看 / 详情 ±1 同受此限），撤销 -1，
状态变化（恢复/批量/收藏）不动它，永不低于 1；last_confirm_at 是最近一次建账/+1 的
时间戳，「今日已赞」态与每日限流都按它判；shown_count/last_shown_at 记回看出场。
post_tags 是用户自定义标签（sid,tag），与决定状态/收藏正交，改标签不动 decision_version。
（text_full / is_long 两列是已下线的长文展开设计遗留，仅存不用。）
"""
import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

from . import config
from .gate import WriteLock

# 用普通 Lock（带 .locked()，锁不变量测试依赖它）而不是 RLock：需要「写文件 + 插行」
# 跨函数单一临界区的调用方，配 _insert_pic_locked 使用，嵌套关系显式。
_write_lock = WriteLock()
write_lock = _write_lock   # 规范公开别名，兼容原有 _write_lock

# shuffle 全序缓存：按 (db_sig, where, args, seed) 命中，写操作或外部修改自动失效。
_SHUFFLE_CACHE: OrderedDict[tuple, tuple[tuple, list[tuple[str, str]]]] = OrderedDict()
_SHUFFLE_CACHE_MAX = 32
_shuffle_lock = threading.Lock()
_shuffle_epoch = 0


def _bump_shuffle_epoch() -> None:
    global _shuffle_epoch
    with _shuffle_lock:
        _shuffle_epoch += 1


# schema 版本：v1 初始；v2 favorite/source；v3 缺图重试与分页；
# v4 决定版本、收录与媒体校验；v5 设备配对与会话；v6 回看计数（keep_count 等）；
# v7 帖子评论（单人批注时间线）；v9 X 本地分组（xg: 组与成员映射）；
# v10 帖子标签（post_tags，用户自定义归类维度）；v11 视频媒体（pics.kind/dur_ms/poster_url）；
# v12 热路径索引与 FTS5 trigram 全文检索；v13 保持 idx_posts_status 晚于复合状态索引 + ANALYZE。
# 迁移全部走「缺列补列 + 幂等回填」，老库升级不丢数据；版本号只作诊断展示，不作为分支依据。
_SCHEMA_VERSION = 13

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  sid        TEXT PRIMARY KEY,          -- mblogid（base62，取原图所在的原博）
  list_id    TEXT,
  uid        TEXT,
  author     TEXT,
  avatar     TEXT,
  text       TEXT,
  created_at TEXT,
  fetched_at TEXT,
  weibo_url  TEXT,
  via        TEXT,                      -- 转发者昵称（原博图时记录）
  status     TEXT DEFAULT 'pending',
  kept_at    TEXT,
  keep_count INTEGER DEFAULT 1,          -- 点赞次数：首次保留=1，回看点赞+1
  shown_count INTEGER DEFAULT 0,         -- 回看出场次数
  last_shown_at TEXT,                    -- 上次被选入回看组的时间
  last_confirm_at TEXT                   -- 上次点赞的时间
);
CREATE TABLE IF NOT EXISTS pics (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  sid   TEXT,
  file  TEXT,
  thumb TEXT,
  w     INTEGER,
  h     INTEGER,
  url   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pics_file ON pics(file);
CREATE INDEX IF NOT EXISTS idx_posts_list ON posts(list_id);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_status_cursor ON posts(status,created_at DESC,sid);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE TABLE IF NOT EXISTS groups (
  list_id  TEXT PRIMARY KEY,
  name     TEXT,
  added_at TEXT,
  active   INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS tombstones (
  sid       TEXT PRIMARY KEY,
  purged_at TEXT
);
CREATE TABLE IF NOT EXISTS image_jobs (
  sid TEXT PRIMARY KEY,
  list_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS decision_undos (
  sid TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  previous TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_groups (
  sid TEXT NOT NULL,
  list_id TEXT NOT NULL,
  PRIMARY KEY(sid,list_id)
);
CREATE TABLE IF NOT EXISTS group_members (
  list_id   TEXT NOT NULL,     -- 本地组（xg:<slug>）；组本身在 groups 表，不是抓取目标
  member_id TEXT NOT NULL,     -- 成员抓取目标 id（xu:<handle>；结构不限定，未来可纳 wbu:）
  added_at  TEXT,
  PRIMARY KEY(list_id,member_id)
);
CREATE INDEX IF NOT EXISTS idx_post_groups_list ON post_groups(list_id,sid);
CREATE TABLE IF NOT EXISTS post_observations (
  sid TEXT NOT NULL,
  list_id TEXT NOT NULL,
  via TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL,
  PRIMARY KEY(sid,list_id,via)
);
CREATE TABLE IF NOT EXISTS media_contents (
  sha256 TEXT PRIMARY KEY,
  file TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_entries (
  file TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  sha256 TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  state TEXT DEFAULT 'unchecked',
  error TEXT DEFAULT '',
  checked_at TEXT
);
CREATE TABLE IF NOT EXISTS file_jobs (
  file TEXT PRIMARY KEY,
  attempts INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_codes (
  hash TEXT PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer','collector')),
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_sessions (
  id TEXT PRIMARY KEY,
  hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer','collector')),
  name TEXT NOT NULL,
  created REAL NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing_attempts (
  client TEXT PRIMARY KEY,
  window REAL NOT NULL,
  failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sid TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('shown','reaffirm','undo')),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_events_sid ON review_events(sid);
CREATE TABLE IF NOT EXISTS post_comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  sid        TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL,              -- 首发 = 显示时间；编辑不改写（批注时间线语义）
  updated_at TEXT                         -- 仅内部记录，不展示
);
CREATE INDEX IF NOT EXISTS idx_post_comments_sid ON post_comments(sid,id);
CREATE TABLE IF NOT EXISTS post_tags (
  sid        TEXT NOT NULL,
  tag        TEXT NOT NULL,             -- 用户自定义标签（trim 后 1-24 字，单帖 ≤10 个）
  created_at TEXT NOT NULL,
  PRIMARY KEY(sid,tag)
);
CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag);
CREATE TABLE IF NOT EXISTS cloud_blobs (
  key         TEXT PRIMARY KEY,            -- 内容 sha256，与备份 blob 文件名同源
  fid         TEXT NOT NULL,               -- 夸克网盘文件 FID（回源寻址）
  size        INTEGER DEFAULT 0,
  uploaded_at TEXT NOT NULL
);
"""


# 线程本地长连接：每线程一条，按 DB_PATH 失效（fixture/测试换库自动换新）。
# 不再用「每调用开一条、用完即关」——建连（findReusableFd 全局互斥）与末连接
# 关闭的 WAL checkpoint（unixLock 排他）在高频并发下会互锁，2026-11-07 实测
# 两次把全部请求楔死（主线程空闲等 kevent、线程池全部卡死在 sqlite 内部）。
_conn_local = threading.local()


def conn() -> sqlite3.Connection:
    cached = getattr(_conn_local, "conn", None)
    if cached is not None and _conn_local.path == config.DB_PATH:
        return cached
    if cached is not None:
        try:
            cached.close()
        except sqlite3.Error:
            pass
    c = sqlite3.connect(config.DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=20000")
    _conn_local.conn = c
    _conn_local.path = config.DB_PATH
    return c


def init_db() -> None:
    config.ensure_dirs()
    with _write_lock, conn() as c:
        current = 0
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            version = c.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
            try:
                current = int(version[0]) if version and version[0] else 0
            except (TypeError, ValueError) as e:
                raise ValueError("数据库版本标记无效，迁移停止，请先检查备份副本") from e
            if current > _SCHEMA_VERSION:
                raise ValueError("数据库来自更新版本，拒绝用旧程序迁移或回写版本号")
        # executescript 会隐式提交，不能用于需要整体回滚的迁移。
        c.execute("BEGIN IMMEDIATE")
        statement = ""
        for line in _SCHEMA.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                c.execute(statement)
                statement = ""
        if statement.strip():
            raise ValueError("迁移建表语句不完整")
        # 同帖同文件只保留最早关系行；不能在唯一索引失败后仍宣称迁移成功。
        duplicates = c.execute("SELECT COUNT(*) FROM pics WHERE id NOT IN "
                               "(SELECT MIN(id) FROM pics GROUP BY sid,file)").fetchone()[0]
        c.execute("DELETE FROM pics WHERE id NOT IN "
                  "(SELECT MIN(id) FROM pics GROUP BY sid,file)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pics_sid_file ON pics(sid, file)")
        # 增量迁移：老库补新列（sqlite 的 ADD COLUMN 带 default 只对 INTEGER 常量可靠，统一可空）
        cols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        for col, ddl in (("text_full", "TEXT"), ("is_long", "INTEGER DEFAULT 0"),
                         ("discarded_at", "TEXT"),
                         ("favorite", "INTEGER DEFAULT 0"),
                         ("source", "TEXT DEFAULT ''"),
                         ("decision_version", "INTEGER NOT NULL DEFAULT 0"),
                         ("handle", "TEXT DEFAULT ''"),
                         ("text_complete", "INTEGER DEFAULT 1"),
                         ("avatar_url", "TEXT DEFAULT ''"),
                         ("keep_count", "INTEGER DEFAULT 1"),
                         ("shown_count", "INTEGER DEFAULT 0"),
                         ("last_shown_at", "TEXT"),
                         ("last_confirm_at", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE posts ADD COLUMN {col} {ddl}")
        # 回看计数建账：全部存量帖（含已保留）记为 ×1；此后只有回看点赞会 +1
        c.execute("UPDATE posts SET keep_count=1 WHERE keep_count IS NULL OR keep_count < 1")
        c.execute("UPDATE posts SET shown_count=0 WHERE shown_count IS NULL")
        # 存量帖来源回填：微博是唯一历史来源；带前缀的新来源（x:…）入库时已写 source
        c.execute("UPDATE posts SET source='weibo' WHERE source='' OR source IS NULL")
        if "text_complete" not in cols:
            c.execute("UPDATE posts SET text_complete=0 WHERE is_long=1 AND COALESCE(text_full,'')='' ")
        c.execute("INSERT OR IGNORE INTO post_groups SELECT sid,list_id FROM posts WHERE list_id IS NOT NULL AND list_id != ''")
        for table, additions in (
            ("pics", (("position", "INTEGER"), ("actual_url", "TEXT DEFAULT ''"),
                      ("quality", "TEXT DEFAULT 'unknown'"), ("media_id", "TEXT DEFAULT ''"),
                      ("kind", "TEXT NOT NULL DEFAULT 'photo'"),
                      ("dur_ms", "INTEGER DEFAULT 0"),
                      ("poster_url", "TEXT DEFAULT ''"))),
            ("image_jobs", (("created_at", "TEXT DEFAULT ''"), ("updated_at", "TEXT DEFAULT ''"),
                            ("next_retry_at", "TEXT DEFAULT ''"), ("error_kind", "TEXT DEFAULT ''"))),
        ):
            existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, ddl in additions:
                if col not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        c.execute("UPDATE pics SET position=(SELECT COUNT(*) FROM pics old WHERE old.sid=pics.sid AND old.id<pics.id) WHERE position IS NULL")
        # 迁移/新建统一补齐热查询索引与 FTS5 trigram 虚拟表（必须在补列之后创建）
        c.execute("DROP INDEX IF EXISTS idx_pics_sid")
        for idx_ddl in (
            "CREATE INDEX IF NOT EXISTS idx_pics_sid_pos ON pics(sid,position,id,thumb,file)",
            "CREATE INDEX IF NOT EXISTS idx_posts_fav_cursor ON posts(created_at DESC,sid) WHERE favorite=1",
            "CREATE INDEX IF NOT EXISTS idx_posts_status_keep ON posts(status,keep_count DESC,created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_posts_author_source ON posts(COALESCE(NULLIF(source,''),'weibo'),uid,created_at DESC,fetched_at DESC,sid DESC)",
            "CREATE INDEX IF NOT EXISTS idx_posts_src_uid_latest ON posts(source,uid,created_at DESC,fetched_at DESC,sid)",
            "CREATE INDEX IF NOT EXISTS idx_post_comments_created ON post_comments(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_review_events_action_created ON review_events(action,created_at)",
        ):
            c.execute(idx_ddl)
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS sync_post_groups_insert "
            "AFTER INSERT ON posts WHEN NEW.list_id IS NOT NULL AND NEW.list_id != '' "
            "BEGIN INSERT OR IGNORE INTO post_groups(sid,list_id) VALUES(NEW.sid,NEW.list_id); END"
        )
        had_posts_fts = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts_fts'"
        ).fetchone() is not None
        had_comments_fts = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_comments_fts'"
        ).fetchone() is not None
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5("
            "text, author, uid, content='posts', content_rowid='rowid', tokenize='trigram')"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN "
            "INSERT INTO posts_fts(rowid,text,author,uid) "
            "VALUES(new.rowid,COALESCE(new.text,''),COALESCE(new.author,''),COALESCE(new.uid,'')); END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN "
            "INSERT INTO posts_fts(posts_fts,rowid,text,author,uid) "
            "VALUES('delete',old.rowid,COALESCE(old.text,''),COALESCE(old.author,''),COALESCE(old.uid,'')); END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE OF text,author,uid ON posts BEGIN "
            "INSERT INTO posts_fts(posts_fts,rowid,text,author,uid) "
            "VALUES('delete',old.rowid,COALESCE(old.text,''),COALESCE(old.author,''),COALESCE(old.uid,'')); "
            "INSERT INTO posts_fts(rowid,text,author,uid) "
            "VALUES(new.rowid,COALESCE(new.text,''),COALESCE(new.author,''),COALESCE(new.uid,'')); END"
        )
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS post_comments_fts USING fts5("
            "body, content='post_comments', content_rowid='id', tokenize='trigram')"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS post_comments_fts_ai AFTER INSERT ON post_comments BEGIN "
            "INSERT INTO post_comments_fts(rowid,body) VALUES(new.id,COALESCE(new.body,'')); END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS post_comments_fts_ad AFTER DELETE ON post_comments BEGIN "
            "INSERT INTO post_comments_fts(post_comments_fts,rowid,body) "
            "VALUES('delete',old.id,COALESCE(old.body,'')); END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS post_comments_fts_au AFTER UPDATE OF body ON post_comments BEGIN "
            "INSERT INTO post_comments_fts(post_comments_fts,rowid,body) "
            "VALUES('delete',old.id,COALESCE(old.body,'')); "
            "INSERT INTO post_comments_fts(rowid,body) VALUES(new.id,COALESCE(new.body,'')); END"
        )
        if not had_posts_fts:
            c.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
        if not had_comments_fts:
            c.execute("INSERT INTO post_comments_fts(post_comments_fts) VALUES('rebuild')")
        _bump_shuffle_epoch()
        c.execute("INSERT OR IGNORE INTO archive_entries(file,day) SELECT substr(k,10),v FROM meta WHERE k LIKE 'exported:%' AND v!=''")
        # 老库里已处于 discarded 的行补计时起点（只补 NULL，不重置已有计时）
        c.execute("UPDATE posts SET discarded_at=? "
                  "WHERE status='discarded' AND discarded_at IS NULL",
                  (datetime.now(config.NOW_TZ).isoformat(timespec="seconds"),))
        invalid = c.execute("SELECT COUNT(*) FROM posts WHERE status IS NULL OR "
                            "status NOT IN ('pending','kept','discarded') OR "
                            "COALESCE(favorite,0) NOT IN (0,1) OR decision_version<0").fetchone()[0]
        if invalid:
            raise ValueError(f"迁移停止：{invalid} 条无效决定记录，需要先修复副本")
        for event in ("INSERT", "UPDATE"):
            c.execute(f"CREATE TRIGGER IF NOT EXISTS validate_posts_{event.lower()} "
                      f"BEFORE {event} ON posts WHEN NEW.status IS NULL OR "
                      "NEW.status NOT IN ('pending','kept','discarded') OR "
                      "COALESCE(NEW.favorite,0) NOT IN (0,1) OR NEW.decision_version<0 "
                      "BEGIN SELECT RAISE(ABORT,'invalid post decision'); END")
            # 时间不变量也在数据库层保障；客户端历史字段不会进入恢复写入。
            c.execute(f"CREATE TRIGGER IF NOT EXISTS validate_post_times_{event.lower()} "
                      f"BEFORE {event} ON posts WHEN "
                      "(NEW.status='pending' AND (NEW.kept_at IS NOT NULL OR NEW.discarded_at IS NOT NULL)) OR "
                      "(NEW.status='kept' AND (NEW.kept_at IS NULL OR julianday(NEW.kept_at) IS NULL OR NEW.discarded_at IS NOT NULL)) OR "
                      "(NEW.status='discarded' AND (NEW.discarded_at IS NULL OR julianday(NEW.discarded_at) IS NULL OR NEW.kept_at IS NOT NULL)) "
                      "BEGIN SELECT RAISE(ABORT,'invalid decision timestamps'); END")
        time_invalid = c.execute("SELECT COUNT(*) FROM posts WHERE "
                                 "(status='pending' AND (kept_at IS NOT NULL OR discarded_at IS NOT NULL)) OR "
                                 "(status='kept' AND (kept_at IS NULL OR julianday(kept_at) IS NULL OR discarded_at IS NOT NULL)) OR "
                                 "(status='discarded' AND (discarded_at IS NULL OR julianday(discarded_at) IS NULL OR kept_at IS NOT NULL))").fetchone()[0]
        if time_invalid:
            raise ValueError(f"迁移停止：{time_invalid} 条决定时间不一致，需要先修复副本")
        if current < 13:
            # SQLite 无统计或同选择度并列时倾向选后创建的同前缀索引；确保 idx_posts_status 在
            # idx_posts_status_cursor / idx_posts_status_keep 之后，避免全状态 LIKE 误走 keep 索引随机寻址。
            c.execute("DROP INDEX IF EXISTS idx_posts_status")
            c.execute("CREATE INDEX idx_posts_status ON posts(status)")
            if c.execute("SELECT 1 FROM posts LIMIT 1").fetchone():
                c.execute("ANALYZE")
        c.execute("INSERT OR IGNORE INTO schema_migrations VALUES(?,?,?)",
                  (_SCHEMA_VERSION, datetime.now(config.NOW_TZ).isoformat(),
                   json.dumps({"merged_duplicate_pics": duplicates})))
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version', ?)",
                  (str(_SCHEMA_VERSION),))


def has_post(sid: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM posts WHERE sid=?", (sid,)).fetchone() is not None


def has_tombstone(sid: str) -> bool:
    """墓碑：被 purge 硬删过的 sid。任何入库通道都必须拦下，防「删了又复活」。"""
    with conn() as c:
        return c.execute("SELECT 1 FROM tombstones WHERE sid=?", (sid,)).fetchone() is not None


def insert_post(p: dict, list_id: str) -> None:
    with _write_lock, conn() as c:
        if c.execute("SELECT 1 FROM tombstones WHERE sid=?", (p["sid"],)).fetchone():
            return
        c.execute(
            "INSERT OR IGNORE INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
            "fetched_at,weibo_url,via,is_long,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["sid"], list_id, p["uid"], p["author"], p["avatar"], p["text"],
             p["created_at"], p["fetched_at"], p["weibo_url"], p["via"],
             p.get("is_long", 0), p.get("source", "weibo")))
        _link_group_locked(c, p, list_id)
        c.execute("UPDATE posts SET handle=?,text_complete=?,avatar_url=? WHERE sid=?",
                  (p.get("handle", ""), int(p.get("text_complete", not p.get("is_long", 0))),
                   p.get("avatar_url", p.get("avatar", "")) if not p.get("avatar", "").startswith("/media/") else p.get("avatar_url", ""), p["sid"]))
    _bump_shuffle_epoch()


def author_key(source: str, uid: str) -> str:
    return uid if source in ("weibo", "", None) else f"{source}:{uid}"


def _link_group_locked(c, p: dict, list_id: str):
    c.execute("INSERT OR IGNORE INTO post_groups VALUES(?,?)", (p["sid"], list_id))
    # 本地组（xg:）归组：成员目标入库时，同时挂进它所属的全部自定义组（幂等）
    c.execute("INSERT OR IGNORE INTO post_groups "
              "SELECT ?, gm.list_id FROM group_members gm WHERE gm.member_id=?",
              (p["sid"], list_id))
    c.execute("INSERT INTO post_observations VALUES(?,?,?,?,?) ON CONFLICT(sid,list_id,via) "
              "DO UPDATE SET text=excluded.text,observed_at=excluded.observed_at",
              (p["sid"], list_id, p.get("via", ""), p.get("text", ""),
               p.get("fetched_at") or datetime.now(config.NOW_TZ).isoformat()))


def observe_post(p: dict, list_id: str):
    with _write_lock, conn() as c:
        if c.execute("SELECT 1 FROM posts WHERE sid=?", (p["sid"],)).fetchone():
            _link_group_locked(c, p, list_id)
    _bump_shuffle_epoch()


def observe_posts(posts: list[dict], list_id: str) -> int:
    """批量收录观测与分组关联：整批仅一次 _write_lock + 一次提交，消除逐条 WAL fsync。"""
    unique = {}
    for p in posts:
        sid = p.get("sid")
        if sid and sid not in unique:
            unique[sid] = p
    if not unique:
        return 0
    now = datetime.now(config.NOW_TZ).isoformat()
    sids = list(unique.keys())
    existing: set[str] = set()
    with _write_lock, conn() as c:
        for idx in range(0, len(sids), 400):
            chunk = sids[idx:idx + 400]
            qmarks = ",".join("?" * len(chunk))
            existing.update(
                r[0] for r in c.execute(f"SELECT sid FROM posts WHERE sid IN ({qmarks})", chunk)
            )
        targets = [sid for sid in sids if sid in existing]
        if targets:
            extra_groups = [
                r[0] for r in c.execute(
                    "SELECT list_id FROM group_members WHERE member_id=?", (list_id,)
                )
            ]
            c.executemany(
                "INSERT OR IGNORE INTO post_groups VALUES(?,?)",
                [(sid, list_id) for sid in targets],
            )
            if extra_groups:
                c.executemany(
                    "INSERT OR IGNORE INTO post_groups VALUES(?,?)",
                    [(sid, gid) for sid in targets for gid in extra_groups],
                )
            c.executemany(
                "INSERT INTO post_observations VALUES(?,?,?,?,?) "
                "ON CONFLICT(sid,list_id,via) DO UPDATE SET text=excluded.text,observed_at=excluded.observed_at",
                [
                    (
                        sid,
                        list_id,
                        unique[sid].get("via", ""),
                        unique[sid].get("text", ""),
                        unique[sid].get("fetched_at") or now,
                    )
                    for sid in targets
                ],
            )
    if targets:
        _bump_shuffle_epoch()
    return len(targets)


def insert_pic(sid: str, file: str, thumb: str, w: int, h: int, url: str,
               kind: str = "photo", dur_ms: int = 0, poster_url: str = "") -> None:
    with _write_lock:
        _insert_pic_locked(sid, file, thumb, w, h, url, kind=kind, dur_ms=dur_ms,
                           poster_url=poster_url)


def _insert_pic_locked(
    sid: str,
    file: str,
    thumb: str,
    w: int,
    h: int,
    url: str,
    kind: str = "photo",
    dur_ms: int = 0,
    poster_url: str = "",
    position: int | None = None,
    media_id: str = "",
    actual_url: str = "",
    quality: str = "",
    replace_url: bool = False,
) -> None:
    """已持 _write_lock 时用：供「写文件 + 插行」跨函数单一事务临界区组合（见 crawler._store_post）。

    kind='video' 时 w/h 存宽高比整数（aspect_w/aspect_h），thumb 存海报缩略图路径，
    dur_ms 存时长毫秒、poster_url 存海报原址（缺图补抓时海报可重下，内容寻址自动去重）。
    """
    with conn() as c:
        if replace_url:
            c.execute("DELETE FROM pics WHERE sid=? AND url=?", (sid, url))
        if position is None:
            c.execute(
                "INSERT OR IGNORE INTO pics(sid,file,thumb,w,h,url,kind,dur_ms,poster_url) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, file, thumb, w, h, url, kind, int(dur_ms or 0), poster_url),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO pics"
                "(sid,file,thumb,w,h,url,kind,dur_ms,poster_url,position,media_id,actual_url,quality) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    file,
                    thumb,
                    w,
                    h,
                    url,
                    kind,
                    int(dur_ms or 0),
                    poster_url,
                    int(position),
                    media_id,
                    actual_url,
                    quality,
                ),
            )
            if quality:
                c.execute(
                    "UPDATE pics SET position=?,media_id=?,actual_url=?,quality=? WHERE sid=? AND file=?",
                    (int(position), media_id, actual_url, quality, sid, file),
                )


def mark_snapshot(sid: str, status: str) -> dict | None:
    if status not in ("pending", "kept", "discarded"):
        raise ValueError("无效决定")
    now = datetime.now(config.NOW_TZ).isoformat(timespec="microseconds")
    with _write_lock, conn() as c:
        row = c.execute("SELECT status,kept_at,discarded_at,decision_version FROM posts WHERE sid=?", (sid,)).fetchone()
        if not row:
            return None
        before = dict(row)
        after = {"status": status, "kept_at": now if status == "kept" else None,
                 "discarded_at": now if status == "discarded" else None,
                 "decision_version": before["decision_version"] + 1}
        # 转入 kept 即建账点赞：盖当日账（COALESCE 让非 kept 转移不碰历史账）
        c.execute("UPDATE posts SET status=?,kept_at=?,discarded_at=?,"
                  "last_confirm_at=COALESCE(?,last_confirm_at),decision_version=? WHERE sid=?",
                  (status, after["kept_at"], after["discarded_at"],
                   now if status == "kept" else None, after["decision_version"], sid))
        c.execute("INSERT OR REPLACE INTO decision_undos VALUES(?,?,?)",
                  (sid, after["decision_version"], json.dumps(before)))
    _bump_shuffle_epoch()
    return {"previous": before, "decision": after}


def restore_snapshot(sid: str, previous: dict, expected: dict) -> bool:
    with _write_lock, conn() as c:
        row = c.execute("SELECT status,kept_at,discarded_at,decision_version FROM posts WHERE sid=?", (sid,)).fetchone()
        if not row or dict(row) != expected:
            return False
        undo = c.execute("SELECT version,previous FROM decision_undos WHERE sid=?", (sid,)).fetchone()
        if not undo or undo["version"] != row["decision_version"] or json.loads(undo["previous"]) != previous:
            return False
        original = json.loads(undo["previous"])
        c.execute("UPDATE posts SET status=?,kept_at=?,discarded_at=?,decision_version=decision_version+1 WHERE sid=?",
                  (original["status"], original["kept_at"], original["discarded_at"], sid))
        c.execute("DELETE FROM decision_undos WHERE sid=?", (sid,))
    _bump_shuffle_epoch()
    return True


def set_status(sid: str, status: str) -> None:
    mark_snapshot(sid, status)


def set_status_bulk(sids: list[str], status: str) -> None:
    """批量改状态：单事务一次提交。逐条提交的 fsync 在批量场景下是数量级开销。"""
    if status not in ("pending", "kept", "discarded"):
        raise ValueError("无效决定")
    now = datetime.now(config.NOW_TZ).isoformat(timespec="microseconds")
    kept_at = now if status == "kept" else None
    discarded_at = now if status == "discarded" else None
    with _write_lock, conn() as c:
        # 批量保留同样算当日建账点赞（COALESCE：非 kept 批量不碰历史账）
        c.executemany("UPDATE posts SET status=?, kept_at=?, discarded_at=?, "
                      "last_confirm_at=COALESCE(?, last_confirm_at), decision_version=decision_version+1 WHERE sid=?",
                      [(status, kept_at, discarded_at, kept_at, s) for s in sids])
        c.executemany("DELETE FROM decision_undos WHERE sid=?", [(s,) for s in sids])
    _bump_shuffle_epoch()


def set_favorite(sid: str, favorite: bool) -> None:
    """收藏独立；取消收藏的不要帖重计清理期限，同时使旧撤销失效。"""
    with _write_lock, conn() as c:
        c.execute("UPDATE posts SET favorite=?, decision_version=decision_version+CASE "
                  "WHEN favorite=1 AND ?=0 AND status='discarded' THEN 1 ELSE 0 END, discarded_at=CASE "
                  "WHEN favorite=1 AND ?=0 AND status='discarded' THEN ? "
                  "ELSE discarded_at END WHERE sid=?",
                  (int(favorite), int(favorite), int(favorite),
                   datetime.now(config.NOW_TZ).isoformat(timespec="seconds"), sid))
    _bump_shuffle_epoch()


def list_comments(sid: str) -> list[dict]:
    """帖子的评论流（旧→新，id 即顺序）；get_post 内嵌与单独取齐两用。"""
    with conn() as c:
        rows = c.execute("SELECT id,body,created_at,updated_at FROM post_comments "
                         "WHERE sid=? ORDER BY id", (sid,)).fetchall()
    return [dict(r) for r in rows]


def comment_counts(sids: list[str]) -> dict[str, int]:
    """一批帖子的评论数（墙卡角标），一次 GROUP BY 防止 N+1。"""
    if not sids:
        return {}
    qmarks = ",".join("?" * len(sids))
    with conn() as c:
        rows = c.execute(f"SELECT sid,COUNT(*) n FROM post_comments WHERE sid IN ({qmarks}) "
                         "GROUP BY sid", sids).fetchall()
    return {r["sid"]: r["n"] for r in rows}


def comments_map(sids: list[str]) -> dict[str, list[dict]]:
    """一批帖子的评论流按 sid 分组（回看选组整卡渲染用），组内旧→新。"""
    if not sids:
        return {}
    qmarks = ",".join("?" * len(sids))
    with conn() as c:
        rows = c.execute(f"SELECT sid,id,body,created_at,updated_at FROM post_comments "
                         f"WHERE sid IN ({qmarks}) ORDER BY id", sids).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["sid"], []).append(
            {k: r[k] for k in ("id", "body", "created_at", "updated_at")})
    return out


def add_comment(sid: str, body: str, created_at: str | None = None) -> dict:
    """追加一条评论；时间由服务端落账（演示数据可显式传入做旧）。"""
    now = created_at or datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
    with _write_lock, conn() as c:
        cur = c.execute("INSERT INTO post_comments(sid,body,created_at) VALUES(?,?,?)",
                        (sid, body, now))
        row = c.execute("SELECT id,body,created_at,updated_at FROM post_comments WHERE id=?",
                        (cur.lastrowid,)).fetchone()
    _bump_shuffle_epoch()
    return dict(row)


def update_comment(sid: str, comment_id: int, body: str) -> dict | None:
    """改写单条正文；created_at 保持首发时刻（「明确的时间」不因编辑漂移）。"""
    with _write_lock, conn() as c:
        cur = c.execute("UPDATE post_comments SET body=?, updated_at=? WHERE id=? AND sid=?",
                        (body, datetime.now(config.NOW_TZ).isoformat(timespec="seconds"),
                         comment_id, sid))
        if cur.rowcount != 1:
            return None
        row = c.execute("SELECT id,body,created_at,updated_at FROM post_comments WHERE id=?",
                        (comment_id,)).fetchone()
    _bump_shuffle_epoch()
    return dict(row)


def delete_comment(sid: str, comment_id: int) -> bool:
    with _write_lock, conn() as c:
        cur = c.execute("DELETE FROM post_comments WHERE id=? AND sid=?", (comment_id, sid))
    _bump_shuffle_epoch()
    return cur.rowcount == 1


# ---------- 标签（用户自定义归类维度，与决定状态/收藏正交） ----------

TAG_MAX_LEN = 24
TAGS_PER_POST = 10


def _clean_tag(tag) -> str:
    """标签名规范化：必须是非空字符串、trim 后 1-24 字；不合法抛 ValueError。"""
    t = str(tag or "").strip()
    if not t:
        raise ValueError("标签不能为空")
    if len(t) > TAG_MAX_LEN:
        raise ValueError(f"标签最长 {TAG_MAX_LEN} 字")
    return t


def _dedup_clean_tags(raw) -> list[str]:
    out: list[str] = []
    for t in raw or ():
        t = _clean_tag(t)
        if t not in out:
            out.append(t)
    return out


def set_post_tags(sid: str, add=None, remove=None) -> list[str]:
    """给帖子增删标签，返回该帖当前全部标签（打标时间序）。

    单事务整体生效：先算终集校验上限，超限抛错整笔回滚，不做半截修改。
    帖子不存在返回空表（路由层负责 404）。
    """
    adds = _dedup_clean_tags(add)
    removes = _dedup_clean_tags(remove)
    now = datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
    with _write_lock, conn() as c:
        if not c.execute("SELECT 1 FROM posts WHERE sid=?", (sid,)).fetchone():
            return []
        current = {r[0] for r in c.execute("SELECT tag FROM post_tags WHERE sid=?", (sid,))}
        final = (current - set(removes)) | set(adds)
        if len(final) > TAGS_PER_POST:
            raise ValueError(f"每帖最多 {TAGS_PER_POST} 个标签")
        if removes:
            qmarks = ",".join("?" * len(removes))
            c.execute(f"DELETE FROM post_tags WHERE sid=? AND tag IN ({qmarks})", (sid, *removes))
        c.executemany("INSERT OR IGNORE INTO post_tags(sid,tag,created_at) VALUES(?,?,?)",
                      [(sid, t, now) for t in adds])
        rows = c.execute("SELECT tag FROM post_tags WHERE sid=? ORDER BY created_at,tag",
                         (sid,)).fetchall()
    _bump_shuffle_epoch()
    return [r[0] for r in rows]


def set_post_tags_bulk(sids: list[str], add=None, remove=None) -> dict:
    """批量增删标签：单事务一次提交（逐条 fsync 是数量级开销，同 set_status_bulk）。

    加标签会让某帖超出单帖上限时跳过该帖而不是整批报错；返回 updated/skipped。
    """
    adds = _dedup_clean_tags(add)
    removes = _dedup_clean_tags(remove)
    sids = list(dict.fromkeys(s for s in (sids or ()) if s))
    updated = skipped = 0
    if not (adds or removes) or not sids:
        return {"updated": 0, "skipped": len(sids) if (adds or removes) else 0}
    now = datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
    qmarks = ",".join("?" * len(sids))
    with _write_lock, conn() as c:
        existing = {r[0] for r in c.execute(
            f"SELECT sid FROM posts WHERE sid IN ({qmarks})", sids)}
        have = {}
        if existing and adds:
            eq = ",".join("?" * len(existing))
            for r in c.execute(f"SELECT sid,tag FROM post_tags WHERE sid IN ({eq})",
                               list(existing)):
                have.setdefault(r[0], set()).add(r[1])
        good = []
        for sid in existing:
            final = (have.get(sid, set()) - set(removes)) | set(adds)
            if len(final) > TAGS_PER_POST:
                skipped += 1
                continue
            good.append(sid)
        if good:
            gq = ",".join("?" * len(good))
            if removes:
                c.execute(f"DELETE FROM post_tags WHERE tag IN "
                          f"({','.join('?' * len(removes))}) AND sid IN ({gq})", (*removes, *good))
            if adds:
                c.executemany(
                    "INSERT OR IGNORE INTO post_tags(sid,tag,created_at) VALUES(?,?,?)",
                    [(sid, t, now) for sid in good for t in adds])
        updated = len(good)
    if good:
        _bump_shuffle_epoch()
    return {"updated": updated, "skipped": skipped}


def post_tags_map(sids: list[str]) -> dict[str, list[str]]:
    """一批帖子的标签按 sid 分组（payload 批量取齐防 N+1），打标时间序。"""
    if not sids:
        return {}
    qmarks = ",".join("?" * len(sids))
    with conn() as c:
        rows = c.execute(f"SELECT sid,tag FROM post_tags WHERE sid IN ({qmarks}) "
                         "ORDER BY created_at,tag", sids).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["sid"], []).append(r["tag"])
    return out


def list_tags() -> list[dict]:
    """全部标签与帖数（任意状态，被硬删的帖自然不再计入）；n 降序、同数按名。"""
    with conn() as c:
        rows = c.execute(
            "SELECT pt.tag tag, COUNT(*) n FROM post_tags pt "
            "JOIN posts p ON p.sid = pt.sid GROUP BY pt.tag ORDER BY n DESC, pt.tag").fetchall()
    return [{"tag": r["tag"], "n": r["n"]} for r in rows]


def search_live(q: str, q_scope: str = "all", limit: int = 15) -> dict:
    """实时搜索：在 SQLite 中快速跨作者、标签、内容与评论多维度检索。

    q_scope: all/author/tag/text/comment
    """
    from .queries import can_use_trigram, fts_phrase, like_contains, parse_search_query

    scope, text = parse_search_query(q, q_scope)
    if not text:
        return {"ok": True, "authors": [], "tags": [], "content": [], "comments": []}

    like = like_contains(text)
    use_fts = can_use_trigram(text)
    phrase = fts_phrase(text) if use_fts else ""

    authors: list[dict] = []
    tags: list[dict] = []
    content: list[dict] = []
    comments: list[dict] = []

    try:
        fetch_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        fetch_limit = 15

    with conn() as c:
        if scope in ("all", "author"):
            if use_fts:
                rows = c.execute(
                    "SELECT source, uid, MAX(author) AS author, MAX(avatar) AS avatar, COUNT(*) AS n "
                    "FROM posts "
                    "WHERE rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?) AND uid != '' "
                    "GROUP BY source, uid "
                    "ORDER BY n DESC, MAX(created_at) DESC LIMIT ?",
                    (f"{{author uid}} : {phrase}", fetch_limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT source, uid, MAX(author) AS author, MAX(avatar) AS avatar, COUNT(*) AS n "
                    "FROM posts "
                    "WHERE (author LIKE ? ESCAPE '\\' OR uid LIKE ? ESCAPE '\\') AND uid != '' "
                    "GROUP BY source, uid "
                    "ORDER BY n DESC, MAX(created_at) DESC LIMIT ?",
                    (like, like, fetch_limit),
                ).fetchall()
            authors = [
                {
                    "uid": author_key(r["source"], r["uid"]),
                    "source_uid": r["uid"],
                    "source": r["source"] or "weibo",
                    "author": r["author"] or "",
                    "avatar": r["avatar"] or "",
                    "n": r["n"] or 0,
                }
                for r in rows
            ]

        if scope in ("all", "tag"):
            rows = c.execute(
                "SELECT tag, COUNT(*) AS n "
                "FROM post_tags "
                "WHERE tag LIKE ? ESCAPE '\\' "
                "GROUP BY tag "
                "ORDER BY n DESC LIMIT ?",
                (like, fetch_limit),
            ).fetchall()
            tags = [{"tag": r["tag"], "n": r["n"] or 0} for r in rows]

        if scope in ("all", "text"):
            text_where = (
                "p.rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?)"
                if use_fts
                else "p.text LIKE ? ESCAPE '\\'"
            )
            text_arg = f"text : {phrase}" if use_fts else like
            rows = c.execute(
                "WITH hits AS ("
                "SELECT p.sid, p.author, p.avatar, p.text, p.created_at "
                "FROM posts p "
                f"WHERE {text_where} "
                "ORDER BY p.created_at DESC LIMIT ?"
                ") "
                "SELECT h.sid, h.author, h.avatar, h.text, h.created_at, pic.thumb, pic.file "
                "FROM hits h "
                "LEFT JOIN pics pic ON pic.id = (SELECT id FROM pics WHERE sid = h.sid ORDER BY position, id LIMIT 1) "
                "ORDER BY h.created_at DESC",
                (text_arg, fetch_limit),
            ).fetchall()
            content = [
                {
                    "sid": r["sid"],
                    "author": r["author"] or "",
                    "avatar": r["avatar"] or "",
                    "text": r["text"] or "",
                    "created_at": r["created_at"] or "",
                    "thumb": r["thumb"] or r["file"] or "",
                }
                for r in rows
            ]

        if scope in ("all", "comment"):
            cmt_where = (
                "pc.id IN (SELECT rowid FROM post_comments_fts WHERE post_comments_fts MATCH ?)"
                if use_fts
                else "pc.body LIKE ? ESCAPE '\\'"
            )
            cmt_arg = f"body : {phrase}" if use_fts else like
            rows = c.execute(
                "WITH hits AS ("
                "SELECT pc.id AS comment_id, pc.sid, pc.body, p.author, p.avatar, pc.created_at "
                "FROM post_comments pc "
                "JOIN posts p ON p.sid = pc.sid "
                f"WHERE {cmt_where} "
                "ORDER BY pc.created_at DESC LIMIT ?"
                ") "
                "SELECT h.comment_id, h.sid, h.body, h.author, h.avatar, h.created_at, pic.thumb, pic.file "
                "FROM hits h "
                "LEFT JOIN pics pic ON pic.id = (SELECT id FROM pics WHERE sid = h.sid ORDER BY position, id LIMIT 1) "
                "ORDER BY h.created_at DESC",
                (cmt_arg, fetch_limit),
            ).fetchall()
            comments = [
                {
                    "sid": r["sid"],
                    "comment_id": r["comment_id"],
                    "body": r["body"] or "",
                    "author": r["author"] or "",
                    "avatar": r["avatar"] or "",
                    "created_at": r["created_at"] or "",
                    "thumb": r["thumb"] or r["file"] or "",
                }
                for r in rows
            ]

    return {
        "ok": True,
        "authors": authors,
        "tags": tags,
        "content": content,
        "comments": comments,
    }


def review_candidates(limit: int, window_days: int = 7) -> list[str]:
    """回看选组：候选=保留帖，7 天内出场过的不重复入选。

    名额按 2:3 分给「点赞次数≥2」与「次数=1」两组：前者按点赞降序（同次数上次
    出场最早优先），后者按保留时间最早优先；某组不足额时余额划给另一组。
    返回 sid 清单，≥2 组在前、=1 组在后（即页面展示顺序）。
    """
    limit = max(1, min(limit, 200))
    cutoff = (datetime.now(config.NOW_TZ) - timedelta(days=window_days))\
        .isoformat(timespec="seconds")
    fresh = "(p.last_shown_at IS NULL OR p.last_shown_at < ?)"
    quota_a = limit * 2 // 5
    with conn() as c:
        a = [r[0] for r in c.execute(
            "SELECT p.sid FROM posts p WHERE p.status='kept' AND COALESCE(p.keep_count,1) >= 2 "
            f"AND {fresh} ORDER BY p.keep_count DESC, COALESCE(p.last_shown_at,'') ASC, p.sid "
            "LIMIT ?", (cutoff, quota_a))]
        b = [r[0] for r in c.execute(
            "SELECT p.sid FROM posts p WHERE p.status='kept' AND COALESCE(p.keep_count,1) = 1 "
            f"AND {fresh} ORDER BY COALESCE(p.kept_at,'') ASC, p.sid LIMIT ?",
            (cutoff, limit - len(a)))]
        if len(b) < limit - len(a):   # =1 组见底：余额划回 ≥2 组（两向互济）
            a += [r[0] for r in c.execute(
                "SELECT p.sid FROM posts p WHERE p.status='kept' AND COALESCE(p.keep_count,1) >= 2 "
                f"AND {fresh} ORDER BY p.keep_count DESC, COALESCE(p.last_shown_at,'') ASC, p.sid "
                "LIMIT ? OFFSET ?", (cutoff, limit - len(a) - len(b), len(a)))]
    return (a + b)[:limit]


def _db_signature(c: sqlite3.Connection) -> tuple | None:
    if c.in_transaction:
        return None
    p = config.DB_PATH
    try:
        st = os.stat(p)
        db_sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    try:
        wst = os.stat(f"{p}-wal")
        wal_sig = (wst.st_mtime_ns, wst.st_size)
    except OSError:
        wal_sig = None
    dv = c.execute("PRAGMA data_version").fetchone()[0]
    prev_changes = getattr(_conn_local, "shuffle_changes", None)
    prev_dv = getattr(_conn_local, "shuffle_dv", None)
    if (prev_changes is not None and c.total_changes != prev_changes) or (
        prev_dv is not None and dv != prev_dv
    ):
        _bump_shuffle_epoch()
    _conn_local.shuffle_changes = c.total_changes
    _conn_local.shuffle_dv = dv
    return (_shuffle_epoch, str(p), db_sig, wal_sig)


def shuffled_sids(where: str, args: list, seed: str) -> list[tuple[str, str]]:
    """随机档的虚拟全序：命中筛选的 sid 按 sha256(seed⊕sid) 升序排列。

    顺序是 (seed, 筛选集) 的纯函数——哈希直排不经过 RNG，任何机器任何时刻算出的
    全序一致（多设备「每天一换」同洗法的前提）；同 seed 同筛选 → 全序恒定，keyed
    游标二分翻页不重不漏，中途集合增减也不漂移（锚是 key，不依赖行还在不在）；
    换 seed 即整套重排。返回 [(key, sid), ...] 按 key 升序，key 同时是该位次的
    游标锚。随机档只影响展示序：回看选组、批量导出等业务序不受它牵动。
    """
    with conn() as c:
        sig = _db_signature(c)
        cache_key = (str(config.DB_PATH), where, tuple(args), seed)
        if sig is not None:
            with _shuffle_lock:
                hit = _SHUFFLE_CACHE.get(cache_key)
                if hit is not None and hit[0] == sig:
                    _SHUFFLE_CACHE.move_to_end(cache_key)
                    return list(hit[1])
        rows = c.execute(f"SELECT p.sid FROM posts p WHERE {where}", args).fetchall()
    keyed = [(hashlib.sha256(f"{seed}\x00{sid}".encode()).hexdigest(), sid)
             for (sid,) in rows]
    keyed.sort()
    if sig is not None:
        with _shuffle_lock:
            _SHUFFLE_CACHE[cache_key] = (sig, list(keyed))
            _SHUFFLE_CACHE.move_to_end(cache_key)
            while len(_SHUFFLE_CACHE) > _SHUFFLE_CACHE_MAX:
                _SHUFFLE_CACHE.popitem(last=False)
    return keyed


def record_shown(sids: list[str]) -> None:
    """回看出场记账：shown_count+1、刷新 last_shown_at、追加事件，单事务提交。"""
    if not sids:
        return
    now = datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
    with _write_lock, conn() as c:
        c.executemany("UPDATE posts SET shown_count=COALESCE(shown_count,0)+1, last_shown_at=? "
                      "WHERE sid=?", [(now, s) for s in sids])
        c.executemany("INSERT INTO review_events(sid,action,created_at) VALUES(?,'shown',?)",
                      [(s, now) for s in sids])


def reaffirm(sid: str) -> dict | None:
    """点赞：keep_count+1、刷新 last_confirm_at、记事件。非保留帖拒绝。"""
    return _review_count(sid, +1, "reaffirm")


def undo_reaffirm(sid: str) -> dict | None:
    """撤销点赞：keep_count−1（不低于建账的 1）、记事件。"""
    return _review_count(sid, -1, "undo")


def _same_cn_day(iso: str | None, now: datetime) -> bool:
    """时间戳是否与 now 同一个东八区日（老数据无时区按东八区补齐再比）。"""
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.NOW_TZ)
    return dt.astimezone(config.NOW_TZ).date() == now.date()


def _review_count(sid: str, delta: int, action: str) -> dict | None:
    now = datetime.now(config.NOW_TZ)
    now_iso = now.isoformat(timespec="seconds")
    with _write_lock, conn() as c:
        row = c.execute("SELECT status, COALESCE(keep_count,1) keep_count, last_confirm_at "
                        "FROM posts WHERE sid=?", (sid,)).fetchone()
        if not row:
            return None
        if row["status"] != "kept":
            return {"error": "只有保留的帖可以点赞"}
        if delta < 0 and row["keep_count"] <= 1:
            return {"error": "没有可撤销的点赞"}
        if delta > 0:
            # 每帖每天最多 +1 一次（点赞语义，服务层统一拦截：卡上/回看/放映/详情同限；
            # −1 不清当日账，撤销后同日仍不可再赞）
            if _same_cn_day(row["last_confirm_at"], now):
                return {"error": "今天已经赞过了，明天再来"}
            c.execute("UPDATE posts SET keep_count=keep_count+1, last_confirm_at=? WHERE sid=?",
                      (now_iso, sid))
        else:
            c.execute("UPDATE posts SET keep_count=keep_count-1 WHERE sid=?", (sid,))
        c.execute("INSERT INTO review_events(sid,action,created_at) VALUES(?,?,?)",
                  (sid, action, now_iso))
        count = c.execute("SELECT keep_count FROM posts WHERE sid=?", (sid,)).fetchone()[0]
    return {"keep_count": count}


def get_post(sid: str) -> dict | None:
    """单帖详情（含图片行与分组名），给详情页与批量打包用。"""
    with conn() as c:
        p = c.execute(
            "SELECT p.*, COALESCE(g.name, p.list_id) group_name FROM posts p "
            "LEFT JOIN groups g ON g.list_id = p.list_id WHERE p.sid=?", (sid,)).fetchone()
        if not p:
            return None
        pics = c.execute("SELECT * FROM pics WHERE sid=? ORDER BY position,id", (sid,)).fetchall()
        groups = c.execute("SELECT list_id FROM post_groups WHERE sid=?", (sid,)).fetchall()
        observations = c.execute("SELECT list_id,via,text FROM post_observations WHERE sid=? ORDER BY observed_at DESC", (sid,)).fetchall()
        comments = c.execute("SELECT id,body,created_at,updated_at FROM post_comments WHERE sid=? ORDER BY id", (sid,)).fetchall()
        tags = c.execute("SELECT tag FROM post_tags WHERE sid=? ORDER BY created_at,tag", (sid,)).fetchall()
    d = dict(p)
    d["pics"] = [dict(r) for r in pics]
    d["groups"] = [r[0] for r in groups]
    d["observations"] = [dict(r) for r in observations]
    d["comments"] = [dict(r) for r in comments]
    d["tags"] = [r[0] for r in tags]
    return d


def list_authors(group: str = "all", status: str = "all", source: str = "all") -> list[dict]:
    """作者聚合：n = 该作者在指定状态（标签页）下的帖数，可能为 0 也照常返回。

    last_at = 该作者最新一帖时间（ISO 串，供「发布时间」排序）；排序交给调用方。
    source='x'/'weibo' 只聚合该来源（与列表的来源筛选同口径，空串归微博）。
    """
    where, args = "uid != ''", []
    if source in ("weibo", "x"):
        where += " AND COALESCE(NULLIF(source,''),'weibo') = ?"
        args.append(source)
    if group != "all":
        where += " AND EXISTS(SELECT 1 FROM post_groups pg WHERE pg.sid=posts.sid AND pg.list_id=?)"
        args.append(group)
    if status == "all":
        n_expr, nargs = "COUNT(*)", []
    elif status == "favorites":
        # 收藏是正交标记：作者条在该页按 favorite=1 计数（跨三种 decision）
        n_expr, nargs = "SUM(CASE WHEN favorite=1 THEN 1 ELSE 0 END)", []
    else:
        n_expr, nargs = "SUM(CASE WHEN status = ? THEN 1 ELSE 0 END)", [status]
    with conn() as c:
        if group == "all":
            # 两段聚合：全表顺序 GROUP BY（+source,+uid 避开非覆盖随机寻址退化），
            # 再按 idx_posts_src_uid_latest 为每作者寻址最新 1 帖拿 author/avatar。
            rows = c.execute(
                f"WITH agg AS (SELECT source,uid,MAX(created_at) last_at,{n_expr} n,"
                f"(SELECT p2.sid FROM posts p2 WHERE p2.source=posts.source AND p2.uid=posts.uid "
                f"ORDER BY p2.created_at DESC,p2.fetched_at DESC,p2.sid LIMIT 1) lsid "
                f"FROM posts WHERE {where} GROUP BY +source,+uid) "
                f"SELECT a.source,a.uid,p.author,p.avatar,a.last_at,a.n "
                f"FROM agg a JOIN posts p ON p.sid=a.lsid",
                nargs + args,
            ).fetchall()
        else:
            rows = c.execute(
                f"WITH ranked AS (SELECT source,uid,author,avatar,created_at,status,favorite,"
                f"ROW_NUMBER() OVER(PARTITION BY source,uid ORDER BY created_at DESC,fetched_at DESC,sid) rn "
                f"FROM posts WHERE {where}) "
                f"SELECT source,uid,MAX(CASE WHEN rn=1 THEN author END) author,"
                f"MAX(CASE WHEN rn=1 THEN avatar END) avatar,MAX(created_at) last_at,{n_expr} n "
                f"FROM ranked GROUP BY source,uid",
                args + nargs,
            ).fetchall()
    return [{**dict(r), "source_uid": r["uid"], "uid": author_key(r["source"], r["uid"])} for r in rows]


def author_profile(source: str, uid: str) -> dict | None:
    """作者全量档案（博主视图头）：跨状态、跨分组聚合 + 最新一帖的身份信息。

    author/avatar/handle 与 list_authors 的 rn=1 同口径取最新一帖上的值；
    comments = 该作者帖子上的批注数（批注豁免硬删，可与可见帖数脱节）。
    无帖返回 None。
    """
    cond = "uid=? AND COALESCE(NULLIF(source,''),'weibo')=?"
    args = (uid, source)
    with conn() as c:
        agg = c.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(status='kept'),0) kept,"
            f" COALESCE(SUM(favorite),0) favorites, MIN(created_at) first_at,"
            f" MAX(created_at) last_at FROM posts WHERE {cond}", args).fetchone()
        if not agg or not agg["n"]:
            return None
        ident = c.execute(
            f"SELECT author, avatar, COALESCE(NULLIF(handle,''),'') handle FROM posts"
            f" WHERE {cond} ORDER BY created_at DESC, fetched_at DESC, sid DESC LIMIT 1",
            args).fetchone()
        comments = c.execute(
            "SELECT COUNT(*) n FROM post_comments pc JOIN posts p ON p.sid = pc.sid"
            f" WHERE p.{cond}", args).fetchone()["n"]
    return {"source": source, "uid": uid, **dict(ident), **dict(agg), "comments": comments}


def x_handle_for_uid(uid: str) -> str:
    """rest_id → 最近一帖上的 handle（UserByScreenName 以 handle 为入参）。"""
    with conn() as c:
        row = c.execute(
            "SELECT COALESCE(NULLIF(handle,''),'') FROM posts"
            " WHERE uid=? AND COALESCE(NULLIF(source,''),'weibo')='x'"
            " ORDER BY created_at DESC, fetched_at DESC, sid DESC LIMIT 1", (uid,)).fetchone()
    return row[0] if row else ""


def _kiosk_group_context(c) -> tuple[set[str], list[dict], dict[tuple[str, str], set[str]], dict[str, list[str]]]:
    """加载报刊亭博主大厅的分组上下文：活跃分组清单、按作者(source,uid)聚合的分组集合与 xg 成员映射。"""
    group_rows = c.execute(
        "SELECT list_id,name,active FROM groups ORDER BY added_at"
    ).fetchall()
    active_set: set[str] = set()
    display_groups: list[dict] = []
    for r in group_rows:
        lid = str(r["list_id"])
        if not r["active"]:
            continue
        active_set.add(lid)
        if lid == "demo" or lid.startswith(("xu:", "wbu:")):
            continue
        src = "x" if lid.startswith(("x:", "xg:")) else "weibo"
        display_groups.append({
            "list_id": lid,
            "name": r["name"],
            "source": src,
            "is_local": lid.startswith("xg:"),
        })
    display_set = {g["list_id"] for g in display_groups}

    author_groups: dict[tuple[str, str], set[str]] = {}
    if display_set:
        for r in c.execute(
            "SELECT DISTINCT p.source,p.uid,pg.list_id FROM post_groups pg "
            "JOIN posts p ON p.sid=pg.sid WHERE p.uid != '' "
            "UNION "
            "SELECT DISTINCT source,uid,list_id FROM posts WHERE uid != '' AND COALESCE(list_id,'') != ''"
        ):
            lid = str(r["list_id"])
            if lid in display_set:
                src = "x" if r["source"] == "x" else "weibo"
                author_groups.setdefault((src, r["uid"]), set()).add(lid)

    member_xgs: dict[str, list[str]] = {}
    for r in c.execute("SELECT list_id,member_id FROM group_members ORDER BY added_at"):
        lid = str(r["list_id"])
        if lid in display_set:
            member_xgs.setdefault(str(r["member_id"]), []).append(lid)
    return active_set, display_groups, author_groups, member_xgs


def kiosk_meta() -> dict:
    """报刊亭管理大厅外围元数据：当前可筛选与归属的活跃分组。"""
    with conn() as c:
        _, display_groups, _, _ = _kiosk_group_context(c)
    return {"groups": display_groups}


def kiosk_shelf() -> list[dict]:
    """报刊亭书架：每位博主一本「杂志」——统计、节奏、封面/内页图、分组与关注状态。

    与 list_authors 同口径分作者（source,uid）；l30 按 SQLite 的 ISO 含时区解析。
    thumbs 取最新 4 帖的缩略图（/media/ 相对路径）；bio 只读 meta 缓存，本接口不出网。
    avatar 只在本地已有（/media/）时返回，远程 URL 留空由前端降级。
    """
    with conn() as c:
        rows = c.execute(
            "WITH agg AS ("
            "  SELECT source,uid,MIN(created_at) first_at,MAX(created_at) last_at,COUNT(*) n,"
            "  COALESCE(SUM(status='kept'),0) kept,COALESCE(SUM(favorite),0) favorites,"
            "  COALESCE(SUM(datetime(created_at) >= datetime('now','-30 days')),0) l30,"
            "  (SELECT p2.sid FROM posts p2 WHERE p2.source=posts.source AND p2.uid=posts.uid "
            "   ORDER BY p2.created_at DESC,p2.fetched_at DESC,p2.sid LIMIT 1) lsid "
            "  FROM posts WHERE uid != '' GROUP BY +source,+uid"
            ") "
            "SELECT a.source,a.uid,p.author,p.avatar,"
            "COALESCE(NULLIF(p.handle,''),'') handle,p.text,"
            "a.first_at,a.last_at,a.n,a.kept,a.favorites,a.l30 "
            "FROM agg a JOIN posts p ON p.sid=a.lsid"
        ).fetchall()
        picks = c.execute(
            "SELECT p.source,p.uid,pc.thumb "
            "FROM (SELECT DISTINCT source,uid FROM posts WHERE uid != '') a "
            "JOIN posts p ON p.sid IN ("
            "  SELECT p2.sid FROM posts p2 "
            "  WHERE p2.source=a.source AND p2.uid=a.uid "
            "    AND EXISTS(SELECT 1 FROM pics px WHERE px.sid=p2.sid AND px.thumb!='') "
            "  ORDER BY p2.created_at DESC,p2.fetched_at DESC,p2.sid LIMIT 4"
            ") "
            "JOIN pics pc ON pc.sid=p.sid "
            "WHERE pc.thumb != '' "
            "ORDER BY p.source,p.uid,p.created_at DESC,p.fetched_at DESC,p.sid,pc.position,pc.id"
        ).fetchall()
        bios = dict(c.execute("SELECT k,v FROM meta WHERE k LIKE 'author_bio:%'").fetchall())
        active_set, display_groups, author_groups, member_xgs = _kiosk_group_context(c)
    group_order = {g["list_id"]: idx for idx, g in enumerate(display_groups)}
    thumbs: dict[tuple[str, str], list[str]] = {}
    for r in picks:
        bucket = thumbs.setdefault((r["source"], r["uid"]), [])
        if len(bucket) < 4:
            bucket.append("/media/" + r["thumb"])
    shelf = []
    for r in rows:
        src = "x" if r["source"] == "x" else "weibo"
        key = author_key(r["source"], r["uid"])
        handle = r["handle"] or ""
        follow_id = f"xu:{handle.lower()}" if (src == "x" and handle) else (f"wbu:{r['uid']}" if src == "weibo" else "")
        followed = bool(follow_id and follow_id in active_set)
        xg_groups = list(member_xgs.get(follow_id, [])) if follow_id.startswith("xu:") else []
        gset = set(author_groups.get((src, r["uid"]), ())) | set(xg_groups)
        groups = sorted(gset, key=lambda lid: group_order.get(lid, 9999))
        shelf.append({
            "key": key, "source": src, "uid": r["uid"],
            "author": r["author"], "handle": handle,
            "text": (r["text"] or "").strip(),
            "avatar": r["avatar"] if (r["avatar"] or "").startswith("/media/") else "",
            "thumbs": thumbs.get((r["source"], r["uid"]), []),
            "n": r["n"], "kept": r["kept"], "favorites": r["favorites"], "l30": r["l30"],
            "first_at": (r["first_at"] or "")[:10], "last_at": (r["last_at"] or "")[:10],
            "bio": bios.get(f"author_bio:{key}", ""),
            "follow_id": follow_id, "followed": followed,
            "groups": groups, "xg_groups": xg_groups,
        })
    shelf.sort(key=lambda d: d["last_at"], reverse=True)
    return shelf


def stats_report() -> dict:
    """回看统计报告（/stats 页数据源）：总览、近 30 天趋势、Top 博主、最常点赞、回看累计。

    全部纯本地聚合；日界用 created_at >= day30 走 idx_posts_created / idx_review_events_action_created
    范围扫描，Top 博主先 GROUP BY 选前 8 再回表取身份。
    """
    day30 = (datetime.now(config.NOW_TZ) - timedelta(days=30)).strftime("%Y-%m-%d")
    with conn() as c:
        overview = dict(c.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(status='pending'),0) pending,"
            " COALESCE(SUM(status='kept'),0) kept, COALESCE(SUM(status='discarded'),0) discarded,"
            " COALESCE(SUM(favorite),0) favorites FROM posts").fetchone())
        overview["pics"] = c.execute("SELECT COUNT(*) FROM pics").fetchone()[0]
        overview["comments"] = c.execute("SELECT COUNT(*) FROM post_comments").fetchone()[0]
        overview["tags"] = c.execute("SELECT COUNT(DISTINCT tag) FROM post_tags").fetchone()[0]
        daily = {r["d"]: {"d": r["d"], "n": r["n"], "likes": 0} for r in c.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) n FROM posts "
            "WHERE created_at >= ? GROUP BY d", (day30,))}
        for r in c.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) n FROM review_events "
                "WHERE action='reaffirm' AND created_at >= ? GROUP BY d", (day30,)):
            daily.setdefault(r["d"], {"d": r["d"], "n": 0, "likes": 0})["likes"] = r["n"]
        top_authors = c.execute(
            "WITH top8 AS ("
            "  SELECT source,uid,COUNT(*) n,COALESCE(SUM(status='kept'),0) kept,"
            "  COALESCE(SUM(favorite),0) favorites FROM posts WHERE uid != '' "
            "  GROUP BY +source,+uid ORDER BY kept DESC, favorites DESC, n DESC LIMIT 8"
            ") "
            "SELECT t.source,t.uid,p.author,p.avatar,t.n,t.kept,t.favorites "
            "FROM top8 t JOIN posts p ON p.sid = ("
            "  SELECT p2.sid FROM posts p2 WHERE p2.source=t.source AND p2.uid=t.uid "
            "  ORDER BY p2.created_at DESC,p2.fetched_at DESC,p2.sid LIMIT 1"
            ") ORDER BY t.kept DESC, t.favorites DESC, t.n DESC"
        ).fetchall()
        top_posts = c.execute(
            "SELECT p.sid,p.author,p.avatar,p.keep_count,p.text,"
            " (SELECT thumb FROM pics WHERE sid=p.sid ORDER BY position,id LIMIT 1) thumb "
            "FROM posts p WHERE p.status='kept' "
            "ORDER BY p.keep_count DESC, p.created_at DESC LIMIT 9").fetchall()
        review = dict(c.execute(
            "SELECT COALESCE(SUM(action='shown'),0) shown,"
            " COALESCE(SUM(action='reaffirm'),0) reaffirmed,"
            " COUNT(DISTINCT substr(created_at,1,10)) active_days "
            "FROM review_events").fetchone())
    top = []
    for r in top_authors:
        top.append({
            "key": author_key(r["source"], r["uid"]),
            "author": r["author"],
            "avatar": r["avatar"] or "",   # 原始路径/远程 URL，映射归前端（avatarSrc 同规则）
            "n": r["n"], "kept": r["kept"], "favorites": r["favorites"],
        })
    return {
        "overview": overview,
        "daily": [daily[d] for d in sorted(daily)],
        "top_authors": top,
        "top_posts": [dict(r) for r in top_posts],
        "review": review,
    }


def active_groups(source: str | None = None) -> list[dict]:
    """活跃分组；source 过滤（weibo 的抓取入口不该碰到 x: 前缀的分组，反之亦然）。
    xg: 本地组归 x 类（回补/抓取入口按成员解包），但它自身不是抓取目标。"""
    with conn() as c:
        rows = c.execute(
            "SELECT list_id,name FROM groups WHERE active=1 ORDER BY added_at").fetchall()
    # 演示分组（list_id=demo）只用于界面预览，不进任何抓取队列
    out = []
    for r in rows:
        lid = str(r["list_id"])
        if lid == "demo":
            continue
        src = "x" if lid.startswith(("x:", "xu:", "xg:")) else "weibo"
        if source and src != source:
            continue
        out.append({"list_id": lid, "name": r["name"], "source": src})
    return out


def add_group(list_id: str, name: str) -> None:
    from datetime import datetime
    with _write_lock, conn() as c:
        c.execute("INSERT INTO groups(list_id,name,added_at,active) VALUES(?,?,?,1) "
                  "ON CONFLICT(list_id) DO UPDATE SET name=excluded.name, active=1",
                  (list_id, name, datetime.now().isoformat(timespec="seconds")))


def remove_group(list_id: str) -> None:
    with _write_lock, conn() as c:
        c.execute("UPDATE groups SET active=0 WHERE list_id=?", (list_id,))
        # xg: 本地组连带清理成员映射与该组的帖子归组行（帖子本身不动，回到各成员目标下）
        if str(list_id).startswith("xg:"):
            c.execute("DELETE FROM group_members WHERE list_id=?", (list_id,))
            c.execute("DELETE FROM post_groups WHERE list_id=?", (list_id,))
    _bump_shuffle_epoch()


# ---------- X 本地分组（xg: 组与成员映射） ----------

def group_members(list_id: str) -> list[dict]:
    """组成员（带抓取目标名）；组不存在或空组都返回空列表。"""
    with conn() as c:
        rows = c.execute(
            "SELECT gm.member_id, COALESCE(g.name, gm.member_id) name "
            "FROM group_members gm LEFT JOIN groups g ON g.list_id=gm.member_id "
            "WHERE gm.list_id=? ORDER BY gm.added_at", (list_id,)).fetchall()
    return [{"member_id": r["member_id"], "name": r["name"]} for r in rows]


def all_group_members() -> list[dict]:
    """全部组员关系（管理弹窗一次取齐；数据量=自定义组×成员，很小）。"""
    with conn() as c:
        rows = c.execute("SELECT list_id,member_id FROM group_members ORDER BY list_id,added_at").fetchall()
    return [{"list_id": r["list_id"], "member_id": r["member_id"]} for r in rows]


def member_groups(member_id: str) -> list[str]:
    """某抓取目标所属的本地组 id 列表。"""
    with conn() as c:
        return [r[0] for r in c.execute(
            "SELECT list_id FROM group_members WHERE member_id=?", (member_id,))]


def add_group_member(list_id: str, member_id: str) -> None:
    """加入成员并回填存量帖归组（幂等）：该目标已有的全部帖子挂进本组。"""
    now = datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
    with _write_lock, conn() as c:
        c.execute("INSERT OR IGNORE INTO group_members VALUES(?,?,?)",
                  (list_id, member_id, now))
        c.execute("INSERT OR IGNORE INTO post_groups "
                  "SELECT p.sid, ? FROM posts p WHERE p.list_id=?", (list_id, member_id))
    _bump_shuffle_epoch()


def remove_group_member(list_id: str, member_id: str) -> None:
    """移出成员：只解除映射与后续归组；已归组的历史帖子保留（采集事实，见设计决定）。"""
    with _write_lock, conn() as c:
        c.execute("DELETE FROM group_members WHERE list_id=? AND member_id=?",
                  (list_id, member_id))


def get_meta(k: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(k: str, v: str) -> None:
    with _write_lock, conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
