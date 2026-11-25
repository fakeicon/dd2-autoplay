using System;
using System.Collections.Generic;
using Assets.Code.Actor;
using Assets.Code.Condition;
using Assets.Code.Effect;
using Assets.Code.Game;
using Assets.Code.Inn.Events;
using Assets.Code.Item;
using Assets.Code.Item.Events;
using Assets.Code.Math;
using Assets.Code.Source;
using Assets.Code.Utils;

namespace DD2TextMod
{
    /// <summary>
    /// 旅馆物品（rest item）的"使用"：不走鼠标，直接按游戏自己的语义落效果。
    /// 依据 logs/decomp/all/Assets.Code.UI.Widgets/RestWidgetBhv.cs:379-498：
    ///   LockStagedItem → ApplyEffects（每个目标 ApplyItemEffects + 两两 ApplyItemCombinationEffects
    ///   + 全队 ApplyItemPartyEffects）→ RecordItemUseHistory + EventRestItemApplied
    ///   → EventInventoryItemConsumed → 消耗品 RemoveSingleItemAt。
    /// 跳过的是 UI/动画/VFX，以及 quirk actout 拦截（游戏里会中断这次使用，这里直接判为 blocked 不落效果）。
    /// </summary>
    internal static class RestItems
    {
        public static object Build()
        {
            var inv = Singleton<GameTypeMgr>.Instance?.PlayerInventory;
            if (inv == null) return new { ok = false, error = "PlayerInventory not found" };

            var party = PartyActors();
            int slots = SafeInt(() => inv.GetNumberOfTotalSlots());
            var items = new List<object>();
            for (int i = 0; i < slots; i++)
            {
                var it = inv.GetItem(i);
                if (it == null) continue;
                var def = it.GetItemDefinition();
                if (def == null || !def.m_usedInInn) continue;
                var blocked = new Dictionary<string, object>();
                foreach (var a in party)
                {
                    var why = BlockedBecause(a, def);
                    if (why != null) blocked[a.ActorGuid.ToString()] = why;
                }
                items.Add(new Dictionary<string, object>
                {
                    ["slot"] = i,
                    ["id"] = def.m_id,
                    ["title"] = SafeString(() => ItemDescription.GetTitle(def)),
                    ["tags"] = def.m_tags,
                    ["qty"] = SafeObject(() => it.GetQty()),
                    ["targets"] = SafeObject(() => def.GetNumberOfSelectableRestItemTargets()),
                    ["random"] = def.m_isRandomTarget,
                    ["forcedActor"] = def.m_forcedTargetActorDataId,
                    ["consumable"] = SafeObject(() => it.IsConsumable()),
                    ["blocked"] = blocked,
                });
            }

            return new Dictionary<string, object>
            {
                ["ok"] = true,
                ["action"] = "restItems",
                ["mode"] = SafeString(() => GameModeMgr.CurrentMode.ToString()),
                ["party"] = PartyJson(party),
                ["items"] = items,
            };
        }

        /// <summary>用一件旅馆物品。targetGuids 为空时：4 目标的全队物品直接给全队，其余要显式指定。</summary>
        public static object Use(string itemId, List<uint> targetGuids, bool dryRun)
        {
            var inv = Singleton<GameTypeMgr>.Instance?.PlayerInventory;
            if (inv == null) return new { ok = false, error = "PlayerInventory not found" };
            if (string.IsNullOrEmpty(itemId)) return new { ok = false, error = "itemId required" };

            int slot = -1;
            Assets.Code.Item.IReadOnlyItemInstance inst = null;
            ItemDefinition def = null;
            int slots = SafeInt(() => inv.GetNumberOfTotalSlots());
            for (int i = 0; i < slots; i++)
            {
                var it = inv.GetItem(i);
                if (it == null) continue;
                var d = it.GetItemDefinition();
                if (d == null || d.m_id != itemId) continue;
                slot = i;
                inst = it;
                def = d;
                break;
            }
            if (inst == null || def == null) return new { ok = false, error = "item not in inventory: " + itemId };
            if (!def.m_usedInInn) return new { ok = false, error = "not an inn item: " + itemId };

            var party = PartyActors();
            if (party.Count == 0) return new { ok = false, error = "no party actors" };

            var byGuid = new Dictionary<uint, ActorInstance>();
            foreach (var a in party) byGuid[a.ActorGuid] = a;

            var targets = new List<ActorInstance>();
            var unknown = new List<string>();
            if (targetGuids != null)
            {
                foreach (var g in targetGuids)
                {
                    if (byGuid.TryGetValue(g, out var a)) targets.Add(a);
                    else unknown.Add(g.ToString());
                }
            }

            int need = SafeInt(() => def.GetNumberOfSelectableRestItemTargets());
            if (targets.Count == 0)
            {
                if (need >= party.Count) targets.AddRange(party);
                else return new { ok = false, error = "need targets", targets = need, party = PartyJson(party) };
            }
            if (def.m_isRandomTarget && targets.Count > def.m_numberOfTargets)
                targets.RemoveRange(def.m_numberOfTargets, targets.Count - def.m_numberOfTargets);

            // 逐个目标查可用性（actout 拦截 / 条件 / 使用上限），有一个不行就整次不做，免得消耗了道具却没效果。
            var blocked = new List<object>();
            foreach (var t in targets)
            {
                var why = BlockedBecause(t, def);
                if (why != null) blocked.Add(new { guid = t.ActorGuid.ToString(), name = t.ActorName, why });
            }
            if (blocked.Count > 0)
                return new { ok = false, error = "target blocked", itemId, blocked, unknown };

            if (dryRun)
                return new
                {
                    ok = true,
                    dry = true,
                    itemId,
                    slot,
                    targets = targets.ConvertAll(t => t.ActorGuid.ToString()),
                    partyEffects = def.GetPartyEffects() != null && def.GetPartyEffects().Count > 0,
                };

            var applied = new List<string>();
            var error = (string)null;
            bool consumable = SafeBool(() => inst.IsConsumable());
            try
            {
                // 1) 每个目标自身：普通效果 + applyLimit 效果 + ActorDataEffects
                foreach (var t in targets)
                {
                    ApplyOne(t, t, def, def.GetEffects(), def.GetApplyLimitEffects(), def.m_effectApplyLimit,
                             SafeActorDataEffects(t));
                }
                // 2) 两两组合效果
                for (int i = 0; i < targets.Count; i++)
                    for (int j = i + 1; j < targets.Count; j++)
                        ApplyOne(targets[i], targets[j], def, def.GetCombinationEffects(),
                                 def.GetCombinationApplyLimitEffects(), def.m_combinationEffectApplyLimit, null);
                // 3) 全队效果（和选中谁无关）
                foreach (var a in party)
                    ApplyOne(a, a, def, def.GetPartyEffects(), def.GetPartyApplyLimitEffects(),
                             def.m_partyEffectApplyLimit, null);

                // 4) 记账 + 事件
                foreach (var t in targets)
                {
                    try { t.RecordItemUseHistory(def); } catch { }
                    try { EventRestItemApplied.Trigger(t.ActorGuid, def.m_id); } catch { }
                    applied.Add(t.ActorGuid.ToString());
                }
                try { EventInventoryItemConsumed.Trigger(inv, def); } catch { }

                // 5) 消耗
                if (consumable) inv.RemoveSingleItemAt(slot);
            }
            catch (Exception e)
            {
                error = (e.InnerException ?? e).Message;
            }

            return new
            {
                ok = error == null,
                action = "useRestItem",
                itemId,
                slot,
                targets = applied,
                consumed = error == null && consumable,
                unknown,
                error,
            };
        }

        private static void ApplyOne(ActorInstance performer, ActorInstance target, ItemDefinition def,
            IReadOnlyList<SourceDefinition<EffectDefinition>> effects,
            IReadOnlyList<SourceDefinition<EffectDefinition>> limitEffects,
            int applyLimit,
            ActorDataEffects actorDataEffects)
        {
            if (target == null) return;
            if ((effects == null || effects.Count == 0) &&
                (limitEffects == null || limitEffects.Count == 0) &&
                actorDataEffects == null) return;

            float chance = 1f;
            try { chance += target.GetClampedStatValue(ActorStatType.REST_ITEM_EFFECT_CHANCE_MODIFIER, def.m_tags); } catch { }
            if (!RandomContainer.CheckValue(RandomIdentifier.INN, chance)) return;

            var cond = new ConditionCalculation.Input(performer, target, SourceType.REST_ITEM, def.m_id);
            if (effects != null && effects.Count > 0)
                EffectApply.Apply(new AppliedEffects.Input<SourceDefinition<EffectDefinition>>(effects, performer, target, cond));
            if (limitEffects != null && limitEffects.Count > 0)
                EffectApply.Apply(new AppliedEffects.Input<SourceDefinition<EffectDefinition>>(limitEffects, applyLimit, performer, target, cond));
            if (actorDataEffects != null)
                EffectApply.Apply(new AppliedEffects.Input<SourceDefinition<EffectDefinition>>(performer, target, cond), actorDataEffects, ActorDataEffectType.REST_ITEM);
        }

        /// <summary>返回 null 表示这个英雄能接这件物品；否则返回不可用原因。</summary>
        private static string BlockedBecause(ActorInstance a, ItemDefinition def)
        {
            if (a == null || def == null) return "no actor";
            try
            {
                if (a.GetIsItemBlocked(def, out var block)) return "actout:" + (block != null ? block.m_Id : "?");
            }
            catch { }
            try { if (!a.GetIsItemConditionsMet(def)) return "condition"; } catch { }
            try { if (!a.GetIsItemUnderUseLimit(def)) return "use_limit"; } catch { }
            try { if (a.GetRestItemBlockedSourceActOut(def.m_tags) != null) return "actout_pending"; } catch { }
            return null;
        }

        private static List<ActorInstance> PartyActors()
        {
            var list = new List<ActorInstance>();
            try
            {
                var rm = Singleton<GameTypeMgr>.Instance?.RosterManager;
                if (rm != null)
                    foreach (var a in rm.GetPartyActors())
                        if (a != null) list.Add(a);
            }
            catch { }
            return list;
        }

        private static List<object> PartyJson(List<ActorInstance> party)
        {
            var outList = new List<object>();
            foreach (var a in party)
            {
                outList.Add(new Dictionary<string, object>
                {
                    ["guid"] = a.ActorGuid.ToString(),
                    ["name"] = SafeString(() => a.ActorName),
                    ["hp"] = SafeObject(() => a.HpRounded),
                    ["stress"] = SafeObject(() => a.Stress),
                });
            }
            return outList;
        }

        private static ActorDataEffects SafeActorDataEffects(ActorInstance a)
        {
            try { return a.ActorData.GetDataContainerSum<ActorDataEffects>(); } catch { return null; }
        }

        private static object SafeObject(Func<object> fn) { try { return fn(); } catch { return null; } }
        private static string SafeString(Func<string> fn) { try { return fn(); } catch { return null; } }
        private static int SafeInt(Func<int> fn) { try { return fn(); } catch { return 0; } }
        private static bool SafeBool(Func<bool> fn) { try { return fn(); } catch { return false; } }
    }
}
