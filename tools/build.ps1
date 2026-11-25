# tools\build.ps1 —— 从开发工作区把「引擎源码 + 数据 + 插件 DLL」同步进本包。
#
#   .\tools\build.ps1                       # 默认取本包上一级目录当工作区
#   .\tools\build.ps1 -Source "E:\某工作区"   # 指定别的工作区
#   .\tools\build.ps1 -DryRun               # 只打印会复制什么
#
# 为什么要有这个脚本：发布包必须自包含（别人机器上没有工作区），
# 但引擎的**唯一真相源**在工作区里。所以 engine\ 和 assets\ 是产物，别手改；
# 改引擎 → 跑一次这个脚本 → 提交。
param(
  [string]$Source = '',
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
if (-not $Source) { $Source = Split-Path $root -Parent }
$engine = Join-Path $root 'engine'
$assets = Join-Path $root 'assets'

# runner/watchdog/launcher 的 import 闭包（用 _tmp\dep_scan.py 现算，这里列死避免漂移）
$modules = @(
  'runner.py', 'combat3.py', 'inn.py', 'watchdog.py', 'loadout.py', 'launcher.py',
  'desk.py', 'dd2paths.py', 'winfind.py', 'trinkets.py', 'stuckwatch.py',
  'dd2poll.py', 'capture_win.py', 'instance_lock.py', 'gamedata.py', 'qq_send.py'
)
# 运行时真正打开的 db 文件（combat3/inn/loadout/trinkets 里 open() 的那几个）
$dbFiles = @(
  'skills.json', 'items.json', 'trinkets.json', 'trinkets_live.json',
  'skills_full.json', 'paths.json', 'skills.loadout.json'
)
$dataFiles = @('skill_move.json', 'trinket_stats.json')
$rootFiles = @('游戏数据-怪物技能.json')

$dll = Join-Path $Source 'mod\bin\Release\netstandard2.1\DD2TextMod.dll'

$missing = @()
foreach ($m in $modules) { if (-not (Test-Path (Join-Path $Source $m))) { $missing += $m } }
foreach ($f in $dbFiles) { if (-not (Test-Path (Join-Path $Source "db\$f"))) { $missing += "db\$f" } }
foreach ($f in $dataFiles) { if (-not (Test-Path (Join-Path $Source "data\$f"))) { $missing += "data\$f" } }
foreach ($f in $rootFiles) { if (-not (Test-Path (Join-Path $Source $f))) { $missing += $f } }
if (-not (Test-Path $dll)) { $missing += 'mod\bin\Release\netstandard2.1\DD2TextMod.dll' }
if ($missing.Count) { throw "源目录缺文件（先确认 -Source 指对）：`n  " + ($missing -join "`n  ") }

if ($DryRun) {
  "会复制到 engine\: $($modules.Count) 个 py + $($dbFiles.Count) 个 db + $($dataFiles.Count) 个 data + $($rootFiles.Count) 个根 json"
  "会复制到 assets\: DD2TextMod.dll ($([Math]::Round((Get-Item $dll).Length / 1KB)) KB)"
  return
}

New-Item -ItemType Directory -Force -Path $engine, (Join-Path $engine 'db'), (Join-Path $engine 'data'), $assets | Out-Null

foreach ($m in $modules) { Copy-Item (Join-Path $Source $m) (Join-Path $engine $m) -Force }
foreach ($f in $dbFiles) { Copy-Item (Join-Path $Source "db\$f") (Join-Path $engine "db\$f") -Force }
foreach ($f in $dataFiles) { Copy-Item (Join-Path $Source "data\$f") (Join-Path $engine "data\$f") -Force }
foreach ($f in $rootFiles) { Copy-Item (Join-Path $Source $f) (Join-Path $engine $f) -Force }
Copy-Item $dll (Join-Path $assets 'DD2TextMod.dll') -Force

$sum = (Get-ChildItem $engine -Recurse -File | Measure-Object Length -Sum)
"engine\  {0} 个文件, {1:N2} MB" -f $sum.Count, ($sum.Sum / 1MB)
"assets\  DD2TextMod.dll {0:N0} KB ({1})" -f ((Get-Item (Join-Path $assets 'DD2TextMod.dll')).Length / 1KB), (Get-Item (Join-Path $assets 'DD2TextMod.dll')).LastWriteTime
