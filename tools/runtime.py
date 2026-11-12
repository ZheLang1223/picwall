"""活目录由容器使用时，维护命令在同一内核内执行以共享文件锁。"""
import json
import subprocess
from pathlib import Path

from app import config


def delegate(script: str, args=()) -> bool:
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
    if not container["running"] or not owned_data:
        return False
    if script not in ("backup.py", "maintenance.py", "pair.py"):
        raise ValueError("未知维护脚本")
    # 确认活容器后不再回退宿主执行，避免容器退出或权限问题触发跨内核写入。
    subprocess.run(["docker", "exec", "-i", "picwall", "python", "/app/tools/" + script, *args], check=True)
    return True
