"""gamedata.py —— 从游戏本体导出的怪物/技能数据（不再靠 dataId 关键字猜）。

数据来源：StreamingAssets\\Excel\\*_data_export.Group.csv → 游戏数据-怪物技能.json
    python _gamedata_build.py     # 重新导出（游戏更新后跑一次）

对外只有两个函数：
    info(data_id)   → {"hp","res","skills"} 或 None
    threat(data_id) → (分数 0~THREAT_CAP, 理由)
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "游戏数据-怪物技能.json")
_CACHE = {}


def _load():
    if _CACHE:
        return _CACHE
    try:
        with open(_PATH, encoding="utf-8") as f:
            _CACHE.update(json.load(f))
    except Exception as e:  # noqa: BLE001
        print("gamedata 读不到（先用关键字兜底）：%s" % e)
    return _CACHE


def info(data_id):
    return _load().get((data_id or "").lower())


def threat(data_id, cap=40):
    """按技能表算威胁：伤害上限 / 群攻 / 上压力 / DoT / 多段 / 暴击。

    这套分是拿数据算的，不再看技能名里有没有 "mortar"/"stress" 这类词。
    拿不到的怪返回 (None, "")，调用方决定怎么兜底。
    """
    rec = info(data_id)
    if not rec:
        return None, ""
    skills = rec.get("skills") or []
    if not skills:
        return None, ""
    dmg = max((float(s.get("dmg") or 0) for s in skills), default=0)
    aoe = any(len(s.get("target") or []) >= 3 for s in skills)
    multi = any(s.get("multi") for s in skills)
    crit = max((float(s.get("crit") or 0) for s in skills), default=0)
    fx = " ".join(",".join(s.get("target_effects") or []) for s in skills)
    stress = "stress_damage" in fx
    dot = "skill_dot" in fx
    move = "move_" in fx or "immobilize" in fx
    sc = dmg * 2.0
    why = []
    if dmg:
        why.append("打%d" % int(dmg))
    if aoe:
        sc += 6
        why.append("群攻")
    if stress:
        sc += 5
        why.append("上压力")
    if dot:
        sc += 4
        why.append("DoT")
    if move:
        sc += 2
        why.append("位移")
    if multi:
        sc += 2
        why.append("多段")
    if crit >= 0.5:
        sc += 3
        why.append("高暴击")
    return int(max(0, min(cap, sc))), "/".join(why) or "-"


def resist(data_id):
    rec = info(data_id)
    return (rec or {}).get("res") or {}
