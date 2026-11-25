# 更新日志

## 0.1.0 — 2026-11-25

首个公开版。

- **引擎可移植化**：新增 `engine/dd2paths.py`（注册表 `SteamPath` → 每个库的 `libraryfolders.vdf` → 默认路径），`launcher.py` / `watchdog.py` 不再写死 `D:\...` 游戏路径；找不到时仍退回原路径，本机行为不变。
- **打包成 DSH 插件**：不再依赖开发工作区。包内 `engine/` 首次加载铺到 `%LOCALAPPDATA%\dsh-dd2\engine` 再跑，报告/日志/告警都落在那儿，按版本号自动同步。
- **新增 agent 工具**：`dd2_status` / `dd2_run` / `dd2_install`；新增命令 `/dd2 install`。
- **修 bug**：`/dd2 start`、`/dd2 stop` 少写 `await`，导致返回的 `text` 是 `undefined`。
- **新增自检**：`tools/smoke.mjs`（不开 DSH 直接跑 `apply()`）、`tools/build.ps1`（从工作区同步 engine 与 DLL）。
