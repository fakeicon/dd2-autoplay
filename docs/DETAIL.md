# 暗黑地牢 2 挂机

DSH 里的「暗黑地牢 2」整页（侧栏入口），用来盯自动挂机、启停挂机进程，并让模型直接看状态。**决策全在本地 Python 引擎里跑，模型不逐回合出招。**

## 它包含三样东西

| 部分 | 在哪 | 作用 |
|---|---|---|
| 游戏内插件 | `assets/DD2TextMod.dll`（BepInEx 5.4.x，装进游戏的 `BepInEx\plugins`） | 反射读游戏状态，在 `127.0.0.1:18082` 提供 `/state` `/brief` `/delta` `/ui` `/catalog` `/action`；点击/长按走游戏自己的 EventSystem，**不注入系统键鼠** |
| 决策引擎 | 包内 `engine/`（16 个 Python 模块，纯标准库） | 读状态 → 开车、出招、买饰品、装备；写 `report.json` / `alert.json` / `combat_log.jsonl` |
| DSH 面板 | 本插件 | 整页看状态与角色卡、开始/停止挂机、放弃本局重开、关游戏、复制摘要 |

## 安装（三步）

1. **装 BepInEx 5.4.x 到游戏目录**（不是本插件的事，但必须）：
   从 <https://github.com/BepInEx/BepInEx/releases> 下 `BepInEx_win_x64_5.4.23.x.zip`，解压进游戏目录（里面有 `Darkest Dungeon II.exe` 的那层）。
2. **装本插件**：DSH 里 **插件 → 添加插件** 选这个目录，或由 `plugin_manager` 装。
3. **装游戏内插件**：DSH 里执行 `/dd2 install`（或让模型调 `dd2_install`）。它会自己找游戏目录、把 DLL 拷进去；装完**重启游戏**。
   然后 `/dd2 status` 里 `游戏` 一行不再显示「不可达」就算通了。

引擎第一次加载会从包内 `engine/` 铺到 `%LOCALAPPDATA%\dsh-dd2\engine` 再跑——日志、报告、告警都落在那儿，升级插件时自动同步。

## 页面内容

- **顶部状态**：`18082/brief` 的 mode/scene/row/gold/速度；游戏不可达时变红。
- **战斗卡片**（战斗态才有）：回合、当前行动者、敌人剩余/总数、每个敌人的血量与死门。
- **四张角色卡**：职业 + 道途、HP 条、压力条、死门标记、当前 token（含剩余回合）、已装 5 个技能、饰品已装/槽数。中文取自游戏自带 `zh_CN.po`（约 2 万条，按 mtime 缓存），不是硬编码。
- **告警条**：`alert.json`、游戏不可达、挂机进程已停。
- **按钮**：刷新 / 开始挂机 / 停止挂机 / 放弃本局并重开 / 关闭游戏 / 复制摘要。

## 动作口径（红线）

- **开始挂机**：起 `python watchdog.py --hours N`（默认 4h）；watchdog 负责团灭/减员重开、卡死重启。
- **停止挂机**：停掉本机所有 `runner.py` / `watchdog.py` 进程（含不是本插件起的）；**游戏本身不动**。
- **放弃本局并重开**：调 mod 的 `abandonRun`，之后交给 watchdog 走「祭坛 → altarEmbark」重开。破坏性，页面有二次确认。
- **关闭游戏**：只走 `launcher.py` 的 `18083 /quit`（它会把控制口拉起来，不是游戏本体）。**开游戏永远归人**——脚本拉 Steam 会崩，也让游戏开成第二个实例。

## 模型能用的三个工具

| 工具 | 干什么 |
|---|---|
| `dd2_status` | 一次调用出全部状态，**不用读 report.json / 日志** |
| `dd2_run` | `start` / `runner` / `stop` / `restart` |
| `dd2_install` | 把自带 DLL 装进 BepInEx；缺 BepInEx 时明确报出来 |

## 限制

- 只针对 **Windows + Steam 版**，且反射绑死某个游戏版本（开发时是 Unity 2022.3.62f2）；游戏更新后可能失效。
- 坐标点按 **1920×1080** UI 空间调过，别的分辨率/UI 缩放**未验证**。
- 会驱动**你的真实存档**（`%USERPROFILE%\AppData\LocalLow\RedHook\Darkest Dungeon II\SaveFiles`），首次使用前请自行备份。
- 只管本插件自己的进程树；挂机被 DSH 后台 job 或手工起时，面板只能靠 `logs\*.lock` 判断有没有人在跑。
- 关掉 DSH 会连带停掉本插件起的子进程（避免孤儿进程）。
