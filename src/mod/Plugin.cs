using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Assets.Code.Actor;
using Assets.Code.Actor.Events;
using Assets.Code.Combat;
using Assets.Code.Combat.Events;
using BepInEx;
using BepInEx.Logging;
using Newtonsoft.Json;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace DD2TextMod
{
    [BepInPlugin("local.dd2.textmod", "DD2 Text Mod", "0.1.0")]
    public class Plugin : BaseUnityPlugin
    {
        internal static ManualLogSource Log;
        internal static readonly ConcurrentQueue<Action> MainQueue = new ConcurrentQueue<Action>();
        internal static HttpServer Server;
        internal static PumpBehaviour Pump;
        internal static string HoldAxis;
        private static Dictionary<string, float> _axisDict;
        private float _nextScan;

        private void Awake()
        {
            Log = Logger;
            Log.LogInfo("DD2 Text Mod loaded");
            try { Application.runInBackground = true; } catch { }
            try
            {
                // Unity 新版输入系统默认"失焦就禁用设备"，这就是后台时键鼠/手柄全失效的原因。
                UnityEngine.InputSystem.InputSystem.settings.backgroundBehavior =
                    UnityEngine.InputSystem.InputSettings.BackgroundBehavior.IgnoreFocus;
                Log.LogInfo("InputSystem backgroundBehavior = IgnoreFocus");
            }
            catch (Exception e) { Log.LogInfo("set backgroundBehavior failed: " + e.Message); }
            try
            {
                Server = new HttpServer(18082);
                Server.Start();
                var pumpGo = new GameObject("DD2TextModPump");
                UnityEngine.Object.DontDestroyOnLoad(pumpGo);
                pumpGo.hideFlags = HideFlags.HideAndDontSave;
                Pump = pumpGo.AddComponent<PumpBehaviour>();
                Log.LogInfo("HTTP listening on http://127.0.0.1:18082");
            }
            catch (Exception e)
            {
                Log.LogError("HTTP start failed: " + e);
            }
        }

        private void Update()
        {
            while (MainQueue.TryDequeue(out var action))
            {
                try { action(); }
                catch (Exception e) { Log.LogError("main-queue: " + e); }
            }

            // 自动挡（心跳式）：必须每帧补，不能挂在下面那个 1 s 扫描节流后面——
            // StageCoachInternalState.UpdateSpeed 在 NO_INPUT 分支每帧都会清掉前进标志。
            try { Coach.AutoTick(); } catch (Exception e) { Log.LogError("autotick: " + e.Message); }

            if (Time.unscaledTime < _nextScan) return;
            _nextScan = Time.unscaledTime + 1f;
            // 2026-11-21：Awake 里设的 runInBackground 可能被游戏/场景覆盖，
            // 每秒兜一次。这个 Update 能在后台跑，本身就说明设置是生效的。
            try { if (!Application.runInBackground) Application.runInBackground = true; } catch { }
            try
            {
                if (CombatBhv.Instance != null && CombatBhv.Instance.IsBattleRunning)
                {
                    var actor = CombatBhv.Instance.GetCurrentActor();
                    if (actor != null) Log.LogDebug("combat actor: " + actor.ActorName + " " + actor.ActorGuid);
                }
            }
            catch { }
        }

        private void OnDestroy()
        {
            Log?.LogInfo("DD2 Text Mod OnDestroy");
            
        }

        internal static void Run(System.Collections.IEnumerator routine)
        {
            if (Pump != null) Pump.StartCoroutine(routine);
        }

        // 每帧住 axisValues 里写值，模拟"一直按着前进"。
        internal static void ApplyHoldAxis()
        {
            if (HoldAxis == null) return;
            if (_axisDict == null)
            {
                var t = Type.GetType("Code.FlyingRat.Simulator.InputSimulatorUtils, IronCrown");
                if (t == null) return;
                _axisDict = t.GetField("axisValues", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)
                    ?.GetValue(null) as Dictionary<string, float>;
                if (_axisDict == null) return;
                if (!_axisDict.ContainsKey(HoldAxis)) _axisDict[HoldAxis] = 0f;
            }
            _axisDict[HoldAxis] = 1f;
        }

        // 超时取消闸：HTTP 线程置位，主线程在动作用户代码之前检查。
        // 必须 volatile：两个线程各写各的，没有它就只靠运气看见。
        private sealed class OnMainGate { public volatile bool Cancelled; }

        // 把请求切回 Unity 主线程；HTTP 线程只等结果。
        // 超时 = 这次请求大概率【根本没执行】（MainQueue 由主线程 FIFO 串行消费），
        // 所以超时必须把排队中的动作作废，否则它会晚一帧自己跑起来 —— 客户端以为失败、
        // 重试一次，于是同一个动作执行两遍（战斗里就是重复出招 / in_turn_perform 卡死）。
        internal static T OnMain<T>(Func<T> fn, int timeoutMs = 3000)
        {
            var tcs = new TaskCompletionSource<T>();
            var gate = new OnMainGate();
            MainQueue.Enqueue(() =>
            {
                if (gate.Cancelled) return;          // 已经超时作废，别再执行
                try { tcs.TrySetResult(fn()); }
                catch (Exception e) { tcs.TrySetException(e); }
            });
            if (!tcs.Task.Wait(timeoutMs))
            {
                gate.Cancelled = true;
                throw new TimeoutException("main thread timeout (cancelled)");
            }
            return tcs.Task.GetAwaiter().GetResult();
        }
    }

    internal class PumpBehaviour : MonoBehaviour
    {
        private void Update()
        {
            while (Plugin.MainQueue.TryDequeue(out var action))
            {
                try { action(); }
                catch (Exception e) { Plugin.Log?.LogError("pump: " + e); }
            }

            try { Plugin.ApplyHoldAxis(); }
            catch { }
        }

        private void OnDestroy()
        {
            Plugin.Log?.LogInfo("pump destroyed");
        }
    }

    internal static class Refl
    {
        public const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        public static object Field(object target, string name)
        {
            if (target == null) return null;
            var f = target.GetType().GetField(name, All);
            return f?.GetValue(target);
        }

        /// <summary>沿着基类一路找字段（Unity 的很多字段声明在泛型基类里）。</summary>
        public static object FieldDeep(object target, string name)
        {
            if (target == null) return null;
            var t = target.GetType();
            while (t != null && t != typeof(object))
            {
                var f = t.GetField(name, All);
                if (f != null)
                {
                    try { return f.GetValue(target); } catch { return null; }
                }
                t = t.BaseType;
            }
            return null;
        }

        public static object StaticField(Type type, string name)
        {
            var f = type.GetField(name, BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            return f?.GetValue(null);
        }

        public static object Call(object target, string name, params object[] args)
        {
            if (target == null) return null;
            var methods = target.GetType().GetMethods(All).Where(m => m.Name == name).ToArray();
            foreach (var m in methods)
            {
                var ps = m.GetParameters();
                if (ps.Length != args.Length) continue;
                try { return m.Invoke(target, args); } catch { }
            }
            return null;
        }
    }
}
