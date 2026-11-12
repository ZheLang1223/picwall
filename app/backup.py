"""完整增量备份：SQLite 快照 + 独立内容副本 + 路径清单。

blob 库去向由 PICWALL_BLOB_STORE 决定（app/cloud.py）：local=本地目录（历史行为），
quark=夸克网盘异地副本。manifest 永远是 hash 清单，与存放位置无关；restore 逐文件
取回并 sha256 校验，任何一步失败清空恢复目标——全有或全无。

凭据文件（cookie.txt / x_auth.txt）是例外：只进本地快照目录的 local/ 子目录（0600），
永不进 blob 库——云端明文凭据等于账号接管面；restore 对凭据从快照 local/ 回源。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import cloud, config
from .gate import gate


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


CREDENTIAL_FILES = ("cookie.txt", "x_auth.txt")


def latest(root: Path) -> Path:
    batches = sorted(root.glob("picwall-*.snapshot/manifest.json"))
    if not batches:
        raise FileNotFoundError("没有完整图片墙备份，请先运行 make backup")
    return batches[-1].parent


def create(keep: int = 7, store: cloud.LocalBlobs | cloud.QuarkBlobs | None = None) -> dict:
    store = store or cloud.from_config()
    root = config.DATA_DIR / "backups"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    started = datetime.now(config.NOW_TZ)
    name = "picwall-" + started.strftime("%Y%m%d-%H%M%S-%f")
    # hardlink 仅用于临时固定文件版本；blob 由 store 落库（本地复制或云端上传）
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=root) as temp:
        staging = Path(temp)
        with gate:
            with closing(sqlite3.connect(config.DB_PATH)) as src, closing(sqlite3.connect(staging / "picwall.db")) as dst:
                src.backup(dst)
            for folder in (config.PICS_DIR, config.EXPORT_DIR):
                for src in sorted(folder.rglob("*")):
                    if not src.is_file() or src.name.startswith("."):
                        continue
                    if src.is_symlink():
                        raise ValueError("媒体目录含符号链接，停止备份")
                    dst = staging / src.relative_to(config.DATA_DIR)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(src, dst)
                    except OSError:
                        shutil.copy2(src, dst)
            for source in (config.COOKIE_FILE, config.X_AUTH_FILE):
                if source.is_file() and source.parent == config.DATA_DIR:
                    shutil.copy2(source, staging / source.name)
                    os.chmod(staging / source.name, 0o600)
        files = {}
        for src in sorted(staging.rglob("*")):
            if src.is_file():
                sha = digest(src)
                files[src.relative_to(staging).as_posix()] = {"sha256": sha, "size": src.stat().st_size}
        # 凭据不进 blob 库（local 模式也不进，口径统一）：本地快照目录是它们唯一的备份位。
        local_only = sorted(rel for rel in files if rel in CREDENTIAL_FILES)
        store.ensure_many([(e["sha256"], staging / rel) for rel, e in files.items()
                           if rel not in local_only])
        with closing(sqlite3.connect(staging / "picwall.db")) as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库完整性失败")
            decisions = c.execute("SELECT sid,status,kept_at,discarded_at,favorite FROM posts ORDER BY sid").fetchall()
            expected = {"pics/" + p for row in c.execute("SELECT file,thumb FROM pics") for p in row if p}
            expected |= {"pics/" + row[0].removeprefix("/media/") for row in c.execute(
                "SELECT avatar FROM posts WHERE avatar LIKE '/media/%'")}
            missing = sorted(expected - files.keys())
        manifest = {"format": 1, "created_at": started.isoformat(), "files": files,
                    "blob_store": getattr(store, "NAME", "local"),
                    "local_only": local_only,
                    "posts": len(decisions), "decisions_sha256": hashlib.sha256(
                        json.dumps(decisions, ensure_ascii=False).encode()).hexdigest(),
                    "preexisting_missing": missing,
                    "duration_seconds": round((datetime.now(config.NOW_TZ) - started).total_seconds(), 3)}
        batch = root / (name + ".snapshot")
        unpublished = staging / "published"
        unpublished.mkdir()
        (unpublished / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in local_only:
            dst = unpublished / "local" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / rel, dst)
            os.chmod(dst, 0o600)
        os.replace(unpublished, batch)
        shutil.copy2(staging / "picwall.db", root / (name + ".db"))
    for old in sorted(root.glob("picwall-*.snapshot"))[:-keep]:
        shutil.rmtree(old)
        (root / (old.name.removesuffix(".snapshot") + ".db")).unlink(missing_ok=True)
    for old in sorted(root.glob("picwall-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
        for suffix in ("cookie.txt", "x_auth.txt"):
            (root / (old.stem + "." + suffix)).unlink(missing_ok=True)
    return {"batch": str(batch), "files": len(files), "posts": len(decisions),
            "store": manifest["blob_store"], "credentials_local": local_only,
            "preexisting_missing": len(missing), "duration_seconds": manifest["duration_seconds"]}


def restore(batch: Path, target: Path, store: cloud.LocalBlobs | cloud.QuarkBlobs | None = None) -> dict:
    if target.exists() and any(target.iterdir()):
        raise ValueError("恢复目标必须是空目录，禁止覆盖现有数据")
    manifest = json.loads((batch / "manifest.json").read_text())
    for rel, entry in manifest["files"].items():
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError("备份清单含越界路径")
        sha = entry["sha256"]
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValueError("备份清单含无效内容哈希")
    store = store or cloud.from_config()
    started = datetime.now(config.NOW_TZ)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    local_only = set(manifest.get("local_only", []))   # 旧快照无此键：凭据仍走 blob 库
    try:
        for rel, entry in manifest["files"].items():
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if rel in local_only:
                src = batch / "local" / rel
                if not src.is_file():
                    raise ValueError(f"本地凭据缺失（快照目录不完整）：{rel}")
                shutil.copy2(src, dst)
            else:
                store.fetch_into(entry["sha256"], dst)
            os.chmod(dst, 0o600)
            if digest(dst) != entry["sha256"]:
                raise ValueError("备份媒体缺失或校验失败")
        with closing(sqlite3.connect(target / "picwall.db")) as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("恢复数据库完整性失败")
            decisions = c.execute("SELECT sid,status,kept_at,discarded_at,favorite FROM posts ORDER BY sid").fetchall()
            sha = hashlib.sha256(json.dumps(decisions, ensure_ascii=False).encode()).hexdigest()
            if sha != manifest["decisions_sha256"]:
                raise ValueError("恢复用户决定校验失败")
            # 恢复旧快照不能复活已撤销的设备；凭据文件保留，设备重新配对。
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            devices_reset = c.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0] if "device_sessions" in tables else 0
            for table in ("device_sessions", "device_codes", "pairing_attempts"):
                if table in tables:
                    c.execute(f"DELETE FROM {table}")
            c.commit()
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)   # 全有或全无：失败不留半份恢复现场
        raise
    return {"files": len(manifest["files"]), "posts": len(decisions), "integrity": "ok",
            "store": manifest.get("blob_store", "local"),
            "preexisting_missing": len(manifest.get("preexisting_missing", [])),
            "snapshot_at": manifest["created_at"],
            "devices_reset": devices_reset,
            "duration_seconds": round((datetime.now(config.NOW_TZ) - started).total_seconds(), 3)}
