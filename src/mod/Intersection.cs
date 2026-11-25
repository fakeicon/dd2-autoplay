using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using UnityEngine;

namespace DD2TextMod
{
    /// <summary>
    /// 岔路口（RoadIntersection）读 + 选。
    ///
    /// 2026-11-20 02:0x 现场：车到岔路会变成
    ///   drivingMode=NO_INPUT / inputEnabled=False / speed 残留 12.1（然后慢慢降到 0）
    /// 因为 TriggerIntersectionBhv.GetCanDrive() 返回 False，把车的输入整体锁死，
    /// 等玩家去点路面上的路标（RoadIndicatorUIBhv，WorldSpace 画布，长按确认）。
    /// 实测：w/a/d、simclick 各种动作名、pointerClick 盲点屏幕 —— 全部零反应；
    /// runner 只能看着 speed=12.1 一路按 w，最后判 STUCK 退出。
    ///
    /// 这里直接走游戏内部那条路：拿 TriggerIntersectionBhv 的 m_PreviewIcons，
    /// 用图标自己的 (m_intersectionRoad, m_nodeIndexInRow, m_intersectionOptionIndex,
    /// m_indicatorNode) 去调 EventRoadIntersectionOptionSelected.Trigger(...)，
    /// 和"玩家点了那个路标"完全等价（MapMgrBhv / TriggerIntersectionBhv 都监听它）。
    /// 事件路线失败才退回调图标自己的 OnClick()。
    /// </summary>
    internal static class Intersection
    {
        private const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        private sealed class Opt
        {
            public Component Icon;
            public int Option;
            public int NodeIndex;
            public string Type;
            public bool Revealed;
            public bool Triggered;
            public bool Active;
        }

        private static object _cached;
        private static float _cachedAt;

        /// <summary>当前岔路的状态 + 可选路线（没在岔路就 found=false）。</summary>
        public static object Info()
        {
            var t = Trigger();
            if (t == null) return new { ok = true, found = false };
            var opts = Options(t);
            return new
            {
                ok = true,
                found = true,
                state = StateName(t),
                selected = Int(Refl.FieldDeep(t, "m_SelectedIntersectionOptionIndex")),
                canDrive = Method(t, "GetCanDrive"),
                count = opts.Count,
                options = opts.Select(o => new
                {
                    i = o.Option,
                    node = o.NodeIndex,
                    type = o.Type,
                    revealed = o.Revealed,
                    triggered = o.Triggered,
                    active = o.Active,
                }).ToList(),
            };
        }

        /// <summary>选第 index 条路（按 m_intersectionOptionIndex 匹配，其次按节点序号，最后取第一条）。</summary>
        public static object Select(int index)
        {
            var t = Trigger();
            if (t == null) return new { ok = false, error = "no intersection trigger" };
            var opts = Options(t);
            if (opts.Count == 0)
                return new { ok = false, error = "no preview icons", state = StateName(t) };

            var pick = opts.FirstOrDefault(o => o.Option == index)
                    ?? opts.FirstOrDefault(o => o.NodeIndex == index)
                    ?? opts[0];

            var evt = Inspect.FindType("EventRoadIntersectionOptionSelected");
            var mi = evt?.GetMethods(All).FirstOrDefault(m => m.Name == "Trigger" && m.GetParameters().Length == 4);
            var road = Refl.FieldDeep(pick.Icon, "m_intersectionRoad");
            var node = Refl.FieldDeep(pick.Icon, "m_indicatorNode");

            string via = "event";
            string err = null;
            if (mi != null)
            {
                try { mi.Invoke(null, new object[] { road, pick.NodeIndex, pick.Option, node }); }
                catch (Exception e) { err = (e.InnerException ?? e).Message; }
            }
            else err = "Trigger(4) not found";

            // 事件路线不行 → 直接点那个路标（很多版本 OnClick 里才走 Confirm 流程）
            if (err != null)
            {
                via = "onClick";
                try { CallNoArgs(pick.Icon, "OnClick"); }
                catch (Exception e2)
                {
                    return new { ok = false, error = err + " / " + (e2.InnerException ?? e2).Message };
                }
            }
            return new
            {
                ok = true,
                via,
                picked = pick.Option,
                node = pick.NodeIndex,
                type = pick.Type,
                state = StateName(t),
                selected = Int(Refl.FieldDeep(t, "m_SelectedIntersectionOptionIndex")),
            };
        }

        // ---------------------------------------------------------------- 内部

        /// <summary>找"正在挡车"的那个岔路触发器（没有 false 的就取最后一个可见的）。</summary>
        private static object Trigger()
        {
            if (_cached != null && Time.realtimeSinceStartup - _cachedAt < 3f) return _cached;
            var type = Inspect.FindType("TriggerIntersectionBhv");
            if (type == null) return null;

            object fallback = null;
            foreach (var mb in UnityEngine.Object.FindObjectsOfType<MonoBehaviour>(true))
            {
                if (mb == null || mb.GetType() != type) continue;
                var go = mb.gameObject;
                if (go == null || !go.activeInHierarchy) continue;
                fallback = mb;
                if (Method(mb, "GetCanDrive") is bool b && !b) { fallback = mb; break; }
            }
            _cached = fallback;
            _cachedAt = Time.realtimeSinceStartup;
            return _cached;
        }

        private static List<Opt> Options(object trigger)
        {
            var list = new List<Opt>();
            if (!(Refl.FieldDeep(trigger, "m_PreviewIcons") is IEnumerable icons)) return list;
            foreach (var o in icons)
            {
                if (!(o is Component c) || c == null) continue;
                list.Add(new Opt
                {
                    Icon = c,
                    Option = Int(Refl.FieldDeep(c, "m_intersectionOptionIndex")),
                    NodeIndex = Int(Refl.FieldDeep(c, "m_nodeIndexInRow")),
                    Type = Refl.FieldDeep(c, "m_nodeType")?.ToString(),
                    Revealed = Bool(Refl.FieldDeep(c, "m_revealed")),
                    Triggered = Bool(Refl.FieldDeep(c, "m_triggered")),
                    Active = c.gameObject != null && c.gameObject.activeInHierarchy,
                });
            }
            return list;
        }

        private static string StateName(object trigger)
        {
            var st = Refl.FieldDeep(trigger, "m_CurrentState");
            if (st == null) return null;
            try { return Method(st, "GetName")?.ToString() ?? st.ToString(); }
            catch { return st.ToString(); }
        }

        private static object Method(object target, string name)
        {
            try
            {
                var mi = target.GetType().GetMethods(All).FirstOrDefault(m => m.Name == name && m.GetParameters().Length == 0);
                return mi?.Invoke(target, null);
            }
            catch { return null; }
        }

        private static void CallNoArgs(object target, string name)
        {
            var mi = target.GetType().GetMethods(All).FirstOrDefault(m => m.Name == name && m.GetParameters().Length == 0);
            if (mi == null) throw new MissingMethodException(target.GetType().Name + "." + name);
            mi.Invoke(target, null);
        }

        private static int Int(object v)
        {
            try { return v == null ? -1 : Convert.ToInt32(v); } catch { return -1; }
        }

        private static bool Bool(object v)
        {
            try { return v != null && Convert.ToBoolean(v); } catch { return false; }
        }
    }
}
