"""单实例锁：防止两个 runner / 两个 watchdog 同时驱动同一局游戏（2026-11-21 撞车事故）。

用法：
    from instance_lock import acquire, release
    fh = acquire("runner")          # 拿不到返回 None
    if fh is None: sys.exit(EXIT_ALREADY_RUNNING)
    ...运行...
    release(fh, "runner")

实现：Windows 上用 msvcrt.locking() 对锁文件加独占字节锁，进程退出/崩溃时系统自动释放，
所以不会留下"僵尸锁"。锁文件本身留着没关系。
"""
import msvcrt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_DIR = os.path.join(HERE, "logs")
EXIT_ALREADY_RUNNING = 9


def _path(name):
    return os.path.join(LOCK_DIR, "%s.lock" % name)


def acquire(name, verbose=True):
    """拿到锁返回文件句柄；已被别的进程持有返回 None。"""
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
    except OSError:
        pass
    fh = open(_path(name), "a+", encoding="utf-8")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        if verbose:
            print("[%s] 已有一个在跑（锁 %s 被占用），本进程退出。退出码=%d"
                  % (name, _path(name), EXIT_ALREADY_RUNNING), file=sys.stderr)
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write("%d\n" % os.getpid())
        fh.flush()
    except OSError:
        pass
    return fh


def other_process(script):
    """数还有几个 python.exe 在跑指定脚本（不含自己/父进程）。
    用于兜住"锁机制上线前就已经在跑"的老进程。"""
    try:
        import subprocess
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
               "Where-Object { $_.CommandLine -match '%s' } | "
               "Select-Object -ExpandProperty ProcessId)" % script]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
        me, parent = os.getpid(), os.getppid()
        return [p for p in pids if p not in (me, parent)]
    except Exception:
        return []


def release(fh, name):
    if not fh:
        return
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
