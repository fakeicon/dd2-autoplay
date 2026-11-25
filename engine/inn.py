"""inn.py —— 旅馆例行事务：花精通点 + 选地区 + 休息出发 + 装备（带回读验证）。

被 runner.py 在 scene=="inn" 时每 tick 调一次；内部用 phase 记录进度，
一个旅馆只做一遍（离开旅馆后 reset）。

**口径（2026-11-23 09:1x 按反编译核实）**：本文件里关子界面/开角色表用的是 mod 的 `simAction`
（CharacterSheet / ExitMenu）。实现是 `InputSimulatorUtils.SimulateActionClick`（`InputSimulatorUtils.cs:33-69`）：
**不碰任何输入设备**，只是把 UI 用 `InputSystemBhv.AddListener` 注册的回调按动作名取出来调一遍
（`performed:true`，隔一帧再发 canceled）—— 即"对游戏自己注册的 UI 处理器做 API 分发"。
所以它不是键鼠模拟，也不需要替换；真正被禁的是 `key`/`mouse`/`pointerClick`（本文件 0 处）。
"""
import json
import os
import time

import combat3 as C
import trinkets as T

HERE = os.path.dirname(os.path.abspath(__file__))
GEAR_LOG = os.path.join(HERE, "gear_log.jsonl")


def _load_item_tables():
    """读游戏导出的两份物品表（作者整理，见 数据索引-物品.md）：
    db/items.json → id: type（currency/rest/combat…）；db/trinkets.json → 饰品 id 集合。
    分类以它们为准，不再靠 id 里的字样猜。
    """
    types, trinkets = {}, set()
    try:
        with open(os.path.join(HERE, "db", "items.json"), encoding="utf-8") as f:
            for it in json.load(f):
                if isinstance(it, dict) and it.get("id"):
                    types[str(it["id"]).lower()] = (it.get("type") or "").lower()
    except Exception:
        pass
    try:
        with open(os.path.join(HERE, "db", "trinkets.json"), encoding="utf-8") as f:
            for it in json.load(f):
                if isinstance(it, dict) and it.get("id"):
                    trinkets.add(str(it["id"]).lower())
    except Exception:
        pass
    return types, trinkets


_ITEM_TYPE, _TRINKET_IDS = _load_item_tables()


def _is_currency(item_id):
    iid = (item_id or "").lower()
    if iid in _TRINKET_IDS:
        return False            # 表里明确是饰品，别再被 "bauble" 字样误伤
    return iid in CURRENCY or _ITEM_TYPE.get(iid) == "currency" or "bauble" in iid

# 2026-11-22：商店里**会买饰品**了（以前 cat() 把 trinket 归到"不买"，导致全场 0/2 裸装）。
# 一次客栈最多买这么多件，避免把钱全砸在饰品上、买不起战斗道具。
TRINKET_PER_STORE = 2

# 装备槽数量（实机）：英雄饰品栏 2 格、战斗道具栏 1 格。
# 注意 GetNumberOfUnlockedSlots() 一直报 0，但 0 号槽实际能用（拖拽实测成功过），
# 所以这里按"总格数"遍历，靠 SwapItems 抛异常来跳过未解锁槽。
TRINKET_SLOTS = 2
COMBAT_SLOTS = 1

# 战斗道具默认**不装**（2026-11-20 02:0x）：mod 那条路 100% 失败，两种报错轮流出现 ——
#   laudanum/combat@0: Index was out of range      （m_CombatInventory 的槽数是 0，SwapItems 越界）
#   smoke_bomb/combat@0: Object reference not set  （SwapItems 内部空引用）
# 失败后还会掉进 UI 拖拽兜底：按 C 开角色表、拖、Esc 关 —— 每次 20~30 秒，一无所获，
# 而且 Esc 关不干净会把暂停菜单开出来（01:5x 那次 stuck 就是这么卡死的）。
# 要再试就 DD2_EQUIP_COMBAT=1。
# 要再试就 DD2_EQUIP_COMBAT=1。**默认关**（注释和代码以前不一致：注释写"默认不装"，
# 代码默认却是 "1" → gear_log 里 153 次失败有一半是 smoke_bomb/laudanum 这类战斗道具，
# 每次都是 mod 抛越界再掉进 20~30 秒的 UI 拖拽兜底）。
COMBAT_EQUIP = os.environ.get("DD2_EQUIP_COMBAT", "0").strip() not in ("0", "false", "False")

# 货币/资源类 id：它们会混进"英雄饰品"和背包物品里（历史 gear 字段出现过 gold/candles），
# 一律不算装备。分类时**先判这个**，避免 gold 被当成饰品去 SwapItems。
CURRENCY = {"gold", "candles", "relics", "hope", "memory", "food", "laurels"}

# 饰品 id 线索（只在旧 DLL cls 为空时兜底）。2026-11-23 04:3x 修正：
#   "bauble" **不是**饰品线索 —— valley_baubles / tundra_baubles 是货币
#   （db/items.json type=currency；游戏自己的「饰品」筛选里 0 件；背包 cls 也回 currency）。
#   以前把 "bauble" 当饰品，mod 的 SwapItems 真把货币塞进了英雄的饰品槽
#   （gear_log.jsonl 全量：11-21~11-23 共 104 次调用、116 件 valley_baubles 被当饰品装上，
#    其中 Baldwin 63 件 / Damian 47 件；logs/复盘-饰品-2026-11-23.out §4 可复跑），
#   verify 的 uiDrag 也一直在拖货币。
TRINKET_HINTS = ("trinket",)

# 战斗道具（P3）id。cls 拿不到时（旧 DLL / 特殊 id）用这份名单兜底。
COMBAT_ITEMS = {
    "thunderclap_grenade", "smoke_bomb", "ablative_powders", "antivenom", "bear_trap",
    "smelling_salts", "torch_consumable", "bandages", "stimulants", "burn_salve",
    "holy_water", "laudanum", "firework", "trap_kit", "shovel", "key_tool",
}

# 攻略 §3 的 5 点顺序（技能 id 用升级版 *_u）：
#   瘟疫战地医疗 → 老兵减压(鼓舞) → 盗墓毒镖 → 手枪 / 飞刀
PLAN = {
    "pd": ["pd_battlefield_medicine_u"],          # 瘟疫：唯一奶，+25%
    "maa": ["maa_bolster_u"],                    # 老兵：鼓舞，自身+目标各减压 2
    "gr": ["gr_poison_dart_u", "gr_thrown_dagger_u"],  # 盗墓：毒镖 → 飞刀
    "hwm": ["hwm_pistol_shot_u"],                # 强盗：手枪（小晕）
}
PRIORITY = ["pd", "maa", "gr", "hwm"]

_phase = [0]     # 0=训练师 1=选地区 2=休息确认 3=embark
_mod_err = [None]   # 最近一次 mod 装备调用的失败原因（写进 gear_log，方便事后分诊）
_mod_tries = []     # 本次装配**每个槽**的结果（{slot,ok,err}）——以前只留最后一条，
                    # 于是 slot 0 的报错被 slot 1 的越界覆盖，永远看不到真因
_mod_slots = {}     # kind → mod 实测的槽数（拿到就不再拿固定 2/1 去撞越界）


def reset():
    _phase[0] = 0


def _act(o):
    try:
        return C.act(o)
    except Exception:
        return {}


def _ui():
    try:
        return C.get("/ui")
    except Exception:
        return {}


def _upg():
    return _act({"type": "upgrades"})


def _hero_prefix(buttons):
    for b in buttons:
        sid = b.get("skillId") or ""
        for p in PRIORITY:
            if sid.startswith(p + "_"):
                return p
    return None


def _open_trainer():
    u = _ui()
    tabs = sorted([x for x in u.get("clickables", [])
                   if "inn_sub_screen_collection/buttons" in (x.get("path") or "")],
                  key=lambda x: x.get("x") or 0)
    if len(tabs) < 4:
        return False
    # 语义点（按路径）；坐标只做兜底 —— 训练师页签以前按"排序后第 4 个"的坐标点。
    tab = tabs[3]
    r = _act({"type": "uiClick", "pick": tab.get("path") or "", "index": tab.get("i", -1)})
    if not (isinstance(r, dict) and r.get("ok")):
        _act({"type": "uiPoint", "x": tab["x"], "y": tab["y"], "holdMs": 120})
    time.sleep(1.5)
    return bool(_upg().get("buttons"))


def spend_mastery():
    """把当前旅馆的精通点按攻略顺序花掉。返回 (done_skills, spent_points_guess)。"""
    done, spent = [], 0
    if not _upg().get("buttons") and not _open_trainer():
        return done, spent
    seen = set()
    for _ in range(12):                    # 最多绕两圈
        r = _upg()
        buttons = r.get("buttons") or []
        if not buttons:
            break
        scr = r.get("screen") or {}
        guid = str(scr.get("actorGuid"))
        pref = _hero_prefix(buttons)
        if pref is None:
            break
        # 这个英雄还没买的计划项
        want = [s for s in PLAN.get(pref, [])
                if not any(b.get("skillId") == s and b.get("upgraded") for b in buttons)]
        for sid in want:
            res = _act({"type": "trainerUpgrade", "skillId": sid})
            time.sleep(0.55)
            chk = _upg()
            ok = any(b.get("skillId") == sid and b.get("upgraded") for b in chk.get("buttons") or [])
            if ok:
                done.append(sid)
                spent += 1
            elif not res.get("ok"):
                break
        seen.add(guid)
        # 换下一个英雄
        u = _ui()
        rb = next((x for x in u.get("clickables", [])
                   if "right_button" in (x.get("path") or "")), None)
        if not rb:
            break
        _act({"type": "uiClick", "index": rb["i"]})
        time.sleep(1.0)
        nxt = str((_upg().get("screen") or {}).get("actorGuid"))
        if nxt in seen:
            break
    _act({"type": "simAction", "name": "ExitMenu"})
    time.sleep(0.8)
    return done, spent



def buy_supplies(max_items=5, dry_run=False):
    """在客栈商店买东西（2026-11-21 20:3x 第三版）。

    2026-11-21 的三次演进：
      1) 写死点第 3 个 tab，tab 顺序一变就点空；
      2) 挨个 tab 试 + 判 UI 里有没有 item_store —— 在 6 个 tab 的客栈上失灵（"6 个 tab 都没商品"）；
      3) **不碰 UI**：货源直接在 `InnBhv.GetInnInstance().StoreInventory` 里（InnStoreUiBhv.cs:54
         只是把它包成 UI 数据源），买入照 StoreInventoryItemBhv.OnTryBuyItem（:95-139）。
         用 mod 的 storeList / storeBuy，按游戏自己的 item type 分档。
    """
    bought = []
    if dry_run:
        return bought
    try:
        cur = _act({"type": "bag"}).get("bag") or []
        free = max(0, 20 - len(cur))
        if free <= 0:
            print("[商店] 背包 %d 格已满，先不买（等装备/消耗腾出格子）" % len(cur), flush=True)
            return bought
        max_items = min(max_items, free)
        # 2026-11-21 20:3x：**不再点页签、不再开商店界面**。货源在
        # `InnBhv.GetInnInstance().StoreInventory` 里（InnStoreUiBhv.cs:54 只是把它包成 UI 数据源），
        # 买入照 StoreInventoryItemBhv.OnTryBuyItem（:95-139）：查价 → 扣钱 → TakeItemQty → 进背包。
        # mod 现在直接给 storeList / storeBuy 两个动作。
        st = _act({"type": "storeList"})
        if not st.get("ok"):
            print("[商店] storeList 失败：%s" % (st.get("error"),), flush=True)
            return bought
        goods = st.get("items") or []
        if not goods:
            print("[商店] 这间客栈货架是空的", flush=True)
            return bought

        # 队伍职业 id：专属饰品只给本人买（错人买了也没用），所以要先知道队里有谁。
        try:
            import loadout
            party_ids = [h.get("dataId") for h in (loadout.party() or []) if h.get("dataId")]
        except Exception:
            party_ids = []

        tr_note = {}

        def cat(it):
            """-1 值得买的饰品 / 0 战斗物品 / 1 马车装备 / 2 旅馆物品 / 3 食物火把 / 4 不买。
            用游戏自己的 item type（combat / stage_coach_upgrade / rest / trinket），不看 tip 文本。
            饰品按 trinkets.worth_buying 打分（数值来自游戏 buff 表，文案来自游戏本地化），
            没有可用数值的一律不买 —— 宁可不买，也不按文案猜。
            """
            tp = (it.get("type") or "").lower()
            iid = (it.get("id") or "").lower()
            tags = [str(t).lower() for t in (it.get("tags") or [])]
            if tp == "trinket":
                ok, why = T.worth_buying(iid, party_ids)
                tr_note[iid] = why
                return -1 if ok else 4
            if tp == "stage_coach_upgrade":
                return 1
            if tp == "combat":
                return 0
            if tp == "rest":
                if "food" in tags or iid.startswith("food") or "torch" in iid:
                    return 3
                return 2
            return 4

        picks = sorted([x for x in goods if cat(x) <= 3 and x.get("afford") is not False], key=cat)
        # 饰品单独限流：优先买，但一次最多 TRINKET_PER_STORE 件
        tr = [x for x in picks if cat(x) == -1]
        for x in tr[TRINKET_PER_STORE:]:
            tr_note[x.get("id")] = "超出本次上限 %d 件" % TRINKET_PER_STORE
        picks = tr[:TRINKET_PER_STORE] + [x for x in picks if cat(x) != -1]
        if tr_note:
            print("[商店] 饰品评估：%s" % "；".join(
                "%s=%s" % (k, v) for k, v in list(tr_note.items())[:8]), flush=True)
        reserved = []
        for r in (1, 2):                      # 先给"长期收益"各留 1 个名额：马车装备、旅馆物品
            for x in picks:
                if cat(x) == r:
                    reserved.append(x)
                    break
        picks = reserved + [x for x in picks if x not in reserved]
        print("[商店] 货架 %d 件（饰品%d/战斗%d/马车%d/旅馆%d/消耗%d），准备买：%s" % (
            len(goods),
            sum(1 for x in goods if cat(x) == -1),
            sum(1 for x in goods if cat(x) == 0),
            sum(1 for x in goods if cat(x) == 1),
            sum(1 for x in goods if cat(x) == 2),
            sum(1 for x in goods if cat(x) == 3),
            [x.get("id") for x in picks[:max_items]]), flush=True)
        for it in picks[:max_items]:
            before = {(x.get("itemId") or "") for x in (_act({"type": "bag"}).get("bag") or [])}
            r = _act({"type": "storeBuy", "index": it["i"]})
            time.sleep(0.4)
            after = {(x.get("itemId") or "") for x in (_act({"type": "bag"}).get("bag") or [])}
            new = sorted(after - before)
            if r.get("ok") and new:
                bought.extend(new)
                print("[商店] 买到 %s（%s）" % (it.get("id"), new), flush=True)
            else:
                print("[商店] 没买到 %s：%s" % (it.get("id"), r.get("error") or "背包没变化"), flush=True)
                if not r.get("ok"):
                    break
        _act({"type": "simAction", "name": "ExitMenu"})
        time.sleep(0.7)
    except Exception as e:
        print("[商店][失败] %s: %s" % (type(e).__name__, e), flush=True)
    return bought


def gear_state():
    """读一次**真实**装备状态（mod 的 trinkets 动作，只看状态不动游戏）。

    返回 {"ok":True, "heroes":[{guid,name,dataId,hp,trinkets:[id]}], "bag":[{slot,itemId,cls,uses}]}
    读不到 RunBhv（祭坛/主菜单/新局没开始时）返回 {"ok":False,"error":...}。
    """
    r = _act({"type": "trinkets"})
    if not isinstance(r, dict) or not r.get("ok"):
        return {"ok": False, "error": (r or {}).get("error") or "trinkets 接口不可用"}
    heroes = []
    for a in r.get("actors") or []:
        did = (a.get("dataId") or "").lower()
        if "corpse" in did:          # 尸体不算；死门（hp<=0 但活着）要算
            continue
        heroes.append({"guid": str(a.get("guid")),
                       "name": a.get("name") or a.get("dataId") or str(a.get("guid")),
                       "dataId": a.get("dataId"),
                       "hp": a.get("hp"),
                       # 读端会混进 gold/candles 这类货币 id（历史 gear 字段里出现过）：
                       # 挡在装备列表外，单独记 junk 当数据质量指标。
                       "trinkets": [i for i in (a.get("trinkets") or [])
                                    if not _is_currency(i)],
                       # 战斗道具栏（2026-11-21 19:5x 加）：以前只有 trinkets，
                       # 于是"拖战斗道具进道具槽"永远验证不到（物品被记成饰品，见 gear_log 字段混用）。
                       "combat": [i for i in (a.get("combat") or [])
                                  if not _is_currency(i)],
                       "junk": [i for i in (a.get("trinkets") or [])
                                if _is_currency(i)]})
    bag = [{"slot": it.get("slot"), "itemId": it.get("itemId") or "",
            "cls": it.get("cls") or "", "uses": it.get("uses")}
           for it in (r.get("bag") or [])]
    return {"ok": True, "heroes": heroes, "bag": bag}


def _kind(item_id, cls=""):
    """物品分类：trinket / combat / other。

    以游戏导出的数据表为准：db/trinkets.json 是饰品全集，db/items.json 的 type 是
    currency/rest/combat…（作者整理的 数据索引-物品.md 同源）。mod 回的 cls 次之，
    表里没有的新 id 才退回字样线索。
    2026-11-23 教训：valley_baubles 是 currency/faction（数据索引-物品.md 第 166 行），
    以前靠 "bauble" 字样猜成饰品 → SwapItems 把货币塞进了饰品槽。
    """
    iid = (item_id or "").lower()
    if iid in _TRINKET_IDS:
        return "trinket"
    t = (cls or "").lower() or _ITEM_TYPE.get(iid, "")
    if t in ("currency", "rest"):
        return "other"          # 货币/旅馆物品：绝不能进 SwapItems
    if "trinket" in t:
        return "trinket"
    if "combat" in t or "battle" in t:
        return "combat"
    # 表里查不到（新内容/旧 DLL）才退回线索
    if iid in CURRENCY or "bauble" in iid:
        return "other"
    if any(h in iid for h in TRINKET_HINTS):
        return "trinket"
    if iid in COMBAT_ITEMS:
        return "combat"
    return "other"


def _bag_ids(st, kind=None):
    return [it["itemId"] for it in (st.get("bag") or [])
            if it.get("itemId") and (kind is None or _kind(it["itemId"], it.get("cls")) == kind)]


def gear_audit():
    """只读体检：谁身上到底戴了什么、背包里还剩多少件没上身。

    {"ok":True, "heroes":[{name,trinkets,n}], "bare":[一个饰品都没有的英雄],
     "bag_trinkets":[id...], "bag_combat":[id...], "unequipped":[id...], "needs_attention":bool}
    """
    st = gear_state()
    if not st.get("ok"):
        return {"ok": False, "error": st.get("error")}
    heroes = [{"name": h["name"], "trinkets": h["trinkets"], "n": len(h["trinkets"]),
               "hp": h["hp"], "junk": h.get("junk") or []} for h in st["heroes"]]
    bag_tr = _bag_ids(st, "trinket")
    bag_cb = _bag_ids(st, "combat")
    junk = [j for h in heroes for j in h["junk"]]
    out = {"ok": True, "heroes": heroes,
           "bare": [h["name"] for h in heroes if not h["trinkets"]],
           "bag_trinkets": bag_tr, "bag_combat": bag_cb,
           "junk_trinkets": junk,
           "unequipped": bag_tr,
           "needs_attention": bool(bag_tr)}
    return out


def _log_gear(rec):
    rec = dict(rec)
    rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(GEAR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _mod_equip(guid, item_id, kind, slots, start=0):
    """按 itemId 走 mod 的 SwapItems 装（不碰鼠标）。返回成功的槽位号，失败 None。

    start = 从这个槽位号开始试。**必须从"第一个空槽"开始**（= 已装备件数）：
    SwapItems 是"对调"，往已经有饰品的槽上装会把原来那件换回背包，
    于是出现"刚装上 A，又装 B 时把 A 换出来"的来回倒腾。
    mod 的返回里带 equipped（装完后的饰品 id 列表），先自检，
    调用方会再读一次 gear_state 做事后验证 —— 两次都对才认。

    失败原因记进 _mod_err[0]（2026-11-20 加：以前只报"装备槽可能没解锁"，
    分不清是 item 不在包/槽没解锁/SwapItems 签名变了 —— 三个原因都栽给同一句话）。
    """
    try:
        g = int(guid)
    except (TypeError, ValueError):
        _mod_err[0] = "actorGuid 不是数字：%r" % (guid,)
        return None
    del _mod_tries[:]
    # mod 报过真实槽数就用它收窄（异常返回里带 tinvSlots）；没报过才按传入的固定值试。
    hi = slots
    if _mod_slots.get(kind) is not None:
        hi = min(hi, int(_mod_slots[kind]))
    for slot in range(max(0, start), hi):
        r = _act({"type": "equipTrinket", "actorGuid": g, "itemId": item_id,
                  "slot": slot, "kind": kind})
        r = r if isinstance(r, dict) else {}
        err = r.get("error")
        _mod_tries.append({"slot": slot, "kind": kind, "ok": bool(r.get("ok")),
                           "err": (err or "")[:160], "equipped": r.get("equipped")})
        if r.get("ok") and item_id in (r.get("equipped") or []):
            return slot
        # 异常路径会带 tinvSlots（mod 现成字段），拿到就记住：别再往不存在的槽上撞。
        try:
            ts = int(r.get("tinvSlots"))
        except (TypeError, ValueError):
            ts = None
        if ts is not None:
            _mod_slots[kind] = ts
            if slot + 1 >= ts:
                break
        if err and _mod_err[0] is None:      # 记**第一**条：这才是真正的失败起点
            _mod_err[0] = "%s/%s@%d: %s" % (item_id, kind, slot, err)
    return None


def _ui_slots(marker):
    """角色表里某类槽位（按 x 排序，左→右）。"""
    return sorted([x for x in _ui().get("clickables", []) if marker in (x.get("path") or "")],
                  key=lambda x: (x.get("x") or 0))


def _bag_widgets():
    """物品栏格子（按 上→下、左→右 排）；下标 = 背包槽位号 bag[i]["slot"]。

    2026-11-23 04:3x 两处实机修正：
      1) 21 个 PlayerInventoryItem 连空格一起返回，且 y 升序是下→上 —— 按 -y 排才是
         上→下、才和 bag 的槽位顺序一致；
      2) **不能按"有文本"过滤** —— 数量 1 的格子不显示数字（刚买的饰品文本为空，
         logs/uidrag-B-diag.out 第 9 行只剩 5 格），过滤会把真物品丢掉。
         现在全返回，调用方用 `widgets[bag[i]["slot"]]` 直接按槽位取格。
    """
    return sorted([x for x in _ui().get("clickables", [])
                   if "PlayerInventoryItem" in (x.get("path") or "")],
                  key=lambda x: (-(x.get("y") or 0), x.get("x") or 0))


def _bag_filter_all():
    """把背包筛选切回「所有物品」（点过饰品槽后游戏会切成「饰品」，格子会清空/重排）。"""
    fa = next((x for x in _ui().get("clickables", [])
               if "Filter - All" in (x.get("path") or "")), None)
    if not fa:
        return False
    _act({"type": "uiClick", "index": fa["i"]})
    time.sleep(0.55)
    return True


def _open_sheet():
    """按 C 打开角色表，成功返回 True。"""
    _act({"type": "simAction", "name": "CharacterSheet"})
    time.sleep(1.0)
    return any("CharacterSheet" in (x.get("path") or "") for x in _ui().get("clickables", []))


def _close_sheet():
    """只在角色表还开着时按 Esc；万一误开了暂停菜单就点「返回游戏」。"""
    u = _ui()
    if any("CharacterSheet" in (x.get("path") or "") for x in u.get("clickables", [])):
        for _ in range(2):
            _act({"type": "simAction", "name": "ExitMenu"})
            time.sleep(0.45)
        u = _ui()
        if any("CharacterSheet" in (x.get("path") or "") for x in u.get("clickables", [])):
            cb = next((x for x in u.get("clickables", [])
                       if "CloseScreenButton" in (x.get("path") or "")), None)
            if cb:
                _act({"type": "uiClick", "index": cb["i"]})
                time.sleep(0.8)
    u = _ui()
    if any("PauseMenu" in (x.get("path") or "") for x in u.get("clickables", [])):
        back = next((x for x in u.get("clickables", [])
                     if (x.get("text") or "").strip() == "返回游戏"), None)
        if back:
            _act({"type": "uiClick", "index": back["i"]})
            time.sleep(0.8)


def _ui_drag_equip(want_tr, want_cb):
    """兜底：开角色表拖拽 —— **先核对包里到底有没有这件、是不是这一类，再拖**。

    2026-11-20 02:1x 用户口径：「先检测包里有没有饰品或者战斗道具，再想着去拉过去给角色」。
    旧写法是 `for w in _bag_widgets()[:5]` —— 抓背包前 5 个格子挨个拖，根本不管
    那格里是食物、金币还是任务品；游戏不接收也不报错，白白开合角色表 20~30 秒，
    01:47 / 01:50 两次 `how=ui-drag eq=0` 就是这么来的（那两次包里只有 1 件 smoke_bomb）。

    现在：
      1. 先读 gear_state，只挑「饰品 → 饰品槽 / 战斗道具 → 道具槽」这两类，算出它是包里第几件；
      2. 背包槽位号必须落在界面格子里（21 格按槽位排，不是按"有文本"），否则直接放弃；
      3. 每拖一次只认「**这一件**从包里少了一个 + 真的出现在身上」，否则立刻停手。
    """
    got = []
    if not want_tr and not want_cb:
        return got
    # 2026-11-23 作者口径「只有出战的人能装饰品」：这条路**没法确认按 C 打开的是谁的角色表**
    # （游戏默认选中谁，代码里读不到），所以默认关；要开必须显式设 DD2_EQUIP_UI_DRAG=1。
    if not os.environ.get("DD2_EQUIP_UI_DRAG"):
        print("[装备][跳过] UI 拖拽兜底默认关闭（无法确认打开的是谁的角色表；要开设 DD2_EQUIP_UI_DRAG=1）",
              flush=True)
        return got
    pg = _party_guids()
    if not pg:
        print("[装备][跳过] 读不到出战名单，UI 兜底也不做", flush=True)
        return got
    try:
        # 战斗里绝不开角色表（2026-11-21 08:4x 现场）：技能栏在屏幕上就说明在打，
        # 这时按 C 开表会把技能格挡住 → 该出的招全"被拒"，一个回合白烧。
        if any("combat_skill_button" in ((x.get("path") or "")) for x in (_ui().get("clickables") or [])):
            print("[装备][跳过] 战斗画面里不开角色表", flush=True)
            return got
        if not _open_sheet():
            return got
        _bag_filter_all()
        for kind, wanted, marker, nslots in (
                ("trinket", want_tr, "Trinkets/TrinketInventoryItem", TRINKET_SLOTS),
                ("combat", want_cb, "CombatInventoryItem", COMBAT_SLOTS)):
            if not wanted:
                continue
            for _ in range(nslots + 2):        # 一次拖一件，最多试这么多次
                st = gear_state()
                if not st.get("ok"):
                    break
                bag = st.get("bag") or []
                idx = next((i for i, it in enumerate(bag)
                            if it.get("itemId") in wanted
                            and _kind(it["itemId"], it.get("cls")) == kind), None)
                if idx is None:
                    break                       # 包里已经没有这一类可装了
                item = bag[idx]["itemId"]
                cell = bag[idx].get("slot")
                widgets = _bag_widgets()
                if not isinstance(cell, int) or not (0 <= cell < len(widgets)):
                    print(f"[装备][跳过] 背包槽 {cell} 超出界面 {len(widgets)} 格，不盲拖",
                          flush=True)
                    break
                slots = _ui_slots(marker)[:nslots]
                if not slots:
                    break
                slot = slots[0]
                # 2026-11-23 04:3x 实机：**别先点槽位** —— 点一下游戏会把背包切成「饰品」筛选，
                # 21 格全不显示物品（logs/uidrag-B-diag.out 第 10~12 行），点前算的坐标不再可信。
                # 直接往槽位拖，拖完回读。
                w = widgets[cell]
                r = _act({"type": "uiDrag", "x1": w["x"], "y1": w["y"],
                          "x2": slot["x"], "y2": slot["y"]})
                if not (isinstance(r, dict) and r.get("ok")):
                    print("[装备][拖拽] uiDrag 没成：%s" % (r or {}).get("error"), flush=True)
                time.sleep(0.9)
                after = gear_state()
                if not after.get("ok"):
                    break
                lost = _bag_ids(after).count(item) < _bag_ids(st).count(item)
                # kind 对应各自的槽（2026-11-21 修字段混用）：战斗道具以前也去 trinkets 里找，
                # 永远找不到 → "拖进道具槽"这条路 100% 判失败。
                field = "combat" if kind == "combat" else "trinkets"
                worn_party = [h.get("name") for h in after["heroes"]
                              if str(h.get("guid")) in pg and item in (h.get(field) or [])]
                worn_other = [h.get("name") for h in after["heroes"]
                              if str(h.get("guid")) not in pg and item in (h.get(field) or [])]
                if worn_other and not worn_party:
                    print("[装备][危险] %s 拖到替补 %s 身上了，停手（只有出战的人能装）"
                          % (item, ",".join(str(x) for x in worn_other)), flush=True)
                    break
                if lost and worn_party:
                    got.append({"item": item, "how": "ui-drag-" + kind, "slot": slot.get("i")})
                    print(f"[装备] 拖拽装上 {item} → {kind} 槽 {slot.get('i')}", flush=True)
                    wanted.discard(item)
                    continue
                print(f"[装备][停手] 拖 {item} 没上到出战的人身上（背包 {len(_bag_ids(st))} → "
                      f"{len(_bag_ids(after))} 件，出战身上 {'有' if worn_party else '没有'}）", flush=True)
                break
    finally:
        _close_sheet()
    return got


HERO_CODE = {
    "grave_robber": "gr", "highwayman": "hwm", "plague_doctor": "pd",
    "man_at_arms": "maa", "hellion": "hel", "leper": "lep",
    "occultist": "occ", "vestal": "ves", "jester": "jes",
    "flagellant": "flg", "runaway": "run", "duelist": "dul",
}
RARITY_SCORE = {"ancestral": 5, "epic": 4, "cultist": 3, "rare": 2, "common": 1}


def trinket_tier(item_id):
    """粗排一个"值不值得戴"的分数（只有 id 拿得到，20 秒量级的近似）。

    分级系列 36 件 = 12 款 ×3 级（minor/greater/普通），minor < greater < 顶级。
    """
    iid = item_id or ""
    if iid.startswith("trinket_tiered_"):
        if iid.endswith("_minor"):
            return 2
        if iid.endswith("_greater"):
            return 4
        return 5
    for k, v in RARITY_SCORE.items():
        if k in iid:
            return v
    return 3


def hero_of(item_id):
    """专属饰品属于哪个英雄：trinket_hero_gr_xxx → 'gr'；通用饰品返回 None。"""
    if (item_id or "").startswith("trinket_hero_"):
        parts = item_id.split("_")
        if len(parts) > 2:
            return parts[2]
    return None


def _party_guids():
    """出战名单（partyInfo 里 status == party 的 guid 集合）。

    2026-11-23 作者口径：**只有出战的人能装饰品**，替补（reserve/hire）一律不碰。
    读不到 partyInfo 返回空集 —— 调用方应放弃本次装备，宁可等，也不要把饰品喂给板凳。
    """
    r = _act({"type": "partyInfo"})
    if not isinstance(r, dict) or not r.get("ok"):
        return set()
    return {str(p.get("guid")) for p in (r.get("party") or [])
            if str(p.get("status") or "") == "party"}


def sort_trinkets(items, heroes):
    """装备优先级：专属的排最前，其次按稀有度/级别降序。

    作者口径（2026-11-20）：先把"正确的饰品戴到对的人身上"，再谈数量。
    专属饰品戴错人没有意义（游戏里也只对那个英雄生效），所以先排它。
    """
    return sorted(items, key=lambda i: (0 if hero_of(i) else 1, -trinket_tier(i)))


def equip_from_bag():
    """把背包里的饰品/战斗道具真的装到角色身上，并且**逐件回读验证**。

    背景（2026-11-19 用户反馈）：以前的写法是"点开饰品槽 → 拖背包第一格 → 完事"，
    既不挑物品也不回读。实测拖的可能是食物、或者拖到未解锁槽位，游戏什么都不做，
    而 runner 还当成功 —— 结果就是"背包里的饰品没了/还在，英雄身上却是空的"。
    现在：
      1. 先读真实状态（trinkets），列出背包里的 trinket/combat 件和没戴满的英雄；
      2. 先走 mod 的 equipTrinket（按 itemId 直接 SwapItems，不碰鼠标），装完回读验证；
      3. mod 路走不通才退回 UI 拖拽，同样每拖一次回读，背包少了才算数；
      4. 结果写 gear_log.jsonl；还剩没上身的会打 [装备][失败] 告警。

    返回 {"ok", "equipped":[{hero,item,how}], "failed":[...], "audit":{...}}
    """
    rep = {"ok": False, "equipped": [], "failed": [], "how": []}
    st0 = gear_state()
    if not st0.get("ok"):
        rep["error"] = st0.get("error")
        print(f"[装备][失败] 读不到装备状态：{st0.get('error')}", flush=True)
        _log_gear(rep)
        return rep
    tr_bag = _bag_ids(st0, "trinket")
    cb_bag = _bag_ids(st0, "combat") if COMBAT_EQUIP else []
    rep["before"] = {h["name"]: h["trinkets"] for h in st0["heroes"]}
    # 2026-11-23（作者口径：只有出战的人能装饰品）：名册 12 人里，替补那 8 个不许碰。
    # 以前这里直接拿 st0["heroes"]（整个名册）排序，按"空槽多、血高"先发 → 替补满血，出战的人带伤，
    # 于是每件饰品都被板凳抢走（gear_log 2026-11-23 当天 14 件全给了 Baldwin/Damian/Pal/Junia，
    # 出战 4 人 0 件）。读不到 partyInfo 就这次不装，宁可等也不喂替补。
    pg = _party_guids()
    if not pg:
        rep["ok"] = False
        rep["error"] = "读不到出战名单（partyInfo），本次不装"
        print("[装备][失败] 读不到出战名单（partyInfo），本次不装", flush=True)
        _log_gear(rep)
        return rep
    if not tr_bag and not cb_bag:
        rep["ok"] = True
        rep["audit"] = {"bare": [h["name"] for h in st0["heroes"] if not h["trinkets"]]}
        return rep                      # 没东西可装，不写日志免得刷屏

    # —— 1) mod 路径：按 itemId 直接装，装一件回读一次 ——
    # 判定口径：英雄的饰品表里出现了这件 **而且** 背包里这件少了一个（同一个 id 可能有两件）。
    # 优先级（作者口径 2026-11-20）：**专属饰品 > 稀有度高的 > 便宜的**；
    # 战斗道具（火把/绷带/净化类）优先给"身上一件战斗道具都没有"的人。
    tr_bag = sort_trinkets(tr_bag, st0["heroes"])
    heroes = [h for h in st0["heroes"] if str(h.get("guid")) in pg]      # 只出战的人
    heroes = sorted(heroes, key=lambda h: (len(h["trinkets"]), -(h.get("hp") or 0)))
    st_cur = st0
    for item in list(tr_bag):
        n_before = _bag_ids(st_cur).count(item)
        for h in heroes:
            if len(h["trinkets"]) >= TRINKET_SLOTS:
                continue
            want = hero_of(item)                      # 专属饰品只戴给本人
            if want and HERO_CODE.get(h.get("dataId")) != want:
                continue
            slot = _mod_equip(h["guid"], item, "trinket", TRINKET_SLOTS,
                              start=len(h["trinkets"]))
            if slot is None:
                continue
            st1 = gear_state()
            if not st1.get("ok"):
                break
            worn = next((x["trinkets"] for x in st1["heroes"] if x["guid"] == h["guid"]), [])
            if item in worn and _bag_ids(st1).count(item) < n_before:
                rep["equipped"].append({"hero": h["name"], "item": item,
                                        "how": "mod-swap", "slot": slot})
                tr_bag = [i for i in tr_bag if i != item]
                h["trinkets"] = worn
                st_cur = st1
                print(f"[装备] {h['name']} 装上饰品 {item}（mod 槽 {slot}，回读确认）", flush=True)
                break
    for item in list(cb_bag):
        n_before = _bag_ids(st_cur).count(item)
        for h in heroes:
            slot = _mod_equip(h["guid"], item, "combat", COMBAT_SLOTS)
            if slot is None:
                continue
            st1 = gear_state()
            if not st1.get("ok"):
                break
            if _bag_ids(st1).count(item) < n_before:
                rep["equipped"].append({"hero": h["name"], "item": item,
                                        "how": "mod-swap-combat", "slot": slot})
                cb_bag = [i for i in cb_bag if i != item]
                st_cur = st1
                print(f"[装备] {h['name']} 装上战斗道具 {item}（mod，回读确认）", flush=True)
                break

    # —— 2) 兜底：mod 一件都没装上（通常是槽位没解锁），退回 UI 拖拽 + 回读 ——
    # 战斗道具**不走 UI 兜底**：那条路一定要开角色表+拖拽，历史上每次 20~30 秒还一无所获，
    # 而且 Esc 关不干净会把暂停菜单开出来（01:5x 卡死就是这么来的）。战斗道具只试 mod 路。
    if tr_bag:
        rep["how"].append("ui-drag")
        for g in _ui_drag_equip(set(tr_bag), set()):
            rep["equipped"].append({"hero": "?", "item": g["item"], "how": g["how"],
                                    "slot": g.get("slot")})
            if "trinket" in g["how"]:
                tr_bag = [i for i in tr_bag if i != g["item"]]
            else:
                cb_bag = [i for i in cb_bag if i != g["item"]]

    # —— 3) 终检：再读一次，把"没上身"的明确列出来 ——
    st2 = gear_state()
    rep["audit"] = gear_audit() if st2.get("ok") else {"ok": False}
    rep["after"] = {h["name"]: h["trinkets"] for h in (st2.get("heroes") or [])} if st2.get("ok") else {}
    left_tr = _bag_ids(st2, "trinket") if st2.get("ok") else tr_bag
    left_cb = _bag_ids(st2, "combat") if (st2.get("ok") and COMBAT_EQUIP) else cb_bag
    rep["failed"] = left_tr + left_cb
    rep["ok"] = not rep["failed"]
    if _mod_tries:
        rep["mod_tries"] = [t for t in _mod_tries if not t.get("ok")]
    if _mod_err[0]:
        rep["mod_error"] = _mod_err[0]
        _mod_err[0] = None
    if left_tr:
        print(f"[装备][失败] 还有 {len(left_tr)} 件饰品没上身：{left_tr} —— "
              f"装备槽可能没解锁，或拖拽没落在格子上", flush=True)
    if left_cb and not left_tr:
        print(f"[装备][提示] 战斗道具没装上（不影响战斗）：{left_cb}", flush=True)
    _log_gear(rep)
    return rep



def free_space(keep=24):
    """背包超载时丢弃低价值重复道具，腾到 keep 格以下。返回丢掉的 id。"""
    dropped = []
    try:
        junk = ["bandages", "blood_brew", "molasses", "antivenom", "rag",
                "food_1", "hero_bones_01", "hero_bones_02", "hero_bones_03"]
        for item in junk:
            cur = _act({"type": "bag"}).get("bag") or []
            if len(cur) <= keep:
                break
            r = _act({"type": "dropItem", "itemId": item, "count": 1})
            if r.get("ok"):
                dropped.append(item)
        return dropped
    except Exception:
        return dropped


def _gear_brief(rep):
    """装备结果压成一行（写 status.json 用）。"""
    if not rep:
        return {}
    out = {"n": len(rep.get("equipped") or []), "fail": (rep.get("failed") or [])[:3]}
    bare = (rep.get("audit") or {}).get("bare") or []
    if bare:
        out["bare"] = bare[:4]
    return out


# —— 旅馆物品（rest 类）：休息前直接把效果落到队伍上 ——
# 走 mod 的 useRestItem（语义同游戏 RestWidgetBhv.LockStagedItem，见 mod/RestItems.cs），
# 不碰鼠标：买完当 tick 就用，省掉"拖物品到英雄头像"那一串 UI 操作。
_HEAL_HINTS = ("heal", "poultice", "bandage", "remedy", "food", "meal", "drink", "salve")
_STRESS_HINTS = ("stress", "calm", "incense", "poetry", "story", "song", "drum", "comfort")


def _rest_targets(item, party, blocked):
    """给一件旅馆物品挑目标 guid。返回 None = 没人接得了。

    2026-11-21 20:1x 实测：4 目标的全队物品传空 targets 时，mod 会挑**全队**（含被 actout
    拦住的英雄）→ 整次使用被拒（`target blocked: Audrey why=actout:quirk_squeamish_neg`）。
    游戏里这种物品也是"跳过被拦的人"照用，所以这里一律给显式名单（只含没被拦的人）。
    """
    need = item.get("targets") or 1
    usable = [h for h in party if (h.get("guid") or "") not in blocked]
    if not usable:
        return None
    hint = " ".join([item.get("id") or "", item.get("title") or ""] + list(item.get("tags") or [])).lower()
    if any(k in hint for k in _STRESS_HINTS):
        usable.sort(key=lambda h: -(h.get("stress") or 0))   # 压力最高的先减压
    else:
        usable.sort(key=lambda h: (h.get("hp") or 0))        # 其余（治疗/buff）给血最少的
    if need >= len(party):
        return [h["guid"] for h in usable]                   # 全队物品：给所有没被拦的人
    return [h["guid"] for h in usable[:need]]


def use_rest_items(dry_run=False):
    """把背包里的旅馆物品用掉（每件一次）。返回 {ok, used, skipped}。"""
    st = _act({"type": "restItems"})
    if not st.get("ok"):
        return {"ok": False, "err": st.get("error") or "restItems failed", "used": [], "skipped": []}
    party = st.get("party") or []
    items = st.get("items") or []
    used, skipped, errors = [], [], []
    for it in items:
        iid = it.get("id") or ""
        blocked = it.get("blocked") or {}
        tg = _rest_targets(it, party, blocked)
        if tg is None:
            skipped.append({"id": iid, "why": "all blocked", "blocked": blocked})
            continue
        r = _act({"type": "useRestItem", "itemId": iid, "targets": tg, "dry": bool(dry_run)})
        if r.get("ok"):
            used.append({"id": iid, "title": it.get("title"),
                         "to": r.get("targets") or ["party"]})
        else:
            errors.append({"id": iid, "err": r.get("error") or "?", "blocked": r.get("blocked")})
    if used or errors or skipped:
        detail = ", ".join("%s→%s" % (u["id"], "+".join(u["to"] if isinstance(u["to"], list) else [u["to"]]))
                           for u in used[:5])
        print("[旅馆物品] 用掉 %d 件%s%s%s" % (
            len(used),
            ("：" + detail) if detail else "",
            ("，失败 %d：%s" % (len(errors), errors[:3])) if errors else "",
            ("，跳过 %d" % len(skipped)) if skipped else ""), flush=True)
    return {"ok": not errors, "used": used, "skipped": skipped, "errors": errors}


def _rest_brief(rep):
    if not rep:
        return {}
    return {"n": len(rep.get("used") or []), "skip": len(rep.get("skipped") or []),
            "err": (rep.get("errors") or [])[:2]}


def step(u, dry_run=False):
    """旅馆里每 tick 调一次。返回 (action, delay, info)。"""
    acts = u.get("clickables", [])
    texts = " ".join(u.get("texts") or [])

    # 超载弹窗：先点「继续」关掉，再丢几件低价值道具
    if any("超出负荷" in (t or "") for t in u.get("texts") or []):
        if not dry_run:
            c = next((x for x in acts if (x.get("text") or "") == "继续"), None)
            if c:
                _act({"type": "uiClick", "index": c["i"]})
            dropped = free_space()
            return "inn_free", 1.5, {"dropped": dropped}
        return "inn_free", 1.5, {}

    # 0) 旅行日志：「继续」收掉结算面板（点页签之前必须先收）
    if any("ExpeditionContinue" in (x.get("path") or "") for x in acts):
        c = next(x for x in acts if "ExpeditionContinue" in (x.get("path") or ""))
        if not dry_run:
            _act({"type": "uiClick", "index": c["i"]})
        return "inn_continue", 2.0, {}

    # 行程选择子界面：直接点「燔城」格（没有就点第一格），选中后回到旅馆主界面。
    slots = [x for x in acts if "biome_choice_slot" in (x.get("path") or "")]
    if slots:
        pick = next((x for x in slots if "燔城" in (x.get("text") or "")), slots[0])
        if not dry_run:
            _act({"type": "uiClick", "index": pick["i"]})
        return "inn_route_pick", 1.6, {"biome": pick.get("text")}

    # 「收集希望」结局层：只 Esc，永不点
    if any("inn_sub_screen_end_run" in (x.get("path") or "") for x in acts):
        if not dry_run:
            _act({"type": "simAction", "name": "ExitMenu"})
        return "dismiss_end_run", 1.6, {}

    # 训练师页开着：只有**现在真的还有升级格子**才花点，否则只关门。
    # （u 是 tick 开头的快照，可能已经过期；这里用一次实时 /action upgrades 判断，避免反复重开。）
    if any("inn_sub_screen_upgrade_skills" in (x.get("path") or "") for x in acts):
        if not dry_run:
            if _upg().get("buttons"):
                done, spent = spend_mastery()
                _phase[0] = max(_phase[0], 1)
                return "inn_trainer", 2.0, {"done": done, "spent": spent}
            _act({"type": "simAction", "name": "ExitMenu"})
            time.sleep(0.7)
        _phase[0] = max(_phase[0], 1)
        return "inn_close_trainer", 1.5, {}

    # 1) 训练师：只在主界面（没有子界面）时做一次
    if _phase[0] == 0:
        if any("inn_sub_screen_upgrade_skills" in (x.get("path") or "") for x in acts):
            # 已经在训练师页：直接花点
            if not dry_run:
                done, spent = spend_mastery()
                rep = equip_from_bag()
                rest = use_rest_items()
                _phase[0] = 1
                return "inn_gear", 2.0, {"done": done, "spent": spent, "bought": [],
                                         "gear": _gear_brief(rep), "rest": _rest_brief(rest)}
            _phase[0] = 1
        tot = _act({"type": "innState"})
        if tot.get("ok"):
            if not dry_run:
                done, spent = spend_mastery()
                bought = buy_supplies()
                rep = equip_from_bag()
                rest = use_rest_items()
                _phase[0] = 1
                return "inn_gear", 2.0, {"done": done, "spent": spent, "bought": bought,
                                         "gear": _gear_brief(rep), "rest": _rest_brief(rest)}
            _phase[0] = 1

    # 2) 选地区（休息按钮没选地区时是灰的）
    st = _act({"type": "innState"})
    if st.get("ok") and not st.get("hasChoice"):
        ch = st.get("choices") or []
        idx = next((c["i"] for c in ch if c.get("biome") == "City"), (ch[0]["i"] if ch else 0))
        if not dry_run:
            _act({"type": "innSelect", "index": idx})
        return "inn_region", 1.2, {"biome": "City" if ch else "?"}

    # 3) 出发：接口 InnBhv.NextState() + CompleteInn()（等价于点「休息」→「是」，
    #    InnPresentationBhv.cs:891 确认回调里就这两句）。失败才退回点按钮。
    if not dry_run:
        r = _act({"type": "innEmbark"})
        if r.get("ok"):
            return "inn_embark", 3.0, {"via": "innEmbark"}
    if any("休息一夜" in (t or "") for t in u.get("texts") or []):
        c = next((x for x in acts if (x.get("text") or "") == "是"), None)
        if c and not dry_run:
            _act({"type": "uiClick", "index": c["i"]})
        return "inn_confirm", 3.0, {}

    rest = next((x for x in acts if "MouseEmbarkButton" in (x.get("path") or "")), None)
    if rest:
        if not dry_run:
            # 语义优先（按路径 → 按序号），坐标兜底。旅馆这个按钮是长按式的；
            # uiHold 只认 text（塞 path 时 mod 先比 label 再比 path）。
            hit = False
            for t in ({"type": "uiClick", "pick": rest.get("path") or ""},
                      {"type": "uiClick", "index": rest.get("i", -1)},
                      {"type": "uiHold", "text": rest.get("path") or "", "seconds": 0.6},
                      {"type": "uiHold", "index": rest.get("i"), "seconds": 0.6}):
                try:
                    r = _act(t)
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("ok"):
                    hit = True
                    break
            if not hit:
                _act({"type": "uiPoint", "x": rest["x"], "y": rest["y"], "holdMs": 150})
        return "inn_rest", 1.5, {"label": rest.get("text")}

    return "inn_idle", 1.5, {}
