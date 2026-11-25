# DD2 自动挂机 · DSH 插件

把自己玩《暗黑地牢 2》（Darkest Dungeon II）这件事交给本地引擎：**开车跑图 → 选路 → 战斗出招 → 旅馆买饰品装备 → 团灭/减员自动重开**，人只在出事时被叫醒。

做成 DSH（DeepSeek Harness）插件：一个整页面板盯状态，三个工具让模型直接读状态/启停挂机/装游戏内插件。

> **决策全在本地 Python 里跑，模型不逐回合出招。** 没有云、没有截图识别、不注入系统键鼠。

| | |
|---|---|
| 平台 | Windows 10/11，Steam 版 DD2（appid `1940340`） |
| 依赖 | BepInEx **5.4.x**（Mono）+ Python **3.8+** + DSH |
| 许可 | MIT |

---

## 它能干什么

- **跑图**：长按「前进」、认节点面板、处理剧情长按、卡住自己救（摘 `CanDrive` 锁 → 脉冲前进 → 重启托管进程）。
- **战斗**：按本地技能表出招，认 token / 死门 / 站位 / 敌方剩余血量；默认**死战到底**（不主动撤退），要允许撤退设 `DD2_ALLOW_RETREAT=1`。
- **旅馆**：按评分买饰品、给四个人装上、**装完回读验证**。
- **兜底**：减员 → 打完本场 → 放弃征程 → 原地重开；团灭 → 自动重开；连续卡同一画面 → 写 `alert.json` 停手等人。
- **面板**：状态、四张角色卡（HP/压力/死门/token/技能/饰品）、战斗卡片、告警条、一键启停。

## 安装

### 1) 装 BepInEx（游戏侧，必须）

从 <https://github.com/BepInEx/BepInEx/releases> 下载 `BepInEx_win_x64_5.4.23.x.zip`，解压进游戏目录（和 `Darkest Dungeon II.exe` 同一层）。启动一次游戏再退出，确认游戏目录里出现 `BepInEx\plugins\`。

### 2) 装本插件（DSH 侧）

- GUI：**插件 → 添加插件**，选这个目录；或
- 让模型调 `plugin_manager` → `install_bundle`，`target` 给本目录绝对路径。

装好后侧栏出现「暗黑地牢 2」，输入框敲 `/dd2 status` 有回显就通了。

### 3) 装游戏内插件

```
/dd2 install
```

它自己找游戏目录（注册表 `SteamPath` → 每个库的 `libraryfolders.vdf`），把 `assets\DD2TextMod.dll` 拷进 `BepInEx\plugins\DD2TextMod\`（根目录若有旧副本会一起替换——BepInEx 同名只加载一份，那份优先）。

**然后重启游戏**，`/dd2 status` 里「游戏」那行不再是「不可达」即为成功。

## 用法

```
/dd2 status      # 状态
/dd2 install     # 装/升级游戏内插件
/dd2 start 4     # 起 watchdog 挂 4 小时（团灭/减员自动重开）
/dd2 runner 30   # 只跑 30 分钟 runner（不重开）
/dd2 stop        # 停掉本机所有 runner/watchdog（游戏不动）
/dd2 restart     # 放弃本局并重开（破坏性）
/dd2 quitgame    # 关游戏（走 launcher 18083；开游戏永远归人）
```

模型侧的三个工具：

| 工具 | 用途 |
|---|---|
| `dd2_status` | 一次调用出全部状态，**不用读 report.json / 日志** |
| `dd2_run` | `start` / `runner` / `stop` / `restart` |
| `dd2_install` | 装自带 DLL；缺 BepInEx 时明确报出来 |

## 工作方式

```
DSH 面板 (本插件)
   │  18084 本机桥（带 token）        18082 HTTP
   ├──────────────► Python 引擎 ◄────────────── 游戏内 BepInEx 插件
   │                 (engine/)                   (DD2TextMod.dll)
   └─ 起/停 watchdog、runner                      反射读状态 / 派 UI 事件
```

- 引擎第一次加载会从包内 `engine/` 铺到 `%LOCALAPPDATA%\dsh-dd2\engine` 再跑；报告、日志、告警都落在那儿，插件升级时按版本号自动同步。
- `report.json` 每 tick ≤120 B；只有 `alert.json` 出现才需要人/模型进场。

## 目录结构

```
index.js            宿主半边：面板数据、启停进程、三个 agent 工具、装 DLL
client.js           页面半边（React，走 18084 桥）
engine/             决策引擎（16 个 Python 模块 + db/ + data/），由 tools/build.ps1 生成
assets/DD2TextMod.dll  游戏内插件（BepInEx 5），由 tools/build.ps1 生成
src/mod/            游戏内插件的 C# 源码（15 个 .cs + csproj，可自行编译）
tools/build.ps1     从开发工作区同步 engine/ 与 DLL
tools/smoke.mjs     不开 DSH 直接跑 apply() 自检
docs/DETAIL.md      插件商店页正文
```

## 已知限制（请先读）

- **绑游戏版本**：插件靠反射读 `IronCrown` 的字段，开发时针对 Unity **2022.3.62f2**。游戏一更新就可能失效，届时需要重新编译/适配。
- **分辨率**：坐标点按 **1920×1080** UI 空间调过；其它分辨率、UI 缩放、超宽屏**未验证**。
- **只测过一台机器**：Windows 10 + Steam 默认安装 + 简中。英文/其它语言的界面文案可能读不到（中文名取自游戏自带 `zh_CN.po`，取不到就显示 dataId）。
- **会动你的真实存档**：存档在 `%USERPROFILE%\AppData\LocalLow\RedHook\Darkest Dungeon II\SaveFiles`。**首次使用前请自己备份**，别拿主力存档试。
- **同时只装一个面板**：本插件占 `127.0.0.1:18084`；同时启用两个 DD2 面板会抢端口（可用环境变量 `DD2_BRIDGE_PORT` 改）。
- **只管自己的进程树**：挂机若是 DSH 后台 job 或手工起的，面板只能靠 `logs\*.lock` 判断有没有人在跑。
- 关掉 DSH 会连带停掉本插件起的子进程（避免孤儿进程）。

## 免责

- 仅用于**单机**。不要用于联机/竞技场景。
- 自动化操作可能违反你对游戏的使用预期，也可能触发反作弊或服务条款限制；**风险自负**。
- 本仓库不含游戏本体、不含 BepInEx 二进制，也不含任何游戏资源文件（`db/` 里的数值表是从游戏本体导出的结构化数据，仅用于本地决策）。

## 开发

```powershell
# 改引擎（工作区里的 runner.py / combat3.py / inn.py ...）之后同步进包：
.\tools\build.ps1

# 不开 DSH 自检插件：
node tools\smoke.mjs

# 编译游戏内插件（需要本机装好游戏与 BepInEx；GameDir 指向游戏目录）
dotnet build src\mod\DD2TextMod.csproj -c Release -p:GameDir="D:\Program Files (x86)\Steam\steamapps\common\Darkest Dungeon® II"
```

## License

MIT，见 [LICENSE](LICENSE)。BepInEx 是 LGPL-2.1，本项目不附带其二进制。

---

## English

A DeepSeek Harness plugin that autoplays **Darkest Dungeon II** (Steam, Windows) with a local Python engine and an in-game BepInEx plugin. It drives the game through the game's own UI event system over `127.0.0.1:18082` — no screenshots, no OS-level input injection, no cloud.

Install: BepInEx 5.4.x into the game folder → add this directory as a DSH plugin → run `/dd2 install` → restart the game → `/dd2 status`.

Known limits: reflection is pinned to Unity 2022.3.62f2; coordinates were tuned at 1920×1080; it drives your real save, so back it up first.
