"""备份 blob 库的存放后端：本地目录（历史行为）或夸克网盘异地副本。

blob 一律按内容 sha256 命名寻址，两个实现对备份格式（manifest 的 hash 清单）无感——
切换后端 = 换 PICWALL_BLOB_STORE 环境变量，或测试里注入 Fake。

夸克实现经 quarkclouddrive skill 的 CLI 子进程（node）走开放平台：
- 上传需渠道标记环境变量（CLAUDECODE=1），ZCode 终端直跑会被服务端 -104 拒绝；
- 回读用 download 命令落到临时目录，搬到指定位置后由调用方 sha256 校验；
- 命令无删除能力：云 GC（purge 联动）延后，云端 blob 只增不减是已知边界。
只有宿主侧 make backup / tools 走本模块；容器内服务从不执行备份。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import config, db


class BlobError(RuntimeError):
    """云端 blob 操作失败（CLI 缺失、上传失败、读取失败等）。"""


def _cli_events(args: list[str], timeout: int, env_extra: dict | None = None) -> list[dict]:
    """跑一条 CLI 子命令，返回全部 NDJSON 事件（解析失败的行跳过）。"""
    cli = config.QUARK_CLI
    if not cli.is_file():
        raise BlobError(f"夸克 CLI 不存在：{cli}（先安装 quarkclouddrive skill）")
    node = shutil.which("node")
    if not node:
        raise BlobError("找不到 node，夸克 CLI 无法运行")
    env = os.environ | {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    env.update(env_extra or {})
    sid = f"{int(datetime.now().timestamp())}-{os.urandom(3).hex()}"
    try:
        proc = subprocess.run([node, str(cli), *args, "--session-id", sid],
                              capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired as e:
        raise BlobError(f"夸克 CLI 超时（{' '.join(args[:2])}）") from e
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue   # 升级横幅等非 NDJSON 输出，忽略
    if not events:
        raise BlobError(f"夸克 CLI 无输出（exit={proc.returncode}）：{proc.stderr[-300:]}")
    return events


def _result(events: list[dict], action: str) -> dict:
    final = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if final is None:
        raise BlobError(f"{action}: CLI 输出缺少 result 行")
    if final.get("code") != 0:
        raise BlobError(f"{action}: {final.get('msg', '未知错误')}（code={final.get('code')}）")
    return final


def resolve_dir(path: str, parent_fid: str = "") -> str:
    """按名称幂等创建目录并返回 FID（create-folder 同名重复创建返回已有 FID）。"""
    args = ["create-folder", "--dir-path", path]
    if parent_fid:
        args += ["--parent-fid", parent_fid]
    events = _cli_events(args, timeout=60)
    fid = (_result(events, "create-folder").get("data") or {}).get("fid")
    if not fid:
        raise BlobError("create-folder 未返回目录 FID")
    return fid


def upload_files(paths: list[Path], parent_fid: str) -> dict[str, str]:
    """一批本地上传到指定目录，返回 {文件名: fid}。存在失败任务即抛错（续跑按表跳过）。"""
    events = _cli_events(["upload", *[str(p) for p in paths], "--parent-fid", parent_fid],
                         timeout=3600)
    result = _result(events, "upload")
    uploaded = {e["data"]["fileName"]: e["data"]["fileId"]
                for e in events if e.get("type") == "list" and e.get("code") == 0
                and e.get("data", {}).get("fileId")}
    if result.get("data", {}).get("successCount") != len(uploaded):
        raise BlobError(f"upload: 有子任务失败（成功 {len(uploaded)}，详见 CLI 输出）")
    return uploaded


def read_blob(fid: str, workdir: Path, retries: int = 3) -> Path:
    """按 FID 读取文件到 workdir，返回落盘路径（调用方负责校验与清理）。

    实际命令是 download（1.0.24 CLI 尚无 read-file 子命令）：result.data.filePath
    直接给出落盘绝对路径，搬到 workdir 顶层后由调用方 sha256 校验。
    服务端瞬时错误（如 53000）按退避重试——整库恢复 6768 次回源，单点抖动不能毁掉全局。
    """
    outdir = workdir / "dl"
    outdir.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(min(2 ** attempt, 8))
        try:
            events = _cli_events(["download", "--fid", fid,
                                  "--output-dir", str(outdir), "--overwrite"],
                                 timeout=1800)
            src = (_result(events, "download").get("data") or {}).get("filePath", "")
            if not src or not Path(src).is_file():
                raise BlobError(f"download 未产出文件（fid={fid[:8]}…）")
            dest = workdir / Path(src).name
            shutil.move(src, dest)
            shutil.rmtree(outdir, ignore_errors=True)
            return dest
        except BlobError as e:
            last = e
    raise last


def known_keys() -> set[str]:
    with db.conn() as c:
        return {r["key"] for r in c.execute("SELECT key FROM cloud_blobs")}


def register(key: str, fid: str, size: int) -> None:
    with db._write_lock, db.conn() as c:
        c.execute("INSERT OR REPLACE INTO cloud_blobs(key,fid,size,uploaded_at) VALUES(?,?,?,?)",
                  (key, fid, size, datetime.now(config.NOW_TZ).isoformat(timespec="seconds")))


class LocalBlobs:
    """本地 backups/blobs/ 目录，行为与历史版本逐位一致。"""

    NAME = "local"

    def __init__(self, root: Path | None = None):
        self.root = root or config.DATA_DIR / "backups" / "blobs"

    def known(self) -> set[str]:
        return {p.name for p in self.root.iterdir()} if self.root.is_dir() else set()

    def ensure_many(self, items: list[tuple[str, Path]]) -> None:
        """把 (sha256, 源文件) 落进本地库；已存在且校验通过的跳过。"""
        import hashlib
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for sha, src in items:
            blob = self.root / sha
            if blob.is_file() and hashlib.sha256(blob.read_bytes()).hexdigest() == sha:
                continue
            fd, tmp = tempfile.mkstemp(prefix=".blob-", dir=self.root)
            os.close(fd)
            try:
                shutil.copyfile(src, tmp)
                if hashlib.sha256(Path(tmp).read_bytes()).hexdigest() != sha:
                    raise OSError("备份复制校验失败")
                os.chmod(tmp, 0o600)
                os.replace(tmp, blob)
            finally:
                Path(tmp).unlink(missing_ok=True)

    def fetch_into(self, key: str, dest: Path) -> None:
        shutil.copyfile(self.root / key, dest)

    def describe(self) -> dict:
        return {"store": "local", "blobs": len(self.known())}


class QuarkBlobs:
    """夸克网盘 <备份目录>/blobs/ 作为 blob 库；登记表 cloud_blobs 是跳传依据。"""

    NAME = "quark"

    def __init__(self, dir_fid: str = "", batch: int = 0):
        self._dir_fid = dir_fid
        self.batch = batch or config.QUARK_UPLOAD_BATCH

    def dir_fid(self) -> str:
        """备份目录 → blobs 子目录，两级幂等创建；FID 配置了就直用。"""
        if not self._dir_fid:
            base = config.QUARK_DIR_FID or resolve_dir(config.QUARK_BACKUP_DIR)
            self._dir_fid = resolve_dir("blobs", parent_fid=base)
        return self._dir_fid

    def known(self) -> set[str]:
        return known_keys()

    def ensure_many(self, items: list[tuple[str, Path]]) -> None:
        known = known_keys()
        pending = [(sha, src) for sha, src in items if sha not in known]
        for i in range(0, len(pending), self.batch):
            chunk = pending[i:i + self.batch]
            uploaded = upload_files([src for _, src in chunk], self.dir_fid())
            by_name = {src.name: (sha, src) for sha, src in chunk}
            for name, fid in uploaded.items():
                sha, src = by_name[name]
                register(sha, fid, src.stat().st_size)
            if len(uploaded) != len(chunk):
                missing = sorted(set(by_name) - set(uploaded))
                raise BlobError(f"upload: {len(missing)} 个 blob 未确认：{missing[:3]}")

    def fetch_into(self, key: str, dest: Path) -> None:
        with db.conn() as c:
            row = c.execute("SELECT fid,size FROM cloud_blobs WHERE key=?", (key,)).fetchone()
        if not row:
            raise BlobError(f"云端缺少 blob {key[:12]}…（表无登记）")
        workdir = Path(tempfile.mkdtemp(prefix="quark-fetch-"))
        try:
            src = read_blob(row["fid"], workdir)
            if src.stat().st_size != row["size"] and row["size"]:
                raise BlobError(f"云端 blob 大小不符（key={key[:12]}…）")
            shutil.move(str(src), dest)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def describe(self) -> dict:
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM cloud_blobs").fetchone()[0]
            last = c.execute("SELECT MAX(uploaded_at) FROM cloud_blobs").fetchone()[0]
        return {"store": "quark", "blobs": n, "last_upload": last}


def from_config():
    """按 PICWALL_BLOB_STORE 构造后端；quark 模式缺 CLI 时宁可失败也不静默落本地。"""
    if config.BLOB_STORE == "quark":
        return QuarkBlobs()
    return LocalBlobs()
