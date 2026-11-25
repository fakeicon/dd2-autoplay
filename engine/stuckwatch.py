"""stuckwatch.py —— 卡画面检测：只问"世界变了吗"，不问"你发了什么动作"。

为什么单独做一个：
runner 原来用 `(mode, scene, row, gold, action)` 连续相同来判卡住。问题是
**action 一变化就清零**——在马车上来回按 w、在客栈反复点按钮、拖饰品拖不动时，
每 tick 的 `action` 字符串都不一样，counter 永远是 0，于是"发呆半天"永远不报。

这里换成一个便宜的指纹：世界状态里挑几个会变的字段，变了就算有进展。
再给每种画面一个预算，超了就判卡住。动作本身不参与判断。

指纹按画面类型取：
    combat : round/turn/当前行动者/battleState/双方血量/存活数
    inn    : 金币/补给/队伍血与压力
    map    : 行号/金币/补给/队伍血与压力
    其它   : 场景本身

用法：
    w = Watch()
    elapsed, stuck = w.feed(now, mode, state, moving=speed > 0.05)
"""

# 每种画面允许"世界不动"多久（秒）。数字来自实测，不是拍的：
#   战斗 60s ：`fights/fight_*.jsonl` 里相邻事件间隔 n=1461，中位 1.0s、p99 11.9s、**max 39.3s**
#              → 取 1.5× 观测最大值。
#   地图 120s：`fights/rec_*.jsonl` 排除战斗段后 n=152，p99 19.7s、**max 81.1s** → 取 1.5×。
#   客栈 90s ：**没有测到**——recorder 不记客栈内部事件，这个数是暂定值，等下次挂机补。
BUDGETS = {
    "combat": 60.0,
    "inn": 90.0,
    "map": 120.0,
    "other": 90.0,
}


def kind(mode, scene):
    """把 (mode, scene) 归成四类之一。"""
    m = (mode or "").lower()
    sc = (scene or "").lower()
    if m == "combat":
        return "combat"
    if "inn" in sc:
        return "inn"
    if m == "map":
        return "map"
    return "other"


def _num(x, d=0.0):
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return d


def party_sum(s):
    """(活人数, 血总和, 压力总和)。"""
    alive, hp, st = 0, 0.0, 0.0
    for a in (s or {}).get("actors") or []:
        if a.get("team") != 0:
            continue
        if a.get("alive"):
            alive += 1
        hp += _num(a.get("hp"))
        st += _num(a.get("stress"))
    return alive, round(hp, 1), round(st, 1)


def foes(s):
    """(活敌数, 敌血总和)。"""
    n, hp = 0, 0.0
    for a in (s or {}).get("actors") or []:
        if a.get("team") == 0:
            continue
        if a.get("alive"):
            n += 1
        hp += _num(a.get("hp"))
    return n, round(hp, 1)


def items_sig(s):
    items = (s or {}).get("items") or []
    return tuple(sorted((str(i.get("id")), i.get("qty")) for i in items))


def fingerprint(mode, s):
    """世界指纹：相等 = 这一 tick 什么都没变。"""
    s = s or {}
    k = kind(mode, s.get("scene"))
    base = (k, s.get("scene"), s.get("mode"))
    if k == "combat":
        return base + (s.get("round"), s.get("turn"), s.get("currentActorGuid"),
                       s.get("battleState"), party_sum(s), foes(s))
    if k == "inn":
        return base + (s.get("gold"), items_sig(s), party_sum(s))
    if k == "map":
        return base + (s.get("mapRow"), s.get("gold"), items_sig(s), party_sum(s))
    return base


class Watch:
    """有状态的小计时器：指纹一变就清零，超预算就报卡。"""

    def __init__(self, budgets=None):
        self.budgets = dict(BUDGETS)
        if budgets:
            self.budgets.update(budgets)
        self._fp = None
        self._since = None

    def budget(self, mode, scene):
        return self.budgets.get(kind(mode, scene), self.budgets["other"])

    def feed(self, now, mode, s, moving=False):
        """喂一帧，返回 (静默秒数, 是否判卡)。

        moving=True（马车正在跑）时无条件清零：跑图本来就不改上面那些字段。
        """
        fp = fingerprint(mode, s)
        if fp != self._fp or self._since is None:
            self._fp, self._since = fp, now
            return 0.0, False
        if moving:
            self._since = now
            return 0.0, False
        el = now - self._since
        return el, el >= self.budget(mode, (s or {}).get("scene"))

    def reset(self):
        self._fp = None
        self._since = None
