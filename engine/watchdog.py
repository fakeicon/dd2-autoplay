"""watchdog.py —— 挂机看门狗：冻死自动重启游戏；团灭/减员自动重开一局。

背景（交接-待优化-20261120凌晨.md P0-2）：`combat_arena_city_resist` 冻在
`battleState=in_turn_perform`，点技能/点目标/pass 全不推进，重启游戏后仍然冻 1 小时 = 真死。
runner 现在能 57 秒判出冻结（FROZEN_SPINS=12）并写 alert，但**只是停，不是救**。
这里补上"救"：kill 游戏 → Steam 拉起 → 等回到主界面 → 重挂 runner。

规则（也是用户定的口径）：
  runner 退出码 4（团灭）   → **自动重开**：回祭坛 → 长按启程（见交接-数据复盘 §0.1）
  runner 退出码 7（减员）   → 同上；中局减员时先"放弃征程"再回祭坛（全程不关游戏）
  runner 退出码 2（卡死）   → 重启游戏，等回主界面，重挂 runner
  runner 退出码 8（画面不动）→ 重启游戏（界面卡死基本只能重启）
  runner 退出码 3（连不上） → 先探 20 秒；还连不上才重启游戏
  runner 退出码 5（祭坛）   → 停手（「启程」只能人点）
  runner 退出码 0（时间到） → 续下一轮（--once 则不续）
  runner 活着但 status/report 超过 --stall 秒没更新 → 当冻死处理
  --no-reroll               → 退回旧口径：团灭/减员都停手等人

用法:
  python watchdog.py --hours 4                # 守 4 小时（每天挂机就这个）
  python watchdog.py --hours 1 --rounds 3     # 最多救 3 次
  python watchdog.py --once                   # 只跑一轮，等价于以前直接开 runner

注意：本进程默认给 runner 带 DD2_NO_RETREAT=1（**死战到底**，与 combat3 的默认一致）；
显式 --retreat 才透传 DD2_NO_RETREAT=0 允许撤退。
杀进程用 taskkill（无管道捕获，沙箱下也能跑）。**默认不拉起游戏**（作者口径 05:44：人从 Steam 开），
确需脚本拉起才 `DD2_ALLOW_LAUNCH=1`；否则只写 alert(kind=need-launch) 停手等人。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:18082"
GAME_IMAGE = "Darkest Dungeon II.exe"
APP_ID = "1940340"
STEAM_CANDIDATES = (
    r"d:\program files (x86)\steam\steam.exe",
    r"c:\program files (x86)\steam\steam.exe",
    r"e:\steam\steam.exe",
)
LOG_DIR = os.path.join(HERE, "logs")
EVENTS = os.path.join(LOG_DIR, "watchdog.jsonl")
STATUS = os.path.join(HERE, "status.json")
REPORT = os.path.join(HERE, "report.json")
# 战斗心跳：combat3 每出一手就写一次（一场仗 5 分钟，report.json 全程不动）
HB = os.path.join(HERE, "logs", "combat_hb")
# 已打完的局数（跨进程持久化）：watchdog 重启/被停手后再起，8 把的计数不能归零
RUNS_FILE = os.path.join(HERE, "logs", "runs_done.json")


def runs_done_count():
    try:
        with open(RUNS_FILE, encoding="utf-8") as f:
            return int((json.load(f) or {}).get("done") or 0)
    except Exception:
        return 0


def set_runs_done(n):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RUNS_FILE, "w", encoding="utf-8") as f:
            json.dump({"done": int(n), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except OSError:
        pass

# 退出码 → 处理方式（纯函数 decide() 用，离线回归直接打这张表）
EXIT_REROLL = {4: "wipe", 7: "member-lost"}     # 本局结束，重开一局
EXIT_STOP = {5: "altar-manual", 9: "hero-story", 127: "spawn-fail"}
EXIT_RESTART = {2: "stuck", 3: "no-mod", 8: "no-progress"}

# 状态文件多久不更新算卡死（按当前画面分档，--stall 只是上限）。
# 卡住的时候 runner 会整个 tick 卡在 do_tick 里（比如拖饰品），此时它连 report.json
# 都写不出来 —— 这种"进程活着但一个字都不写"只能在这一层抓。
# 数字来源：`combat_log.jsonl` 里单次出招耗时 n=789，中位 3.3s、p99 10.7s、max 11.1s；
# 战斗档取 120s ≈ 10× 最慢的一手。地图/客栈两档**没有测到 tick 级数据**，是暂定值。
STALL_BY_MODE = {"combat": 120.0, "inn": 300.0, "map": 240.0}
STALL_DEFAULT = 300.0


def say(msg):
    print("[watch %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def stop_message(code, why, alert=None):
    """停手时要发给主人的那句话（纯函数，方便测）。"""
    head = {"wipe": "团灭", "member-lost": "减员", "stall": "卡住（被看门狗杀掉）",
            "no-mod": "连不上 mod", "stuck": "画面不动", "altar-manual": "停在祭坛等人",
            "hero-story": "神殿剧情等你打", "need-launch": "游戏没起来"}.get(why, why)
    s = "【DD2】停手：%s（退出码 %s）" % (head, code)
    if alert and alert.get("kind"):
        s += "\n告警：%s — %s" % (alert["kind"], str(alert.get("why"))[:90])
    s += "\n游戏停在原地，没自动重开、没重启进程。"
    return s


def _read_alert():
    """读当前 alert.json（读不到就当空）。"""
    try:
        with open(os.path.join(HERE, "alert.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def notify_qq(text):
    """停手时给主人发一条 QQ（经 Aurabot / SnowLuma）。发不出去绝不影响看门狗。"""
    try:
        import qq_send
        qq_send.send(text[:900])
        event("qq_sent", chars=len(text))
        return True
    except Exception as e:
        event("qq_fail", error="%s: %s" % (type(e).__name__, str(e)[:120]))
        return False


def event(kind, **kw):
    """写一行看门狗事件（给事后看，不进对话/上下文）。"""
    kw.update(ts=time.strftime("%Y-%m-%d %H:%M:%S"), kind=kind)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ 纯决策（可离线测）
def decide(code, wipe_stop=0, wipes=0):
    """runner 退出码 → (动作, 原因)。动作 ∈ stop/restart/reroll/continue。

    wipe_stop = "第几次团灭就停手"（作者口径 2026-11-20 21:5x：**连吃两把团灭**才停手、
    回希望祭坛等人；减员(7) 仍然自动重开）。
    兼容旧调用：wipe_stop 传 True/False 时按"第一次团灭就停/不停"处理。
    """
    if isinstance(wipe_stop, bool):
        wipe_stop, wipes = (1 if wipe_stop else 0), 1
    if code in EXIT_REROLL:
        if code == 4 and wipe_stop and wipes >= wipe_stop:
            return "stop", "wipe"
        return "reroll", EXIT_REROLL[code]
    if code in EXIT_STOP:
        return "stop", EXIT_STOP[code]
    if code in EXIT_RESTART:
        return "restart", EXIT_RESTART[code]
    return "continue", "time-up"


def can_reroll(history, now, max_per_hour=20, min_gap=90.0, storm_n=5, storm_gap=180.0):
    """history=重开时刻列表。返回 (能否重开, 说明)。

    阈值比 can_restart 宽：重开是**正常产出**（每局都有蜡烛），不是故障。
    但要挡住两种病态：
      - 间隔 < min_gap：上一局几乎秒死，重开也是白开；
      - 连续 storm_n 次间隔都 < storm_gap：引擎在开局就必掉人，该修代码，不该继续采样。
    """
    recent = [t for t in history if now - t < 3600]
    if len(recent) >= max_per_hour:
        return False, "1 小时内已重开 %d 次，停手" % len(recent)
    if history and now - history[-1] < min_gap:
        return False, "距上次重开只有 %.0fs，停手" % (now - history[-1])
    chain = history[-(storm_n - 1):] + [now] if storm_n > 1 else [now]
    if len(chain) >= storm_n and all(b - a < storm_gap for a, b in zip(chain, chain[1:])):
        return False, "连续 %d 次重开间隔都小于 %.0fs（开局就掉人），停手待修" % (storm_n, storm_gap)
    return True, "ok"


def stalled(last_touch, now, limit):
    """status/report 多久没更新算冻死。last_touch=0（从没写过）不算。"""
    if not last_touch:
        return False
    return (now - last_touch) > limit


def stall_limit(mode, cap=None):
    """当前画面允许"状态文件多久不更新"。cap 是 --stall 传进来的上限。"""
    lim = STALL_BY_MODE.get((mode or "").lower(), STALL_DEFAULT)
    return min(lim, cap) if cap else lim


def status_mode(path=None):
    """从 status.json 读当前 mode（读不到就返回 None，按默认档处理）。"""
    try:
        with open(path or STATUS, encoding="utf-8") as f:
            return (json.load(f) or {}).get("mode")
    except Exception:
        return None


def can_restart(history, now, max_per_hour=8, min_gap=60.0):
    """history=重启时刻列表。返回 (能否重启, 说明)。防止重启风暴。"""
    recent = [t for t in history if now - t < 3600]
    if len(recent) >= max_per_hour:
        return False, "1 小时内已重启 %d 次，停手" % len(recent)
    if history and now - history[-1] < min_gap:
        return False, "距上次重启只有 %.0fs，停手" % (now - history[-1])
    return True, "ok"


# ------------------------------------------------------------------ 游戏进程 / 接口
def mod_ok(timeout=3.0):
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def brief(timeout=3.0):
    try:
        with urllib.request.urlopen(BASE + "/brief", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception:
        return {}


LAUNCHER = os.environ.get("DD2_LAUNCHER") or "http://127.0.0.1:18083"


def ensure_launcher():
    """确保 launcher API（18083）在跑；它只是 API 服务，拉它不等于拉游戏。"""
    try:
        with urllib.request.urlopen(LAUNCHER + "/health", timeout=2) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        pass
    try:
        subprocess.Popen([sys.executable, os.path.join(HERE, "launcher.py")],
                         cwd=HERE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception as e:
        say("拉 launcher 失败：%s" % e)
        return False


def launcher_post(path, timeout=240.0):
    """打 launcher API（开/关游戏）。第一次失败就先起 launcher 再试一次。"""
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(LAUNCHER + path, data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")) or {}
        except Exception:
            if attempt or not ensure_launcher():
                return {}
            time.sleep(2)
    return {}


def game_exit():
    """关游戏：只走 launcher API（quitGame → 超时才优雅关窗），不 taskkill /F。

    2026-11-22 口径：所有游戏行为只走 API。强杀会让 Unity 弹
    UnityCrashHandler64「已停止工作」，还得人去点。返回码 0=关掉了。
    """
    r = launcher_post("/quit", timeout=180.0)
    if not r:
        say("launcher 没响应：关不了游戏（不退化成本机强杀）")
        return -1
    say("关游戏：%s" % (r.get("how") or r.get("why") or "?"))
    return 0 if r.get("ok") else -1


def steam_exe():
    # 2026-11-25：先走 dd2paths（注册表 + libraryfolders.vdf），本机命中同一个，
    # 换机器也能找对；都找不到再用老的候选表。
    try:
        import dd2paths
        p = dd2paths.steam_exe()
        if p:
            return p
    except Exception:
        pass
    for p in STEAM_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def allow_launch():
    """作者口径（2026-11-20 05:44）：**游戏由人自己从 Steam 打开，脚本不拉**。

    实测：脚本起 steam.exe 会崩（exit -105 / steamerrorreporter），而且本机
    `Get-Process "Darkest Dungeon II"` 看不到已在跑的游戏进程，重复拉起就会把游戏
    开成第二个实例/报错。所以默认禁止；确需放行才 DD2_ALLOW_LAUNCH=1。
    """
    return os.environ.get("DD2_ALLOW_LAUNCH") == "1"


def write_alert(kind, why, **kw):
    """给上层/人看的告警（runner 不在跑的时候由看门狗自己写）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        d = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "why": why}
        d.update(kw)
        with open(os.path.join(HERE, "alert.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except OSError as e:
        say("写 alert 失败：%s" % e)


def game_launch():
    if not allow_launch():
        say("按口径不拉起游戏（游戏由人从 Steam 打开）")
        event("launch_forbidden")
        return False
    r = launcher_post("/launch?confirm=1", timeout=240.0)
    if not r:
        say("launcher 没响应：不拉游戏（不退化成本机 Start-Process）")
        return False
    if r.get("ok"):
        say("游戏已起（%.0fs）" % float(r.get("elapsed") or 0))
        return True
    say("拉起失败：%s" % (r.get("why") or "?"))
    return False


def wait_ready(limit=300, need_menu=True):
    """等 mod 接口 + 回到主界面。返回 True/False。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        if mod_ok():
            b = brief()
            if not need_menu or (b.get("mode") == "out_of_combat" and b.get("scene") == "main_menu"):
                return True
        time.sleep(5)
    return False


def post_action(payload, timeout=8.0):
    """给 mod 发一个动作（HTTP POST，不用管道捕获）。失败返回 {}。"""
    try:
        req = urllib.request.Request(
            BASE + "/action",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception:
        return {}


def at_altar(b=None):
    b = brief() if b is None else b
    txt = "%s/%s" % ((b or {}).get("scene") or "", (b or {}).get("mode") or "")
    return "altar" in txt.lower()


def get_ui(timeout=5.0):
    """整页可点元素（只在真的要点界面时用，平时别碰 —— 17KB）。"""
    try:
        with urllib.request.urlopen(BASE + "/ui", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")) or {}
    except Exception:
        return {}


# 「放弃征程」不再靠点按钮（2026-11-23 08:5x 作者选方案 B）：
# 原来的 ABANDON_TEXT / ABANDON_DENY / ABANDON_YES 文案安全闸，以及 _find_clickable / _click
# 两个辅助函数，**已经没有任何调用方**（不删的话会让人以为那道闸还在挡「退出游戏」）。
# 现在走 mod 的 abandonRun（见 abandon_run()）。
# 历史文案留着备查旧日志：暂停菜单里的按钮叫「放弃征程」，节点路径 PauseMenuButton - QuitRun；
# 旁边那个是「退出游戏」，路径 PauseMenuButton - Quit（2026-11-20 06:2x 实拍）。
# 2026-11-23 08:4x：abandonRun 发出后，只用 /brief 观测落点多久（不点任何东西）。
# 15 秒够"结算屏出现"；之后 drive_to_altar 会接手点「继续」。
ABANDON_WATCH_SECS = 15.0


def _row0(b):
    """是不是"开局马车"（row 缺省或 0）。row>0 = 半路中局，按 W 到不了祭坛。"""
    row = (b or {}).get("row")
    try:
        return row is None or int(row) <= 0
    except (TypeError, ValueError):
        return True


def abandon_run(limit=45.0):
    """中局减员后：走 mod 的 `abandonRun`（游戏自己的 RunScoreManager.SetGameOver/TriggerGameOver）。

    作者口径（交接-数据复盘 §0.1）：本场硬打完 → 放弃征程 → 原地重开一局。
    这里只管"发起放弃 + 记下真实落点"，点「继续」/回祭坛交给 drive_to_altar，启程交给 reroll()。

    2026-11-23 08:4x 作者选**方案 B（完整迁移）**，换掉了原来的三条老路：
      `simAction PauseMenu`（**禁用**：走 InputSimulator）→ 找「放弃征程」按钮 → 点「是」。
    07:42 那次就是这条路上失败（`watchdog.jsonl`：`abandon_fail`「放弃后没回到祭坛」）。
    新路依据：`mod\\StateBuilder.cs:583-606` 的 abandonRun 08:0x 实机返回
      `{"ok":true,"reason":"abandon"}`，随后游戏走
      `RunScoreManager.TriggerGameOver(skipTransition:false, abandoned:true)`
      → `SetMode(DRIVING, …, SceneTransition.GAME_OVER, …)`（`RunScoreManager.cs:145-154`），
      落到「迷失黑暗」结算屏。**结算屏之后落在哪，08:0x 那次没记录 → 这里记下来（abandon_landing）。**
    """
    t0 = time.time()
    event("abandon_begin")
    r = post_action({"type": "abandonRun"}) or {}
    if not (isinstance(r, dict) and r.get("ok")):
        why = r.get("error") if isinstance(r, dict) else r
        event("abandon_fail", why="mod 不接受 abandonRun", err=str(why)[:120])
        say("放弃征程：mod 没接受（%s）" % (why,))
        return False, "abandonRun 未被接受：%s" % (why,)
    event("abandon_sent", reason=r.get("reason"), manual=r.get("manual"))

    # 落点观测：只读 /brief，记 (mode, scene, row) 的变化序列（变化才记，最后一条就是落在哪）。
    seen = []
    landed_altar = False
    while time.time() - t0 < min(limit, ABANDON_WATCH_SECS):
        b = brief()
        key = ((b or {}).get("mode"), (b or {}).get("scene"), (b or {}).get("row"))
        if not seen or seen[-1] != key:
            seen.append(key)
            event("abandon_landing", mode=key[0], scene=key[1], row=key[2],
                  btn=str((b or {}).get("btn") or "")[:30])
        if at_altar(b):
            landed_altar = True
            break
        # 结算屏的「继续」不在这里点：drive_to_altar 的 looks_wiped 分支会点（game_over_click）。
        time.sleep(1.2)
    event("abandon_ok" if landed_altar else "abandon_no_altar",
          secs=round(time.time() - t0, 1),
          landing=[[m, sc, rw] for (m, sc, rw) in seen[-4:]])
    return True, ("abandoned（已到祭坛）" if landed_altar else
                  "abandoned（落点见 abandon_landing）")


def altar_panel_click():
    """马车开到祭坛门口时会出现 screen_enter_node_panel 的「希望祭坛」按钮，点它才进祭坛。

    2026-11-20 22:47 实测：这个面板里有**两个**都叫「希望祭坛」的元素 ——
    0 号是整个面板（Layer - Story/screen_enter_node_panel），1 号才是真正能点的
    InteractButton。旧版点的是第一个命中的（面板本身），点 100 次也不进去，
    最后被当成"重开失败"停手。所以这里优先找 /InteractButton。
    """
    ui = get_ui()
    cands = []
    for c in (ui or {}).get("clickables") or []:
        p = str(c.get("path") or "")
        t = str(c.get("text") or "")
        if "screen_enter_node_panel" in p or "希望祭坛" in t:
            cands.append((0 if "InteractButton" in p else 1, c, t, p))
    if not cands:
        return False
    cands.sort(key=lambda x: x[0])
    _, c, t, p = cands[0]
    post_action({"type": "uiClick", "index": c.get("i")})
    event("altar_enter_click", text=t[:20], path=p[-40:])
    return True


def game_over_click():
    """局结束的「迷失黑暗」面板上点「继续」。返回 True=点到了。

    2026-11-21 03:05 实测踩坑：面板根节点 `Layer - Story/screen_game_over_panel(Clone)`
    的 text 也是「迷失黑暗」，而且在 clickables 里排在 0 号（比 ContinueButton 靠前）。
    旧代码按顺序取第一个命中的 → 点了面板本身，什么都不发生；于是 drive_to_altar
    "点了 3 次「继续」都没反应" → 重开失败 → 停手。这跟祭坛面板（0=面板/1=按钮）是同一类坑。
    现在优先 ContinueButton，其次 text 精确等于「继续」的元素，面板根节点永远不点。
    """
    ui = get_ui()
    best = None
    for c in (ui or {}).get("clickables") or []:
        p = str(c.get("path") or "")
        t = str(c.get("text") or "").strip()
        score = 0
        if "ContinueButton" in p:
            score += 3
        if t == "继续":
            score += 2
        if "screen_game_over_panel" in p and "ContinueButton" not in p and t != "继续":
            score = -1                      # 面板本身：点了没用
        if score > 0 and (best is None or score > best[0]):
            best = (score, c)
    if best:
        c = best[1]
        post_action({"type": "uiClick", "index": c.get("i")})
        event("game_over_continue", text=str(c.get("text") or "")[:12],
              path=str(c.get("path") or "")[-40:])
        return True
    return False


def looks_wiped(b=None):
    """画面是不是"团灭结算"（迷失黑暗）。用于把"减员退出但已经全灭"按团灭算。"""
    b = brief() if b is None else b
    s = "%s %s" % (b.get("sig") or "", b.get("btn") or "")
    return "迷失黑暗" in s


def _drive_pulse(ms=900):
    """推一下马车（**非模拟输入**）：`wagonDrive` → `Coach.Drive`（`mod\\Coach.cs:191-216`）
    只置 `m_inputAlwaysMoveForward`，并由 mod 侧协程在 holdMs 到点自动松开。

    为什么不用 `drive`（`StateBuilder.cs:609-616`，同一机制）：它没有 holdMs，要靠调用方再发一次
    `active=False`；进程在脉冲中途死掉，马车会一直自动前进。wagonDrive 的松开在 mod 侧，进程死了也会松。
    """
    return post_action({"type": "wagonDrive", "on": True, "holdMs": int(ms or 0)})


def drive_to_altar(limit=90.0):
    """团灭/弃局之后把游戏开到「希望祭坛」。就三步，每步都验：

      1) 结算屏「迷失黑暗」→ 点一次「继续」
      2) 马车出现在路上 → 按一次 W（马车会自己跑到祭坛）
      3) 祭坛门口出现 InteractButton → 点进去

    2026-11-21 00:5x 实测：这三步 8 秒就到祭坛。之前失败是因为**游戏窗口不在前台**，
    点击/按键都被吞掉（mod 返回 ok，游戏没反应），于是循环里狂点「继续」43 次。
    """
    t0 = time.time()
    cont_clicks = 0
    w_presses = 0
    while time.time() - t0 < limit:
        b = brief()
        if at_altar(b):
            return True, "已到祭坛（%.0fs）" % (time.time() - t0)
        if "希望祭坛" in (b.get("btn") or ""):
            if altar_panel_click():
                time.sleep(2)
                continue
        if looks_wiped(b):
            if cont_clicks < 3 and game_over_click():
                cont_clicks += 1
                time.sleep(2)
                continue
            # 点过「继续」但 sig 还挂着 → 说明点没生效（十有八九是游戏不在前台）
            if cont_clicks >= 3:
                return False, "点了 %d 次「继续」都没反应（游戏窗口可能不在前台）" % cont_clicks
        # 只有开局马车（row 0/None）推一下才有用：row>0 是半路中局，推也到不了祭坛。
        if (b or {}).get("scene") == "MainScene" and _row0(b):
            w_presses += 1
            _drive_pulse(900)
            time.sleep(3)
            continue
        time.sleep(2)
    return False, "%.0f 秒内没到祭坛（继续 %d 次 / W %d 次）" % (time.time() - t0, cont_clicks, w_presses)


def park_at_altar(limit=120.0):
    """团灭后把游戏停在**希望祭坛**（只进去、不启程），等人来点。

    作者口径（2026-11-20）：连续两把团灭后才停手，且要"返回希望祭坛等我操作"。
    """
    ok, note = drive_to_altar(limit)
    event("park_result", ok=ok, note=note)
    return ok, note


def reroll(limit=150.0, allow_abandon=True):
    """回祭坛 → 长按「启程」，原地开新局。返回 (ok, 说明)。

    能原地重开就原地重开；回不到祭坛才由上层走 restart_game 兜底。
    中局减员时游戏还在马车地图上，等不到祭坛 —— 先「放弃征程」再开回去。
    """
    t0 = time.time()
    event("reroll_begin")
    say("重开：先开到祭坛…")
    ok, note = drive_to_altar(30)
    if not ok and allow_abandon:
        say("没到祭坛（%s）→ 先放弃征程" % note)
        ok_a, note_a = abandon_run()
        event("reroll_abandon", ok=ok_a, note=note_a)
        say("放弃征程：%s" % note_a)
        ok, note = drive_to_altar(45)
    say("开到祭坛：%s" % note)
    # 用 drive_to_altar 的结论，不再自己重读一次 brief：
    # 多读一次会在"刚好离开祭坛"的瞬间判失败（测试里 3 例就是这么红的），
    # 实机上还会引出"明明已经开新局了又去点放弃"。
    if not ok:
        event("reroll_fail", secs=round(time.time() - t0, 1), note=note)
        return False, "没回到祭坛"
    r = post_action({"type": "altarEmbark"})
    say("已发 altarEmbark（%s）" % (r.get("ok") if isinstance(r, dict) else r))
    for _ in range(20):
        time.sleep(3)
        b2 = brief()
        if b2 and not at_altar(b2):
            event("reroll_ok", secs=round(time.time() - t0, 1),
                  scene=b2.get("scene"), mode=b2.get("mode"))
            say("已离开祭坛（%.0fs）→ 交回 runner" % (time.time() - t0))
            return True, "embarked"
    event("reroll_fail", secs=round(time.time() - t0, 1))
    return False, "没启程"


def restart_game(force=False):
    """kill → 等端口放掉 → steam 拉起 → 等主界面。返回 (ok, 耗时)。

    默认**不拉起**（作者口径：游戏由人从 Steam 打开）——这时也不杀进程，
    只写 alert(kind=need-launch) 并停手，避免"杀了开不回来"。

    force=True：**mod 还能响应也照杀**。2026-11-21 01:54 实测：游戏卡在黑屏加载
    （ScreenFaderBhv/Throbber），mod 仍然回 /brief、但版本号 n 冻结 700 不动 ——
    "mod 活着"不等于"游戏能玩"，这个场景只能重启。
    """
    t0 = time.time()
    if mod_ok() and not force:
        # 2026-11-20 22:12 实测：reroll 失败后走到这里，游戏其实好好的（就是没回到祭坛），
        # 上一版直接写 need-launch 停手 —— 这是第二次同类假警。mod 有响应就别喊人。
        say("游戏还活着（mod 有响应）→ 不是游戏挂了，不要求人工重启")
        event("restart_skip", why="game-alive")
        return False, 0.0
    if not allow_launch():
        why = "游戏挂了/需要重开，但按口径不由脚本拉起 —— 请人工从 Steam 打开游戏"
        say(why)
        event("restart_skip", why="launch-forbidden")
        write_alert("need-launch", why, mode="", scene="", row=None, gold=None)
        return False, 0.0
    event("restart_begin")
    say("杀游戏进程…")
    game_exit()
    for _ in range(20):                       # 等 mod 接口确实断开
        if not mod_ok():
            break
        time.sleep(1)
    time.sleep(3)
    if not game_launch():
        return False, time.time() - t0
    if not wait_ready(300):
        event("restart_fail", why="等不到主界面", secs=round(time.time() - t0, 1))
        return False, time.time() - t0
    time.sleep(3)
    event("restart_ok", secs=round(time.time() - t0, 1))
    say("游戏回到主界面（%.0fs）" % (time.time() - t0))
    return True, time.time() - t0


# ------------------------------------------------------------------ runner 子进程
def spawn_runner(minutes, no_retreat, tag):
    os.makedirs(LOG_DIR, exist_ok=True)
    out = os.path.join(LOG_DIR, "watch-%s.out" % tag)
    f = open(out, "w", encoding="utf-8", errors="replace")   # 文件句柄，不用管道
    env = dict(os.environ)
    env["DD2_NO_RETREAT"] = "1" if no_retreat else env.get("DD2_NO_RETREAT", "0")
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "runner.py"),
                          "--until", "alert", "--minutes", str(minutes),
                          "--report-json", "--quiet"],
                         cwd=HERE, env=env, stdout=f, stderr=subprocess.STDOUT)
    return p, f, out


def _touch(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def state_clock(t_status, t_report, t_spawn, t_hb=0):
    """状态时钟：早于本轮启动的旧快照不算数。

    否则上一轮遗留的 status.json / report.json（可能几小时前）会让本轮第一个 5s
    就被判成卡死 —— 实测 2026-11-20 18:18 启动即被杀。

    t_hb = 战斗心跳（combat3 每手一写）：一场仗打 5 分钟时 report.json 全程不动，
    不加它就会把"正常在打"判成冻死（2026-11-21 01:32 实测误杀一次）。
    """
    return max(t_status, t_report, t_spawn, t_hb)


def run_round(minutes, no_retreat, stall, tag):
    """跑一轮 runner，返回 (退出码, 是否因 stall 被杀, 是否"游戏冻死")。

    第三个返回值：mod 还能响应、但 /brief 的版本号 n 连续 120 秒没变 →
    游戏画面其实冻住了（黑屏加载/死锁），只能重启游戏，重拉 runner 没用。
    2026-11-21 01:54 实测：15 个 tick 卡在黑屏 throbber，n=700 不动。
    """
    prev_alert = os.path.join(HERE, "alert.json")
    if os.path.exists(prev_alert):
        try:                                   # 上一轮的告警留档，别直接丢证据
            os.makedirs(LOG_DIR, exist_ok=True)
            os.replace(prev_alert, os.path.join(LOG_DIR, "alert-%s.json" % tag))
        except OSError:
            try:
                os.remove(prev_alert)
            except OSError:
                pass
    try:
        p, f, out = spawn_runner(minutes, no_retreat, tag)
    except Exception as e:
        say("runner 起不来：%s: %s" % (type(e).__name__, e))
        event("spawn_fail", error=str(e)[:200])
        return 127, False
    say("runner 起来了（pid=%d，日志 %s）" % (p.pid, os.path.basename(out)))
    t_spawn = time.time()
    killed = False
    frozen = False
    last_n, n_since = None, time.time()
    try:
        while p.poll() is None:
            time.sleep(5)
            last = state_clock(_touch(STATUS), _touch(REPORT), t_spawn, _touch(HB))
            mode_now = status_mode()
            lim = stall_limit(mode_now, stall)
            # 游戏是否真的还活着：看 mod 的版本号 n 有没有变（每 5 秒采样一次，150 字节）
            nb = brief()
            if nb.get("n") != last_n:
                last_n, n_since = nb.get("n"), time.time()
            if stalled(last, time.time(), lim):
                frozen = mod_ok() and (time.time() - n_since) > 120
                say("%.0f 秒没有任何状态更新（%s 档预算 %.0fs）→ 判定卡死，杀掉 runner%s"
                    % (lim, mode_now or "?", lim,
                       "（且 n 已冻结 %.0fs → 游戏本身冻住了）" % (time.time() - n_since)
                       if frozen else ""))
                event("stall_kill", mode=mode_now, frozen=frozen,
                      n=last_n, n_idle=round(time.time() - n_since, 1))
                p.kill()
                killed = True
                break
        code = p.wait()
    finally:
        if p.poll() is None:                   # 中断/异常：别把 runner 留成孤儿进程
            p.kill()
        f.close()
    return code, killed, frozen


def main():
    # 单实例：两个 watchdog 会各自拉起 runner，双驱动（2026-11-21 撞车事故）。
    try:
        from instance_lock import acquire, release, other_process, EXIT_ALREADY_RUNNING
        olds = other_process(r"watchdog\.py")
        if olds:
            print("[watchdog] 已有 watchdog 在跑（PID %s），本进程退出。退出码=%d"
                  % (olds, EXIT_ALREADY_RUNNING), file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        _wd_lock = acquire("watchdog")
        if _wd_lock is None:
            return EXIT_ALREADY_RUNNING
        import atexit
        atexit.register(release, _wd_lock, "watchdog")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="DD2 挂机看门狗")
    ap.add_argument("--hours", type=float, default=4.0, help="守多久（默认 4）")
    ap.add_argument("--minutes", type=float, default=120.0, help="每轮 runner 跑多久（默认 120）")
    ap.add_argument("--rounds", type=int, default=0, help="最多重启游戏几次（0=不限，但有每小时上限）")
    ap.add_argument("--stall", type=float, default=480.0,
                    help="状态文件多久没更新算卡死的上限（默认 480s）。实际阈值按画面取更小的："
                         "战斗 240s / 客栈 300s / 马车 240s")
    ap.add_argument("--once", action="store_true", help="只跑一轮（等价直接开 runner）")
    ap.add_argument("--retreat", action="store_true", help="允许撤退（默认死战到底）")
    ap.add_argument("--no-reroll", action="store_true",
                    help="团灭/减员后停手等人（旧口径）；默认是自动重开一局")
    ap.add_argument("--wipe-stop", nargs="?", const=1, type=int, default=0,
                    help="连吃几次团灭才停手（默认 0=不停，一直重开）。"
                         "例：--wipe-stop 2 = 第二把团灭才停、回希望祭坛等人；"
                         "只写 --wipe-stop 等于 1。减员(7)始终自动重开")
    ap.add_argument("--no-qq", action="store_true",
                    help="停手时不往 QQ 发通知（默认发，走 Aurabot/SnowLuma）")
    ap.add_argument("--runs", type=int, default=0,
                    help="打满几把就停（0=不限）。第一把算 1，每次重开 +1；"
                         "例：--runs 8 = 第 8 把结束后停在祭坛并退出")
    a = ap.parse_args()

    no_retreat = not a.retreat
    deadline = time.time() + a.hours * 3600
    restarts, rerolls, rounds, tag = [], [], 0, time.strftime("%m%d-%H%M")
    wipes = 0
    done_prev = runs_done_count()      # 上一次 watchdog 已经打完的局数（持久化）
    say("看门狗启动：守 %.1f 小时，每轮 %.0f 分钟，硬打=%s，冻死阈值 %.0fs，"
        "团灭停手阈值=%s"
        % (a.hours, a.minutes, "是" if no_retreat else "否", a.stall,
           ("第 %d 把" % a.wipe_stop) if a.wipe_stop else "不停"))
    event("start", hours=a.hours, minutes=a.minutes, no_retreat=no_retreat, wipe_stop=a.wipe_stop)

    while time.time() < deadline:
        rounds += 1
        budget = max(1.0, min(a.minutes, (deadline - time.time()) / 60.0))
        try:
            code, killed, frozen = run_round(budget, no_retreat, a.stall, "%s-r%d" % (tag, rounds))
        except KeyboardInterrupt:
            say("被中断")
            return 130
        if killed:
            code, action, why = 2, "restart", "stall"
            # 2026-11-20 21:20 实测的假警：runner 被冻结判死，随后直接按"游戏挂了"写
            # need-launch 停手 —— 可那时游戏好好地在战斗回合选择里（/brief 正常）。
            # 卡的是 runner，不是游戏。先问一句 mod：还活着就只重拉 runner。
            # 2026-11-21 01:54 补：mod 能回话但 n 冻结 = 游戏冻死（黑屏 throbber），
            # 这时重拉 runner 只会再卡一次，必须强制重启游戏。
            if mod_ok() and not frozen:
                say("stall 后 mod 仍有响应 → 游戏没死，只重拉 runner（不写 need-launch）")
                event("stall_respawn", why="mod-alive")
                action, why = "rerun", "stall-respawn"
            elif frozen:
                say("mod 还能响应，但状态版本号冻结 → 游戏冻死，强制重启游戏")
                event("stall_game_frozen", why="n-frozen")
            else:
                say("stall 后 mod 也没了 → 当游戏挂了处理"
                    "（另：这次 kill 会把减员基线打回残队人数，见 runner.run_baseline）")
                event("stall_game_gone", why="mod-down")
        else:
            # 2026-11-21 00:44 实测：减员(7) 会先于团灭触发 —— runner 看到 3 人就退了，
            # 可那场其实已经全灭（画面是「迷失黑暗」）。上一版只按退出码算，团灭没被计数，
            # 于是"明明团灭了却又开了一把"。这里以画面为准。
            if code == 7 and looks_wiped():
                say("减员退出，但画面已经是团灭结算 → 按团灭计数")
                event("member_lost_but_wiped")
                code = 4
            if code == 4:
                wipes += 1
                say("团灭计数：第 %d 把（停手阈值 %s）" % (wipes, a.wipe_stop or "不停"))
            action, why = decide(code, a.wipe_stop, wipes)
        say("第 %d 轮结束：退出码 %s（%s）→ %s" % (rounds, code, why, action))
        event("round_end", round=rounds, code=code, why=why)

        if action == "restart" and mod_ok() and not frozen:
            # 2026-11-20 23:52 实测：no-progress(8) 判完要走"重启游戏"，但游戏明明还活着，
            # restart_game 直接返回 False，上层却当成"游戏没救回来"停手 —— 整晚白停一次。
            # 卡的是画面/runner，不是游戏进程：直接重拉 runner 接着打。
            say("要重启游戏，但 mod 仍有响应 → 只重拉 runner")
            event("restart_skipped_alive", code=code)
            action = "rerun"
        if action == "stop":
            if code == 4:
                say("第 %d 把团灭 → 按约定停手：先把游戏停在希望祭坛，等人来点。" % wipes)
                ok_p, note_p = park_at_altar()
                say("回祭坛：%s" % note_p)
                event("park_at_altar", ok=ok_p, note=note_p)
            elif code == 5:
                say("祭坛等人点『启程』 → 停手。")
            else:
                say("停止原因：%s（退出码 %s）→ 停手，等人看" % (why, code))
            event("paused", code=code, why=why)
            if not a.no_qq:
                notify_qq(stop_message(code, why, _read_alert()))
            return code
        if a.once:
            return 0
        if action == "rerun":
            now = time.time()
            ok, note = can_restart(restarts, now)      # 和重启共用限额，防 stall 风暴
            if not ok:
                say(note + " → 收工，等人看")
                event("give_up", why="stall-respawn")
                return 6
            restarts.append(now)
            say("→ 重拉 runner（游戏没动，接着上一局打）")
            continue
        if action == "reroll":
            if a.no_reroll:
                say("团灭/减员 → 按 --no-reroll 停手（alert.json 已写 %s）" % why)
                event("paused", code=code, why=why)
                if not a.no_qq:
                    notify_qq(stop_message(code, why, _read_alert()))
                return code
            # 把数：初始那局算第 1 把，每次重开 +1。到期就停手回祭坛，不再开新局。
            done_runs = done_prev + 1 + len(rerolls)
            event("run_end", nth=done_runs, code=code, why=why)
            set_runs_done(done_runs)
            say("第 %d 把结束（%s）" % (done_runs, why))
            if a.runs and done_runs >= a.runs:
                say("已打满 %d 把 → 停手：把游戏停在希望祭坛，等人来点。" % a.runs)
                ok_p, note_p = park_at_altar()
                say("回祭坛：%s" % note_p)
                event("runs_done", runs=done_runs, ok=ok_p, note=note_p)
                return 0
            now = time.time()
            ok, note = can_reroll(rerolls, now)
            if not ok:
                say(note + " → 收工，等人看")
                event("give_up", why=note)
                return 6
            rerolls.append(now)
            event("reroll", code=code, why=why, nth=len(rerolls))
            say("→ 重开本局（第 %d 次）" % len(rerolls))
            ok2, note2 = reroll()
            if not ok2:
                say("原地重开失败（%s）→ 再试一次（多按一次 W 往祭坛开）" % note2)
                ok2, note2 = reroll()
            if not ok2:
                say("原地重开失败（%s）→ 重启游戏兜底" % note2)
                if not restart_game(force=frozen)[0]:
                    say("兜底也失败 → 停手，等人看")
                    event("give_up", why="reroll-restart-failed")
                    return 6
            continue
        if action == "restart":
            now = time.time()
            ok, note = can_restart(restarts, now)
            if not ok:
                say(note + " → 收工，等人看")
                event("give_up", why=note)
                return 6
            if a.rounds and len(restarts) >= a.rounds:
                say("重启次数到上限（%d）→ 收工" % a.rounds)
                return 6
            restarts.append(now)
            if not restart_game(force=frozen)[0]:
                say("游戏没救回来 → 停手，等人看")
                return 6
    say("守满 %.1f 小时，正常收工" % a.hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())

