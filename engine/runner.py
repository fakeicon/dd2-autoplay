"""runner.py —— tick 模式：长按「前进」→ 开车 → 节点面板 → 剧情长按 → 战斗（保命优先）→ 拿奖励。

每个 tick：读 /state → 选一个动作 → 执行 → 覆盖写 status.json → 打一行日志。
「前进」这类宽按钮（含巢穴/湮灭壁垒的「前往下一场战斗？」确认框）必须按住才生效，
见下面 do_hold_button()；单击的 uiClick 对它无效。
同一个画面连续 N 个 tick 没有任何变化 → 打一行 STUCK 并退出（exit 2），
这样 job_output(wait:true) 不用等满 timeout，卡住了也能马上看到原因。

用法:
  python runner.py                 # 跑 10 分钟（和以前一样）
  python runner.py --minutes 1     # 跑 1 分钟
  python runner.py --ticks 30      # 跑 30 个 tick
  python runner.py --dry-run       # 只读状态、只写 status.json，不操作游戏
退出码: 0=时间/tick 用完正常结束  2=同一画面卡住  3=连不上 MOD
"""
import argparse
import json
import os
import sys
import time

import combat3 as C
import dd2poll
import inn
import loadout
import stuckwatch as SW

_prev_mode = [None]   # 上一 tick 的 mode：决定 /state 能不能吃缓存（A 层优化）

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_DEFAULT = os.path.join(HERE, "status.json")
ALERT_DEFAULT = os.path.join(HERE, "alert.json")
# 本轮 alert 路径。main 里算出来后写进来，do_tick 里只读 —— 2026-11-20 19:11 事故：
# do_tick 里直接引用 main 的局部变量 alert_path → 每个 tick 都 NameError → 判 stall。
_ALERT_PATH = [ALERT_DEFAULT]
# 每局开局的选人：2026-11-23 23:4x 作者口径 —— 就按当前这局的人打，不再换阵容。
#   1 强盗 highwayman   2 麻风病人 leper   3 老兵 man_at_arms   4 修女 vestal
#   依据：23:40 partyInfo 实测这 4 人 status=party（Dismas/Baldwin/Barristan/Junia），
#        道途 hwy_rogue / lep_poet / maa_sergeant / ves_confessor。顺序按 partyInfo 的队列顺序。
PARTY = "highwayman,leper,man_at_arms,vestal"
# 2026-11-23 作者口径：只钉阵容，道途/技能不自动改（hero_select 与旅馆两处都不动）。
# 要恢复旧的"自动配道途 + 点名技能"，设环境变量 DD2_AUTO_LOADOUT=1。
AUTO_LOADOUT = os.environ.get("DD2_AUTO_LOADOUT", "") == "1"

# 剧情事件里英雄立绘的候选长按点（游戏坐标，原点左下）。四个英雄大致横排在左下区域，
# 逐 tick 轮换着试，哪个是"可选"的由游戏决定。
STORY_POINTS = sorted(
    [(x, y) for y in (400, 500, 620, 300, 720) for x in (960, 860, 1060, 760, 1160, 660, 1260)],
    key=lambda p: (p[0] - 959) ** 2 + (p[1] - 412) ** 2)
_story_try = [0]
# 2026-11-21 21:5x：同一块剧情屏只试一次 storyChoice 接口。这块屏上 OnChoiceSelected 会"成功"
# 但 EventSelectStoryChoice 被游戏丢掉，每 tick 都回 ok 就把后面的 uiHold 轮按短路了（8 tick 判 stuck）。
_story_api_sig = [None]
_inn_dumped = [False]   # 旅馆/子界面 /ui 只落盘一次
_trainer_probe = [False]  # 训练师页只探一次


def click_by(u, pred_key, path_key=None):
    """按文字/路径找第一个可点元素。"""
    for x in u.get("clickables", []):
        if pred_key in (x.get("text") or "") or (path_key and path_key in (x.get("path") or "")):
            return x
    return None


def click_el(el, dry_run=False):
    """点一个 /ui 元素：**先按语义点（路径 → 序号），最后才退坐标**。

    2026-11-22 口径（方案 C）：坐标点击只做兜底。mod 的 `uiClick` 支持 `pick`=完整路径，
    路径对不上就点空（无损），不会像 index 那样因为两次 Collect 的列表长度不同而点到别的元素。
    2026-11-20 01:5x 的"连点 8 次返回游戏没反应"是 index 漂移，按路径点不会有这个问题。
    """
    if dry_run or not el:
        return False
    path = el.get("path") or ""
    if path:
        try:
            r = C.act({"type": "uiClick", "pick": path})
            if isinstance(r, dict) and r.get("ok"):
                return True
        except Exception:
            pass
    if el.get("i") is not None:
        try:
            r = C.act({"type": "uiClick", "index": el.get("i")})
            if isinstance(r, dict) and r.get("ok"):
                return True
        except Exception:
            pass
    x, y = el.get("x"), el.get("y")
    if x is not None and y is not None:
        try:
            C.act({"type": "uiPoint", "x": int(x), "y": int(y), "holdMs": 80})
            return True
        except Exception:
            pass
    return False


def combat_fingerprint(s):
    """(回合, 回合内序号, 当前出手人, 全体血量) —— 用来判断战斗到底有没有推进。"""
    actors = s.get("actors") or []
    hp = ",".join("%s:%s" % (a.get("guid"), a.get("hp")) for a in actors)
    return (s.get("round"), s.get("turn"), s.get("currentActorGuid"), hp)


_combat_sig = [None, 0]
_drive_since = [0.0]        # 这一段 "drive" 从什么时候开始（用来给开车设时间上限）
_stop_since = [0.0]         # 车从什么时候开始停着（speed<0.5），用来区分"起步"和"真停住"
_road_try = [None, 0.0]     # 上次在岔路口选了哪条 + 什么时候（避免每 tick 重选）
_NO_RETREAT = [None]        # True=死战到底；写进 status.json，外面一眼能看到口径
DRIVE_STALL_SECS = 240.0    # 同一段 drive 连续这么久就不再给"正在开车"豁免
# 2026-11-23：一次 C.main 的手数上限。40 太小 —— combat_log 里 101 行 turns>=40，
# 11-23 的 8 场 unknown 有 7 场正好撞在 40（敌人还剩 1~3 个），同一场被拆成两次调用两行日志。
# 60 只是把"大多数场次一次打完"的余量，不是调难度；环境变量可覆盖（回滚不用改代码）。
COMBAT_MAX_STEPS = int(os.environ.get("DD2_COMBAT_MAX_STEPS") or 60)   # 空串/没设都回默认
_nudge_at = [0.0]           # 上次"真按一次 W 叫车"的时间
_need_focus = [0]           # 剧情屏连续几次想真鼠标但游戏不在前台
_modal_try = [0]            # 全屏模态连着点了几次（空格只试一次，之后点正中）
_input_block = [0]          # 车停着 + 游戏自己把输入锁了（inputEnabled=False）连续几拍
# 车停着时的现场（写进 report.json，只在卡住那几拍加）：inp=1/0 游戏还接不接受输入，
# blk=第一个 =False 的 canDrive 委托（是谁在拦车）。正常行驶的行不带这两个字段，守住 ≤120B。
_drive_diag = {"inp": None, "blk": None}
INPUT_BLOCK_TICKS = 12      # 连续这么久"锁输入 + 无可点面板"就不刷 drive，直接告警停手

# 2026-11-25 卡墙真因：游戏自己的"自动挡" GO_TO_TARGET 没有超时 —— 它只在
# AdvanceTowardsTarget 够到目标时退出（StageCoachInternalState.cs:161-181），
# 这个模式下按 W 无效（:219-249 自己算 accelMult 且清 alwaysForward），
# 拦车委托也无效（:470-483 EnableInput 只动 NO_INPUT↔PLAYER_INPUT）。
# 于是"追一个到不了的点"时轮速照旧 12.1，位移却被 ClampPosToRoad 钳在路带边上
# （StageCoachVehicleControlBhv.cs:513-636 / :799-819）—— 车贴墙，但没有任何判据会响。
# 判据必须带**位移**：岔路口的 SLOW_DOWN 也是 GO_TO_TARGET（TriggerIntersectionBhv.cs:149-155），
# 只按时间取消会把它停在半路，岔路状态机就等不到减速点、永远出不来。
AUTO_MODE_MAX_SECS = 4.0    # 同一个 GO_TO_TARGET 窗口最多忍这么久
AUTO_MODE_MIN_MOVE = 2.0    # 这么久里至少挪这么多米，否则算空转
_goto_ref = [None]          # 本窗口起点 (t, x, z)；None = 当前不在 GO_TO_TARGET


def wagon_nudge_needed(stopped_secs, panel_open, blocks, mode_now, since_nudge,
                       wait=6.0, gap=10.0):
    """车停着时该不该直接真按一次 W。

    2026-11-20 作者实测口径：打完仗回到路上，**按一下 W 车就自己跑了**。
    而这里原来先去 unblock / goNode / 改驾驶模式绕圈，上一局就是这么空转了
    十几个 tick、车一步没动。所以：没面板、没人锁输入、驾驶模式也对，就先按 W。
    """
    if panel_open or blocks or stopped_secs < wait:
        return False
    if mode_now and mode_now != "PLAYER_INPUT":
        return False
    return since_nudge >= gap


def drive_progressing(action, speed, elapsed):
    """这个动作算不算"画面在推进"（卡死判定用）。

    2026-11-20 01:1x 实测：暂停菜单开着时 `coach` 仍报 speed=12.1（残留），
    runner 一路按 w 什么也不动，而旧判据 `action=="drive" and speed>0.5` 一直给豁免
    → unchanged 永不累加 → 卡了 40 分钟都没 STUCK。所以开车这条路必须有时间上限。
    """
    if action == "combat":
        return True
    return action == "drive" and speed > 0.5 and elapsed < DRIVE_STALL_SECS


def drive_since_after(action, prev_action, since, now=None):
    """本 tick 结束后"这一段 drive 从什么时候开始"。

    2026-11-20 02:0x 实测的真 bug：`last_action` 只在 `if not quiet and ...` 里更新，
    而生产环境跑的是 `--quiet` → `last_action` 永远是 None → 每个 tick 都
    `last_action != "drive"` → 起点被反复重置成"现在" → 自动挡空转 >60 秒那条修复
    永远不触发，车就原地按 w 空转了 5 分钟（speed 残留 12.1、unchanged 一直是 0）。
    所以起点只在"动作真的从别的变成 drive"那一 tick 记，连续 drive 一律沿用。
    """
    if action != "drive":
        return 0.0
    if prev_action == "drive" and since:
        return since
    return now if now is not None else time.time()



_gear_at = [0.0]
_gear_fail_sig = [None]   # 上次"装不上"的那批 id，用来决定要不要马上重试
_ov_tries = [0]           # 暂停菜单连点了几次（≥3 换 Esc，见 do_tick）


def _in_combat_now(s):
    """现在是不是在战斗里（含战斗结算）。

    2026-11-21 08:4x 现场：装备的 UI 兜底会按 C 打开角色表，而它原本只在"车停住"时
    才被放行 —— 战斗里车也不动，于是角色表被开在战斗画面上，技能格全被遮住，
    出招被拒、整个回合白烧。装备只在马车/客栈做，战斗里一律不碰。
    """
    s = s or {}
    return (str(s.get("mode") or "").lower() == "combat"
            or "combat" in str(s.get("scene") or "").lower())


def maybe_equip(speed=0.0, force=False):
    """背包里有饰品/战斗道具就装到角色身上。带节流 + 回读验证。

    2026-11-19 实测的无效路径：时速 12~14 时发起拖拽，3 件饰品一次都没上身，
    每个 tick 重试一遍（日志刷 `[装备][失败] 装备槽可能没解锁`）。
    所以现在：
      - 车还在动（speed>=0.5）就不做 —— 画面在动，拖拽必然落空；
      - 同一批 id 装不上 → 5 分钟后再试一次，不再每 30 秒刷同一条失败；
      - 装上了 → 30 秒后再看（可能又捡到新的）。
    """
    now = time.time()
    if not force and float(speed or 0.0) >= 0.5:
        return None
    if now - _gear_at[0] < 30:
        return None
    try:
        rep = inn.equip_from_bag()
    except Exception as e:
        print("[装备][失败] %s: %s" % (type(e).__name__, e), flush=True)
        _gear_at[0] = now + 120
        return {"ok": False, "equipped": [], "failed": [], "error": str(e)}
    # 2026-11-21：顺手把背包里的**战斗道具**塞给空着道具栏的队员（每人 1 格）。
    # 和饰品互不影响；失败只打印，不影响饰品结果。
    try:
        ci = loadout.equip_combat_items()
        if ci:
            print("[战斗道具] %s" % ci, flush=True)
            rep["combat_items"] = ci
    except Exception as e:
        print("[战斗道具][失败] %s: %s" % (type(e).__name__, e), flush=True)
    # 2026-11-23（A2）：战斗技能点名表——db/skills.loadout.json 里没写的 dataId 一律不动。
    # 2026-11-23 作者口径：默认不再自动改技能（开 DD2_AUTO_LOADOUT=1 才恢复）。
    if AUTO_LOADOUT:
        try:
            sk = loadout.apply_skills()
            if sk:
                print("[技能] %s" % sk, flush=True)
                rep["skills"] = sk
        except Exception as e:
            print("[技能][失败] %s: %s" % (type(e).__name__, e), flush=True)
    # 2026-11-21：马车装备——商店买回来后在背包里，这里装到马车上（等价于马车匠界面）。
    try:
        cu = loadout.install_coach_upgrades()
        if cu:
            print("[马车装备] %s" % cu, flush=True)
            rep["coach"] = cu
    except Exception as e:
        print("[马车装备][失败] %s: %s" % (type(e).__name__, e), flush=True)
    failed = tuple(rep.get("failed") or [])
    if rep.get("equipped"):
        _gear_at[0] = now + 30
        _gear_fail_sig[0] = None
    elif failed and failed == _gear_fail_sig[0]:
        _gear_at[0] = now + 300          # 同一批还是装不上 → 别刷屏，5 分钟后再试
    elif failed:
        _gear_fail_sig[0] = failed
        _gear_at[0] = now + 60
    else:
        _gear_at[0] = now + 90
    return rep


def gear_brief(rep):
    """把装备结果压成一行放进 status.json（不能太大）。"""
    if not rep:
        return {}
    a = rep.get("audit") or {}
    out = {"n": len(rep.get("equipped") or []), "fail": (rep.get("failed") or [])[:3]}
    bare = a.get("bare") or []
    if bare:
        out["bare"] = bare[:4]
    if rep.get("error"):
        out["error"] = str(rep["error"])[:80]
    return out


def write_status(path, **fields):
    """原子覆盖写 status.json，给外面的人一条实时读的状态。"""
    fields["ts"] = time.strftime("%H:%M:%S")
    fields["epoch"] = round(time.time(), 3)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def clear_alert(path=ALERT_DEFAULT):
    try:
        os.remove(path)
    except OSError:
        pass


def write_alert(kind, why, s=None, path=ALERT_DEFAULT):
    """只写"需要人/模型进场"的那一种事件（≤300 字节），正常跑不产生任何要读的东西。

    省 token 的第一优先级不是压缩 prompt，而是让模型**少被叫起来**：
    以前要把 runner.log / report.json 轮着看才知道出没出事；现在只有 alert.json 出现时
    才需要看一眼，正常挂机模型可以完全不醒。
    """
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "why": str(why)[:240]}
    if s:
        rec.update({"mode": s.get("mode"), "scene": s.get("scene"),
                    "row": s.get("mapRow"), "gold": s.get("gold")})
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass
    # 一行显眼输出：外层 job 的日志里能直接看到"为什么被叫醒"，不用再去 cat alert.json
    print("[告警] %s: %s" % (kind, str(why)[:160]), flush=True)


def real_click(x, y, hold_ms=350, hover_ms=250, allow_focus=None):
    """点/按住（游戏坐标）。**不碰鼠标**：走 mod 的 uiPoint（射线 + 事件分发）。

    2026-11-23：连"进程内注入 pointerClick"也去掉了 —— 那仍然是往 Mouse.current 塞事件。
    现在的实现是 EventSystem.RaycastAll 命中后直接 ExecuteEvents（down/up/click），
    等于把玩家点击的**后果**直接交给游戏，不经过输入设备。
    返回 (x, y) = 已发出；None = mod 没响应/该点没东西（调用方按"没点成"处理）。
    """
    try:
        r = C.act({"type": "uiPoint", "x": x, "y": y, "holdMs": max(0, hold_ms)})
    except Exception:
        return None
    if isinstance(r, dict) and r.get("ok"):
        return (x, y)
    return None


# ---------------------------------------------------------------- 长按按钮「前进」
# 游戏里这种宽按钮（深蓝底、右边带箭头、上面一个菱形装饰）都是"按住不放才生效"，
# 不是单击。已确认原型的两个：
#   1) 巢穴（LAIR）/ 湮灭壁垒（OBLIVION'S RAMPART）打完一场后的确认框：
#      正文「前往下一场战斗？」，按钮「前进」/「逃离！」。
#      Assets.Code.UI.DungeonConfirmationDialogBhv：OnAdvancePointerDown 把
#      m_advanceHeld 置 true，Update() 用 unscaledDeltaTime 往 m_accAdvanceTimeHeld
#      累加，够 m_holdTime 才 OnConfirm()；OnPointerUp 只是清零。
#      → uiClick / uiInvoke 一定没反应（等于按下立刻松开），必须按住。
#   2) 通用长按按钮：Assets.Code.UI.HoldToActionBhv（m_holdTime / m_accTimeHeld /
#      m_held，同样是 Update 里累计到点才触发 m_actionEvent）。
# uiHold 的实现正好是 pointerEnter → pointerDown → 等 seconds → pointerUp，全程进程内，
# 不用抢真实鼠标、不切前台。按住时长给 2s，比 m_holdTime 宽一点没关系。
# 万一 uiHold 被遮罩吃掉射线（弹窗的 scrim），兜底走游戏自己的输入动作 "Submit"：
# HandleOnConfirmPressed 里 performed = 按下、canceled = 松开，正好对应长按的起止。
HOLD_BUTTON_TEXT = "前进"
HOLD_SECONDS = 2.0
_hold_tries = {}   # 同一画面按了 N 次都没动静就不再按，交回正常逻辑

# 巢穴（LAIR）/湮灭壁垒：最多打两层，第三层（头目）不打 —— 攻略 §8.6/§4：
# 「逃离保留前两层奖励，不亏」。用「前往下一场战斗？」确认框出现的次数计层数。
_lair_depth = [0]
_lair_key = [None]
LAIR_ESCAPE_AFTER = 1   # 打完 1 层后出现的下一次「前进」= 第 3 层，直接逃离


def find_text_button(u, label):
    for x in u.get("clickables", []):
        if (x.get("text") or "").strip().startswith(label):
            return x
    return None


def find_hold_button(u):
    """屏幕上有文字正好是「前进」的按钮就返回它。"""
    for x in u.get("clickables", []):
        if (x.get("text") or "").strip() == HOLD_BUTTON_TEXT:
            return x
    return None


def press_esc(pause=0.15):
    """关子界面/取消弹窗：发游戏的 ExitMenu 动作（= CommonUiBhv.GoBack）。

    机制（2026-11-23 09:1x 核实）：mod `simAction` → `InputSimulatorUtils.SimulateActionClick`
    （`InputSimulatorUtils.cs:33-69`）= 调 UI 已注册的回调，**不碰输入设备**，隔一帧再发 canceled。
    """
    C.act({"type": "simAction", "name": "ExitMenu"})
    if pause:
        time.sleep(pause)


def do_hold_button(s, u, dry_run=False):
    """有「前进」长按按钮就按住它。返回 (action, delay, extra)，没有就 None。"""
    btn = find_hold_button(u)
    if not btn:
        _hold_tries.clear()
        return None

    sig = ((s.get("scene") or ""), len(u.get("clickables") or []),
           len(u.get("texts") or []))
    tries = _hold_tries.get(sig, 0)
    if tries >= 3:
        return None          # 连按 3 次画面没变：多半不是长按键，别再耗时间
    _hold_tries[sig] = tries + 1

    if dry_run:
        return "hold_button", 2, {"note": "dry-run 不按前进", "tries": tries + 1}

    # 是不是那个确认框：实测 path 是 DungeonConfirmationDialog/Widget/ButtonLayout/ConfirmBtn，
    # 而 /ui 的 texts 里只有「前进」两个字（标题/正文不在 texts 里），所以按 path 认，别按文本认。
    dialog = any("DungeonConfirmationDialog" in (x.get("path") or "")
                 for x in u.get("clickables", [])) \
        or any("前往下一场战斗" in (t or "") for t in u.get("texts", []))

    if dialog:
        lk = ((s.get("scene") or ""), s.get("mapRow"))
        if _lair_key[0] != lk:
            _lair_key[0] = lk
            _lair_depth[0] = 0
        # 已经打完 LAIR_ESCAPE_AFTER 层：第三层（头目）不打，长按「逃离」
        if _lair_depth[0] >= LAIR_ESCAPE_AFTER:
            esc_btn = find_text_button(u, "逃离")
            if esc_btn:
                if dry_run:
                    return "lair_escape", 2, {"note": "dry-run 不按逃离"}
                try:
                    C.act({"type": "uiHold", "text": esc_btn["text"], "seconds": HOLD_SECONDS})
                except Exception:
                    pass
                _lair_depth[0] = 0
                return "lair_escape", 3, {"depth": LAIR_ESCAPE_AFTER}
        _lair_depth[0] += 1
    ok = False
    try:
        ok = bool(C.act({"type": "uiHold", "text": HOLD_BUTTON_TEXT,
                         "seconds": HOLD_SECONDS}).get("ok"))
    except Exception:
        pass

    time.sleep(0.8)
    try:
        still = find_hold_button(C.get("/ui")) is not None
    except Exception:
        still = True

    if still and dialog:
        # 兜底 1：按住确认（Submit 动作：start=按下 → end=松开）。
        # 2026-11-23 09:1x 核实：simAction 调的是游戏已注册的 Submit 回调（不碰输入设备）。
        try:
            C.act({"type": "simAction", "name": "Submit", "phase": "start"})
            time.sleep(HOLD_SECONDS + 0.5)
            C.act({"type": "simAction", "name": "Submit", "phase": "end"})
        except Exception:
            pass
        time.sleep(0.6)
        try:
            still = find_hold_button(C.get("/ui")) is not None
        except Exception:
            still = True
        if still:
            # 兜底 2：直接喂游戏输入动作 Submit（started/performed → canceled）
            C.act({"type": "call", "name": "InputSimulatorUtils",
                   "method": "SimulateActionStart", "args": ["Submit"]})
            time.sleep(HOLD_SECONDS + 0.5)
            C.act({"type": "call", "name": "InputSimulatorUtils",
                   "method": "SimulateActionEnd", "args": ["Submit", False]})
            return "hold_advance_sim", 2.0, {"via": "sim", "uiHoldOk": ok,
                                             "tries": tries + 1}
        return "hold_advance_key", 2.0, {"via": "space", "uiHoldOk": ok,
                                         "tries": tries + 1}

    return "hold_advance", 2.0, {"via": "uiHold", "uiHoldOk": ok,
                                 "tries": tries + 1, "still": still}


# ---------------------------------------------------------------- 通用「是/否」弹窗
# 2026-11-19 22:14 卡死实例：客栈里点开角色面板再离开，弹出
# 「仍有技能可以装备！确定要离开吗？」+ 是/否。没人点 → inn_idle 空转 40 tick 判死。
# 只对"确定要离开"这类安全文案点「是」；未知弹窗一律不动
# （怕把"确定要放弃这次远征？"点掉）。
# 2026-11-20 收紧：以前白名单里有"是否离开"，理论上能匹配"是否离开这次远征？"，
# 一句文案就能让脚本自己扔掉一整局。现在双层守：安全短语命中 + 危险词否决。
DIALOG_SAFE_YES = ("仍有技能", "确定要离开", "确定离开", "离开此界面")
DIALOG_DANGER = ("远征", "放弃", "投降", "退出游戏", "退出到", "清除", "删档", "结束此局",
                 "结束远征", "全部卸载")


def dialog_confirm_yes(u):
    """认出"安全可以点是"的确认框 → 返回「是」按钮，否则 None。"""
    texts = " ".join(t or "" for t in (u or {}).get("texts") or [])
    if any(k in texts for k in DIALOG_DANGER):      # 危险词优先：宁可卡住，也不自己扔存档
        return None
    if not any(k in texts for k in DIALOG_SAFE_YES):
        return None
    return next((c for c in (u or {}).get("clickables") or []
                 if (c.get("text") or "").strip() == "是"), None)


# ---------------------------------------------------------------- 暂停菜单 / 崩溃报告弹窗
# 2026-11-19 22:40 实测卡死：一段剧情/战斗后同时弹出「隐私条款 + 即将被发送的用户报告」
# 模态框和 PauseMenu，inn 流程看不到可点元素 → inn_rest 空转 40 tick 判死。
# 处理：报告框只点「取消」（不发送），暂停菜单只点「返回游戏」，其它按钮一概不碰。
CRASH_REPORT_HINTS = ("隐私条款", "即将被发送的用户报告")
PAUSE_RETURN_PATH = "PauseMenuButton - Return"
REPORT_DECLINE_TEXT = "取消"

# 会挡住所有输入、但**不会让 speed 归零**的全屏面板（2026-11-20 01:1x 实测卡了 40 分钟）：
# 暂停菜单开着时 `coach` 仍然报 driving=True / speed=12.1（残留值），于是 runner 每 tick
# 按一次 w、什么也不动，而 `progressing` 把它当"正在开车" → unchanged 永远不清零 → 永不 STUCK。
# 修法两条：① map 里也认出暂停菜单并点「返回游戏」；② drive 连续太久不给豁免（见 DRIVE_STALL_SECS）。
OVERLAY_PANELS = ("PauseMenuButton - Return",)   # 按 path 认，稳过按文字认


def blocking_overlay(u):
    """认出挡住输入的全屏面板 → 返回要点的按钮（或 None）。"""
    els = (u or {}).get("clickables") or []
    for key in OVERLAY_PANELS:
        b = next((c for c in els if key in (c.get("path") or "")), None)
        if b is not None:
            return b
    return None


def dialog_decline_report(u):
    """崩溃报告/隐私条款弹窗 → 返回「取消」按钮，否则 None。"""
    texts = " ".join(t or "" for t in (u or {}).get("texts") or [])
    if not any(k in texts for k in CRASH_REPORT_HINTS):
        return None
    return next((c for c in (u or {}).get("clickables") or []
                 if (c.get("text") or "").strip() == REPORT_DECLINE_TEXT), None)


def pause_return(u):
    """PauseMenu 里的「返回游戏」→ 返回按钮，否则 None。"""
    return next((c for c in (u or {}).get("clickables") or []
                 if PAUSE_RETURN_PATH in (c.get("path") or "")), None)


MODAL_ROOT = "Layer - Modal/"
# 只有"纯动画、点一下屏幕正中就过"的全屏模态才允许盲点。必须按名字白名单认，
# 不能"没找到子按钮就算纯动画"：旅馆里 `screen_confirmation_dialog_inn_embark`
# 的「是/否」按钮挂在另一个父节点下，靠"子元素"判会把它误判成纯动画，
# 结果每 tick 点屏幕正中、把 inn.py 的「休息一夜→是」流程整个挡死（02:2x 实测）。
MODAL_ADVANCE_HINTS = ("screen_doom_reset",)


def plain_modal(u):
    """认得"只有动画、点一下就走"的全屏模态 → 返回它的根元素，否则 None。

    现场（2026-11-20 02:2x）：怨念满 → `screen_doom_reset(Clone)` 挂上，
    `coach` 报 `UiScreenBhv.Delegate_RestrictDriving=False`、speed=0，
    runner 每个 tick 按 w 什么都不动（和岔路口长得一模一样，但这里没得选路，
    点一下屏幕正中就过去了）。
    """
    els = (u or {}).get("clickables") or []
    for c in els:
        p = c.get("path") or ""
        if MODAL_ROOT not in p:
            continue
        key = p.split(MODAL_ROOT, 1)[1]
        if "/" in key:
            continue                                  # 这是模态的子元素，不是根
        if not any(h in key for h in MODAL_ADVANCE_HINTS):
            continue                                  # 不在白名单：不擅自点
        prefix = MODAL_ROOT + key + "/"
        if not any((x.get("path") or "").startswith(prefix) for x in els):
            return c
    return None


# ---------------------------------------------------------------- 残队避险（优先不打仗的节点）
WEAK_PARTY_ALIVE = 3      # 活着的英雄少于这个数 → 尽量绕开战斗节点
WEAK_HP_PCT = 45.0        # 队伍平均血量低于这个百分比 → 也优先去客栈/商店（别硬推到团灭）
# 2026-11-20 实抓（probe_nodes.py → logs\node_types.txt，12 种）：
#   BossSelect / HeroSelect / Bridge / Inn / Gate / StoryAssist / CreatureDen /
#   Dungeon / Store / StoryCosmic / GameResults / AltarOfHope
# 真·非战斗：Inn（客栈）、Store（商店）、StoryAssist（剧情辅助）
# 真·战斗：Dungeon / CreatureDen（另加图里会出现但探针没抓到的 Fight/Elite/Boss/Resist 类）
# 语义不明、按"可能打"处理（不选）：Bridge / Gate / StoryCosmic / GameResults / AltarOfHope
# 历史词（hospital/camp/heal/refuge/sanctuary/shop）这版地图不出现，留着不亏：
# 万一别的地区/版本有，行为反而更保守正确。
SAFE_NODE_HINTS = ("inn", "store", "storyassist",
                   "hospital", "camp", "heal", "refuge", "sanctuary", "shop")
FIGHT_NODE_HINTS = ("fight", "battle", "combat", "elite", "boss", "resist", "gaunt",
                    "arena", "lair", "ambush", "dungeon", "creatureden", "creature")
_wipe_pending = [False]   # 见过 game_over 面板但还没确认（面板只亮 1 tick，见 do_tick）
_run_started = [False]    # 本进程已经走过一次"选忏悔"= 远征真正开跑；用来挡开局残留的旧面板
_T0 = [0.0]               # 进程启动时刻（挡"开局残留面板"改成按时间挡，见 early_start）
EARLY_START_SECS = 90.0   # 启动后这么久之内看到的 game_over 面板当"残留"，不记团灭

# —— 减员检测（D3）：Boss 前掉一个人，本局基本就没了（作者口径 90%）。
# 掉人不等于团灭，但继续跑完剩下的区域是负收益，所以这里单独判、单独退。
MEMBER_LOST_TICKS = 3     # 连续这么多 tick 都看到人少了才算数（挡过场动画/状态抖动）
PARTY_FULL = 4            # DD2 一局就是 4 个人，**只减不增**（所以基线不能靠"第一次看到几个"）


def run_baseline(na, seen=0):
    """本局活人基线。na=本 tick 数到的人数，seen=之前记下的基线。

    2026-11-20 21:20 实测的坑：`_run_party` 原来取"本进程见过的最多人数"。
    watchdog 因 240s 冻结杀掉 runner 重启时，Dismas 刚死、残队已经是 3 人 →
    新进程第一次数人就是 3 → 基线=3 → 4→3 这次减员永远判不出来 →
    21:22、21:27 两场残队硬打，最后团灭（`MEMBER LOST` 一次没打印）。
    口径是"减员即弃局"（减员之后 Boss 根本没法打），所以基线直接钉 PARTY_FULL：
    半路接管到 3 人的残局，第一眼就判减员 → 立刻弃局重开，不再白打。
    """
    if not na or na > PARTY_FULL:      # 0 人=过场/结算，>4=祭坛花名册，都不算
        return seen
    return PARTY_FULL
_run_party = [0]          # 本局最多同时见过几个活人（掉人不回升）
_lost_streak = [0]
_lost_in_fight = [False]   # 战斗里见过掉人（等打完这场再弃局）


def early_start(now=None, secs=EARLY_START_SECS):
    """还在刚启动的那 90 秒里吗？

    2026-11-20 00:41 实测又一次漏报团灭：那一轮 runner 是从**半路接管**的
    （游戏里已经在远征第 2 行、gold=40），它从没点过"选忏悔"→ `_run_started` 永远是 False
    → 见到 game_over 面板也不记 pending → 40 分钟后同一进程里队伍全灭被当成"新远征"
    继续跑（gold 40→0）。所以"开局残留"只能按**时间**挡，不能按"点过忏悔没有"挡。
    """
    t0 = _T0[0] or 0.0
    if not t0:
        return False
    return ((now or time.time()) - t0) < secs


def stuck_in_auto_mode(driving_mode, idle_secs, panel_open, limit=60.0):
    """车卡在自动挡了吗（真因，2026-11-20 01:1x 空转 40 分钟）。

    现场：`drivingMode=GO_TO_TARGET`、`speed=12.1` 一直不变、row 卡着不动。
    GO_TO_TARGET 下按 w 是被忽略的，而 speed 又是残留值 → 旧代码只在 `speed<0.5` 时
    才去 `coachMode PLAYER_INPUT`，于是永远修不到；一路按 w 空转到天荒地老。
    实测把模式强行改回 PLAYER_INPUT 后 speed 立刻 12.1 → 14.3，车就动了。
    """
    if panel_open:
        return False                    # 节点面板开着时游戏本来就锁输入
    if not driving_mode or driving_mode == "PLAYER_INPUT":
        return False
    return idle_secs > limit


def auto_mode_wedged(mode_now, elapsed, moved,
                     max_secs=AUTO_MODE_MAX_SECS, min_move=AUTO_MODE_MIN_MOVE):
    """自动挡 GO_TO_TARGET 在空转吗（2026-11-25 卡墙真因，判据带位移）。

    为什么不能只看时间：岔路口的 SLOW_DOWN / CHOICE_RESULT 也用 GO_TO_TARGET
    （TriggerIntersectionBhv.cs:149-162），那是**正常**的自动挡，几秒内一定在挪位置。
    真正坏掉的是"追一个到不了的点"：轮速维持 12.1、位置被钳住不动。

    为什么不能只看"动没动"：贴着墙扭方向盘时单拍位移能有一点点，得按窗口累计看。
    """
    if mode_now != "GO_TO_TARGET" or not elapsed:
        return False
    return elapsed > max_secs and moved < min_move


def run_reset_seen(u):
    """团灭后面板一闪而过、runner 又回到"选忏悔"屏 —— 这就是"上一局已经结束"的铁证。"""
    return any("screen_boss_select_panel" in (c.get("path") or "")
               for c in (u or {}).get("clickables") or [])


def party_alive(s):
    return sum(1 for a in (s or {}).get("actors") or []
               if a.get("team") == 0 and a.get("alive"))


def party_count(mode, scene, s):
    """这一 tick 该数几个人；返回 None = 不数。

    祭坛/选人时 actors 是"花名册"，会比 4 大，一律不数。
    2026-11-20 实测：这里没返回值时下游直接 UnboundLocalError，整轮 runner 秒退。
    """
    # 2026-11-22 03:44 实测：角色起源故事战是**单人上场**（Boudica 31/31 vs 4 个 31/31），
    # actors 里只有 1 个我方 → 旧的 4 人基线把它判成"减员 4→1"，runner 直接退 7。
    # 故事战是脚本战，不参与本局人数统计。
    if "hero_story" in (scene or ""):
        return None
    if mode in ("map", "combat") and scene not in ("altar_of_hope", "hero_select"):
        return party_alive(s)
    return None


def party_hp_pct(s):
    """队伍平均血量百分比（只算活着的我方）。没有 hpMax 的数据按 100% 算。"""
    vals = []
    for a in (s or {}).get("actors") or []:
        if a.get("team") != 0 or not a.get("alive"):
            continue
        mx = a.get("hpMax") or 0
        hp = a.get("hp") or 0
        vals.append(100.0 * hp / mx if mx else 100.0)
    return round(sum(vals) / len(vals), 1) if vals else 100.0


_coach_cache = [0.0, None]   # 限频读 coach：顶墙时 /state 的 speed 仍报 12.1
_coach_pos = [None, None, 0]  # [上次 x, 上次 z, 连续"没位移"的采样数] —— 见 _coach_stuck
_last_truth = [None]         # 最近一次 {"type":"battle"} 的返回（结束判定的真值）
_barr_since = [0.0]          # 顶墙开始时间：>6s 就 coachUnblock 一次


def _battle_truth():
    """游戏自己说的"这一场结束没有"：BattleConditions.CheckForEnd / GetIsComplete。

    依据 Battle.cs:161（Update 里 CheckForEnd 才 BattleEnd）、BattleConditions.cs:132-154。
    返回 None 表示拿不到（不在战斗 / 老 DLL），调用方要能退化。
    """
    try:
        d = C.act({"type": "battle"}) or {}
        if d.get("ok"):
            _last_truth[0] = d
            return d
    except Exception:
        pass
    return None


def _coach_stuck():
    """返回 {"barricade": bool, "forced": bool}，2 秒缓存。

    2026-11-21：马车顶在墙上空转时，/state 的 speed 一直是 12.1（残留值），
    所以 `moving=speed>0.05` 永远成立、卡画面检测被无限清零。
    coach 里的 m_isStoppingAtBarricade / m_onForcedStop 才是真的。
    """
    now = time.time()
    if _coach_cache[1] is not None and now - _coach_cache[0] < 2.0:
        return _coach_cache[1]
    out = {"barricade": False, "forced": False, "nogress": False}
    try:
        r = C.act({"type": "coach"}) or {}
        st = r.get("st") or {}
        out["barricade"] = str(st.get("stoppingAtBarricade")).lower() == "true"
        out["forced"] = bool(st.get("onForcedStop"))
        # 2026-11-21 22:5x：mod 现在会给 transform.position。speed>2 但 3 拍（约 6 秒）
        # 位移 < 0.6 米 = 顶着墙空转（speed 是轮子转速，不是位移）。
        px, pz = r.get("posX"), r.get("posZ")
        if px is not None and pz is not None:
            px, pz = float(px), float(pz)
            if _coach_pos[0] is not None:
                moved = ((px - _coach_pos[0]) ** 2 + (pz - _coach_pos[1]) ** 2) ** 0.5
                seen_spd = float(r.get("speed") or 0)
                if seen_spd > 2.0 and moved < 0.6:
                    _coach_pos[2] += 1
                else:
                    _coach_pos[2] = 0
            _coach_pos[0], _coach_pos[1] = px, pz
            out["nogress"] = _coach_pos[2] >= 3
    except Exception:
        pass
    _coach_cache[0], _coach_cache[1] = now, out
    return out


def member_lost(base, alive_n, streak, need=MEMBER_LOST_TICKS):
    """减员检测：本局活人数从 base 掉到 alive_n，连续 need 个 tick 才算数。

    返回 (是否确认, 新 streak)。几条边界都是实测踩过的：
      - alive_n == 0 是团灭，走 game_over 那条路，这里不判（否则会和 wipe 抢）；
      - base < 2：还没建立基线（刚开进程、还在祭坛/选人），不判；
      - 人数又回到 base：streak 清零，挡住过场动画造成的假掉人。
    """
    if base >= 2 and 0 < alive_n < base:
        streak += 1
        return streak >= need, streak
    return False, 0


def roster_evidence(pi, base=None):
    """减员那一刻的名册原始状态（2026-11-23 §25.5 方案 A：把真 status 钉进日志）。

    为什么读 partyInfo：它遍历 RosterManager.m_Entries 全表、逐条报 m_RosterStatus
    （mod\\StateBuilder.cs 的 partyInfo 分支；枚举见 RosterStatusType.cs:9-25），
    所以「DEAD 条目还在」和「条目被 Remove」在这里长得不一样——正是要钉住的事实。
    2026-11-23 起 partyInfo 还带 classId/actorFound：DEAD 的 isActorRequired=false
    （RosterStatusType.cs:15），actor 不在库时只有 classId 能认人。
    读不到就如实写「读不到」，不拿推测顶（大前提：不许编）。
    """
    if not isinstance(pi, dict) or not pi.get("ok"):
        err = str(pi.get("error") or "") if isinstance(pi, dict) else ""
        return ("名册读不到" + ("（%s）" % err if err else ""))[:160]
    party = pi.get("party") or []
    hist, named = {}, []
    for p in party:
        st = str(p.get("status") or "?").upper()
        hist[st] = hist.get(st, 0) + 1
        if st in ("RESERVE", "HIRE", "HIRE_REPLACED", "LOAD"):
            continue          # 板凳/待命不逐条点名，省掉 alert 的 240 字上限
        who = p.get("name") or p.get("classId") or p.get("dataId") or p.get("guid") or "?"
        if p.get("actorFound") is False:
            extra = "（actor不在库）"
        else:
            hp = p.get("hp")
            extra = "" if hp is None else " hp%s" % hp
        named.append("%s=%s%s" % (who, st, extra))
    tag = "/".join("%s%d" % (k, v) for k, v in sorted(hist.items()))
    s = "名册%d条(%s)" % (len(party), tag)
    if named:
        s += "：" + "，".join(named[:6]) + ("+%d" % (len(named) - 6) if len(named) > 6 else "")
    return s[:180]


RETREAT_SAFE_SECS = 600.0     # 撤退后这段时间只走安全节点（客栈/商店/剧情辅助）
_retreated_at = [0.0]
_block_seen = [None, 0]       # (canDrive 里 False 的签名, 连续几拍) —— 见 do_tick 的 unblock 分支


def _recently_retreated(now=None):
    """最近一次撤退是不是还在"避险窗口"里。"""
    return (time.time() if now is None else now) - _retreated_at[0] < RETREAT_SAFE_SECS


def note_fight_result():
    """记下 combat3 的收尾结果：只要撤过，就进避险窗口。

    放成独立函数是为了能被离线测试直接调用（不依赖真打一场）。
    """
    last = getattr(C, "LAST_FIGHT", None) or {}
    if last.get("retreat"):
        _retreated_at[0] = time.time()
        return True
    return False


def party_dead_door(s):
    """我方站在死门上的人数（活着但 0 血）。"""
    return sum(1 for a in (s or {}).get("actors") or []
               if a.get("team") == 0 and a.get("alive") and (a.get("hp") or 0) <= 0)


def need_safe_node(s):
    """该不该绕开战斗节点：人少了 / **有人站死门** / 血低了。

    2026-11-20 再收紧一次：作者口径是"Boss 前减员 90% 过不了"，
    所以只要有人站死门就按残队处理，不等平均血量掉到 45%——那时候往往已经晚了。

    2026-11-20 实测（00:29~01:24 连续 5 场）：Barristan 46→38→30→26→24、Audrey 29→21…
    一路硬推，中途不进客栈，最后 00:41 那场直接打到只剩 2 人（然后团灭）。
    旧判据只看"活着的人数 <3"，等触发时已经晚了；血量掉到 45% 就该找客栈/商店。

    2026-11-21 07:31 又加一条：**刚撤退过**也按残队算。那局撤退出来两人站死门，
    但因为撤退本身会把血补一点，判据立刻失效 → 接着连开两场 → 团灭。
    撤退说明"这队现在打不过"，跟血量数字无关，所以用时间窗锁住（RETREAT_SAFE_SECS）。
    """
    return (party_alive(s) < WEAK_PARTY_ALIVE
            or party_dead_door(s) >= 1
            or _recently_retreated()
            or party_hp_pct(s) < WEAK_HP_PCT)


def pick_safe_node(s):
    """当前行里挑一个"不打仗"的节点 → (row, index, type) 或 None。

    只按节点类型名判断（inn/hospital/camp 之类），没把握的类型当战斗处理（宁可不选）。

    2026-11-25：**不再拿它的结果去 goNode 导航**（那是卡墙真因，见 do_tick map 分支里的
    删除说明）。留着是因为岔路口避险要用同一套类型口径、并且 test_runner_guards.py:60-87
    用它钉住"哪些类型算安全"；岔路避险走 pick_road(prefer_safe=...)。
    """
    nodes = (s or {}).get("nodes") or []
    for n in nodes:
        t = (n.get("type") or "").lower()
        if any(k in t for k in SAFE_NODE_HINTS) and not any(k in t for k in FIGHT_NODE_HINTS):
            return (n.get("row"), n.get("index"), n.get("type"))
    return None


def pick_road(options, prefer_safe=False):
    """岔路口的可选路线里挑一条。

    options 来自 mod 的 `{"type":"intersect"}`：每条是
    {i: 路口选项序号, node: 该行节点序号, type: 节点类型, revealed, triggered}。
    prefer_safe=True（残队/血低）时优先非战斗节点（客栈/商店/剧情辅助），
    否则优先已揭示的那条，再不行取第一条 —— 总比停在路口不动强。
    """
    opts = [o for o in (options or []) if isinstance(o, dict)]
    if not opts:
        return None
    if prefer_safe:
        for o in opts:
            t = (o.get("type") or "").lower()
            if any(k in t for k in SAFE_NODE_HINTS) and not any(k in t for k in FIGHT_NODE_HINTS):
                return o
    for o in opts:
        if o.get("revealed"):
            return o
    return opts[0]


def do_tick(s, dry_run=False, confession=""):
    """执行一个 tick，返回 (action, 选完之后等几秒，额外写进 status 的字段)。"""
    mode = s.get("mode")
    scene = s.get("scene") or ""

    # ① 最先做：任何 mode 都可能被全屏面板挡着（暂停菜单 / 崩溃报告框）。
    # 暂停菜单尤其阴：它在 map 里挂着时 `coach` 仍报 speed=12.1，runner 会一路按 w
    # 却什么都不动（2026-11-20 01:1x 卡了 40 分钟才被发现）。所以放在所有分支之前。
    try:
        u_top = C.get("/ui")
    except Exception:
        u_top = {}
    ov = blocking_overlay(u_top)
    if ov is not None:
        _ov_tries[0] += 1
        if _ov_tries[0] >= 3:
            # 连点 3 次「返回游戏」还挡着 → 换手势：Esc 在 DD2 里就是暂停菜单的开关。
            # 2026-11-23 09:1x 核实：simAction PauseMenu 调的是游戏注册的暂停处理器（不碰设备）。
            if not dry_run:
                C.act({"type": "simAction", "name": "PauseMenu"})
            _ov_tries[0] = 0
            return "resume_pause_esc", 1.2, {"mode": mode}
        click_el(ov, dry_run)
        return "resume_pause", 1.5, {"mode": mode}
    _ov_tries[0] = 0
    rep_top = dialog_decline_report(u_top)
    if rep_top is not None:
        if not dry_run:
            C.act({"type": "uiClick", "index": rep_top["i"]})
        return "dismiss_report", 1.5, {}

    # 只有动画、没有任何按钮的全屏模态（2026-11-20 02:2x 实测：`screen_doom_reset`
    # —— 怨念满时"山峰震颤"，`UiScreenBhv.Delegate_RestrictDriving=False` 把车锁死，
    # runner 一路按 w 一动不动，row 停在那儿）。这类屏点一下屏幕正中（或空格）就过，
    # 不会有任何选择后果，所以可以放心点；带按钮的模态一律不碰（交给上面的安全名单）。
    modal = plain_modal(u_top) if mode == "map" else None
    if modal is None:
        _modal_try[0] = 0
    else:
        if not dry_run:
            # 2026-11-22 实测：这类屏（screen_doom_reset 之类）**不吃空格** —— 只发空格会连卡
            # 8 个 tick 判 stuck。所以空格只试第一次，之后退回"点屏幕正中"（这类屏没有按钮，
            # 点正中不会触发任何选择后果）。两条都是 API。
            _modal_try[0] += 1
            if _modal_try[0] <= 1:
                # 2026-11-23 09:1x 核实：simAction Submit = 调游戏注册的 Submit 回调（不碰设备）。
                C.act({"type": "simAction", "name": "Submit"})
            else:
                C.act({"type": "uiPoint", "x": 960, "y": 540, "holdMs": 100})
        return "modal_advance", 2.0, {"modal": (modal.get("path") or "")[-40:],
                                      "via": "space" if _modal_try[0] <= 1 else "center"}

    # 2026-11-23 13:0x 实测补：巢穴「前往下一场战斗？」确认框（DungeonConfirmationDialog）不吃 uiHold。
    # 12:57 那轮在 combat_arena_city_dungeon_exterior 连卡 15 拍报 stuck：do_hold_button 里
    # uiHold(前进) 试了 3 次不推进（scrim 挡射线），而**同一屏 uiClick(index) 立刻生效**
    # （13:06 手工点「逃离！」返回 ok=true、hit=Text (TMP)）。所以这一屏单独走点击，
    # 判据是「逃离！」和「前进」两个按钮同时在 —— 普通「前进」长按键只有后者。
    dlg_leave = find_text_button(u_top, "逃离")
    dlg_forward = find_text_button(u_top, "前进")
    if dlg_leave and dlg_forward:
        lk = ((s.get("scene") or ""), s.get("mapRow"))
        if _lair_key[0] != lk:
            _lair_key[0] = lk
            _lair_depth[0] = 0
        # 攻略 §8.6/§4：打完 LAIR_ESCAPE_AFTER 层就逃离（保留已拿的奖励，不碰头目层）
        leave = _lair_depth[0] >= LAIR_ESCAPE_AFTER
        btn = dlg_leave if leave else dlg_forward
        if dry_run:
            return ("lair_leave" if leave else "lair_forward"), 2, {"note": "dry-run 不点"}
        try:
            # 实测（13:06 点「逃离！」/ 13:18 复验）：这一屏 uiClick(index) 返回 ok=true 但**不推进**
            # （hit=Text (TMP)，等于点在了字上），uiHold(text) 按 3 次也没用；只有 uiPoint 按住
            # 1200ms 才真的走（`via=down(hold)`，点完 1.5 秒确认框就消失）。和选忏悔那屏同一套手势。
            C.act({"type": "uiPoint", "x": int(btn.get("x")), "y": int(btn.get("y")),
                   "holdMs": 1200})
        except Exception as e:
            print("[警告] 巢穴确认框点击失败：%s" % e, flush=True)
        _lair_depth[0] = 0 if leave else _lair_depth[0] + 1
        return ("lair_leave" if leave else "lair_forward"), 2.0, {
            "via": "uiPoint", "depth": _lair_depth[0], "btn": (btn.get("text") or "")[:8]}

    # 「前进」这类长按按钮可能出现在任何 mode 上（战斗结算后、地图里、旅馆前），先查它
    try:
        d = do_hold_button(s, u_top, dry_run=dry_run)
    except Exception as e:
        d = None
        print("[警告] 查长按按钮失败：%s" % e, flush=True)
    if d:
        return d

    if mode == "combat":
        if dry_run:
            return "combat", 2, {}
        # 2026-11-21 22:5x：不再靠"actors 空"猜结束。游戏自己有两个真值：
        #   BattleConditions.CheckForEnd()（阶段结束） / GetIsComplete()（算不算赢）
        # 见 Battle.cs:161、BattleConditions.cs:132-154。actors 空但 checkForEnd=False
        # = 演出/召唤间隙，不是结束（这就是 133 条 unknown 的来源）。
        bt = _battle_truth()
        if not s.get("actors"):
            if bt and not bt.get("checkForEnd"):
                return "combat_settle", 1.2, {"truth": "not_end"}
            return "combat_settle", 1.2, {"truth": "end" if bt else "no_api"}
        # "僵尸战斗"检测：双方都 0 血、回合数不动时，C.main 会一直空转，
        # 而 action=="combat" 在卡死判定里算"有进展"，于是永远不报 STUCK。
        # 这里用 (round, turn, 当前出手人, 全体血量) 当指纹，连续 2 次没变就交出去。
        sig = combat_fingerprint(s)
        if sig == _combat_sig[0]:
            _combat_sig[1] += 1
        else:
            _combat_sig[0], _combat_sig[1] = sig, 0
        s2 = C.main(max_steps=COMBAT_MAX_STEPS)
        note_fight_result()          # 撤过就进避险窗口（need_safe_node 会用）
        # combat3 判出死锁会把这个标记挂回 state（120 秒零变化）：立刻交出去，
        # 否则要再等 2~3 个 tick（每个 tick 可能又耗 120 秒）才报 stuck。
        if isinstance(s2, dict) and s2.get("_frozen"):
            return "combat_stuck", 1, {"frozen_by": "combat3", "secs": s2["_frozen"].get("secs")}
        if _combat_sig[1] >= 2:
            return "combat_stuck", 1, {"same": _combat_sig[1], "fp": str(sig)}
        return "combat", 0, {}

    if "combat_results" in scene:
        # 结算画面是硬证据：write_fight 落行时常还看不到它（actors 已清空 → unknown），
        # 这里把紧挨着的那行补判成 win（只改最后一行、5 分钟内的 unknown）。
        try:
            C.mark_last_unknown_win()
        except Exception:
            pass
        # 2026-11-21 20:4x：先走接口（LootUiControllerBhv.ButtonTakeAll），失败再点按钮。
        if not dry_run:
            try:
                r = C.act({"type": "lootTakeAll"})
                if isinstance(r, dict) and r.get("ok"):
                    return "loot", 2, {"via": "lootTakeAll"}
            except Exception:
                pass
        u = C.get("/ui")
        c = click_by(u, "拿取", "TakeAll")
        if c:
            if not dry_run:
                C.act({"type": "uiClick", "index": c["i"]})
            return "loot", 2, {}
        # 20:5x：空奖励的「继续」先走接口（LootUiControllerBhv.ButtonClose, :266），
        # 失败（旧 DLL / 包里还有东西）再退回点按钮。
        if not dry_run:
            try:
                r = C.act({"type": "lootClose"})
                if isinstance(r, dict) and r.get("ok"):
                    return "results_continue", 2, {"via": "lootClose"}
            except Exception:
                pass
        # 没有"拿取全部"（空奖励）时，结算面板只有一个「继续」，不点会一直卡住
        cont = click_by(u, "继续", "ContinueButton")
        if cont:
            if not dry_run:
                C.act({"type": "uiClick", "index": cont["i"]})
            return "results_continue", 2, {}
        return "combat_results", 2, {}

    if mode == "map":
        # 选人界面（每局开局）。牌是拖拽进队伍的，uiClick 拖不动；用 mod 的 party 动作
        # 直接把 4 张牌塞进队伍并确认，确认后游戏自动进地图。
        if (s.get("scene") or "") == "hero_select":
            if not dry_run:
                C.act({"type": "party", "dataIds": PARTY, "confirm": True})
                # 2026-11-23 作者口径：阵容钉死、道途/技能不自动改 → 这里只选人 + confirm 启程。
                if AUTO_LOADOUT:
                    # 老口径：道途是在**选队伍这一步**定的，不是后边到客栈才换。
                    # 所以选完人立刻设一次（此时 ActorInstance 可能还没建好，失败就靠 pending 在进图后补）。
                    loadout.mark_paths_pending()
                    try:
                        ch = loadout.apply_paths(force=True)
                        if ch:
                            print("[道途] 选人时定：%s" % ch, flush=True)
                    except Exception as e:
                        print("[道途][失败] %s: %s" % (type(e).__name__, e), flush=True)
                    # A2：点名技能表（没配的 dataId 不动）。这里的 setSkills 可能因"只能旅馆换"而失败，
                    # 失败会在 maybe_equip 的停车站点再试（同一份配置，成功后不再刷）。
                    try:
                        sk = loadout.apply_skills(force=True)
                        if sk:
                            print("[技能] 选人时配：%s" % sk, flush=True)
                    except Exception as e:
                        print("[技能][失败] %s: %s" % (type(e).__name__, e), flush=True)
            return "hero_select", 4, {}

        # 选人的那次如果 ActorInstance 还没建好（setPath 失败），进图后补一次；
        # 节流在 loadout.apply_paths 里（15 秒一次），成功就自动清 pending。
        if AUTO_LOADOUT and not dry_run and loadout._path_pending[0]:
            try:
                ch = loadout.apply_paths()
                if ch:
                    print("[道途] 进图补设：%s" % ch, flush=True)
            except Exception as e:
                print("[道途][失败] %s: %s" % (type(e).__name__, e), flush=True)

        # 希望祭坛（每局开局准备页）：这里没有车，inputEnabled=False、mapState 还会是
        # OUT_OF_SCOPE，所以绝不能落进下面 coachReset / coachMode 那两条"修车"兜底，
        # 否则每 tick 重试一次、画面零变化，12 tick 后自己判死退出。
        #
        # 「启程」不是普通按钮，是"按住不放才生效"：AltarOfHopeUiBhv 里是
        # m_accEmbarkTimeHeld / SetEmbarkHeld / m_embarkHoldFills 这一套。
        # 单击（uiClick，按下即松开）只会走 OnDisabledEmbarkClick；uiHold 也不行，
        # 因为按住判定依赖真实悬停状态，进程内虚拟指针拿不到。
        # 实测唯一有效：真鼠标按住 —— realclick.ps1（会把游戏窗口切前台，约 4 秒）。
        if "altar" in (scene or "").lower():
            if dry_run:
                return "altar", 2, {"note": "dry-run 不点启程"}
            # 首选：mod 新增的后台动作 altarEmbark —— 直接调 AltarOfHopeUiBhv 的方法
            # （SetEmbarkHeld(true) → 等 3 秒 → 还在就 OnEmbark()），不碰鼠标、不抢焦点。
            via = "mod"
            ok = False
            try:
                r = C.act({"type": "altarEmbark"})
                ok = isinstance(r, dict) and r.get("ok") is True
            except Exception:
                ok = False
            if not ok:
                # 兜底（仍然只走 API）：按路径 → 文本 → 序号 uiHold 那个「启程」按钮。
                # 2026-11-22：原来这里退"真鼠标"，已删 —— 最差也只是写告警停手。
                via = "uiHold"
                u = C.get("/ui")
                btn = next((x for x in u.get("clickables", [])
                            if "MouseEmbarkButton" in (x.get("path") or "")), None)
                if not btn:
                    return "altar_idle", 2, {}
                for tag, t in (("path", {"type": "uiHold", "text": btn.get("path") or "",
                                         "seconds": HOLD_SECONDS}),
                               ("text", {"type": "uiHold", "text": (btn.get("text") or "启程").strip(),
                                         "seconds": HOLD_SECONDS}),
                               ("index", {"type": "uiHold", "index": btn.get("i"),
                                          "seconds": HOLD_SECONDS})):
                    try:
                        r = C.act(t)
                    except Exception:
                        continue
                    if isinstance(r, dict) and r.get("ok"):
                        ok, via = True, "uiHold:" + tag
                        break
                if not ok:      # 三条接口都推不动 → 攒几 tick 写告警停手（不抢鼠标/不切前台）
                    _need_focus[0] += 1
                    if _need_focus[0] >= 8:
                        reason = "祭坛『启程』推不动（altarEmbark + uiHold 三条全无效）"
                        print("NEED-FOCUS:", reason, flush=True)
                        write_alert("need-focus", reason, s, path=_ALERT_PATH[0])
                        return "need_focus_stop", 0, {}
                    return "altar_need_focus", 2.0, {"try": _need_focus[0]}
            return "altar_embark", 4, {"via": via}

        # 马车跑到地图外：用游戏自带的"回到驿站马车"逻辑拉回来。
        # 必须限定 gameMode == DRIVING：祭坛/旅馆页也会报 OUT_OF_SCOPE，
        # 那两处输入是锁死的，coachReset 必然失败（日志里那句
        # "Failed to reset stage coach because player input is not enabled"）。
        if ((s.get("mapState") or "") == "OUT_OF_SCOPE"
                and (s.get("gameMode") or "") == "DRIVING"):
            if not dry_run:
                C.act({"type": "coachReset"})
            return "reset_coach", 2, {}
        u = C.get("/ui")

        # 团灭确认（2026-11-19 22:47 实测）：game_over 面板只亮 1 个 tick（我们自己的
        # uiInvoke 立刻把它点掉），"连续 2 个 tick" 的判据永远等不到 —— 上一局团灭后
        # runner 直接当成新远征继续跑（gold 78→40、row 归 0），团灭告警一次都没发。
        # 铁证：团灭后游戏清空远征、回到第 2 行重新选忏悔。第二次看到 boss_select 面板
        # 就说明上一局已经结束；首局选忏悔时 pending=False，不会误报。
        if _wipe_pending[0] and run_reset_seen(u):
            _wipe_pending[0] = False
            return "game_over", 1.0, {"confirmed": "new-confession"}

        # 已经见过 game_over 面板（pending）→ 这一 tick **什么都不点**，只把动作交回去，
        # 让 main 的 over_ticks 攒到 2 → 写 wipe 告警、退出码 4。
        # 03:0x 实测：光靠"第二次看到忏悔屏"确认太晚 —— 中间那几个 tick runner 已经
        # 替玩家点了「启程」，把新一局开起来了（gold 归 40、row 0），团灭告警是响了，
        # 但"团灭只停不救"变成了"团灭先偷偷开新局"。
        if _wipe_pending[0]:
            return "game_over", 1.5, {"pending": True}

        # 全灭/迷失黑暗：游戏停在"迷失黑暗"面板上等点击。
        # 这时候地图还在空转（spd 有值、row 不变），按 W 是没反应的，看起来就像卡死。
        # 只认 screen_game_over_panel —— 战后"结算"面板也有个"继续"，
        # 按文本匹配会把正常结算当成团灭，直接误判成 RUN OVER。
        # 另外这里必须用 uiInvoke 直接调 onClick：uiClick 是射线点击，
        # 会被面板遮罩吃掉（返回 hit=glow），点了没反应。
        over = any("screen_game_over_panel" in (x.get("path") or "") for x in u.get("clickables", []))
        if over:
            if not early_start():    # 只有"刚启动那 90 秒"的旧面板才不算团灭
                _wipe_pending[0] = True
            cont = next((x for x in u.get("clickables", [])
                         if "ContinueButton" in (x.get("path") or "")), None)
            if not dry_run and cont:
                C.act({"type": "uiInvoke", "index": cont["i"]})
            return "game_over", 1.5, {}

        # 奖励面板（「拿取全部」）。必须放在剧情之前：
        # 实测那种「捡东西 + 剧情选择」同屏的事件，奖励面板开着时剧情长按是不生效的，
        # 先拿完东西，剧情选项自己就收掉了（否则会一直卡在 story_hold 直到判死）。
        c = click_by(u, "拿取", "TakeAll")
        if c:
            if not dry_run:
                click_el(c)          # 坐标优先：index 会因 mod 重新 Collect 而指错（见 click_el 注释）
            return "reward", 2, {}

        # 另一种结算面板：只有「继续」（Loot 层）。不点掉同样会挡住剧情长按。
        cont = next((x for x in u.get("clickables", [])
                     if (x.get("text") or "").strip() == "继续"
                     and ("Loot" in (x.get("path") or "") or "Reward" in (x.get("path") or ""))), None)
        if cont is None and any("奖励" in (t or "") for t in u.get("texts", [])):
            cont = next((x for x in u.get("clickables", [])
                         if (x.get("text") or "").strip() == "继续"), None)
        if cont is not None:
            if not dry_run:
                click_el(cont)       # 2026-11-22：uiClick index 在 Loot 层会点空，卡 reward_continue 8 tick
            return "reward_continue", 2, {}

        # 剧情事件：提示「长按英雄以做出选择」。老办法（InputSimulator 的 Interact 长按）
        # 这个版本已经推不动了，会一直停在 story_hold 直到判死。
        # 现在改成指针注入：在英雄立绘上按住 2.5 秒；一次试一个横坐标，逐 tick 轮换，
        # 因为不同剧情里"可选的英雄"不一样（有的只有 2 号位能选）。
        prompt = any("长按英雄" in (t or "") for t in u.get("texts", []))
        if prompt:
            # 2026-11-20 07:01 实测：这种"捡东西+剧情选择"同屏的界面，奖励面板盖在上面，
            # 长按英雄永远不生效 —— runner 连卡 20 tick 判死（alert kind=stuck）。
            # 保险：同屏只要有「拿取全部/继续」就先点掉，再谈长按。
            tk = next((x for x in u.get("clickables", [])
                       if (x.get("text") or "").strip() in ("拿取全部", "拿取", "继续")), None)
            if tk is not None:
                if not dry_run:
                    C.act({"type": "uiClick", "index": tk["i"]})
                return "story_take", 2, {}
            # 2026-11-19 22:05 实测（「迷途军！」剧情）：uiHold 返回 ok=True，但 target 是
            # MouseDrivingArea（驾车遮罩）—— 长按被遮罩吃掉，画面连续 20 tick 不动直到判死。
            # pointerClick 注入同样推不动。唯一有效的是**真实鼠标**在英雄立绘上按住 2.6s。
            # 所以：前 4 次仍走 uiHold（别的剧情它有效，且不用抢焦点），之后改真鼠标网格。
            if not dry_run:
                # 2026-11-21 20:5x：先走接口 —— StoryChoiceButtonBhv.OnChoiceSelected()（:345）
                # 内部只发 EventSelectStoryChoice，不用长按、不用指针。
                # 2026-11-21 21:5x：但「无人知晓……」这类屏接口回 ok 却推不动（事件被丢），
                # 逐 tick 重试会让下面的 uiHold 永远轮不到 → 同一块屏只试一次（按按钮路径指纹去重）。
                btns = [x for x in u.get("clickables", [])
                        if "StoryChoiceButton" in (x.get("path") or "")]
                sig = (len(u.get("clickables") or []),
                       tuple(b.get("path") or "" for b in btns))
                if btns and sig != _story_api_sig[0]:
                    _story_api_sig[0] = sig
                    try:
                        rr = C.act({"type": "storyChoice"})
                        if isinstance(rr, dict) and rr.get("ok"):
                            return "story_choice", 2, {"via": "storyChoice", "n": rr.get("count")}
                    except Exception:
                        pass
                # 2026-11-20 07:2x 实测：长按的是**英雄立绘**（坐标在选项气泡下方约 240px），
                # 而且只有真鼠标能推动（uiHold/pointerClick 都被遮罩吃掉）。
                # 立绘位置直接由选项气泡的 x/y 推出来，比原来那串猜的 STORY_POINTS 准。
                pts = [(b["x"], 1080 - (b["y"] + 240)) for b in btns
                       if b.get("x") and b.get("y")]
                clicked = None
                # 2026-11-20 实测：mod 改造后 uiHold 能绕开驾车遮罩，剧情长按**纯后台就能推**
                # （实测按住 StoryChoiceButton 2.5s，画面直接过场），不需要真鼠标——
                # 真鼠标必须抢前台，作者不要。所以先用 uiHold 轮着按住三个选项，
                # 连试 6 次还没过，才退真鼠标（非前台会返回 None → 写 need-focus 停手）。
                if btns and _story_try[0] < 6:
                    b = btns[_story_try[0] % len(btns)]
                    C.act({"type": "uiHold", "index": b["i"], "seconds": 2.4})
                    clicked = ("uiHold", b["i"])
                elif btns and _story_try[0] < 10:
                    # 2026-11-22：按序号按住没成 → 换"按路径 / 按文本"（列表重排不影响它们）
                    # 注意 mod 的 uiHold 只认 text 参数（内部先按 label、再按 path 匹配），
                    # 所以按路径按住时要把 path 塞进 text —— 和 uiClick 的 pick 是同一套解析。
                    b = btns[_story_try[0] % len(btns)]
                    label = (b.get("text") or "").strip()
                    for t in (b.get("path") or "", label):
                        if not t:
                            continue
                        r = C.act({"type": "uiHold", "text": t, "seconds": 2.4})
                        if isinstance(r, dict) and r.get("ok"):
                            clicked = ("uiHold", t)
                            break
                elif pts:
                    x, y = pts[_story_try[0] % len(pts)]
                    clicked = real_click(x, y, hold_ms=2600, hover_ms=400)
                elif btns:
                    b = btns[_story_try[0] % len(btns)]
                    C.act({"type": "uiHold", "index": b["i"], "seconds": 1.6})
                    clicked = ("uiHold", b["i"])
                else:
                    x, y = STORY_POINTS[_story_try[0] % len(STORY_POINTS)]
                    clicked = real_click(x, y, hold_ms=2600, hover_ms=500)
                _story_try[0] += 1
                # 游戏不在前台 → 不抢焦点（作者口径），攒够几 tick 就写告警停手，
                # 别在这条路上假装还在干活（上一局就是这么卡了 7 分钟）。
                if not clicked:
                    _need_focus[0] += 1
                    if _need_focus[0] >= 8:
                        reason = "剧情屏长按推不动（只走后台注入，不抢鼠标/不切前台）"
                        print("NEED-FOCUS:", reason, flush=True)
                        write_alert("need-focus", reason, s, path=_ALERT_PATH[0])
                        return "need_focus_stop", 0, {}
                    return "story_need_focus", 2.0, {"try": _story_try[0]}
            _need_focus[0] = 0
            return "story_hold", 2.5, {}
        _story_try[0] = 0

        # 自动装备（P2/P3）：装完会回读验证，结果（装上了几件 / 还有什么没上身）进 status.json。
        # 只有"真装上了"或"有件装不上"才占一个 tick；纯"没东西可装"就继续开车。
        # 车在动就不装（拖拽会落空，见 maybe_equip 注释）。
        if not dry_run and not _in_combat_now(s):
            rep = maybe_equip(speed=float(s.get("speed") or 0.0))
            if rep is not None and (rep.get("equipped") or rep.get("failed")):
                return "equip_gear", 2.0, {"gear": gear_brief(rep)}

        # 罪状/忏悔选择（第 2 行的 BossSelect 节点）。
        # 面板是 screen_boss_select_panel + BossOption(Clone)。
        # 实测的完整手势：
        #   1) 在选项上"按住"约 1.2 秒 —— 单击没用，uiClick/uiInvoke 也没用；
        #   2) 再点右下那张羊皮纸上的签名区 → OnConfirm → 罪状成立、面板关闭。
        # 只按住选项不点签名，面板会留在原地（这就是之前 8 个 tick 卡死的原因）。
        boss_opt = None
        for x in u.get("clickables", []):
            p = x.get("path") or ""
            if "BossOption" not in p:
                continue
            label = (x.get("text") or "").strip()
            if confession and not label.startswith(confession):
                continue
            boss_opt = x
            break
        if boss_opt:
            # 见过 game_over 面板、现在又回到"选忏悔"屏 → 上一局确实团灭了（铁证）。
            # 这时候**别点新局**：直接回报告警、runner 退出码 4，看门狗停手等人。
            if _wipe_pending[0]:
                _wipe_pending[0] = False
                return "game_over", 1.0, {"confirmed": "new-confession", "where": "boss_opt"}
            if not dry_run:
                sig = next((x for x in u.get("clickables", [])
                            if "close_button_parent" in (x.get("path") or "")), None)

                def _boss_open():
                    try:
                        ul = C.get("/ui").get("clickables") or []
                    except Exception:
                        return False        # 读不到就当它关了，别在这儿死循环
                    return any("BossOption" in (x.get("path") or "") for x in ul)

                def _coords_gesture():
                    """这屏必须"按住 + 点签名"：走 mod 的 uiPoint（坐标射线 + 事件分发）。"""
                    C.act({"type": "uiPoint", "x": boss_opt["x"], "y": boss_opt["y"], "holdMs": 1200})
                    time.sleep(1.6)
                    if sig:
                        C.act({"type": "uiPoint", "x": sig["x"], "y": sig["y"], "holdMs": 150})

                # 2026-11-22 实测（新局第一屏就卡这儿）：这屏 uiHold 回 ok=True 但**面板不关**
                # （按住判定依赖真实悬停）。只看 ok 就收工 → 8 个 tick 判 stuck。
                # 所以：语义按住 → 点签名 → **回读 /ui 确认**；还在就退回坐标手势。
                ok_sem = False
                try:
                    r = C.act({"type": "uiHold", "text": boss_opt.get("path") or "",
                               "seconds": 1.4})
                    ok_sem = isinstance(r, dict) and r.get("ok")
                except Exception:
                    ok_sem = False
                if ok_sem:
                    time.sleep(0.8)
                    if sig:
                        C.act({"type": "uiClick", "pick": sig.get("path") or "",
                               "index": sig.get("i", -1)})
                    time.sleep(1.0)
                if not ok_sem or _boss_open():
                    _coords_gesture()
                    time.sleep(1.0)
            _run_started[0] = True
            return "choose_confession", 3, {"confession": boss_opt.get("text")}

        # 节点面板
        # 节点面板：注意 click_by 会先匹配到面板本身（path 里也含 screen_enter_node_panel），
        # 必须挑那个带 InteractButton 的，否则永远点不到"进入"
        panel = None
        for x in u.get("clickables", []):
            p = x.get("path") or ""
            if "screen_enter_node_panel" in p and "InteractButton" in p:
                panel = x
                break
        if panel:
            if not dry_run:
                C.act({"type": "uiClick", "index": panel["i"]})
            return "enter_node", 2, {"node": panel.get("text")}

        # 开车：必须先松开再按，否则游戏会锁住
        # 车没速度时先查两件事，而且顺序很重要：
        #   1) 先看 canDrive 里有没有 =False 的拦截委托（TileNodeBhv.BlockInput 最常见）。
        #      有拦截时车会被强制锁在 NO_INPUT，此时去 coachMode PLAYER_INPUT 永远不生效，
        #      就会变成每个 tick 重试一次、画面零变化的死循环（实测卡在 row=2 那次就是）。
        #   2) 没有拦截、但驾驶模式还停在 NO_INPUT，才去改驾驶模式。
        # 节点面板开着的时候游戏本来就会锁输入，别去硬改。
        panel_open = any("screen_enter_node_panel" in (x.get("path") or "")
                         for x in u.get("clickables", []))
        # 2026-11-25 起每 tick 问一次 coach：卡墙那次的现场是"轮速 12.1、车贴墙不动"，
        # 只在 stopped 时才问根本看不到它（speed>0.5 就跳过）→ 空挡转了 60 秒才被兜底抓。
        # 代价是每 tick 一次 1KB 小 POST（tick 间隔 2.5s），换"当场能判"。
        stopped = float(s.get("speed") or 0) < 0.5
        if not stopped:
            _stop_since[0] = 0.0
        elif not _stop_since[0]:
            _stop_since[0] = time.time()
        stopped_secs = (time.time() - _stop_since[0]) if _stop_since[0] else 0.0
        coach = None
        if not dry_run and not panel_open:
            try:
                coach = C.act({"type": "coach"})
            except Exception:
                coach = None
        # 车停着时的现场（只写进 report，不改决策）
        _drive_diag["inp"] = None
        _drive_diag["blk"] = None
        if coach:
            _drive_diag["inp"] = 1 if str(coach.get("inputEnabled")).lower() == "true" else 0
            _drive_diag["blk"] = next(
                (str(b).split(".")[0] for b in (coach.get("canDrive") or [])
                 if str(b).endswith("=False")), None)

        # 2026-11-25 卡墙真因的兜底：GO_TO_TARGET 是游戏自己的"自动挡"，没有超时、
        # 不听 W、也不受拦车委托管（见 AUTO_MODE_MAX_SECS 上面的注释与行号）。
        # 只要它在 4 秒窗口里几乎没挪位置，就当场掰回 PLAYER_INPUT——那是 EnableInput
        # 认识的模式（StageCoachInternalState.cs:470-483），掰回来车立刻重新受输入/委托管。
        # 位置够就滑动窗口，别把一次正常的长距离 goto 当空转。
        mode_live = ((coach or {}).get("st") or {}).get("drivingMode") or ""
        if mode_live == "GO_TO_TARGET":
            now = time.time()
            px, pz = float(coach.get("posX") or 0), float(coach.get("posZ") or 0)
            if not _goto_ref[0]:
                _goto_ref[0] = (now, px, pz)
            else:
                t0, x0, z0 = _goto_ref[0]
                moved = ((px - x0) ** 2 + (pz - z0) ** 2) ** 0.5
                if auto_mode_wedged("GO_TO_TARGET", now - t0, moved):
                    _goto_ref[0] = None
                    if not dry_run:
                        C.act({"type": "coachMode", "mode": "PLAYER_INPUT"})
                    return "coach_mode", 1.5, {"was": "GO_TO_TARGET",
                                               "why": "自动挡空转", "moved": round(moved, 1)}
                elif now - t0 > AUTO_MODE_MAX_SECS:
                    _goto_ref[0] = (now, px, pz)      # 正常在走 → 换窗口重记
        else:
            _goto_ref[0] = None

        # 岔路口（2026-11-20 02:0x 真因，卡了 40 分钟）：车停在岔路口，
        # speed 从残留的 12.1 慢慢降到 0、drivingMode=NO_INPUT、inputEnabled=False。
        # TriggerIntersectionBhv.GetCanDrive() 返回 False 把车的输入整体锁死，
        # 等玩家去点路面上的路标（RoadIndicatorUIBhv，长按确认）。
        # 实测按 w / a / d、simclick 各种动作名、盲点屏幕 —— 全都没反应；
        # 只有替玩家走一次"选中这条路"的事件才行（mod 的 intersectSelect）。
        # 2026-11-25：coach 改成每 tick 都抓之后，这里必须**补回 stopped**——
        # 车还在往减速点开的那几秒（SLOW_DOWN，TriggerIntersectionBhv.cs:149-155）
        # 也是 inputEnabled=False + intersectionCount>0，提前 intersectSelect 会打乱岔路状态机。
        if coach and stopped and str(coach.get("inputEnabled")).lower() == "false" \
                and int(coach.get("intersectionCount") or 0) > 0:
            if not _road_try[0] or time.time() - _road_try[1] > 20:
                try:
                    opts = (C.act({"type": "intersect"}) or {}).get("options") or []
                except Exception:
                    opts = []
                road = pick_road(opts, prefer_safe=need_safe_node(s))
                if road is not None:
                    C.act({"type": "intersectSelect", "index": road.get("i")})
                    _road_try[:] = [road.get("i"), time.time()]
                    return "pick_road", 2.5, {"road": road.get("type"),
                                              "node": road.get("node")}
            # 选过一次还是不动 → 把车拉回最近的路（游戏自带逻辑），下次重选
            C.act({"type": "coachReset"})
            _road_try[:] = [None, 0.0]
            return "coach_reset", 2.0, {"why": "岔路选路没生效"}

        # 2026-11-23：车停着、游戏自己把输入锁了（inputEnabled=False）、又不是岔路口 ——
        # 这时候刷 drive 一点用没有（实测两轮挂机各空转 30 拍才判死）。
        # wagonDrive 改的是 m_inputAlwaysMoveForward，而 UpdateSpeed 只在 PLAYER_INPUT 分支
        # 读它，PLAYER_INPUT 由 EnableInput(IsPlayerInputEnabled) 决定 → 输入被锁时谁按都没用。
        # 所以这里只计量、不抢分支：unblock / coachMode 该试照试；连续 INPUT_BLOCK_TICKS 拍
        # 还没解开就交回主循环（写 alert、退出码 6），把 32 拍的空转省下来。
        if (coach is not None and stopped
                and str(coach.get("inputEnabled")).lower() != "true"
                and int(coach.get("intersectionCount") or 0) == 0):
            _input_block[0] += 1
            if _input_block[0] >= INPUT_BLOCK_TICKS:
                return "input_blocked", 2.0, {"blocks": _drive_diag.get("blk") or "?",
                                              "n": _input_block[0]}
        else:
            _input_block[0] = 0

        # 2026-11-20 作者口径：车停住了先推它一把（他实测按一下 W 车就自己跑），
        # 别先去 unblock / goNode 绕圈 —— 上一局就是这么绕了十几个 tick，车一步没动。
        # 2026-11-23：不再按 W，改置游戏自己的 m_inputAlwaysMoveForward（纯 API，见 combat3.drive_pulse），
        # 按 0.9 秒后自己松开。
        blocks_n = [x for x in ((coach or {}).get("canDrive") or []) if x.endswith("=False")]
        mode_n = ((coach or {}).get("st") or {}).get("drivingMode") or ""
        if not dry_run and wagon_nudge_needed(stopped_secs, panel_open, blocks_n, mode_n,
                                              time.time() - (_nudge_at[0] or time.time())):
            _nudge_at[0] = time.time()
            C.drive_auto(8000)
            return "drive_nudge", 2.0, {"stopped": round(stopped_secs, 1)}

        # 残队避险（2026-11-19 22:05：2 人残队被连拉 6 场战斗，每场只能撤退，白耗半小时）。
        # 2026-11-25 **删掉原来的 go_safe_node 分支**（那是卡墙真因）：它调 goNode，
        # 让游戏用 GO_TO_TARGET 直线去追任意节点的世界坐标（mod\StateBuilder.cs:717-731），
        # 目标一旦不在"前方路面 10m"那条投影里（StageCoachVehicleControlBhv.cs:884-928），
        # 就变成永远追不到、轮速 12.1、贴墙不动 —— 这一局的 t=1 就是这么把自己焊在墙上的。
        # 避险只走岔路口那条：pick_road(opts, prefer_safe=need_safe_node(s))（上面一笔），
        # 那是游戏自己的"选这条路"事件，不产生追不到的自动挡目标。

        if coach:
            blocks = [x for x in (coach.get("canDrive") or []) if x.endswith("=False")]
            if blocks:
                # 2026-11-21 09:1x 实测：正常行驶（speed 11.8、模式 GO_TO_TARGET）时
                # canDrive 里也常驻 `TileNodeBhv.BlockInput=False` —— 那是"正在过节点"的瞬态，
                # 不是卡住。旧代码见到 False 就 coachUnblock，今天因此空打 148 次（drive 359 次），
                # 每打一次白等 1.5 秒，还会把输入重置一下 → 作者看到的"路上偶尔卡一下"。
                # 现在只认"车真的停了 + 同一个 block 连续两拍还在"。
                sig = tuple(blocks)
                if sig != _block_seen[0]:
                    _block_seen[0], _block_seen[1] = sig, 1
                else:
                    _block_seen[1] += 1
                if float(s.get("speed") or 0) < 1.0 and _block_seen[1] >= 2:
                    _block_seen[1] = 0
                    C.act({"type": "coachUnblock"})
                    return "unblock", 1.5, {"blocks": blocks, "n": _block_seen[1]}
            else:
                _block_seen[0], _block_seen[1] = None, 0
            mode_now = ((coach.get("st") or {}).get("drivingMode") or "")
            # 刚停下 3 秒内别急着改模式：goNode 的 GO_TO_TARGET 起步时 speed 也是 0。
            if mode_now and mode_now != "PLAYER_INPUT" and stopped_secs > 3:
                C.act({"type": "coachMode", "mode": "PLAYER_INPUT"})
                return "coach_mode", 1.5, {"was": mode_now}
        # 上面两条都要么要求 speed<0.5、要么要求 stopped_secs>3；2026-11-25 起
        # GO_TO_TARGET 的行为由本函数开头的 4 秒空转判据管（它不看速度、只看位移）。
        # 这一条留作**其它**模式的最后兜底：同一段 drive 超过 60 秒还不在 PLAYER_INPUT
        # （2026-11-20 01:1x 实测 row 卡 1 空转 40 分钟）→ 强行改回来，实测立刻就动了。
        # coach 已经是每 tick 抓的，不再重复问一次。
        if not dry_run and not panel_open:
            idle = (time.time() - _drive_since[0]) if _drive_since[0] else 0.0
            mode_now = ((coach or {}).get("st") or {}).get("drivingMode") or ""
            if stuck_in_auto_mode(mode_now, idle, panel_open):
                C.act({"type": "coachMode", "mode": "PLAYER_INPUT"})
                _drive_since[0] = time.time()          # 重新计时，别每 tick 都改模式
                return "coach_mode", 1.5, {"was": mode_now, "why": "自动挡空转>60s"}
        # 2026-11-24：改成自动挡（`driveAuto` 心跳 4 s；旧 DLL 自动退化成加长脉冲）。
        # 每 2.5 s 一次的 1.5 s 脉冲，中间 1 s 会掉速 —— 作者看到的"每走一步停一下"就是这个。
        if not dry_run:
            C.drive_auto(8000)
        return "drive", 2.5, {}

    if mode == "out_of_combat":
        u = C.get("/ui")
        texts = " ".join(u.get("texts") or [])
        scene_l = (scene or "").lower()

        # 旅馆/子界面快照：把 /ui 原样落一份，方便离线分析"技能升级/饰品/关系"这些
        # 界面到底有哪些可点元素（只写一次，不刷盘）。
        if scene_l in ("inn", "reflection", "shrine") and not _inn_dumped[0]:
            _inn_dumped[0] = True
            try:
                with open(os.path.join(HERE, "inn_ui_%s.json" % scene_l), "w",
                          encoding="utf-8") as f:
                    json.dump(u, f, ensure_ascii=False, indent=1)
            except OSError:
                pass

        # 崩溃报告弹窗 + 暂停菜单：必须先于一切界面处理，否则底下所有按钮都点不动。
        rep = dialog_decline_report(u)
        if rep is not None:
            click_el(rep, dry_run)
            return "dismiss_report", 1.5, {}
        pr = pause_return(u)
        if pr is not None:
            click_el(pr, dry_run)
            return "resume_pause", 1.5, {}

        # 希望祭坛：有时 mod 报的是 out_of_combat/MainScene（不是 altar_of_hope），
        # 靠 scene 名判会漏 → 一直 idle_MainScene 直到判死（2026-11-20 08:24 实测）。
        # 认「启程」按钮 + 「希望祭坛」文案；旅馆的休息/出发也挂同名按钮，
        # 只认 scene=MainScene 才不会在旅馆里空转（08:32 实测踩过）。
        emb = next((x for x in u.get("clickables", [])
                    if "MouseEmbarkButton" in (x.get("path") or "")), None)
        if emb is not None and (scene or "") == "MainScene":
            if not dry_run:
                C.act({"type": "altarEmbark"})
            return "altar_embark", 4, {}

        # 通用确认框（是/否）：必须在旅馆流程和主菜单之前，否则子界面一开弹窗就空转。
        dlg = dialog_confirm_yes(u)
        if dlg is not None:
            if not dry_run:
                C.act({"type": "uiClick", "index": dlg["i"]})
            return "dialog_yes", 1.5, {}

        # 主菜单：罪状 → 新罪状 →（有存档时）放弃存档「是」。
        # 游戏重启后 runner 停在这一屏，以前只能人工点，现在用后台指针注入走完。
        if scene_l == "main_menu":
            for key, label in (("ConfirmBtn", "menu_confirm"),
                               ("NewGameButton", "menu_new"),
                               ("ConfessionButton", "menu_confession")):
                b = next((x for x in u.get("clickables", [])
                          if key in (x.get("path") or "")), None)
                if b:
                    if not dry_run:
                        # 语义点（路径优先）——主菜单按钮以前按坐标点，窗口大小/DPI 一变就偏
                        C.act({"type": "uiClick", "pick": b.get("path") or "",
                               "index": b.get("i", -1)})
                    return label, 2.5, {}
            return "menu_idle", 2, {}

        # 旅馆/营地：精通点 → 选地区 → 休息 → 出发，全部交给 inn.py（带 phase 状态机）。
        # stressworld / camp 这类场景也是同一套 inn 界面（只是 scene 名不同）。
        if scene_l == "inn" or any("inn_sub_screen" in (x.get("path") or "")
                                   for x in u.get("clickables", [])):
            # 2026-11-21：客栈里顺带处理"道途"（改道途在游戏里就是客栈才允许的操作）。
            # 失败/没得换都不影响 inn 主流程，异常一律吞掉。
            # 2026-11-23 作者口径：默认不自动改道途（开 DD2_AUTO_LOADOUT=1 才恢复）。
            if AUTO_LOADOUT:
                try:
                    if not dry_run:
                        ch = loadout.apply_paths()
                        if ch:
                            print("[道途] %s" % ch, flush=True)
                except Exception as e:
                    print("[道途][失败] %s: %s" % (type(e).__name__, e), flush=True)
            a, delay, info = inn.step(u, dry_run)
            return a, delay, info

        # 其它子界面：什么都不按（按 Esc 会把暂停菜单打开，反而卡死）。
        return "idle_%s" % (scene or "?"), 2.0, {}
    if mode == "embark":
        if not dry_run:
            C.act({"type": "embark"})
        return "embark", 4, {}

    return "idle:%s" % mode, 2, {}


def write_report(path, s, tick, action, stuck=False, why=""):
    """极简一屏报告（--report-json）。字段名用缩写，整行 ≤120 字节。

    这是给"外面看一眼"用的：比 status.json 少一半字段，比 runner.log 少两个数量级。
    """
    try:
        spd = round(float(s.get("speed") or 0.0), 1)
    except (TypeError, ValueError):
        spd = 0.0
    r = {
        "t": tick,
        "mode": s.get("mode"),
        "scene": s.get("scene"),
        "row": s.get("mapRow"),
        "spd": spd,
        "gold": s.get("gold"),
        "act": action,
        "stuck": 1 if stuck else 0,
    }
    if why:
        r["why"] = str(why)[:60]
    # 车停着还没走（drive / drive_blocked / input_blocked）才加现场字段：
    # inp=1/0 游戏此刻还接不接受输入；blk=是谁在拦车（canDrive 里第一个 =False 的委托）。
    # 正常行驶的行不带，整行仍然 ≤120 字节（2026-11-23：带诊断时超了 2 字节，
    # 所以下面按 120 硬裁：先丢 blk，再丢 inp）。
    if action in ("drive", "drive_blocked", "input_blocked") and spd < 0.5:
        if _drive_diag.get("inp") is not None:
            r["inp"] = _drive_diag["inp"]
        if _drive_diag.get("blk"):
            r["blk"] = _drive_diag["blk"][:9]
        while len(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 120:
            if "blk" in r:
                r.pop("blk")
            elif "inp" in r:
                r.pop("inp")
            else:
                break
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))


def _single_instance(fn):
    """进程级单实例：进入 main 拿锁，返回/异常都释放。

    之前只注册 atexit，同一进程连调两次 main（test_until_alert.py）会被自己
    挡成退出码 9；锁的真正语义是"防两个进程同时驱动同一局"，不是防函数重入。
    """
    def wrapper(*a, **k):
        lock = rel = None
        try:
            from instance_lock import acquire, release, other_process, EXIT_ALREADY_RUNNING
            rel = release
            olds = other_process(r"runner\.py")
            if olds:
                print("[runner] 已有 runner 在跑（PID %s），本进程退出。退出码=%d"
                      % (olds, EXIT_ALREADY_RUNNING), file=sys.stderr)
                return EXIT_ALREADY_RUNNING
            lock = acquire("runner")
            if lock is None:
                return EXIT_ALREADY_RUNNING
        except Exception:
            lock = None
        try:
            return fn(*a, **k)
        finally:
            if lock is not None and rel is not None:
                rel(lock, "runner")

    wrapper.__wrapped__ = fn
    wrapper.__name__ = getattr(fn, "__name__", "main")
    return wrapper


@_single_instance
def main(minutes=10, ticks=0, stuck_after=8, status_path=STATUS_DEFAULT,
         dry_run=False, quiet=False, confession="", report_path=None, until="time"):
    """跑循环。minutes<=0 且 ticks<=0 表示不限时间（一直跑）。返回退出码。

    退出码：0=到点正常结束  2=卡死  3=连不上 MOD  4=团灭  5=祭坛等人
            6=车停着且游戏锁了输入（input_blocked，无可点面板）
            7=减员（Boss 前掉人，本局作废，交给 watchdog 重开）
    （2~7 这几种都会先写 alert.json —— 所以在 --until alert 下，这个退出码本身就是"该进场了"的信号。）

    until="time"  ：跑到 --minutes 到点就退（老行为）。
    until="alert" ：事件驱动 —— 平时一直跑，出现告警（或 --minutes 上限到了）才退，
                    外层只需要 job_output(wait) 睡等，不用定时轮询 report.json。

    report_path 非空时（--report-json）：每个 tick 覆盖写一份极简 JSON
    （≤120 字节），外面的人/模型只看这一行就够了，不用读 runner.log 和 status.json。
    """
    if until not in ("time", "alert"):
        raise ValueError("until 只能是 time / alert：%r" % (until,))
    t0 = time.time()
    _T0[0] = t0                     # 给 early_start() 用：启动 90 秒内的 game_over 面板算残留
    end = t0 + minutes * 60 if minutes and minutes > 0 else float("inf")
    alert_path = os.path.join(os.path.dirname(os.path.abspath(status_path)), "alert.json")
    _ALERT_PATH[0] = alert_path
    clear_alert(alert_path)          # 新一轮开始：旧告警作废，别让外面看到过期消息
    print("MODE: until=%s minutes=%s alert=%s"
          % (until, ("不限" if end == float("inf") else minutes), alert_path), flush=True)
    # 撤退口径必须每次都打出来：02:3x 就是"漏了个环境变量"导致一路撤退、还看不出来。
    try:
        import combat3 as _c3
        print("[模式] %s" % ("死战到底（不撤退）" if _c3.NO_RETREAT
                             else "允许撤退（DD2_ALLOW_RETREAT 生效）"), flush=True)
        _NO_RETREAT[0] = bool(_c3.NO_RETREAT)
    except Exception:
        pass
    if until == "alert":
        print("[等待] 事件驱动：只在出告警时退出，期间不需要轮询（退出码 0=跑满无告警）", flush=True)
    tick = 0
    prev_sig = None
    unchanged = 0
    fails = 0
    last_action = None
    last_print = 0.0
    over_ticks = 0   # 连续几个 tick 看到"迷失黑暗"，旧局残留的面板会闪一下，不能一看到就退出
    watch = SW.Watch()   # 卡画面检测：世界指纹 + 每画面预算（见 stuckwatch.py）
    s = {}

    while time.time() < end and (ticks <= 0 or tick < ticks):
        tick += 1

        try:
            # A 层优化：马车阶段允许吃 1s 缓存（drive 是长按，状态变不变无所谓）；
            # 战斗里必须新鲜，max_age=0。见 dd2poll.py 顶部注释：省请求，不省带宽。
            s = dd2poll.get_state(max_age=1.0 if _prev_mode[0] == "map" else 0.0,
                                  fetch=lambda: C.get("/state"))
            fails = 0
        except Exception as e:
            fails += 1
            print("[%s] #%d 连不上 MOD（%d）：%s" % (time.strftime("%H:%M:%S"), tick, fails, e), flush=True)
            write_status(status_path, tick=tick, action="no-state", stuck=False,
                         error=str(e), run_elapsed=round(time.time() - t0, 1))
            if report_path:
                print(write_report(report_path, s, tick, "no-state", why=str(e)), flush=True)
            if fails >= stuck_after:
                reason = "连不上 MOD %d 次：%s" % (fails, e)
                print("STUCK:", reason, flush=True)
                write_alert("mod-down", reason, s, path=alert_path)
                write_status(status_path, tick=tick, action="no-state", stuck=True,
                             reason=reason, run_elapsed=round(time.time() - t0, 1))
                if report_path:
                    print(write_report(report_path, s, tick, "no-state", stuck=True, why=reason), flush=True)
                return 3
            time.sleep(2)
            continue

        mode = s.get("mode")
        prev_mode = _prev_mode[0]
        _prev_mode[0] = mode
        scene = s.get("scene") or ""

        # 2026-11-25 作者定：英雄神殿剧情（角色可习得技能那段）停下挂机，交人工打。
        # 判定只认 /state.gameMode（mod\StateBuilder.cs:50 直接给 GameModeMgr.CurrentMode 名字）。
        # 全游戏唯一入口：TriggerStoryBhv.cs:143-146（StoryType.HERO）+:201（ResultType 为 COMBAT/UI）
        # → HeroStoryIntroPresentationBhv.cs:235 SetMode(HERO_STORY_INTRO)。辅助/抗拒/宇宙/邪教剧情不进这个模式。
        # 必须在这里先手：下面剧情分支（:1129-1255）每 tick 自己点「继续」，5 s 一次的插件轮询来不及。
        if s.get("gameMode") == "HERO_STORY_INTRO":
            reason = "英雄神殿剧情（gameMode=HERO_STORY_INTRO）：章节自带战斗，停手等人工打"
            print("[%s] #%d %s" % (time.strftime("%H:%M:%S"), tick, reason), flush=True)
            write_alert("hero-story", reason, s, path=alert_path)
            write_status(status_path, tick=tick, mode=mode, scene=scene, action="hero-story",
                         stuck=False, reason=reason, run_elapsed=round(time.time() - t0, 1))
            if report_path:
                print(write_report(report_path, s, tick, "hero-story", why=reason), flush=True)
            return 9

        if scene not in ("inn", "combat_arena_stressworld", "camp"):
            inn.reset()
        row = s.get("mapRow")
        gold = s.get("gold")
        try:
            speed = float(s.get("speed") or 0.0)
        except (TypeError, ValueError):
            speed = 0.0

        # 2026-11-21：speed 是残留值，顶墙空转时照样报 12.1（实测）。
        # 用 coach 的 barricade/forced 校正"到底在不在动"，否则卡画面检测永远被清零。
        # 顶墙超过 6 秒就打一次 coachUnblock（mod 里的官方解卡接口）。
        moving = speed > 0.05
        if mode == "map" and moving:
            cb = _coach_stuck()
            if cb["barricade"] or cb["forced"]:
                moving = False
            # 2026-11-21 22:5x：顶墙但 barricade/forced 都为 False 的情况（游戏自己不认为被挡），
            # 用坐标判：speed>2 但 6 秒位移 <0.6 米 → 算没在动，并解一次。
            if cb.get("nogress"):
                moving = False
                try:
                    C.act({"type": "coachUnblock"})
                except Exception:
                    pass
            if cb["barricade"]:
                if not _barr_since[0]:
                    _barr_since[0] = time.time()
                elif time.time() - _barr_since[0] > 6.0:
                    try:
                        C.act({"type": "coachUnblock"})
                    except Exception:
                        pass
                    _barr_since[0] = time.time()
            else:
                _barr_since[0] = 0.0
        else:
            _barr_since[0] = 0.0

        # —— D3 减员检测：Boss 前掉一个人本局基本就没了（作者口径 90%）。
        # 作者口径（2026-11-20）：**本场硬打完 → 放弃征程 → 原地重开**，不是当场撤。
        # 所以战斗里掉人只记标记，等这场结束回到 map 再退 7（交给 watchdog 放弃/重开）。
        # 祭坛/选人时 actors 是"花名册"，会比 4 大 —— 那两种模式一律不数。
        na = party_count(mode, scene, s)
        if na is not None:
            if na > 0:
                _run_party[0] = run_baseline(na, _run_party[0])
                if na < _run_party[0]:
                    _lost_in_fight[0] = True
        lost = False
        if mode == "map" and na is not None:
            lost, _lost_streak[0] = member_lost(_run_party[0], na, _lost_streak[0])
            if _lost_in_fight[0] and na and na < _run_party[0]:
                lost = True          # 战斗里掉过人就够了，不必再等 3 个 tick
        elif mode == "combat" and prev_mode != "combat" and na and na < _run_party[0]:
            # 2026-11-20 22:06 实测漏判：掉人的那场打完直接进了下一场，
            # 中间没有 map tick（结算屏 actors 是空的，na=0 → 不判），
            # 于是残队又开了一场，`MEMBER LOST` 一直没打印。
            # 口径是"本场硬打完 → 弃局"，所以**新一场开打时已经是残队**就该立刻弃。
            lost = True
        if lost:
            # 2026-11-23 §25.5 方案 A：减员时顺手读一次名册，把对方真实 status 钉进
            # alert/status（大前提：拿不准的数据就调 API，不拿推测顶）。
            # 只读、只发这一次；读不到（mod 掉线/场景不对）也照常弃局，不许因为诊断卡住流程。
            try:
                ev = roster_evidence(C.act({"type": "partyInfo"}))
            except Exception as e:
                ev = "名册读不到（%s: %s）" % (type(e).__name__, str(e)[:60])
            reason = "减员：本局最多 %d 人，现在 %d 人（%s）｜%s" % (
                _run_party[0], na, "战斗前" if mode == "combat" else "地图上", ev)
            print("MEMBER LOST:", reason, flush=True)
            write_alert("member-lost", reason, s, path=alert_path)
            write_status(status_path, tick=tick, mode=mode, scene=scene,
                         action="member-lost", stuck=False, reason=reason,
                         roster=ev,
                         run_elapsed=round(time.time() - t0, 1))
            if report_path:
                print(write_report(report_path, s, tick, "member-lost", why=reason), flush=True)
            return 7

        # —— 卡画面检测：只问"世界变了吗"。action 每 tick 都在变也不会清零，
        # 所以马车发呆/客栈发呆/拖饰品发呆都能抓到（老判据只看动作字符串）。
        silent, frozen = watch.feed(time.time(), mode, s, moving=moving)
        if frozen:
            reason = "画面 %.0f 秒没变化（%s/%s）" % (silent, mode, scene)
            print("NO-PROGRESS:", reason, flush=True)
            write_alert("no-progress", reason, s, path=alert_path)
            write_status(status_path, tick=tick, mode=mode, scene=scene,
                         action="no-progress", stuck=True, reason=reason,
                         run_elapsed=round(time.time() - t0, 1))
            if report_path:
                print(write_report(report_path, s, tick, "no-progress", stuck=True, why=reason), flush=True)
            return 8

        try:
            action, delay, extra = do_tick(s, dry_run=dry_run, confession=confession)
        except Exception as e:
            action, delay, extra = "err:%s" % type(e).__name__, 2, {"error": str(e)}
            print("[%s] #%d 动作异常：%s" % (time.strftime("%H:%M:%S"), tick, e), flush=True)

        # 卡住判定：同一个（模式/场景/行/金币/动作）连续出现，且不是在正常开车
        sig = (mode, scene, row, gold, action)
        _drive_since[0] = drive_since_after(action, last_action, _drive_since[0])
        elapsed_drive = (time.time() - _drive_since[0]) if _drive_since[0] else 0.0
        progressing = drive_progressing(action, speed, elapsed_drive)

        if action == "need_focus_stop":
            # 剧情屏/祭坛要真鼠标，但游戏不在前台（不抢焦点）→ 停手等人，别空转判 stall
            write_status(status_path, tick=tick, mode=mode, scene=scene,
                         action="need-focus", stuck=False, reason="游戏不在前台",
                         run_elapsed=round(time.time() - t0, 1))
            return 5
        if action == "altar":
            print("在希望祭坛：请手动点右上角『启程』。这个按钮脚本点不动（uiInvoke/uiClick/鼠标都试过）", flush=True)
            write_alert("altar-manual", "在祭坛等「启程」（脚本点不动，需要人按一下）", s, path=alert_path)
            write_status(status_path, tick=tick, mode=mode, scene=scene, action="altar",
                         stuck=False, reason="waiting for manual embark", run_elapsed=round(time.time() - t0, 1))
            if report_path:
                print(write_report(report_path, s, tick, "altar", why="waiting for manual embark"), flush=True)
            return 5

        if action == "input_blocked":
            # 车停着 + 游戏自己锁了输入 + 没有可点的面板：刷 drive 一点用没有
            # （wagonDrive 改的标志只在 PLAYER_INPUT 分支生效）。提前停手并留下现场，
            # 别让 32 拍空转把告警拖到最后 —— 也免得看门狗那边看不出是"谁在拦车"。
            ex = extra if isinstance(extra, dict) else {}
            reason = "车停着、游戏锁了输入（block=%s），连续 %d 拍无可点面板" % (
                ex.get("blocks") or "?", ex.get("n") or 0)
            print("INPUT-BLOCKED:", reason, flush=True)
            write_alert("input-blocked", reason, s, path=alert_path)
            write_status(status_path, tick=tick, mode=mode, scene=scene,
                         action="input-blocked", stuck=True, reason=reason,
                         run_elapsed=round(time.time() - t0, 1))
            if report_path:
                print(write_report(report_path, s, tick, "input_blocked", stuck=True,
                                   why=reason), flush=True)
            return 6

        if action == "game_over":
            over_ticks += 1
            confirmed = bool((extra or {}).get("confirmed")) if isinstance(extra, dict) else False
            # 面板通常只亮 1 tick（被我们自己的 uiInvoke 点掉），所以正常路径靠 do_tick 的
            # "回到选忏悔屏"确认（confirmed）；连续 2 tick 这条只留给面板自己卡住的情况。
            if confirmed or over_ticks >= 2:
                print("RUN OVER: 队伍全灭（迷失黑暗）%s" % ("（回到忏悔屏已确认）" if confirmed else ""),
                      flush=True)
                write_alert("wipe", "队伍全灭（迷失黑暗）", s, path=alert_path)
                write_status(status_path, tick=tick, mode=mode, scene=scene, action="game_over",
                             stuck=False, reason="party wiped", run_elapsed=round(time.time() - t0, 1))
                if report_path:
                    print(write_report(report_path, s, tick, "game_over", why="party wiped"), flush=True)
                return 4
        else:
            over_ticks = 0
        if sig == prev_sig and not progressing:
            unchanged += 1
        else:
            unchanged = 0
            prev_sig = sig
        # 僵尸战斗（双方 0 血、回合不动）不用等 8 个 tick，3 次就交出去；
        # 剧情长按要试"立绘网格"，点多，给它 20 次。
        if action == "combat_stuck":
            limit = 3
        elif action == "combat_settle":
            limit = 15          # 结算动画比较长，但超过 15 个 tick 就是真卡了
        elif action == "story_hold":
            limit = 20
        elif action == "drive":
            limit = stuck_after * 4
        elif action.startswith("inn") or action in ("lair_escape", "lair_forward", "lair_leave"):
            limit = 40          # 旅馆是一套多步流程，画面本身不变是正常的
        else:
            limit = stuck_after

        now = time.time()
        base = dict(tick=tick, mode=mode, scene=scene, row=row, gold=gold,
                    speed=round(speed, 2), action=action, unchanged=unchanged,
                    retro=("off" if _NO_RETREAT[0] else ("on" if _NO_RETREAT[0] is False else "?")),
                    dry_run=dry_run, run_elapsed=round(now - t0, 1))
        write_status(status_path, stuck=False, **dict(base, **extra))

        if report_path and (action != last_action or tick % 20 == 0):
            print(write_report(report_path, s, tick, action), flush=True)

        if not quiet and (action != last_action or now - last_print >= 30):
            print("[%s] #%d %s/%s row=%s gold=%s act=%s speed=%.1f unchanged=%d"
                  % (time.strftime("%H:%M:%S"), tick, mode, scene, row, gold,
                     action, speed, unchanged), flush=True)
            last_print = now
        # last_action 必须每个 tick 都更新：--quiet 下不更新会让 _drive_since 每 tick 重置
        # （见 drive_since_after 的注释，02:0x 那次 5 分钟空转就是这么来的）。
        last_action = action

        if unchanged >= limit:
            reason = ("同一画面连续 %d 个 tick 没变化（mode=%s scene=%s row=%s action=%s）"
                      % (unchanged, mode, scene, row, action))
            print("STUCK:", reason, flush=True)
            write_alert("stuck", reason, s, path=alert_path)
            write_status(status_path, stuck=True, reason=reason, **dict(base, **extra))
            if report_path:
                print(write_report(report_path, s, tick, action, stuck=True, why=reason), flush=True)
            return 2

        # A 层优化：马车阶段 2s 轮询一次就够（原来 ~0.5s 一次，白烧主线程往返）
        if mode == "map":
            delay = max(float(delay or 0), 2.0)
        time.sleep(delay)

    print("END: until=%s 无告警退出 mode=%s scene=%s gold=%s 用时%.0fs"
          % (until, s.get("mode"), s.get("scene"), s.get("gold"), time.time() - t0), flush=True)
    write_status(status_path, tick=tick, mode=s.get("mode"), scene=s.get("scene"),
                 row=s.get("mapRow"), gold=s.get("gold"), action="end",
                 stuck=False, run_elapsed=round(time.time() - t0, 1))
    if report_path:
        print(write_report(report_path, s, tick, "end"), flush=True)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="DD2 tick runner")
    ap.add_argument("--minutes", type=float, default=None,
                    help="跑多久（默认 10；--until alert 时默认 0=不限，由告警来结束）")
    ap.add_argument("--ticks", type=int, default=0, help="跑多少个 tick（0=不限）")
    ap.add_argument("--until", choices=("time", "alert"), default="time",
                    help="退出条件：time=到点就退（默认）；alert=只在出告警时退（--minutes 仍作上限）")
    ap.add_argument("--confession", default="",
                    help="罪状面板优先选的文本前缀（如 'I.'）；空=选第一个（最简单）")
    ap.add_argument("--stuck-after", type=int, default=8,
                    help="同一画面连续多少个 tick 没变化就判卡死（默认 8）")
    ap.add_argument("--status", default=STATUS_DEFAULT, help="status.json 路径")
    ap.add_argument("--report-json", action="store_true",
                    help="每 tick 覆盖写一份极简 JSON 报告（≤120 字节），只打这一行")
    ap.add_argument("--report", default=os.path.join(HERE, "report.json"),
                    help="--report-json 写到哪里（默认 report.json）")
    ap.add_argument("--dry-run", action="store_true", help="只读状态，不动游戏")
    ap.add_argument("--quiet", action="store_true", help="不打常规日志（STUCK/END 仍打）")
    return ap


def resolve_opts(a):
    """把 argparse 结果翻成 main() 的参数（默认值规则集中在这里，方便离线回归）。"""
    minutes = a.minutes
    if minutes is None:
        minutes = 0.0 if a.until == "alert" else 10.0
    return dict(minutes=minutes, ticks=a.ticks, stuck_after=a.stuck_after,
                status_path=a.status, dry_run=a.dry_run, quiet=a.quiet,
                confession=a.confession,
                report_path=(a.report if a.report_json else None), until=a.until)


if __name__ == "__main__":
    sys.exit(main(**resolve_opts(build_parser().parse_args())))

