from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class InstanceAlreadyRunningError(RuntimeError):
    pass


class ConfigInstanceLock:
    def __init__(self, config_path: str | Path) -> None:
        config = Path(config_path).resolve()
        self.lock_path = config.with_name(f".{config.name}.lock")
        self._handle: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> ConfigInstanceLock:
        if not self.acquire():
            raise InstanceAlreadyRunningError("该配置已由另一个程序实例打开")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
