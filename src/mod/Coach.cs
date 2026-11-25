using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Assets.Code.Game;
using Assets.Code.Map;
using Assets.Code.UI.Managers;
using Assets.Code.Utils;
using UnityEngine;

namespace DD2TextMod
{
    /// <summary>
    /// 马车（StageCoachVehicleControlBhv）的活体状态与控制。
    /// 目的：把"为什么开不动 / 岔路选了哪条"读出来，并且由我们（而不是游戏 AI）做路线决策。
    /// </summary>
    internal static class Coach
    {
        private const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        public static object Vehicle()
        {
            try
            {
                var mgr = SingletonMonoBehaviour<MapMgrBhv>.Instance;
                if (mgr == null) return null;
                var v = Refl.FieldDeep(mgr, "m_stagecoachBhv");
                if (v != null) return v;
                return mgr.GetVehicleControl();
            }
            catch { return null; }
        }

        public static object Info()
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var st = Refl.FieldDeep(v, "m_state");
            float px = 0f, py = 0f, pz = 0f;
            try
            {
                var tr = (v as UnityEngine.Component)?.transform;
                if (tr != null) { px = tr.position.x; py = tr.position.y; pz = tr.position.z; }
            }
            catch { }
            return new
            {
                ok = true,
                posX = px,
                posY = py,
                posZ = pz,
                inputEnabled = S(v, "IsPlayerInputEnabled"),
                driving = S(v, "IsPlayerDriving"),
                speed = M(v, "GetSpeed"),
                alwaysForward = M(v, "GetAlwaysMoveForward"),
                noInputReason = S(v, "m_noInputReasonLocKey"),
                intersectionCount = S(v, "m_intersectionCount"),
                intersectionIndex = S(v, "m_intersectionSelectedOptionIndex"),
                narrationCount = S(v, "m_narrationPanelCount"),
                canUpdateCamera = S(v, "m_canUpdateCameraTargets"),
                canDrive = CanDriveResults(v),
                st = new
                {
                    drivingMode = SF(st, "m_drivingMode"),
                    inputAlwaysMoveForward = SF(st, "m_inputAlwaysMoveForward"),
                    inNarrationPanel = SF(st, "m_inNarrationPanel"),
                    onForcedStop = SF(st, "m_onForcedStop"),
                    stoppingAtBarricade = SF(st, "m_isStoppingAtBarricade"),
                    onReachedTarget = SF(st, "m_onReachedTarget"),
                    disableTurning = SF(st, "m_disableTurning"),
                    speed = SF(st, "m_spd"),
                    accelMult = SF(st, "m_accelMult"),
                },
                type = v.GetType().Name,
            };
        }

        /// <summary>把马车拉回最近的路上（游戏自带的 "回到驿站马车" 逻辑）。</summary>
        public static object ResetCoach()
        {
            try
            {
                var mgr = SingletonMonoBehaviour<MapMgrBhv>.Instance;
                if (mgr == null) return new { ok = false, error = "map mgr not found" };
                var r = mgr.TryResetStagecoach();
                return new { ok = true, result = r.ToString() };
            }
            catch (Exception e) { return new { ok = false, error = e.Message }; }
        }
        private static object CanDriveResults(object v)
        {
            try
            {
                var list = Refl.FieldDeep(v, "m_CanDriveDelegates") as IEnumerable;
                if (list == null) return null;
                var outList = new List<string>();
                foreach (var d in list)
                {
                    if (d == null) continue;
                    try
                    {
                        var del = d as Delegate;
                        var name = (del?.Method?.DeclaringType?.Name ?? "?") + "." + (del?.Method?.Name ?? "?");
                        outList.Add(name + "=" + (del?.DynamicInvoke()?.ToString() ?? "null"));
                    }
                    catch (Exception e) { outList.Add("err=" + (e.InnerException ?? e).Message); }
                }
                return outList;
            }
            catch { return null; }
        }

        /// <summary>把返回 false 的 CanDrive 委托摘掉（= 解除"不让开"的锁）。</summary>
        public static object Unblock()
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var list = Refl.FieldDeep(v, "m_CanDriveDelegates") as IList;
            if (list == null) return new { ok = false, error = "m_CanDriveDelegates not found" };
            var unreg = v.GetType().GetMethod("UnregisterCanDriveDelegate", All);
            var removed = new List<string>();
            for (int i = list.Count - 1; i >= 0; i--)
            {
                var del = list[i] as Delegate;
                if (del == null) continue;
                bool res;
                try { res = (bool)del.DynamicInvoke(); }
                catch { res = true; }
                if (res) continue;
                var name = (del.Method.DeclaringType?.Name ?? "?") + "." + del.Method.Name;
                try { unreg?.Invoke(v, new object[] { del }); removed.Add(name); }
                catch (Exception e) { removed.Add(name + "!err:" + (e.InnerException ?? e).Message); }
            }
            return new { ok = true, removed, inputEnabled = S(v, "IsPlayerInputEnabled"), mode = SF(Refl.FieldDeep(v, "m_state"), "m_drivingMode") };
        }

        /// <summary>让马车直接开往某个坐标（我决定路线，车只负责走）。</summary>
        public static object GoTo(float x, float z)
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var mi = v.GetType().GetMethods(All).FirstOrDefault(m => m.Name == "GoToTarget");
            if (mi == null) return new { ok = false, error = "GoToTarget not found" };
            var target = new Vector3(x, 0f, z);
            var ps = mi.GetParameters();
            var args = new object[ps.Length];
            args[0] = target;
            for (int i = 1; i < ps.Length; i++)
            {
                args[i] = ps[i].HasDefaultValue ? ps[i].DefaultValue : (ps[i].ParameterType.IsValueType ? Activator.CreateInstance(ps[i].ParameterType) : null);
            }
            if (ps.Length > 1 && ps[1].ParameterType == typeof(bool)) args[1] = true;
            try
            {
                var r = mi.Invoke(v, args);
                return new { ok = true, action = "coachGo", x, z, result = r?.ToString(), inputEnabled = S(v, "IsPlayerInputEnabled"), speed = M(v, "GetSpeed") };
            }
            catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
        }

        /// <summary>直接把驾驶模式设成 PLAYER_INPUT / GO_TO_TARGET / NO_INPUT。</summary>
        public static object SetMode(string mode)
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var st = Refl.FieldDeep(v, "m_state");
            if (st == null) return new { ok = false, error = "m_state not found" };
            var f = FindField(st.GetType(), "m_drivingMode");
            if (f == null) return new { ok = false, error = "m_drivingMode not found" };
            try
            {
                var en = Enum.Parse(f.FieldType, mode, true);
                f.SetValue(st, en);
                var vf = FindField(v.GetType(), "m_state");
                vf.SetValue(v, st);
                var check = Refl.FieldDeep(v, "m_state");
                var now = FindField(check.GetType(), "m_drivingMode")?.GetValue(check);
                return new { ok = true, mode = now?.ToString(), inputEnabled = S(v, "IsPlayerInputEnabled") };
            }
            catch (Exception e) { return new { ok = false, error = e.Message }; }
        }

        /// <summary>
        /// 马车"往前开"（原来是 key W 按一下）。
        /// 直接置 StageCoachInternalState.m_inputAlwaysMoveForward：UpdateSpeed 在 PLAYER_INPUT
        /// 模式下读到它为 true 就把 m_accelMult 当 1（= 一直按着前进），全程不碰键鼠。
        /// holdMs &gt; 0 时到点自动松开（等价于"点一下 W 就松手"）。
        /// 注意：若车当时处于 FORCE_STOP / GO_TO_TARGET，UpdateSpeed 会自己清掉这个标志，
        /// 那是游戏在按它自己的逻辑开，我们不该硬掰。
        /// </summary>
        public static object Drive(bool on, int holdMs)
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var st = Refl.FieldDeep(v, "m_state");
            if (st == null) return new { ok = false, error = "m_state not found" };
            var f = FindField(st.GetType(), "m_inputAlwaysMoveForward");
            if (f == null) return new { ok = false, error = "m_inputAlwaysMoveForward not found" };
            try { f.SetValue(st, on); }
            catch (Exception e) { return new { ok = false, error = e.Message }; }
            // 每次 Drive 都换一个令牌：上一轮的自动松开协程如果还没到点，直接作废。
            // （runner 每 2~2.5 秒调一次、holdMs 900~1500，会重叠；没令牌的话先到点的那次
            //   会把后一次的前进提前掐掉 —— 2026-11-23 自查第一遍发现。）
            int token = ++_driveToken;
            if (on && holdMs > 0) Plugin.Run(ReleaseAfter(holdMs, token));
            return new
            {
                ok = true,
                action = "wagonDrive",
                on,
                alwaysForward = S(st, "m_inputAlwaysMoveForward"),
                mode = SF(st, "m_drivingMode"),
                speed = M(v, "GetSpeed"),
                inputEnabled = S(v, "IsPlayerInputEnabled"),
            };
        }

        private static int _driveToken;

        private static float _autoUntil;

        /// <summary>
        /// 自动挡开关（心跳式）。on=true 只续 4 秒：runner 每个 tick 续一次，
        /// runner 死了 4 秒后自己松手，不会留下"车一直自己开"。
        /// </summary>
        public static object Auto(bool on)
        {
            _autoUntil = on ? Time.unscaledTime + 4f : 0f;
            var v = Vehicle();
            return new
            {
                ok = true,
                action = "driveAuto",
                on,
                until = _autoUntil,
                speed = M(v, "GetSpeed"),
                alwaysForward = v == null ? null : S(v, "GetAlwaysMoveForward"),
            };
        }

        /// <summary>
        /// 由 Plugin.Update 每帧调用（掉帧也要补，不然 UpdateSpeed 会把标志清掉）：
        /// 心跳期内满足「在跑图 / 可驾驶 / 没有 UI 遮挡」时，把驾驶模式拉回 PLAYER_INPUT
        /// 并保持"按住前进"。依据：StageCoachInternalState.UpdateSpeed:185-218 —— 只有
        /// PLAYER_INPUT 下 GetAlwaysMoveForward() 才会被当成一直按着 W；NO_INPUT 分支
        /// (:247-251) 每帧都会把这个标志清成 false，所以必须逐帧补。
        /// 节点交互(L2101 RegisterCanDriveDelegate(BlockInput)) 和 UI 遮挡时不碰，
        /// 等它们自己结束（那才是游戏真正要停车的地方）。
        /// </summary>
        public static void AutoTick()
        {
            if (_autoUntil <= 0f) return;
            if (Time.unscaledTime > _autoUntil) { _autoUntil = 0f; return; }
            try
            {
                var mgr = SingletonMonoBehaviour<MapMgrBhv>.Instance;
                if (mgr == null || !mgr.IsInDrivingState()) return;
                var v = Vehicle();
                if (v == null) return;
                if (!Truthy(Val(v, "IsPlayerInputEnabled"))) return;
                var ui = SingletonMonoBehaviour<CommonUiBhv>.Instance;
                if (ui != null && ui.IsDrivingInputBlockerActive) return;
                var st = Refl.FieldDeep(v, "m_state");
                if (st == null) return;
                var mf = FindField(st.GetType(), "m_drivingMode");
                if (mf != null)
                {
                    string cur = mf.GetValue(st)?.ToString();
                    if (cur == "NO_INPUT") mf.SetValue(st, Enum.Parse(mf.FieldType, "PLAYER_INPUT"));
                    else if (cur != "PLAYER_INPUT") return;   // FORCE_STOP / GO_TO_TARGET：游戏自己在开，别抢
                }
                FindField(st.GetType(), "m_inputAlwaysMoveForward")?.SetValue(st, true);
            }
            catch { }
        }

        private static bool Truthy(object v)
        {
            if (v == null) return false;
            if (v is bool b) return b;
            return string.Equals(v.ToString(), "True", StringComparison.OrdinalIgnoreCase);
        }

        private static void SetForward(bool on)
        {
            var v = Vehicle();
            if (v == null) return;
            var st = Refl.FieldDeep(v, "m_state");
            if (st == null) return;
            var f = FindField(st.GetType(), "m_inputAlwaysMoveForward");
            f?.SetValue(st, on);
        }

        private static IEnumerator ReleaseAfter(int ms, int token)
        {
            yield return new WaitForSecondsRealtime(ms / 1000f);
            if (token != _driveToken) yield break;      // 后面又有一次 Drive，交给它
            try { SetForward(false); } catch { }
        }

        private static string SF(object structObj, string field)
        {
            try
            {
                if (structObj == null) return null;
                var f = FindField(structObj.GetType(), field);
                var val = f?.GetValue(structObj);
                return val?.ToString();
            }
            catch { return null; }
        }

        /// <summary>无参方法调用，返回字符串。</summary>
        private static string M(object target, string method)
        {
            try
            {
                var mi = target.GetType().GetMethods(All).FirstOrDefault(m => m.Name == method && m.GetParameters().Length == 0);
                return mi?.Invoke(target, null)?.ToString();
            }
            catch { return null; }
        }

        /// <summary>把任意成员读成字符串，绝不抛。</summary>
        private static string S(object target, string member)
        {
            try
            {
                var val = Val(target, member);
                return val == null ? null : val.ToString();
            }
            catch { return null; }
        }

        private static object Val(object target, string member)
        {
            try
            {
                var t = target.GetType();
                var p = t.GetProperty(member, All);
                if (p != null) return p.GetValue(target);
                var f = FindField(t, member);
                return f?.GetValue(target);
            }
            catch { return null; }
        }

        private static FieldInfo FindField(Type t, string name)
        {
            while (t != null && t != typeof(object))
            {
                var f = t.GetField(name, All);
                if (f != null) return f;
                t = t.BaseType;
            }
            return null;
        }

        /// <summary>调马车上的无参/单参方法。</summary>
        public static object CallMethod(string method, object arg)
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var methods = v.GetType().GetMethods(All).Where(m => m.Name == method).ToArray();
            if (methods.Length == 0) return new { ok = false, error = "method not found: " + method };
            foreach (var m in methods)
            {
                var ps = m.GetParameters();
                if (ps.Length > 1) continue;
                try
                {
                    object[] args = ps.Length == 0 ? null : new[] { ConvertArg(arg, ps[0].ParameterType) };
                    var r = m.Invoke(v, args);
                    return new { ok = true, method, result = r?.ToString(), inputEnabled = S(v, "IsPlayerInputEnabled"), speed = M(v, "GetSpeed") };
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
            }
            return new { ok = false, error = "no overload" };
        }

        /// <summary>直接写字段（岔路选项、状态等）。</summary>
        public static object SetField(string field, object value)
        {
            var v = Vehicle();
            if (v == null) return new { ok = false, error = "stagecoach not found" };
            var f = FindField(v.GetType(), field);
            if (f == null) return new { ok = false, error = "field not found: " + field };
            try
            {
                f.SetValue(v, ConvertArg(value, f.FieldType));
                return new { ok = true, field, now = f.GetValue(v)?.ToString() };
            }
            catch (Exception e) { return new { ok = false, error = e.Message }; }
        }

        private static object ConvertArg(object v, Type t)
        {
            if (v == null) return null;
            if (t == typeof(string)) return Convert.ToString(v);
            if (t == typeof(bool)) return Convert.ToBoolean(v);
            if (t == typeof(int)) return Convert.ToInt32(v);
            if (t == typeof(float)) return Convert.ToSingle(v);
            if (t.IsEnum) return Enum.Parse(t, Convert.ToString(v), true);
            return v;
        }
    }
}

