"""Advisory locks for cooperating manager processes on Linux."""

import fcntl
import os
from pathlib import Path


class LockUnavailable(RuntimeError):
    """Another operation owns the lock, or its file cannot be opened."""


class FileLock:
    """Lock a separate, persistent file; never unlink a live lock inode."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            raise LockUnavailable('当前操作已经持有锁')
        fd = None
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(str(self.path), flags, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise LockUnavailable(f'无法取得操作锁 {self.path}: {exc}') from exc
        self._fd = fd
        return self

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()


def workspace_lock(root):
    return FileLock(Path(root) / '.workspace_manager.lock')
