using System.Collections;
using Code.FlyingRat.Simulator;

namespace DD2TextMod
{
    /// <summary>
    /// 用**游戏自己的输入动作模拟器**（Code.FlyingRat.Simulator.InputSimulatorUtils）触发
    /// 一个已注册的输入动作（Submit / ExitMenu / PauseMenu / CharacterSheet / TakeAllLoot …）。
    ///
    /// 这不是模拟键鼠：SimulateActionStart/End 直接调用游戏自己 InputSystemBhv.AddListener
    /// 注册过的回调（CommonUiBhv 的 HandleGoBack、各屏的 OnSubmit 等），
    /// 与 InputSystem 里的真实设备无关，也不需要真实光标/焦点。
    /// 游戏自带的回归/演示工具（FlyingRat 模拟器）走的就是这条路。
    /// </summary>
    internal static class SimAction
    {
        /// <summary>点一次某个输入动作（start → 等一帧 → end）。</summary>
        public static object Click(string name)
        {
            return Fire(name, "click");
        }

        /// <summary>
        /// phase = click（默认）/ start / end。
        /// 长按类交互（弹窗的"按住前进"）要 start → 等一会儿 → end，拆成两次调用。
        /// </summary>
        public static object Fire(string name, string phase)
        {
            if (string.IsNullOrEmpty(name)) return new { ok = false, error = "name required" };
            int bound = Bound(name);
            if (bound == 0)
                return new { ok = false, error = "action not bound: " + name, name, bound, phase };

            try
            {
                if (phase == "start") InputSimulatorUtils.SimulateActionStart(name);
                else if (phase == "end") InputSimulatorUtils.SimulateActionEnd(name);
                else Plugin.Run(ClickRoutine(name));
            }
            catch (System.Exception e)
            {
                return new { ok = false, error = e.Message, name, bound, phase };
            }
            return new { ok = true, action = "simAction", name, bound, phase };
        }

        /// <summary>游戏这个动作当前绑了几个回调（0 = 这一屏没有这个动作，换了也没用）。</summary>
        public static int Bound(string name)
        {
            try
            {
                if (InputSimulatorUtils.simulatorActions != null
                    && InputSimulatorUtils.simulatorActions.TryGetValue(name, out var list))
                    return list?.Count ?? 0;
            }
            catch { }
            return 0;
        }

        private static IEnumerator ClickRoutine(string name)
        {
            try { InputSimulatorUtils.SimulateActionStart(name); } catch { }
            yield return null;   // 让游戏处理一帧（按下与抬起本来就不在同一帧）
            try { InputSimulatorUtils.SimulateActionEnd(name); } catch { }
        }
    }
}
