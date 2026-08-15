"""共有runtime/configを変更する処理のプロセス間排他。"""

from __future__ import annotations

import fcntl
import os
import stat

from .app import UserPaths


class LifecycleLockError(RuntimeError):
    pass


class LifecycleLock:
    """kernel lockを保持する間だけ共有状態の変更を許可する。"""

    def __init__(self, paths: UserPaths):
        self.path = paths.state_dir / "lifecycle.lock"
        self._fd: int | None = None

    def __enter__(self) -> "LifecycleLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise LifecycleLockError(f"lifecycle lockがsymlinkです: {self.path}")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise LifecycleLockError(f"lifecycle lockを開けません: {error}") from error
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LifecycleLockError(f"lifecycle lockが通常ファイルではありません: {self.path}")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LifecycleLockError(
                    "Codex OpenRouterは既に起動中または更新中です"
                ) from error
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_error: object) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
