"""长维护任务：先持久登记，再后台执行；刷新、重启均可核对结果。"""
import asyncio
import errno
import json
import logging
import threading
import uuid
from datetime import datetime

from . import config, db

# download 是批量打包任务：与归档/补抓一样可恢复核对，打包产物 zip 路径记录在 result.file。
KINDS = ("archive", "media", "download")
ACTIVE = ("queued", "running")
logger = logging.getLogger("picwall.jobs")


def _now():
    return datetime.now(config.NOW_TZ).isoformat(timespec="seconds")


def _read(c, kind):
    row = c.execute("SELECT v FROM meta WHERE k=?", ("job:" + kind,)).fetchone()
    return json.loads(row[0]) if row else {"kind": kind, "state": "idle"}


def _write(c, job):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              ("job:" + job["kind"], json.dumps(job, ensure_ascii=False)))


def status(kind):
    if kind not in KINDS:
        raise ValueError("未知维护任务")
    with db.conn() as c:
        return _read(c, kind)


def _update(kind, job_id, **values):
    with db._write_lock, db.conn() as c:
        current = _read(c, kind)
        if current.get("id") == job_id and current["state"] in ACTIVE:
            _write(c, current | values)


def recover_interrupted():
    """仅在单进程启动时调用；原媒体/归档记录保留，用户可继续重试。"""
    with db._write_lock, db.conn() as c:
        for kind in KINDS:
            job = _read(c, kind)
            if job["state"] in ACTIVE:
                _write(c, job | {"state": "interrupted", "finished_at": _now(),
                                 "error": "服务重启，任务中断；已完成的文件保留，可重试"})


class Runner:
    def __init__(self):
        self.tasks = {}

    def start(self, kind, work):
        if kind not in KINDS:
            raise ValueError("未知维护任务")
        # 登记和派发之间不 await；即使发起请求随即断开，任务仍有持有者。
        with db._write_lock, db.conn() as c:
            job = _read(c, kind)
            if job["state"] in ACTIVE:
                return job
            job = {"kind": kind, "id": uuid.uuid4().hex, "state": "queued",
                   "started_at": _now(), "finished_at": "", "progress": {},
                   "result": None, "error": ""}
            _write(c, job)
        stop = threading.Event()
        task = asyncio.create_task(self._execute(job, work, stop))
        self.tasks[job["id"]] = (task, stop)
        task.add_done_callback(lambda _: self.tasks.pop(job["id"], None))
        return job

    async def _execute(self, job, work, stop):
        kind, job_id = job["kind"], job["id"]
        def progress(done, total):
            _update(kind, job_id, progress={"done": done, "total": total})
        try:
            await asyncio.to_thread(_update, kind, job_id, state="running")
            result = await work(stop.is_set, progress)
            if stop.is_set():
                raise InterruptedError()
            # 文件级异常由工作函数的结果解释；任务完成不等于全部媒体完整。
            state = "complete" if result.get("ok", True) else "failed"
            if "history" in result:
                result = result | {"history": {k: v for k, v in result["history"].items() if k != "files"}}
            await asyncio.to_thread(_update, kind, job_id, state=state, result=result,
                                    error=result.get("msg", "") if state == "failed" else "",
                                    finished_at=_now())
        except (asyncio.CancelledError, InterruptedError):
            await asyncio.to_thread(_update, kind, job_id, state="interrupted", finished_at=_now(),
                                    error="服务停止，任务中断；已完成的文件保留，可重试")
        except Exception as e:
            message = "磁盘空间不足，请腾出空间后重试" if isinstance(e, OSError) and e.errno == errno.ENOSPC else "任务失败，请重试；持续失败时检查本机日志与磁盘"
            logger.error("maintenance kind=%s error_type=%s", kind, type(e).__name__)
            await asyncio.to_thread(_update, kind, job_id, state="failed", finished_at=_now(),
                                    error=message, error_type=type(e).__name__)

    async def shutdown(self):
        pending = list(self.tasks.values())
        for _, stop in pending:
            stop.set()
        if not pending:
            return
        _, unfinished = await asyncio.wait([t for t, _ in pending], timeout=5)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*(t for t, _ in pending), return_exceptions=True)


runner = Runner()
