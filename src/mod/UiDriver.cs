using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace DD2TextMod
{
    /// <summary>
    /// 纯文字 UI 驱动：把当前界面上的可点击元素 + 文字读出来，并能按文字/路径/序号"点"它。
    /// 不依赖鼠标，也不依赖截图。
    /// </summary>
    internal static class UiDriver
    {
        internal sealed class Entry
        {
            public GameObject Go;
            public RectTransform Rt;
            public string Path;
            public string Label;
            public Vector2 Screen;
            public float W;
            public float H;
        }

        private static readonly List<Entry> _clickables = new List<Entry>();

        // ---------- 读取 ----------

        public static object Build(int maxClick = 120, int maxText = 160)
        {
            var texts = new List<string>();
            Collect(texts, maxText, maxClick);
            var list = new List<object>();
            for (int i = 0; i < _clickables.Count; i++)
            {
                var e = _clickables[i];
                list.Add(new Dictionary<string, object>
                {
                    ["i"] = i,
                    ["path"] = e.Path,
                    ["text"] = e.Label,
                    ["x"] = Mathf.RoundToInt(e.Screen.x),
                    ["y"] = Mathf.RoundToInt(e.Screen.y),
                });
            }
            return new Dictionary<string, object>
            {
                ["ok"] = true,
                ["scene"] = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name,
                ["screen"] = Screen.width + "x" + Screen.height,
                ["texts"] = texts,
                ["clickables"] = list,
                ["canvases"] = CanvasInfo(),
            };
        }

        private static List<object> CanvasInfo()
        {
            var list = new List<object>();
            foreach (var c in UnityEngine.Object.FindObjectsOfType<Canvas>(true))
            {
                if (c == null || !c.isActiveAndEnabled) continue;
                var cam = ResolveCamera(c);
                list.Add(new Dictionary<string, object>
                {
                    ["name"] = c.name,
                    ["mode"] = c.renderMode.ToString(),
                    ["cam"] = cam == null ? "null" : cam.name,
                    ["scale"] = Mathf.Round(c.scaleFactor * 100f) / 100f,
                });
            }
            return list;
        }

        private static void Collect(List<string> texts, int maxText, int maxClick)
        {
            _clickables.Clear();
            var canvases = UnityEngine.Object.FindObjectsOfType<Canvas>(true);
            if (canvases == null || canvases.Length == 0) return;

            var labelByGo = new Dictionary<GameObject, string>();

            foreach (var canvas in canvases)
            {
                if (canvas == null || !canvas.isActiveAndEnabled) continue;
                MonoBehaviour[] monos;
                try { monos = canvas.GetComponentsInChildren<MonoBehaviour>(true); }
                catch { continue; }
                foreach (var mb in monos)
                {
                    if (mb == null) continue;
                    var go = mb.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    try
                    {
                        string t = ReadText(mb);
                        if (!string.IsNullOrEmpty(t))
                        {
                            if (!labelByGo.ContainsKey(go)) labelByGo[go] = t;
                            if (texts.Count < maxText && !texts.Contains(t)) texts.Add(t);
                        }
                    }
                    catch { }
                }
            }

            var seen = new HashSet<int>();
            foreach (var canvas in canvases)
            {
                if (canvas == null || !canvas.isActiveAndEnabled) continue;
                Camera cam = ResolveCamera(canvas);
                MonoBehaviour[] monos;
                try { monos = canvas.GetComponentsInChildren<MonoBehaviour>(true); }
                catch { continue; }
                foreach (var mb in monos)
                {
                    if (mb == null) continue;
                    if (!(mb is IPointerClickHandler)) continue;
                    var go = mb.gameObject;
                    if (go == null || !go.activeInHierarchy) continue;
                    if (_clickables.Count >= maxClick) return;

                    var rt = go.GetComponent<RectTransform>();
                    if (rt == null) continue;
                    if (!seen.Add(rt.GetInstanceID())) continue;

                    Vector3 center = rt.TransformPoint(rt.rect.center);
                    Vector2 sp = RectTransformUtility.WorldToScreenPoint(cam, center);
                    if (sp.x < -40 || sp.y < -40 || sp.x > Screen.width + 40 || sp.y > Screen.height + 40) continue;

                    // 按钮上的字：自己或子物体上的 Text/TMP
                    string label = null;
                    if (!labelByGo.TryGetValue(go, out label) || string.IsNullOrEmpty(label))
                        label = FindChildLabel(go, labelByGo) ?? "";

                    _clickables.Add(new Entry
                    {
                        Go = go,
                        Rt = rt,
                        Path = PathOf(go, canvas.transform),
                        Label = label,
                        Screen = sp,
                        W = rt.rect.width * rt.lossyScale.x,
                        H = rt.rect.height * rt.lossyScale.y,
                    });
                }
            }
        }

        /// <summary>
        /// 主菜单是 WorldSpace / ScreenSpaceCamera 画布，worldCamera 可能为 null。
        /// 用 null 相机调 WorldToScreenPoint 会把世界坐标当屏幕坐标返回，射线就永远打空。
        /// </summary>
        private static Camera ResolveCamera(Canvas canvas)
        {
            if (canvas.renderMode == RenderMode.ScreenSpaceOverlay) return null;
            if (canvas.worldCamera != null) return canvas.worldCamera;
            var cam = Camera.main;
            if (cam != null) return cam;
            var all = UnityEngine.Object.FindObjectsOfType<Camera>();
            return all != null && all.Length > 0 ? all[0] : null;
        }

        private static string FindChildLabel(GameObject go, Dictionary<GameObject, string> map)        {
            var t = go.transform;
            for (int i = 0; i < t.childCount; i++)
            {
                var child = t.GetChild(i);
                if (map.TryGetValue(child.gameObject, out var s) && !string.IsNullOrEmpty(s)) return s;
            }
            for (int i = 0; i < t.childCount; i++)
            {
                var s = FindChildLabel(t.GetChild(i).gameObject, map);
                if (!string.IsNullOrEmpty(s)) return s;
            }
            return null;
        }

        private static string ReadText(Component c)
        {
            var tn = c.GetType().Name;
            if (!tn.Contains("Text") || tn.Contains("Texture")) return null;
            var p = c.GetType().GetProperty("text", BindingFlags.Instance | BindingFlags.Public);
            if (p == null || p.PropertyType != typeof(string)) return null;
            var s = p.GetValue(c) as string;
            if (string.IsNullOrWhiteSpace(s)) return null;
            s = s.Replace("\r", " ").Replace("\n", " ").Trim();
            if (s.Length > 160) s = s.Substring(0, 160);
            return s;
        }

        private static string PathOf(GameObject go, Transform root)
        {
            var parts = new List<string>();
            var t = go.transform;
            while (t != null)
            {
                parts.Add(t.name);
                if (t == root) break;
                t = t.parent;
            }
            parts.Reverse();
            if (parts.Count > 4) parts = parts.Skip(parts.Count - 4).ToList();
            return string.Join("/", parts);
        }

        // ---------- 按住 ----------

        public static object Hold(string pick, int index, float seconds)
        {
            var es = EventSystem.current;
            if (es == null) return new { ok = false, error = "EventSystem not found" };
            var texts = new List<string>();
            Collect(texts, 0, 500);
            if (_clickables.Count == 0) return new { ok = false, error = "no clickable ui on screen" };

            Entry target = null;
            if (!string.IsNullOrEmpty(pick))
            {
                string p = pick.Trim();
                target = _clickables.FirstOrDefault(e => e.Label == p)
                      ?? _clickables.FirstOrDefault(e => e.Path == p)
                      ?? _clickables.FirstOrDefault(e => !string.IsNullOrEmpty(e.Label) && e.Label.Contains(p))
                      ?? _clickables.FirstOrDefault(e => e.Path.EndsWith(p, StringComparison.OrdinalIgnoreCase));
            }
            if (target == null && index >= 0 && index < _clickables.Count) target = _clickables[index];
            if (target == null) return new { ok = false, error = "no match for '" + pick + "'" };

            var ped = new PointerEventData(es)
            {
                position = target.Screen,
                button = PointerEventData.InputButton.Left,
                clickCount = 1,
                pointerId = -1,
            };
            var results = new List<RaycastResult>();
            es.RaycastAll(ped, results);
            GameObject go = results.Count > 0 ? results[0].gameObject : null;
            // 2026-11-20：剧情屏最上层盖着驾车遮罩（MouseDrivingArea），raycast 命中的是遮罩，
            // 指针事件全被它吞掉 —— uiHold 返回 ok=True 但画面一动不动，于是只能退真鼠标，
            // 而真鼠标又必须抢前台（作者不想被抢）。命中不是目标自己那一支时，直接对目标发事件。
            if (go != null && !InSubtree(target.Go, go)) go = null;
            if (go == null)
            {
                foreach (var mb in target.Go.GetComponentsInChildren<MonoBehaviour>(true))
                {
                    if (mb == null) continue;
                    if (!(mb is IPointerClickHandler) && !(mb is IPointerDownHandler)) continue;
                    go = mb.gameObject;
                    break;
                }
            }
            if (go == null) go = target.Go;

            ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerEnterHandler);
            ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerDownHandler);
            Plugin.Run(ReleaseLater(go, ped, seconds));

            return new { ok = true, held = seconds, path = target.Path, label = target.Label, target = go.name };
        }

        private static bool InSubtree(GameObject root, GameObject node)
        {
            if (root == null || node == null) return false;
            for (var t = node.transform; t != null; t = t.parent)
                if (t.gameObject == root) return true;
            return false;
        }

        private static System.Collections.IEnumerator ReleaseLater(GameObject go, PointerEventData ped, float seconds)
        {
            yield return new WaitForSecondsRealtime(Mathf.Max(0.1f, seconds));
            if (go != null)
            {
                ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerUpHandler);
                ExecuteEvents.Execute(go, ped, ExecuteEvents.pointerClickHandler);
            }
        }

        // ---------- 点击 ----------

        public static object Click(string pick, int index)
        {
            var es = EventSystem.current;
            if (es == null) return new { ok = false, error = "EventSystem not found" };

            var texts = new List<string>();
            Collect(texts, 0, 500);
            if (_clickables.Count == 0) return new { ok = false, error = "no clickable ui on screen" };

            Entry target = null;
            if (!string.IsNullOrEmpty(pick))
            {
                string p = pick.Trim();
                target = _clickables.FirstOrDefault(e => e.Label == p)
                      ?? _clickables.FirstOrDefault(e => e.Path == p)
                      ?? _clickables.FirstOrDefault(e => !string.IsNullOrEmpty(e.Label) && e.Label.Contains(p))
                      ?? _clickables.FirstOrDefault(e => e.Path.EndsWith(p, StringComparison.OrdinalIgnoreCase));
            }
            if (target == null && index >= 0 && index < _clickables.Count) target = _clickables[index];
            if (target == null)
            {
                var avail = string.Join(" | ", _clickables.Select(e => e.Label));
                return new { ok = false, error = "no match for '" + pick + "'", available = avail };
            }

            var ped = new PointerEventData(es)
            {
                position = target.Screen,
                button = PointerEventData.InputButton.Left,
                clickCount = 1,
                pointerId = -1,
            };
            var results = new List<RaycastResult>();
            es.RaycastAll(ped, results);
            GameObject hit = results.Count > 0 ? results[0].gameObject : null;

            var fired = new List<string>();
            var candidates = new List<GameObject>();
            if (hit != null) candidates.Add(hit);
            // 兜底：从目标子树里找"最深的"点击处理器（很多按钮真正的 handler 挂在子物体上）
            foreach (var mb in target.Go.GetComponentsInChildren<MonoBehaviour>(true))
            {
                if (mb == null || !(mb is IPointerClickHandler)) continue;
                if (mb.gameObject == target.Go) continue;
                candidates.Add(mb.gameObject);
            }
            if (target.Go != null) candidates.Add(target.Go);

            foreach (var go in candidates)
            {
                if (go == null) continue;
                if (!(go.transform is RectTransform)) continue;
                bool any = false;
                any |= Execute(go, ped, ExecuteEvents.pointerEnterHandler);
                any |= Execute(go, ped, ExecuteEvents.pointerDownHandler);
                any |= Execute(go, ped, ExecuteEvents.pointerUpHandler);
                any |= Execute(go, ped, ExecuteEvents.pointerClickHandler);
                if (any) { fired.Add(go.name); break; }
            }

            if (fired.Count == 0)
            {
                // 最后的兜底：直接调 Button.onClick
                var btn = target.Go.GetComponentInChildren<Button>(true);
                if (btn != null) { btn.onClick.Invoke(); fired.Add("onClick:" + btn.name); }
            }

            return new
            {
                ok = fired.Count > 0,
                clicked = target.Label,
                path = target.Path,
                x = Mathf.RoundToInt(target.Screen.x),
                y = Mathf.RoundToInt(target.Screen.y),
                via = string.Join(",", fired),
                hit = hit != null ? hit.name : null,
            };
        }

        // ---------- 按屏幕坐标拖拽：射线 + drag 事件分发，不注入鼠标 ----------
        // 装备兜底原来是 drag 动作（往 Mouse.current 塞按下+移动+抬起）。这里改成：
        // 源点射线下发 pointerDown/BeginDrag，目标点下发 Drag/EndDrag/Drop/pointerUp。
        //
        // 2026-11-23 04:0x 实机：事件都发出了，背包/身上零变化（logs/verify-uidrag-diag.out
        // 第 5/8 行 from=border→to=Image(1)）。对照 Unity 真实拖拽流程，这里差 4 处，
        // 游戏多半就是靠这些字段判断"拖到哪、能不能放"：
        //   1) 从不设 pointerCurrentRaycast（真流程每帧按指针位置重算，Drop 处理器常读它）；
        //   2) dragging 恒 false（真流程从 beginDrag 到 endDrag 全程 true）；
        //   3) pointerPress/rawPointerPress 没填（按下时 EventSystem 会填）；
        //   4) 顺序：真流程先 drop 再 endDrag 再 pointerUp，且拖过目标时会先 exit/enter/move。
        public static object PointDrag(float x1, float y1, float x2, float y2)
        {
            var es = EventSystem.current;
            if (es == null) return new { ok = false, error = "EventSystem not found" };
            var ped = new PointerEventData(es)
            {
                position = new Vector2(x1, y1),
                button = PointerEventData.InputButton.Left,
                clickCount = 1,
                pointerId = -1,
            };
            var hits = new List<RaycastResult>();
            es.RaycastAll(ped, hits);
            if (hits.Count == 0) return new { ok = false, error = "nothing at source", x = x1, y = y1 };
            var src = hits[0].gameObject;
            ped.pointerCurrentRaycast = hits[0];
            // 真流程里 pointerPress/pointerDrag 是按下时用 GetEventHandler 找到的**处理器所在物体**
            // （常常不是射线命中的那张图），游戏的 Drop 处理器会拿它们比对"拖的是谁"。
            var pressGo = ExecuteEvents.GetEventHandler<IPointerClickHandler>(src);
            var dragGo = ExecuteEvents.GetEventHandler<IDragHandler>(src);
            ped.pointerPress = pressGo != null ? pressGo : src;
            ped.rawPointerPress = src;
            bool down = Execute(src, ped, ExecuteEvents.pointerDownHandler);
            bool begin = Execute(src, ped, ExecuteEvents.beginDragHandler);
            ped.pointerDrag = dragGo != null ? dragGo : src;
            ped.dragging = true;

            ped.position = new Vector2(x2, y2);
            var hits2 = new List<RaycastResult>();
            es.RaycastAll(ped, hits2);
            if (hits2.Count > 0) ped.pointerCurrentRaycast = hits2[0];
            var dst = hits2.Count > 0 ? hits2[0].gameObject : null;

            bool drag = Execute(src, ped, ExecuteEvents.dragHandler);
            bool exit = false, enter = false, move = false;
            if (dst != null && dst != src)
            {
                exit = Execute(src, ped, ExecuteEvents.pointerExitHandler);
                enter = Execute(dst, ped, ExecuteEvents.pointerEnterHandler);
                move = Execute(dst, ped, ExecuteEvents.pointerMoveHandler);
            }
            bool drop = dst != null && Execute(dst, ped, ExecuteEvents.dropHandler);
            bool end = Execute(src, ped, ExecuteEvents.endDragHandler);
            bool up = Execute(src, ped, ExecuteEvents.pointerUpHandler);
            ped.dragging = false;
            ped.pointerDrag = null;
            ped.pointerPress = null;
            ped.rawPointerPress = null;
            var fired = new List<string>();
            if (down) fired.Add("down");
            if (begin) fired.Add("beginDrag");
            if (drag) fired.Add("drag");
            if (exit) fired.Add("exit");
            if (enter) fired.Add("enter");
            if (move) fired.Add("move");
            if (drop) fired.Add("drop");
            if (end) fired.Add("endDrag");
            if (up) fired.Add("up");
            return new
            {
                ok = down || begin,
                from = src.name,
                to = dst != null ? dst.name : null,
                press = pressGo != null ? pressGo.name : src.name,
                pdrag = dragGo != null ? dragGo.name : src.name,
                via = string.Join(",", fired),
            };
        }

        // ---------- 按屏幕坐标点/按住：射线 + 事件分发，不注入鼠标 ----------
        // 2026-11-23 作者口径：不许再模拟键鼠。原来 pointerClick 是往进程内
        // Mouse.current 塞事件（虽然是进程内，仍算"模拟鼠标"）。这里改成：
        // 在坐标上做 GraphicRaycaster 射线，取最上层命中的物体，直接 ExecuteEvents
        // 分发 pointerDown/Up/Click —— 和 UI 按钮自己收到的不是一回事，但玩家点下去
        // 之后游戏跑的就是这些 handler。按 path/index 点不到的元素（无 Button 的
        // 自绘面板、半透明遮罩）就走这条。

        public static object PointClick(float x, float y, int holdMs)
        {
            var es = EventSystem.current;
            if (es == null) return new { ok = false, error = "EventSystem not found" };
            var ped = new PointerEventData(es)
            {
                position = new Vector2(x, y),
                button = PointerEventData.InputButton.Left,
                clickCount = 1,
                pointerId = -1,
            };
            var hits = new List<RaycastResult>();
            try { es.RaycastAll(ped, hits); }
            catch (Exception e) { return new { ok = false, error = "raycast: " + e.Message, x, y }; }
            if (hits.Count == 0) return new { ok = false, error = "nothing at point", x, y };

            var go = hits[0].gameObject;
            Execute(go, ped, ExecuteEvents.pointerEnterHandler);
            bool down = Execute(go, ped, ExecuteEvents.pointerDownHandler);
            if (holdMs > 0)
            {
                Plugin.Run(ReleaseAfter(go, ped, holdMs));
                return new { ok = true, hit = go.name, x, y, holdMs, via = down ? "down(hold)" : "hold-only" };
            }
            bool up = Execute(go, ped, ExecuteEvents.pointerUpHandler);
            bool click = Execute(go, ped, ExecuteEvents.pointerClickHandler);
            return new
            {
                ok = down || up || click,
                hit = go.name,
                x,
                y,
                via = (down ? "down," : "") + (up ? "up," : "") + (click ? "click" : ""),
            };
        }

        private static System.Collections.IEnumerator ReleaseAfter(GameObject go, PointerEventData ped, int holdMs)
        {
            yield return new WaitForSecondsRealtime(holdMs / 1000f);
            try
            {
                Execute(go, ped, ExecuteEvents.pointerUpHandler);
                Execute(go, ped, ExecuteEvents.pointerClickHandler);
            }
            catch { }
        }

        // ---------- 直接调 onClick（对付没有射线目标的画布，比如主菜单） ----------

        public static object Invoke(string pick, int index)
        {
            var texts = new List<string>();
            Collect(texts, 0, 500);
            if (_clickables.Count == 0) return new { ok = false, error = "no clickable ui on screen" };

            Entry target = null;
            if (!string.IsNullOrEmpty(pick))
            {
                string p = pick.Trim();
                target = _clickables.FirstOrDefault(e => e.Label == p)
                      ?? _clickables.FirstOrDefault(e => e.Path == p)
                      ?? _clickables.FirstOrDefault(e => !string.IsNullOrEmpty(e.Label) && e.Label.Contains(p));
            }
            if (target == null && index >= 0 && index < _clickables.Count) target = _clickables[index];
            if (target == null) return new { ok = false, error = "no match for '" + pick + "'" };

            var btn = target.Go.GetComponent<Button>() ?? target.Go.GetComponentInParent<Button>() ?? target.Go.GetComponentInChildren<Button>(true);
            if (btn == null) return new { ok = false, error = "no Button on " + target.Path };
            try
            {
                btn.onClick.Invoke();
                return new { ok = true, invoked = btn.name, path = target.Path, label = target.Label };
            }
            catch (Exception e) { return new { ok = false, error = e.Message }; }
        }

        private static bool Execute<T>(GameObject go, PointerEventData ped, ExecuteEvents.EventFunction<T> fn) where T : IEventSystemHandler
        {
            try
            {
                var handler = ExecuteEvents.GetEventHandler<T>(go);
                if (handler == null) return false;
                ExecuteEvents.Execute(handler, ped, fn);
                return true;
            }
            catch { return false; }
        }
    }
}
