"""
单实例锁模块 (InstanceLock)
===========================
基于操作系统级文件锁（Windows 使用 msvcrt.locking，POSIX 使用 fcntl.flock）。
进程退出（无论是正常退出还是崩溃/被杀）时，操作系统自动释放文件锁，
绝不会因遗留 lock 文件而导致后续进程无法启动。
"""
import os
import sys

# 平台适配
if os.name == "nt":
    import msvcrt
else:
    try:
        import fcntl
    except ImportError:
        fcntl = None


class InstanceLockError(Exception):
    """获取单实例锁失败"""
    pass


class InstanceLock:
    """操作系统级文件锁管理器"""

    def __init__(self, lock_path: str):
        self.lock_path = os.path.abspath(lock_path)
        self._fd = None
        self._locked = False

    def acquire(self) -> bool:
        """尝试获取锁。获取成功返回 True，被占用返回 False"""
        if self._locked:
            return True

        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)

        try:
            # 打开或创建锁文件，使用读写模式
            self._fd = open(self.lock_path, "a+")
            self._fd.seek(0)

            if os.name == "nt":
                # Windows: 锁定第一个字节，非阻塞模式
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                if fcntl is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self._locked = True
            # 记录当前进程 PID 便于运维排查
            try:
                self._fd.seek(0)
                self._fd.truncate()
                self._fd.write(f"pid={os.getpid()}\n")
                self._fd.flush()
            except Exception:
                pass
            return True

        except (BlockingIOError, OSError, PermissionError):
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
            self._locked = False
            return False

    def release(self):
        """主动释放锁"""
        if not self._locked or not self._fd:
            return

        try:
            if os.name == "nt":
                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                if fcntl is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._locked = False

    @property
    def is_locked(self) -> bool:
        return self._locked

    def __enter__(self):
        if not self.acquire():
            raise InstanceLockError(f"已有抢课进程正在运行，拒绝启动第二个实例。(lock={self.lock_path})")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
