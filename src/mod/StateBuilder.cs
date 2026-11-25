using System;
using System.Collections;
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
using Assets.Code.Embark;
using Assets.Code.Game;
using Assets.Code.Library;
using Assets.Code.Map;
using Assets.Code.Map.Generation;
using Assets.Code.Utils;
using Newtonsoft.Json;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace DD2TextMod
{
    internal static class StateBuilder
    {
        private const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        public static object Build()
        {
            var root = new Dictionary<string, object>
            {
                ["ok"] = true,
                ["plugin"] = "dd2-textmod",
                ["version"] = "0.1.0",
                ["scene"] = SceneManager.GetActiveScene().name,
                ["time"] = DateTime.Now.ToString("HH:mm:ss"),
                ["timeScale"] = Time.timeScale,
                // 2026-11-21：排查"失焦后游戏不动"。三个数一起看：
                // runInBackground=false → Awake 里那次设置被覆盖了；
                // runInBackground=true 但 frame 不涨 → Unity 自己也停了更新；
                // frame 在涨 → 停的是游戏逻辑，不是引擎。
                ["runInBackground"] = Application.runInBackground,
                ["focused"] = Application.isFocused,
                ["frame"] = Time.frameCount,
                ["deltaTime"] = Math.Round(Time.deltaTime, 4),
                ["gameMode"] = Safe(() => Assets.Code.Game.GameModeMgr.CurrentMode.ToString()),
            };

            try
            {
                var inv = Singleton<GameTypeMgr>.Instance?.PlayerInventory;
                if (inv != null)
                {
                    root["gold"] = Safe(() => inv.GetGoldQty());
        // 火炬值（0~100）。作者口径 2026-11-20：火炬快灭了要加亮度，但我们一直读不到这个数。
        // 读的是驿站马车 HUD 上的火炬控件私有字段 m_curValue（0.8s 后追上 target），
        // 比去猜 TorchManager/RunValues 的字典键可靠。
        root["torch"] = TorchValue();
                    var items = new List<object>();
                    foreach (var item in inv.GetItems())
                    {
                        if (item == null) continue;
                        items.Add(new Dictionary<string, object>
                        {
                            ["id"] = Safe(() => item.GetItemDefinition()?.GetKey()),
                            ["name"] = Safe(() => item.GetName()),
                            ["qty"] = Safe(() => item.GetQty()),
                        });
                        if (items.Count >= 24) break;
                    }
                    root["items"] = items;
                }
            }
            catch { }

            try
            {
                var combat = CombatBhv.Instance;
                bool inBattle = combat != null && combat.IsBattleRunning;
                root["mode"] = inBattle ? "combat" : "out_of_combat";

                if (inBattle)
                {
                    var battle = Refl.Field(combat, "m_Battle");
                    var teams = Refl.Field(battle, "m_BattleTeams");
                    root["battleState"] = Safe(() => combat.CurrentBattleState?.ToString());
                    root["round"] = Safe(() => combat.CurrentRound);
                    root["turn"] = Safe(() => combat.CurrentTurn);
                    root["currentActorGuid"] = Safe(() => combat.CurrentActorGuid);
                    root["currentActor"] = Safe(() => combat.GetCurrentActor()?.ActorName);
                    root["partyInBattle"] = Safe(() => combat.IsPartyInBattle);

                    var actors = new List<object>();
                    if (teams != null)
                    {
                        for (int teamIndex = 0; teamIndex < 2; teamIndex++)
                        {
                            var team = Refl.Call(teams, "GetTeam", teamIndex);
                            var list = Refl.Field(team, "m_Actors") as System.Collections.IEnumerable;
                            if (list == null) continue;
                            foreach (var item in list)
                            {
                                var actor = item as ActorInstance;
                                if (actor == null) continue;
                                actors.Add(ActorState(actor, teamIndex));
                            }
                        }
                    }
                    root["actors"] = actors;
                    root["hint"] = "combat: use POST /action {type:'skill', actorGuid, skillId, targetGuid}; skills are listed per actor";
                }
                else
                {
                    var embark = EmbarkBhv.Instance;
                    if (embark != null && embark.EmbarkIsStarted)
                    {
                        root["mode"] = "embark";
                        root["embarkCamp"] = Safe(() => embark.IsCamp);
                        root["hint"] = "embark screen: POST /action {type:'embark'} to start";
                    }
                    else
                    {
                        var map = MapMgrBhv.Instance;
                        if (map != null && map.IsPlayable())
                        {
                            root["mode"] = "map";
                            root["mapState"] = Safe(() => map.CurrentState.ToString());
                            var vehicle = map?.GetVehicleControl();
                            if (vehicle != null)
                            {
                                root["speed"] = Safe(() => vehicle.GetSpeed());
                                root["alwaysMoveForward"] = Safe(() => vehicle.GetAlwaysMoveForward());
                                root["playerDriving"] = Safe(() => vehicle.IsPlayerDriving);
                            }
                            root["hint"] = "map: POST /action {type:'drive',active:true} to auto-drive; node selection not implemented yet";
                            try
                            {
                                var mapObj = map.GetMap();
                                int rowIdx = mapObj.GetCurrentBiomeRowIndex();
                                var row = mapObj.GetBiomeRowAtIndex(rowIdx);
                                if (row != null)
                                {
                                    var nodeList = new List<object>();
                                    int count = row.GetNumberOfNodes();
                                    for (int i = 0; i < count; i++)
                                    {
                                        var node = row.GetNodeAtIndex(i);
                                        if (node == null) continue;
                                        nodeList.Add(new Dictionary<string, object>
                                        {
                                            ["index"] = i,
                                            ["type"] = Safe(() => node.GetNodeType()?.ToString()),
                                            ["row"] = rowIdx,
                                            ["x"] = Safe(() => node.GetPosition().x),
                                            ["z"] = Safe(() => node.GetPosition().z),
                                        });
                                    }
                                    root["mapRow"] = rowIdx;
                                    root["nodes"] = nodeList;
                                    root["hint"] = "map: POST /action {type:'goNode',row,index} or {type:'drive',active:true}";
                                }
                            }
                            catch { }
                        }
                        else
                        {
                            root["hint"] = "not in combat; menu/inn/loading";
                        }
                    }
                }
            }
            catch (Exception e)
            {
                root["ok"] = false;
                root["error"] = e.ToString();
            }
            return root;
        }

        private static object ActorState(ActorInstance actor, int teamIndex)
        {
            var skills = new List<object>();
            try
            {
                var list = Refl.Field(actor, "m_CombatSkills") as System.Collections.IEnumerable;
                if (list != null)
                {
                    foreach (var s in list)
                    {
                        if (s == null) continue;
                        var id = Refl.Field(s, "m_SkillId") as string;
                        var equipped = Refl.Field(s, "m_IsEquipped");
                        if (string.IsNullOrEmpty(id)) continue;
                        // 2026-11-24：还剩几回合冷却（0=可用）。查不到技能定义就给 null，别拿 0 冒充可用。
                        // 依据 ActorInstance.GetRemainingSkillCooldown(ActorDataSkill)（decomp :5635-5649），
                        // 和游戏判 INVALID_COOLDOWN 用的是同一个函数（ActorControllerBase.cs:612）。
                        var skDef = Catalog.Find(id);
                        skills.Add(new Dictionary<string, object>
                        {
                            ["id"] = id,
                            ["equipped"] = equipped is bool b && b,
                            ["cd"] = skDef == null ? (object)null : actor.GetRemainingSkillCooldown(skDef),
                        });
                    }
                }
            }
            catch { }

            var tokens = new List<object>();
            try
            {
                var tc = Refl.FieldDeep(actor, "m_TokenContainer");
                var insts = Refl.FieldDeep(tc, "m_ActorContainerInstances") as System.Collections.IEnumerable;
                if (insts != null)
                {
                    foreach (var inst in insts)
                    {
                        if (inst == null) continue;
                        var def = Refl.FieldDeep(inst, "m_Definition");
                        if (def == null) continue;
                        var tid = Refl.FieldDeep(def, "m_Id") as string;
                        if (string.IsNullOrEmpty(tid)) continue;
                        var dur = Refl.FieldDeep(inst, "m_DurationAmountRemaining");
                        var sk = Refl.FieldDeep(def, "m_ActorDataSkill");
                        var sid = sk != null ? (Refl.FieldDeep(sk, "m_Id") as string) : null;
                        tokens.Add(new Dictionary<string, object>
                        {
                            ["id"] = tid,
                            ["dur"] = dur == null ? null : dur.ToString(),
                            ["skill"] = sid,
                        });
                        if (tokens.Count >= 12) break;
                    }
                }
            }
            catch { }

            return new Dictionary<string, object>
            {
                ["guid"] = actor.ActorGuid,
                ["name"] = Safe(() => actor.ActorName),
                ["dataId"] = Safe(() => actor.ActorDataId),
                ["team"] = teamIndex,
                ["position"] = Safe(() => actor.TeamPosition),
                ["hp"] = Safe(() => Math.Round(actor.HpRounded, 1)),
                ["hpMax"] = Safe(() => Math.Round(actor.CurrentHpMax, 1)),
                ["hpPct"] = Safe(() => Math.Round(actor.CurrentHpPercent * 100, 1)),
                ["stress"] = Safe(() => Math.Round(actor.Stress, 1)),
                ["alive"] = Safe(() => actor.IsLiving),
                ["selectedSkill"] = Safe(() => actor.SelectedSkillId),
                // 2026-11-21：尸体也留在队伍里、hp 还>0（实测 fanatic_immolatist_corpse hp=11），
                // Python 侧只能按名字猜 "_corpse"。游戏自己的答案是 ActorDataClass.m_IsTargetable，
                // 不可选的目标 SetSelectedSkill/EventSelectActor 一律静默丢弃 → 那就是
                // "target invalid" 的主要来源。直接把它吐出来，别再猜。
                ["targetable"] = Safe(() =>
                {
                    var dc = Refl.FieldDeep(actor, "m_ActorDataClass");
                    if (dc == null) return true;
                    var t = Refl.FieldDeep(dc, "m_IsTargetable");
                    return t is bool b ? b : true;
                }),
                // 2026-11-21：道途 + 战斗道具。战斗道具每人有独立背包（m_CombatInventory），
                // 以前 /state 完全看不到，所以"装备战斗道具/在战斗里用"一直没做过。
                ["path"] = Safe(() => actor.ActorDataPath == null ? null : actor.ActorDataPath.Id),
                ["combatItems"] = Safe(() =>
                {
                    var lst = new List<string>();
                    try
                    {
                        foreach (var id in actor.GetCombatSkillInventoryIds())
                            if (!string.IsNullOrEmpty(id)) lst.Add(id);
                    }
                    catch { }
                    return lst;
                }),
                ["combatItemSlots"] = Safe(() =>
                {
                    try { return Convert.ToInt32(Refl.Call(actor.GetCombatSkillInventory(), "GetNumberOfTotalSlots")); }
                    catch { return 0; }
                }),
                ["skills"] = skills,
                ["tokens"] = tokens,
            };
        }

        private static IEnumerator SkillSequence(uint actorGuid, string skillId, uint targetGuid)
        {
            EventSelectActor.Trigger(actorGuid, true);
            EventSkillSelectionChanged.Trigger(true, actorGuid, skillId, true, false);
            yield return new WaitForSeconds(0.8f);
            if (targetGuid != 0)
            {
                EventSkillTargetSelected.Trigger(actorGuid, targetGuid, skillId);
                yield return new WaitForSeconds(0.1f);
                EventSelectActor.Trigger(targetGuid, true);
            }
        }

        /// <summary>下一帧再退：先让 HTTP 把 ok 回出去，否则连接可能被直接掐断。</summary>
        private static System.Collections.IEnumerator QuitNextFrame()
        {
            yield return null;
            try { Assets.Code.Utils.Serialization.SaveUtils.SaveCurrentProfile(); } catch { }
            try { UnityEngine.Application.Quit(); } catch { }
        }

        private static List<string> CombatItemIds(ActorInstance a)
        {
            var ids = new List<string>();
            try
            {
                foreach (var cid in a.GetCombatSkillInventoryIds())
                    if (!string.IsNullOrEmpty(cid)) ids.Add(cid);
            }
            catch { }
            return ids;
        }

        private static string ItemIdOf(object it)
        {
            if (it == null) return null;
            // 状态里一直用的是 item.GetItemDefinition().GetKey()，这里优先走同一条路。
            try
            {
                var def = Refl.Call(it, "GetItemDefinition");
                if (def != null)
                {
                    var k = Refl.Call(def, "GetKey") as string;
                    if (!string.IsNullOrEmpty(k)) return k;
                }
            }
            catch { }
            foreach (var n in new[] { "ItemId", "m_ItemId", "itemId", "m_Id" })
            {
                var v = Refl.FieldDeep(it, n);
                if (v is string sv && !string.IsNullOrEmpty(sv)) return sv;
            }
            return null;
        }

        // 物品类型（trinket/combat/rest/currency/stage_coach_upgrade/memory）：
        //   ItemInstance.GetItemDefinition().m_type 是 CustomEnum<ItemType>，ToString() 就是名字
        //   （ItemType.cs 里都是 ToLowerInvariant 构造的）。
        // 以前读的是 "ItemClass"——游戏里根本没这个名字，Refl 取不到就给 null，
        // 于是 bag.cls 恒空，inn.py:36 只好按 id 前缀猜。2026-11-22 修。
        private static string ItemTypeOf(object it)
        {
            if (it == null) return null;
            try
            {
                var def = Refl.Call(it, "GetItemDefinition");
                var t = Refl.Field(def, "m_type");
                if (t != null) return Convert.ToString(t);
            }
            catch { }
            return null;
        }

        private static Assets.Code.Run.RunBhv FindRun()
        {
            // RunBhv 是 MonoBehaviour（SingletonMonoBehaviour<RunBhv>），直接场景里找最稳。
            var run = UnityEngine.Object.FindObjectOfType<Assets.Code.Run.RunBhv>();
            if (run != null) return run;
            var t = typeof(Assets.Code.Run.RunBhv);
            while (t != null)
            {
                try
                {
                    var p = t.GetProperty("Instance", All);
                    if (p != null) return p.GetValue(null) as Assets.Code.Run.RunBhv;
                }
                catch { }
                try
                {
                    var f = t.GetField("m_instance", All) ?? t.GetField("s_instance", All);
                    if (f != null) return f.GetValue(null) as Assets.Code.Run.RunBhv;
                }
                catch { }
                t = t.BaseType;
            }
            return null;
        }

        private static Assets.Code.Inn.InnBhv GetInn()
        {
            var t = typeof(Assets.Code.Utils.Singleton<Assets.Code.Inn.InnBhv>);
            try
            {
                var p = t.GetProperty("Instance", All);
                if (p != null) return p.GetValue(null) as Assets.Code.Inn.InnBhv;
            }
            catch { }
            try
            {
                var f = t.GetField("m_instance", All);
                if (f != null) return f.GetValue(null) as Assets.Code.Inn.InnBhv;
            }
            catch { }
            return null;
        }

        internal static string GoPath(Transform t)
        {
            var parts = new List<string>();
            while (t != null) { parts.Add(t.name); t = t.parent; }
            parts.Reverse();
            return string.Join("/", parts);
        }

        private static object Safe(Func<object> fn)
        {
            try { return fn(); } catch { return null; }
        }

        /// <summary>
        /// 这个技能本场已经用过几次。用的是**元数据里确实存在**的 API：
        ///   SkillButtonBhv.ActorInstance  (public get)
        ///   SkillButtonBhv.SkillId       (public get)
        ///   ActorInstance.GetSkillUseCount(String)  (public instance, 返回 Int32)
        /// 拿不到返回 -1。**只上报，不据此过滤候选**——"次数用尽"是不是拒招的原因还没被证实，
        /// 等 logs/action_fail.jsonl 里攒够数据再决定要不要拿它做闸门。
        /// </summary>
        /// <summary>
        /// 当前火炬值（0~100），读驿站马车 HUD 火炬控件的私有字段 m_curValue。
        /// 读不到返回 null（不是 0 —— 别把"读不到"当成"火炬灭了"）。
        /// </summary>
        private static object TorchValue()
        {
            try
            {
                var t = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.StageCoachTorchUiBhv>(true);
                if (t == null) return null;
                var f = typeof(Assets.Code.UI.StageCoachTorchUiBhv).GetField("m_curValue",
                    System.Reflection.BindingFlags.Instance |
                    System.Reflection.BindingFlags.NonPublic |
                    System.Reflection.BindingFlags.Public);
                if (f == null) return null;
                return Convert.ToInt32(f.GetValue(t));
            }
            catch { return null; }
        }

        private static int SkillUsesUsedOf(Assets.Code.UI.SkillButtonBhv b)
        {
            try
            {
                var ai = b.ActorInstance;
                var sid = b.SkillId;
                if (ai == null || string.IsNullOrEmpty(sid)) return -1;
                return ai.GetSkillUseCount(sid);
            }
            catch { return -1; }
        }

        /// <summary>通用反射调用：静态方法，或单例/Instance 上的实例方法；返回 IEnumerator 时自动当协程跑。</summary>
        internal static object Call(string action, string typeName, string methodName, List<object> args)
        {
            var t = Inspect.FindType(typeName);
            if (t == null) return new { ok = false, error = "type not found: " + typeName };

            var methods = t.GetMethods(All).Where(m => m.Name == methodName && m.GetParameters().Length == args.Count).ToArray();
            if (methods.Length == 0) return new { ok = false, error = "method not found: " + methodName + "/" + args.Count };

            foreach (var m in methods)
            {
                var ps = m.GetParameters();
                var conv = new object[ps.Length];
                bool okArgs = true;
                for (int i = 0; i < ps.Length; i++)
                {
                    try { conv[i] = ConvertArg(args[i], ps[i].ParameterType); }
                    catch { okArgs = false; break; }
                }
                if (!okArgs) continue;

                object target = null;
                if (!m.IsStatic)
                {
                    target = t.GetProperty("Instance", All)?.GetValue(null) ?? t.GetField("s_instance", All)?.GetValue(null);
                    if (target == null) continue;
                }
                try
                {
                    var ret = m.Invoke(target, conv);
                    if (ret is System.Collections.IEnumerator ie)
                    {
                        Plugin.Run(ie);
                        return new { ok = true, action, method = m.Name, started = "coroutine" };
                    }
                    return new { ok = true, action, method = m.Name, result = ret?.ToString() };
                }
                catch (Exception e)
                {
                    return new { ok = false, error = (e.InnerException ?? e).Message };
                }
            }
            return new { ok = false, error = "no overload matched: " + methodName };
        }

        private static object ConvertArg(object v, Type t)
        {
            if (t == typeof(string)) return Convert.ToString(v);
            if (t == typeof(float)) return Convert.ToSingle(v);
            if (t == typeof(double)) return Convert.ToDouble(v);
            if (t == typeof(int)) return Convert.ToInt32(v);
            if (t == typeof(bool)) return Convert.ToBoolean(v);
            if (t == typeof(uint)) return Convert.ToUInt32(v);
            if (t.IsEnum) return Enum.Parse(t, Convert.ToString(v), true);
            return v;
        }

        public static ActorInstance FindActor(uint guid)        {
            try
            {
                var lib = SingletonMonoBehaviour<Library<uint, ActorInstance>>.Instance;
                if (lib == null) return null;
                return lib.GetLibraryElement(guid);
            }
            catch { return null; }
        }

        // 把游戏自己算的 SkillPreview 拍平成一个匿名对象（字段见 SkillCalculation.cs:264-279）。
        private static object Preview(Assets.Code.Skill.SkillCalculation.ActorResult.SkillPreview pv)
        {
            return new
            {
                performer = pv.m_PerformerActorGuid,
                target = pv.m_TargetActorGuid,
                hit = pv.m_ToHitChance,
                low = pv.m_DamageLow,
                high = pv.m_DamageHigh,
                crit = pv.m_CritChance,
                critDamage = pv.m_CritDamage,
                isDamage = pv.m_IsDamageValid,
                isHeal = pv.m_IsHealValid,
                isCrit = pv.m_IsCritValid
            };
        }

        public static object Act(string body)
        {
            var req = JsonConvert.DeserializeObject<Dictionary<string, object>>(body ?? "{}") ?? new Dictionary<string, object>();
            string type = req.TryGetValue("type", out var t) ? Convert.ToString(t) : "";
            uint actorGuid = req.TryGetValue("actorGuid", out var ag) ? Convert.ToUInt32(ag) : 0u;
            uint targetGuid = req.TryGetValue("targetGuid", out var tg) ? Convert.ToUInt32(tg) : 0u;
            string skillId = req.TryGetValue("skillId", out var sk) ? Convert.ToString(sk) : "";

            var combat = CombatBhv.Instance;
            if (type == "embark")
            {
                var c = UnityEngine.Object.FindObjectOfType<EmbarkControllerBhv>();
                if (c == null) return new { ok = false, error = "EmbarkControllerBhv not found" };
                c.Embark();
                return new { ok = true, action = "embark" };
            }
            // 希望祭坛的「启程」：这个按钮是"按住不放才生效"的长按按钮
            // （AltarOfHopeUiBhv.m_accEmbarkTimeHeld / SetEmbarkHeld / m_embarkHoldFills），
            // 而且只认真实指针的悬停状态 —— uiClick / uiInvoke / uiHold / 虚拟键盘全都不行。
            // 这里直接在游戏里调它的方法，全程不碰鼠标、不抢焦点。
            // 策略：先 SetEmbarkHeld(true) 让它自己计时 3 秒；还没走就 OnEmbark() 强制出发。
            if (type == "altarEmbark")
            {
                var altar = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.AltarOfHopeUiBhv>();
                if (altar == null) return new { ok = false, error = "AltarOfHopeUiBhv not found" };
                Plugin.Run(AltarEmbarkRoutine(altar));
                return new { ok = true, action = "altarEmbark", note = "hold 3s, then force OnEmbark if still here" };
            }

            if (type == "skipEmbark")
            {
                var e = EmbarkBhv.Instance;
                if (e == null) return new { ok = false, error = "EmbarkBhv not found" };
                e.SkipEmbark();
                return new { ok = true, action = "skipEmbark" };
            }

            // 2026-11-23：放弃征程（零键鼠 —— 不按 Esc、不用 simAction/PauseMenu）。
            // 照抄游戏自己的 PauseMenuUiControllerBhv.OnAbandonRun：
            //   logs\decomp\all\Assets.Code.UI.Controllers\PauseMenuUiControllerBhv.cs:438-455
            //   IsChangingState() → 不动；已 GameOver → 不动；
            //   否则 SetGameOver(ABANDON 或 CASH_OUT) → TriggerGameOver(skipTransition:false, abandoned:true) → Narration.Stop()。
            // TriggerGameOver 会把 mode 切到 DRIVING（就是实机上"放弃后回到开局马车"的那一步），
            // 之后由 watchdog/脚本按 W 开到祭坛，再 altarEmbark。
            if (type == "abandonRun")
            {
                try
                {
                    var mgr = Assets.Code.Game.GameModeMgr.Instance;
                    if (mgr == null) return new { ok = false, error = "GameModeMgr not found" };
                    if (mgr.IsChangingState()) return new { ok = false, error = "IsChangingState" };
                    var run = FindRun();
                    if (run == null) return new { ok = false, error = "RunBhv not found" };
                    var score = run.RunScoreManager;
                    if (score == null) return new { ok = false, error = "RunScoreManager null" };
                    if (score.IsGameOver()) return new { ok = false, error = "already game over" };
                    var mode = mgr.GetCurrentMode();
                    var reason = (mode != null && mode.m_isRunEndCashOut)
                        ? GameOverReason.CASH_OUT : GameOverReason.ABANDON;
                    score.SetGameOver(reason);
                    bool manual = mode != null && mode.m_isManualHandleGameOver;
                    if (!manual) score.TriggerGameOver(skipTransition: false, abandoned: true);
                    try { Assets.Code.Audio.Narration.NarrationMgr.Instance?.Stop(); } catch { }
                    return new { ok = true, action = "abandonRun", reason = reason.ToString(), manual };
                }
                catch (Exception e)
                {
                    return new { ok = false, error = e.GetType().Name + ": " + e.Message };
                }
            }
            if (type == "drive")
            {
                var map = MapMgrBhv.Instance;
                var vehicle = map?.GetVehicleControl();
                if (vehicle == null) return new { ok = false, error = "vehicle not found" };
                bool active = !(req.TryGetValue("active", out var av) && av is bool b && !b);
                vehicle.SetInputAlwaysMoveForward(active);
                return new { ok = true, action = "drive", active, speed = Safe(() => vehicle.GetSpeed()) };
            }
            if (type == "simulate")
            {
                string axis = req.TryGetValue("axis", out var ax) ? Convert.ToString(ax) : "Move";
                float val = req.TryGetValue("value", out var vv) ? Convert.ToSingle(vv) : 1f;
                var simType = Type.GetType("Code.FlyingRat.Simulator.InputSimulator, IronCrown");
                var utilType = Type.GetType("Code.FlyingRat.Simulator.InputSimulatorUtils, IronCrown");
                if (simType == null || utilType == null) return new { ok = false, error = "InputSimulator types not found" };
                var runProp = simType.GetProperty("Run", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                runProp?.SetValue(null, true);
                var dict = utilType.GetField("axisValues", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(null)
                    as Dictionary<string, float>;
                if (dict == null) return new { ok = false, error = "axisValues not found" };
                dict[axis] = val;
                return new { ok = true, action = "simulate", axis, value = val, run = runProp?.GetValue(null) };
            }
            if (type == "simEnter")
            {
                var simType = Type.GetType("Code.FlyingRat.Simulator.InputSimulator, IronCrown");
                if (simType == null) return new { ok = false, error = "InputSimulator type not found" };
                var inst = simType.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(null);
                if (inst == null) return new { ok = false, error = "InputSimulator instance not found" };
                var mode = Assets.Code.Game.GameModeMgr.CurrentMode;
                var mi = simType.GetMethod("OnGameModeEnterComplete", BindingFlags.Instance | BindingFlags.Public);
                mi?.Invoke(inst, new object[] { mode, false });
                return new { ok = true, action = "simEnter", mode = mode.ToString() };
            }
            if (type == "sim")
            {
                var simType = Type.GetType("Code.FlyingRat.Simulator.InputSimulator, IronCrown");
                if (simType == null) return new { ok = false, error = "InputSimulator type not found" };
                var inst = simType.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(null);
                if (inst == null) return new { ok = false, error = "InputSimulator instance not found; call autoplay first" };
                string method = req.TryGetValue("method", out var mm) ? Convert.ToString(mm) : "Submit";
                var mi = simType.GetMethod(method, BindingFlags.Instance | BindingFlags.Public);
                if (mi == null) return new { ok = false, error = "method not found: " + method };
                object[] args = null;
                if (mi.GetParameters().Length == 1)
                {
                    string dir = req.TryGetValue("direction", out var dd) ? Convert.ToString(dd) : "Down";
                    Type enumType = null;
                    foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                    {
                        try { enumType = asm.GetTypes().FirstOrDefault(x => x.Name == "MoveDirection"); } catch { }
                        if (enumType != null) break;
                    }
                    if (enumType == null) return new { ok = false, error = "MoveDirection enum not found" };
                    args = new[] { Enum.Parse(enumType, dir, true) };
                }
                mi.Invoke(inst, args);
                return new { ok = true, action = "sim", method };
            }
            if (type == "autoplay")
            {
                var simType = Type.GetType("Code.FlyingRat.Simulator.InputSimulator, IronCrown");
                if (simType == null) return new { ok = false, error = "InputSimulator type not found" };
                // 找到 INPUT_SIMULATOR_ENABLED 这个 TextBasedEditorPrefsBoolType 静态字段并置 true。
                object prefObj = null;
                string prefOwner = "";
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type[] types;
                    try { types = asm.GetTypes(); } catch { continue; }
                    foreach (var ty in types)
                    {
                        var f = ty.GetField("INPUT_SIMULATOR_ENABLED", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                        if (f == null) continue;
                        prefObj = f.GetValue(null);
                        prefOwner = ty.FullName;
                        break;
                    }
                    if (prefObj != null) break;
                }
                if (prefObj == null) return new { ok = false, error = "INPUT_SIMULATOR_ENABLED not found" };
                var setValue = prefObj.GetType().GetMethod("SetValue", new[] { typeof(bool) });
                setValue?.Invoke(prefObj, new object[] { true });
                var prefsType = Type.GetType("Assets.Code.Utils.TextBasedEditorPrefs, IronCrown");
                var baseType = prefObj.GetType().BaseType;
                var setBool = prefsType?.GetMethod("SetBool", new[] { baseType, typeof(bool) });
                try { setBool?.Invoke(null, new[] { prefObj, (object)true }); } catch { }
                var go = new GameObject("DD2InputSimulator");
                UnityEngine.Object.DontDestroyOnLoad(go);
                var comp = go.AddComponent(simType);
                var runProp = simType.GetProperty("Run", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                var disabledField = simType.GetField("_disabled", BindingFlags.Instance | BindingFlags.NonPublic);
                if (runProp?.GetValue(null) is bool running && !running)
                {
                    runProp.SetValue(null, false);
                    disabledField?.SetValue(comp, false);
                    var setup = simType.GetMethod("Setup", BindingFlags.Instance | BindingFlags.NonPublic);
                    try { setup?.Invoke(comp, null); } catch (Exception e) { return new { ok = false, error = "setup: " + e.InnerException?.Message ?? e.Message }; }
                }
                var run = runProp?.GetValue(null);
                return new { ok = true, action = "autoplay", prefOwner, component = comp != null, run };
            }
            if (type == "goNode")
            {
                var map = MapMgrBhv.Instance;
                var vehicle = map?.GetVehicleControl();
                var mapObj = map?.GetMap();
                if (vehicle == null || mapObj == null) return new { ok = false, error = "map/vehicle not found" };
                int rowIdx = req.TryGetValue("row", out var rv) ? Convert.ToInt32(rv) : mapObj.GetCurrentBiomeRowIndex();
                int nodeIdx = req.TryGetValue("index", out var iv) ? Convert.ToInt32(iv) : 0;
                var row = mapObj.GetBiomeRowAtIndex(rowIdx);
                var node = row?.GetNodeAtIndex(nodeIdx);
                if (node == null) return new { ok = false, error = "node not found" };
                var pos = node.GetPosition();
                vehicle.GoToTarget(pos, true);
                return new { ok = true, action = "goNode", row = rowIdx, index = nodeIdx, x = pos.x, z = pos.z };
            }
            if (type == "vehicle")
            {
                var map = MapMgrBhv.Instance;
                var vehicle = map?.GetVehicleControl();
                if (vehicle == null) return new { ok = false, error = "vehicle not found" };
                string method = req.TryGetValue("method", out var mv) ? Convert.ToString(mv) : "";
                if (string.IsNullOrEmpty(method)) return new { ok = false, error = "method required" };
                var mi = vehicle.GetType().GetMethod(method, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (mi == null) return new { ok = false, error = "method not found: " + method };
                var ps = mi.GetParameters();
                object[] args = ps.Length == 0 ? null : new object[] { req.TryGetValue("arg", out var agv) ? agv : true };
                var ret = mi.Invoke(vehicle, args);
                return new { ok = true, action = "vehicle", method, result = ret?.ToString() };
            }
            if (type == "uiClick")
            {
                string pick = req.TryGetValue("text", out var tx) ? Convert.ToString(tx)
                            : req.TryGetValue("pick", out var pk) ? Convert.ToString(pk) : "";
                int idx = req.TryGetValue("index", out var ix) ? Convert.ToInt32(ix) : -1;
                return UiDriver.Click(pick, idx);
            }
            if (type == "uiHold")
            {
                string pick = req.TryGetValue("text", out var hx) ? Convert.ToString(hx) : "";
                int hidx = req.TryGetValue("index", out var hi) ? Convert.ToInt32(hi) : -1;
                float secs = req.TryGetValue("seconds", out var hs) ? Convert.ToSingle(hs) : 2f;
                return UiDriver.Hold(pick, hidx, secs);
            }
            if (type == "dump")
            {
                string tn = req.TryGetValue("name", out var dt) ? Convert.ToString(dt)
                          : req.TryGetValue("targetType", out var dt2) ? Convert.ToString(dt2) : "";
                string fn = req.TryGetValue("field", out var df) ? Convert.ToString(df) : "";
                return string.IsNullOrEmpty(fn) ? Inspect.DumpType(tn) : Inspect.DumpObject(tn, fn);
            }
            if (type == "axes")
            {
                var utilType = Type.GetType("Code.FlyingRat.Simulator.InputSimulatorUtils, IronCrown")
                            ?? Inspect.FindType("InputSimulatorUtils");
                if (utilType == null) return new { ok = false, error = "InputSimulatorUtils not found" };
                var dict = utilType.GetField("axisValues", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(null)
                    as System.Collections.Generic.Dictionary<string, float>;
                var fields = new List<string>();
                foreach (var f in utilType.GetFields(BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic))
                {
                    object v = null;
                    try { v = f.GetValue(null); } catch { }
                    fields.Add(f.Name + "=" + (v?.ToString() ?? "null"));
                }
                return new
                {
                    ok = true,
                    axisKeys = dict == null ? null : dict.Keys.ToArray(),
                    axisValues = dict == null ? null : dict.ToDictionary(k => k.Key, k => k.Value),
                    statics = fields,
                    type = utilType.FullName,
                };
            }
            if (type == "holdAxis")
            {
                string ax = req.TryGetValue("axis", out var ha) ? Convert.ToString(ha) : "Move";
                float hv = req.TryGetValue("value", out var hvv) ? Convert.ToSingle(hvv) : 1f;
                if (hv == 0f) { Plugin.HoldAxis = null; return new { ok = true, action = "holdAxis", cleared = ax }; }
                Plugin.HoldAxis = ax;
                return new { ok = true, action = "holdAxis", axis = ax, value = hv };
            }
            if (type == "call")
            {
                string tn = req.TryGetValue("name", out var cn) ? Convert.ToString(cn) : "";
                string mn = req.TryGetValue("method", out var cm) ? Convert.ToString(cm) : "";
                var argList = new List<object>();
                if (req.TryGetValue("args", out var ca) && ca is Newtonsoft.Json.Linq.JArray ja)
                {
                    foreach (var item in ja) argList.Add(item.Type == Newtonsoft.Json.Linq.JTokenType.String ? (object)item.ToString() : (object)item.ToObject<double>());
                }
                return Call(type, tn, mn, argList);
            }
            if (type == "simaxis")
            {
                string ax = req.TryGetValue("axis", out var sa) ? Convert.ToString(sa) : "Move";
                float val = req.TryGetValue("value", out var sv) ? Convert.ToSingle(sv) : 1f;
                float secs = req.TryGetValue("seconds", out var ss) ? Convert.ToSingle(ss) : 2f;
                return Call(type, "InputSimulatorUtils", "SimulateAxis", new List<object> { ax, (double)val, (double)secs });
            }
            if (type == "simclick")
            {
                string nm = req.TryGetValue("method", out var sm) ? Convert.ToString(sm) : "Submit";
                return Call(type, "InputSimulatorUtils", "SimulateActionClick", new List<object> { nm });
            }
            if (type == "pad")
            {
                float px = req.TryGetValue("x", out var vx) ? Convert.ToSingle(vx) : 0f;
                float py = req.TryGetValue("y", out var vy) ? Convert.ToSingle(vy) : 0f;
                string btns = req.TryGetValue("buttons", out var vb) ? Convert.ToString(vb) : "";
                return VirtualPad.Push(px, py, btns);
            }
            if (type == "padRelease") return VirtualPad.Release();
            if (type == "padInfo") return VirtualPad.Info();
            // 后台指针点击：把事件直接塞进 Input System 的**真实** Mouse.current，
            // 不移动 Windows 光标、不抢焦点。游戏（InputSystemUIInputModule）会当成
            // 真的鼠标点击，连 hover 状态都是真的 —— 这是"后台点启程 / 点主菜单"的正解。
            // 注意不要用 AddDevice 造虚拟鼠标：那台设备不是 Mouse.current，UI 不认。
            if (type == "pointerClick")
            {
                float px = req.TryGetValue("x", out var vpx) ? Convert.ToSingle(vpx) : 960f;
                float py = req.TryGetValue("y", out var vpy) ? Convert.ToSingle(vpy) : 540f;
                float hold = req.TryGetValue("hold", out var vhd) ? Convert.ToSingle(vhd) : 0.06f;
                var mouseNow = UnityEngine.InputSystem.Mouse.current;
                if (mouseNow == null) return new { ok = false, error = "Mouse.current is null" };
                Plugin.Run(PointerClickRoutine(mouseNow, px, py, hold));
                return new { ok = true, action = "pointerClick", x = px, y = py, hold };
            }

            // 读出战斗界面 5 个技能格到底绑了哪个技能。
            // SkillButtonBhv 上就有 SkillId / SkillButtonIndex / IsValid，
            // 有了这个就不必再猜"skills 数组前 5 个 = 界面 5 个格子"——
            // 猜错会点出别的技能、目标和射程对不上，整回合白扔。
            if (type == "skills")
            {
                // 只认战斗技能栏那 5 个格子（MoveButton / pass_skill_button 也挂 SkillButtonBhv，
                // 以前一起吐出来会让"第 i 个技能格"整体错位）。
                var btns = UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.SkillButtonBhv>(true);
                var list = new List<object>();
                foreach (var b in btns)
                {
                    if (b == null) continue;
                    var go = b.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    if (go.name.IndexOf("combat_skill_button", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    bool valid = false;
                    try { valid = b.IsValid; } catch { }
                    bool selected = false;
                    try { selected = b.Selected; } catch { }
                    var rt = go.GetComponent<UnityEngine.RectTransform>();
                    string pos = "";
                    if (rt != null)
                    {
                        // 用按钮自己的 Canvas 相机算屏幕坐标；Camera.main 在战斗 UI 上取到的是
                        // 场景相机，算出来是 16000+ 的鬼坐标。
                        var canvas = go.GetComponentInParent<Canvas>();
                        Camera cam = canvas == null ? Camera.main
                                   : (canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera);
                        var sp = UnityEngine.RectTransformUtility.WorldToScreenPoint(cam, rt.TransformPoint(rt.rect.center));
                        pos = Mathf.RoundToInt(sp.x) + "," + Mathf.RoundToInt(sp.y);
                    }
                    list.Add(new Dictionary<string, object>
                    {
                        ["i"] = b.SkillButtonIndex,
                        ["skillId"] = b.SkillId ?? "",
                        ["buttonIndex"] = b.SkillButtonIndex,
                        ["actorGuid"] = b.ActorGuid.ToString(),
                        ["disabled"] = !valid,
                        ["valid"] = valid,
                        ["usesUsed"] = SkillUsesUsedOf(b),
                        ["selected"] = selected,
                        ["screen"] = pos,
                        ["go"] = go.name,
                    });
                }
                list.Sort((x, y) => Convert.ToInt32(((Dictionary<string, object>)x)["buttonIndex"])
                                     .CompareTo(Convert.ToInt32(((Dictionary<string, object>)y)["buttonIndex"])));
                return new { ok = true, action = "skills", skills = list };
            }

            // 2026-11-21：**游戏自己算好的"这一手能打谁"**。
            // 「skill not selected / target invalid」不是事件机制坏了，是 Python 侧拿 CSV 的
            // 射程表在猜合法性（Catalog.cs 只导了 m_LaunchRanks/m_TargetRanks，连
            // m_TargetRelativeRanks、潜行、目标技能封锁、技能条件都没有），猜错就被游戏静默拒。
            // 游戏每次轮到我方都已经把结果算在 ActorController 的 m_SkillTargetEntries 里，
            // 这里直接用它的两个公开查询函数吐出来，Python 只需要**在有效集合里挑**：
            //   {ok, action:"valid_targets", actorGuid, skills:[{skillId, valid, targets:[guid...]}]}
            if (type == "valid_targets")
            {
                if (combat == null || !combat.IsBattleRunning) return new { ok = false, error = "not in combat" };
                if (actorGuid == 0) actorGuid = combat.CurrentActorGuid;
                var me = FindActor(actorGuid);
                if (me == null) return new { ok = false, error = "actor not found" };
                var ctrl = me.Controller;
                if (ctrl == null) return new { ok = false, error = "no controller" };

                // 2026-11-21：这张表会被游戏自己清掉（BattleTurnOrder.AttemptRollExtraTurn
                // 调 GetHasValidCombatSkillOfType(saveCalculation:false) → ClearValidSkillTargetEntries），
                // 只读的话会拿到**空表**，于是所有技能都被判为不可用。
                // 这里不能直接调 CalculateValidCombatSkillTargetEntries：它只 Add 不清（Add 在
                // ActorControllerBase.cs:414），连调三次 entries 就从 32 涨到 48 —— 旧条目越堆越多，
                // GetIsValidSkill 只要命中任意一条就返回 true，于是"明明不能打还报可打"，
                // 发出去就被游戏拒（这就是剩下的 skill not selected）。
                // 正确做法是调游戏自己的 OnTurnSelect()：它先 RefreshSkillMaps()（清空+重算），
                // 而 ActorControllerInput 的 SelectSkilIId/SelectTargetGuid 都返回 null/0，没有副作用。
                // 只对**我方当前出手的 actor**做，绝不碰 AI（AI 的 OnTurnSelect 会真的出招）。
                int entries = -1;
                try
                {
                    if (actorGuid == combat.CurrentActorGuid && me.TeamIndex == 0)
                    {
                        try { Refl.Call(ctrl, "OnTurnSelect"); } catch { }
                    }
                }
                catch { }
                try
                {
                    var lst = Refl.FieldDeep(ctrl, "m_SkillTargetEntries");
                    var cnt = lst == null ? null : lst.GetType().GetProperty("Count")?.GetValue(lst);
                    if (cnt != null) entries = Convert.ToInt32(cnt);
                }
                catch { }

                // 所有演员（两边都算，友方技能的目标在我方）
                var everyone = new List<ActorInstance>();
                try
                {
                    var battle = Refl.Field(combat, "m_Battle");
                    var teams = Refl.Field(battle, "m_BattleTeams");
                    for (int ti = 0; ti < 2; ti++)
                    {
                        var team = Refl.Call(teams, "GetTeam", ti);
                        var list = Refl.Field(team, "m_Actors") as System.Collections.IEnumerable;
                        if (list == null) continue;
                        foreach (var item in list)
                        {
                            var a = item as ActorInstance;
                            if (a != null && a.IsLiving) everyone.Add(a);
                        }
                    }
                }
                catch { }

                // 技能集合只认界面那 5 个战斗格（和 skills 分支同一套过滤，避免 move/pass 混进来）
                var outSkills = new List<object>();
                try
                {
                    var btns = UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.SkillButtonBhv>(true);
                    foreach (var b in btns)
                    {
                        if (b == null) continue;
                        var go = b.gameObject;
                        if (go == null || !go.activeInHierarchy) continue;
                        if (go.name.IndexOf("combat_skill_button", StringComparison.OrdinalIgnoreCase) < 0) continue;
                        var sid = b.SkillId;
                        if (string.IsNullOrEmpty(sid)) continue;
                        bool valid = false;
                        try { valid = ctrl.GetIsValidSkill(sid); } catch { }
                        var tgts = new List<uint>();
                        foreach (var a in everyone)
                        {
                            bool okT = false;
                            try { okT = ctrl.GetIsValidSkillTarget(sid, a.ActorGuid); } catch { }
                            if (okT) tgts.Add(a.ActorGuid);
                        }
                        outSkills.Add(new Dictionary<string, object>
                        {
                            ["skillId"] = sid,
                            ["buttonIndex"] = b.SkillButtonIndex,
                            ["valid"] = valid,
                            ["targets"] = tgts,
                        });
                    }
                }
                catch (Exception e) { return new { ok = false, error = e.GetType().Name + ":" + e.Message }; }
                return new { ok = true, action = "valid_targets", actorGuid, entries, skills = outSkills };
            }

            // 诊断：按名字子串列出程序集里的类型（用来找 actor_info_panel 上到底挂了哪个 Bhv）。
            if (type == "types")
            {
                string match = req.TryGetValue("match", out var tm) ? Convert.ToString(tm) : "";
                var found = new List<string>();
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type[] ts;
                    try { ts = asm.GetTypes(); } catch { continue; }
                    foreach (var tp in ts)
                    {
                        if (tp == null) continue;
                        if (string.IsNullOrEmpty(match) || tp.Name.IndexOf(match, StringComparison.OrdinalIgnoreCase) >= 0)
                            found.Add(tp.FullName);
                    }
                }
                found.Sort(StringComparer.OrdinalIgnoreCase);
                return new { ok = true, action = "types", count = found.Count, types = found };
            }

            // 诊断：把每个 actor_info_panel 上的组件/字段/子节点吐出来，
            // 用来确认"这个敌人现在能不能被当前技能选中"到底存在哪个字段里。
            if (type == "panels")
            {
                var res = new List<object>();
                var seen = new HashSet<int>();
                foreach (var tr in UnityEngine.Object.FindObjectsOfType<Transform>(true))
                {
                    if (tr == null) continue;
                    var go = tr.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    if (go.name.IndexOf("actor_info_panel", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (!seen.Add(go.GetInstanceID())) continue;
                    var rt = go.GetComponent<RectTransform>();
                    string pos = "";
                    if (rt != null)
                    {
                        var canvas = go.GetComponentInParent<Canvas>();
                        Camera cam = canvas == null ? Camera.main
                                   : (canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera);
                        var sp = UnityEngine.RectTransformUtility.WorldToScreenPoint(cam, rt.TransformPoint(rt.rect.center));
                        pos = Mathf.RoundToInt(sp.x) + "," + Mathf.RoundToInt(sp.y);
                    }
                    var comps = new List<object>();
                    foreach (var mb in go.GetComponents<MonoBehaviour>())
                    {
                        if (mb == null) continue;
                        var fl = new List<object>();
                        foreach (var f in mb.GetType().GetFields(All))
                        {
                            object v = null;
                            try { v = f.GetValue(mb); } catch { continue; }
                            string sv = v == null ? "null"
                                      : v is UnityEngine.Object uo ? (uo == null ? "<destroyed>" : uo.name)
                                      : v is System.Collections.ICollection cc ? "count=" + cc.Count
                                      : v.ToString();
                            fl.Add(new { n = f.Name, t = f.FieldType.Name, v = sv });
                        }
                        comps.Add(new { t = mb.GetType().Name, f = fl });
                    }
                    var kids = new List<string>();
                    foreach (Transform ch in go.transform)
                    {
                        kids.Add(ch.name + (ch.gameObject.activeSelf ? "" : "(off)"));
                        if (kids.Count >= 20) break;
                    }
                    res.Add(new { path = GoPath(go.transform), screen = pos, kids, comps });
                }
                return new { ok = true, action = "panels", panels = res };
            }

            // 旅馆训练师：每个 upgradeskill_button 绑的是哪个英雄的哪个技能。
            if (type == "upgrades")
            {
                var list = new List<object>();
                foreach (var b in UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.UpgradeSkillButton>(true))
                {
                    if (b == null) continue;
                    var go = b.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    bool valid = false;
                    try { valid = b.IsValid; } catch { }
                    var rt = go.GetComponent<RectTransform>();
                    string pos = "";
                    if (rt != null)
                    {
                        var canvas = go.GetComponentInParent<Canvas>();
                        Camera cam = canvas == null ? Camera.main
                                   : (canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera);
                        var sp = UnityEngine.RectTransformUtility.WorldToScreenPoint(cam, rt.TransformPoint(rt.rect.center));
                        pos = Mathf.RoundToInt(sp.x) + "," + Mathf.RoundToInt(sp.y);
                    }
                    list.Add(new Dictionary<string, object>
                    {
                        ["skillId"] = b.SkillId ?? "",
                        ["actorGuid"] = b.ActorGuid.ToString(),
                        ["selected"] = b.Selected,
                        ["valid"] = valid,
                        ["upgraded"] = Refl.Call(b, "IsSkillVisiblyUpgraded") is bool ub && ub,
                        ["screen"] = pos,
                    });
                }
                object scrInfo = null;
                var scr = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.Screens.InnUpgradeSkillsBhv>();
                if (scr != null)
                {
                    scrInfo = new Dictionary<string, object>
                    {
                        ["actorGuid"] = scr.ActiveActorGuid.ToString(),
                        ["numSkills"] = scr.NumSkills,
                        ["numSelected"] = scr.NumSelectedSkills,
                        ["canAfford"] = scr.CanAffordSkill,
                    };
                }
                return new { ok = true, action = "upgrades", screen = scrInfo, buttons = list };
            }

            // 通用：按名字子串找 GameObject，dump 每个上面的组件字段（诊断饰品/道具/关系用）。
            if (type == "goDump")
            {
                string match = req.TryGetValue("match", out var gm) ? Convert.ToString(gm) : "actor_info_panel";
                int max = req.TryGetValue("max", out var gx) ? Convert.ToInt32(gx) : 40;
                var res = new List<object>();
                var seen = new HashSet<int>();
                foreach (var tr in UnityEngine.Object.FindObjectsOfType<Transform>(true))
                {
                    if (tr == null || res.Count >= max) continue;
                    var go = tr.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    if (go.name.IndexOf(match, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (!seen.Add(go.GetInstanceID())) continue;
                    var rt = go.GetComponent<RectTransform>();
                    string pos = "";
                    if (rt != null)
                    {
                        var canvas = go.GetComponentInParent<Canvas>();
                        Camera cam = canvas == null ? Camera.main
                                   : (canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera);
                        var sp = UnityEngine.RectTransformUtility.WorldToScreenPoint(cam, rt.TransformPoint(rt.rect.center));
                        pos = Mathf.RoundToInt(sp.x) + "," + Mathf.RoundToInt(sp.y);
                    }
                    var comps = new List<object>();
                    foreach (var mb in go.GetComponents<MonoBehaviour>())
                    {
                        if (mb == null) continue;
                        var fl = new List<object>();
                        foreach (var f in mb.GetType().GetFields(All))
                        {
                            if (fl.Count >= 14) break;
                            object v = null;
                            try { v = f.GetValue(mb); } catch { continue; }
                            string sv = v == null ? "null"
                                      : v is UnityEngine.Object uo ? (uo == null ? "<destroyed>" : uo.name)
                                      : v is System.Collections.ICollection cc ? "count=" + cc.Count
                                      : v.ToString();
                            fl.Add(new { n = f.Name, t = f.FieldType.Name, v = sv });
                        }
                        comps.Add(new { t = mb.GetType().Name, f = fl });
                    }
                    res.Add(new { path = GoPath(go.transform), screen = pos, comps });
                }
                return new { ok = true, action = "goDump", match, count = res.Count, objects = res };
            }

            // 旅馆训练师：按 skillId 选中一个升级项并提交（花精通点）。
            if (type == "trainerUpgrade")
            {
                string sid = req.TryGetValue("skillId", out var ts) ? Convert.ToString(ts) : "";
                var scr = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.Screens.InnUpgradeSkillsBhv>();
                if (scr == null) return new { ok = false, error = "trainer screen not open" };
                var btns = UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.UpgradeSkillButton>(true);
                var btn = btns.FirstOrDefault(x => x != null && x.gameObject.activeInHierarchy && x.SkillId == sid);
                if (btn == null)
                    return new { ok = false, error = "button not found: " + sid,
                                 skills = btns.Where(x => x != null && x.gameObject.activeInHierarchy)
                                              .Select(x => x.SkillId).ToArray() };
                var actor = FindActor(btn.ActorGuid);
                if (actor == null) return new { ok = false, error = "actor not found: " + btn.ActorGuid };
                bool sel0 = false;
                try
                {
                    var mi = scr.GetType().GetMethod("IsSkillSelectedForUpgrade", All);
                    if (mi != null) sel0 = Convert.ToBoolean(mi.Invoke(scr, new object[] { actor, sid }));
                }
                catch { }
                int n0 = scr.NumSelectedSkills;
                try
                {
                    var mi = scr.GetType().GetMethod("ApplySkillToUpgrade", All);
                    if (mi == null) return new { ok = false, error = "ApplySkillToUpgrade not found" };
                    mi.Invoke(scr, new object[] { actor, sid, true });
                }
                catch (Exception e) { return new { ok = false, error = "select: " + (e.InnerException ?? e).Message }; }
                int n1 = scr.NumSelectedSkills;
                try
                {
                    var mi = scr.GetType().GetMethod("ApplySkillUpgrades", All);
                    if (mi == null) return new { ok = false, error = "ApplySkillUpgrades not found" };
                    mi.Invoke(scr, null);
                }
                catch (Exception e) { return new { ok = false, error = "apply: " + (e.InnerException ?? e).Message, selectedAfter = n1 }; }
                return new { ok = true, action = "trainerUpgrade", skillId = sid,
                             actorGuid = btn.ActorGuid.ToString(), selectedBefore = sel0,
                             numBefore = n0, numAfterA = n1, numAfterB = scr.NumSelectedSkills };
            }

            // 旅馆行程：休息按钮在没选地区时是灰的，直接读/选 InnBhv 的 biome choice。
            if (type == "innState")
            {
                var inn = GetInn();
                if (inn == null) return new { ok = false, error = "InnBhv not found" };
                var sys = Refl.FieldDeep(inn, "m_InnSystem");
                var list = Refl.FieldDeep(sys, "m_BiomeChoices") as System.Collections.IList;
                var opts = new List<object>();
                if (list != null)
                {
                    for (int i = 0; i < list.Count; i++)
                    {
                        var c = list[i];
                        var gen = Refl.FieldDeep(c, "m_BiomeGenerationData");
                        var bt = Refl.FieldDeep(gen, "m_type");
                        var len = Refl.FieldDeep(gen, "m_runLength");
                        opts.Add(new { i, biome = Convert.ToString(bt), runLength = Convert.ToString(len) });
                    }
                }
                return new
                {
                    ok = true, action = "innState",
                    canEmbark = Refl.Call(inn, "GetCanEmbark") is bool ce && ce,
                    hasChoice = Refl.Call(inn, "HasSelectedBiomeChoice") is bool hc && hc,
                    selIndex = Convert.ToString(Refl.FieldDeep(sys, "m_SelectedBiomeChoiceIndex")),
                    choices = opts,
                };
            }
            if (type == "innSelect")
            {
                var inn = GetInn();
                if (inn == null) return new { ok = false, error = "InnBhv not found" };
                int idx = req.TryGetValue("index", out var ii) ? Convert.ToInt32(ii) : 0;
                Refl.Call(inn, "SetSelectedBiomeChoiceIndex", idx);
                return new { ok = true, action = "innSelect", index = idx,
                             hasChoice = Refl.Call(inn, "HasSelectedBiomeChoice") is bool hc && hc, canEmbark = inn.GetCanEmbark() };
            }

            // 队伍状态：旅馆/地图里也能读到每个英雄的血/压力，以及两两关系（P5）。
            if (type == "partyState")
            {
                var run = FindRun();
                if (run == null) return new { ok = false, error = "RunBhv not found" };
                var am = Refl.FieldDeep(run, "m_AffinityManager");
                var rm = Refl.FieldDeep(am, "m_RosterManager");
                var entries = Refl.FieldDeep(rm, "m_Entries") as System.Collections.IEnumerable;
                var actors = new List<object>();
                var seen = new HashSet<uint>();
                if (entries != null)
                {
                    foreach (var e in entries)
                    {
                        uint g = 0;
                        try { g = Convert.ToUInt32(Refl.FieldDeep(e, "m_ActorGuid")); } catch { }
                        if (g == 0 || !seen.Add(g)) continue;
                        var a = FindActor(g);
                        if (a == null) continue;
                        actors.Add(new
                        {
                            guid = g.ToString(),
                            name = a.ActorName,
                            dataId = a.ActorDataId,
                            hp = Math.Round(a.HpRounded, 1),
                            hpMax = Math.Round(a.CurrentHpMax, 1),
                            hpPct = Math.Round(a.CurrentHpPercent * 100, 1),
                            stress = Math.Round(a.Stress, 1),
                            alive = a.IsLiving,
                        });
                    }
                }
                var conns = new List<object>();
                var cl = Refl.FieldDeep(am, "m_Connections") as System.Collections.IEnumerable;
                if (cl != null)
                {
                    foreach (var c in cl)
                    {
                        if (c == null) continue;
                        var pair = new List<string>();
                        if (Refl.FieldDeep(c, "m_ActorGuids") is System.Collections.IEnumerable gl)
                            foreach (var x in gl) pair.Add(Convert.ToString(x));
                        var rel = Refl.FieldDeep(c, "m_CurrentRelationship");
                        string relId = null;
                        if (rel != null)
                        {
                            relId = (Refl.FieldDeep(rel, "m_Id") as string) ?? rel.ToString();
                        }
                        conns.Add(new
                        {
                            pair,
                            leaning = Convert.ToString(Refl.FieldDeep(c, "m_Leaning")),
                            relationship = relId,
                        });
                    }
                }
                return new { ok = true, action = "partyState", actors, connections = conns };
            }

            // 剧情选项：直接调 StoryChoiceButtonBhv.OnChoiceSelected()（:345，内部只做一件事：
            // EventSelectStoryChoice.Trigger(storyType, actorGuid)）——不用长按、不用指针。
            if (type == "storyChoice")
            {
                var btns2 = UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.Story.StoryChoiceButtonBhv>(true);
                var live = new List<object>();
                foreach (var b in btns2)
                {
                    if (b == null || !b.gameObject.activeInHierarchy) continue;
                    live.Add(b);
                }
                if (live.Count == 0) return new { ok = false, error = "no story choice buttons" };
                int pick2 = req.TryGetValue("index", out var pk2) ? Convert.ToInt32(pk2) : 0;
                if (pick2 < 0 || pick2 >= live.Count) pick2 = 0;
                var target2 = live[pick2];
                try
                {
                    var mi3 = target2.GetType().GetMethod("OnChoiceSelected", All);
                    if (mi3 == null) return new { ok = false, error = "OnChoiceSelected not found" };
                    mi3.Invoke(target2, null);
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = true, action = "storyChoice", index = pick2, count = live.Count };
            }

            // 客栈「休息/出发」：等价于点「休息」→「是」。
            // InnPresentationBhv.cs:891 的确认回调就三件事：藏子界面 → InnBhv.NextState() → InnBhv.CompleteInn()。
            if (type == "innEmbark")
            {
                var inns = Singleton<Assets.Code.Inn.InnBhv>.Instance;
                if (inns == null) return new { ok = false, error = "no InnBhv" };
                if (!inns.GetCanEmbark()) return new { ok = false, error = "cannot embark", state = Safe(() => inns.GetInnState().ToString()) };
                try
                {
                    var pres2 = SingletonMonoBehaviour<Assets.Code.Inn.Presentation.InnPresentationBhv>.Instance;
                    if (pres2 != null) pres2.ClearRestWidgetTargettingAndDeselect();
                }
                catch { }
                inns.NextState();
                inns.CompleteInn();
                return new { ok = true, action = "innEmbark" };
            }

            // 战利品「拿取全部」：LootUiControllerBhv.ButtonTakeAll()（:311 → PlayerInventory.TryTakeAllFrom，成功会自己关界面）。
            if (type == "lootTakeAll")
            {
                var lootc = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.Controllers.LootUiControllerBhv>(true);
                if (lootc == null) return new { ok = false, error = "LootUiControllerBhv not found" };
                try { lootc.ButtonTakeAll(); }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = true, action = "lootTakeAll" };
            }

            // 战利品「继续」（空奖励时）：LootUiControllerBhv.ButtonClose()（:266）
            // → 演出中先 SkipPresentation()，否则 CloseAction()（:296）→ m_onFinished → ForceCloseScreen。
            // 包里还有可拿的东西时，ButtonClose 会弹"确认放弃"对话框——那条路这里不做，
            // 返回 has_items 让 runner 先 lootTakeAll / 点「拿取」。
            if (type == "lootClose")
            {
                var lootc2 = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.Controllers.LootUiControllerBhv>(true);
                if (lootc2 == null) return new { ok = false, error = "LootUiControllerBhv not found" };
                // m_closeBtnPressed 一旦置真，这个实例上的 ButtonClose 就永远 no-op（类里没有重置点）。
                // 真要是复用实例卡住，必须让 runner 退回点按钮，而不是拿 ok:true 死循环。
                if (Refl.FieldDeep(lootc2, "m_closeBtnPressed") is bool pressed2 && pressed2)
                    return new { ok = false, error = "close_already_pressed" };
                int nitems = -1;
                try
                {
                    var cont = Refl.FieldDeep(lootc2, "m_itemContainerBhv");
                    var inv = Refl.FieldDeep(cont, "m_inventory");
                    var items = Refl.Call(inv, "GetValidItems") as System.Collections.ICollection;
                    if (items != null) nitems = items.Count;
                }
                catch { }
                if (nitems > 0) return new { ok = false, error = "has_items", count = nitems };
                bool presenting = false;
                try
                {
                    var w = Refl.FieldDeep(lootc2, "m_lootScreenWidget");
                    var ip = Refl.Call(w, "IsPresenting");
                    presenting = ip is bool b && b;
                }
                catch { }
                try { lootc2.ButtonClose(); }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = true, action = "lootClose", skippedPresentation = presenting, itemsBefore = nitems };
            }

            // 游戏自己算的「如果我用这个技能打这个目标，会掉多少血」。
            // 依据：Assets.Code.Skill.Queries/QuerySkillPreview.cs:29-34（Trigger 走 EventManager），
            //       SkillCalculation.cs:1775-1799（handler 从 m_PotentialResults 取本回合已算好的结果），
            //       SkillCalculation.cs:264-322（字段 m_ToHitChance/m_DamageLow/m_DamageHigh/m_CritChance/m_CritDamage）。
            // 这是 UI 提示框上显示的那个数，含暴击/格挡/易伤/buff 全部乘区 —— 不需要我们复算。
            if (type == "skillPreview")
            {
                uint pguid = req.TryGetValue("actorGuid", out var pgv) ? Convert.ToUInt32(pgv) : 0u;
                uint tguid2 = req.TryGetValue("targetGuid", out var tgv) ? Convert.ToUInt32(tgv) : 0u;
                string skid = req.TryGetValue("skillId", out var skv) ? Convert.ToString(skv) : "";
                if (pguid == 0 || string.IsNullOrEmpty(skid))
                    return new { ok = false, error = "need actorGuid + skillId" };
                try
                {
                    var q = Assets.Code.Skill.Queries.QuerySkillPreview.Trigger(pguid, skid, tguid2);
                    var arr = new List<object>();
                    foreach (var pv in q.m_SkillPreviews)
                    {
                        arr.Add(Preview(pv));
                    }
                    // 查询表里没有（玩家还没选中技能时 m_PotentialResults 是空的）→ 直接让游戏现算：
                    // Battle.m_BattleSkillCalculation（Battle.cs:38,72）→ SkillCalculation.AddPotentialResult
                    // （SkillCalculation.cs:1871）→ SkillResult.ActorResults[].m_SkillPreview。
                    if (arr.Count == 0)
                    {
                        var cb = CombatBhv.Instance;
                        if (cb == null || !cb.IsBattleRunning) return new { ok = false, error = "not in combat" };
                        var bt = Refl.Field(cb, "m_Battle");
                        var sc = Refl.Field(bt, "m_BattleSkillCalculation") as Assets.Code.Skill.SkillCalculation;
                        if (sc == null) return new { ok = false, error = "m_BattleSkillCalculation not found" };
                        var inp = new Assets.Code.Skill.SkillCalculation.Input(pguid, skid, tguid2);
                        var tgts = new List<uint>();
                        if (tguid2 != 0) tgts.Add(tguid2);
                        var res = sc.AddPotentialResult(inp, tgts);
                        if (res != null)
                        {
                            foreach (var ar in res.ActorResults)
                            {
                                if (ar.m_SkillPreview != null) arr.Add(Preview(ar.m_SkillPreview));
                            }
                        }
                    }
                    return new { ok = true, action = "skillPreview", n = arr.Count, previews = arr };
                }
                catch (Exception e)
                {
                    return new { ok = false, error = (e.InnerException ?? e).Message };
                }
            }

            // 战斗真值：BattleConditions.CheckForEnd/GetIsComplete + BattleStateMachine.GetIsStateValidForEnd。
            // 依据 Battle.cs:74,161、BattleConditions.cs:132-154、BattleResult.cs:106-108。
            // 用它替掉 runner 里"actors 空了就当打完"的猜测。
            if (type == "battle")
            {
                var cbk = CombatBhv.Instance;
                if (cbk == null || !cbk.IsBattleRunning) return new { ok = false, error = "not in combat" };
                var btk = Refl.Field(cbk, "m_Battle");
                if (btk == null) return new { ok = false, error = "m_Battle null" };
                var condk = Refl.Field(btk, "m_BattleConditions");
                var smk = Refl.Field(btk, "m_BattleStateMachine");
                return new
                {
                    ok = true,
                    action = "battle",
                    checkForEnd = Refl.Call(condk, "CheckForEnd") is bool c1 && c1,
                    isComplete = Refl.Call(condk, "GetIsComplete") is bool c2 && c2,
                    isRetreat = Refl.Field(condk, "m_IsRetreat") is bool rr && rr,
                    isForceEnd = Refl.Field(condk, "m_IsForceEnd") is bool ff && ff,
                    stateValidForEnd = Refl.Call(smk, "GetIsStateValidForEnd") is bool s1 && s1,
                    state = Safe(() => Convert.ToString(Refl.Call(smk, "GetState")))
                };
            }

            // 整张预览表：SkillCalculation.m_PotentialResults（SkillCalculation.cs:1658,1868）
            // 每个 (施法者, 技能, 目标) 一条，值就是游戏算好的 SkillPreview。
            if (type == "potential")
            {
                int maxn = req.TryGetValue("max", out var mv) ? Convert.ToInt32(mv) : 60;
                var cbp = CombatBhv.Instance;
                if (cbp == null || !cbp.IsBattleRunning) return new { ok = false, error = "not in combat" };
                var btp = Refl.Field(cbp, "m_Battle");
                var scp = Refl.Field(btp, "m_BattleSkillCalculation");
                var dict = Refl.Field(scp, "m_PotentialResults") as System.Collections.IDictionary;
                if (dict == null) return new { ok = false, error = "m_PotentialResults null" };
                var lst = new List<object>();
                foreach (System.Collections.DictionaryEntry de in dict)
                {
                    if (lst.Count >= maxn) break;
                    var inp = de.Key;
                    object pv0 = null;
                    try
                    {
                        // ActorResults / PreviewOnlyActorResults 都是**属性**（SkillCalculation.cs:1421,1423），
                        // GetField 取不到；真正装结果的是私有字段 m_ActorResults / m_PreviewOnlyActorResults。
                        // 以前写 "ActorResults"，每条 preview 恒 null 还回 ok:true。2026-11-22 修。
                        foreach (var fld in new[] { "m_ActorResults", "m_PreviewOnlyActorResults" })
                        {
                            var ars = Refl.Field(de.Value, fld) as System.Collections.IEnumerable;
                            if (ars == null) continue;
                            foreach (var ar in ars)
                            {
                                pv0 = Refl.Field(ar, "m_SkillPreview");
                                if (pv0 != null) break;
                            }
                            if (pv0 != null) break;
                        }
                    }
                    catch { }
                    lst.Add(new
                    {
                        performer = Convert.ToUInt32(Refl.Field(inp, "m_PerformerGuid")),
                        skillId = Convert.ToString(Refl.Field(inp, "m_SelectedSkillId")),
                        target = Convert.ToUInt32(Refl.Field(inp, "m_SelectedTargetActorGuid")),
                        preview = pv0 == null ? null : Preview((Assets.Code.Skill.SkillCalculation.ActorResult.SkillPreview)pv0)
                    });
                }
                return new { ok = true, action = "potential", total = dict.Count, n = lst.Count, entries = lst };
            }

            // 关游戏：让 Unity 自己退（2026-11-21 20:35：外部 Kill 会让 Unity 弹「已停止工作」对话框，
            // 每次都得人去点确定；这条走 Application.Quit，跟点「退出游戏」同一条路）。
            if (type == "quitGame")
            {
                Plugin.Run(QuitNextFrame());
                return new { ok = true, action = "quitGame" };
            }

            // 主菜单「继续征程」：不点按钮，直接走游戏自己的续玩入口
            // （MainMenuControllerBhv.cs:294 OnContinueGameType → :318 BeginGameContinuation
            //  → SaveLoadMgr.SetGameModeFromSave + GameModeMgr 切模式）。
            if (type == "continueRun")
            {
                var mct = typeof(Assets.Code.UI.Managers.MainMenuControllerBhv);
                object mcObj = null;
                try
                {
                    var pi2 = typeof(SingletonMonoBehaviour<Assets.Code.UI.Managers.MainMenuControllerBhv>)
                              .GetProperty("Instance", All);
                    mcObj = pi2 == null ? null : pi2.GetValue(null);
                }
                catch { }
                if (mcObj == null)
                {
                    try { mcObj = UnityEngine.Object.FindObjectOfType(mct, true); } catch { }
                }
                if (mcObj == null)
                    return new { ok = false, error = "MainMenuControllerBhv not found", scene = Safe(() => SceneManager.GetActiveScene().name) };
                Assets.Code.Utils.Serialization.SaveUtils.SaveValidationAction sa;
                Assets.Code.Utils.Serialization.SaveUtils.SaveFailureReason fr;
                bool okc = false;
                try
                {
                    var mi2 = mct.GetMethod("OnContinueGameType", All);
                    if (mi2 == null) return new { ok = false, error = "OnContinueGameType not found" };
                    var args2 = new object[] { Assets.Code.Game.GameType.EXPEDITION, null, null };
                    okc = (bool)mi2.Invoke(mcObj, args2);
                    sa = (Assets.Code.Utils.Serialization.SaveUtils.SaveValidationAction)args2[1];
                    fr = (Assets.Code.Utils.Serialization.SaveUtils.SaveFailureReason)args2[2];
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = okc, action = "continueRun", saveAction = sa.ToString(), reason = fr.ToString() };
            }

            // 客栈商店：**不经过 UI**。货源就在 InnInstance.StoreInventory 里（InnStoreUiBhv.cs:54
            // 也只是把它包成 UI 数据源），买入逻辑照 StoreInventoryItemBhv.OnTryBuyItem（:95-139）
            // 那条：查价 → 扣钱 → TakeItemQty → 给背包 → 发 EventInventoryItemPurchased。
            // 2026-11-21 20:2x：作者说每间客栈都有马车装备卖，之前"6 个 tab 都没商品"就是点页签没点开。
            if (type == "storeList" || type == "storeBuy")
            {
                var inn = Singleton<Assets.Code.Inn.InnBhv>.Instance;
                var innInst = inn == null ? null : inn.GetInnInstance();
                var sinv = innInst == null ? null : innInst.StoreInventory;
                if (sinv == null) return new { ok = false, error = "no store inventory (camp / no inn)" };
                int slots = 0;
                try { slots = sinv.GetNumberOfTotalSlots(); } catch { }
                var list2 = new List<object>();
                for (int i = 0; i < slots; i++)
                {
                    var it2 = sinv.GetItem(i);
                    if (it2 == null) continue;
                    var def2 = it2.GetItemDefinition();
                    if (def2 == null) continue;
                    string tp = "";
                    try { tp = it2.GetItemType().GetName(); } catch { }
                    float mult = 1f;
                    try { mult = Singleton<GameTypeMgr>.Instance.RunDataManager.GetStatValue(
                              Assets.Code.Run.RunStatType.STORE_COST_BUY_MULTIPLIER, tp); } catch { }
                    bool afford = false;
                    try { afford = Assets.Code.Cost.CostCalculation.CanAffordCost(it2.BuyCostDefinition, mult); } catch { }
                    list2.Add(new Dictionary<string, object>
                    {
                        ["i"] = i,
                        ["id"] = def2.m_id,
                        ["type"] = tp,
                        ["tags"] = def2.m_tags,
                        ["qty"] = Safe(() => it2.GetQty()),
                        ["afford"] = afford,
                    });
                }
                if (type == "storeList") return new { ok = true, action = "storeList", count = list2.Count, items = list2 };

                int bi = req.TryGetValue("index", out var bix) ? Convert.ToInt32(bix) : -1;
                if (bi < 0 || bi >= slots) return new { ok = false, error = "index out of range", slots };
                var buyItem = sinv.GetItem(bi);
                if (buyItem == null) return new { ok = false, error = "slot empty", slot = bi };
                var bdef = buyItem.GetItemDefinition();
                string btype = "";
                try { btype = buyItem.GetItemType().GetName(); } catch { }
                float bmult = 1f;
                try { bmult = Singleton<GameTypeMgr>.Instance.RunDataManager.GetStatValue(
                           Assets.Code.Run.RunStatType.STORE_COST_BUY_MULTIPLIER, btype); } catch { }
                var bcost = buyItem.BuyCostDefinition;
                if (bcost != null && !Assets.Code.Cost.CostCalculation.CanAffordCost(bcost, bmult))
                    return new { ok = false, error = "cannot afford", id = bdef == null ? null : bdef.m_id };
                if (bcost != null && !Assets.Code.Cost.CostCalculation.AttemptSpendCost(bcost, bmult, Assets.Code.Source.SourceType.STORE))
                    return new { ok = false, error = "spend failed", id = bdef == null ? null : bdef.m_id };
                try
                {
                    var taken = sinv.TakeItemQty(bi, 1);
                    taken.SetQty(1, 1);
                    var pdef = taken.GetItemDefinition();
                    Singleton<GameTypeMgr>.Instance.PlayerInventory.AddItemsWithOverflow(pdef, 1, true);
                    Assets.Code.Item.Events.EventInventoryItemPurchased.Trigger(pdef, false);
                    return new { ok = true, action = "storeBuy", slot = bi, id = pdef.m_id, type = btype };
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
            }

            // 客栈商店：直接让游戏打开它的子界面（2026-11-21 20:2x：作者说每个客栈都有马车装备可卖，
            // 那"6 个 tab 都没商品"就是我们点页签没点开。ToggleVisibility 就是页签按钮自己走的那条路）。
            if (type == "openInnStore")
            {
                var store = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.Screens.InnStoreUiBhv>(true);
                if (store == null) return new { ok = false, error = "InnStoreUiBhv not found" };
                bool wasOpen = false;
                try { wasOpen = store.ScreenState == Assets.Code.UI.Screens.UiScreenState.Open; } catch { }
                if (!wasOpen)
                {
                    try { store.ToggleVisibility(); }
                    catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                }
                string st2 = "";
                try { st2 = store.ScreenState.ToString(); } catch { }
                return new { ok = true, action = "openInnStore", wasOpen, state = st2 };
            }

            // 商店：列出槽位 / 买第 i 个（OnTryBuyItem 是 Private，走反射）。
            if (type == "store" || type == "buySlot")
            {
                var items = UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.Items.StoreInventoryItemBhv>(true);
                var list = new List<object>();
                foreach (var it in items)
                {
                    if (it == null) continue;
                    var go = it.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    var rt = go.GetComponent<RectTransform>();
                    string pos = "";
                    if (rt != null)
                    {
                        var canvas = go.GetComponentInParent<Canvas>();
                        Camera cam = canvas == null ? Camera.main
                                   : (canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera);
                        var sp = UnityEngine.RectTransformUtility.WorldToScreenPoint(cam, rt.TransformPoint(rt.rect.center));
                        pos = Mathf.RoundToInt(sp.x) + "," + Mathf.RoundToInt(sp.y);
                    }
                    var ctx = Refl.FieldDeep(it, "m_dataContextBhv");
                    string tip = null;
                    try
                    {
                        foreach (var mb in it.GetComponentsInChildren<MonoBehaviour>(true))
                        {
                            if (mb == null) continue;
                            if (mb.GetType().Name.IndexOf("ItemTooltip", StringComparison.OrdinalIgnoreCase) < 0) continue;
                            var mt = Refl.FieldDeep(mb, "m_text") as string;
                            if (!string.IsNullOrEmpty(mt)) { tip = mt; break; }
                        }
                    }
                    catch { }
                    var ctxMap = Refl.FieldDeep(ctx, "m_ContextMap") as System.Collections.IDictionary;
                    var kv = new List<object>();
                    if (ctxMap != null)
                    {
                        foreach (System.Collections.DictionaryEntry de in ctxMap)
                        {
                            string vv = Convert.ToString(de.Value);
                            try
                            {
                                var inner = Refl.FieldDeep(de.Value, "Value");
                                if (inner != null) vv = Convert.ToString(inner);
                            }
                            catch { }
                            kv.Add(new { k = Convert.ToString(de.Key), v = vv });
                        }
                    }
                    list.Add(new { i = list.Count, screen = pos, go = go.name, tip, ctx = kv });
                }
                if (type == "store") return new { ok = true, action = "store", count = list.Count, items = list };
                int idx = req.TryGetValue("index", out var bi) ? Convert.ToInt32(bi) : 0;
                if (idx < 0 || idx >= items.Length) return new { ok = false, error = "index out of range", count = items.Length };
                var target = items[idx];
                if (target == null) return new { ok = false, error = "item null" };
                try
                {
                    var mi = target.GetType().GetMethod("OnTryBuyItem", All);
                    if (mi == null) return new { ok = false, error = "OnTryBuyItem not found" };
                    mi.Invoke(target, null);
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = true, action = "buySlot", index = idx };
            }

            // 背包：按屏幕顺序列出 / 直接丢弃第 i 个（OnDiscardItem，Private，走反射）。
            if (type == "bagSlots" || type == "discardSlot")
            {
                var comps = UnityEngine.Object.FindObjectsOfType<Assets.Code.UI.Items.PlayerInventoryItemBhv>(true);
                var rows = new List<object[]>();
                foreach (var c in comps)
                {
                    if (c == null) continue;
                    var go = c.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    var rt = go.GetComponent<RectTransform>();
                    string pos = "";
                    int yy = 0, xx = 0;
                    if (rt != null)
                    {
                        var canvas = go.GetComponentInParent<Canvas>();
                        Camera cam = canvas == null ? Camera.main
                                   : (canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera);
                        var sp = UnityEngine.RectTransformUtility.WorldToScreenPoint(cam, rt.TransformPoint(rt.rect.center));
                        xx = Mathf.RoundToInt(sp.x); yy = Mathf.RoundToInt(sp.y);
                        pos = xx + "," + yy;
                    }
                    rows.Add(new object[] { yy, xx, pos, c });
                }
                rows.Sort((a, b) =>
                {
                    int ay = Convert.ToInt32(a[0]), by = Convert.ToInt32(b[0]);
                    if (ay != by) return by.CompareTo(ay);
                    return Convert.ToInt32(a[1]).CompareTo(Convert.ToInt32(b[1]));
                });
                var list = new List<object>();
                for (int i = 0; i < rows.Count; i++) list.Add(new { i, screen = Convert.ToString(rows[i][2]) });
                if (type == "bagSlots") return new { ok = true, action = "bagSlots", count = list.Count, slots = list };
                int idx = req.TryGetValue("index", out var bi2) ? Convert.ToInt32(bi2) : 0;
                if (idx < 0 || idx >= rows.Count) return new { ok = false, error = "index out of range", count = rows.Count };
                var target2 = rows[idx][3] as Assets.Code.UI.Items.PlayerInventoryItemBhv;
                try
                {
                    var mi = target2.GetType().GetMethod("OnDiscardItem", All);
                    if (mi == null) return new { ok = false, error = "OnDiscardItem not found" };
                    mi.Invoke(target2, null);
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = true, action = "discardSlot", index = idx };
            }

            // 直接按 itemId 丢 N 个（背包超载时用来腾空间）。
            if (type == "dropItem")
            {
                string itemId = req.TryGetValue("itemId", out var di) ? Convert.ToString(di) : "";
                int n = req.TryGetValue("count", out var dc) ? Convert.ToInt32(dc) : 1;
                var run = FindRun();
                if (run == null) return new { ok = false, error = "RunBhv not found" };
                var pinv = Refl.FieldDeep(run, "m_PlayerInventory");
                int slots = 0;
                try { slots = Convert.ToInt32(Refl.Call(pinv, "GetNumberOfTotalSlots")); } catch { }
                int removed = 0;
                for (int k = 0; k < n; k++)
                {
                    int found = -1;
                    for (int i = 0; i < slots; i++)
                    {
                        var it = Refl.Call(pinv, "GetItem", i);
                        if (it != null && ItemIdOf(it) == itemId) { found = i; break; }
                    }
                    if (found < 0) break;
                    try { Refl.Call(pinv, "RemoveItemAt", found, true); }
                    catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message, removed }; }
                    removed++;
                }
                return new { ok = removed > 0, action = "dropItem", itemId, removed };
            }

            // 队伍背包（含饰品/战斗道具的 id、类型、剩余次数）。
            if (type == "bag")
            {
                var run = FindRun();
                if (run == null) return new { ok = false, error = "RunBhv not found" };
                var pinv = Refl.FieldDeep(run, "m_PlayerInventory");
                var bag = new List<object>();
                int slots = 0;
                try { slots = Convert.ToInt32(Refl.Call(pinv, "GetNumberOfTotalSlots")); } catch { }
                for (int i = 0; i < slots; i++)
                {
                    var it = Refl.Call(pinv, "GetItem", i);
                    if (it == null) continue;
                    bag.Add(new
                    {
                        slot = i,
                        itemId = ItemIdOf(it),
                        cls = ItemTypeOf(it),
                        type = it.GetType().Name,
                        uses = Convert.ToString(Refl.Call(it, "GetQty")),
                    });
                }
                return new { ok = true, action = "bag", bag };
            }

            // 旅馆物品（rest 类）：列出背包里所有 m_usedInInn 的物品 + 每个英雄接不了的原因。
            if (type == "restItems") return RestItems.Build();

            // 用旅馆物品：{type:"useRestItem", itemId:"buff_x", targets:["guid",...], dry:true}
            // targets 留空时只有"全队 4 目标"的物品会自动给全队。
            if (type == "useRestItem")
            {
                string riId = req.TryGetValue("itemId", out var ri) ? Convert.ToString(ri) : "";
                var rTargets = new List<uint>();
                if (req.TryGetValue("targets", out var tv) && tv is System.Collections.IEnumerable tvl && !(tv is string))
                    foreach (var o in tvl) { try { rTargets.Add(Convert.ToUInt32(o)); } catch { } }
                bool rDry = req.TryGetValue("dry", out var dv) && dv is bool dbv && dbv;
                return RestItems.Use(riId, rTargets, rDry);
            }

            // 饰品：每个英雄已装备的 + 队伍背包里能装的（P2）。
            if (type == "trinkets")
            {
                var run = FindRun();
                if (run == null) return new { ok = false, error = "RunBhv not found" };
                var am = Refl.FieldDeep(run, "m_AffinityManager");
                var rm = Refl.FieldDeep(am, "m_RosterManager");
                var entries = Refl.FieldDeep(rm, "m_Entries") as System.Collections.IEnumerable;
                var actors = new List<object>();
                if (entries != null)
                {
                    foreach (var e in entries)
                    {
                        uint g = 0;
                        try { g = Convert.ToUInt32(Refl.FieldDeep(e, "m_ActorGuid")); } catch { }
                        if (g == 0) continue;
                        var a = FindActor(g);
                        if (a == null) continue;
                        var inv = Refl.FieldDeep(a, "m_TrinketInventory");
                        var ids = new List<string>();
                        var items = Refl.Call(inv, "GetItems") as System.Collections.IEnumerable;
                        if (items != null)
                            foreach (var it in items)
                            {
                                var id = ItemIdOf(it);
                                if (!string.IsNullOrEmpty(id)) ids.Add(id);
                            }
                        actors.Add(new { guid = g.ToString(), name = a.ActorName, dataId = a.ActorDataId,
                                         hp = Math.Round(a.HpRounded, 1), stress = Math.Round(a.Stress, 1),
                                         trinkets = ids, combat = CombatItemIds(a) });
                    }
                }
                var bag = new List<object>();
                var pinv = Refl.FieldDeep(run, "m_PlayerInventory");
                int slots = 0;
                try { slots = Convert.ToInt32(Refl.Call(pinv, "GetNumberOfTotalSlots")); } catch { }
                for (int i = 0; i < slots; i++)
                {
                    var it = Refl.Call(pinv, "GetItem", i);
                    if (it == null) continue;
                    bag.Add(new { slot = i, itemId = ItemIdOf(it),
                                 cls = ItemTypeOf(it),
                                 uses = Convert.ToString(Refl.Call(it, "GetQty")) });
                }
                return new { ok = true, action = "trinkets", actors, bag };
            }

            // 只读：把游戏里**所有饰品定义**的本地化名字 + 描述吐出来。
            // 为什么加：本地 data/trinket_stats.json 里 68/191 件解析不出数值、172 件 desc 为空，
            // 这些文本游戏自己有（ItemDescription.GetTitle / GetDescription），读一遍就能补齐。
            // 不碰存档、不改游戏状态，纯读（2026-11-22 加）。
            if (type == "trinketInfo")
            {
                var list = new List<object>();
                string how = "";
                try
                {
                    // 取实例的三条路，按可靠性从高到低（2026-11-22 08:1x 实测：
                    // 主菜单下 LibraryItem.LibraryItemInstance 是 null —— s_instance 还没挂上）。
                    Assets.Code.Item.LibraryItem lib = null;
                    try { lib = Assets.Code.Item.LibraryItem.LibraryItemInstance; } catch { }
                    if (lib != null) how = "LibraryItemInstance";
                    if (lib == null)
                    {
                        try
                        {
                            lib = UnityEngine.Object.FindObjectOfType<Assets.Code.Item.LibraryItem>(true);
                            if (lib != null) how = "FindObjectOfType";
                        }
                        catch { }
                    }
                    if (lib == null)
                    {
                        try
                        {
                            lib = SingletonMonoBehaviour<Library<string, Assets.Code.Item.ItemDefinition>>.Instance
                                  as Assets.Code.Item.LibraryItem;
                            if (lib != null) how = "SingletonMonoBehaviour";
                        }
                        catch { }
                    }
                    if (lib == null)
                        return new { ok = false, error = "library not ready（主菜单/加载中，进局后再试）" };
                    var all = lib.GetLibraryElements();
                    for (int i = 0; i < (all == null ? 0 : all.Count); i++)
                    {
                        var def = all[i];
                        if (def == null) continue;
                        string tn = "";
                        try { tn = def.m_type == null ? "" : def.m_type.GetName(); } catch { }
                        if (!string.Equals(tn, "trinket", StringComparison.OrdinalIgnoreCase)) continue;
                        string title = "", desc = "";
                        try { title = Assets.Code.Item.ItemDescription.GetTitle(def, 0) ?? ""; } catch { }
                        try { desc = Assets.Code.Item.ItemDescription.GetDescription(def, 1, false, 0, false, false) ?? ""; } catch { }
                        var tags = new List<string>();
                        try { if (def.m_tags != null) foreach (var tagStr in def.m_tags) tags.Add(tagStr); } catch { }
                        list.Add(new Dictionary<string, object>
                        {
                            ["id"] = def.m_id ?? "",
                            ["title"] = title,
                            ["desc"] = desc,
                            ["tags"] = tags,
                        });
                    }
                }
                catch (Exception e)
                {
                    return new { ok = false, error = e.GetType().Name + ": " + e.Message };
                }
                return new { ok = true, action = "trinketInfo", count = list.Count, via = how, trinkets = list };
            }

            // 装/卸饰品：直接把队伍背包的格子与英雄饰品栏的格子对调。
            // 2026-11-21：队伍名册（不在战斗里也能读到）——道途/战斗道具要在客栈里决策，
            // 而 /state 的 actors 只有战斗中存在。用游戏自己的名册接口取 PARTY 成员：
            //   Singleton<GameTypeMgr>.Instance.RosterManager.GetActorGuids(RosterStatusType.PARTY)
            if (type == "partyInfo")
            {
                var info = new List<object>();
                try
                {
                    // 用 partyState 同一套枚举（RunBhv → AffinityManager → RosterManager → m_Entries）：
                    // RosterManager.GetActorGuids(PARTY) 在跑图模式下会返回空（实测 count=0），这条才稳。
                    var run2 = FindRun();
                    if (run2 == null) return new { ok = false, error = "RunBhv not found" };
                    var am2 = Refl.FieldDeep(run2, "m_AffinityManager");
                    var rm2 = Refl.FieldDeep(am2, "m_RosterManager");
                    var entries2 = Refl.FieldDeep(rm2, "m_Entries") as System.Collections.IEnumerable;
                    var seen2 = new HashSet<uint>();
                    if (entries2 != null)
                    {
                        foreach (var e in entries2)
                        {
                            uint g = 0;
                            try { g = Convert.ToUInt32(Refl.FieldDeep(e, "m_ActorGuid")); } catch { }
                            if (g == 0 || !seen2.Add(g)) continue;
                            var a = FindActor(g);
                            if (a == null)
                            {
                                // 2026-11-23：DEAD 的 isActorRequired=false（RosterStatusType.cs:15），
                                // actor 不在库时这里原来直接 continue，于是"死了"和"条目被删"
                                // 在 API 上长得一模一样（都是 party 里少一人）。只读补一条：
                                // status/classId 仍从名册条目读（RosterEntry.cs:12/16），不碰游戏状态。
                                info.Add(new Dictionary<string, object>
                                {
                                    ["guid"] = g.ToString(),
                                    ["dataId"] = null,
                                    ["name"] = null,
                                    ["status"] = Convert.ToString(Refl.FieldDeep(e, "m_RosterStatus")),
                                    ["classId"] = Safe(() => Convert.ToString(Refl.FieldDeep(e, "m_ActorClassId"))),
                                    ["actorFound"] = false,
                                    ["hp"] = null,
                                    ["hpMax"] = null,
                                    ["path"] = null,
                                    ["combatItems"] = new List<string>(),
                                    ["combatItemSlots"] = 0,
                                });
                                continue;
                            }
                            var citems = new List<string>();
                            try
                            {
                                foreach (var cid in a.GetCombatSkillInventoryIds())
                                    if (!string.IsNullOrEmpty(cid)) citems.Add(cid);
                            }
                            catch { }
                            int cslots = 0;
                            try { cslots = Convert.ToInt32(Refl.Call(a.GetCombatSkillInventory(), "GetNumberOfTotalSlots")); } catch { }
                            string pid = null;
                            try { pid = a.ActorDataPath == null ? null : a.ActorDataPath.Id; } catch { }
                            info.Add(new Dictionary<string, object>
                            {
                                ["guid"] = g.ToString(),
                                ["dataId"] = Safe(() => a.ActorDataId),
                                ["name"] = Safe(() => a.ActorName),
                                // 名册状态：party / idle / reserve …（取 PARTY 才装道具/改道途）
                                ["status"] = Convert.ToString(Refl.FieldDeep(e, "m_RosterStatus")),
                                ["hp"] = Safe(() => Math.Round(a.HpRounded, 1)),
                                ["hpMax"] = Safe(() => Math.Round(a.CurrentHpMax, 1)),
                                ["path"] = pid,
                                ["combatItems"] = citems,
                                ["combatItemSlots"] = cslots,
                                // 2026-11-23：给"减员到底是死还是被删"留个锚。classId 一直在名册条目上
                                // （RosterEntry.cs:12），不依赖 actor 存不存在；actorFound 与上面 actor==null
                                // 那条对称，这样消费方不用猜"这行是缺 actor 还是真没这条"。
                                ["classId"] = Safe(() => Convert.ToString(Refl.FieldDeep(e, "m_ActorClassId"))),
                                ["actorFound"] = true,
                            });
                        }
                    }
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                return new { ok = true, action = "partyInfo", count = info.Count, party = info };
            }

            // 2026-11-21：道途（Path）选择。每人一个 ActorDataPath，改它=换整套技能替换/属性。
            // 游戏自己的接口：ActorInstance.SetActorPath(path, refundUnlockedSkills)（ActorInstance.cs:2452），
            // 可用列表：ActorPathCalculation.GetActorDataPaths(actor, isHeroSelect)（ActorPathCalculation.cs:10）。
            // 客栈里改道途是要付费的（InnDefinition.ActorDataPathChangeCost），
            // UI 里确认时也是直接 SetActorPath(path, true)，这里走同一条路，不自己算钱。
            if (type == "paths" || type == "setPath")
            {
                uint g = req.TryGetValue("actorGuid", out var pg) ? Convert.ToUInt32(pg) : 0u;
                var actor = FindActor(g);
                if (actor == null) return new { ok = false, error = "actor not found: " + g };
                var list = new List<object>();
                try
                {
                    foreach (var p in Assets.Code.Actor.ActorPathCalculation.GetActorDataPaths(actor, false))
                    {
                        if (p == null) continue;
                        string nm = "";
                        try { nm = Assets.Code.Actor.ActorPathDescription.GetNameString(p, "", false); } catch { }
                        string ds = "";
                        try { ds = Assets.Code.Actor.ActorPathDescription.GetDescriptionString(p, null); } catch { }
                        // 道途到底换了哪些技能（2026-11-21）：Game 里 ActorDataPath 带一份
                        // ActorDataSkillReplacement，RefreshSkillReplacements 就按它把基础技能换掉。
                        // 这里把 from→to 全列出来，Python 才能按"实际技能"挑道途，而不是猜描述。
                        var reps = new List<object>();
                        try
                        {
                            var repData = p.GetDataContainerSum<Assets.Code.Skill.ActorDataSkillReplacement>();
                            if (repData != null && repData.SourceSkillReplacements != null)
                                foreach (var sr in repData.SourceSkillReplacements)
                                {
                                    if (sr == null || sr.Definition == null) continue;
                                    reps.Add(new Dictionary<string, object>
                                    {
                                        ["from"] = sr.Definition.m_FromActorDataSkillId,
                                        ["to"] = sr.Definition.m_ToActorDataSkillId,
                                    });
                                }
                        }
                        catch { }
                        list.Add(new Dictionary<string, object>
                        {
                            ["id"] = p.Id,
                            ["name"] = nm,
                            ["desc"] = ds,
                            ["replaces"] = reps,
                            ["current"] = actor.ActorDataPath != null && actor.ActorDataPath.Id == p.Id,
                        });
                    }
                }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message }; }
                string curId = null;
                try { curId = actor.ActorDataPath == null ? null : actor.ActorDataPath.Id; } catch { }
                if (type == "paths")
                    return new { ok = true, action = "paths", actorGuid = g.ToString(), current = curId, paths = list };

                string pathId = req.TryGetValue("pathId", out var pi) ? Convert.ToString(pi) : "";
                if (string.IsNullOrEmpty(pathId)) return new { ok = false, error = "pathId required", current = curId };
                var lib = SingletonMonoBehaviour<Library<string, Assets.Code.Actor.ActorDataPath>>.Instance;
                var target = lib == null ? null : lib.GetLibraryElement(pathId);
                if (target == null) return new { ok = false, error = "path not found: " + pathId, current = curId };
                bool isAvail = false;
                foreach (var x in list)
                {
                    var d = x as Dictionary<string, object>;
                    if (d != null && Convert.ToString(d["id"]) == pathId) { isAvail = true; break; }
                }
                if (!isAvail) return new { ok = false, error = "path not available for this hero", current = curId, pathId };
                try { actor.SetActorPath(target, true); }
                catch (Exception e) { return new { ok = false, error = (e.InnerException ?? e).Message, current = curId }; }
                string now = null;
                try { now = actor.ActorDataPath == null ? null : actor.ActorDataPath.Id; } catch { }
                return new { ok = now == pathId, action = "setPath", actorGuid = g.ToString(), was = curId, now, pathId };
            }

            // 2026-11-21：马车装备（马车匠的升级件）。游戏接口：
            //   Singleton<GameTypeMgr>.Instance.StageCoach.GetSlotInventory(itemDefinition.m_slot)
            //   → DataItemInventory（StageCoach.cs:183），装法同样是目标背包 .SwapItems(源背包, 源下标, 目标下标, true)。
            // 槽位四类：GENERAL / TROPHY / PET / FLAME。
            if (type == "coachSlots" || type == "installCoach")
            {
                var sc = Singleton<GameTypeMgr>.Instance.StageCoach;
                if (sc == null) return new { ok = false, error = "no stagecoach" };
                var stypes = new[] { Assets.Code.Item.ItemSlotType.GENERAL, Assets.Code.Item.ItemSlotType.TROPHY,
                                     Assets.Code.Item.ItemSlotType.PET, Assets.Code.Item.ItemSlotType.FLAME };
                if (type == "coachSlots")
                {
                    var outSlots = new List<object>();
                    foreach (var st2 in stypes)
                    {
                        object inv = null;
                        try { inv = sc.GetSlotInventory(st2); } catch { }
                        if (inv == null) continue;
                        var ids = new List<string>();
                        int total = 0, filled = 0;
                        try
                        {
                            total = Convert.ToInt32(Refl.Call(inv, "GetNumberOfTotalSlots"));
                            filled = Convert.ToInt32(Refl.Call(inv, "GetNumberOfFilledSlots"));
                            var its = Refl.Call(inv, "GetItems") as System.Collections.IEnumerable;
                            if (its != null)
                                foreach (var it in its)
                                {
                                    var id = ItemIdOf(it);
                                    if (!string.IsNullOrEmpty(id)) ids.Add(id);
                                }
                        }
                        catch { }
                        outSlots.Add(new Dictionary<string, object>
                        {
                            ["slot"] = st2.ToString(),
                            ["total"] = total,
                            ["filled"] = filled,
                            ["items"] = ids,
                        });
                    }
                    return new { ok = true, action = "coachSlots", slots = outSlots };
                }

                string cid = req.TryGetValue("itemId", out var ci2) ? Convert.ToString(ci2) : "";
                if (string.IsNullOrEmpty(cid)) return new { ok = false, error = "itemId required" };
                var run3 = FindRun();
                if (run3 == null) return new { ok = false, error = "RunBhv not found" };
                var pinv3 = Refl.FieldDeep(run3, "m_PlayerInventory");
                if (pinv3 == null) return new { ok = false, error = "player inventory not found" };
                int foundIdx = -1;
                int slotsTotal = 0;
                try { slotsTotal = Convert.ToInt32(Refl.Call(pinv3, "GetNumberOfTotalSlots")); } catch { }
                object itemInst = null;
                for (int i2 = 0; i2 < slotsTotal; i2++)
                {
                    var it = Refl.Call(pinv3, "GetItem", i2);
                    if (it != null && ItemIdOf(it) == cid) { foundIdx = i2; itemInst = it; break; }
                }
                if (foundIdx < 0) return new { ok = false, error = "item not in bag: " + cid };
                var def3 = Refl.Call(itemInst, "GetItemDefinition");
                var slotType = def3 == null ? null : Refl.FieldDeep(def3, "m_slot");
                if (slotType == null) return new { ok = false, error = "item has no slot type" };
                var tinv3 = Refl.Call(sc, "GetSlotInventory", slotType);
                if (tinv3 == null) return new { ok = false, error = "slot inventory not found: " + slotType };
                int tTotal = Convert.ToInt32(Refl.Call(tinv3, "GetNumberOfTotalSlots"));
                int tFree = -1;
                for (int i3 = 0; i3 < tTotal; i3++)
                {
                    var it = Refl.Call(tinv3, "GetItem", i3);
                    if (it == null) { tFree = i3; break; }
                }
                if (tFree < 0) return new { ok = false, error = "coach slot full: " + slotType, slot = slotType.ToString(), total = tTotal };
                try
                {
                    var mi3 = tinv3.GetType().GetMethod("SwapItems", All, null,
                        new[] { typeof(Assets.Code.Item.ItemInventory), typeof(int), typeof(int), typeof(bool) }, null);
                    if (mi3 != null)
                        mi3.Invoke(tinv3, new object[] { pinv3, foundIdx, tFree, true });
                    else
                        Refl.Call(tinv3, "DoSafeSwapItems", pinv3, foundIdx, tFree);
                }
                catch (Exception e)
                {
                    return new { ok = false, error = (e.InnerException ?? e).Message, slot = slotType.ToString() };
                }
                var nowIds = new List<string>();
                try
                {
                    var its = Refl.Call(tinv3, "GetItems") as System.Collections.IEnumerable;
                    if (its != null) foreach (var it in its) { var id = ItemIdOf(it); if (!string.IsNullOrEmpty(id)) nowIds.Add(id); }
                }
                catch { }
                return new { ok = true, action = "installCoach", itemId = cid, slot = slotType.ToString(), equipped = nowIds };
            }

            if (type == "equipTrinket")
            {
                uint g = req.TryGetValue("actorGuid", out var eg) ? Convert.ToUInt32(eg) : 0u;
                string itemId = req.TryGetValue("itemId", out var ei) ? Convert.ToString(ei) : "";
                int slot = req.TryGetValue("slot", out var es) ? Convert.ToInt32(es) : 0;
                string kind = req.TryGetValue("kind", out var ek) ? Convert.ToString(ek) : "trinket";
                var run = FindRun();
                if (run == null) return new { ok = false, error = "RunBhv not found" };
                var actor = FindActor(g);
                if (actor == null) return new { ok = false, error = "actor not found: " + g };
                var tinv = kind == "combat" ? Refl.FieldDeep(actor, "m_CombatInventory")
                                            : Refl.FieldDeep(actor, "m_TrinketInventory");
                var pinv = Refl.FieldDeep(run, "m_PlayerInventory");
                if (tinv == null || pinv == null) return new { ok = false, error = "inventory not found" };
                int found = -1;
                int slots = 0;
                try { slots = Convert.ToInt32(Refl.Call(pinv, "GetNumberOfTotalSlots")); } catch { }
                for (int i = 0; i < slots; i++)
                {
                    var it = Refl.Call(pinv, "GetItem", i);
                    if (it != null && ItemIdOf(it) == itemId) { found = i; break; }
                }
                if (found < 0) return new { ok = false, error = "item not in bag: " + itemId };
                // 2026-11-23 作者定（方案 A）：DLL 侧自己也验一道类型 —— SwapItems 是"给什么塞什么"，
                // Python 侧一漏判就直接污染存档（valley_baubles 就是这么被塞进 Baldwin 饰品槽的）。
                // 只审 trinket 这条路；kind=combat 走 m_CombatInventory，类型名没验过就不动它。
                if (kind != "combat")
                {
                    string cls = null;
                    try { cls = ItemTypeOf(Refl.Call(pinv, "GetItem", found)); } catch { }
                    if (cls != "trinket")
                        return new { ok = false, error = "not a trinket: " + itemId + " cls=" + (cls ?? "?"),
                                     itemId, kind, typeCheck = "reject", order = "none" };
                }
                int tinvSlots = 0;
                try { tinvSlots = Convert.ToInt32(Refl.Call(tinv, "GetNumberOfTotalSlots")); } catch { }
                int tinvUnlocked = 0;
                try { tinvUnlocked = Convert.ToInt32(Refl.Call(tinv, "GetNumberOfUnlockedSlots")); } catch { }
                if (tinvUnlocked < 1)
                {
                    // 没解锁的槽位 SwapItems 会越界；先试游戏的解锁/扩槽接口。
                    try { Refl.Call(tinv, "UnlockAllLockedSlots"); } catch { }
                    try { tinvUnlocked = Convert.ToInt32(Refl.Call(tinv, "GetNumberOfUnlockedSlots")); } catch { }
                    if (tinvUnlocked < 1)
                    {
                        try { Refl.Call(tinv, "AddSlots", 1, true); } catch { }
                        try { tinvUnlocked = Convert.ToInt32(Refl.Call(tinv, "GetNumberOfUnlockedSlots")); } catch { }
                    }
                }
                try
                {
                    if (slot < 0 || (tinvSlots > 0 && slot >= tinvSlots))
                        return new { ok = false, error = "slot out of range: " + slot + " / " + tinvSlots, tinvSlots, tinvUnlocked, order = "none" };
                    // 2026-11-21 06:5x 用 IronCrown.dll 的**元数据**实锤了签名（不再猜）：
                    //   ItemInventory.SwapItems(ItemInventory sourceInventory, int sourceIndex, int index, bool isPurchase)
                    // 它是"在**目标**背包上调用、把 source 里第 sourceIndex 件搬到本背包的第 index 格"。
                    // 所以装饰品必须这样调：
                    //   tinv.SwapItems(pinv, found, slot, true)   ← 目标=饰品栏，源=玩家背包
                    // 以前写成 pinv.SwapItems(tinv, found, slot) —— 目标反了，等于"把饰品栏第 6 件
                    // 搬回背包"，而饰品栏只有 4 格 → IndexOutOfRange。这才是 153 次失败的真正原因。
                    var mi = tinv.GetType().GetMethod("SwapItems", All, null,
                        new[] { typeof(Assets.Code.Item.ItemInventory), typeof(int), typeof(int), typeof(bool) }, null);
                    if (mi == null) return new { ok = false, error = "SwapItems(ItemInventory,int,int,bool) not found", tinvSlots, tinvUnlocked };
                    string order;
                    try
                    {
                        mi.Invoke(tinv, new object[] { pinv, found, slot, true });
                        order = "tinv.SwapItems(pinv,found,slot)";
                    }
                    catch (Exception eMain)
                    {
                        // 兜底：DoSafeSwapItems(sourceInventory, sourceIndex, index) —— 游戏自己的安全版
                        var mi2 = tinv.GetType().GetMethod("DoSafeSwapItems", All, null,
                            new[] { typeof(Assets.Code.Item.ItemInventory), typeof(int), typeof(int) }, null);
                        if (mi2 == null) throw eMain;
                        mi2.Invoke(tinv, new object[] { pinv, found, slot });
                        order = "tinv.DoSafeSwapItems(pinv,found,slot)";
                    }
                    var okIds = new List<string>();
                    var okItems = Refl.Call(tinv, "GetItems") as System.Collections.IEnumerable;
                    if (okItems != null)
                        foreach (var it in okItems)
                        {
                            var id = ItemIdOf(it);
                            if (!string.IsNullOrEmpty(id)) okIds.Add(id);
                        }
                    return new { ok = true, action = "equipTrinket", actorGuid = g.ToString(), itemId, slot, kind, equipped = okIds, order, tinvSlots, tinvUnlocked };
                }
                catch (Exception e)
                {
                    return new { ok = false, error = (e.InnerException ?? e).Message, tinvSlots, tinvUnlocked };
                }
            }
            // 诊断：把训练师界面的实例字段全 dump 出来（m_upgradeHoldTime / 选中集合 / 点数）。
            if (type == "trainerState")
            {
                var scr = UnityEngine.Object.FindObjectOfType<Assets.Code.UI.Screens.InnUpgradeSkillsBhv>();
                if (scr == null) return new { ok = false, error = "trainer screen not open" };
                var vals = new List<object>();
                foreach (var f in scr.GetType().GetFields(All))
                {
                    object v = null;
                    try { v = f.GetValue(scr); } catch { continue; }
                    string sv = v == null ? "null"
                              : v is UnityEngine.Object uo ? (uo == null ? "<destroyed>" : uo.name)
                              : v is System.Collections.ICollection cc ? "count=" + cc.Count
                              : v.ToString();
                    vals.Add(new { n = f.Name, t = f.FieldType.Name, v = sv });
                }
                return new { ok = true, action = "trainerState", fields = vals };
            }

            // 拖拽：饰品/战斗道具装备必须"拖"（背包格子 → 角色表槽位），点击不管用。
            if (type == "drag")
            {
                float x1 = req.TryGetValue("x1", out var dx1) ? Convert.ToSingle(dx1) : 0f;
                float y1 = req.TryGetValue("y1", out var dy1) ? Convert.ToSingle(dy1) : 0f;
                float x2 = req.TryGetValue("x2", out var dx2) ? Convert.ToSingle(dx2) : 0f;
                float y2 = req.TryGetValue("y2", out var dy2) ? Convert.ToSingle(dy2) : 0f;
                float hold = req.TryGetValue("hold", out var dh) ? Convert.ToSingle(dh) : 0.25f;
                var mouseNow = UnityEngine.InputSystem.Mouse.current;
                if (mouseNow == null) return new { ok = false, error = "Mouse.current is null" };
                Plugin.Run(DragRoutine(mouseNow, x1, y1, x2, y2, hold));
                return new { ok = true, action = "drag", from = x1 + "," + y1, to = x2 + "," + y2 };
            }

            if (type == "mouse")
            {
                float mx = req.TryGetValue("x", out var vmx) ? Convert.ToSingle(vmx) : 960f;
                float my = req.TryGetValue("y", out var vmy) ? Convert.ToSingle(vmy) : 540f;
                bool lb = req.TryGetValue("left", out var vlb) && Convert.ToBoolean(vlb);
                bool rb = req.TryGetValue("right", out var vrb) && Convert.ToBoolean(vrb);
                return VirtualInput.MouseMove(mx, my, lb, rb);
            }
            if (type == "key")
            {
                string keys = req.TryGetValue("keys", out var vk) ? Convert.ToString(vk) : "w";
                bool up = req.TryGetValue("up", out var vu) && Convert.ToBoolean(vu);
                return VirtualInput.Keys(keys, up);
            }
            if (type == "vinfo") return VirtualInput.Info();
            if (type == "coach") return Coach.Info();
            if (type == "coachUnblock") return Coach.Unblock();
            if (type == "coachReset") return Coach.ResetCoach();
            if (type == "coachCall")
            {
                string m = req.TryGetValue("method", out var cm) ? Convert.ToString(cm) : "";
                object a = req.TryGetValue("arg", out var ca) ? ca : null;
                return Coach.CallMethod(m, a);
            }
            if (type == "coachSet")
            {
                string f = req.TryGetValue("field", out var cf) ? Convert.ToString(cf) : "";
                object a = req.TryGetValue("value", out var cv) ? cv : null;
                return Coach.SetField(f, a);
            }
            if (type == "coachGo")
            {
                float gx = req.TryGetValue("x", out var cx2) ? Convert.ToSingle(cx2) : 0f;
                float gz = req.TryGetValue("z", out var cz2) ? Convert.ToSingle(cz2) : 0f;
                return Coach.GoTo(gx, gz);
            }
            if (type == "coachMode")
            {
                string md = req.TryGetValue("mode", out var cm2) ? Convert.ToString(cm2) : "PLAYER_INPUT";
                return Coach.SetMode(md);
            }
            // 2026-11-23 作者口径：游戏里的操作全走游戏 API，不再模拟键鼠。
            // 马车前进 = 直接置 m_inputAlwaysMoveForward（以前是 key W）。
            if (type == "wagonDrive")
            {
                bool won = !req.TryGetValue("on", out var vwon) || Convert.ToBoolean(vwon);
                int whold = req.TryGetValue("holdMs", out var vwh) ? Convert.ToInt32(vwh) : 0;
                return Coach.Drive(won, whold);
            }
            // 2026-11-24：自动挡（心跳 4 s）。比 wagonDrive 多了"逐帧补 + 节点/UI 结束后自动续"，
            // 依据 StageCoachInternalState.UpdateSpeed:185-218 与 :247-251，见 Coach.AutoTick() 注释。
            if (type == "driveAuto")
            {
                bool aon = !req.TryGetValue("on", out var vao) || Convert.ToBoolean(vao);
                return Coach.Auto(aon);
            }
            // 取消已选技能/目标（以前是右键点屏幕正中，或 Esc）。
            if (type == "cancelSelection") return CombatUi.CancelSelection();
            // 触发游戏自己注册的输入动作（Submit / ExitMenu / PauseMenu / CharacterSheet …），
            // 走 Code.FlyingRat.Simulator.InputSimulatorUtils，不碰真实键鼠。
            if (type == "simAction")
            {
                // 2026-11-23 自查第五遍：不给默认名。原来缺 name 会默认按 Submit，
                // 在一屏上可能误触发确认；现在缺参直接回错。
                string sin = req.TryGetValue("name", out var vsi) ? Convert.ToString(vsi) : "";
                string sph = req.TryGetValue("phase", out var vsp) ? Convert.ToString(vsp) : "click";
                return SimAction.Fire(sin, sph);
            }
            // 岔路口：读可选路线 / 替玩家选一条（车停在岔路口时唯一能解开的手势）
            if (type == "intersect") return Intersection.Info();
            if (type == "intersectSelect")
            {
                int oi = req.TryGetValue("index", out var oiv) ? Convert.ToInt32(oiv) : 0;
                return Intersection.Select(oi);
            }
            if (type == "uiInvoke")
            {
                string pick = req.TryGetValue("text", out var ivx) ? Convert.ToString(ivx) : "";
                int iidx = req.TryGetValue("index", out var ivi) ? Convert.ToInt32(ivi) : -1;
                return UiDriver.Invoke(pick, iidx);
            }
            // 按屏幕坐标点（射线 + 事件分发，不注入鼠标）：pointerClick 的 API 版。
            if (type == "uiPoint")
            {
                float px = req.TryGetValue("x", out var vpx) ? Convert.ToSingle(vpx) : 960f;
                float py = req.TryGetValue("y", out var vpy) ? Convert.ToSingle(vpy) : 540f;
                int hold = req.TryGetValue("holdMs", out var vhd) ? Convert.ToInt32(vhd) : 0;
                return UiDriver.PointClick(px, py, hold);
            }
            // 按屏幕坐标拖拽（射线 + drag 事件分发）：drag 动作的 API 版。
            if (type == "uiDrag")
            {
                float dx1 = req.TryGetValue("x1", out var vdx1) ? Convert.ToSingle(vdx1) : 0f;
                float dy1 = req.TryGetValue("y1", out var vdy1) ? Convert.ToSingle(vdy1) : 0f;
                float dx2 = req.TryGetValue("x2", out var vdx2) ? Convert.ToSingle(vdx2) : 0f;
                float dy2 = req.TryGetValue("y2", out var vdy2) ? Convert.ToSingle(vdy2) : 0f;
                return UiDriver.PointDrag(dx1, dy1, dx2, dy2);
            }
            if (type == "heroes") return Party.Info();
            if (type == "party")
            {
                string ids = req.TryGetValue("dataIds", out var pd) ? Convert.ToString(pd) : "";
                bool cf = req.TryGetValue("confirm", out var pc) && Convert.ToBoolean(pc);
                return Party.Build(ids, cf);
            }
            // 2026-11-22 作者要求：技能也要能走 API 改，不要点界面。
            // 依据 Assets.Code.Actor.ActorInstance：
            //   SetBaseCombatSkillIdsEquipped(IReadOnlyList<string>)  （decompiled L374227）
            //   GetBaseEquippedCombatSkillIds() / GetEquippedCombatSkillIds()（L374384 / L374291）
            // 2026-11-23：只读 —— 这个英雄**这局到底有哪些战斗技能**（跑图/客栈也能查）。
            // 依据 logs\decomp\all\Assets.Code.Actor\ActorInstance.cs:
            //   GetUnlockedCharacterSheetCombatSkillIds() / GetEquippedCharacterSheetCombatSkillIds() /
            //   GetLockedCombatSkillIds() / GetCombatBaseSkillId(SkillInstance)：
            //   后者对"替换型"变体返回被替换的那个技能（GetCombatReplacedSkill），也就是
            //   SetBaseCombatSkillIdsEquipped 唯一认的 base id。
            // 为什么要它：db/skills.loadout.json 里存的是变体 id（_p1/_p2），而 SetBaseCombatSkillIdsEquipped
            // 只匹配 base id → 变体 id 全被丢弃、技能栏被截断（2026-11-23 实机 Audrey 5→2）。
            if (type == "heroSkills")
            {
                uint hg = 0;
                try { hg = Convert.ToUInt32(req.TryGetValue("actorGuid", out var hgv) ? hgv : 0); } catch { }
                if (hg == 0) return new { ok = false, error = "need actorGuid" };
                var ha = FindActor(hg);
                if (ha == null) return new { ok = false, error = "actor not found: " + hg };
                var t3 = ha.GetType();
                Func<string, List<string>> callIds = (name) =>
                {
                    var o = new List<string>();
                    try
                    {
                        var mi = t3.GetMethod(name, All);
                        var res = mi == null ? null : mi.Invoke(ha, null) as System.Collections.IEnumerable;
                        if (res != null) foreach (var x in res) if (x != null) o.Add(x.ToString());
                    }
                    catch { }
                    return o;
                };
                var unlocked = callIds("GetUnlockedCharacterSheetCombatSkillIds");
                var equipped = callIds("GetEquippedCharacterSheetCombatSkillIds");
                var locked = callIds("GetLockedCombatSkillIds");
                var baseOf = new Dictionary<string, string>();
                try
                {
                    var getBase = t3.GetMethod("GetCombatBaseSkillId", All);
                    var inst = t3.GetMethod("GetCombatSkillInstance", All);
                    if (getBase != null && inst != null)
                    {
                        foreach (var sid in unlocked.Concat(equipped).Concat(locked).Distinct())
                        {
                            try
                            {
                                var si = inst.Invoke(ha, new object[] { sid });
                                if (si == null) continue;
                                var b = Convert.ToString(getBase.Invoke(ha, new object[] { si }));
                                if (!string.IsNullOrEmpty(b)) baseOf[sid] = b;
                            }
                            catch { }
                        }
                    }
                }
                catch { }
                string hp2 = null;
                try { hp2 = ha.ActorDataPath == null ? null : ha.ActorDataPath.Id; } catch { }
                return new { ok = true, action = "heroSkills", actorGuid = hg, path = hp2,
                             unlocked, equipped, locked, baseOf };
            }

            if (type == "setSkills")
            {
                uint sg = 0;
                try { sg = Convert.ToUInt32(req.TryGetValue("actorGuid", out var sgv) ? sgv : 0); } catch { }
                string sids = req.TryGetValue("skillIds", out var siv) ? Convert.ToString(siv) : "";
                var want = sids.Split(',').Select(x => x.Trim()).Where(x => x.Length > 0).ToList();
                if (sg == 0 || want.Count == 0) return new { ok = false, error = "need actorGuid + skillIds" };
                var sact = FindActor(sg);
                if (sact == null) return new { ok = false, error = "actor not found: " + sg };
                try
                {
                    // 2026-11-22 实测：SetBaseCombatSkillIdsEquipped **只装备、不卸下**，
                    // 5 格满时新技能会被静默丢弃（decompiled L374227 里只有 equip:true 分支）。
                    // 所以先按实际装备的技能 id 全部卸下，再按 base id 装回去。
                    var t2 = sact.GetType();
                    var getMi = t2.GetMethod("GetBaseEquippedCombatSkillIds", All);
                    var getEq = t2.GetMethod("GetEquippedCombatSkillIds", Type.EmptyTypes);
                    var setOne = t2.GetMethod("SetCombatSkillEquipped",
                                              new Type[] { typeof(string), typeof(bool), typeof(bool) });
                    var was = getMi == null ? null : getMi.Invoke(sact, null);
                    var cur = getEq == null ? null : getEq.Invoke(sact, null) as System.Collections.IEnumerable;
                    int un = 0;
                    if (setOne != null && cur != null)
                    {
                        var ids = new List<string>();
                        foreach (var o in cur) if (o != null) ids.Add(o.ToString());
                        foreach (var sid in ids)
                        {
                            setOne.Invoke(sact, new object[] { sid, false, false });
                            un++;
                        }
                    }
                    var setMi = t2.GetMethod("SetBaseCombatSkillIdsEquipped", All);
                    if (setMi == null) return new { ok = false, error = "SetBaseCombatSkillIdsEquipped not found" };
                    setMi.Invoke(sact, new object[] { want });
                    var now = getMi == null ? null : getMi.Invoke(sact, null);
                    return new { ok = true, action = "setSkills", was, unequipped = un, requested = want, now };
                }
                catch (Exception e)
                {
                    return new { ok = false, error = (e.InnerException ?? e).Message };
                }
            }
            if (combat == null || !combat.IsBattleRunning) return new { ok = false, error = "not in combat" };

            if (type == "pass")
            {
                if (actorGuid == 0) actorGuid = combat.CurrentActorGuid;
                EventBattlePass.Trigger(actorGuid);
                return new { ok = true, action = "pass", actorGuid };
            }

            if (type == "select")
            {
                EventSelectActor.Trigger(actorGuid, true);
                return new { ok = true, action = "select", actorGuid };
            }

            if (type == "skill")
            {
                if (actorGuid == 0) actorGuid = combat.CurrentActorGuid;
                if (string.IsNullOrEmpty(skillId)) return new { ok = false, error = "skillId required" };
                var actor = FindActor(actorGuid);
                if (actor == null) return new { ok = false, error = "actor not found" };

                // 2026-11-21 重写。依据反编译源码 BattleStateMachine.HandleEventSelectActor：
                // 游戏真实流程 = ①点技能格 EventSkillSelectionChanged(自己) ②点目标 EventSelectActor(目标)。
                // 旧实现把 actorGuid(自己) 当目标传、顺序反了、还手动 Trigger EventSkillTargetSelected
                // （那是状态机结算时自己发的输出事件）。结果 skill 82 次 0 成功、54 次 HTTP 500。
                // DD2_SKILL_EVENT_LEGACY=1 可切回旧行为做对照。
                bool legacy = Environment.GetEnvironmentVariable("DD2_SKILL_EVENT_LEGACY") == "1";
                if (legacy)
                {
                    EventSelectActor.Trigger(actorGuid, true);
                    EventSkillSelectionChanged.Trigger(true, actorGuid, skillId, true, false);
                    if (targetGuid != 0) EventSkillTargetSelected.Trigger(actorGuid, targetGuid, skillId);
                    return new { ok = true, action = "skill", actorGuid, targetGuid, skillId, mode = "legacy" };
                }

                // 2026-11-21：回合动画没播完时 Controller.GetIsInputValid() 是 false，
                // 这时发技能事件会被静默丢弃（日志里 skill not selected 的另一个来源）。
                // 先等它说"可以输入"，最多等 1 秒。
                for (int w = 0; w < 20; w++)
                {
                    bool ready = false;
                    try { ready = actor.Controller != null && actor.Controller.GetIsInputValid(); }
                    catch (Exception) { }
                    if (ready) break;
                    Thread.Sleep(50);
                }

                // 2026-11-21：ActorInstance.HandleEventSkillSelectionChanged 的入口条件是
                // !GetIsSelectionComplete()，而它 = "skillId 非空 且 targetGuid 非 0"。
                // 上一手如果 skill+target 都挂着没清，下一次选技能会被直接拒（日志里 77 次
                // skill not selected 就是这么来的）。发技能前先清一次残留。
                try
                {
                    if (actor.GetIsSelectionComplete())
                    {
                        actor.ResetSelection();
                        Thread.Sleep(30);
                    }
                }
                catch (Exception) { }

                // 2026-11-21：刚轮到我方那一瞬，游戏会吞掉第一个技能事件（combat3.py 里
                // 早写过 "实测 7/7 第一次无效"）。所以这里必须重发，不能发一次就判死：
                // 失败日志里 49 次 "skill not selected" 全是只发一次造成的。
                string sel = null;
                for (int attempt = 0; attempt < 3; attempt++)
                {
                    EventSkillSelectionChanged.Trigger(true, actorGuid, skillId, true, false);
                    Thread.Sleep(130);
                    try { sel = actor.GetSelectedSkillId(); } catch (Exception) { }
                    if (sel == skillId) break;
                }

                // 选完技能后控制器要重算"有效目标"（EventActorControllerCalculatedValidSkills），
                // 在算完之前 GetIsValidSkillTarget 必为 false —— 直接点目标会被状态机静默丢弃。
                bool inputValid = false, targetValid = false;
                string diag = "";
                for (int i = 0; i < 30; i++)
                {
                    Thread.Sleep(20);
                    try
                    {
                        inputValid = actor.Controller != null && actor.Controller.GetIsInputValid();
                        targetValid = actor.Controller != null && actor.Controller.GetIsValidSkillTarget(skillId, targetGuid);
                        diag = "";
                    }
                    catch (Exception e) { diag = e.GetType().Name + ":" + e.Message; }
                    if (inputValid && targetValid) break;
                }
                string sel2 = null;
                try { sel2 = actor.GetSelectedSkillId(); } catch (Exception) { }
                if (sel2 != skillId)
                    return new { ok = false, error = "skill not selected", selected = sel2, skillId, inputValid, targetValid, diag, mode = "ui-events" };
                if (targetGuid != 0 && !targetValid)
                    return new { ok = false, error = "target invalid", selected = sel2, skillId, inputValid, targetValid, diag, mode = "ui-events" };
                if (targetGuid != 0)
                {
                    // 2026-11-21：这里必须用 isUserInput:false（游戏 AI 走的那条路）。
                    // BattleStateMachine.HandleEventSelectActor（BattleStateMachine.cs:102-145）：
                    //   isUserInput=true  时，要 m_CurrentState==IN_TURN_SELECT 且
                    //     Controller.GetIsValidSkillTarget(...) 同时成立才 SetSelectedTargetActorGuid，
                    //     否则 flag=false → **整件事静默丢弃**，selection 永远不完整，
                    //     状态卡在 in_turn_select（实测日志：selected=maa_rampart、targetValid=true
                    //     但 battleState=in_turn_select，就是这样）。
                    //   isUserInput=false 时 flag=true 无条件设置 target，然后
                    //     BattleStateMachine 在 IN_TURN_SELECT 的 Update 里自己轮询
                    //     GetIsSelectionComplete() → CALCULATE → PERFORM（BattleStateMachine.cs:410-431）。
                    // 目标合法性我们在上一段已经自己验过（targetValid），所以走这条路是安全的。
                    EventSelectActor.Trigger(targetGuid, false);
                    Thread.Sleep(60);
                }

                // 诊断：source 里 GetIsSelectionComplete() 就是 GetSelectedTargetActorGuid()!=0，
                // 而 Battle 的状态机只在 IN_TURN_SELECT 接受目标。把这两样读回来，才能分辨
                // "事件没被处理" 还是 "处理了但状态机没推"。
                string bstate = "";
                uint tgtSet = 0;
                try
                {
                    var cb = CombatBhv.Instance;
                    var bf = cb == null ? null : typeof(CombatBhv).GetField("m_Battle", All);
                    var battle = bf == null ? null : bf.GetValue(cb);
                    var bp = battle == null ? null : battle.GetType().GetProperty("CurrentBattleState");
                    if (bp != null) bstate = Convert.ToString(bp.GetValue(battle));
                    tgtSet = actor.GetSelectedTargetActorGuid();
                }
                catch (Exception e) { bstate = "ERR:" + e.GetType().Name; }

                return new { ok = true, action = "skill", actorGuid, targetGuid, skillId, mode = "ui-events", selected = sel, inputValid, targetValid, battleState = bstate, targetSet = tgtSet };
            }

            return new { ok = false, error = "unknown action: " + type };
        }

        /// <summary>
        /// 用 Mouse.current 注入一次"移动 → 按下 → 保持 → 松开"，不碰系统光标。
        /// UI 的 hover/click 判定全靠这条链路，所以对"只认真实指针"的按钮也有效。
        /// </summary>
        private static System.Collections.IEnumerator DragRoutine(
            UnityEngine.InputSystem.Mouse mouse, float x1, float y1, float x2, float y2, float hold)
        {
            UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                new UnityEngine.InputSystem.LowLevel.MouseState { position = new UnityEngine.Vector2(x1, y1), buttons = 0 });
            yield return new UnityEngine.WaitForSecondsRealtime(0.25f);
            UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                new UnityEngine.InputSystem.LowLevel.MouseState { position = new UnityEngine.Vector2(x1, y1), buttons = 1 });
            yield return new UnityEngine.WaitForSecondsRealtime(Mathf.Max(0.1f, hold));
            // 分几步移动，让 UI 的 drag 事件跟得上
            for (int i = 1; i <= 4; i++)
            {
                float t = i / 4f;
                UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                    new UnityEngine.InputSystem.LowLevel.MouseState
                    {
                        position = new UnityEngine.Vector2(Mathf.Lerp(x1, x2, t), Mathf.Lerp(y1, y2, t)),
                        buttons = 1,
                    });
                yield return new UnityEngine.WaitForSecondsRealtime(0.08f);
            }
            yield return new UnityEngine.WaitForSecondsRealtime(0.15f);
            UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                new UnityEngine.InputSystem.LowLevel.MouseState { position = new UnityEngine.Vector2(x2, y2), buttons = 0 });
        }

        private static System.Collections.IEnumerator PointerClickRoutine(
            UnityEngine.InputSystem.Mouse mouse, float x, float y, float hold)
        {
            var pos = new UnityEngine.Vector2(x, y);
            UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                new UnityEngine.InputSystem.LowLevel.MouseState { position = pos, buttons = 0 });
            yield return new UnityEngine.WaitForSecondsRealtime(0.35f);
            UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                new UnityEngine.InputSystem.LowLevel.MouseState { position = pos, buttons = 1 });
            yield return new UnityEngine.WaitForSecondsRealtime(UnityEngine.Mathf.Max(0.05f, hold));
            UnityEngine.InputSystem.InputSystem.QueueStateEvent(mouse,
                new UnityEngine.InputSystem.LowLevel.MouseState { position = pos, buttons = 0 });
        }

        /// <summary>
        /// 祭坛「启程」的协程版：先让游戏自己走完"按住 3 秒"的计时，
        /// 如果 3 秒后 UI 还在（说明计时没触发），再直接调 OnEmbark() 强制出发。
        /// altar 被销毁后 Unity 的 == 重载会返回 null，所以不会重复出发。
        /// </summary>
        private static System.Collections.IEnumerator AltarEmbarkRoutine(Assets.Code.UI.AltarOfHopeUiBhv altar)
        {
            try { altar.SetEmbarkHeld(true); } catch { }
            yield return new UnityEngine.WaitForSeconds(3.0f);
            if (altar != null)
            {
                try { altar.SetEmbarkHeld(false); } catch { }
                try { altar.OnEmbark(); } catch { }
            }
        }
    }

}


