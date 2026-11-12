"""数据库约束与异常旧库的确定结果。"""
import sqlite3

from app import db
from tests.test_api import ApiTestBase


class InvariantTest(ApiTestBase):
    def test_invalid_status_favorite_and_timestamps_are_rejected(self):
        for sql in ("UPDATE posts SET status='bad' WHERE sid='demo0000'",
                    "UPDATE posts SET favorite=3 WHERE sid='demo0000'",
                    "UPDATE posts SET status='kept',kept_at=NULL WHERE sid='demo0000'",
                    "UPDATE posts SET status='discarded',discarded_at='bad' WHERE sid='demo0000'",
                    "UPDATE posts SET kept_at='2026-10-01' WHERE sid='demo0000'"):
            with self.assertRaises(sqlite3.IntegrityError), db.conn() as c:
                c.execute(sql)
        self.assertEqual(db.get_post("demo0000")["status"], "pending")

    def test_failed_migration_does_not_advertise_new_version(self):
        with db.conn() as c:
            c.execute("DROP TRIGGER validate_posts_update")
            c.execute("UPDATE posts SET favorite=99 WHERE sid='demo0000'")
            c.execute("UPDATE meta SET v='3' WHERE k='schema_version'")
        with self.assertRaises(ValueError):
            db.init_db()
        self.assertEqual(db.get_meta("schema_version"), "3")

    def test_unique_media_relationship_is_enforced_after_migration(self):
        db.init_db()
        p = db.get_post("demo0000")["pics"][0]
        with self.assertRaises(sqlite3.IntegrityError), db.conn() as c:
                c.execute("INSERT INTO pics(sid,file) VALUES(?,?)", (p["sid"], p["file"]))

    def test_future_schema_is_rejected_without_changing_schema_or_user_records(self):
        with db.conn() as c:
            c.execute("UPDATE meta SET v='99' WHERE k='schema_version'")
            before=c.execute("SELECT sql FROM sqlite_master ORDER BY type,name").fetchall()
        decision=db.get_post("demo0000")
        with self.assertRaisesRegex(ValueError,"更新版本"):
            db.init_db()
        self.assertEqual(db.get_meta("schema_version"),"99")
        self.assertEqual(db.get_post("demo0000"),decision)
        with db.conn() as c:
            self.assertEqual([tuple(r) for r in c.execute("SELECT sql FROM sqlite_master ORDER BY type,name")], [tuple(r) for r in before])

    def test_failed_legacy_migration_rolls_back_tables_columns_and_duplicate_merge(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from tests.test_migration import MigrationBase
        with tempfile.TemporaryDirectory(prefix="picwall-migration-atomic-") as directory:
            path=Path(directory)/"picwall.db"
            from app import config
            with patch.object(config,"DB_PATH",path), patch.object(config,"DATA_DIR",Path(directory)):
                MigrationBase.make_v1_db(self, duplicate_pics=True)
                with db.conn() as c:
                    c.execute("UPDATE posts SET status='broken' WHERE sid='w2'")
                    before=c.execute("SELECT sql FROM sqlite_master ORDER BY type,name").fetchall()
                with self.assertRaises(ValueError):
                    db.init_db()
                with db.conn() as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM pics").fetchone()[0],3)
                    self.assertEqual([tuple(r) for r in c.execute("SELECT sql FROM sqlite_master ORDER BY type,name")], [tuple(r) for r in before])
                    self.assertIsNone(c.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone())
