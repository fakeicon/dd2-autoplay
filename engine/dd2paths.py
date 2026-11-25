"""dd2paths.py —— 找 DD2 的游戏目录 / Steam / 存档目录。

**为什么要有这个文件**：脚本原来把游戏目录写死成
`D:\\Program Files (x86)\\Steam\\steamapps\\common\\Darkest Dungeon® II`，
换一台机器就全废。这里按 Steam 自己的规则找，找不到就返回 None，
由调用方报错（不许静默用错路径，见 AGENTS.md「不许猜」）。

查找顺序：
  1. 环境变量 `DD2_GAME_DIR`（最高优先，调试/特殊安装用）
  2. 注册表 `HKCU\\Software\\Valve\\Steam` 的 `SteamPath`
  3. 该 Steam 的 `steamapps\\libraryfolders.vdf` 里列出的每个库目录（多盘安装）
  4. 常见默认路径兜底
只有「目录存在且游戏 exe 在里面」才算命中。
"""
import os
import re
import sys

GAME_FOLDER = "Darkest Dungeon® II"
GAME_EXE = "Darkest Dungeon II.exe"
APP_ID = "1940340"

_FALLBACK = (
    r"D:\Program Files (x86)\Steam",
    r"C:\Program Files (x86)\Steam",
    r"D:\Steam",
    r"E:\Steam",
    r"E:\Program Files (x86)\Steam",
)


def _reg(path, name):
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, path) as k:
                v, _ = winreg.QueryValueEx(k, name)
                if v:
                    return os.path.normpath(str(v))
        except OSError:
            continue
    return None


def steam_root():
    """Steam 安装目录（不含 steamapps）。找不到返回 None。"""
    for p in (
        _reg(r"Software\Valve\Steam", "SteamPath"),
        _reg(r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        _reg(r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        if p and os.path.isdir(p):
            return p
    for p in _FALLBACK:
        if os.path.isdir(p):
            return p
    return None


def _vdf_paths(vdf):
    """从 libraryfolders.vdf 里抠出所有 "path" 值（VDF 是老式 KV，正则够了）。

    注意里面是 `"path"  "D:\\\\SteamLibrary"`（转义的反斜杠），要还原。
    """
    out = []
    try:
        with open(vdf, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return out
    for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', txt):
        raw = m.group(1).replace("\\\\", "\\")
        if raw and os.path.isdir(raw):
            out.append(raw)
    return out


def library_roots():
    """所有 Steam 库根目录（含主目录）。"""
    steam = steam_root()
    roots = []
    if steam:
        roots.append(steam)
        roots += _vdf_paths(os.path.join(steam, "steamapps", "libraryfolders.vdf"))
    for p in _FALLBACK:
        if os.path.isdir(p):
            roots.append(p)
    seen, uniq = set(), []
    for r in roots:
        k = os.path.normcase(os.path.normpath(r))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def game_dir():
    """游戏目录（含 Darkest Dungeon II.exe）。找不到返回 None。"""
    env = os.environ.get("DD2_GAME_DIR")
    if env and os.path.isfile(os.path.join(env, GAME_EXE)):
        return os.path.normpath(env)
    for root in library_roots():
        cand = os.path.join(root, "steamapps", "common", GAME_FOLDER)
        if os.path.isfile(os.path.join(cand, GAME_EXE)):
            return cand
    return None


def steam_exe():
    """steam.exe 路径，找不到返回 None。"""
    root = steam_root()
    if root:
        p = os.path.join(root, "steam.exe")
        if os.path.isfile(p):
            return p
    return None


def save_dir():
    """玩家存档目录（SaveFiles 的父目录）。

    Windows: %USERPROFILE%\\AppData\\LocalLow\\RedHook\\Darkest Dungeon II
    """
    base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(base, "AppData", "LocalLow", "RedHook", "Darkest Dungeon II")


if __name__ == "__main__":
    for name, fn in (("steam", steam_root), ("game", game_dir),
                     ("steam.exe", steam_exe), ("save", save_dir)):
        print("%-10s %s" % (name, fn()))
