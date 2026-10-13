"""Process-held locks and maintenance-aware waits; no bot/data access."""
from contextlib import AbstractContextManager
from pathlib import Path
import os
import time


class AlreadyRunning(RuntimeError):
    pass


class ProcessLock(AbstractContextManager):
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.file = self.path.open('a+b')
        # Windows forbids reading a byte owned by another process's lock.
        # Inspect size only; acquire the OS lock before any byte access.
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise AlreadyRunning('Process lock is held') from None
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()  # OS also releases the lock on a crash.
            self.file = None
        return False


def maintenance_requested(root):
    return (Path(root) / 'telegram.maintenance').exists()


def wait_or_maintenance(root, seconds, interval=0.5):
    deadline = time.monotonic() + seconds
    while not maintenance_requested(root):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))
    return True
