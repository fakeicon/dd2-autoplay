"""launcher.py —— 游戏进程的 API 入口（127.0.0.1:18083）。

2026-11-22 作者硬性口径：**所有游戏行为都必须通过 API，包括开游戏、关游戏**。
mod（18082）活在游戏进程里，游戏没起来它就不存在 —— 所以"启动"这件事必须由一个
常驻的外部 API 承担，就是本文件。不要手敲 `Start-Process`、不要 `Task Manager`。

端点（GET / POST 都收）:
    /health                     服务 + 游戏进程 + mod 是否在线
    /status                     同上（给人看）
    /launch?confirm=1&timeout=180   拉起游戏，等 mod 起来才算成功
    /quit                       走 mod quitGame，等进程真退出
    /mod                        POST body 直接转给 mod `/action`（一个入口管全部）
    /mod?p=/brief               GET 透传到 mod 的任意只读端点

写进代码的铁律（不靠记性）:
  1. 已有游戏进程在跑 → /launch 直接拒（绝不拉第二实例）。
  2. 拉游戏**不给 exe 传任何参数**，工作目录必须是游戏目录（AGENTS.md 血泪教训：
     带参数 Steam 会弹"用自定义参数启动"，游戏自己 RestartAppIfNecessary 自杀）。
  3. /quit 只走 quitGame；超时才优雅关窗（taskkill 不带 /F），**绝不 Kill()**
     —— Unity 会拉 UnityCrashHandler64 弹"已停止工作"框，还得人去点。
  4. 只监听 127.0.0.1。
  5. 启动默认要有放行：`confirm=1` 或环境变量 DD2_ALLOW_LAUNCH=1（防误触/防脚本乱开）。

用法:
    python launcher.py            # 常驻；Ctrl+C 或进程管理关掉
    curl http://127.0.0.1:18083/status
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dd2paths
import winfind

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DD2_LAUNCHER_PORT") or 18083)
MOD = "http://127.0.0.1:18082"
GAME_EXE = "Darkest Dungeon II.exe"
APP_ID = "1940340"
# 2026-11-25：为了让包能在别人机器上用，游戏目录改成自动发现（dd2paths.py：
# 环境变量 → 注册表 SteamPath → 每个库的 libraryfolders.vdf → 常见默认路径）。
# 本机仍然命中同一个目录，行为不变；都找不到才退回原来的写死路径。
GAME_DIR = (os.environ.get("DD2_GAME_DIR")
            or dd2paths.game_dir()
            or r"D:\Program Files (x86)\Steam\steamapps\common\Darkest Dungeon® II")
DEVNULL = subprocess.DEVNULL


# ------------------------------------------------------------------ 基础读写
def http_json(url, payload=None, timeout=8.0):
    """GET（payload=None）或 POST JSON。失败一律返回 {}，不抛。"""
    try:
        data, headers = None, {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception:
        return {}


def mod_alive(timeout=1.5):
    return bool(http_json(MOD + "/health", timeout=timeout).get("ok"))


def game_pids():
    return winfind.pids_by_image(GAME_EXE)


def status():
    pids = game_pids()
    port_pid = winfind.game_pid()
    alive = mod_alive(1.0)
    b = http_json(MOD + "/brief", timeout=1.5) if alive else {}
    # 本机枚举不到游戏进程（tasklist/Get-Process/Toolhelp 都看不到它，AGENTS.md 记过），
    # 所以"在不在跑"要三路取或：进程名 / 占着 18082 的 PID / mod 活着。
    # 少算一路的后果是"mod 冻死但游戏还开着"时被当成没跑 → 拉出第二个实例。
    running = bool(pids) or bool(port_pid) or alive
    return {"ok": True,
            "launcher": {"port": PORT},
            "game": {"running": running, "pids": pids,
                     "pid_from_port": port_pid, "mod": alive,
                     "exe": os.path.join(GAME_DIR, GAME_EXE),
                     "exe_exists": os.path.isfile(os.path.join(GAME_DIR, GAME_EXE))},
            "brief": {k: b.get(k) for k in ("mode", "scene", "row", "gold")} if b else {}}


def allow_launch():
    return (os.environ.get("DD2_ALLOW_LAUNCH") or "").strip() == "1"


# ------------------------------------------------------------------ 动作
def _spawn(exe, cwd):
    """拉进程。**参数表只有 exe 一个元素** —— 这是硬约束，别加参数。"""
    return subprocess.Popen([exe], cwd=cwd, close_fds=True,
                            stdout=DEVNULL, stderr=DEVNULL)


def launch(confirm=False, timeout=180.0):
    st = status()
    g = st["game"]
    if g["running"]:
        return {"ok": False, "why": "already_running", "pids": g["pids"], "mod": g["mod"],
                "hint": "已经在跑就别再拉；要进局用 mod 的 continueRun"}
    if not (confirm or allow_launch()):
        return {"ok": False, "why": "needs_confirm",
                "hint": "带 confirm=1 或设 DD2_ALLOW_LAUNCH=1 才放行"}
    exe = os.path.join(GAME_DIR, GAME_EXE)
    if not os.path.isfile(exe):
        return {"ok": False, "why": "exe_not_found", "exe": exe}
    appid = os.path.join(GAME_DIR, "steam_appid.txt")
    if not os.path.isfile(appid):
        return {"ok": False, "why": "steam_appid_missing", "path": appid,
                "hint": "没有 steam_appid.txt 时游戏会走 Steam 的 RestartAppIfNecessary 自杀"}
    t0 = time.time()
    p = _spawn(exe, GAME_DIR)
    while time.time() - t0 < timeout:
        if mod_alive(1.0):
            return {"ok": True, "pid": p.pid, "mod": True,
                    "elapsed": round(time.time() - t0, 1)}
        time.sleep(3)
    return {"ok": False, "why": "timeout", "pid": p.pid,
            "elapsed": round(time.time() - t0, 1),
            "hint": "进程起了但 mod 没响应：看 BepInEx 日志，别反复再拉"}


def _graceful_close(pid):
    """不带 /F = 发 WM_CLOSE（等同点窗口右上角），逼 Unity 自己收尾。"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       text=True, errors="replace", timeout=20)
        return True
    except Exception:
        return False


def quit_game(timeout=90.0):
    st = status()
    g = st["game"]
    if not g["running"]:
        return {"ok": True, "why": "not_running"}
    how = "quitGame"
    if g["mod"]:
        http_json(MOD + "/action", {"type": "quitGame"}, timeout=6.0)
    else:
        how = "taskkill"
        for pid in (g["pids"] or [g["pid_from_port"]]):
            if pid:
                _graceful_close(pid)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not game_pids() and not mod_alive(0.8):
            return {"ok": True, "how": how, "elapsed": round(time.time() - t0, 1)}
        time.sleep(1.5)
    left = game_pids()
    if not g["mod"] and left:
        how = "taskkill"
        for pid in left:
            _graceful_close(pid)
        t1 = time.time()
        while time.time() - t1 < 30:
            if not game_pids():
                return {"ok": True, "how": how, "elapsed": round(time.time() - t0, 1)}
            time.sleep(1.5)
    return {"ok": False, "why": "still_running", "pids": game_pids(),
            "note": "不 Kill：Unity 会弹 UnityCrashHandler64，得人去点确定"}


def mod_proxy(payload, timeout=15.0):
    if not isinstance(payload, dict) or not payload.get("type"):
        return {"ok": False, "why": "need_json_body_with_type"}
    return http_json(MOD + "/action", payload, timeout=timeout)


# ------------------------------------------------------------------ HTTP 壳
class H(BaseHTTPRequestHandler):
    server_version = "dd2-launcher/1.0"

    def log_message(self, fmt, *a):
        sys.stdout.write("[launcher] %s\n" % (fmt % a))
        sys.stdout.flush()

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def _route(self, body=None):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/health"
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]
        if path in ("/", "/health", "/status"):
            return self._send(200, status())
        if path == "/launch":
            try:
                to = float(one("timeout", 180))
            except Exception:
                to = 180.0
            ok = str(one("confirm", "0")).lower() in ("1", "true", "yes")
            r = launch(confirm=ok, timeout=to)
            return self._send(200 if r.get("ok") else 409, r)
        if path == "/quit":
            r = quit_game()
            return self._send(200 if r.get("ok") else 409, r)
        if path == "/mod":
            p = one("p")
            if p:
                return self._send(200, http_json(MOD + p, timeout=15.0) or {"ok": False})
            r = mod_proxy(body or {})
            return self._send(200 if r.get("ok") else 400, r)
        return self._send(404, {"ok": False, "why": "no_route", "path": path,
                                "routes": ["/health", "/status", "/launch", "/quit", "/mod"]})

    def do_GET(self):
        try:
            self._route()
        except Exception as e:
            self._send(500, {"ok": False, "err": str(e)[:200]})

    def do_POST(self):
        try:
            self._route(self._body())
        except Exception as e:
            self._send(500, {"ok": False, "err": str(e)[:200]})


def main(argv=None):
    port = PORT
    for a in (argv or sys.argv[1:]):
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print("[launcher] api on http://127.0.0.1:%d  (game dir: %s)" % (port, GAME_DIR))
    print("[launcher] GET /status | POST /launch?confirm=1 | POST /quit | POST /mod")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[launcher] bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
