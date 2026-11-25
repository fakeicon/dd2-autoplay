"""combat3.py —— 数据驱动战斗打手 v5：威胁排序 + 每场战斗落一份日志。

v4：把每招 2.4 秒的固定等待压到 0.1 秒轮询。
v5：目标不再"打血最少的"，改成"打最危险的"（远程/群攻/上压力/治疗/召唤），
    能一刀收掉的仍然优先（少一个敌人 = 少一整个回合）。
    每场战斗往 combat_log.jsonl 追加一行：回合数、谁打了谁多少、血线变化、赢没赢。
    有了这份日志，才谈得上"改规则能不能提高胜率"。
"""
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:18082"
CAT = {}
HEAL = ("heal", "grace", "medicine", "sanctuary", "comfort", "fortitude")

# 只对自己用的治疗技能（2026-11-20 作者实测：掘墓女可以给自己回血，我们一次都没用过）。
# 全历史 combat_log 里 gr_dead_of_night 出现 0 次——因为 hurt 列表把"自己"过滤掉了。
SELF_HEAL = ("dead_of_night", "absinthe", "artemisia", "self_heal")

# 2026-11-23（方案 A）：死门"跳过行动回血"的正主是**技能** pass_heal，不是 pass 动作。
# 依据：{"type":"pass"} → EventBattlePass → BattleStateMachine.HandleEventBattlePass 只
# QueueNextState(END_TURN)（BattleStateMachine.cs:178），不跑技能；UI 上的 pass 按钮其实是
# IsPassSkill 技能（SkillSelectionBhv.cs:269-323），pass_heal 效果 = heal_20pct + stress_damage_2
# （db/skills_full.json）。数据：253 次死门 pass 只有 30 次回血（12%）。
PASS_HEAL = "pass_heal"

# 免疫 DoT 的目标（2026-11-20 作者实测：建筑物不吃毒，乌鸦一直往路障上放毒）。
# 目前只认实测见过的路障；以后遇到别的结构再加，别按名字猜。
STRUCTURE_IDS = ("barricade",)
DOT_SKILLS = ("noxious", "blinding_gas", "plague", "toxin", "poison", "blight", "burn", "ignite")


def is_immune_target(a):
    """结构/建筑类敌人：不吃毒（也不该被当 DoT 目标）。"""
    return any(k in (a.get("dataId") or "").lower() for k in STRUCTURE_IDS)


def skill_is_dot(sk):
    """是不是持续伤害技能：优先看 catalog 的 tags，查不到才退回名字关键字。

    2026-11-21：名字规则会漏（苦艾酒那种改名/换 id 的情况），tags 是游戏自己给的。
    """
    c = CAT.get(sk) or {}
    tags = [str(t).lower() for t in (c.get("tags") or [])]
    if tags:
        return any(t in ("blight", "bleed", "burn", "dot") for t in tags)
    return any(k in (sk or "").lower() for k in DOT_SKILLS)


HERE = os.path.dirname(os.path.abspath(__file__))
_SHOT = {"n": 0}          # DD2_FAIL_SHOT 的配额（只影响诊断截图，不影响决策）
# 最近一场的收尾信息。runner 靠它判"刚撤退过 → 接下来只走安全节点"，
# 以前撤退完直接把残队又推进下一场（2026-11-21 07:31 那局就是这么团灭的）。
LAST_FIGHT = {}
COMBAT_LOG = os.path.join(HERE, "combat_log.jsonl")
DD_LOG = os.path.join(HERE, "logs", "dd.jsonl")


def dd_record(fight, rec):
    """死门样本：**一发生就**追加一行到 logs/dd.jsonl。

    2026-11-20 20:2x 教训：样本原来只在战斗结束时随 combat_log 落盘（一场一行），
    结果那场打到第 10 回合游戏卡死、整局被结束 → 6 条死门样本全丢。
    现在与"战斗是否结束"解耦，边打边写。
    """
    out = dict(rec)
    out["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["scene"] = (fight or {}).get("scene")
    try:
        os.makedirs(os.path.dirname(DD_LOG), exist_ok=True)
        with open(DD_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return out

POLL = 0.10          # 确认阶段每帧间隔
FAST_CONFIRM = 0.9  # skill 之后等这么久还没变化 → 补 select
SKILL_CONFIRM = 4.0  # 出招后等它推进的时间：技能动画 2~4 秒，0.9 秒就重试 = 重复出招（死锁来源）
SLOW_CONFIRM = 0.50  # 补了 select 之后再等这么久
IDLE_POLL = 0.15     # 敌人回合/非我方回合，多久看一次
MAX_TRIES = 3        # 一次行动最多试几组
MAX_FAILS_BEFORE_PASS = 2  # 试两下都没推动 → 这回合大概不让出招，直接 pass
_dd_round = [None]         # 死门逐回合记录的"上一回合号"（每场战斗重置）
EVENT_CONFIRM = 1.9  # 文字 skill 事件发出后等结算的时间（2026-11-21 实测 1.2s 掉血）
PASS_CONFIRM = 0.80  # 发 pass 之后等它推进的时间
SEL_CONFIRM = 0.90   # 点完技能格后回读"到底选中没有"的窗口（mod 的 skills 带 selected，能直接看）
TURN_SETTLE = 1.20   # 刚轮到我方那 1~2 秒游戏会把技能事件吞掉（实测同一招隔一秒重发就生效）
DMG_EST = 11.0       # 估算一刀伤害（catalog 的 stats 现在是 null，先按经验值算）
# —— 收尾/撤退保护（2026-11-19 15:20 加）——
# 血案：15:12 那场 combat_arena_city_gaunt，只剩 1 个 hp1 的 shared_lost_soul_yeoman，
# Dismas 把它削到 0 血后 **0.3 秒**（2 次轮询）就被下面的"僵尸战斗"分支判死，
# 发了 EventBattleRetreat → 游戏结算成「突围」→ 到手的胜利变成撤退（gold 没涨、无奖励）。
FINISH_ENEMY_HP = 12.0    # 最后一个还活着的敌人 hp ≤ 这个数 → 收尾阶段，任何理由都不撤
FINISH_HP_PCT = 15.0      # 对面总血 ≤ 15%（或每个敌人都 ≤8）→ 收尾阶段
STALL_DOWNED_SECS = 25.0  # 敌人全 0 血却一直不结算：等满 25 秒才当"真僵尸"处理（以前 0.3 秒）
STALL_REPORT_SECS = 2.0   # 等满 2 秒先打一行日志，方便事后确认"是不是又被误判了"
# 2026-11-19 23:00 实测：combat_arena_city_resist 卡在 battleState=in_turn_perform，
# 点技能/点目标/pass/EventBattlePass 全都不推进 —— 40 个动作白烧 130 秒，
# 而 max_secs=900 要等满 15 分钟才交回 runner。连续这么多个"动作"画面零变化就判冻结。
FROZEN_SPINS = 12
# 连续这么多个动作零变化时，先试一次"解冻三招"（右键取消/再pass/Esc收暂停），见 unstick()。
# 三招都不动才继续数到 FROZEN_SPINS 判冻结。实测冻结判太早会白重启游戏，判太晚白烧时间。
FROZEN_RESCUE = 5
# 2026-11-20 01:28 起改成**按时间**判（`battleState=in_turn_perform` 是硬死锁：
# 右键/pass/点技能/点敌人 四种手势全都不推进，实测每种都白等 2.5 秒）。
# 按"动作次数"数的老判据在硬死锁下太吵（每次点击都算一次），而且 poll 间隔一变就不准。
FROZEN_RESCUE_SECS = 40.0     # 画面 40 秒没变化 → 试自救三招
FROZEN_SECS = 120.0           # 120 秒还没变化 → 判冻结，交回 runner
IN_FLIGHT_STATES = ("perform", "anim", "resolve")   # battleState 含这些词 = 正在播动作


def in_flight(state):
    """游戏正在播动作（这时点什么都没用）。"""
    bs = str((state or {}).get("battleState") or "").lower()
    return any(k in bs for k in IN_FLIGHT_STATES)


def frozen_stage(elapsed, rescue_tried):
    """画面多久没变 → 'ok' / 'rescue'（试三招）/ 'frozen'（交回 runner）。"""
    if elapsed >= FROZEN_SECS:
        return "frozen"
    if not rescue_tried and elapsed >= FROZEN_RESCUE_SECS:
        return "rescue"
    return "ok"
# 用户口径（2026-11-20 明确）：**默认死战到底** —— 该撤也硬打完，绝不主动撤退。
# 2026-11-19 夜那版是"默认撤退、设 DD2_NO_RETREAT=1 才硬打"，结果 02:3x 那轮我漏了这个
# 环境变量，一路 `[撤退] 局面崩了…尝试撤退` 退了七八场，违背了"死战到底"的要求。
# 所以现在反过来：撤退要**显式**开。
#   DD2_ALLOW_RETREAT=1 → 恢复"打不过就撤"（旧名 DD2_NO_RETREAT=0 也认，兼容老脚本）
def _retreat_allowed():
    for name, yes in (("DD2_ALLOW_RETREAT", ("1", "true", "True")),
                      ("DD2_NO_RETREAT", ("0", "false", "False"))):
        if os.environ.get(name, "").strip() in yes:
            return True
    return False


NO_RETREAT = not _retreat_allowed()
_item_used = [False]  # 本场战斗是否已经扔过战斗道具（P3，一场一次）
_affinity = [{}]      # 本场战斗开场时的两两关系值 {(guidA,guidB): leaning}（P5）

# 敌方威胁：按 dataId + 技能名关键字给分
THREAT_TAGS = (
    (("mortar", "barrage", "aoe", "blast", "spray", "grenade", "splash"), 9, "群攻"),
    (("stress", "terror", "howl", "shriek", "madness", "dread", "panic"), 8, "上压力"),
    (("summon", "reinforce", "spawn", "raise", "call_forth"), 8, "召唤"),
    (("ranged", "archer", "gun", "cannon", "thrower", "bow", "shot"), 6, "远程"),
    (("heal", "mend", "bless", "ward", "sanctuary", "restore"), 6, "治疗/增益"),
    # 攻略 §6：指挥/加 buff 的怪优先级最高（燔城萨满那种比 4 号位女爵还优先）
    (("bishop", "shaman", "priest", "commander", "captain", "altar", "chant", "worship",
      "inspire", "rally", "banner", "totem"), 9, "指挥/光环"),
    (("guard", "defender", "protect", "bulwark", "shield"), -3, "护盾型"),
)
THREAT_CAP = 18


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def act(o):
    d = json.dumps(o, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/action", data=d, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def skills_of(a):
    sk = a.get("skills") or []
    if isinstance(sk, str):
        return []
    return [x["id"] for x in sk if isinstance(x, dict) and x.get("id")]


# ---- 游戏操作：能用游戏 API 就用 API（2026-11-23 作者口径，2026-11-23 08:5x 复核并更正）----
# 实际机制（**按 mod 源码核过，别照旧注释里的说法**）：
#   drive        → 置 m_inputAlwaysMoveForward（StateBuilder.cs:609-616，纯 API）
#   wagonDrive   → Coach.Drive，也是置 m_inputAlwaysMoveForward（mod\Coach.cs:191-216）
#                  —— 和 drive 同一机制，只是名字还在作者给的"不用"清单里 → 本文件已改用 drive
#   cancelSelection → CombatUi.CancelSelection（旧路，清单里）
#   simAction    → SimAction.Fire → InputSimulatorUtils.SimulateActionClick（StateBuilder.cs:2324-2330）
#                  实现见 InputSimulatorUtils.cs:33-69：**不碰任何输入设备**。它只是把 UI 代码用
#                  `InputSystemBhv.AddListener(actionName, cb)`（InputSystemBhv.cs:164-173）注册的回调
#                  按 actionName 取出来，用伪造的 InputActionDelegateValues(performed:true) 调一遍，
#                  隔一帧再发一次 canceled —— 也就是"对游戏自己注册的 UI 处理器做一次 API 分发"。
#                  （2026-11-23 09:1x 按反编译核实：它不是键鼠模拟，别再说它是。）
# 回退策略（2026-11-23 08:5x 改）：以前 API 一失败就**无条件**回退到 key/mouse/pointerClick，
# 日志里只留一行 [api-fallback]；现在回退默认**关**（`_legacy_input_allowed()`，要开设
# DD2_LEGACY_INPUT=1），失败就如实报错、交给调用方的重试/卡画面逻辑 —— 不许悄悄注入键鼠。
_FALLBACK_NOTE = {}


def _note_fallback(name, why):
    if _FALLBACK_NOTE.get(name) == why:
        return
    _FALLBACK_NOTE[name] = why
    print("[api-fallback] %s → 键鼠（%s）" % (name, why), flush=True)


def _legacy_input_allowed():
    """允许"回退到键鼠注入"吗？默认**不许**（作者 2026-11-23 口径：游戏内只走游戏 API）。

    这些回退用的是 mod 的 `key` / `mouse` / `pointerClick` —— 前三个就是注入本机输入。
    默认关掉之后：API 失败就**如实报错**、交给调用方的重试/卡画面逻辑处理，
    而不是悄悄注入键鼠（以前 API 一失败就默默走回键鼠，日志里看不出来）。
    老 DLL（没有新 action）或要复现历史行为时才设 `DD2_LEGACY_INPUT=1`。
    """
    return os.environ.get("DD2_LEGACY_INPUT", "").strip() in ("1", "true", "True")


def _note_legacy_blocked(name, why):
    """回退被策略挡下时只打印一次（别每 tick 刷屏）。"""
    key = "%s|blocked" % name
    if _FALLBACK_NOTE.get(key) == why:
        return
    _FALLBACK_NOTE[key] = why
    print("[api-fallback] %s → 键鼠**被策略挡下**（%s；要回退设 DD2_LEGACY_INPUT=1）"
          % (name, why), flush=True)


def drive_pulse(ms=900):
    """推一下马车，`ms` 毫秒后自动松开。

    机制（2026-11-23 08:5x 按 mod 源码核过，不是猜）：
      · `wagonDrive` → `Coach.Drive`（`mod\\Coach.cs:191-216`）= 置 `m_inputAlwaysMoveForward`
        + 由 **mod 侧协程**在 holdMs 到点自动松开 —— 和 `drive`（`StateBuilder.cs:609-616`）
        是**同一个机制**，**不是键鼠模拟**。名字留在作者的旧禁用清单里属"清单过期"，已提请改口径。
      · 这里仍用 wagonDrive 而不是 drive：`drive` 没有 holdMs，得靠进程自己再发一次 off，
        进程在脉冲中途死掉就会让马车一直"自动前进"；wagonDrive 的松开在 mod 侧，进程死了也会松。
    """
    return act({"type": "wagonDrive", "on": True, "holdMs": int(ms or 0)})


def drive_auto(ms=8000):
    """马车前进：自动挡 `driveAuto` **和**长脉冲一起发 —— 两个都要，缺一就有空档。

    · `driveAuto` → `Coach.Auto`（`mod\\Coach.cs:227-244`）**只置心跳**：`_autoUntil = now + 4f`
      （`:229`）；真正每帧置 `m_inputAlwaysMoveForward` 的是 `AutoTick()`（`:251-276`）。
      作用是把 `NO_INPUT` 拉回 `PLAYER_INPUT`、节点/UI 结束后自己续。
    · 但心跳只有 4 s，runner 两次 drive tick 间隔实测 4~6 s（`runner.py:1421` / `:1477`）→
      空档里 af 会被清（`StageCoachInternalState.cs:247-251`），车速每 ~5 s 掉一次。
    · 所以再补一发 `wagonDrive` 脉冲：`holdMs` 比 tick 间隔长，下一次脉冲作废上一次的自动松开
      （`Coach.cs:295-299` 的 token）→ 输入连续；松开在 mod 侧，进程死了也会松手。
    · **不能**用 `driveAuto` 响应里的 `alwaysForward` 判断是否生效：`Coach.cs:238` 用 `S()` 去查
      `GetAlwaysMoveForward` 这个**无参方法**，永远返回 null（实测恒 null）。
    """
    r = act({"type": "driveAuto", "on": True})
    drive_pulse(ms)
    return r


def wagon_drive(on=True, hold_ms=0):
    """**已无调用方（2026-11-23 08:5x）**：保留只为兼容老脚本，内部转纯 API 的 `drive`。

    新代码请直接用 `drive_pulse(ms)`（runner 就是这么做的）；这个壳子留着不碍事，
    但别再往里加逻辑 —— 它的名字和已删的 wagonDrive action 很像，容易混。
    """
    if on:
        return drive_pulse(hold_ms or 0)
    return act({"type": "drive", "active": False})


def cancel_selection():
    """取消已选技能/目标（以前是右键点屏幕正中）。"""
    r = {}
    try:
        r = act({"type": "cancelSelection"})
        if isinstance(r, dict) and r.get("ok"):
            return r
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    why = (r or {}).get("error") or "no cancelSelection"
    if not _legacy_input_allowed():
        _note_legacy_blocked("cancel_selection", why)
        return r
    _note_fallback("cancel_selection", why)
    try:
        act({"type": "mouse", "x": 960, "y": 540, "right": True})
    except Exception:
        pass
    return r


def ui_point(x, y, hold_ms=0):
    """按屏幕坐标点/按住：mod 里做射线 + 事件分发（不再是 pointerClick 注入鼠标）。"""
    r = {}
    try:
        r = act({"type": "uiPoint", "x": int(x), "y": int(y), "holdMs": int(hold_ms or 0)})
        if isinstance(r, dict) and r.get("ok"):
            return r
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    why = (r or {}).get("error") or "nothing at point"
    nm = "ui_point(%d,%d)" % (int(x), int(y))
    if not _legacy_input_allowed():
        _note_legacy_blocked(nm, why)
        return r
    _note_fallback(nm, why)
    try:
        act({"type": "pointerClick", "x": int(x), "y": int(y), "hold": max(0, hold_ms) / 1000.0})
    except Exception:
        pass
    return r


def ui_action(name, phase="click"):
    """触发游戏自己注册的输入动作（Submit / ExitMenu / PauseMenu / CharacterSheet …）。

    **机制（2026-11-23 09:1x 按反编译核实，之前注释写错了）**：这条路是 mod 的 `simAction`
    → `SimAction.Fire` → `InputSimulatorUtils.SimulateActionClick`（`InputSimulatorUtils.cs:33-69`）。
    它**不碰键盘鼠标**：只是把 UI 用 `InputSystemBhv.AddListener` 注册的回调按名字取出来，
    用伪造的 `InputActionDelegateValues(performed:true)` 调一遍，隔一帧再发 canceled。
    也就是"对游戏自己的 UI 处理器做 API 分发"——所以它是**当前最合适的通道**，
    没有更纯的替代品（`InputSystemBhv` 只暴露 AddListener，没有公开的'按名触发'入口）。
    phase: click / start / end（长按拆两次）。
    """
    r = {}
    try:
        r = act({"type": "simAction", "name": name, "phase": phase})
        if isinstance(r, dict) and r.get("ok"):
            return r
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    why = (r or {}).get("error") or "not bound"
    nm = "ui_action:%s/%s" % (name, phase)
    if not _legacy_input_allowed():
        _note_legacy_blocked(nm, why)
        return r
    _note_fallback(nm, why)
    key = {"Submit": "space", "ExitMenu": "esc", "PauseMenu": "esc",
           "CharacterSheet": "c"}.get(name)
    if key:
        try:
            if phase == "start":
                act({"type": "key", "keys": key})
            elif phase == "end":
                act({"type": "key", "keys": key, "up": True})
            else:
                act({"type": "key", "keys": key})
                time.sleep(0.05)
                act({"type": "key", "keys": key, "up": True})
        except Exception:
            pass
    return r


# ---- 游戏自己的伤害预览（2026-11-21 接入）--------------------------------
# POST /action {"type":"skillPreview"} → QuerySkillPreview.Trigger(...) →
# SkillCalculation.AddPotentialResult(...) → ActorResult.SkillPreview。
# 字段见 SkillCalculation.cs:264-279；这是 UI 提示框上的那个数，
# 含暴击/格挡/易伤/buff 全部乘区，不需要我们复算。
_PV_CACHE = {}


def preview(performer_guid, skill_id, target_guid):
    """返回 (low, high, crit, is_damage) / None。按 (人,技能,目标) 缓存，每 tick 清一次。"""
    key = (int(performer_guid or 0), str(skill_id or ""), int(target_guid or 0))
    if key in _PV_CACHE:
        return _PV_CACHE[key]
    out = None
    if key[1]:
        try:
            r = act({"type": "skillPreview", "actorGuid": key[0],
                     "skillId": key[1], "targetGuid": key[2]})
        except Exception:
            r = None
        if isinstance(r, dict) and r.get("ok"):
            for p in r.get("previews") or []:
                if int(p.get("target") or 0) != key[2]:
                    continue
                out = (float(p.get("low") or 0.0), float(p.get("high") or 0.0),
                       float(p.get("crit") or 0.0), bool(p.get("isDamage")))
                break
    _PV_CACHE[key] = out
    return out


def load_cat(ids):
    ids = [i for i in dict.fromkeys(ids) if i not in CAT]
    if not ids:
        return
    try:
        r = get("/catalog?ids=" + urllib.parse.quote(",".join(ids)))
        for s in r.get("skills", []):
            CAT[s["id"]] = s
    except Exception as e:
        print("catalog err", e)


def threat_of(a):
    """这个敌人有多危险：**先用游戏本体数据算**，查不到才退回关键字。返回 (分数, 理由)。

    2026-11-20：以前全靠 dataId/技能名里的关键字（mortar/stress/aoe…）猜，
    现在 138 只怪、495 条技能从 StreamingAssets\\Excel 导成了
    `游戏数据-怪物技能.json`（伤害/群攻/压力/DoT/多段/暴击都是真数值）。
    """
    try:
        import gamedata
        sc, why = gamedata.threat(a.get("dataId"))
        if sc is not None:
            if (a.get("position") or 0) >= 2:
                sc = min(THREAT_CAP, sc + 2)
            return sc, why
    except Exception:
        pass
    text = ((a.get("dataId") or "") + " " + " ".join(skills_of(a))).lower()
    sc, why = 0, []
    for keys, w, label in THREAT_TAGS:
        if any(k in text for k in keys):
            sc += w
            why.append(label)
    if (a.get("position") or 0) >= 2 and any(k in text for k in ("ranged", "mortar", "shot", "bow")):
        sc += 2  # 后排远程更难处理
    return max(-5, min(THREAT_CAP, sc)), "/".join(why) or "-"


def score(sk, c, t, enemies, th):
    """技能×目标的分数：威胁是主因，能一刀收掉的额外加分。

    目标优先级按攻略 §6：指挥/加 buff 位 > 后排 > 普通。
    后排权重单独给，因为"先杀后排"是作者的明确口径（只有后排难杀时才改打前排）。
    """
    sc = th * 3.0
    # 收人头 >> 打满血：少一个敌人 = 少一整个回合的输出。
    # 这两个数值要压过"威胁 18 × 3 = 54"，否则会放着 6 血的残兵去打满血的指挥。
    if t["hp"] <= 6:
        sc += 90
    elif t["hp"] <= DMG_EST:
        sc += 60
    # §6「先杀后排」：站位越大越靠后（0 是前排）
    pos = t.get("position") or 0
    if pos >= 3:
        sc += 10
    elif pos == 2:
        sc += 5
    if c.get("multi"):
        ranks = set(int(x) for x in (c.get("target") or []))
        n = sum(1 for e in enemies if not ranks or e["position"] in ranks)
        sc += 5 * max(0, n - 1)
    if any(k in sk for k in ("stun", "daze", "rampart", "gas", "blind")):
        sc += 6 if t["hp"] > 18 else 2
    if any(k in sk for k in ("blight", "bleed", "burn", "incision", "blast")):
        sc += 4
    sc -= (c.get("cd") or 0) * 4
    if "move" in sk:
        sc -= 6
    return sc


def _leaning(g1, g2):
    """两人的关系值；没有连接就当 99（排最后）。"""
    try:
        return _affinity[0].get(frozenset((str(g1), str(g2))), 99)
    except Exception:
        return 99


# ---- 技能位移表（从游戏数据生成：build_move_table.py → data/skill_move.json）----
# self    = 施术者自己位移（负=向前，正=向后）
# target  = 目标被推/拉的位移
# to_target = 移动到目标位置（Move 类动作）
MOVES = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "skill_move.json"),
              encoding="utf-8") as _mf:
        MOVES = json.load(_mf)
except (OSError, ValueError):
    MOVES = {}


def usable_at(me, pos, mine):
    """站在 pos 时这个英雄能出手的攻击技能数（看每个技能的 launch 区间）。"""
    n = 0
    for sk in mine:
        c = CAT.get(sk) or {}
        if c.get("friendly"):
            continue
        launch = [int(x) for x in (c.get("launch") or [])]
        if launch and pos in launch:
            n += 1
    return n


def reposition_pick(me, mine):
    """被换位后：按数据表里的位移，找一个能明显多放技能的位置。

    只有"多出 ≥2 种技能可用"才值得花一手去挪（否则不如直接打）。
    返回 (技能, 目标位, 增益) 或 None。
    """
    cur = me.get("position")
    if cur is None or (me.get("hp") or 0) <= 0:
        return None
    base = usable_at(me, cur, mine)
    best = None
    for sk in mine:
        info = MOVES.get(sk) or {}
        mv = info.get("self") or 0
        if not mv:
            continue
        dst = int(cur) + int(mv)
        if dst < 0 or dst > 3:          # 我方最多 4 人，位次 0..3
            continue
        n = usable_at(me, dst, mine)
        if n - base >= 2 and (best is None or n > best[2]):
            best = (sk, dst, n - base)
    return best


def stranded_move(me, mine, allies, vm=None):
    """当前位次一手攻击都放不出时，用游戏自带的 `*_move` 挪回能打的位置。

    依据（都回反编译核过，2026-11-23 08:5x 把一条悬空引用换掉了）：
      · `ActorDataSkill.IsMoveSkill => m_Tags.Contains("move")`（ActorDataSkill.cs:259）；
      · `BattleStateMachine.ApplySkillStartEffects`：选中技能的 `m_IsMoveToTarget` 且有目标时，
        执行 `SetTargetTeamPosition(目标.TeamPosition, …)`（BattleStateMachine.cs:572-583；
        方法定义在 ActorInstance.cs:3883）—— 也就是"挪到那个队友的位次、他把位次让给你"。
        （原来这里引的是那个已删的大文件的行号；2026-11-23 08:5x 换成上面可核的行号）
      · `*_move` 不挂在战斗界面那 5 个技能格上（mod StateBuilder.cs:846 注释：MoveButton /
        pass_skill_button 也挂 SkillButtonBhv），所以 mod 的 valid_targets 看不到它，必须直接发 skill 事件。

    只有"一个合法攻击技能都没有"才挪（vm 里有任何一招 valid 就不算被卡住）。
    返回 (moveSkillId, 目标英雄guid, 说明) 或 None。
    """
    cur = me.get("position")
    if cur is None or (me.get("hp") or 0) <= 0:
        return None
    attacks = [sk for sk in mine if not (CAT.get(sk) or {}).get("friendly")]
    if not attacks:
        return None
    if any((vm or {}).get(sk, (False, set()))[0] for sk in attacks):
        return None                      # 游戏说还有能打的招 → 不是被位次卡住
    if usable_at(me, cur, mine) > 0:
        return None                      # catalog 也认为能打 → 不动
    mv_sk = next((str(k) for k in skills_of(me) if str(k).endswith("_move")), None)
    if not mv_sk:
        return None
    if _battle_fails.get((mv_sk, me["guid"]), 0) >= 2:
        return None                      # 本场这招已经被游戏拒过 2 次，别硬试
    best = None
    for a in allies:
        if a["guid"] == me["guid"] or a.get("position") is None:
            continue
        n = usable_at(me, a["position"], mine)
        if n > 0 and (best is None or n > best[1]):
            best = (a, n)
    if not best:
        return None
    a, n = best
    return (mv_sk, a["guid"],
            f"挪位{mv_sk}->{a.get('name')}的第{a['position'] + 1}位(可放{n}招)")


def _attack_skills(a):
    """这个英雄装备的攻击技能（排除 *_move / pass_*）。"""
    return [x for x in skills_of(a)
            if not str(x).endswith("_move") and not str(x).startswith("pass_")]


def shift_positions(pos, caster, delta):
    """预测"施法者自己前/后移 delta 位"之后全队位次（0-based，已压实）。

    依据反编译源码 `logs/decomp/all/Assets.Code.Actor/ActorTeamPositionCalculation.cs:8-152`：
    `SortActorsByTargetTeamPositions` 把移动者插到目标位，**途经的每个英雄各让 1 位**，
    最后 `SetActorTeamPositions` 按新顺序压实成 0..n-1。
    实测对得上 05:46 那场：Barristan 2→0 时 Baldwin 0→1、Boudica 1→2。
    """
    pos = dict(pos)
    if caster not in pos or not delta:
        return pos
    p = int(pos[caster])
    n = len(pos)
    dest = max(0, min(n - 1, p + int(delta)))
    for g, q in list(pos.items()):
        if g == caster:
            continue
        if delta < 0 and dest <= q < p:
            pos[g] = q + 1          # 往前插 → 途经的人后退 1
        elif delta > 0 and p < q <= dest:
            pos[g] = q - 1          # 往后插 → 途经的人前进 1
    pos[caster] = dest
    rank = {g: i for i, (g, _) in enumerate(sorted(pos.items(), key=lambda kv: kv[1]))}
    return {g: rank[g] for g in pos}


def strands_ally(me, sk, allies):
    """这招的自移会不会把某个队友挤到"一个攻击技能都放不出"的位次。

    2026-11-22 现场：士官在 4 号位用 `maa_hold_the_line`（self -3）一路插到 1 号位，
    把麻风病人顶到 3 号位；麻风的 chop/purge/intimidate 的 launch 只有 0-1 位，
    于是整场只剩 `lep_withstand`（日志里反复出现"游戏表可用 lep_withstand:1"）。
    返回 (被卡住的英雄名 或 "", 详情)。
    """
    delta = int((MOVES.get(sk) or {}).get("self") or 0)
    if not delta:
        return "", ""
    pos = {a["guid"]: a.get("position") for a in allies if a.get("position") is not None}
    if me["guid"] not in pos:
        return "", ""
    before = {}
    for a in allies:
        g = a["guid"]
        if g in pos:
            before[g] = usable_at(a, pos[g], _attack_skills(a))
    after = shift_positions(pos, me["guid"], delta)
    for a in allies:
        g = a["guid"]
        if g == me["guid"] or g not in pos:
            continue
        if before.get(g, 0) >= 1 and usable_at(a, after[g], _attack_skills(a)) == 0:
            return (a.get("name") or str(g),
                    f"{sk} 会把 {a.get('name')} 从第{pos[g] + 1}位挤到第{after[g] + 1}位")
    return "", ""


def _item_kind(sid):
    """战斗道具的效果类型：heal / stress / defense / attack / utility。

    2026-11-21 用游戏里的 skill 元数据（db/skills.json 的 tags/friendly/target）分类，
    不再按名字瞎猜：
      tags=heal         → heal（burn_salve / pustule_salve / medicinal_herbs）
      tags=stress_heal  → stress（laudanum）
      friendly 且 target 为空 → utility（torch_consumable）—— 战斗里不用
      friendly 其他      → defense（ablative_powders 死甲 / rag / stimulants）
      非 friendly        → attack（smoke_bomb / pouch_of_lye / bear_trap）
    """
    sk = _skills().get(sid) or {}
    tags = str(sk.get("tags") or "").lower()
    friendly = sk.get("friendly")
    if "heal" in tags and "stress" not in tags:
        return "heal"
    if "stress" in tags:
        return "stress"
    if not friendly:
        return "attack"
    if not (sk.get("target") or []):
        return "utility"
    return "defense"


def _item_damage(sid):
    """战斗道具的预估伤害：db 里 damage 是字符串，拿不到就给个保守值 6。"""
    try:
        d = (_skills().get(sid) or {}).get("damage")
        if d is None or d == "":
            return 6
        return int(float(str(d).split("~")[0]))
    except (TypeError, ValueError):
        return 6


def _skills():
    global _SKILL_DB
    try:
        return _SKILL_DB
    except NameError:
        pass
    _SKILL_DB = {}
    try:
        with open(os.path.join(HERE, "db", "skills.json"), encoding="utf-8") as f:
            for x in json.load(f):
                _SKILL_DB[x["id"]] = x
    except Exception:
        pass
    return _SKILL_DB


# 每场战斗的"闸门计量"：为什么挑不出招、白等了多久（写进 combat_log 的 gates 字段）。
# 2026-11-22 加：没有计量就只能靠猜"是游戏说不能用，还是我们自己的条件把它滤掉了"。
_GATES = {"no_map": 0, "no_valid": 0, "only_friendly": 0, "filtered": 0,
          "slot_wait": 0, "slot_wait_ms": 0}


def _gate(k, n=1):
    try:
        _GATES[k] = _GATES.get(k, 0) + int(n)
    except Exception:
        pass


def _friendly_fallback(me, mine, vm, allies):
    """没有可用攻击时的兜底友方招（A1）：只在"否则就 pass"时用。

    背景（2026-11-22 日志）：Barristan 的回合里游戏只给了 maa_bolster/maa_defender/maa_command，
    而 candidates() 的友方分支都要满足条件（队友<50% / 压力≥7 / 有人濒死）才触发 —— 全队 90%+
    血时就一个候选都没有，直接 pass 白扔一手。

    这里把"游戏说合法、但条件没触发"的友方招拿来顶：减压类优先（士气/压力是长期资源），
    其次才是其它 buff。目标必须来自游戏的合法性表（vm），不合法就不选。
    """
    best = None
    for sk in mine:
        if sk.startswith("pass_"):
            continue
        c = CAT.get(sk) or {}
        if c.get("friendly") is not True:
            continue
        valid, tg = vm.get(sk, (False, set()))
        if not valid or not tg:
            continue
        if _battle_fails.get((sk, me["guid"]), 0) >= 2:
            continue
        tags = [str(t).lower() for t in (c.get("tags") or [])]
        is_stress = (any(t.startswith("stress_heal") for t in tags)
                     or any(k in sk.lower() for k in ("bolster", "command", "rally", "comfort")))
        cands = [a for a in allies if a.get("guid") in tg]
        if not cands:
            continue
        if is_stress:
            cands.sort(key=lambda a: -(a.get("stress") or 0))
        else:
            cands.sort(key=lambda a: (a.get("hpPct") or 100))
        t = cands[0]
        sc = 100 if is_stress else 50
        if best is None or sc > best[0]:
            best = (sc, sk, t["guid"],
                    "友方兜底%s->%s(压力%s)"
                    % (sk, t.get("name") or t.get("dataId"), t.get("stress") or 0))
    return (best[1], best[2], best[3]) if best else None


def _valid_map(s, me):
    """问游戏：**这一手哪些技能真能用、每个技能能打谁**。

    2026-11-21：mod 新加的 valid_targets，直接读 ActorController 的
    m_SkillTargetEntries（游戏每回合自己算好的合法性表）。
    以前 Python 拿 CSV 射程猜合法性，猜错就被静默拒：
    skill not selected（招本身出不了）/ target invalid（这个目标不行）。
    返回 {skillId: (valid, {targetGuid...})}；拿不到返回 None（退回老行为）。

    2026-11-21 教训：这张表**不能按回合缓存**。游戏的 m_SkillTargetEntries 会被回合序
    重算清掉（BattleTurnOrder.AttemptRollExtraTurn:444 → GetHasValidCombatSkillOfType(
    saveCalculation:false) → ClearValidSkillTargetEntries），"刚问到"和"要发时"可能已经变了。
    实测按回合缓存时仍有一半被拒（event → 状态没变 → 再发被拒）；所以每手都重问。
    """
    try:
        r = act({"type": "valid_targets", "actorGuid": me["guid"]})
    except Exception:
        return None
    if not isinstance(r, dict) or not r.get("ok"):
        return None
    if r.get("entries") == 0:
        # 表是空的（游戏刚清过、还没重算完）→ 这份答案不可信，宁可退回老模型，
        # 也不能把它当成"什么都不能打"从而白 pass 一手。
        return None
    vm = {}
    for x in r.get("skills") or []:
        sid = x.get("skillId")
        if not sid:
            continue
        tg = set()
        for g in x.get("targets") or []:
            try:
                tg.add(int(g))
            except (TypeError, ValueError):
                pass
        vm[sid] = (bool(x.get("valid")), tg)
    return vm or None


def _valid_ok(vm, sk, tgt):
    """vm 里没有这招（道具/位移/非按钮技能）就不管；有就按游戏的答案卡。"""
    if not vm or sk not in vm:
        return True
    valid, tg = vm[sk]
    return bool(valid) and int(tgt) in tg


def _skill_cd(a, sid):
    """该技能还剩几回合冷却（mod state 的 skills[].cd，0=可用）。

    2026-11-24 加：/state 现在带每技能剩余冷却，判据就用它，别再用"背包里有没有这招"猜。
    缺字段（老 mod）或非数字一律按 0=可用，保持旧行为不变。
    """
    for x in a.get("skills") or []:
        if isinstance(x, dict) and x.get("id") == sid:
            try:
                return int(x.get("cd") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _deaths_door_picks(s, me):
    """死门（hp<=0）时"先治"的候选：pass_heal → 自疗；都没有就返回 []（调用方退回 pass）。

    2026-11-23（作者选方案 A）。以前死门无条件发 {"type":"pass"}，那只 END_TURN、不跑技能
    （BattleStateMachine.cs:178），所以 253 次里只回血 30 次（12%）；UI 的 pass 按钮走的是
    IsPassSkill 技能 pass_heal（heal_20pct + stress_damage_2，db/skills_full.json）。
    这里把它当普通技能出（打自己）；拿不到就退回老路，不改变"治不了就别硬打"的口径。

    2026-11-24：pass_heal 有 3 回合冷却，冷却中发出去必被拒（游戏判 INVALID_COOLDOWN，
    ActorControllerBase.cs:612 → action_fail 的 "skill not selected"，实测 2060~2143 ms 白等）。
    132 次死门 pass_heal 里 41 次失败，**41/41 都在同一个人 3 回合内刚成功用过之后**、0 例外。
    所以冷却中直接跳过 pass_heal 走自疗/退回 pass，别再烧那 2 s。
    """
    if PASS_HEAL in skills_of(me):
        # 冷却中就不用它 —— 用了也选不中，还挡住自疗。
        if _skill_cd(me, PASS_HEAL) == 0:
            vm = _valid_map(s, me)
            # pass_heal 不一定在游戏合法性表里（那张表只装 5 个战斗技）；vm 里没有就不卡它。
            if _valid_ok(vm, PASS_HEAL, me.get("guid")):
                return [(PASS_HEAL, me.get("guid"), "死门pass_heal->自己")]
    # 没有 pass_heal / 冷却中 / 表说不行：用 candidates 里已经算好的自疗（why 以"自疗"开头）。
    return [p for p in candidates(s, me) if str(p[2]).startswith("自疗")]


def candidates(s, me):
    allies = [a for a in s["actors"] if a["team"] == me["team"] and a["alive"]]
    # 血 0 ≠ 已经死了：带 Death Armor（死甲）的敌人 HP 归零会进"死亡门"，人还活着、
    # 照样能被打，必须再挨一下（或处决）才会真死 —— Pit Fighter 就带 2 层死甲。
    # 以前这里用 hp>0 过滤，于是 0 血怪永远不被打，战斗永远结束不了
    # （2026-11-20 03:31 combat_arena_city_resist：Pit Fighter 0/41 从第 6 回合站到跑完）。
    # 真死的敌人 alive=false 或直接从 actors 里消失；只剩死亡动画时点空一次就拉黑本回合。
    enemies = [a for a in s["actors"] if a["team"] != me["team"] and a["alive"]
               and "corpse" not in (a.get("dataId") or "").lower()
               and a.get("targetable") is not False]
    if not enemies:
        return []
    load_cat([i for a in s["actors"] for i in skills_of(a)])
    # 只考虑"界面 5 个格子里真的能点"的技能：mod 的 skills 给真映射 + disabled。
    # 拿不到才退回"skills 列表前 5 个"的旧猜法。
    slots = skill_slots(me["guid"])
    if slots:
        mine = [x["skillId"] for x in slots if x.get("skillId") and _slot_usable(x)]
    else:
        mine = equipped_of(me)
    # 游戏自己的合法性表：能出的招、每招能打谁（2026-11-21）
    vm = _valid_map(s, me)
    out = []

    # 攻略 §8.3 硬阈值：瘟疫战地医疗只能对 HP<50% 的目标用（共 3 次、回 25%）。
    # 排序改成"血最少的排最前"：0 血（死门）最急，按一次就可能少一个人。
    # 以前把 0 血排到最后（因为事件接口治不了死门），现在走 UI 点击，死门也能奶。
    hurt = sorted([a for a in allies if a["hpPct"] < 50 and a["guid"] != me["guid"]],
                  key=lambda a: (a["hpPct"], _leaning(a["guid"], me["guid"])))
    if hurt:
        for sk in mine:
            if sk.startswith("pass_"):
                continue  # pass_heal / pass_stress 是自己对自己用的，别拿去治队友
            c = CAT.get(sk)
            if c is not None and c.get("friendly") is not True:
                continue  # catalog 明确说不是友方技能，别往队友身上丢
            # 2026-11-21：优先看 tags（92 个治疗技能的名字里压根没有 heal 关键字）
            tags = [str(t).lower() for t in ((c or {}).get("tags") or [])]
            if "heal" in tags or any(k in sk.lower() for k in HEAL):
                if _battle_fails.get((sk, hurt[0]["guid"]), 0) >= 2:
                    continue          # 见 _battle_fails 的注释：失败 2 次后本场只有 13% 会成功
                out.append((sk, hurt[0]["guid"], f"治疗{sk}->{hurt[0]['name']}({hurt[0]['hpPct']:.0f}%)"))
                break

    # 自我治疗：只奶自己的技能（苦艾酒 = gr_artemisia，target 为空 + heal 标签）。
    # 判据用标签而不是名字：2026-11-21 实测游戏把 absinthe 改名成 gr_artemisia，
    # 名字匹配直接失效，全历史 0 次自疗。
    if me["hpPct"] < 50:
        for sk in mine:
            if sk.startswith("pass_"):
                continue
            c = CAT.get(sk) or {}
            tag_self = (c.get("friendly") is True
                        and "heal" in [str(t).lower() for t in (c.get("tags") or [])]
                        and not (c.get("target") or []))
            if not tag_self and not any(k in sk.lower() for k in SELF_HEAL):
                continue
            if c.get("friendly") is not True:
                continue
            if _battle_fails.get((sk, me["guid"]), 0) >= 2:
                continue
            out.append((sk, me["guid"], f"自疗{sk}->自己({me['hpPct']:.0f}%)"))
            break

    # P5：压力 ≥7 优先用减压技能（老兵的鼓舞/指挥这类，自身+目标各减压）。
    # 这是"立刻减压"里唯一在战斗内能做的事；旅馆的医生服务不是每个旅馆都有。
    stress_hi = [a for a in allies if (a.get("stress") or 0) >= 7]
    if stress_hi:
        stress_hi.sort(key=lambda a: -(a.get("stress") or 0))
        for sk in mine:
            c = CAT.get(sk)
            if c is not None and c.get("friendly") is not True:
                continue
            tags = [str(t).lower() for t in ((c or {}).get("tags") or [])]
            if any(t.startswith("stress_heal") for t in tags) or \
                    any(k in sk.lower() for k in ("bolster", "command", "rally", "comfort", "stress")):
                tgt = stress_hi[0]
                if _battle_fails.get((sk, tgt["guid"]), 0) >= 2:
                    continue          # 同上：2 次之后本场基本不会成功（bolster 连拒 17 次就是这么来的）
                out.append((sk, tgt["guid"],
                            f"减压{sk}->{tgt['name'] or tgt['dataId']}(压力{tgt['stress']:.0f})"))
                break

    # 保命/守护：攻略里老兵是抗线位，守护是他的核心职责。
    # 原来只在队友 ≤32% 才用一次，结果对方往往已经进死门了才挂守护 —— 太晚。
    # 现在只要有人 <50% 就挂给血最少的那位，把它挡下来。
    critical = [a for a in allies if a["hpPct"] < 50 and a["guid"] != me["guid"]]
    critical.sort(key=lambda a: (a["hpPct"], _leaning(a["guid"], me["guid"])))
    if critical:
        for sk in mine:
            if any(k in sk for k in ("defender", "hold_the_line", "fortitude", "sanctuary", "guard")):
                out.append((sk, critical[0]["guid"],
                            f"守护 {sk} -> {critical[0]['name']}({critical[0]['hpPct']:.0f}%)"))
                break

    threats = {e["guid"]: threat_of(e) for e in enemies}

    # 2026-11-21：战斗道具按**效果类型**分档用（实机看过道具的 skill 元数据）：
    #   tags=heal        → 治疗（绷带/药膏/草药）
    #   tags=stress_heal → 减压（laudanum）
    #   friendly 且不是 heal/stress → 防御/自用（ablative_powders 死甲、rag、stimulants、torch）
    #   非 friendly      → 伤害（smoke_bomb / pouch_of_lye / bear_trap…）
    # 用法：有人 <25% 时治疗/防御道具**优先于技能**；其余排在技能之后（技能能出就不浪费道具）。
    urgent_items, item_picks = [], []
    for slot in slots:
        sid = slot.get("skillId") or ""
        idx = slot.get("buttonIndex")
        if not sid or slot.get("disabled") or idx is None or int(idx) < 5:
            continue
        kind = _item_kind(sid)
        if kind == "heal":
            hurt = sorted([a for a in allies if a.get("alive") and (a.get("hpPct") or 0) < 50],
                          key=lambda a: a.get("hpPct") or 100)
            if hurt:
                ent = (sid, hurt[0]["guid"], f"道具{sid}->{hurt[0]['name']}({hurt[0]['hpPct']:.0f}%)")
                (urgent_items if (hurt[0].get("hpPct") or 0) < 25 else item_picks).append(ent)
        elif kind == "stress":
            s_hi = sorted([a for a in allies if a.get("alive") and (a.get("stress") or 0) >= 60],
                          key=lambda a: -(a.get("stress") or 0))
            if s_hi:
                item_picks.append((sid, s_hi[0]["guid"], f"道具{sid}->{s_hi[0]['name']}(压力{s_hi[0]['stress']:.0f})"))
        elif kind == "defense":
            crit = sorted([a for a in allies if a.get("alive") and (a.get("hpPct") or 0) < 35],
                          key=lambda a: a.get("hpPct") or 100)
            if crit:
                ent = (sid, crit[0]["guid"], f"道具{sid}->{crit[0]['name']}({crit[0]['hpPct']:.0f}%)")
                (urgent_items if (crit[0].get("hpPct") or 0) < 20 else item_picks).append(ent)
        elif kind == "attack":
            dmg = _item_damage(sid)
            fin = sorted([e for e in enemies if (e.get("hp") or 99) <= dmg],
                         key=lambda e: e.get("hp") or 99)
            if fin:
                item_picks.append((sid, fin[0]["guid"],
                                   f"道具{sid}->{fin[0]['name']}(补刀 hp{fin[0]['hp']:.0f}≤{dmg})"))
        # utility（火把之类）不用：战斗中浪费一手

    # 被换位后回位（作者口径 2026-11-20）：数据表里这招自带位移、且挪过去能多放 ≥2 种技能，
    # 就先把位置修正过来再接敌。收拾残局时不挪（对面都快死了，挪位是浪费一手）。
    if all((e.get("hp") or 0) <= 6 for e in enemies):
        pass
    else:
        rp = reposition_pick(me, mine)
        if rp:
            sk, dst, gain = rp
            bad, _why = strands_ally(me, sk, allies)
            if bad:
                rp = None            # 自己回位却把队友挤废 → 不值得
        if rp:
            sk, dst, gain = rp
            c = CAT.get(sk) or {}
            tr = [int(x) for x in (c.get("target") or [])]
            tgt = next((e for e in enemies if e["position"] in tr), None) if tr else me
            if tgt is not None:
                gid = tgt["guid"] if tgt is not me else me["guid"]
                out.append((sk, gid, f"回位{sk}->第{dst + 1}位(+{gain})"))
                return out

    scored = []
    vetoed = []          # 会因为自移把队友挤到"一手都放不出"的招式（没有替代时才用）
    # 被位次卡死（一张攻击牌都不合法）→ 用游戏自带的 *_move 挪回去，别白 pass
    stranded = stranded_move(me, mine, allies, vm)
    for sk in mine:
        c = CAT.get(sk) or {}
        if c.get("friendly"):
            continue
        # 2026-11-21：catalog 拿不到时**不能让这招消失**。
        # 日志证据：`挑不出招（游戏表可用 pd_ounce_of_prevention:4, pd_noxious_blast:2）→ pass`
        # ——游戏明确说这两招各有 4/2 个合法目标，但我们因为 CAT 里没有这招就 continue 了，
        # 结果白 pass 一手。现在：能不能打一律以游戏的 vm 为准，catalog 只用来打分。
        launch = [int(x) for x in (c.get("launch") or [])]
        if launch and me["position"] not in launch:
            continue
        # 阵型纪律（2026-11-22）：这招若自带前/后移，先算一遍全队位次 —— 会把队友挤到
        # "一个攻击技能都放不出"就不选（麻风病人被顶到 3 号位就只能 lep_withstand）。
        trapped, trap_why = strands_ally(me, sk, allies)
        if trapped:
            trapped_note = trap_why
        else:
            trapped_note = ""
        tr = [int(x) for x in (c.get("target") or [])]
        for t in enemies:
            game_says_ok = vm is not None and sk in vm
            if game_says_ok:
                # 游戏自己的合法性表：它说能打就是能打（潜行/条件/封锁/相对射程它都算过了）
                if not _valid_ok(vm, sk, t["guid"]):
                    continue
            elif tr and t["position"] not in tr:
                continue
            if skill_is_dot(sk) and is_immune_target(t):
                continue      # 建筑/结构不吃毒：上毒等于白扔一手（作者实机指出）
            nf = _battle_fails.get((sk, t["guid"]), 0)
            if nf >= 4:
                continue          # 本场这招打这个目标已经废了 4 次，别再用
            th, tw = threats[t["guid"]]
            tag = f"威胁{th}" + (f" {tw}" if tw and tw != "-" else "")
            # 2026-11-21：分数里加游戏自己算的预览伤害（真值，不是估的）。
            # 期望值 = (low+high)/2 × (1 + 0.5×crit)；能一击打死额外 +40；打不动（low=0）-20。
            pv = preview(me.get("guid"), sk, t["guid"])
            extra = 0.0
            pvtxt = ""
            if pv and pv[3]:
                lo, hi, cr, _ = pv
                extra = 6.0 * ((lo + hi) / 2.0) * (1.0 + 0.5 * cr)
                if hi >= t["hp"] > 0:
                    extra += 40.0
                elif lo <= 0:
                    extra -= 20.0
                pvtxt = f",pv{lo:.0f}-{hi:.0f}"
            # 降权强度也按数据来：失败 1 次后本场还成功过的只有 15%（28/187），
            # 2 次后 13%（8/63），3 次后 16%（3/19）—— 也就是说失败基本是终局的。
            entry = (score(sk, c, t, enemies, th) + extra - 10 * nf, sk, t["guid"],
                     f"{sk}->{t['name'] or t['dataId']}(hp{t['hp']:.0f},pos{t['position']},{tag}{pvtxt})")
            if trapped_note:
                vetoed.append((entry[0] - 1000.0, sk, t["guid"],
                               entry[3] + f"，阵型代价（{trapped_note}）"))
            else:
                scored.append(entry)
    if not scored and vetoed:
        # 全是"会挤坏阵型"的招 → 只能挑分最高的那个，总比白 pass 一手强（并留证据）
        scored = sorted(vetoed, key=lambda x: -x[0])
        try:
            print("[阵型] 没有不伤阵型的招，退回：%s" % scored[0][1], flush=True)
        except Exception:
            pass
    scored.sort(key=lambda x: -x[0])
    # 集火：按分数取前 MAX_TRIES 个，**允许打同一个目标**。
    # 以前是"强制每个候选都换个目标"，结果 4 个人各打各的、伤害全摊开
    # （04:08 那场 4 敌 94 血，8 回合只死了 2 个）。
    # 但"3 个候选全砸一个够不着的目标"那个老坑还得防：真全同目标时，
    # 把最后一个换成本轮得分最高的另一个目标，留个退路。
    # 候选多备几个：事件通路下一手被拒只花 ~1.2 秒且不消耗回合，多试几组才划算
    # （旧点击路径每次 4~7 秒，所以原来只给 3 个）。由调用方按预算切。
    picked = [(sk, tg, why) for sc, sk, tg, why in scored[:6]]
    if len(picked) >= 2 and len(enemies) >= 2 and len({p[1] for p in picked}) == 1:
        alt = next(((sk, tg, why) for sc, sk, tg, why in scored if tg != picked[0][1]), None)
        if alt:
            picked[-1] = alt
    # 紧急道具（有人 <25% 血 / <20% 濒死时的治疗与死甲）**排在技能前面**：这时候保命比输出重要。
    out.extend(urgent_items)
    if stranded:
        # 被位次卡死：先挪回能打的位次（`*_move`），后面的攻击候选本来也打不出去
        out.append(stranded)
    else:
        out.extend(picked)
    # 其余道具放最后（兜底/收尾）：技能都打不出去时才用。
    out.extend(item_picks)
    # 友方技能（治疗/自我治疗/道具/回位）也过一遍游戏的合法性表：
    # 死掉的队友、射程外的 Ally 都在这里被剔掉，而不是发给游戏让它拒。
    if vm is not None:
        out = [x for x in out if _valid_ok(vm, x[0], x[1])]
    # A1（2026-11-22）：过完合法性表一个候选都不剩时，别直接 pass —— 用条件没触发的友方招顶一手。
    # 必须放在过滤**之后**：否则主流程挑了个非法目标、被滤掉后又变回空，那还是 pass。
    if not out and vm is not None:
        fb = _friendly_fallback(me, mine, vm, allies)
        if fb:
            out.append(fb)
    return out


def _turn_advanced(st, cur, rnd, turn):
    """这一手是不是已经执行完了（B：技能栏空着时别再白等 2 秒）。

    判据：battleState 离开 in_turn_select，或 该谁动 / 回合号 / turn 变了。
    出招成功后技能栏会短暂清空，老代码在这儿固定等 10×0.2s（最多两轮）——
    一场 40 行动的仗就是 ≈80 秒纯空等。
    """
    if not isinstance(st, dict) or not st:
        return False
    b = st.get("battleState")
    if b and str(b) != "in_turn_select":
        return True
    if st.get("mode") and st.get("mode") != "combat":
        return True
    ca = st.get("currentActorGuid")
    if ca is not None and str(ca) != str(cur):
        return True
    if st.get("round") is not None and st.get("round") != rnd:
        return True
    if turn is not None and st.get("turn") is not None and st.get("turn") != turn:
        return True
    return False


def wait_progress(cur, rnd, timeout, turn=None, base_battle=None):
    """等"这一手被游戏接下了"的证据。

    2026-11-21 实测（fail log）：出招后 `currentActorGuid` / `round` / `turn` 可能**全都不变**——
    因为状态机是 同一个人 sameActor=True 地从 in_turn_select 走到 after_turn，
    而 wait_change() 只看那三样，于是把**已经执行成功的**一手判成没推进，
    再拿去走点击兜底 → 点在已经在结算的战斗上 → 记成废招。
    所以这里补上第四样：`battleState`。它离开 in_turn_select 就说明状态机动了。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(POLL)
        try:
            s2 = get("/state")
        except Exception:
            continue
        if s2.get("mode") != "combat":
            return s2
        b = s2.get("battleState")
        if b and base_battle and b != base_battle:
            return s2
        if s2.get("currentActorGuid") != cur or s2.get("round") != rnd:
            return s2
        if turn is not None and s2.get("turn") != turn:
            return s2
    return None


def wait_change(cur, rnd, timeout, turn=None):
    """等 currentActorGuid / round / turn 变化；变了返回新 state，超时返回 None。

    turn 用**出招前**的快照（fire 里传进来）：动作被接受的瞬间 turn 就会涨，
    如果等到动作结束后才拿基线，就会把"已经出招成功"误判成没变化、再补一刀。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(POLL)
        try:
            s2 = get("/state")
        except Exception:
            continue
        if s2.get("mode") != "combat":
            return s2
        if s2.get("currentActorGuid") != cur or s2.get("round") != rnd:
            return s2
        if turn is not None and s2.get("turn") != turn:
            return s2
    return None


def unstick(cur, rnd, turn=None, wait=2.5):
    """冻结自救：把游戏从 `in_turn_perform` 的死锁里挤出来。返回 (新 state 或 None, 用了哪招)。

    2026-11-20 00:2x 实测（`combat_arena_valley_gaunt` + shared_lost_soul）：
    技能都落了（各 3.4s、血量掉 1），随后 state 一路停在 in_turn_perform，
    pass 也不推进 → 12 个动作判冻结 → runner 只能重启游戏，整场白扔。
    这里赌的是"游戏在等玩家把半截操作收掉"（选技能没点目标 / 动画被吞），按顺序试：
      1) 右键：DD2 里取消"已选技能/目标"的标准手势，且不会开暂停菜单；
      2) 再 pass 一次：第一次点常被动画吞掉；
      3) Esc + 如果有暂停菜单就点「返回游戏」：先把任何挡住输入的面板收掉。
    每招之后等 wait_change：只要 (回合/手序/出手人) 变了就算救回来。
    """
    moves = [("取消选择", "cancel"),
             ("再pass", "pass"),
             ("Esc/收暂停", "esc")]
    for name, mv in moves:
        try:
            if mv == "pass":
                act({"type": "pass", "actorGuid": cur})
            elif mv == "cancel":
                cancel_selection()
            elif mv == "esc":
                # 等价于按 Esc（PauseMenu 动作），但走游戏自己的输入动作回调。
                ui_action("PauseMenu")
                # 暂停菜单开出来必须自己点掉，否则整局都按不动
                # （2026-11-20 01:1x 就是这么卡了 40 分钟 —— 暂停菜单在 map 里挂着）。
                for _ in range(8):
                    time.sleep(0.5)
                    try:
                        u = get("/ui") or {}
                    except Exception:
                        continue
                    back = next((c for c in u.get("clickables") or []
                                 if "PauseMenuButton - Return" in (c.get("path") or "")), None)
                    if back is not None:
                        act({"type": "uiClick", "index": back["i"]})
                        time.sleep(0.6)
                        break
            else:
                act(mv)
        except Exception:
            continue
        s2 = wait_change(cur, rnd, wait, turn)
        if s2 is not None:
            return s2, name
    return None, ""


def dd_snapshot(s, guid):
    """死门前后抓一份该英雄的状态快照（只读，写进 combat_log 的 dd_events）。

    2026-11-20 30 条样本里 9 条回血、21 条 0->0，光看血线分不出来原因
    （"本回合挨打"这个假设已经被数据否掉了：挨打 4/17 回、不挨打 5/13 回）。
    要看的是 tokens（死门标记、治疗减益之类）和 stress——这些 /state 里现成有。
    """
    a = next((x for x in ((s or {}).get("actors") or []) if x.get("guid") == guid), None)
    if not a:
        return None
    return {"hp": a.get("hp"), "hpMax": a.get("hpMax"), "stress": a.get("stress"),
            "pos": a.get("position"), "tokens": a.get("tokens")}


def panel_open():
    """技能面板是不是开着（= 上一次 skill 事件把面板打开但没落到目标上）。"""
    try:
        u = get("/ui")
    except Exception:
        return False
    return any("skill_selection_panel" in (x.get("path") or "") for x in u.get("clickables", []))


def ui_fire(target_pos):
    """面板已经开着时，用真·UI 点击出招：点技能按钮 → 点目标。

    事件接口这时候是进不去的（面板抢了输入），但 UI 点击能走通——实测点完
    目标 13→2 血、回合推进。这是 panel_open() 为真时唯一的自救路径。
    """
    try:
        u = get("/ui")
    except Exception:
        return False
    btns = [x for x in u.get("clickables", []) if "combat_skill_button" in (x.get("path") or "")]
    panels = [x for x in u.get("clickables", [])
              if "actor_info_panel" in (x.get("path") or "") and "shared_" in (x.get("path") or "")]
    if not btns or not panels:
        return False
    panels.sort(key=lambda p: p.get("x") or 0)
    idx = min(max(int(target_pos or 0), 0), len(panels) - 1)
    act({"type": "uiClick", "index": btns[0]["i"]})
    time.sleep(0.25)
    act({"type": "uiClick", "index": panels[idx]["i"]})
    return True


def equipped_of(a):
    """兜底：skills 里第一个 *_move / pass_* 之前的那些（最多 5 个）。

    只在 mod 的 {"type":"skills"} 拿不到时才用。真正的映射以 skill_slots() 为准。
    """
    out = []
    for x in skills_of(a):
        if x.endswith("_move") or x.startswith("pass_"):
            break
        out.append(x)
    return out[:5]


def skill_slots(cur):
    """读战斗界面 5 个技能格**真正**绑定的技能（mod 的 skills 动作）。

    mod 只返回 combat_skill_button 那 5 个（不再混进 MoveButton/pass），
    并带 disabled：射程/冷却/次数不符的格子点了也不会出招，直接跳过。
    """
    try:
        r = act({"type": "skills"})
    except Exception:
        return []
    out = [s for s in (r.get("skills") or [])
           if str(s.get("actorGuid")) == str(cur)]
    out.sort(key=lambda s: s.get("buttonIndex") if s.get("buttonIndex") is not None else 0)
    _slot_cache[str(cur)] = out          # 给失败日志取 usesUsed 用，不参与决策
    return out


def _slot_usable(x):
    """这个技能格现在能不能点：只看 mod 给的 disabled。

    以前这里还看过"剩余次数"（usesLeft）——那是我猜的字段，
    用 IronCrown.dll 的元数据反射确认过：`SkillButtonBhv` 上没有 m_Skill 之类的成员，
    那个判断永远是死代码，已删。现在 mod 只上报 `usesUsed`（本场用过几次），
    "次数用尽是不是拒招的原因"还没被数据证实，所以不拿它当闸门。
    """
    return not x.get("disabled")


def _slot_index(sk, slots):
    for s in slots:
        if s.get("skillId") == sk and s.get("buttonIndex") is not None:
            return int(s["buttonIndex"])
    return None


def _selected_now(cur, sk):
    """这一手现在到底"选中"了没有 —— 两个独立信号，任一为真就算选中。

    1) mod 的 skills 动作里，这一格 SkillButtonBhv.Selected 为真；
    2) /state 里这个人的 selectedSkill 等于这一手。
    两个都是点完技能格后 **立刻** 会变的，不用等动画，所以能把"被吞"在 1 秒内判出来。
    """
    try:
        for x in skill_slots(cur):
            if x.get("skillId") == sk and x.get("selected"):
                return True
    except Exception:
        pass
    try:
        st = get("/state")
        for a in st.get("actors") or []:
            if str(a.get("guid")) == str(cur):
                return (a.get("selectedSkill") or "") == sk
    except Exception:
        pass
    return False


def _sel_what(cur):
    """点完技能格之后，游戏认为当前高亮/选中的是哪个技能（None = 一个都没选中）。

    只用于失败日志分诊：`swallowed` 有两种完全不同的病 ——
      a) None      → 点击被吞（回合初锁输入 / 坐标过期），重试机制能救；
      b) 别的技能  → 技能格序号和 UI 对不上，点错了按钮，重试永远点不对。
    """
    try:
        st = get("/state")
        for a in st.get("actors") or []:
            if str(a.get("guid")) == str(cur):
                v = a.get("selectedSkill")
                if v:
                    return v
    except Exception:
        pass
    try:
        for x in skill_slots(cur):
            if x.get("selected"):
                return x.get("skillId")
    except Exception:
        pass
    return None


def _skill_buttons(clicks):
    b = [x for x in clicks if "combat_skill_button" in (x.get("path") or "")]
    b.sort(key=lambda x: x.get("x") or 0)   # 从左到右 = buttonIndex 0..4
    return b


def _panel_name(p):
    return (p.get("path") or "").split("/")[-1]


def _name_score(panel_name, data_id):
    """头像节点名 vs dataId 的匹配分：精确 3、前缀 2、不匹配 0。

    实测头像节点常比 dataId 短：dataId=fanatic_flayer_ignited → 节点名 fanatic_flayer；
    尸体则是 dataId 带 _corpse、节点名不带。所以前缀必须算匹配。
    """
    d = (data_id or "").replace("_corpse", "")
    n = panel_name or ""
    if not d or not n:
        return 0
    if n == d:
        return 3
    if d.startswith(n) or n.startswith(d):
        return 2
    return 0


def _pick_panel(clicks, s, me, tgt):
    """在**点击技能之后**的新快照里找目标头像。

    先按 dataId 名字匹配（精确 > 前缀），再按"站位 = 头像从左到右"对位；
    尸体也占一格（尸体节点的名字会去掉 _corpse 后缀）。
    """
    panels = [x for x in clicks if "actor_info_panel" in (x.get("path") or "")]
    if not panels:
        return None
    tid = tgt.get("dataId") or ""
    if tgt.get("team") == me.get("team"):
        best = None
        for p in panels:
            sc = _name_score(_panel_name(p), tid)
            if sc > (best[0] if best else 0):
                best = (sc, p)
        return best[1] if best else None

    scored = [(_name_score(_panel_name(p), tid), p) for p in panels]
    scored = [x for x in scored if x[0] > 0]
    if len(scored) == 1:
        return scored[0][1]
    if scored:
        scored.sort(key=lambda x: (-x[0], x[1].get("x") or 0))
        return scored[0][1]

    # 名字完全对不上：退回站位顺序（排除友方头像）
    ally_names = {a.get("dataId") or "" for a in s.get("actors", []) if a.get("team") == me.get("team")}
    ep = [p for p in panels if _panel_name(p) not in ally_names]
    ep.sort(key=lambda x: x.get("x") or 0)
    units = sorted([a for a in s.get("actors", []) if a.get("team") != me.get("team")],
                   key=lambda a: a.get("position") or 0)
    for i, a in enumerate(units):
        if a.get("guid") == tgt.get("guid") and i < len(ep):
            return ep[i]
    return None


def ui_attack(s, cur, sk, target_guid, skip_slot=False, slots=None, rnd=None):
    """真·UI 出招：点对应技能格 → 重新读界面 → 点目标头像。

    返回 True = 手势都发出去了（会不会推进由 fire 的 wait_change 判）；
    返回 "swallowed" = 技能格点了但**没选中**（回合初吞第一下 / 格子已过期），
                      一手都没发出去，fire 可以立刻重点，不用等 SKILL_CONFIRM。
    """
    me = next((a for a in s.get("actors", []) if a.get("guid") == cur), None)
    tgt = next((a for a in s.get("actors", []) if a.get("guid") == target_guid), None)
    if not me or not tgt:
        return False
    # 点击现场：不进决策，只给失败日志。fire() 已经 clear 过，这里只补字段。
    _CLICK.update({
        "round": s.get("round"), "turn": s.get("turn"),
        "current_actor": s.get("currentActor"),
        "current_actor_guid": s.get("currentActorGuid"),
        "sel_before": me.get("selectedSkill"), "sel_after": None,
        "clickables": None, "slot_xy": None, "target_xy": None, "clicks": 0,
    })
    try:
        clicks = get("/ui").get("clickables", [])
    except Exception:
        return False
    _CLICK["clickables"] = len(clicks)
    btns = _skill_buttons(clicks)
    if len(btns) < 5:
        # 技能栏是"开关式"渲染：关着的时候点不到技能格 —— 先把它打开。
        try:
            act({"type": "select", "actorGuid": cur})
        except Exception:
            pass
        time.sleep(0.35)
        try:
            clicks = get("/ui").get("clickables", [])
        except Exception:
            return False
        _CLICK["clickables"] = len(clicks)
        btns = _skill_buttons(clicks)
    if len(btns) < 5:
        return False

    if slots is None:
        slots = skill_slots(cur)
    k = _slot_index(sk, slots)
    if k is None:
        eq = equipped_of(me)
        if sk not in eq:
            return False
        k = eq.index(sk)
    if k >= len(btns):
        return False
    slot = next((x for x in slots if x.get("skillId") == sk), None)
    if slot is not None and slot.get("disabled"):
        return False   # 射程/冷却/次数不符，点了也不出招

    # 点技能格：用屏幕坐标注入指针（不走 index —— index 在"选目标"切换时会整体重排）。
    # skip_slot=True：上一轮已经确认选中了，别再点（再点会把选中**取消**掉）。
    if not skip_slot:
        _CLICK["slot_xy"] = [btns[k].get("x"), btns[k].get("y")]
        _CLICK["clicks"] += 1
        ui_point(btns[k]["x"], btns[k]["y"], hold_ms=80)
        # 立刻回读"选中了没有"：选中 = 这一手进去了；没选中 = 被吞了。
        # 以前这里盲睡 0.65 秒、失败了再盲等 SKILL_CONFIRM=4 秒 —— 48% 的废招就烧在这儿。
        t_sel = time.time()
        sel = False
        while time.time() - t_sel < SEL_CONFIRM:
            time.sleep(0.12)
            sel = _selected_now(cur, sk)
            if sel:
                break
        _CLICK["sel_after"] = bool(sel)
        _CLICK["sel_what"] = _sel_what(cur)      # 点了之后游戏到底高亮了谁（None=一个都没选中）
        _CLICK["slot_path"] = btns[k].get("path")
        _CLICK["slot_expect"] = sk
        _CLICK["btns_n"] = len(btns)
        _CLICK["sel_wait_ms"] = int((time.time() - t_sel) * 1000)
        if not sel:
            # 坐标注入没选中：换一条通道再来一次（按元素点，ExecuteEvents）。
            # 两条通道的失败原因不一样（指针注入怕坐标过期，元素点击怕没有 handler），
            # 所以互相当兜底，比同一条路重点两次划算。
            if btns[k].get("i") is not None:
                _CLICK["slot_fallback"] = "uiClick"
                act({"type": "uiClick", "index": btns[k]["i"]})
                t_sel = time.time()
                while time.time() - t_sel < SEL_CONFIRM:
                    time.sleep(0.12)
                    sel = _selected_now(cur, sk)
                    if sel:
                        break
        if not sel:
            return "swallowed"
        time.sleep(0.15)   # 选中后镜头推近，给 UI 一点时间重排
    else:
        time.sleep(0.20)

    # 点完技能后镜头推近、技能栏收起，头像坐标会动。等到坐标连续两次不变再点，
    # 否则点到的是动画中途的位置（实测一半的"点空"就是这么来的）。
    panel = None
    prev = None
    for _ in range(8):
        try:
            clicks2 = get("/ui").get("clickables", [])
        except Exception:
            return False
        p = _pick_panel(clicks2, s, me, tgt)
        if p is not None:
            key = (p.get("x"), p.get("y"))
            if key == prev:
                panel = p
                break
            prev = key
        time.sleep(0.18)
    if panel is None:
        panel = _pick_panel(get("/ui").get("clickables", []), s, me, tgt)
    if panel is None:
        return False
    _CLICK["target_xy"] = [panel.get("x"), panel.get("y")]
    _CLICK["target_panel"] = _panel_name(panel)
    _CLICK["clicks"] += 1
    # 目标这一下改成"按元素点"（mod 的 uiClick 走 ExecuteEvents，不依赖屏幕坐标）。
    # 2026-11-21 数据：废招里 24/34 是"技能已选中 + 目标按坐标点了两次 + 回合不动"，
    # 而技能格（同样走 uiClick）选中率很高 —— 可疑的是坐标点击本身，不是选招。
    _click_target(panel)

    # 道具格（buttonIndex>=5）里的"多目标机关"（烟雾弹 = 敌方 2 个目标）：
    # 点第二个敌人要**只点一次**，不能对同一个目标补第二下（会把它取消掉）。
    if k >= 5:
        time.sleep(0.40)
        try:
            u3 = get("/ui").get("clickables", [])
            enemy_names = {(a.get("dataId") or "").replace("_corpse", "")
                           for a in s.get("actors", []) if a.get("team") != me.get("team")}
            cands = [p for p in u3
                     if "actor_info_panel" in (p.get("path") or "")
                     and _panel_name(p) in enemy_names
                     and p.get("x") != panel.get("x")]
            if cands:
                cands.sort(key=lambda p: abs((p.get("x") or 0) - (panel.get("x") or 0)))
                ui_point(cands[0]["x"], cands[0]["y"], hold_ms=80)
        except Exception:
            pass
        return True

    # 普通技能：补一次同点（接住被动画吞掉的第一下）。
    # 2026-11-21 03:0x 对比：保留补点 30.5% 废招、去掉后 41.9% —— 补点确实在救"第一下被吞"。
    time.sleep(0.28)
    _CLICK["clicks"] += 1
    _click_target(panel)
    return True


def _click_target(panel):
    """点目标：按**路径**点（uiClick pick=完整路径），点不到再退回坐标。

    2026-11-21 09:2x 作者反馈："战斗中不断呼出角色表"。查过 C 键那条路（inn._open_sheet）：
    它只在装备兜底里、且现在按 C 前先验有没有战斗技能栏。剩下的唯一机制是**按序号点** ——
    uiClick 的 `index` 是 `/ui` 快照里的下标，而战斗里技能栏展开/收起、回合切换、tooltip
    都会让这份列表重排；按下标点就等于点"当时那一格序号"，很可能落到英雄头像上，
    而游戏里点自己头像 = 开角色表，表一开技能格全被挡 → 出招全被拒（废招）。
    改成按路径点：路径对不上就点不到（无损），不会再点到别的东西上去。
    """
    path = panel.get("path") or ""
    if path:
        res = act({"type": "uiClick", "pick": path})
        got = (res or {}).get("path")
        _CLICK["target_via"] = "uiClick-path"
        if got and got != path:
            _CLICK["target_path_drift"] = got      # 想点 A 却点到 B —— 值得留证的现场
        if (res or {}).get("ok"):
            return
        _CLICK["target_via"] = "coords-fallback"
    else:
        _CLICK["target_via"] = "coords"
    ui_point(panel["x"], panel["y"], hold_ms=80)


_DBG = []          # 本次出招的尝试轨迹，只给失败诊断用，不参与决策
# 本次出招的"点击现场"：技能格坐标 / 点击前后 selectedSkill / clickable 数 / 轮到谁。
# 只写不读，给 _fail_log 用 —— 有了它才能把"点空 / 被吞 / 游戏拒绝 / 重复点击"分开。
_CLICK = {}
# 战斗心跳：一场仗可能打 5 分钟，期间 runner 的 report.json 完全不动，
# 看门狗（按 120 秒没更新判死）就会把还在正常出招的 runner 杀掉 —— 2026-11-21 01:32 实测。
# 每轮往这个文件写一行时间戳，watchdog.state_clock 把它算进"状态时钟"。
HB_PATH = os.path.join(HERE, "logs", "combat_hb")


def _hb():
    try:
        with open(HB_PATH, "w", encoding="utf-8") as f:
            f.write("%.0f\n" % time.time())
    except OSError:
        pass
FAIL_LOG = os.path.join(HERE, "logs", "action_fail.jsonl")
# 本场累计被拒次数，(技能, 目标guid) → n。回合结束**不清**：
# 回合内的 dead 只防"这一手连点"，防不了"每回合都被拒一次的废招"（bolster 17 次的教训）。
_battle_fails = {}
_slot_cache = {}   # actorGuid → 最近一次 /skills 读到的格子（只给失败日志补充 usesUsed）
_VALID_CACHE = {}  # (actorGuid, round, turn) → {skillId: (valid, {目标guid})}，游戏自己的合法性表
_LAST_ERR = ""     # 上一次 fire() 被游戏拒绝的原因（"" / "skill not selected" / "target invalid"）
_LAST_SKIP = False # 上一次 fire() 根本没发出去（格子灰/栏没绑上），不算废一手
# 2026-11-23：上一次 fire() 撞上「事件选上了、游戏却还停在 in_turn_select 等选择，且仍是我方回合」。
# 数据（action_fail.jsonl 03:42 那一回合）：这种现场里连试 5~6 招全部同样失败，每手白烧 10~13 秒；
# 11-22 起 EVENT=1003 次、UI=1 次，所以这种时候再回退 UI 是纯亏 —— 直接判这回合出不了招、去 pass。
_STUCK_TURN = [False]


def _note(attempt, sent, stage, **extra):
    try:
        rec = {"attempt": attempt, "sent": sent, "stage": stage}
        rec.update(extra)
        _DBG.append(rec)
    except Exception:
        pass


def _fail_log(s, cur, rnd, sk, tgt, why, name, ms):
    """出招失败时落一行，用来分辨四种原因：

    技能用尽 / 目标不可选 / 点击没生效 / 动画期误判。

    只写不读，不影响战斗逻辑；任何异常都吞掉，绝不能因为它卡住回合。
    """
    try:
        actors = s.get("actors") or []
        me = next((a for a in actors if a.get("guid") == cur), {})
        t = next((a for a in actors if a.get("guid") == tgt), {})
        c = CAT.get(sk) or {}
        slot = next((x for x in _slot_cache.get(str(cur), []) if x.get("skillId") == sk), None)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rnd": rnd, "turn": s.get("turn"), "actor": name,
            "skill": sk, "target": tgt,
            "t_name": t.get("name") or t.get("dataId"), "t_dataId": t.get("dataId"),
            "t_hp": t.get("hp"), "t_alive": t.get("alive"), "t_pos": t.get("position"),
            "m_hp": me.get("hp"), "m_pos": me.get("position"),
            # /state 的 skills 是 [{id,equipped}]（mod\StateBuilder.cs:184-204 ActorState 里拼的，
            # 字典在 198-202），必须取 id 再比；
            # 以前直接 `sk in skills` 拿字符串比字典 → 350/350 恒 False，这个字段等于没有。
            "skill_in_state": sk in [x.get("id") for x in (me.get("skills") or [])
                                     if isinstance(x, dict)],
            # 这两个来自 mod 的 /skills（元数据确认过的字段），用来验"是不是次数用尽被拒"
            "uses_used": (slot or {}).get("usesUsed"),
            "slot_disabled": (slot or {}).get("disabled"),
            # 点击现场（ui_attack 里填）：点没点上、选中没有、点了几次、当时轮到谁
            "click": dict(_CLICK),
            "cat": {k: c.get(k) for k in ("cd", "launch", "target", "friendly", "multi")},
            "ms": ms, "why": why, "tries": list(_DBG),
        }
        os.makedirs(os.path.dirname(FAIL_LOG), exist_ok=True)
        # DD2_FAIL_SHOT=1：把"技能选中了但点击没推进"的那一瞬间存成 PNG（最多 6 张/进程），
        # 用来肉眼分诊到底是"点错对象/点了尸体"还是"这手本来就出不去"。
        if os.environ.get("DD2_FAIL_SHOT") and _SHOT["n"] < 6 and (rec["click"] or {}).get("sel_after") is True:
            try:
                import capture_win
                _SHOT["n"] += 1
                fn = os.path.join(HERE, "logs", "fails", "f_%02d_%s.png"
                                  % (_SHOT["n"], time.strftime("%H%M%S")))
                os.makedirs(os.path.dirname(fn), exist_ok=True)
                capture_win.grab(fn)
                rec["shot"] = fn
            except Exception:
                pass
        with open(FAIL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _fire_impl(s, cur, rnd, sk, tgt, why, name, slots=None):
    """原来的 fire()，逻辑一行没动，只多了 _note() 打点。

    2026-11-21：主路径改成 mod 的文字事件（/action skill）。
    依据 IronCrown.dll 反编译：游戏 UI 点技能格=EventSkillSelectionChanged、
    点目标=EventSelectActor(target)。实测 10 手压测 7/7 生效、1.2 秒结算、0 次点击。
    事件失败（target invalid / 没选中）就落回下面的 UI 点击，旧逻辑完全保留。
    DD2_SKILL_UI_ONLY=1 可强制走回纯点击，做对照。
    """
    t0 = time.time()
    # C1（2026-11-23）：事件没吃下时把现场留下来，等真的落到 UI 出招时一并打出来。
    # 只写日志，不参与任何决策（以前 UI 成功只打 "9648ms(UI)"，看不出事件为什么被拒）。
    ev_note = ""
    if not os.environ.get("DD2_SKILL_UI_ONLY"):
        try:
            r = act({"type": "skill", "actorGuid": cur, "skillId": sk, "targetGuid": tgt})
        except Exception as e:
            r = None
            _note(0, None, "event-exc", error=str(e))
            ev_note = "exc:%s" % str(e)[:60]
        if isinstance(r, dict) and r.get("ok"):
            _note(0, True, "event")
            s2 = wait_progress(cur, rnd, EVENT_CONFIRM, s.get("turn"), s.get("battleState"))
            if s2:
                print(f"R{rnd} {name}: {why} {int((time.time() - t0) * 1000)}ms(EVENT)", flush=True)
                return True, s2, int((time.time() - t0) * 1000)
            # 只记录、不改变任何决策：mod 回来时说游戏停在哪个状态。
            # in_turn_perform = 游戏已经在演这一手（是我们判早了，不该再点）；
            # in_turn_select  = 真没执行（那就是别的原因）。
            # 2026-11-21 补：再记一次"这手之后游戏的当前出手者是谁"。
            # targetSet 已经设上(1850)却停在 in_turn_select，最可能的解释是
            # 我们设的这一手已经不是当前 actor 的（回合在我们发之前就过去了），
            # 那样 selection 永远不会被状态机消费，残留还会把下一手卡成 skill not selected。
            after = {}
            try:
                sa = get("/state")
                after = {"curAfter": sa.get("currentActorGuid"),
                         "battleAfter": sa.get("battleState"),
                         "sameActor": str(sa.get("currentActorGuid")) == str(cur)}
            except Exception:
                pass
            _note(0, True, "event-nochange", battleState=r.get("battleState"),
                  targetSet=r.get("targetSet"), selected=r.get("selected"),
                  targetValid=r.get("targetValid"), waited=EVENT_CONFIRM, **after)
            ev_note = ("nochange:bs=%s set=%s sel=%s tv=%s curAfter=%s"
                       % (r.get("battleState"), r.get("targetSet"), r.get("selected"),
                          r.get("targetValid"), after.get("curAfter")))
            # 2026-11-23：游戏说"选上了"却还停在 in_turn_select、且仍是我方回合 → 这一回合
            # 大概率根本不消费选择。打标记，让调用方别再换招白试（每次约 10~13 秒），直接 pass。
            if (r.get("battleState") == "in_turn_select"
                    and r.get("selected") == sk and r.get("targetValid")
                    and after.get("sameActor")):
                # 2026-11-23 补：标记只能在**真走跳过 UI 这条路**时立。DD2_UI_FALLBACK=1 是回滚开关，
                # 以前回滚时也立标记 → 调用方（本文件 `if _STUCK_TURN[0]: ... break` 那段）照样 break
                # → 只回滚了一半（UI 点了，但不再换下一个候选）。现在回滚时既不早退、也不立标记，就是旧路。
                if not os.environ.get("DD2_UI_FALLBACK"):
                    _STUCK_TURN[0] = True
                    _note(0, None, "stuck-turn-skip-ui")
                    return False, None, int((time.time() - t0) * 1000)
        elif isinstance(r, dict):
            _note(0, None, "event-rejected", error=r.get("error"),
                  battleState=r.get("battleState"), selected=r.get("selected"),
                  targetValid=r.get("targetValid"))
            # 把游戏拒绝的**原因**留出来给调用方分支用（2026-11-21）：
            # skill not selected = 这一招此刻根本不能出（冷却/站位/被锁）→ 这回合别再试它；
            # target invalid     = 招选中了、只是这个目标不行 → 换目标就行，别放弃这招。
            global _LAST_ERR
            _LAST_ERR = str(r.get("error") or "")
            ev_note = ("rejected:%s bs=%s sel=%s tv=%s"
                       % (r.get("error"), r.get("battleState"), r.get("selected"),
                          r.get("targetValid")))
    for attempt in (0, 1):
        # 第二次点之前先看游戏是不是已经在演这一手：技能动画本身 2~4 秒，
        # 以前 FAST_CONFIRM=0.9 秒就判"没推动" → 再点一遍 → **重复出招**，
        # 实测这就是 `battleState=in_turn_perform` 死锁的来源（2026-11-20 01:28）。
        if attempt:
            try:
                if in_flight(get("/state")):
                    break
            except Exception:
                pass
        sent = False
        try:
            sent = ui_attack(s, cur, sk, tgt, slots=slots, rnd=rnd)
        except Exception as e:
            print(f"! UI 出招失败 {sk}: {e}", flush=True)
        _note(attempt, sent, "ui")
        if sent == "ignored":
            # 技能选中了、目标面板也对、点了 2.5 秒没推进 → 别再走 4 秒的事件兜底
            # （这些场景事件接口同样被拒，实测 4 次直接 HTTP 500）。
            _note(attempt, True, "target-ignored")
            return False, None, int((time.time() - t0) * 1000)
        if sent == "swallowed":
            # 技能格被吞 = 一手都没发出去，重点一次是安全的，只花 ~1 秒。
            # 但先复核：万一 0.9 秒后才真选中，再点会把选中**取消**掉 → 直接去点目标。
            try:
                if _selected_now(cur, sk):
                    sent = ui_attack(s, cur, sk, tgt, skip_slot=True, slots=slots, rnd=rnd)
                else:
                    continue
            except Exception:
                continue
        if sent:
            s2 = wait_change(cur, rnd, SKILL_CONFIRM, s.get("turn"))
            if s2:
                print(f"R{rnd} {name}: {why} {int((time.time() - t0) * 1000)}ms(UI)"
                      + (f" ev={ev_note}" if ev_note else ""), flush=True)
                return True, s2, int((time.time() - t0) * 1000)
            _note(attempt, sent, "ui-nochange")
            time.sleep(0.2)
            continue

        # 兜底：老的技能事件（有些场景面板没渲染出来时只能靠它）
        try:
            if in_flight(get("/state")):
                break
        except Exception:
            pass
        try:
            r = act({"type": "skill", "actorGuid": cur, "skillId": sk, "targetGuid": tgt})
            if isinstance(r, dict) and r.get("ok") is False:
                _note(attempt, None, "event-rejected", error=r.get("error"))
                return False, None, int((time.time() - t0) * 1000)
        except Exception as e:
            print(f"! skill 请求失败 {sk}: {e}", flush=True)
            _note(attempt, None, "event-exc", error=str(e))
            return False, None, int((time.time() - t0) * 1000)

        s2 = wait_change(cur, rnd, SKILL_CONFIRM, s.get("turn"))
        if s2:
            print(f"R{rnd} {name}: {why} {int((time.time() - t0) * 1000)}ms", flush=True)
            return True, s2, int((time.time() - t0) * 1000)

        # 注意：这里绝对不能发 select 兜底（v4 干过）。EventSelectActor 会把英雄的
        # 技能面板打开，之后技能事件就进不去，面板一直挂在那儿——"战斗卡住"就是这么来的。
        _note(attempt, None, "event-nochange")
        time.sleep(0.25)
    return False, None, int((time.time() - t0) * 1000)


def _is_corpse(s, guid):
    """这个目标是不是尸体（不是活人、也不是死门）。

    2026-11-21：优先用游戏自己的 `targetable`（ActorDataClass.m_IsTargetable），
    名字里带 corpse 只是兜底——名字是猜的，targetable 是游戏给的。
    """
    for a in (s or {}).get("actors") or []:
        if a.get("guid") == guid:
            if a.get("targetable") is False:
                return True
            return "corpse" in ((a.get("dataId") or "") + (a.get("name") or "")).lower()
    return False


def fire(s, cur, rnd, sk, tgt, why, name):
    """发一个技能，返回 (是否推进, 新 state, 耗时 ms)。

    主路径 = UI 点击（稳定）；事件接口留作兜底。
    失败时落一行诊断到 logs/action_fail.jsonl —— 只写不读，不改任何决策。
    """
    if _is_corpse(s, tgt):
        # 尸体不能当目标：04:08 那场 8.1 回合出现过 noxious_blast 打尸体（掉 3 血、白费一手）。
        # 候选层已经过滤，这里是最后一道闸。
        return False, None, 0
    del _DBG[:]
    _CLICK.clear()
    global _LAST_ERR, _LAST_SKIP
    _LAST_ERR = ""      # 每次出招前清空：只让**这一次**的拒绝原因影响重挑
    _LAST_SKIP = False
    _STUCK_TURN[0] = False   # 同上：只让本次的"选择没被消费"影响这回合
    # 出招前先看一眼这一格现在还在不在、是不是灰的（/skills 一次，跟 ui_attack 共用）。
    # 灰格子点了游戏也不收（历史 350 条失败里 17 条是这种），以前要走完"点击 + 等 4 秒 +
    # 事件兜底"≈7 秒才失败；现在直接跳过，只留一行诊断。
    slots = skill_slots(cur)
    slot = next((x for x in slots if x.get("skillId") == sk), None)
    _CLICK.update({
        "slots_n": len(slots),
        "slot_in_ui": slot is not None,
        "slot_disabled": None if slot is None else bool(slot.get("disabled")),
        "uses_used": None if slot is None else slot.get("usesUsed"),
    })
    if slot is not None and slot.get("disabled"):
        _note(None, False, "slot-disabled")
        _fail_log(s, cur, rnd, sk, tgt, why, name, 0)
        _LAST_SKIP = True     # 0ms、没发给游戏：不算"废了一手"，让调用方换下一个候选
        return False, None, 0
    ok, s2, ms = _fire_impl(s, cur, rnd, sk, tgt, why, name, slots=slots)
    if not ok:
        _fail_log(s, cur, rnd, sk, tgt, why, name, ms)
    return ok, s2, ms



def _snap(s):
    out = {}
    for a in s.get("actors", []):
        if a.get("team") == 0:
            out[a["guid"]] = (a.get("name") or a.get("dataId"), a.get("hp"), a.get("stress"))
    return out


def _pos_snap(s):
    """{guid: (名字, 位置)} —— 两边都取，用来抓推拉换位。"""
    out = {}
    for a in s.get("actors", []):
        if a.get("position") is None:
            continue
        out[a.get("guid")] = (a.get("name") or a.get("dataId"), a.get("position"))
    return out


def note_delta(old, new, by, fight):
    """记一笔我方掉血/涨压力/位置变化，by = 当时在行动的人（通常是敌人）。"""
    o, n = _snap(old), _snap(new)
    for guid, (name, hp, stress) in n.items():
        if guid not in o:
            continue
        ohp, ost = o[guid][1] or 0, o[guid][2] or 0
        d = round((ohp or 0) - (hp or 0), 1)
        ds = round((stress or 0) - (ost or 0), 1)
        if d > 0.4 or ds > 0.4:
            fight["dmg_events"].append({"by": by, "to": name, "dmg": d, "stress": ds, "r": fight["rounds"]})
    # 位置变化（2026-11-20 作者口径）：敌人推/拉把我们挤走是随机的，只能靠记录看规律。
    # 这里两边都记：谁从第几位到了第几位、当时是谁在行动（by）。
    op = _pos_snap(old)
    np_ = _pos_snap(new)
    for guid, (name, pos) in np_.items():
        if guid not in op:
            continue
        if op[guid][1] != pos:
            fight.setdefault("pos_events", []).append(
                {"by": by, "who": name, "from": op[guid][1], "to": pos, "r": fight["rounds"]})


def _live_enemies(st):
    # 历史名字，等价于 _enemies_alive（后者是 2026-11-19 加的，带 hp 判断的版本另见 _finishing）。
    return _enemies_alive(st)


def _live_heroes(st):
    return [a for a in st.get("actors", []) if a.get("team") == 0 and a.get("alive")]


RESULTS_SCENE = "combat_results"   # 赢下战斗后才会出现的结算画面（撤退/突围不会经过它）
DUP_WINDOW = 5.0                   # 同一场景在这个秒数内重复落盘 → 视为同一场写了两次
SUPERSEDE_WINDOW = 180.0           # 同一场先记 unknown、之后又判出结果的时间窗（实测 13s）
# 2026-11-23：上一行是"到 max_steps 被截断"的半场（partial）时，下一段的 ts 起点比它晚
# 整整一次调用的时长（数据里 97~180s，max_steps 提到 60 后可能 >200s），所以用更大的窗口。
CONTINUE_WINDOW = 900.0
_last_write = [None, 0.0]          # (scene, epoch)，去重用


def _ts_epoch(ts):
    """'2026-11-19 22:41:13' → epoch；格式不对返回 None。"""
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _merge_partial(prev, rec, lines, idx, gap):
    """续战合并：上一行是 partial（到 max_steps 被截断的半场），把两段拼成一行。

    2026-11-23 依据：`combat_log` 里 101 行 turns>=40，其中 35 行在 10 分钟内同场景又落了一行
    （第二段 outcome：win 15 / unknown 11 / retreat 9）—— 同一场被算成两行，胜率统计被污染。
    以前第二段如果还是 unknown，`_merge_with_last` 直接判 dup 丢掉（06:07:51 那场就是这样）。
    现在：turns/secs 相加、rounds 取 max、actions/事件按顺序拼（各截 80 条）、gates 相加；
    有真结果时用这一段的 outcome（`classify_outcome` 已经算好）。
    """
    add_turns = int(rec.get("turns") or 0)
    if add_turns <= 0:
        return "dup"                     # 一点进展都没有：还是当重复丢掉，别把 secs 越加越大

    def cat(key, cap=80):
        return (list(prev.get(key) or []) + list(rec.get(key) or []))[:cap]

    rec["actions"] = cat("actions")
    rec["dmg_events"] = cat("dmg_events")
    if prev.get("pos_events") or rec.get("pos_events"):
        rec["pos_events"] = cat("pos_events")
    if prev.get("dd_events") or rec.get("dd_events"):
        rec["dd_events"] = cat("dd_events")
    rec["turns"] = int(prev.get("turns") or 0) + add_turns
    rec["rounds"] = max(int(prev.get("rounds") or 0), int(rec.get("rounds") or 0))
    rec["secs"] = round(float(prev.get("secs") or 0) + float(rec.get("secs") or 0), 1)
    if prev.get("hp_before"):
        rec["hp_before"] = prev["hp_before"]
    if prev.get("gold_before") is not None:
        rec["gold_before"] = prev["gold_before"]
    if prev.get("gear") and not rec.get("gear"):
        rec["gear"] = prev["gear"]
    if prev.get("gates"):
        g = dict(prev["gates"])
        for k, v in (rec.get("gates") or {}).items():
            g[k] = g.get(k, 0) + int(v or 0)
        rec["gates"] = g
    rec["merged_from"] = {"ts": prev.get("ts"), "outcome": prev.get("outcome"),
                          "end": prev.get("end"), "partial": True,
                          "turns": prev.get("turns"), "secs": prev.get("secs")}
    if not rec.get("partial"):
        rec.pop("partial", None)         # 这一段真打完了 → 不再标半场
    lines[idx] = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with open(COMBAT_LOG, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        print("! 战斗日志改写失败:", e, flush=True)
        return ""
    print("[战斗] 续战合并上一行 partial（同场景 %.0fs 内，turns %s+%d=%d）"
          % (gap, prev.get("turns"), add_turns, rec["turns"]), flush=True)
    return "merged"


def _merge_with_last(rec):
    """和日志最后一行对比，决定「改写 / 当重复丢掉 / 正常追加」。返回动作字符串。

    三种情况（前两种出自 2026-11-19 那两场冻结/过场，第三种 2026-11-23 加）：
      - merged：上一行同场景 unknown、且是 partial（到 max_steps 被截断）→ 这是同一场接着打，
                两段合成一行（以前第二段是 unknown 就被当 dup 丢掉，一场被算成两行）；
      - merged：上一行同场景 unknown，这次判出了真结果（13s 后复判 win）→ 改写那一行；
      - dup   ：上一行同场景 unknown，这次还是 unknown（冻结每 57s 落一行，一场刷了 6 行）
                → 同一场没进展，直接丢掉；
      - ""    ：没关系，正常追加。
    """
    if not rec.get("scene"):
        return ""
    try:
        with open(COMBAT_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return ""
    try:
        prev = json.loads(lines[idx])
    except ValueError:
        return ""
    if prev.get("scene") != rec.get("scene") or prev.get("outcome") != "unknown":
        return ""
    t0, t1 = _ts_epoch(prev.get("ts")), _ts_epoch(rec.get("ts"))
    if t0 is None or t1 is None:
        return ""
    gap = t1 - t0
    # 续战（2026-11-23）：上一行是半场，且不是冻结 —— 冻结行永远不合并（那是同一场原地打转）
    if prev.get("partial") and not prev.get("stuck"):
        if not 0 <= gap < CONTINUE_WINDOW:
            return ""
        # 2026-11-23 作者选定（方案 A）：回合号倒退 = 这是**新开的一场**，不是续战。
        # scene 名是复用的（28 种盖住 566 行），只靠"同 scene + 900s"会把两场不同的仗粘成一行；
        # 实测 29 个真实续战对 rec.rounds 全部 >= prev.rounds（一场之内最大回合号只增不减）。
        if int(rec.get("rounds") or 0) < int(prev.get("rounds") or 0):
            print("[战斗] 同场景但回合号倒退（%s→%s）→ 判为新一场，不合并"
                  % (prev.get("rounds"), rec.get("rounds")), flush=True)
            return ""
        return _merge_partial(prev, rec, lines, idx, gap)
    if not 0 <= gap < SUPERSEDE_WINDOW:
        return ""
    if rec.get("outcome") in (None, "unknown"):
        return "dup"
    rec["merged_from"] = {"ts": prev.get("ts"), "outcome": prev.get("outcome"),
                          "end": prev.get("end"), "secs": prev.get("secs")}
    lines[idx] = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with open(COMBAT_LOG, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        print("! 战斗日志改写失败:", e, flush=True)
        return ""
    print("[战斗] 合并上一行 unknown（同场景 %.0fs 内复判为 %s）"
          % (t1 - t0, rec["outcome"]), flush=True)
    return "merged"


def mark_last_unknown_win(window=300.0, why="results-scene-late"):
    """进到结算画面（combat_results）时，把紧挨着的 unknown 行补判成 win（只改最后一行）。

    背景（2026-11-21 19:3x 实测，runner 日志同一场连发三次 unknown）：这几场都是打到
    对面剩 1 个残兵，游戏先把 actors 清空 → write_fight 拿到的 final 没 actor、只能用
    last_combat，而那份快照里敌人还活着 → unknown(no-end-marker)；1~2 秒后才进 combat_results。
    _merge_with_last 只在一场**新战斗**写行时才会回头改，于是这些行永远停在 unknown
    （今天 64 行，占当天一半）。
    结算画面能出现就说明这一场打完了：撤退走 retreat 分支，团灭走 wipe，
    都不需要这里。只在"最后一行、outcome=unknown、window 秒内"时改，其它一律不动。
    返回 "fixed" / ""。
    """
    try:
        with open(COMBAT_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return ""
    try:
        rec = json.loads(lines[idx])
    except ValueError:
        return ""
    if rec.get("outcome") != "unknown":
        return ""
    t = _ts_epoch(rec.get("ts"))
    if t is None or time.time() - t > window:
        return ""
    rec["outcome"] = "win"
    rec["end"] = why
    rec["fixed_late"] = {"from": "unknown", "why": why, "after_secs": round(time.time() - t, 1)}
    lines[idx] = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with open(COMBAT_LOG, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError:
        return ""
    print("[战斗] 补判上一行 unknown → win（结算画面晚到 %.0fs，scene=%s）"
          % (time.time() - t, rec.get("scene")), flush=True)
    return "fixed"


def _all_heroes_dead(final):
    """final 里我方 actor 全部 alive=false → 团灭。没有 actor（过场/结算清空）时无法判断，返回 False。"""
    hs = [a for a in (final or {}).get("actors") or [] if a.get("team") == 0]
    return bool(hs) and not any(a.get("alive") for a in hs)


def classify_outcome(fight, final, base):
    """判定这场到底赢没赢（返回 (outcome, 依据)）。

    2026-11-19 之前只有 win/wipe/unknown 三档，实测 20 场里 7 场 unknown（35%），
    其中既有"赢了但快照里敌人还挂着"，也有我们自己发的撤退。现在按证据分：
      我方全灭            → wipe     (heroes-dead)
      敌人全清            → win      (enemies-clear)
      双方都活着 + 我们撤 → retreat  (retreat)   —— 自己退的，不算输也不算看不清
      双方都活着 + 结算画面 → win     (results-scene)
      双方都活着 + 金币涨  → win      (gold+N)    —— 结算画面被跳过的兜底
      其余                → unknown  (no-end-marker)
    """
    if _all_heroes_dead(final):
        return "wipe", "heroes-dead"
    # 撤退必须排在"敌人清"前面：撤退后游戏会把 actors 清空，_live_enemies(base) 为空，
    # 以前这样就判成 win —— 实测 1 回合 2 行动、金币没涨的"胜利"全是撤退。
    if fight.get("retreat") or fight.get("forced_retreat"):
        return "retreat", "retreat"
    if not _live_enemies(base):
        return "win", "enemies-clear"
    if RESULTS_SCENE in ((final or {}).get("scene") or "").lower():
        return "win", "results-scene"
    g0, g1 = fight.get("gold_before"), base.get("gold")
    if isinstance(g0, int) and isinstance(g1, int) and g1 > g0:
        return "win", "gold+%d" % (g1 - g0)
    # 2026-11-21：敌人**全部 0 血**也是赢（放在结算画面/金币之后，前面更硬的证据优先）。
    # 血 0 但 alive=true 的是正在播死亡动画（或死门）：游戏会在动画中途清空 actors，
    # 这时 _live_enemies 仍有 1 个，旧逻辑就判 unknown —— 实测 12:20~12:33 四场全这样，
    # 日志同一时刻写着 "[收尾] 敌人已全部 0 血（yeoman(0.0/42)），等游戏结算"，是稳赢的仗。
    _live = _live_enemies(base)
    if _live and all((e.get("hp") or 0) <= 0 for e in _live):
        return "win", "enemies-down"
    # 注意：**不能**把"actors 被清空"直接判成 win。实测剧情过场也会中途清空 actors
    # （test_fight_outcome 里就有这个用例：先 unknown、13 秒后真结算再合并成一行）。
    # 所以这一档保持 unknown，靠后面的 _merge_with_last 合并。
    return "unknown", "no-end-marker"


def write_fight(fight, final, last_combat):
    # 同一场被写两次（2026-11-19 实测：10:08:49/50 同场景、同金币、同血量各落一行）。
    # 战斗最短也要 10 秒，5 秒内同场景必然是重复，直接跳过。
    _now = time.time()
    if fight.get("scene") and _last_write[0] == fight["scene"] and _now - _last_write[1] < DUP_WINDOW:
        print("[战斗] 跳过重复记录（%.1fs 内同场景 %s 已写过）"
              % (_now - _last_write[1], fight["scene"]), flush=True)
        return

    # 结束时游戏可能已经切到结算/地图、actors 清空，这时用最后一份战斗内的快照
    base = final if (final.get("actors") and _live_heroes(final)) else last_combat
    fight["outcome"], fight["end"] = classify_outcome(fight, final, base)
    # 2026-11-23：partial 的语义是"到 max_steps 被截断、没打完"。max_steps 恰好等于最后一手时
    # （第 60 手收尾、判出 win/wipe）会被 main() 误标 —— 判出真结局就不是半场，摘掉标记，
    # 免得复盘把一场赢了的仗算成"半场"。（main() 标 partial 时还没有 outcome，只能在这里收）
    if fight.get("partial") and fight["outcome"] != "unknown":
        fight.pop("partial", None)
    LAST_FIGHT.clear()
    LAST_FIGHT.update({"outcome": fight["outcome"], "ts": time.time(),
                       "retreat": bool(fight.get("retreat") or fight.get("forced_retreat"))})
    if fight.get("timeout"):
        fight["end"] = (fight.get("end") or "") + "+timeout"
    fight["enemies_left"] = len(_live_enemies(base))

    fight["hp_after"] = {v[0]: v[1] for v in _snap(base).values()} or fight.get("hp_before", {})
    fight["stress_after"] = {v[0]: v[2] for v in _snap(base).values()}
    fight["gold_after"] = base.get("gold")
    fight["actions"] = fight["actions"][:80]
    fight["dmg_events"] = fight["dmg_events"][:80]
    fight["gates"] = {k: v for k, v in _GATES.items() if v}
    how = _merge_with_last(fight)     # 同场 unknown：判出结果就改写，还是 unknown 就丢掉
    if how == "dup":
        print("[战斗] 同场 %.0fs 内还是 unknown（无进展），不再落行  %s"
              % (time.time() - _last_write[1] if _last_write[1] else 0, fight.get("scene")), flush=True)
        return
    if how != "merged":
        try:
            with open(COMBAT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(fight, ensure_ascii=False) + "\n")
        except OSError as e:
            print("! 战斗日志写失败:", e, flush=True)
    _last_write[0], _last_write[1] = fight.get("scene"), time.time()

    hp = "/".join(str(int(v[1])) for v in _snap(base).values())
    print(f"[战斗] {fight['outcome']}({fight.get('end', '?')}) {fight['rounds']}回合 {fight['turns']}行动 "
          f"{fight.get('secs', 0):.1f}s 我方hp={hp}{'  ←合并上一行' if how == 'merged' else ''}", flush=True)

    # —— 撤退对账（2026-11-19）——
    # 已知的翻车模式：对面只剩 1 个残兵 / 对面已经打崩，我方却退出战斗（撤退/突围）。
    # 这里在战斗结束时再查一次，命中就打一条显眼的告警，方便事后从 runner 日志里一眼看到。
    left = _enemies_alive(base)
    if fight["outcome"] == "unknown" and left and not fight.get("partial"):
        # partial = 到 max_steps 被截断、下一轮接着打（2026-11-23），不是"退出战斗"，别刷这条告警。
        if len(left) <= 1 or _finishing(base):
            print("[告警] 这一场没打完就退出了，但对面只剩 %s —— "
                  "不是失误就是误判，查一下 retreat/forced_retreat 字段" % enemy_brief(base),
                  flush=True)


def _enemies_alive(s):
    """活着的、不是尸体的敌人。"""
    return [e for e in s.get("actors", []) if e.get("team") != 0 and e.get("alive")
            and "corpse" not in (e.get("dataId") or "").lower()]


def enemy_brief(s):
    """"yeoman(1/45), immolatist(16/23)" 这种短描述，写日志/告警用。"""
    out = []
    for e in _enemies_alive(s):
        nm = (e.get("dataId") or "?").replace("shared_lost_soul_", "")
        mx = e.get("hpMax") or 0
        out.append("%s(%s/%s)" % (nm, e.get("hp"), int(mx) if mx else "?"))
    return ", ".join(out) or "-"


def _finishing(s):
    """收尾阶段：对面已经打崩了，这时候撤退是纯亏，必须打完。

    判据（任一成立）：
      - 只剩 1 个活着的敌人，且 hp ≤ FINISH_ENEMY_HP（残兵）；
      - 每个活着的敌人 hp 都 ≤ 8；
      - 对面总血 ≤ FINISH_HP_PCT%。
    "敌人全 0 血"不算收尾 —— 那是在等游戏结算，归 main 的僵尸分支管。
    """
    enemies = _enemies_alive(s)
    live_hp = [(e.get("hp") or 0) for e in enemies if (e.get("hp") or 0) > 0]
    if not live_hp:
        return False
    if len(live_hp) == 1 and live_hp[0] <= FINISH_ENEMY_HP:
        return True
    if max(live_hp) <= 8:
        return True
    total = sum(live_hp)
    total_max = sum((e.get("hpMax") or 0) for e in enemies) or 0
    return bool(total_max) and total / total_max * 100.0 <= FINISH_HP_PCT


def _desperate(s):
    """老的"崩盘"判据（只看我方剩几个、对面还剩几个），不含收尾/单敌判断。"""
    heroes = [a for a in s.get("actors", []) if a.get("team") == 0 and a.get("alive")]
    enemies = _enemies_alive(s)
    if not heroes or not enemies:
        return False
    down = sum(1 for a in heroes if (a.get("hp") or 0) <= 0)
    avg = sum(a.get("hpPct") or 0 for a in heroes) / max(1, len(heroes))
    if len(heroes) <= 2:
        return True
    if down >= 2:
        return True
    return avg < 30 and len(enemies) >= 3


def death_door_count(s):
    """我方站在死门上的人数（活着但 0 血）。"""
    return sum(1 for a in (s or {}).get("actors", [])
               if a.get("team") == 0 and a.get("alive") and (a.get("hp") or 0) <= 0)


def _is_boss_scene(s):
    """这一场是不是关底/章节 Boss（那种撤退没意义、只能打完的）。"""
    sc = ((s or {}).get("scene") or "").lower()
    return any(k in sc for k in ("boss", "confession", "mountain"))


# 死门逃生阈值：几个人站死门就撤。
# 2026-11-20 口径是"1 人就撤"，实跑一天（2026-11-21）数据推翻了它：
#   今日 74 场里 37 场撤退，**37/37 都是"死门 1 人"**，能打赢的仗只有 22.4% 废招、
#   撤退场却烧到 44.7%，一局 32 分钟有 20 分钟在撤退-重进。
# 所以默认改成 **2 人死门才撤**（真的快减员了才保人）。
# 想回到旧口径不用改代码：DD2_DD_RETREAT_MIN=1。
DD_RETREAT_MIN = max(1, int(os.environ.get("DD2_DD_RETREAT_MIN", "2") or 2))


def should_retreat(s):
    """按"减员 = 本局失败"的口径决定要不要撤。返回 (要不要撤, 理由)。

    2026-11-20 21:3x 改：**不再看 `_retreat_allowed()` / NO_RETREAT 开关**。
    21:04 那局就是这个开关的代价：R2 就有人死门、判据想撤、被"死战到底"拦下，
    硬打到 3 人全站死门 → 团灭，一局 27 分钟白扔。
    "死战到底"是"不主动放弃一场能赢的仗"，不是"眼看要减员还硬吃"。
    仍然保留两个例外：收尾阶段（对面已打崩）、Boss 战（撤了也没意义）。
    """
    n = death_door_count(s)
    if n <= 0:
        return False, ""
    if _finishing(s):
        return False, "收尾阶段（死门 %d 人也不撤）" % n
    if _is_boss_scene(s):
        return False, "Boss 战不撤（死门 %d 人）" % n
    if n < DD_RETREAT_MIN:
        return False, ""
    return True, "死门 %d 人（≥%d 才撤：掉一个人这局就废了）" % (n, DD_RETREAT_MIN)


def _badly_losing(s):
    """是不是已经打不过了（攻略 §8.6 的撤退判据）。

    2026-11-19 加了两道闸，**先判"不许撤"再判"该撤"**：
      - 只剩 1 个敌人 → 永远不撤（收掉更划算）；
      - 收尾阶段（对面残了）→ 永远不撤（新增；血案就死在这条上）。
    实测撤退（EventBattleRetreat）会结算成「突围」：掉金币/压力、拿不到奖励。
    """
    enemies = _enemies_alive(s)
    if not enemies or len(enemies) <= 1:
        return False
    # 敌人全 0 血 = 正在等游戏结算（死亡动画/僵尸卡死），这时候撤退 100% 是误判。
    if not any((e.get("hp") or 0) > 0 for e in enemies):
        return False
    if _finishing(s):
        return False
    return _desperate(s)


def retreat_veto(s):
    """"本来该撤、但被闸门拦下"的理由；没拦返回 ''。写日志用。"""
    enemies = _enemies_alive(s)
    if not enemies:
        return ""
    if not any((e.get("hp") or 0) > 0 for e in enemies):
        return "敌人全部 0 血（等结算）"
    if not _desperate(s):
        return ""
    if len(enemies) <= 1:
        return "只剩 1 个敌人"
    if _finishing(s):
        return "收尾阶段（对面 %s）" % enemy_brief(s)
    return ""


def try_retreat():
    """触发游戏的撤退事件：EventBattleRetreat.Trigger(bool isDungeon)。

    这个事件是静态可触发的（和 EventBattlePass 一样），所以不用点那个"长按撤退"按钮。
    """
    for flag in ("False", "True"):
        try:
            r = act({"type": "call", "name": "EventBattleRetreat",
                     "method": "Trigger", "args": [flag]})
            if isinstance(r, dict) and r.get("ok"):
                return True
        except Exception:
            pass
    return False


def _close_stray_sheet(s=None):
    """战斗里还挂着角色表就记一笔 + 按 Esc 关掉。

    2026-11-21 09:0x 存疑：查了 `combat_log` × `gear_log` 的时间窗，**0 次**装备尝试落在
    战斗时间窗内（赛前 2 分钟有过装备尝试的 27 场 废招 33.4%，其余 73 场 35.5%）——
    所以"装备兜底在战斗里按 C 开表"这个说法**没有数据支持**，别当结论。
    这里只做两件事：真出现了就记下来（logs/sheet_events.jsonl，带当时谁在出手、可点元素数），
    然后再关掉，让下一轮跑能直接回答"表是谁开的、多频繁"。
    """
    global _LAST_SHEET
    now = time.time()
    if now - _LAST_SHEET[0] < 2.0:      # /ui 一次 8KB，别每个 tick 都读
        return False
    _LAST_SHEET[0] = now
    try:
        u = get("/ui").get("clickables") or []
    except Exception:
        return False
    if not any("CharacterSheet" in (x.get("path") or "") for x in u):
        return False
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "scene": (s or {}).get("scene"), "round": (s or {}).get("round"),
               "turn": (s or {}).get("turn"),
               "actor": (s or {}).get("currentActor"),
               "clickables": len(u),
               "paths": [x.get("path") for x in u if "CharacterSheet" in (x.get("path") or "")][:3]}
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        with open(os.path.join(HERE, "logs", "sheet_events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        ui_action("ExitMenu")
        time.sleep(0.6)
        print("[开战] 角色表挂在战斗画面上，已记录并用 ExitMenu 关掉", flush=True)
    except Exception:
        return False
    return True


_LAST_SHEET = [0.0]


def main(max_steps=150, max_secs=900):
    s = get("/state")
    if s.get("mode") != "combat":
        # 不在战斗里就当空调用，不写日志（避免 runner 在过场时刷一堆空记录）
        print(f"[跳过] mode={s.get('mode')} scene={s.get('scene')}", flush=True)
        return s
    t_start = time.time()
    _PV_CACHE.clear()             # 预览数值随回合变化，每 tick 重问游戏
    _close_stray_sheet()          # 角色表挡住技能格 → 整场都点不出招，先关掉
    _item_used[0] = False
    _affinity[0] = {}
    try:
        _ps = act({"type": "partyState"})
        for _c in _ps.get("connections") or []:
            _pair = _c.get("pair") or []
            if len(_pair) == 2:
                _affinity[0][frozenset((str(_pair[0]), str(_pair[1])))] = int(_c.get("leaning") or 0)
    except Exception:
        pass
    fight = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scene": s.get("scene"),
        "hp_before": {v[0]: v[1] for v in _snap(s).values()},
        "gold_before": s.get("gold"),
        "actions": [],
        "dmg_events": [],
        "pos_events": [],
        "dd_events": [],
        "rounds": 0,
        "turns": 0,
    }
    _dd_round[0] = None
    for _gk in _GATES:          # 每场重新计量（gates 写进 combat_log）
        _GATES[_gk] = 0
    # —— 饰品检测（P2）：开战时读一次"谁身上到底戴了什么"，整场战斗都带着这份快照 ——
    # 以前只有"拖一下就当成装上了"（没有回读），实战里出现过"背包里的饰品没了、英雄身上空着"
    # 的假装备。这份快照用来事后对账：如果背包里还有 trinket_*，就在这里直接告警。
    try:
        _tr = act({"type": "trinkets"})
        if _tr.get("ok"):
            fight["gear"] = {a.get("name") or a.get("dataId"): (a.get("trinkets") or [])
                             for a in (_tr.get("actors") or [])
                             if "corpse" not in ((a.get("dataId") or "").lower())}
            _bagtr = [it.get("itemId") for it in (_tr.get("bag") or [])
                      if "trinket" in (it.get("itemId") or "").lower()]
            if _bagtr:
                fight["bag_trinkets"] = _bagtr
                print("[装备][告警] 开战时背包里还有 %d 件饰品没上身：%s"
                      % (len(_bagtr), _bagtr), flush=True)
    except Exception:
        pass
    last_combat = s
    idle_spins = 0
    _battle_fails.clear()   # 新一场：整场失败计数归零
    dead = {}  # (技能, 目标guid) → 本回合失败次数
    dead_round = [-1]
    turn_tries = {}  # (actor, 回合号, turn) → 这一手已经白试几次（跨循环累计）
    slot_wait = {}   # 同一手「技能栏没绑上」等过几回（每手最多 2 回，等不到就照旧出招）
    retreat_tried = False
    frozen_sig = [None]
    frozen_spins = [0]
    frozen_since = [0.0]     # 这个指纹从什么时候开始没变（按时间判冻结，见 frozen_stage）
    rescue_tried = [False]   # 本场已经试过"解冻三招"（每场只试一次，失败就老实判冻结）
    downed_since = [0.0]    # 敌人第一次"全 0 血"的时刻（僵尸/等结算计时）
    downed_logged = [False]

    # 注意：max_steps 算的是"我方出手次数"，不是循环次数。
    # 敌人回合每 0.15 秒转一圈，以前按循环计数会让一场战斗只打两下就被截断。
    while fight["turns"] < max_steps and time.time() - t_start < max_secs:
        _hb()                     # 战斗内每轮都碰一下心跳文件（看门狗靠它区分"长战斗"和"真冻死"）
        _close_stray_sheet(s)     # 角色表挡住技能格 = 整轮点不出招：记一笔再关掉
        if s.get("mode") != "combat":
            print(f"[结束] mode={s.get('mode')} scene={s.get('scene')}", flush=True)
            break
        # 巢穴/湮灭壁垒打完后会弹「前往下一场战斗？」确认框：mode 还是 combat，
        # 但 actors 已经清空。这时候要立刻交回 runner 去按「前进 / 逃离！」。
        if not s.get("actors"):
            print("[结束] 无 actors（确认框/过场），交回 runner", flush=True)
            break
        # —— 僵尸战斗兜底（2026-11-19 重写）——
        # 旧逻辑：敌人全 0 血、连续 **2 次轮询（≈0.3 秒）** 就发 EventBattleRetreat 强制突围。
        # 代价：15:12 那场只剩 1 个 hp1 的 yeoman，被削到 0 血后 0.3 秒就被判"僵尸"，
        # 结算成「突围」—— 赢定的仗变成撤退，所以这里必须给游戏留够结算时间。
        # 新逻辑：连续"敌人全 0 血"满 STALL_DOWNED_SECS(25s) 才动撤退；2 秒先打一行日志。
        enemies_now = _enemies_alive(s)
        # 0 血也可能是 Death's Door（带死甲的敌人）：人还活着，还轮到我方出招就必须接着打。
        # 以前不看轮到谁，一律当成"等结算"，干等 25 秒后判僵尸突围 —— 赢定的仗变撤退
        # （2026-11-20 03:31 combat_arena_city_resist 的 Pit Fighter 0/41 就是这么被拖没的）。
        _cur0 = s.get("currentActorGuid")
        _me0 = next((a for a in s.get("actors", []) if a.get("guid") == _cur0), None)
        _can_act = bool(_me0) and _me0.get("team") == 0 and not in_flight(s)
        if (enemies_now and all((a.get("hp") or 0) <= 0 for a in enemies_now)
                and not _can_act):
            if not downed_since[0]:
                downed_since[0] = time.time()
                print(f"[收尾] 敌人已全部 0 血（{enemy_brief(s)}），等游戏结算，不再 0.3 秒就突围",
                      flush=True)
                fight.setdefault("downed_waits", []).append(
                    {"r": s.get("round"), "enemies": enemy_brief(s)})
            waited = time.time() - downed_since[0]
            if waited >= STALL_REPORT_SECS and not downed_logged[0]:
                downed_logged[0] = True
                print(f"[收尾] 已等 {waited:.1f}s 仍未结算（{enemy_brief(s)}），继续等", flush=True)
            if waited >= STALL_DOWNED_SECS:
                print(f"[僵尸] 敌人全 0 血 {waited:.0f}s 仍不结算，判定真卡死 → 突围结算", flush=True)
                fight["forced_retreat"] = {"why": "all-enemies-0hp-stall", "secs": round(waited, 1),
                                           "round": s.get("round"), "enemies": enemy_brief(s)}
                if try_retreat():
                    time.sleep(1.5)
                    s = get("/state")
                    break
        else:
            downed_since[0] = 0.0
            downed_logged[0] = False
        # —— 战斗冻结兜底（2026-11-19 23:00 起，2026-11-20 01:28 改成按**时间**）——
        # 「(回合, 当前手序号, 行动者, 双方血量, 敌人数) 一直不变」= 游戏根本不接受输入：
        # 40 秒先试自救三招，120 秒判冻结、交回 runner（别等 max_secs=900 拖 15 分钟）。
        _sig = (s.get("round"), s.get("turn"), s.get("currentActorGuid"),
                tuple(sorted((a.get("guid") or 0, a.get("hp")) for a in s.get("actors") or [])),
                len(_enemies_alive(s)))
        if _sig == frozen_sig[0]:
            frozen_spins[0] += 1
        else:
            frozen_sig[0] = _sig
            frozen_spins[0] = 0
            frozen_since[0] = time.time()
            rescue_tried[0] = False
        idle_secs = time.time() - (frozen_since[0] or time.time())
        stage = frozen_stage(idle_secs, rescue_tried[0])
        # 先自救再判死刑：救回来了就继续打这场（比重启游戏白扔一场强）。
        if stage == "rescue":
            rescue_tried[0] = True
            print(f"[解冻] 画面 {idle_secs:.0f}s 没变（state={s.get('battleState')}，"
                  f"{frozen_spins[0]} 个动作）→ 试自救三招", flush=True)
            s2, how = unstick(s.get("currentActorGuid"), s.get("round") or 0, s.get("turn"))
            if s2 is not None:
                print(f"[解冻] 成功（{how}）→ 继续这场战斗", flush=True)
                fight["unstuck"] = {"by": how, "secs": round(idle_secs, 1),
                                    "round": s.get("round")}
                s = s2
                frozen_spins[0] = 0
                frozen_since[0] = time.time()
                continue
            print("[解冻] 三招都没用，继续等到 120 秒判冻结", flush=True)
        if stage == "frozen":
            print(f"[冻结] 画面 {idle_secs:.0f}s 零变化（{enemy_brief(s)}），"
                  f"state={s.get('battleState')} → 判战斗冻结，交回 runner", flush=True)
            fight["stuck"] = {"why": "no-progress", "secs": round(idle_secs, 1),
                              "spins": frozen_spins[0],
                              "round": s.get("round"), "state": s.get("battleState")}
            break
        cur = s.get("currentActorGuid")
        rnd = s.get("round") or 0
        fight["rounds"] = max(fight["rounds"], rnd)
        # 死门逐回合记（2026-11-20 20:5x）：只在 pass 那一刻记 hp，分不清"回血是延迟到下一回合"
        # 还是"根本没回"。每个新回合给仍停在死门的英雄记一行，之后就能按回合对齐。
        if rnd != _dd_round[0]:
            _dd_round[0] = rnd
            for _a in s.get("actors") or []:
                if _a.get("team") == 0 and (_a.get("hp") or 0) <= 0:
                    dd_record(fight, {"kind": "round", "r": rnd, "who": _a.get("name"),
                                      "before": dd_snapshot(s, _a.get("guid")),
                                      "enemies": len(_enemies_alive(s)),
                                      "note": "回合开始仍在死门"})
        me = next((a for a in s.get("actors", []) if a["guid"] == cur), None)

        # 正在播动作（in_turn_perform / 结算动画）时**别点**：点什么都被吞，还白烧 5 秒。
        # 2026-11-20 01:28 实测：battleState=in_turn_perform 时右键/pass/技能/点敌人四种手势
        # 全都不推进，界面还停在"粉碎已选中"那一屏 —— 干点只会让日志更吵、判断更慢。
        if in_flight(s):
            time.sleep(IDLE_POLL)
            s2 = get("/state")
            note_delta(s, s2, (me.get("name") or me.get("dataId")) if me else "?", fight)
            s = s2
            continue
        cur = s.get("currentActorGuid")
        rnd = s.get("round") or 0
        fight["rounds"] = max(fight["rounds"], rnd)
        me = next((a for a in s.get("actors", []) if a["guid"] == cur), None)

        if not me or me["team"] != 0:
            idle_spins += 1
            if idle_spins > 800:  # ~2 分钟没轮到我们，判卡住
                print(f"[停止] 连着 {idle_spins} 次没轮到我方，mode={s.get('mode')}", flush=True)
                break
            time.sleep(IDLE_POLL)
            s2 = get("/state")
            note_delta(s, s2, (me.get("name") or me.get("dataId")) if me else "?", fight)
            s = s2
            continue
        idle_spins = 0

        fight["turns"] += 1

        # 攻略 §8.6：打不过就撤。撤退保留已得的蜡烛，巢穴/精英尤其值得
        # （作者口径：巢穴三连战打不过第三层，逃离拿前两层奖励，不亏）。
        # 每场只试一次：撤退有失败概率，反复试只会白烧回合。
        # 2026-11-19：撤退前先过 retreat_veto —— "只剩 1 个敌人 / 对面已经打崩" 一律不撤，
        # 而且把"本来该撤、被拦下"的现场写进 combat_log（事后能查是不是又误判了）。
        # —— D2 死门撤退：有人站死门，这一场就不值得赌了（掉人 = 本局报废）。
        # 只在允许撤退的模式下生效（默认死战；watchdog 加 --retreat 才开）。
        dd_ok, dd_why = should_retreat(s)
        if dd_ok and not retreat_tried:
            retreat_tried = True
            why = "%s / 我方 %d 人 vs 对面 %d 个：%s" % (
                dd_why, len(_live_heroes(s)), len(_enemies_alive(s)), enemy_brief(s))
            print(f"[撤退] {dd_why} → 减员即本局失败，撤", flush=True)
            fight["retreat"] = {"r": rnd, "why": why, "forced": False}
            if try_retreat():
                time.sleep(1.5)
                s2 = get("/state")
                if s2.get("mode") != "combat":
                    s = s2
                    break
                s = s2
                continue
        elif NO_RETREAT:
            # 用户口径（2026-11-19 夜）：需要撤退的战斗也硬打。只记一次"本来该撤"的现场，方便事后对账。
            if not retreat_tried and _badly_losing(s):
                retreat_tried = True
                fight["retreat_disabled"] = {"r": rnd, "enemies": enemy_brief(s),
                                             "heroes": {v[0]: v[1] for v in _snap(s).values()}}
                print(f"[撤退][禁用] 默认死战到底 —— 本来该撤，硬打（{enemy_brief(s)}）", flush=True)
        elif not retreat_tried:
            veto = retreat_veto(s)
            if veto:
                retreat_tried = True
                fight.setdefault("retreat_veto", []).append(
                    {"r": rnd, "why": veto, "enemies": enemy_brief(s),
                     "heroes": {v[0]: v[1] for v in _snap(s).values()}})
                print(f"[撤退][否决] {veto} —— 不撤，把这一场打完", flush=True)
            elif _badly_losing(s):
                retreat_tried = True
                down = sum(1 for a in s.get("actors", []) if a.get("team") == 0
                           and a.get("alive") and (a.get("hp") or 0) <= 0)
                why = "死门 %d 人 / 我方 %d 人 vs 对面 %d 个：%s" % (
                    down, len(_live_heroes(s)), len(_enemies_alive(s)), enemy_brief(s))
                print(f"[撤退] 局面崩了（{why}），尝试撤退", flush=True)
                fight["retreat"] = {"r": rnd, "why": why, "forced": False}
                if try_retreat():
                    time.sleep(1.5)
                    s2 = get("/state")
                    if s2.get("mode") != "combat":
                        s = s2
                        break
                    s = s2
                    continue

        # 注意：不要再用"技能面板开着就自救"那套。skill_selection_panel 在整场战斗里
        # 一直都在（技能栏就挂在这个节点下），所以 panel_open() 永远为真，那个分支会
        # 每回合抢着点第一个技能格 + 随便一个目标，等于白扔一回合。
        # 现在 fire() 自己就是 UI 点击（点对应技能格 → 点目标），不需要这个兜底。
        # 刚轮到我方那一瞬，游戏会吞掉第一个技能事件（实测 7/7 都是第一次无效），
        # 先让它稳一下，比"连发 4 个技能试出哪个能被吃"快得多。
        time.sleep(TURN_SETTLE)
        # 重试预算按"这个人的这一手"（actor + 回合号 + turn 序号）记账。
        # 以前只在"循环一圈"内计数：pass 没推进时 while 会回到同一个 actor，预算每次归零
        # → 同一个废招连点 3~5 次，一次 4~11 秒（2026-11-20 01:24 实测 103 秒/回合、
        # 10 次 ok:false 之后还剩 4 个敌人）。现在这一手废够两次就闭嘴，交给 pass/自救。
        # 死门（0 血）：**先治，治不了才 pass**（2026-11-23 作者选方案 A）。
        # 以前这里无条件 pass —— 但 {"type":"pass"} 只 END_TURN、不跑技能（BattleStateMachine.cs:178），
        # "跳过行动回血"其实是 pass_heal 技能（UI 的 pass 按钮 = IsPassSkill 技能，
        # SkillSelectionBhv.cs:269-323）；实测 253 次死门 pass 只回血 30 次（12%），
        # 23:14 那场 7 手全 0->0 之后团灭。所以先出 pass_heal / 自疗，出不了才退回 pass。
        dd_heals = None
        if (me.get("hp") or 0) <= 0:
            dd_heals = _deaths_door_picks(s, me)
        if (me.get("hp") or 0) <= 0 and not dd_heals:
            hp0 = me.get("hp") or 0
            dd_before = dd_snapshot(s, cur)
            act({"type": "pass", "actorGuid": cur})
            s2 = wait_change(cur, rnd, 2.0, s.get("turn"))
            tag = ""
            if s2 is None:
                # pass 没被接受：出手人 / 回合 / 手序一个都没变。
                # 2026-11-20 19:3x 实测（R3 Paracelsus）：同一回合连打 3 次 pass，全是 0->0，
                # 每次白烧 2 秒还占着回合。这不是"没轮到他"，是 pass 被吞了。
                # 先走一次冻结自救（右键取消 → 再pass → Esc），救不回来就把这手记成废手。
                s2u, mv = unstick(cur, rnd, s.get("turn"))
                why = "死门pass未接受 %d（%s）" % (hp0, mv or "自救也没推进")
                fight["actions"].append({"r": rnd, "actor": me["name"], "why": why,
                                         "ms": 0, "ok": False})
                print("R%d %s: %s" % (rnd, me["name"], why), flush=True)
                if s2u is None:
                    s = get("/state")
                    ev = {"r": rnd, "who": me["name"], "accepted": False,
                          "before": dd_before, "after": dd_snapshot(s, cur),
                          "enemies": len(_enemies_alive(s)), "note": why}
                    fight.setdefault("dd_events", []).append(ev)
                    dd_record(fight, ev)
                    continue
                s2 = s2u
                tag = "（pass被吞，靠%s推进）" % mv
            s2 = s2 or get("/state")
            if s2.get("actors"):
                note_delta(s, s2, me["name"], fight)
                me2 = next((a for a in s2["actors"] if a.get("guid") == cur), None)
                hp1 = (me2 or {}).get("hp") or 0
                fight["actions"].append({"r": rnd, "actor": me["name"],
                                         "why": f"死门pass回血 {hp0:.0f}->{hp1:.0f}{tag}", "ms": 0,
                                         "ok": True, "accepted": True})
                ev = {"r": rnd, "who": me["name"], "accepted": True,
                      "before": dd_before, "after": dd_snapshot(s2, cur),
                      "enemies": len(_enemies_alive(s2)), "note": tag}
                fight.setdefault("dd_events", []).append(ev)
                dd_record(fight, ev)
                print(f"R{rnd} {me['name']}: 死门 → pass 回血 {hp0:.0f}->{hp1:.0f}{tag}", flush=True)
                s = s2
                continue

        tkey = (cur, rnd, s.get("turn"))
        if len(turn_tries) > 400:
            turn_tries.clear()   # 别让长战斗把它撑爆（每手一个 key）
        if turn_tries.get(tkey, 0) >= MAX_FAILS_BEFORE_PASS:
            print(f"R{rnd} {me['name']}: 这一手已废 {turn_tries[tkey]} 次，不再点 → pass", flush=True)
            picks = []
        else:
            picks = dd_heals if dd_heals else candidates(s, me)
        # 死门之外：正常出招（UI 点击这条路是有效的）。
        if not picks:
            # 为什么会"一手都挑不出来"必须留证据：是游戏表说全不可用，还是根本没拿到表。
            # 现在 pass 从"被拒几次后放弃"变成了"根本挑不出候选"，不查清就是换一种白扔。
            vm_dbg = _valid_map(s, me)
            if vm_dbg is None:
                _gate("no_map")
                print(f"R{rnd} {me['name']}: 挑不出招（拿不到游戏合法性表）→ pass", flush=True)
            else:
                oks = [k for k, v in vm_dbg.items() if v[0]]
                if not oks:
                    _gate("no_valid")
                elif all((CAT.get(k) or {}).get("friendly") is True for k in oks):
                    _gate("only_friendly")
                else:
                    _gate("filtered")
                ok_n = ",".join(f"{k}:{len(vm_dbg[k][1])}" for k in oks)
                print(f"R{rnd} {me['name']}: 挑不出招（游戏表可用 {ok_n or '无'}，"
                      f"敌人 {enemy_brief(s)}）→ pass", flush=True)
            act({"type": "pass", "actorGuid": cur})
            s2 = wait_change(cur, rnd, 1.0, s.get("turn")) or get("/state")
            if s2.get("actors"):
                note_delta(s, s2, me["name"], fight)
            s = s2
            continue

        # 技能栏还没绑上时别出招：/skills 返回空 = 游戏还在切人/播动画，
        # 这一瞬发事件、点格子都会被静默拒。11-21 新 DLL 四场实测 47 条 "skill not selected"，
        # 其中 31 条就是在这个窗口发的（slots_n=0），全是白废；stress_skill.py 因为先等
        # 技能格非空才发，所以压测测不到这一类 —— 这正是「压测 4/4、正式 57%」的来源。
        # 只等不等死：同一手最多等 2 回（各约 2 秒），还空就照旧往下出招（保持老行为兜底）。
        if not skill_slots(cur) and slot_wait.get(tkey, 0) < 2:
            # B（2026-11-22）：先问"这手是不是已经执行完了"。出招成功后技能栏必然短暂清空，
            # 老代码在这儿固定等 10×0.2s（最多两轮）；40 行动的仗 ≈80 秒纯空等。
            # 状态机既然动了（battleState 离开 in_turn_select / 换人 / 回合号变），
            # 就直接回外层重读，不再等技能栏。
            t_w = time.time()
            advanced = False
            for _ in range(2):                  # 0.4 秒足够覆盖 UI 换人的那一瞬
                time.sleep(0.2)
                if skill_slots(cur):
                    break
                try:
                    s_now = get("/state")
                except Exception:
                    s_now = None
                if _turn_advanced(s_now, cur, rnd, s.get("turn")):
                    s, advanced = s_now, True
                    break
            _gate("slot_wait_ms", int((time.time() - t_w) * 1000))
            if advanced:
                continue                        # 这手已经执行 → 回外层，别等技能栏
            if not skill_slots(cur):
                slot_wait[tkey] = slot_wait.get(tkey, 0) + 1
                _gate("slot_wait")
                print(f"R{rnd} {me['name']}: 技能栏还没绑上，等完这一回再挑", flush=True)
                time.sleep(0.2)
                s = get("/state")
                continue
            # 技能栏刚绑上：之前那批候选是按"拿不到格子"的兜底（equipped_of）算的，
            # 里面可能有根本点不了的招 → 用现在的状态重挑一次。
            picks = dd_heals if dd_heals else candidates(s, me)

        moved = False
        tried = 0
        # 黑名单只在一个回合内有效：上一轮点空过不代表这一轮还打不了
        # （敌人位移、技能冷却、动画结束都会让同一个组合重新变得可用）。
        # 以前是"整场战斗拉黑"，结果就是"不打那个濒死的怪了"。
        if rnd != dead_round[0]:
            dead_round[0] = rnd
            dead.clear()
        # 自己站在死门上时，多半只能出 pass，试一次就够了，别把整个回合烧在无效出招上
        # 2026-11-21：事件通路失败**不消耗回合**（被拒时 state 还是 in_turn_select），
        # 一次探测只花 ~1.2 秒；可预算还按旧点击路径（4~7 秒/次）定成 2，
        # 结果实测 33~43% 的我方回合是"试两下不行就 pass"白扔的
        # （10:57 21 回合→pass 9、11:01 24→10、11:12 26→10、11:17 18→6）。
        # 所以事件模式下把预算放宽到 6，并且按拒绝原因分支重挑，而不是傻试同一个组合。
        event_mode = not os.environ.get("DD2_SKILL_UI_ONLY")
        if (me.get("hp") or 0) <= 0:
            # 死门：有治就先试（最多两招：pass_heal → 自疗），连治都没有才只试一次
            no_move_budget = min(2, len(dd_heals)) if dd_heals else 1
        else:
            no_move_budget = 6 if event_mode else MAX_FAILS_BEFORE_PASS
        bad_skill_turn = set()      # 这一回合明确的"不能用"的技能（skill not selected = 冷却/站位/被锁）
        tgt_fresh = None
        for sk, tgt, why in picks[:max(no_move_budget, MAX_TRIES)]:
            if dead.get((sk, tgt), 0) >= 2 or sk in bad_skill_turn:
                continue  # 这个回合已经废了两次 / 这招这回合确定不能用，别再浪费
            # 每手都重问一次游戏的合法性表：候选是几十毫秒前挑的，
            # 中间只要游戏重算过回合序，这张表就可能已经变了（上一版按回合缓存的坑）。
            vm_now = _valid_map(s, me)
            if vm_now is not None and not _valid_ok(vm_now, sk, tgt):
                continue      # 这组已经过时，换下一个候选
            if tgt_fresh is not None:
                # 重试用的是**上一次失败后的旧快照**：目标可能已经死了/挪位了，
                # 拿旧快照接着试 = 必被拒（失败日志里同一目标连试 2~3 次就是这么来的）。
                a = next((x for x in (tgt_fresh.get("actors") or []) if x.get("guid") == tgt), None)
                if a is None or not a.get("alive") or (a.get("hp") or 0) <= 0:
                    dead[(sk, tgt)] = 2
                    continue
            ok, s2, ms = fire(s, cur, rnd, sk, tgt, why, me["name"])
            if _LAST_SKIP:
                # 格子灰了 / 技能栏还没绑上：这一手**根本没发给游戏**（0ms）。
                # 不算"废了一手"（不计入战果、不吃尝试预算），直接换下一个候选。
                continue
            fight["actions"].append({"r": rnd, "actor": me["name"], "why": why, "ms": ms, "ok": ok})
            if ok:
                note_delta(s, s2, "反击/DoT", fight)
                s, moved = s2, True
                break
            if _LAST_ERR == "skill not selected":
                bad_skill_turn.add(sk)      # 招本身出不了，换目标也没用
            # 残血目标（能收人头）不要拉黑：多试几次也值得。
            # 0 血目标正相反：要么是 Death's Door（该打，但点空一次就说明这回合点不上），
            # 要么只是死亡动画（再点还是空）—— 都只给一次机会，别把整回合烧在它身上。
            tgt_hp = next((a.get("hp") or 0 for a in s.get("actors", [])
                           if a.get("guid") == tgt), 99)
            if tgt_hp <= 0:
                dead[(sk, tgt)] = 2
            elif tgt_hp > 6:
                dead[(sk, tgt)] = dead.get((sk, tgt), 0) + 1
            _battle_fails[(sk, tgt)] = _battle_fails.get((sk, tgt), 0) + 1
            tried += 1
            turn_tries[tkey] = turn_tries.get(tkey, 0) + 1
            if _STUCK_TURN[0]:
                # 2026-11-23：这回合游戏不消费选择（见 _STUCK_TURN 注释）。换招也一样，
                # 别把 5~6 手 × 10~13 秒烧在同一回合里，直接交给下面的 pass 收尾。
                print(f"R{rnd} {me['name']}: 选择没被消费（in_turn_select 同回合）→ 不再换招，pass", flush=True)
                break
            if tried >= no_move_budget:
                break  # 试不动，多半这回合本来就不让出招，别再一个个白试
            try:
                s3 = get("/state")
                if s3 and s3.get("actors"):
                    tgt_fresh = s3
                    if s3.get("currentActorGuid") != cur:
                        break       # 回合已经过去了，收手（否则下一手是别人的回合）
            except Exception:
                pass

        if not moved:
            # pass 才是"这回合放弃行动"的正确接口：死门、被控、技能被锁时只有它能推进
            act({"type": "pass", "actorGuid": cur})
            time.sleep(0.2)
            s2 = wait_change(cur, rnd, PASS_CONFIRM, s.get("turn"))
            if s2:
                fight["actions"].append({"r": rnd, "actor": me["name"], "why": "出招被拒→pass", "ms": 0, "ok": True})
                print(f"R{rnd} {me['name']}: 出招被拒，pass 推进", flush=True)
                note_delta(s, s2, "反击/DoT", fight)
                s = s2
            else:
                print(f"R{rnd} {me['name']}: pass 也没推进", flush=True)
                s = get("/state")

        if s.get("mode") == "combat":
            last_combat = s

    fight["secs"] = round(time.time() - t_start, 1)
    # 2026-11-23：到 max_steps 上限退出 = 这一场**没打完**（不是结局）。打上 partial，
    # 下一次 C.main 接着打时由 _merge_partial 把两段合成一行（以前第二段会被当成 dup 丢掉，
    # 于是 11-23 的 8 场 unknown 里有 7 场 turns 正好=40、敌人还剩 1~3 个）。
    if fight["turns"] >= max_steps:
        fight["partial"] = True
    # 240 秒上限（2026-11-19 改成 900）：实测 9 回合的战斗打到 241.9s 被切断，
    # 于是同一场被记成两行（第一行 end=no-end-marker）。真被切断时留个明确标记。
    if time.time() - t_start >= max_secs:
        fight["timeout"] = True
    if not fight["actions"]:
        # 过场/切场景时 mode 会闪一下 combat，这种"进去啥也没干就出来"的记录没有价值
        return last_combat
    write_fight(fight, s, last_combat)
    # 冻结了就把"死锁"这个事实带回去：runner 下一个 tick 立刻报 stuck，
    # 不用再等 2~3 个 tick（每个 tick 都可能再耗 120 秒才判一次冻结）。
    if fight.get("stuck") and isinstance(last_combat, dict):
        last_combat["_frozen"] = dict(fight["stuck"])
    return last_combat


if __name__ == "__main__":
    st = main()
    print("MODE:", st.get("mode"), "scene:", st.get("scene"), "gold:", st.get("gold"))
    for a in st.get("actors", []):
        print(f"  {a['team']} {a['name'] or a['dataId']} pos={a['position']} hp={a['hp']}/{a['hpMax']} alive={a['alive']}")


