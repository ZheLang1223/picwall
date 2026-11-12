"""schema 迁移回归测试：v1 旧库（无 favorite/source）→ init_db 升级到 v2。

背景：9/18 出过一次 sqlite 页损坏，用户数据只有备份保命；任何迁移必须先证明
「老库升级不丢数据、可重复执行」。v1 DDL 对应 favorite/source 引入前的表结构。
"""
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import config, db

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  sid        TEXT PRIMARY KEY,
  list_id    TEXT,
  uid        TEXT,
  author     TEXT,
  avatar     TEXT,
  text       TEXT,
  created_at TEXT,
  fetched_at TEXT,
  weibo_url  TEXT,
  via        TEXT,
  status     TEXT DEFAULT 'pending',
  kept_at    TEXT
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
CREATE INDEX IF NOT EXISTS idx_pics_sid ON pics(sid);
CREATE INDEX IF NOT EXISTS idx_posts_list ON posts(list_id);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE TABLE IF NOT EXISTS groups (
  list_id  TEXT PRIMARY KEY,
  name     TEXT,
  added_at TEXT,
  active   INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

_V1_ROWS = [
    # sid, list_id, uid, author, text, status, kept_at  （discarded_at 尚未存在的老库：
    # 迁移后应给 discarded 行补计时起点，pending/kept 行保持 NULL）
    ("w1", "g1", "1001", "作者甲", "保留帖", "kept", "2026-09-10T09:00:00"),
    ("w2", "g1", "1002", "作者乙", "待处理帖", "pending", None),
    ("w3", "g2", "1003", "作者丙", "已删帖", "discarded", None),
]


class MigrationBase(unittest.TestCase):
    """与 test_api 同款五元组替换，但 setUp **不跑** init_db——先手工建 v1 旧库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-mig-"))
        self._old = (config.DATA_DIR, config.PICS_DIR, config.EXPORT_DIR,
                     config.DB_PATH, config.COOKIE_FILE)
        config.DATA_DIR = self.tmp
        config.PICS_DIR = self.tmp / "pics"
        config.EXPORT_DIR = self.tmp / "export"
        config.DB_PATH = self.tmp / "picwall.db"
        config.COOKIE_FILE = self.tmp / "cookie.txt"

    def tearDown(self):
        (config.DATA_DIR, config.PICS_DIR, config.EXPORT_DIR,
         config.DB_PATH, config.COOKIE_FILE) = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_v1_db(self, duplicate_pics=False):
        with closing(sqlite3.connect(config.DB_PATH)) as c, c:
            c.executescript(_V1_SCHEMA)
            for sid, lid, uid, author, text, status, kept in _V1_ROWS:
                c.execute(
                    "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
                    "fetched_at,weibo_url,via,status,kept_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, lid, uid, author, "", text, "2026-09-10T08:00:00+08:00",
                     "2026-09-10T08:00:00+08:00", f"https://weibo.com/u/{sid}", "", status, kept))
            c.execute("INSERT INTO groups(list_id,name,added_at,active) VALUES('g1','分组一','2026-09-01T00:00:00',1)")
            c.execute("INSERT INTO meta(k,v) VALUES('legacy_key','legacy_value')")
            pairs = [("w1", "a.jpg"), ("w2", "b.jpg")]
            if duplicate_pics:
                pairs.append(("w1", "a.jpg"))
            c.executemany("INSERT INTO pics(sid,file,thumb,w,h,url) VALUES(?,?,?,?,?,?)",
                          [(s, f, f"t_{f}", 10, 10, "") for s, f in pairs])


class V1ToV2MigrationTest(MigrationBase):
    def test_upgrade_backfills_source_and_preserves_data(self):
        self.make_v1_db()
        db.init_db()
        with closing(sqlite3.connect(config.DB_PATH)) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        self.assertIn("favorite", cols)
        self.assertIn("source", cols)
        with db.conn() as c:
            rows = {r["sid"]: r for r in c.execute("SELECT * FROM posts").fetchall()}
        # 存量全部回填 weibo；favorite 默认 0；正文/作者/状态逐字段不丢
        for sid, row in rows.items():
            self.assertEqual(row["source"], "weibo", sid)
            self.assertIn(row["favorite"], (0, None), sid)
        self.assertEqual(rows["w1"]["author"], "作者甲")
        self.assertEqual(rows["w1"]["status"], "kept")
        self.assertEqual(rows["w3"]["status"], "discarded")
        # schema 版本落 meta；老 meta 不受影响
        self.assertEqual(db.get_meta("schema_version"), str(db._SCHEMA_VERSION))
        self.assertEqual(db.get_meta("legacy_key"), "legacy_value")

    def test_discarded_at_backfill_only_touches_discarded(self):
        self.make_v1_db()
        db.init_db()
        with db.conn() as c:
            rows = {r["sid"]: r for r in c.execute("SELECT * FROM posts").fetchall()}
        self.assertIsNotNone(rows["w3"]["discarded_at"])   # 已删行补计时起点
        self.assertIsNone(rows["w1"]["discarded_at"])      # kept/pending 不误补
        self.assertIsNone(rows["w2"]["discarded_at"])

    def test_upgrade_is_idempotent(self):
        self.make_v1_db()
        db.init_db()
        db.init_db()   # 再跑一遍：不抛错、版本不变、行数不变
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pics").fetchone()[0], 2)
        self.assertEqual(db.get_meta("schema_version"), str(db._SCHEMA_VERSION))

    def test_legacy_duplicate_pics_still_upgradeable(self):
        # 老库重复关系合并后必须具备唯一索引，迁移结果确定
        self.make_v1_db(duplicate_pics=True)
        db.init_db()
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pics").fetchone()[0], 2)

    def test_new_inserts_default_to_weibo_source(self):
        self.make_v1_db()
        db.init_db()
        db.insert_post({
            "sid": "new1", "uid": "9", "author": "新人", "avatar": "", "text": "t",
            "created_at": "2026-09-19T00:00:00+08:00", "fetched_at": "2026-09-19T00:00:00+08:00",
            "weibo_url": "https://weibo.com/9/new1", "via": "", "is_long": 0,
        }, "g1")
        with db.conn() as c:
            self.assertEqual(
                c.execute("SELECT source FROM posts WHERE sid='new1'").fetchone()[0], "weibo")


if __name__ == "__main__":
    unittest.main()
