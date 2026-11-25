using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace DD2TextMod
{
    /// <summary>
    /// /brief、/delta —— 给"只看一眼"用的极简状态。
    ///
    /// 背景：/state 7.7KB + /ui 9.2KB ≈ 5k token，而每个 tick 真正需要的只有
    /// "现在什么模式、人在哪、谁快死了、屏幕上有哪些按钮"。
    /// 这里不重复实现反射，直接复用 StateBuilder.Build() / UiDriver.Build() 再做投影，
    /// 所以游戏状态源永远只有一份，不会出现两套口径。
    ///
    /// /delta 是"版本号 + 差量"：每次调用给一个 n，客户端下次带 ?since=n，
    /// 没变化就回 {"n":x,"same":true}（约 20 字节）。轮询几乎零成本。
    /// </summary>
    internal static class Brief
    {
        private const int RingMax = 32;

        private static readonly object Gate = new object();
        private static readonly List<Ver> Ring = new List<Ver>();
        private static int _next;

        private sealed class Ver
        {
            public int N;
            public string Json;
        }

        // 这些文字一出现，就说明画面卡在"等某个手势"上，比逐个按钮更有信号量
        private static readonly string[] Signals =
        {
            "长按英雄", "前往下一场战斗", "迷失黑暗", "完成地区", "祭奠陨落英雄", "长按",
        };

        // ---------------------------------------------------------------- 公开入口

        public static object Build()
        {
            string snap = Snapshot();
            var o = JObject.Parse(snap);
            o["n"] = Observe(snap);
            return o;
        }

        public static object Delta(string since)
        {
            string snap = Snapshot();
            int sinceN;
            bool has = int.TryParse(since, NumberStyles.Integer, CultureInfo.InvariantCulture, out sinceN);

            lock (Gate)
            {
                Ver cur = ObserveLocked(snap);
                Ver baseVer = has ? Ring.FirstOrDefault(v => v.N == sinceN) : null;

                // 没有可比对的基线（第一次、或太旧被挤出环形缓冲）→ 回全量
                if (baseVer == null)
                    return new Dictionary<string, object> { ["n"] = cur.N, ["full"] = JObject.Parse(snap) };

                if (baseVer.N == cur.N)
                    return new Dictionary<string, object> { ["n"] = cur.N, ["same"] = true };

                var a = JObject.Parse(baseVer.Json);
                var b = JObject.Parse(cur.Json);

                var chg = new JObject();
                foreach (var p in b.Properties())
                {
                    var old = a[p.Name];
                    if (old == null || !JToken.DeepEquals(old, p.Value)) chg[p.Name] = p.Value.DeepClone();
                }
                var gone = a.Properties().Where(p => b[p.Name] == null).Select(p => p.Name).ToList();

                var res = new Dictionary<string, object> { ["n"] = cur.N };
                if (chg.HasValues) res["chg"] = chg;
                if (gone.Count > 0) res["gone"] = gone;
                if (!chg.HasValues && gone.Count == 0) res["same"] = true;
                return res;
            }
        }

        // ---------------------------------------------------------------- 版本环

        private static int Observe(string snap)
        {
            lock (Gate) return ObserveLocked(snap).N;
        }

        private static Ver ObserveLocked(string snap)
        {
            var last = Ring.Count > 0 ? Ring[Ring.Count - 1] : null;
            if (last != null && last.Json == snap) return last;

            var v = new Ver { N = ++_next, Json = snap };
            Ring.Add(v);
            if (Ring.Count > RingMax) Ring.RemoveAt(0);
            return v;
        }

        // ---------------------------------------------------------------- 投影

        private static string Snapshot()
        {
            var o = new JObject();
            try
            {
                var st = StateBuilder.Build() as IDictionary<string, object>;
                if (st != null)
                {
                    o["mode"] = Tok(st, "mode");
                    o["scene"] = Tok(st, "scene");
                    if (st.ContainsKey("mapRow")) o["row"] = Tok(st, "mapRow");
                    if (st.ContainsKey("speed")) o["spd"] = Num(Tok(st, "speed"));
                    if (st.ContainsKey("gold")) o["gold"] = Tok(st, "gold");
                    if (st.ContainsKey("battleState")) o["bs"] = Tok(st, "battleState");
                    if (st.ContainsKey("round")) o["rnd"] = Tok(st, "round");
                    if (st.ContainsKey("turn")) o["turn"] = Tok(st, "turn");
                    if (st.ContainsKey("currentActor")) o["who"] = Tok(st, "currentActor");

                    var actors = st.ContainsKey("actors") ? st["actors"] as IEnumerable<object> : null;
                    if (actors != null) o["act"] = Actors(actors);
                }
            }
            catch { }

            try
            {
                var ui = UiDriver.Build() as IDictionary<string, object>;
                if (ui != null)
                {
                    var texts = ui.ContainsKey("texts") ? ui["texts"] as IEnumerable<string> : null;
                    if (texts != null) o["sig"] = SignalsOf(texts);
                    var clicks = ui.ContainsKey("clickables") ? ui["clickables"] as IEnumerable<object> : null;
                    if (clicks != null) o["btn"] = Buttons(clicks);
                }
            }
            catch { }

            return o.ToString(Formatting.None);
        }

        private static string Actors(IEnumerable<object> actors)
        {
            var parts = new List<string>();
            foreach (var item in actors)
            {
                var a = item as IDictionary<string, object>;
                if (a == null) continue;
                string name = Str(a, "name");
                if (string.IsNullOrEmpty(name)) name = Str(a, "guid");
                int team = 0;
                try { team = Convert.ToInt32(a.ContainsKey("team") ? a["team"] : 0, CultureInfo.InvariantCulture); } catch { }
                string hp = Str(a, "hp") + "/" + Str(a, "hpMax");
                bool alive = true;
                try { alive = !(a.ContainsKey("alive") && a["alive"] is bool b && !b); } catch { }
                parts.Add((team == 1 ? "E:" : "") + name + " " + hp + (alive ? "" : "†"));
                if (parts.Count >= 12) break;
            }
            return string.Join("|", parts);
        }

        private static string Buttons(IEnumerable<object> clicks)
        {
            var seen = new List<string>();
            foreach (var item in clicks)
            {
                var c = item as IDictionary<string, object>;
                if (c == null) continue;
                string t = Clean(Str(c, "text"));
                if (t.Length == 0) continue;
                // 纯数字的标签（蜡烛数、金币数之类）没有可点语义，跳过
                if (t.All(char.IsDigit)) continue;
                if (seen.Contains(t)) continue;
                seen.Add(t);
                if (seen.Count >= 16) break;
            }
            var s = string.Join("|", seen);
            return s.Length > 160 ? s.Substring(0, 160) : s;
        }

        /// <summary>去掉 Unity 富文本标签（&lt;size=150%&gt; 之类），压掉多余空白，单个标签最多 20 字。</summary>
        private static string Clean(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            s = System.Text.RegularExpressions.Regex.Replace(s, "<[^>]*>", " ");
            s = System.Text.RegularExpressions.Regex.Replace(s, @"\s+", " ").Trim();
            if (s.Length > 20) s = s.Substring(0, 20);
            return s;
        }

        private static string SignalsOf(IEnumerable<string> texts)
        {
            var hit = new List<string>();
            foreach (var t in texts)
            {
                if (string.IsNullOrEmpty(t)) continue;
                foreach (var sig in Signals)
                {
                    if (t.Contains(sig) && !hit.Contains(sig)) hit.Add(sig);
                }
            }
            return string.Join("+", hit);
        }

        // ---------------------------------------------------------------- 小工具

        private static JToken Tok(IDictionary<string, object> d, string k)
        {
            var v = d.ContainsKey(k) ? d[k] : null;
            return v == null ? JValue.CreateNull() : JToken.FromObject(v);
        }

        private static JToken Num(object v)
        {
            try { return new JValue(Math.Round(Convert.ToDouble(v, CultureInfo.InvariantCulture), 1)); }
            catch { return JValue.CreateNull(); }
        }

        private static string Str(IDictionary<string, object> d, string k)
        {
            var v = d.ContainsKey(k) ? d[k] : null;
            if (v == null) return "";
            try { return Convert.ToString(v, CultureInfo.InvariantCulture); }
            catch { return ""; }
        }
    }
}
