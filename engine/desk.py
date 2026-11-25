"""desk.py —— 一眼看全局：一次调用，≤15 行输出，替代"翻 runner.log / status.json / combat_log.jsonl"。

为什么要有它（2026-11-19 15:45）：
    账本实测本工作区今天 **819.9M token / 2924 步**，平均每步上下文 **280k**，
    工具调用里 pwsh 2071 次、read 214 次（982k 字符）—— 都是"看一眼状态"花掉的。
    模型每读一次大文件，它的字面量会被后面每一步重发一次（放大倍数 464）。
    所以省 token 的正确做法不是压缩 prompt，而是**把"看一眼"变成一次极小的输出**。

用法:
    python desk.py              # 人看的 15 行摘要
    python desk.py --gear       # 多问一次 mod 要饰品装备状态（+1 次 HTTP，慢 ~50ms）
    python desk.py --fights 5   # 最近几场战斗（默认 3）
    python desk.py --json       # 单行 JSON（给模型/脚本读）

退出码: 0=一切正常  2=有告警（alert.json 存在 / STUCK）  3=连不上游戏 mod
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:18082"
REPORT = os.path.join(HERE, "report.json")
STATUS = os.path.join(HERE, "status.json")
ALERT = os.path.join(HERE, "alert.json")
COMBAT_LOG = os.path.join(HERE, "combat_log.jsonl")


def _get(path, timeout=2.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _tail_jsonl(path, max_bytes=96 * 1024):
    """只读文件末尾（不把 347KB 的 combat_log 整个读进来）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                    # 丢掉被截断的半行
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _age(p):
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return None


def fight_brief(f):
    """一场战斗压成一句：时间 场景 结果 回合 对面剩几个 [撤退/告警]。"""
    acts = f.get("actions")
    if acts is None:  # trim_logs.py 精简过的历史行：只留计数
        bad = f.get("actions_bad_n", len(f.get("actions_fail") or []))
        pas = f.get("pass_n", 0)
        ok = max(0, (f.get("actions_n") or 0) - bad - pas)
    else:
        ok = sum(1 for a in acts if a.get("ok") and "pass" not in (a.get("why") or ""))
        bad = sum(1 for a in acts if not a.get("ok"))
        pas = sum(1 for a in acts if "pass" in (a.get("why") or ""))
    tag = ""
    if f.get("forced_retreat"):
        tag = " 僵尸突围"
    elif f.get("retreat"):
        tag = " 撤退"
    elif f.get("retreat_veto"):
        tag = " 拦下撤退"
    if f.get("bag_trinkets"):
        tag += " 饰品没上身"
    return ("%s %-26s %-7s %d回合 出招%d/废%d/pass%d 对面剩%s%s"
            % ((f.get("ts") or "")[11:16], f.get("scene"), f.get("outcome"),
               f.get("rounds") or 0, ok, bad, pas, f.get("enemies_left", "?"), tag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gear", action="store_true", help="顺便读饰品装备状态")
    ap.add_argument("--fights", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rep = _read_json(REPORT)
    st = _read_json(STATUS)
    alert = _read_json(ALERT)
    age = _age(REPORT)
    running = age is not None and age < 90
    brief, mod_ok = {}, False
    try:
        brief, mod_ok = _get("/brief"), True
    except Exception:
        pass
    fights = _tail_jsonl(COMBAT_LOG)[-max(1, a.fights):]
    # 一场战斗的**一个 tick** 可能跑几分钟（combat3.main(max_steps=60)，2026-11-23 从 40 提到 60），
    # report.json 会跟着一直不更新。这时光看 90 秒阈值会误报"停"。
    # 上限按 combat3 的 max_secs=900 给到 1000 秒；超过就算真没心跳（进程可能被杀）。
    slow_tick = (not running and age is not None and age < 1000
                 and brief.get("mode") == "combat")
    if slow_tick:
        running = True
    gear = {}
    # 撤退口径：status.json 里 retro=off 就是"死战到底"。没事先声明时留空。
    retro = (st or {}).get("retro")
    retreat_note = ("  死战到底" if retro == "off"
                    else "  ⚠允许撤退" if retro == "on" else "")
    if a.gear:
        try:
            req = urllib.request.Request(BASE + "/action",
                                         data=json.dumps({"type": "trinkets"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as r:
                tr = json.loads(r.read().decode("utf-8"))
            worn = {x.get("name"): x.get("trinkets") or [] for x in (tr.get("actors") or [])
                    if "corpse" not in ((x.get("dataId") or "").lower())}
            bagtr = [i.get("itemId") for i in (tr.get("bag") or [])
                     if "trinket" in (i.get("itemId") or "").lower()]
            gear = {"worn": worn, "bag_trinkets": bagtr}
        except Exception as e:
            gear = {"error": str(e)[:60]}

    out = {
        "runner": ("在跑" if running else "停")
                  + ("" if age is None else "(%ds前%s)" % (int(age), "，战斗长tick" if slow_tick else "")),
        "alert": alert or None,
        "brief": brief or None,
        "report": rep or None,
        "fights": [fight_brief(f) for f in fights],
        "gear": gear or None,
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    else:
        print("[runner] %s  %s%s" % (out["runner"], "mod 可用" if mod_ok else "mod 连不上",
                                     retreat_note))
        if alert:
            print("[告警]   %s: %s" % (alert.get("kind"), alert.get("why")))
        if brief:
            print("[brief]  %s/%s row=%s spd=%s gold=%s%s"
                  % (brief.get("mode"), brief.get("scene"), brief.get("row"),
                     brief.get("spd"), brief.get("gold"),
                     ("  " + brief["act"]) if brief.get("act") else ""))
            if brief.get("sig"):
                print("[信号]   %s" % brief["sig"])
        print("[report] %st=%s act=%s stuck=%s%s"
              % ("(%.0f分钟前的旧数据) " % (age / 60) if age and age > 120 else "",
                 rep.get("t"), rep.get("act"), rep.get("stuck", 0),
                 ("  " + str(rep.get("why"))) if rep.get("why") else ""))
        for line in out["fights"]:
            print("[战斗]   " + line)
        if gear:
            if gear.get("error"):
                print("[装备]   读不到：%s" % gear["error"])
            else:
                bare = [k for k, v in gear["worn"].items() if not v]
                print("[装备]   %d 人 | 背包没上身 %d 件 | 空着：%s"
                      % (len(gear["worn"]), len(gear["bag_trinkets"]), ",".join(bare[:4]) or "无"))
    return 2 if (alert or st.get("stuck")) else (0 if mod_ok else 3)


if __name__ == "__main__":
    sys.exit(main())
