"""给主人发 QQ 私聊（经 Aurabot 的 SnowLuma / OneBot v11）。

token 从 Aurabot 配置里读，不写进代码。

    python qq_send.py "要发的内容"
"""
import json
import os
import sys
import urllib.request

CFG = os.path.join(os.environ["USERPROFILE"], ".dsh", "plugins-data",
                   "aurabot", "config.json")


def conf():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def send(text):
    c = conf()
    sl = c.get("snowluma") or {}
    base = sl.get("httpUrl") or "http://127.0.0.1:3000"
    token = sl.get("httpToken") or ""
    owner = c.get("ownerQQ") or c.get("botQQ")
    body = json.dumps({"user_id": int(owner), "message": text},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/send_private_msg", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "测试"
    print(json.dumps(send(msg), ensure_ascii=False))
