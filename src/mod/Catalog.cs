using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Assets.Code;
using Assets.Code.Library;
using Assets.Code.Utils;
using Assets.Code.Skill;
using Newtonsoft.Json;

namespace DD2TextMod
{
    /// <summary>把游戏自己的技能定义（射程/冷却/多段/标记）读出来，不靠人背。</summary>
    internal static class Catalog
    {
        private const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        public static object Build(string onlyIds)
        {
            var lib = SingletonMonoBehaviour<Library<string, ActorDataSkill>>.Instance;
            if (lib == null) return new { ok = false, error = "skill library not found" };

            var dict = FindDict(lib);
            if (dict == null) return new { ok = false, error = "skill dictionary not found in library" };

            var wanted = string.IsNullOrEmpty(onlyIds)
                ? null
                : new HashSet<string>(onlyIds.Split(',').Select(s => s.Trim()).Where(s => s.Length > 0));

            var list = new List<object>();
            foreach (DictionaryEntry e in dict)
            {
                var id = e.Key as string;
                if (id == null) continue;
                if (wanted != null && !wanted.Contains(id)) continue;
                var sk = e.Value as ActorDataSkill;
                if (sk == null) continue;
                list.Add(Describe(sk, id));
            }
            list.Sort((a, b) => string.CompareOrdinal(((Dictionary<string, object>)a)["id"].ToString(), ((Dictionary<string, object>)b)["id"].ToString()));
            return new { ok = true, count = list.Count, skills = list };
        }

        private static object Describe(ActorDataSkill sk, string id)
        {
            var d = new Dictionary<string, object>
            {
                ["id"] = id,
                ["type"] = Safe(() => Refl.FieldDeep(sk, "m_Type")?.ToString()),
                ["friendly"] = Safe(() => Refl.FieldDeep(sk, "m_IsFriendly")),
                ["launch"] = Safe(() => Ints(Refl.FieldDeep(sk, "m_LaunchRanks"))),
                ["target"] = Safe(() => Ints(Refl.FieldDeep(sk, "m_TargetRanks"))),
                ["cd"] = Safe(() => Refl.FieldDeep(sk, "m_Cooldown")),
                ["multi"] = Safe(() => Refl.FieldDeep(sk, "m_IsMultiHit")),
                ["tags"] = Safe(() => Strings(Refl.FieldDeep(sk, "m_Tags"))),
                ["stats"] = Safe(() => DescribeStats(Refl.FieldDeep(sk, "m_ActorDataStats"))),
                ["effects"] = Safe(() => DescribeEffects(Refl.FieldDeep(sk, "m_ActorDataEffects"))),
            };
            return d;
        }

        private static object DescribeStats(object stats)
        {
            if (stats == null) return null;
            var d = new Dictionary<string, object>();
            foreach (var f in AllFields(stats.GetType()))
            {
                var n = f.Name;
                if (!(n.IndexOf("damage", StringComparison.OrdinalIgnoreCase) >= 0 ||
                      n.IndexOf("accuracy", StringComparison.OrdinalIgnoreCase) >= 0 ||
                      n.IndexOf("crit", StringComparison.OrdinalIgnoreCase) >= 0 ||
                      n.IndexOf("resist", StringComparison.OrdinalIgnoreCase) >= 0)) continue;
                object v = null;
                try { v = f.GetValue(stats); } catch { }
                if (v == null) continue;
                if (v is IFormattable || v is string) d[n] = v;
            }
            return d.Count == 0 ? null : d;
        }

        private static object DescribeEffects(object effects)
        {
            if (effects == null) return null;
            var list = new List<string>();
            foreach (var f in AllFields(effects.GetType()))
            {
                object v = null;
                try { v = f.GetValue(effects); } catch { }
                if (v is IEnumerable en && !(v is string))
                {
                    foreach (var item in en)
                    {
                        if (item == null) continue;
                        var iid = Safe(() => Refl.FieldDeep(item, "m_Id")) as string;
                        if (!string.IsNullOrEmpty(iid)) { list.Add(f.Name + ":" + iid); }
                    }
                }
            }
            return list.Count == 0 ? null : list.Distinct().Take(12).ToList();
        }

        // 2026-11-24：给 /state 每个技能算"还剩几回合冷却"用。ActorInstance.GetRemainingSkillCooldown
        // 要的是 ActorDataSkill 对象，而 m_CombatSkills 里的 SkillInstance 只存 id，所以得按 id 反查。
        // 只读缓存，不写任何游戏状态。
        private static Dictionary<string, ActorDataSkill> s_ById;

        public static ActorDataSkill Find(string id)
        {
            if (string.IsNullOrEmpty(id)) return null;
            if (s_ById == null)
            {
                var map = new Dictionary<string, ActorDataSkill>();
                var lib = SingletonMonoBehaviour<Library<string, ActorDataSkill>>.Instance;
                var dict = lib == null ? null : FindDict(lib);
                if (dict != null)
                {
                    foreach (DictionaryEntry e in dict)
                    {
                        var k = e.Key as string;
                        var v = e.Value as ActorDataSkill;
                        if (k != null && v != null) map[k] = v;
                    }
                }
                s_ById = map;
            }
            ActorDataSkill found;
            return s_ById.TryGetValue(id, out found) ? found : null;
        }

        private static IDictionary FindDict(object obj)
        {
            foreach (var f in AllFields(obj.GetType()))
            {
                object v = null;
                try { v = f.GetValue(obj); } catch { continue; }
                if (v is IDictionary d && v.GetType().IsGenericType)
                {
                    var args = v.GetType().GetGenericArguments();
                    if (args.Length == 2 && args[0] == typeof(string)) return d;
                }
            }
            return null;
        }

        private static IEnumerable<FieldInfo> AllFields(Type t)
        {
            while (t != null && t != typeof(object))
            {
                foreach (var f in t.GetFields(All)) yield return f;
                t = t.BaseType;
            }
        }

        private static object Ints(object list)
        {
            if (list is IEnumerable en && !(list is string))
                return en.Cast<object>().Select(x => x?.ToString()).Where(x => x != null).ToList();
            return null;
        }

        private static object Strings(object list)
        {
            if (list is IEnumerable en && !(list is string))
                return en.Cast<object>().Select(x => x?.ToString()).Where(x => x != null).ToList();
            return null;
        }

        private static object Safe(Func<object> fn)
        {
            try { return fn(); } catch { return null; }
        }
    }
}

