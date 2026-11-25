"""trinkets.py —— 饰品估值（采购决策用）。

数据源（两条都来自游戏本体，不编）：
  1. data/trinket_stats.json    —— 191 件，stats 字段是 db/rules.json 里 buff 表的数值
  2. db/trinkets_live.json      —— 196 件，mod 的 trinketInfo 读的游戏内本地化名字+描述
     （2026-11-22 08:1x 补：之前 68 件没数值、172 件没描述，只能靠这份补）

口径：**数值优先**。数值缺失的饰品不参与打分（宁可不买，也不按文案猜）。
权重 pts/百分点，明写在 WEIGHTS 里；改它就能改采购倾向。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STATS = os.path.join(HERE, "data", "trinket_stats.json")
LIVE = os.path.join(HERE, "db", "trinkets_live.json")

WEIGHTS = {
    "resistance_ignore": 1.0,          # 穿透
    "health_damage_dealt_percent": 1.0,
    "crit_chance": 1.0,
    "speed": 0.8,
    "health_max": 0.1,
    "health_heal_received_percent": 0.5,
    "health_heal_dealt_percent": 0.4,
    "resistance": 0.15,
    "health_damage": 2.0,
    "health_damage_received_percent": -1.0,
    "dot_effect_value_dealt_change": 0.15,
    "dot_extra_duration_dealt": 0.5,
    "dot_effect_value_received_change": -0.15,
    "dot_extra_duration_received": -0.5,
    "dot_effect_value_received_multiplier": -0.5,
    "health_damage_range": 0.5,
}

FLAT = ("health_max", "health_damage")     # 这两项按点数算，不乘 100

LABEL = {
    "resistance_ignore": "穿透", "health_damage_dealt_percent": "伤害%", "crit_chance": "暴击",
    "speed": "速度", "health_max": "生命上限", "health_heal_received_percent": "受疗%",
    "health_heal_dealt_percent": "治疗%", "resistance": "抗性", "health_damage": "伤害",
    "health_damage_received_percent": "受伤%", "dot_effect_value_dealt_change": "DOT伤害",
    "dot_extra_duration_dealt": "DOT多跳", "dot_effect_value_received_change": "受DOT",
    "dot_extra_duration_received": "受DOT多跳", "dot_effect_value_received_multiplier": "受DOT倍率",
    "health_damage_range": "伤害浮动",
}

_cache = {}


def _stats():
    if "stats" not in _cache:
        _cache["stats"] = json.load(open(STATS, encoding="utf-8"))["trinkets"]
    return _cache["stats"]


def _live():
    if "live" not in _cache:
        try:
            _cache["live"] = json.load(open(LIVE, encoding="utf-8"))
        except Exception:
            _cache["live"] = {}
    return _cache["live"]


def title(tid):
    t = _live().get(tid) or {}
    return t.get("title") or ((_stats().get(tid) or {}).get("name_cn") or tid)


def desc(tid):
    return ((_live().get(tid) or {}).get("desc") or "").strip()


def rarity(tid):
    return ((_stats().get(tid) or {}).get("rarity") or "")


def hero_of(tid):
    """专属饰品属于谁：trinket_hero_lep_xxx → 'lep'；通用返回 None。"""
    if not (tid or "").startswith("trinket_hero_"):
        return None
    return tid[len("trinket_hero_"):].split("_", 1)[0]


def score(tid):
    """→ (净分, 正分, 负分, [(pts, '属性 值'), ...])；没有可用数值时净分是 None。"""
    t = _stats().get(tid)
    if not t:
        return None, 0.0, 0.0, []
    pts = []
    for st in t.get("stats") or []:
        k = st.get("stat")
        if k not in WEIGHTS:
            continue
        try:
            v = float(st.get("value"))
        except (TypeError, ValueError):
            continue
        mult = 1.0 if k in FLAT else 100.0
        p = WEIGHTS[k] * v * mult
        op = st.get("op") or ""
        sub = ""
        if op.startswith(("sub:", "add:")) or op == "mul":
            sub = "·" + op.replace("sub:", "").replace("add:", "")
        pts.append((p, "%s%s %+g" % (LABEL.get(k, k), sub, v)))
    if not pts:
        return None, 0.0, 0.0, []
    pts.sort(key=lambda x: -abs(x[0]))
    good = sum(p for p, _ in pts if p > 0)
    bad = sum(p for p, _ in pts if p < 0)
    return good + bad, good, bad, pts


# 采购闸门：净分 ≥ MIN_SCORE 且副作用不超过 BAD_LIMIT 才买（这轮改动前挂机根本不买饰品）
MIN_SCORE = 40.0
BAD_LIMIT = 15.0


def worth_buying(tid, party_data_ids=()):
    """→ (bool, 原因)。专职饰品只给本人买。"""
    h = hero_of(tid)
    if h and h not in set(party_data_ids or ()):
        return False, "专属(%s)不在队伍" % h
    net, good, bad, _ = score(tid)
    if net is None:
        return False, "无可用数值（不猜）"
    if net < MIN_SCORE:
        return False, "净分 %.0f < %.0f" % (net, MIN_SCORE)
    if bad < -BAD_LIMIT:
        return False, "副作用 %.0f 过重" % bad
    return True, "净分 %.0f（+%.0f/-%.0f）" % (net, good, bad)
