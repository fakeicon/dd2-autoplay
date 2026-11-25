using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Assets.Code.Campaign;
using Assets.Code.UI.HeroSelect;
using UnityEngine;

namespace DD2TextMod
{
    /// <summary>
    /// 英雄选择界面：枚举可选英雄（头像），按 dataId 自动组队，再确认。
    /// 绕开拖拽 UI。
    /// </summary>
    internal static class Party
    {
        private const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        private static HeroSelectBhv Screen()
        {
            try { return UnityEngine.Object.FindObjectOfType<HeroSelectBhv>(); }
            catch { return null; }
        }

        private static object Field(object obj, string name)
        {
            if (obj == null) return null;
            var t = obj.GetType();
            while (t != null && t != typeof(object))
            {
                var f = t.GetField(name, All);
                if (f != null) { try { return f.GetValue(obj); } catch { return null; } }
                t = t.BaseType;
            }
            return null;
        }

        private static string ResourceId(object resourceActor)
        {
            if (resourceActor == null) return null;
            var v = Field(resourceActor, "m_Id") ?? Field(resourceActor, "m_ActorDataId") ?? Field(resourceActor, "m_IdOverride");
            if (v != null) return v.ToString();
            try
            {
                var mi = resourceActor.GetType().GetMethod("GetId", All);
                return mi?.Invoke(resourceActor, null)?.ToString();
            }
            catch { return null; }
        }

        /// <summary>从 guid 反查 dataId（走游戏自己的 ActorInstance 库）。</summary>
        private static string DataIdFromGuid(object guidObj)
        {
            try
            {
                if (guidObj == null) return null;
                var guid = Convert.ToUInt32(guidObj);
                if (guid == 0) return null;
                var actor = StateBuilder.FindActor(guid);
                return actor?.ActorDataId;
            }
            catch { return null; }
        }

        private static List<HeroSelectActorUIBhv> Uis()
        {
            try { return UnityEngine.Object.FindObjectsOfType<HeroSelectActorUIBhv>(true).ToList(); }
            catch { return new List<HeroSelectActorUIBhv>(); }
        }

        public static object Info()
        {
            var hs = Screen();
            if (hs == null) return new { ok = false, error = "hero select screen not open" };

            var heroes = new List<object>();
            var slots = new List<object>();
            foreach (var ui in Uis())
            {
                if (ui == null) continue;
                var isSlot = Field(ui, "m_IsRosterSlot") is bool b && b;
                var entry = new Dictionary<string, object>
                {
                    ["guid"] = Field(ui, "m_ActorGuid")?.ToString(),
                    ["dataId"] = ResourceId(Field(ui, "m_ResourceActor")) ?? DataIdFromGuid(Field(ui, "m_ActorGuid")),
                    ["index"] = Field(ui, "m_RosterIndex")?.ToString(),
                    ["locked"] = Field(ui, "m_locked")?.ToString(),
                };
                if (isSlot) slots.Add(entry); else heroes.Add(entry);
            }
            return new { ok = true, heroes, slots };
        }

        public static object Build(string dataIds, bool confirm)
        {
            var hs = Screen();
            if (hs == null) return new { ok = false, error = "hero select screen not open" };

            var wanted = (dataIds ?? "").Split(',').Select(s => s.Trim().ToLowerInvariant()).Where(s => s.Length > 0).ToList();
            if (wanted.Count == 0) return new { ok = false, error = "no dataIds" };

            var uis = Uis().Where(u => u != null && !(Field(u, "m_IsRosterSlot") is bool b && b)).ToList();
            var addMi = hs.GetType().GetMethod("AddToFirstPossibleSlot", All);
            var log = new List<string>();

            foreach (var id in wanted)
            {
                var match = uis.FirstOrDefault(u =>
                {
                    var rid = ((ResourceId(Field(u, "m_ResourceActor")) ?? DataIdFromGuid(Field(u, "m_ActorGuid"))) ?? "").ToLowerInvariant();
                    return rid == id || (rid.Length > 0 && rid.Contains(id));
                });
                if (match == null) { log.Add(id + ": 没有"); continue; }
                if (Field(match, "m_locked") is bool locked && locked) { log.Add(id + ": 锁着"); continue; }
                var guid = Field(match, "m_ActorGuid");
                try
                {
                    addMi?.Invoke(hs, new object[] { Convert.ToUInt32(guid) });
                    log.Add(id + ": 已加入");
                }
                catch (Exception e) { log.Add(id + ": 失败 " + (e.InnerException ?? e).Message); }
            }

            string confirmResult = null;
            if (confirm)
            {
                try
                {
                    hs.GetType().GetMethod("ConfirmRosterSelection", All)?.Invoke(hs, null);
                    confirmResult = "confirmed";
                }
                catch (Exception e) { confirmResult = "confirm failed: " + (e.InnerException ?? e).Message; }
            }
            return new { ok = true, log, confirm = confirmResult };
        }
    }
}

