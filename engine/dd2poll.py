# -*- coding: utf-8 -*-
"""dd2poll.py —— 统一轮询器（A 层优化的核心）。

为什么要它：实测 /brief(313B) 比 /state(9.3KB) 还慢 —— 开销在「排到 Unity 主线程再等一帧」，
不在包大小。所以省带宽没用，**少发请求 + 多进程共用一次结果**才省。

用法：
    import dd2poll
    s = dd2poll.get_state(max_age=2.0)      # 2 秒内的缓存直接复用，不重复打 mod
    dd2poll.max_age_for(s)                  # 按当前界面给一个合适的 max_age
    dd2poll.stats()                         # {'fetch': n, 'cache': m, 'dedupe': ...}

跨进程：/state 的结果落到 %TEMP%\\dd2_state_cache.json，runner 和记录器共用。
"""
import json
import os
import tempfile
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:18082"
CACHE = os.path.join(tempfile.gettempdir(), "dd2_state_cache.json")
GAP = 0.15                     # 两次真实请求之间至少隔这么久，防抖
_lock = threading.Lock()
_stats = {"fetch": 0, "cache": 0, "err": 0}
_last_fetch = [0.0]


def _read_cache(max_age):
    if max_age <= 0:
        return None
    try:
        age = time.time() - os.path.getmtime(CACHE)
        if age > max_age:
            return None
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(obj):
    try:
        tmp = CACHE + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def get(path, timeout=5.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def post(body, timeout=15.0):
    req = urllib.request.Request(BASE + "/action", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_state(max_age=0.0, timeout=6.0, fetch=None):
    """max_age 秒内别重复问 mod；同机其他进程刚问过的话直接拿它的结果。

    fetch: 可选的无参函数，返回 state。传它进来是为了让测试/调用方能替换取数方式
    （比如 runner 传 lambda: C.get("/state")，测试里换掉 C.get 就能模拟断线）。
    """
    with _lock:
        cached = _read_cache(max_age)
        if cached is not None:
            _stats["cache"] += 1
            return cached
        wait = GAP - (time.time() - _last_fetch[0])
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.time()
        try:
            s = fetch() if fetch is not None else get("/state", timeout=timeout)
        except Exception:
            _stats["err"] += 1
            raise
        _stats["fetch"] += 1
        _write_cache(s)
        return s


def max_age_for(s):
    """按界面给合适的缓存时长。战斗中要快，马车上要省。"""
    if not s:
        return 0.5
    mode = s.get("mode")
    scene = (s.get("scene") or "")
    if mode == "combat":
        return 0.7
    if mode == "map" and scene in ("MainScene",):
        return 2.0
    return 1.0


def interval_for(s):
    """轮询间隔建议：马车 2s，战斗 0.5s，其它 1.2s。"""
    if not s:
        return 1.0
    mode = s.get("mode")
    if mode == "combat":
        return 0.5
    if mode == "map":
        return 2.0
    return 1.2


def stats():
    d = dict(_stats)
    d["ratio"] = round(d["cache"] / max(1, d["cache"] + d["fetch"]), 2)
    return d
