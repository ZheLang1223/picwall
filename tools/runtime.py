"""活目录由容器使用时，维护命令在同一内核内执行以共享文件锁。"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import config


def container_owns_data() -> bool:
    """运行中的 picwall 容器是否正在绑定挂载当前 config.DATA_DIR。"""
    if Path("/.dockerenv").is_file():
        return False
    try:
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=^/picwall$", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10, check=True)
    except FileNotFoundError:
        # 裸进程安装没有 Docker；本机备份仍与本机 app 共用内核文件锁。
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("无法确认 PWALL 容器状态，停止本次维护；请先检查 make status") from e
    if not listed.stdout.strip():
        return False
    info = subprocess.run(
        ["docker", "inspect", "--format", '{"running":{{.State.Running}},"mounts":{{json .Mounts}}}', "picwall"],
        capture_output=True, text=True, timeout=10, check=True)
    container = json.loads(info.stdout)
    owned_data = any(m["Type"] == "bind" and m["Destination"] == "/app/data" and
                     Path(m["Source"]).resolve() == config.DATA_DIR.resolve() for m in container["mounts"])
    return bool(container["running"] and owned_data)


def require_stopped_container(action: str = "本工具") -> None:
    """拒绝在活容器共享挂载同一数据目录时从宿主直接写库/写盘。"""
    if container_owns_data():
        raise RuntimeError(
            f"picwall 容器正在使用 {config.DATA_DIR}；请先 make picwall-stop 后再运行{action}，避免跨内核写入损坏 SQLite WAL")


def delegate(script: str, args=()) -> bool:
    if not container_owns_data():
        return False
    if script not in ("backup.py", "maintenance.py", "pair.py"):
        raise ValueError("未知维护脚本")
    # 确认活容器后不再回退宿主执行，避免容器退出或权限问题触发跨内核写入。
    subprocess.run(["docker", "exec", "-i", "picwall", "python", "/app/tools/" + script, *args], check=True)
    return True


def _latest_snapshot(backups_dir: Path) -> Path | None:
    if not backups_dir.is_dir():
        return None
    snaps = [p for p in backups_dir.glob("picwall-*.snapshot") if p.is_dir() and not p.is_symlink()]
    if not snaps:
        return None
    return max(snaps, key=lambda p: p.stat().st_mtime)


def snapshot_needs_backup(data_dir: Path | None = None, max_age_sec: int = 86400) -> bool:
    """仅依据 picwall-*.snapshot 目录新鲜度判断是否需要补一轮备份（忽略 blobs/ 与杂项文件）。"""
    d = data_dir or config.DATA_DIR
    latest = _latest_snapshot(d / "backups")
    if latest is None:
        return True
    return (time.time() - latest.stat().st_mtime) >= max_age_sec


def format_status(data_dir: Path | None = None) -> list[str]:
    """生成跨平台快照新鲜度、夸克上云滞后与磁盘余量状态行（只读）。"""
    d = data_dir or config.DATA_DIR
    backups_dir = d / "backups"
    latest = _latest_snapshot(backups_dir)
    lines: list[str] = []
    if latest is None:
        lines.append("备份：暂无快照（跑 make picwall-backup 或 make backup）")
    else:
        hours = max(0, int((time.time() - latest.stat().st_mtime) // 3600))
        mf = latest / "manifest.json"
        store = "local"
        if mf.is_file():
            try:
                store = json.loads(mf.read_text()).get("blob_store", "local")
            except (OSError, json.JSONDecodeError):
                pass
        sev = ""
        if hours >= 72:
            sev = " [严重过期 ≥72h，请立即备份]"
        elif hours >= 24:
            sev = " [超 24h，建议 make picwall-backup]"
        lines.append(f"备份：{latest.name}（{hours} 小时前，store={store}）{sev}")

    blobs_dir = backups_dir / "blobs"
    db_path = d / "picwall.db"
    if blobs_dir.is_dir():
        known: set[str] = set()
        got_known = False
        if data_dir is None and container_owns_data():
            try:
                known = set(cloud_db("known_keys") or ())
                got_known = True
            except Exception:
                pass
        if not got_known and db_path.is_file():
            try:
                with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as c:
                    has_tbl = c.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_blobs'"
                    ).fetchone()
                    if has_tbl:
                        known = {r[0] for r in c.execute("SELECT key FROM cloud_blobs")}
            except sqlite3.Error:
                pass
        pending = sum(
            1 for p in blobs_dir.iterdir()
            if not p.is_symlink() and p.is_file() and not p.name.startswith(".") and p.name not in known
        )
        if pending:
            lines.append(f"夸克滞后：{pending} 个本地 blob 尚未上云（跑 make picwall-cloud）")

    target = d if d.exists() else d.parent
    if target.exists():
        free_gb = shutil.disk_usage(target).free / (1024 ** 3)
        if free_gb < 8:
            lines.append(f"磁盘警告：剩余 {free_gb:.1f} GiB（低于 8 GiB，可能影响备份与抓取）")
        elif free_gb < 20:
            lines.append(f"磁盘提示：剩余 {free_gb:.1f} GiB（低于 20 GiB）")
        else:
            lines.append(f"磁盘：剩余 {free_gb:.1f} GiB")
    return lines


def _handle_cloud_db(req: dict):
    """在数据目录所属内核内执行 cloud_blobs 相关 DB 操作。"""
    from datetime import datetime

    from app import cloud, db

    action = req.get("action", "")
    if action == "init_db":
        db.init_db()
        return True
    if action == "known_keys":
        return sorted(cloud.known_keys())
    if action == "list_blobs":
        with db.conn() as c:
            rows = c.execute("SELECT key,fid,size FROM cloud_blobs ORDER BY key").fetchall()
        return [{"key": r["key"], "fid": r["fid"], "size": r["size"]} for r in rows]
    if action == "register":
        items = req.get("items")
        if items is None and {"key", "fid", "size"} <= set(req):
            items = [{"key": req["key"], "fid": req["fid"], "size": req["size"]}]
        items = list(items or ())
        if not items:
            return 0
        now = datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
        rows = [(str(it["key"]), str(it["fid"]), int(it["size"]), now) for it in items]
        with db._write_lock, db.conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO cloud_blobs(key,fid,size,uploaded_at) VALUES(?,?,?,?)",
                rows,
            )
        return len(rows)
    raise ValueError(f"未知 cloud-db 操作: {action}")


def cloud_db(action: str, payload: dict | None = None):
    """夸克同步工具用的 DB 访问层：活容器走 docker exec，停机时直接本机执行。"""
    req = {"action": action, **(payload or {})}
    if not container_owns_data():
        return _handle_cloud_db(req)
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", "picwall", "python", "/app/tools/runtime.py", "cloud-db"],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        if "needs-backup|status" in detail:
            raise RuntimeError("容器内 tools/runtime.py 版本较旧；请先 make picwall 重建容器后再同步") from e
        raise RuntimeError(f"容器内 cloud-db 执行失败：{detail or e}") from e
    resp = json.loads(proc.stdout)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or "容器内 cloud-db 执行失败")
    return resp.get("result")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "needs-backup":
        print("1" if snapshot_needs_backup() else "0")
    elif cmd == "status":
        for line in format_status():
            print(line)
    elif cmd == "cloud-db":
        try:
            req = json.loads(sys.stdin.read() or "{}")
            print(json.dumps({"ok": True, "result": _handle_cloud_db(req)}))
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
            sys.exit(1)
    else:
        raise SystemExit("用法: python tools/runtime.py [needs-backup|status|cloud-db]")
