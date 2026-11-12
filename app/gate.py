"""数据库和媒体共同的可重入进程锁；备份固定文件快照时使用同一边界。"""
import os
import threading
from functools import wraps

from . import config

_threads = threading.RLock()
_local = threading.local()


class DataGate:
    def __enter__(self):
        _threads.acquire()
        try:
            if not getattr(_local, "depth", 0):
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                _local.file = (config.DATA_DIR / ".mutation.lock").open("a+b")
                if os.name == "nt":
                    import msvcrt
                    _local.file.write(b"0")
                    _local.file.flush()
                    _local.file.seek(0)
                    msvcrt.locking(_local.file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(_local.file.fileno(), fcntl.LOCK_EX)
            _local.depth = getattr(_local, "depth", 0) + 1
            return self
        except BaseException:
            if getattr(_local, "file", None):
                _local.file.close()
            _threads.release()
            raise

    def __exit__(self, *_exc):
        _local.depth -= 1
        try:
            if not _local.depth:
                if os.name == "nt":
                    import msvcrt
                    _local.file.seek(0)
                    msvcrt.locking(_local.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(_local.file.fileno(), fcntl.LOCK_UN)
                _local.file.close()
        finally:
            _threads.release()


gate = DataGate()


def guarded(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with gate:
            return fn(*args, **kwargs)
    return call


class WriteLock:
    """保持原非重入写锁接口，进程锁总是在它之前获得。"""
    def __init__(self):
        self.lock = threading.Lock()

    def __enter__(self):
        gate.__enter__()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()
        gate.__exit__(*exc)

    def locked(self):
        return self.lock.locked()
