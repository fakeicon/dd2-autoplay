"""capture_win.py —— 只读：把 DD2 窗口的画面抠出来存 PNG（PrintWindow，不抢焦点、不动鼠标）。

    python capture_win.py logs/shots/name.png

2026-11-21 去掉了 Pillow 依赖（本机 Python 没装 PIL，`import PIL` 直接 ModuleNotFound）：
BGRA → RGB 自己转，PNG 用 zlib 手写（IHDR/IDAT/IEND），任何 Python 3 都能跑。
"""
import ctypes
import os
import struct
import sys
import zlib
from ctypes import wintypes

import winfind

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))


def write_png(path, w, h, rows):
    """rows = 每行 RGB 字节（top→bottom），已去掉 alpha。"""
    raw = b"".join(b"\x00" + r for r in rows)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr))
        f.write(_chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(_chunk(b"IEND", b""))


def grab(out):
    pid = winfind.game_pid()
    hwnd = winfind.find_window(pid) if pid else 0
    if not hwnd:
        print("no window for pid", pid)
        return 1
    r = winfind.client_rect(hwnd)
    if not r:
        print("no client rect")
        return 1
    w, h = r[2], r[3]
    hdc = _user32.GetDC(hwnd)
    mdc = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    _gdi32.SelectObject(mdc, bmp)
    ok = _user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # 负高 = 从上到下的行序
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    got = _gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    _gdi32.DeleteObject(bmp)
    _gdi32.DeleteDC(mdc)
    _user32.ReleaseDC(hwnd, hdc)
    if not got:
        print("GetDIBits=0（抓不到画面）")
        return 1
    data = buf.raw
    rows = []
    for y in range(h):
        line = data[y * w * 4:(y + 1) * w * 4]
        # BGRA → RGB：丢掉 alpha，交换 R/B
        rgb = bytearray(w * 3)
        rgb[0::3] = line[2::4]
        rgb[1::3] = line[1::4]
        rgb[2::3] = line[0::4]
        rows.append(bytes(rgb))
    d = os.path.dirname(os.path.abspath(out))
    if d:
        os.makedirs(d, exist_ok=True)
    write_png(out, w, h, rows)
    print("PrintWindow=%s GetDIBits=%s -> %s (%dx%d)" % (ok, got, out, w, h))
    return 0


if __name__ == "__main__":
    sys.exit(grab(sys.argv[1] if len(sys.argv) > 1 else "logs/win.png"))
