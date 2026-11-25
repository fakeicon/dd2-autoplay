using System;
using Assets.Code.Actor;
using Assets.Code.Combat;
using Assets.Code.UI;

namespace DD2TextMod
{
    /// <summary>
    /// 战斗里的"取消选择"（原来靠右键点屏幕正中 / Esc）：
    ///  ① 清掉当前出手人挂着的 skill+target（ActorInstance.ResetSelection）；
    ///  ② 让技能栏回到"选技能"状态（SkillSelectionBhv.CancelTargetSelection）。
    /// 两条都是游戏自己的方法，不发任何键鼠。
    /// </summary>
    internal static class CombatUi
    {
        public static object CancelSelection()
        {
            bool running = false, reset = false, ui = false;
            string selected = null, err = null;
            try
            {
                var cb = CombatBhv.Instance;
                running = cb != null && cb.IsBattleRunning;
                if (running)
                {
                    ActorInstance actor = null;
                    try { actor = cb.GetCurrentActor(); } catch { }
                    if (actor != null)
                    {
                        try { selected = actor.GetSelectedSkillId(); } catch { }
                        try
                        {
                            if (actor.GetIsSelectionComplete())
                            {
                                actor.ResetSelection();
                                reset = true;
                            }
                        }
                        catch { }
                    }
                }
                var bhv = UnityEngine.Object.FindObjectOfType<SkillSelectionBhv>();
                if (bhv != null)
                {
                    bhv.CancelTargetSelection();
                    ui = true;
                }
            }
            catch (Exception e) { err = (e.InnerException ?? e).Message; }
            return new { ok = err == null, battle = running, reset, ui, selected, error = err };
        }
    }
}
