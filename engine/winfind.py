"""winfind.py —— 只读的窗口 / 进程定位（不输入、不切前台、不截图）。

从原 winclick.py 里剥出来的纯读取部分：按镜像名或端口反查 PID、找主窗口、
拿客户区矩形、游戏坐标(1920x1080, 原点左下)→屏幕像素。

这里**只允许 Get/Enum 类调用**：出现鼠标键盘注入、SetForegroundWindow/SetActiveWindow
之类都算违反「一切游戏操作只走 API」（test_api_only.py 会拦）。
游戏内操作一律走 mod：http://127.0.0.1:18082 。
"""
import ctypes
import subprocess
from ctypes import wintypes

_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32

TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.CHAR * MAX_PATH)]


def pids_by_image(image="Darkest Dungeon II.exe"):
    """按可执行文件名找 PID（Toolhelp 快照，只读）。

    AGENTS.md 记过：这台机器 `Get-Process "Darkest Dungeon II"` 看不到已在跑的它，
    所以别用 PowerShell 名字枚举判断"游戏在不在"。
    """
    want = (image or "").strip().lower()
    out = []
    if not want:
        return out
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return out
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = _k32.Process32First(snap, ctypes.byref(e))
        while ok:
            name = bytes(e.szExeFile).split(b"\x00", 1)[0].decode("mbcs", "replace")
            if name.strip().lower() == want:
                out.append(int(e.th32ProcessID))
            ok = _k32.Process32Next(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def game_pid(port=18082):
    """通过 mod 端口反查游戏 PID；没有监听返回 0。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             errors="replace", timeout=20).stdout
    except Exception:
        return 0
    tag = ":%d" % int(port)
    for line in out.splitlines():
        if tag in line and "LISTEN" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return 0


def find_window(pid):
    """按 PID 找最大的可见窗口句柄；找不到返回 0。"""
    best = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not _u32.IsWindowVisible(hwnd):
            return True
        wpid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        r = RECT()
        if _u32.GetWindowRect(hwnd, ctypes.byref(r)):
            w = r.right - r.left
            if w > 800 and (not best or w > best[0][1]):
                best.append((hwnd, w))
        return True

    _u32.EnumWindows(cb, 0)
    return best[-1][0] if best else 0


def client_rect(hwnd):
    """窗口客户区的屏幕坐标 (left, top, width, height)；拿不到返回 None。"""
    r = RECT()
    if not hwnd or not _u32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    o = POINT(0, 0)
    if not _u32.ClientToScreen(hwnd, ctypes.byref(o)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    return (o.x, o.y, w, h) if w > 200 and h > 200 else None


def screen_size():
    return _u32.GetSystemMetrics(0), _u32.GetSystemMetrics(1)


def is_foreground(pid=None):
    """游戏窗口是不是当前前台。拿不到窗口时按"是"处理（老机器上枚举会失败）。"""
    hwnd = find_window(pid or game_pid())
    if not hwnd:
        return True
    return _u32.GetForegroundWindow() == hwnd


def _to_phys(gx, gy, hwnd=0, gw=1920, gh=1080):
    """游戏坐标（原点左下，1920x1080 空间）→ 物理屏幕像素。

    纯换算，不发输入。有窗口句柄按客户区映射（窗口化也能对上），否则按整屏。
    """
    cr = client_rect(hwnd)
    if cr:
        left, top, w, h = cr
        return int(round(left + gx * w / gw)), int(round(top + (gh - gy) * h / gh))
    sw, sh = screen_size()
    return int(round(gx * sw / gw)), int(round(sh - gy * sh / gh))
