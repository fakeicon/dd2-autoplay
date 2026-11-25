"""loadout.py —— 战斗道具（装卸）+ 道途（Path）选择。

2026-11-21 新加。全部走 mod 的文字接口，不点鼠标：
  partyInfo  → 队伍成员（含 status / path / combatItems / combatItemSlots）
  bag        → 背包里的 itemId
  equipTrinket kind=combat → 把战斗道具塞进那个人的战斗道具栏（mod 里 SwapItems 到 m_CombatInventory）
  paths / setPath         → 读可用道途 / 改道途（ActorInstance.SetActorPath）

战斗道具每个英雄只有 1 格（实测 combatItemSlots=1），所以是"谁空就给谁一件"，
优先级：治疗/解毒类 > 其他；战斗中怎么用见 combat3.candidates() 的道具分支。
"""
import json
import os
import re
import time

import combat3

HERE = os.path.dirname(os.path.abspath(__file__))
PATH_PREF = os.path.join(HERE, "db", "paths.json")   # 可选：{"dataId": ["首选pathId", "次选"]}
# 可选：{"dataId": ["技能1", ..., "技能5"]}。**没写到的 dataId 一律不动**（不猜）。
# 依据：2026-11-23 A1 侦察发现每人 8 个"已装"＝5 战斗技 + move + pass_heal + pass_stress，
# 另外 6 个未装；所以这里只点名那 5 个战斗技，`setSkills` 的 `now` 里带 move/pass 是正常的。
SKILL_PREF = os.path.join(HERE, "db", "skills.loadout.json")

_SKILL = None
_SKILL_FULL = None        # db/skills_full.json 缓存（变体 → base id 的离线依据）
_ITEM = None
_path_tried = {}      # guid → 上次尝试时间（避免每 tick 都改道途）
_path_done = set()    # 已经确认过的 guid
_path_pending = [False]   # 选人那一刻没来得及设道途 → 进地图后强制补一次
_path_last = [0.0]        # pending 补设的节流（秒），防止每 tick 刷 setPath
_skill_done = {}          # dataId → 已确认装上的 5 个技能
_skill_last = [0.0]       # setSkills 节流（秒），失败后别每 tick 刷
_skill_bad = {}           # dataId → 这一局已经证明设不上的 want（不再重试）
_skill_fail = {}          # dataId → 连续失败次数（到 SKILL_MAX_FAIL 就进 _skill_bad）
SKILL_COOLDOWN = 60.0
SKILL_BACKOFF = 300.0     # 失败后退避（别每 60 秒刷一条同样的失败）
SKILL_MAX_FAIL = 3        # 同一 want 连失败这么多次就不再试（setSkills 会先全卸再装，反复调=反复折腾）


def _act(o):
    try:
        return combat3.act(o) or {}
    except Exception:
        return {}


def party():
    """当前队伍（status == party）。拿不到就返回空。"""
    r = _act({"type": "partyInfo"})
    out = []
    for p in r.get("party") or []:
        if str(p.get("status") or "").strip().lower() == "party":
            out.append(p)
    return out


def bag_ids():
    r = _act({"type": "bag"})
    return [b.get("itemId") for b in (r.get("bag") or []) if b.get("itemId")]


def _skills():
    global _SKILL
    if _SKILL is None:
        try:
            _SKILL = {x["id"]: x for x in json.load(open(os.path.join(HERE, "db", "skills.json"),
                                                         encoding="utf-8"))}
        except Exception:
            _SKILL = {}
    return _SKILL


def _items():
    global _ITEM
    if _ITEM is None:
        try:
            _ITEM = {x["id"]: x for x in json.load(open(os.path.join(HERE, "db", "items.json"),
                                                        encoding="utf-8"))}
        except Exception:
            _ITEM = {}
    return _ITEM


def _is_combat_item(item_id):
    it = _items().get(item_id) or {}
    return it.get("type") == "combat" or it.get("combat") is True


def _item_rank(item_id):
    """越小越优先：治疗/解毒类先装（这些在战斗里能救命）。"""
    sk = _skills().get(item_id) or {}
    tags = str(sk.get("tags") or "").lower()
    friendly = sk.get("friendly")
    if friendly and "heal" in tags:
        return 0
    if any(k in item_id.lower() for k in ("bandage", "antivenom", "salve", "clotting", "tonic")):
        return 0
    if friendly:
        return 1
    return 2


def equip_combat_items():
    """给空着战斗道具栏的队伍成员装一件。返回做过的事（给日志）。

    2026-11-21：按"身上道具最少的人优先"装，而不是按队伍顺序——
    否则前面的人把背包里的道具全吃掉，最后一个（常常是奶妈）一件都没有。
    """
    ps = party()
    if not ps:
        return []
    bag = [i for i in bag_ids() if _is_combat_item(i)]
    if not bag:
        return []
    bag.sort(key=_item_rank)
    did = []
    # 两轮：第一轮每人补 1 件（保证人人有），第二轮再往多格的人身上填。
    for round_no in (0, 1):
        ps_sorted = sorted(ps, key=lambda h: len(h.get("combatItems") or []))
        filled = {int(h["guid"]): len(h.get("combatItems") or []) for h in ps
                  if str(h.get("guid") or "").isdigit()}
        for h in ps_sorted:
            try:
                guid = int(h.get("guid"))
            except (TypeError, ValueError):
                continue
            slots = int(h.get("combatItemSlots") or 0)
            used = filled.get(guid, 0)
            if slots <= 0 or used >= slots:
                continue
            if round_no == 1 and used == 0:
                continue          # 第一轮已经补过 0 件的人，说明背包没了
            if not bag:
                break
            item = bag.pop(0)
            r = _act({"type": "equipTrinket", "actorGuid": guid, "itemId": item,
                      "slot": used, "kind": "combat"})
            ok = bool(r.get("ok")) and item in (r.get("equipped") or [])
            if ok:
                filled[guid] = used + 1
            did.append({"hero": h.get("name"), "item": item, "ok": ok, "err": r.get("error")})
        if not bag:
            break
    return did


# ---------- 道途 ----------

ROLE = {
    "grave_robber": "dps", "highwayman": "dps", "hellion": "dps", "leper": "dps",
    "flagellant": "dps", "bounty_hunter": "dps", "runaway": "dps",
    "plague_doctor": "heal", "vestal": "heal", "occultist": "heal", "jester": "support",
    "man_at_arms": "tank",
}

KEYWORDS = {
    "dps": ("伤害", "攻击", "暴击", "致命", "击杀", "伤害加成"),
    "heal": ("治疗", "回复", "生命", "恢复"),
    "tank": ("格挡", "护甲", "减伤", "守护", "保护"),
    "support": ("压力", "减压", "位移", "增益", "队友"),
}

# db/paths.json 的偏好值多少分（只在有 replaces 数据时用）：平手时偏好赢，
# 但换掉招明显更强/更弱的道途能盖过它（一个技能分差 = 1 点，够拉开）。
PREF_BONUS = 0.75

_TAG = re.compile(r"<[^>]+>")


def _plain(s):
    return _TAG.sub("", str(s or "")).replace("\n", " ").strip()


def _prefs():
    try:
        with open(PATH_PREF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_coach_item(item_id):
    it = _items().get(item_id) or {}
    return it.get("type") == "stage_coach_upgrade"


def coach_slots():
    r = _act({"type": "coachSlots"}) or {}
    return r.get("slots") or []


def install_coach_upgrades():
    """把背包里的马车装备装到马车上（商店买 → 这里装，等价于"进马车匠界面装"）。

    2026-11-21：mod 的 installCoach 走游戏自己的 StageCoach.GetSlotInventory(slot).SwapItems(...)，
    槽位由物品定义里的 m_slot 决定（GENERAL/TROPHY/PET/FLAME），不用我们挑槽。
    """
    bag = [i for i in bag_ids() if _is_coach_item(i)]
    if not bag:
        return []
    did = []
    for item in bag:
        r = _act({"type": "installCoach", "itemId": item})
        ok = bool(r.get("ok")) and item in (r.get("equipped") or [])
        did.append({"item": item, "slot": r.get("slot"), "ok": ok, "err": r.get("error")})
    return did


def _dmg(sid):
    try:
        return float((_skills().get(sid) or {}).get("damage") or 0)
    except (TypeError, ValueError):
        return 0.0


def _sk_score(sid, role):
    """单个技能对某个定位的"价值分"（只用 db/skills.json 里真有的字段：damage / tags）。"""
    sk = _skills().get(sid) or {}
    dmg = _dmg(sid)
    tags = str(sk.get("tags") or "").lower()
    if role == "heal":
        # 治疗位：伤害只值一点零头。2026-11-21 20:0x 实测踩过坑：伤害权重 0.2 时，
        # 瘟疫医生的"炼金术士"（全是伤害替换）被算成 +2.4，压过了治疗向的"内科医生"。
        return dmg * 0.05 + (3.0 if "heal" in tags else 0.0) + (1.0 if "stress_heal" in tags else 0.0)
    if role == "tank":
        if any(k in tags for k in ("guard", "block", "protect", "taunt", "riposte", "counter")):
            return dmg + 2.0
        return dmg * 0.4
    if role == "support":
        return dmg * 0.2 + (2.0 if ("stress_heal" in tags or "heal" in tags) else 0.0)
    return dmg          # dps


def _path_score(path, role):
    """按"这条道途实际把哪些招换成了什么"打分：Σ(新招分 − 旧招分)。

    数据来自 mod 的 paths 动作（`replaces: [{from,to}]`，读的是游戏里的
    ActorDataSkillReplacement）。没有这份数据时返回 None，调用方回退到描述关键词。
    """
    reps = path.get("replaces") or []
    if not reps:
        return None
    sc = 0.0
    for r in reps:
        sc += _sk_score(r.get("to"), role) - _sk_score(r.get("from"), role)
    return sc


def pick_path(data_id, paths, current):
    """挑一个道途 id。

    顺序：db/paths.json 显式偏好 → 按**实际技能替换**打分 → 按描述关键词（老办法，兜底）。
    """
    ids = [p.get("id") for p in (paths or []) if p.get("id")]
    pref = [p for p in (_prefs().get(data_id) or []) if p in ids]
    role = ROLE.get(data_id)
    if not role:
        return pref[0] if pref else current

    # 1) 实际技能替换打分（mod 2026-11-21 19:5x 起提供 replaces）
    #    paths.json 里的偏好转成 +PREF_BONUS：明显更强的道途能盖过偏好，平手时还是偏好赢。
    scored = []
    for p in paths or []:
        sc = _path_score(p, role) if p.get("id") else None
        if sc is not None:
            scored.append((p["id"], sc + (PREF_BONUS if p["id"] in pref else 0.0)))
    if scored:
        base = next((s for pid, s in scored if pid == current), None)
        if base is None:
            base = PREF_BONUS if current in pref else 0.0
        best, best_sc = current, base
        for pid, sc in scored:
            if pid == current:
                continue
            if sc > best_sc + 1e-9:
                best, best_sc = pid, sc
        return best

    # 2) 没有替换数据：先偏好，再描述关键词（老办法）
    if pref:
        return pref[0]
    words = KEYWORDS.get(role, ())
    best, best_sc = current, -1
    for p in paths or []:
        if not p.get("id") or p.get("id") == current:
            continue
        name = _plain(p.get("name"))
        desc = _plain(p.get("desc"))
        sc = sum(desc.count(w) * 2 for w in words) + sum(name.count(w) for w in words)
        if sc > best_sc:
            best, best_sc = p["id"], sc
    return best if best_sc > 0 else current


def apply_paths(cooldown=600.0, dry_run=False, force=False):
    """给队伍成员换到更合适的道途。每个 guid 有冷却，避免反复改。

    force=True 时无视冷却/已确认（选人那一刻用）；另外 `_path_pending` 会让
    下一次调用自动按 force 跑一遍 —— 因为 hero_select 刚确认时 ActorInstance 可能还没建好，
    setPath 会失败，得等进入地图再补一次。
    """
    ps = party()
    if not ps:
        return []
    now = time.time()
    if not force and _path_pending[0] and now - _path_last[0] > 15:
        force = True
        _path_last[0] = now
    did = []
    left = 0
    for h in ps:
        try:
            guid = int(h.get("guid"))
        except (TypeError, ValueError):
            continue
        if not force and (guid in _path_done or now - _path_tried.get(guid, 0) < cooldown):
            continue
        cur = h.get("path")
        r = _act({"type": "paths", "actorGuid": guid})
        paths = r.get("paths") or []
        if not paths:
            _path_tried[guid] = now
            left += 1
            continue
        want = pick_path(h.get("dataId"), paths, cur)
        if want == cur:
            _path_done.add(guid)
            continue
        _path_tried[guid] = now
        if dry_run:
            did.append({"hero": h.get("name"), "was": cur, "want": want, "dry": True})
            continue
        s = _act({"type": "setPath", "actorGuid": guid, "pathId": want})
        ok = bool(s.get("ok")) and s.get("now") == want
        if ok:
            _path_done.add(guid)
        else:
            left += 1
        did.append({"hero": h.get("name"), "was": cur, "want": want, "ok": ok,
                    "err": s.get("error")})
    if _path_pending[0] and not dry_run and left == 0:
        _path_pending[0] = False       # 全队都到位了，别再每 tick 强刷
    return did


def _skill_prefs():
    """db/skills.loadout.json：{"<dataId>": ["技能1".."技能5"]}；没写到的一律不动（不猜）。"""
    try:
        d = json.load(open(SKILL_PREF, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for k, v in (d or {}).items():
        if k.startswith("_") or not isinstance(v, list) or len(v) != 5:
            continue
        if all(isinstance(x, str) and x for x in v) and len(set(v)) == 5:
            out[str(k).lower()] = v
    return out


def _skill_full():
    global _SKILL_FULL
    if _SKILL_FULL is None:
        try:
            with open(os.path.join(HERE, "db", "skills_full.json"), encoding="utf-8") as f:
                _SKILL_FULL = json.load(f)
        except (OSError, ValueError):
            _SKILL_FULL = {}
    return _SKILL_FULL


def base_skill_id(sid):
    """变体技能 id → 游戏要的 base id（离线数据，不按 id 字样猜）。

    依据：`ActorInstance.SetBaseCombatSkillIdsEquipped` 遍历 m_CombatSkills，只匹配
    `GetCombatBaseSkillId(instance)`；而它对"替换型"技能返回被替换的那个技能
    （`GetCombatReplacedSkill`），装备时用的是**变体实例自己的 SkillId**。
    这条替换关系在导出的 `db/skills_full.json` 里有：变体的 tags 就带着被替换的技能 id
    （`gr_thrown_dagger_p1` 的 tags 含 `gr_thrown_dagger`、`gr_absinthe_p1` 含 `gr_artemisia`）。
    """
    tags = ((_skill_full().get(sid) or {}).get("tags") or [])
    for t in tags:
        if isinstance(t, str) and t != sid and t in _skill_full():
            return t
    return sid


def _hero_skills(guid):
    """问 mod 这个英雄这局的技能表（heroSkills，只读）。拿不到就返回 {}。"""
    r = _act({"type": "heroSkills", "actorGuid": guid})
    return r if isinstance(r, dict) and r.get("ok") else {}


def resolve_want(want, info=None):
    """把配置里的 want（可能带 `_p1/_p2` 变体）换成游戏认的 base id。

    有 `info`（mod 的 heroSkills：unlocked/equipped/locked/baseOf）时以**游戏自己报的**为准；
    没有（老 DLL / 调用失败）就退回离线数据 `db/skills_full.json` 的 tags 反查。
    有 info 且 want 里有的技能这局没有（锁定/没有）→ 用**现在已装的** base 补满，
    绝不发一个"更短的列表"（setSkills 会先全卸，短列表 = 技能栏缩水）。
    返回 (send_ids, missing)。
    """
    base_of = (info or {}).get("baseOf") or {}
    want = list(want or [])
    known = set()
    if info:
        for key in ("unlocked", "equipped"):
            for x in info.get(key) or []:
                known.add(base_of.get(x, x))
    send, missing = [], []
    for w in want:
        b = base_of.get(w) or base_skill_id(w)
        if info and b not in known:
            missing.append(w)
            continue
        if b not in send:
            send.append(b)
    if info and missing and len(send) < len(want):
        for x in info.get("equipped") or []:
            b = base_of.get(x, x)
            if b not in send:
                send.append(b)
            if len(send) >= len(want):
                break
    return send, missing


def apply_skills(dry_run=False, force=False):
    """按 db/skills.loadout.json 把队伍的战斗技能换成点名的 5 个（mod 的 setSkills）。

    不猜：没配的 dataId 一个都不动。成功过的记进 `_skill_done`，别每 tick 刷。
    判定用"send ⊆ now"（`now` 是 mod 报的 base id；`*_move`/`pass_*` 不在 base 列表里）。
    返回 [{hero, dataId, want, sent, ok, was, now, missing, restored, err}]

    2026-11-23 两次实机教训：
    1) `SetBaseCombatSkillIdsEquipped` 只认 **base id**，而配置里存的是变体 id（`_p1/_p2`）→
       变体全被丢弃、技能栏被截断（Audrey 5→2）。所以先把 want 解析成 base id（`resolve_want`）。
    2) mod 的 setSkills 是"先全卸再装"，万一还是失败，**立刻用 was 回滚**，同一 want 连失败 3 次后
       这一局不再重试（免得反复卸/装）。
    """
    prefs = _skill_prefs()
    if not prefs:
        return []
    now_t = time.time()
    if not force and now_t - _skill_last[0] < SKILL_COOLDOWN:
        return []
    _skill_last[0] = now_t
    out = []
    failed = False
    for h in party():
        did = (h.get("dataId") or "").lower()
        want = prefs.get(did)
        if not want or _skill_done.get(did) == want:
            continue
        if not force and _skill_bad.get(did) == want:
            continue            # 这一局已经证明设不上：别再"先卸再装"折腾技能栏
        if dry_run:
            out.append({"hero": h.get("name"), "dataId": did, "want": want, "dry": True})
            continue
        try:
            g = int(h.get("guid"))
        except (TypeError, ValueError):
            continue
        info = _hero_skills(g)
        send, missing = resolve_want(want, info)
        # 2026-11-23 08:5x：**已经装对了就别再"全卸再装"一遍**。
        # mod 的 setSkills 是先全卸再装，而 `_skill_done` 只活在进程里 → 每起一次 runner 都会重新走这里，
        # 没有短路就会 4 个人各卸一遍再装回来（**推断，依据是代码路径；没实机验"起 runner 会发 4 次"**）。
        # 口径：`equipped` 报的是**变体实例**（`gr_thrown_dagger_p1`），send 是 base id
        # → 必须先过 `baseOf` 再比，直接比字符串会永远不等（这就是变体坑的另一面）。
        # force=True（选人/补设）时照发，不短路。
        if info and not force:
            eq = [x for x in (info.get("equipped") or []) if isinstance(x, str)]
            bmap = info.get("baseOf") or {}
            eq_base = {(bmap.get(x) or x) for x in eq}
            if eq and len(eq) == len(send) and set(send) <= eq_base:
                _skill_done[did] = want
                out.append({"hero": h.get("name"), "dataId": did, "want": want, "sent": send,
                            "ok": True, "skip": "already", "now": eq})
                continue
        if not send:
            failed = True
            out.append({"hero": h.get("name"), "dataId": did, "want": want, "sent": [],
                        "ok": False, "missing": missing, "now": [],
                        "err": "没有游戏认的技能 id（heroSkills 里一个都没有）"})
            n = _skill_fail.get(did, 0) + 1
            _skill_fail[did] = n
            if n >= SKILL_MAX_FAIL:
                _skill_bad[did] = want
            continue
        r = _act({"type": "setSkills", "actorGuid": g, "skillIds": ",".join(send)}) or {}
        now_list = [x for x in (r.get("now") or []) if isinstance(x, str)]
        ok = bool(r.get("ok")) and set(send) <= set(now_list)
        rec = {"hero": h.get("name"), "dataId": did, "want": want, "sent": send, "ok": ok,
               "missing": list(missing), "was": r.get("was"), "now": now_list,
               "err": r.get("error")}
        if ok:
            _skill_done[did] = want
            _skill_bad.pop(did, None)
            _skill_fail.pop(did, None)
        else:
            failed = True
            base_of = (info or {}).get("baseOf") or {}
            rec["missing"] = missing + [w for w in want
                                        if (base_of.get(w) or base_skill_id(w)) not in now_list
                                        and w not in missing]
            was = [x for x in (r.get("was") or []) if isinstance(x, str)]
            rec["restored"] = False
            if was and set(was) != set(now_list):
                r2 = _act({"type": "setSkills", "actorGuid": g,
                           "skillIds": ",".join(was)}) or {}
                back = [x for x in (r2.get("now") or []) if isinstance(x, str)]
                rec["restore"] = back
                rec["restored"] = bool(r2.get("ok")) and set(was) <= set(back)
                if rec["restored"]:
                    rec["now"] = back
            n = _skill_fail.get(did, 0) + 1
            _skill_fail[did] = n
            if n >= SKILL_MAX_FAIL:
                _skill_bad[did] = want
        out.append(rec)
    if failed:
        # 失败别每 60 秒刷一条：退避后再试（下次可能是旅馆，setSkills 也许就吃下了）。
        _skill_last[0] = now_t + SKILL_BACKOFF
    return out


def mark_paths_pending():
    """新一局选人：把"道途还没设"记下来，下一次 apply_paths 强制跑。"""
    _path_done.clear()
    _path_tried.clear()
    _path_pending[0] = True


def reset():
    _path_done.clear()
    _path_tried.clear()
    _path_pending[0] = False
    _skill_done.clear()
    _skill_bad.clear()
    _skill_fail.clear()
    _skill_last[0] = 0.0
