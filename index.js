/**
 * DD2 挂机面板 —— Host 半边（v3）。
 *
 * 职责：
 *  1. 读状态：18082 /brief + /state（战斗态有 round/actors/tokens）+ partyInfo + 每人 heroSkills + trinkets，
 *     再拼 alert.json / runner.lock / watchdog.lock / launcher(18083) 在线情况；
 *  2. 起停本插件托管的 python 子进程（watchdog.py / runner.py）；
 *  3. 动作：quitgame 只走 launcher 18083（红线）；restart = mod 的 abandonRun（StateBuilder.cs:583）后交给挂机重开；
 *  4. 给浏览器面板一个 127.0.0.1:18084 的桥（commands remote 要 sessionId，全局页面拿不到）。
 *
 * 红线：不碰游戏进程、不用 OS 键鼠、不 Start-Process 游戏。开游戏永远归人。
 */
import { spawn, execFile, execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync, existsSync, statSync, mkdirSync, cpSync, copyFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { createServer } from 'node:http'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'

export const inject = ['commands']

const PKG_DIR = dirname(fileURLToPath(import.meta.url))
const VERSION = (() => {
  try { return JSON.parse(readFileSync(join(PKG_DIR, 'package.json'), 'utf8')).version || '0.0.0' } catch { return '0.0.0' }
})()

// ---------------------------------------------------------------------------
// 自定位：别人的机器上没有开发工作区，所以引擎放包里的 engine/，
// 运行时铺到可写目录（%LOCALAPPDATA%\dsh-dd2\engine）再跑，日志/报告都落那儿。
// ---------------------------------------------------------------------------
const GAME_FOLDER = 'Darkest Dungeon® II'
const GAME_EXE = 'Darkest Dungeon II.exe'

function engineDirDefault() {
  const env = String(process.env.DD2_ENGINE_DIR || '').trim()
  if (env) return env
  const base = process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local')
  return join(base, 'dsh-dd2', 'engine')
}

/** 把包内 engine/ 同步到可写目录；版本没变就跳过。返回是否同步过。 */
function syncEngine(workdir) {
  const src = join(PKG_DIR, 'engine')
  if (!existsSync(src)) return false
  const stamp = join(workdir, '.engine-version')
  const want = `${VERSION}\n`
  try { if (readFileSync(stamp, 'utf8') === want) return false } catch {}
  mkdirSync(workdir, { recursive: true })
  cpSync(src, workdir, { recursive: true, force: true })
  for (const d of ['logs', join('logs', 'fails')]) {
    try { mkdirSync(join(workdir, d), { recursive: true }) } catch {}
  }
  try { writeFileSync(stamp, want) } catch {}
  return true
}

// 游戏目录：与 engine/dd2paths.py 同一套规则（环境变量 → 注册表 → libraryfolders.vdf → 默认路径）。
function regGet(hive, key, name) {
  try {
    const out = execFileSync('reg', ['query', `${hive}\\${key}`, '/v', name], { encoding: 'utf8', windowsHide: true, timeout: 4000 })
    const m = out.match(new RegExp(`${name}\\s+REG_(?:EXPAND_)?SZ\\s+(.+)`, 'i'))
    return m ? m[1].trim() : null
  } catch { return null }
}

function vdfPaths(vdf) {
  try {
    const txt = readFileSync(vdf, 'utf8')
    return [...txt.matchAll(/"path"\s+"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1].replace(/\\\\/g, '\\'))
  } catch { return [] }
}

function discoverGameDir() {
  const env = process.env.DD2_GAME_DIR
  if (env && existsSync(join(env, GAME_EXE))) return env
  const steam = regGet('HKCU', 'Software\\Valve\\Steam', 'SteamPath')
    || regGet('HKLM', 'SOFTWARE\\WOW6432Node\\Valve\\Steam', 'InstallPath')
  const roots = []
  if (steam) {
    roots.push(steam)
    roots.push(...vdfPaths(join(steam, 'steamapps', 'libraryfolders.vdf')))
  }
  roots.push('D:\\Program Files (x86)\\Steam', 'C:\\Program Files (x86)\\Steam')
  for (const r of roots) {
    const cand = join(r, 'steamapps', 'common', GAME_FOLDER)
    if (existsSync(join(cand, GAME_EXE))) return cand
  }
  return null
}

let GAME_DIR = discoverGameDir()

/** python 解释器：DD2_PYTHON → python → py -3 → python3。找不到返回 null。 */
let pyCmd = null
function pythonCmd() {
  if (pyCmd) return pyCmd
  const env = String(process.env.DD2_PYTHON || '').trim()
  if (env) return (pyCmd = [env])
  for (const c of [['python'], ['py', '-3'], ['python3']]) {
    try {
      execFileSync(c[0], [...c.slice(1), '-c', 'import sys;raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'], { timeout: 8000, windowsHide: true, stdio: 'ignore' })
      return (pyCmd = c)
    } catch {}
  }
  return null
}

const PY_HELP = '没找到可用的 Python（需要 3.8+）。装好后勾选 “Add python.exe to PATH”，或用环境变量 DD2_PYTHON 指到 python.exe。'

const GAME = 'http://127.0.0.1:18082'
const LAUNCHER = 'http://127.0.0.1:18083'
const BRIDGE_PORT = Number(process.env.DD2_BRIDGE_PORT || 18084)
const SNAPSHOT_TTL_MS = 2500

// 游戏自带中文（gettext .po）：msgctxt 是键、msgstr 是官方译文。
// 键规则（对过游戏 Sources/*.txt）：职业/敌人=dataId；道途=hero_path_name_<id>；
// 技能=skill_name_<id>；token=token_name_<id>；饰品/道具=item_name_<id>。
// 路径跟着游戏目录走（GAME_DIR 在下面由 discoverGameDir() 决定），不再写死盘符。
function poPathFor(dir) {
  return dir ? join(dir, 'Darkest Dungeon II_Data', 'StreamingAssets', 'Localization', 'Poedit', 'zh_CN.po') : ''
}
let PO_PATH = poPathFor(GAME_DIR)
let poCache = { mtime: -1, map: new Map() }

function stripMarkup(s) {
  return String(s).replace(/<[^>]*>/g, '').replace(/\{q\}/g, '').replace(/\s+/g, ' ').trim()
}

function loadPo() {
  try {
    const mtime = statSync(PO_PATH).mtimeMs
    if (mtime === poCache.mtime) return poCache.map
    const txt = readFileSync(PO_PATH, 'utf8')
    const map = new Map()
    for (const block of txt.split(/\r?\n\r?\n/)) {
      const ctx = block.match(/^msgctxt "(.*)"$/m)
      if (!ctx) continue
      const at = block.search(/^msgstr /m)
      if (at < 0) continue
      const quoted = [...block.slice(at).matchAll(/"((?:[^"\\]|\\.)*)"/g)]
        .map((m) => m[1].replace(/\\n/g, '\n').replace(/\\"/g, '"').replace(/\\\\/g, '\\'))
        .join('')
      if (!quoted) continue
      map.set(ctx[1], stripMarkup(quoted))
    }
    poCache = { mtime, map }
    return map
  } catch {
    return poCache.map
  }
}

function zhOf(map, ...keys) {
  for (const k of keys) {
    if (!k) continue
    const v = map.get(k)
    if (v) return v
  }
  return null
}

function readJson(p) {
  try { return JSON.parse(readFileSync(p, 'utf8')) } catch { return null }
}

function lockInfo(p) {
  if (!existsSync(p)) return { free: true }
  let mtime = 0
  try { mtime = Math.round((Date.now() - statSync(p).mtimeMs) / 1000) } catch {}
  const txt = readJson(p)
  const pid = txt && (txt.pid ?? txt.owner ?? null)
  return { free: false, pid, staleSeconds: mtime }
}

// 饰品槽数不从天上猜：读 inn.py 的 TRINKET_SLOTS（inn.py:62 = 2，装备链路就在用它）。
function innTrinketSlots(workdir) {
  try {
    const m = readFileSync(join(workdir, 'inn.py'), 'utf8').match(/^TRINKET_SLOTS\s*=\s*(\d+)/m)
    return m ? Number(m[1]) : null
  } catch { return null }
}

/**
 * 面板动作令牌：落在 logs\.dd2panel-token，插件热重载/进程重启后不变，
 * 浏览器手里的 token 就不会因为重载变成"token 不对"。
 */
function panelToken(workdir) {
  const p = join(workdir, 'logs', '.dd2panel-token')
  try {
    const t = readFileSync(p, 'utf8').trim()
    if (/^[0-9a-f]{16,}$/.test(t)) return t
  } catch {}
  const t = randomBytes(12).toString('hex')
  try { writeFileSync(p, t) } catch {}
  return t
}

/** 桥诊断日志：只写请求来源/结果，用于排查面板拿不到 token 这类链问题。 */
function bridgeLog(workdir, msg) {
  const p = join(workdir, 'logs', '.dd2panel-bridge.log')
  try {
    if (existsSync(p) && statSync(p).size > 200000) writeFileSync(p, '')
    writeFileSync(p, `${new Date().toISOString()} ${msg}\n`, { flag: 'a' })
  } catch {}
}

/**
 * 马车"撞墙"判据（纯函数，便于用日志样本回放）。
 * 样本只用 /brief 的实测字段：mode/scene/row/spd/btn。
 * - 只在 map 态判：战斗、旅馆、祭坛都不算；
 * - spd <= 0.5 且 (scene,row,btn) 三件套不变才算"原地不动"——车停在节点上选东西时
 *   btn 文字会变，所以不会被误判成撞墙；
 * - 阶梯修复：25s 摘 CanDrive 锁 → 35s 脉冲前进 → 60s 重启托管进程（今天 08:26~10:08
 *   那几次卡死现场就是 spd=0.0 + act=drive，重拉 runner 立刻恢复）。
 */
const STALL_SPEED = 0.5
const STALL_UNBLOCK_SEC = 25
const STALL_DRIVE_SEC = 35
const STALL_RESTART_SEC = 60

export function stallStep(prev, s, now) {
  const p = prev || { key: '', since: 0, stage: 0, at: 0 }
  const idle = { key: '', since: 0, stage: 0, at: 0 }
  if (!s || String(s.mode) !== 'map') return { next: idle, action: null, stuckSec: 0 }
  // 只有明确报 0（数字）才算停；字段缺失（null/undefined）不能当 0，否则会误修。
  const spd = typeof s.spd === 'number' ? s.spd : NaN
  if (!Number.isFinite(spd) || spd > STALL_SPEED) return { next: idle, action: null, stuckSec: 0 }
  // btn 有字 = 游戏在等一个按钮/事件（节点选择、奇遇、战利品），不是"撞墙"。
  if (typeof s.btn === 'string' && s.btn.trim()) return { next: idle, action: null, stuckSec: 0 }
  const key = `${s.scene}|${s.row}|${s.btn}`
  // 用 key 相等来判断"同一现场"：不能写 `p.since &&`，因为首样本的 now 可能恰为 0。
  const since = p.key === key ? p.since : now
  const stuckSec = Math.round((now - since) / 1000)
  let { stage, at } = p
  let action = null
  if (stuckSec >= STALL_RESTART_SEC && stage < 3) { action = 'restart'; stage = 3; at = now }
  else if (stuckSec >= STALL_DRIVE_SEC && stage < 2) { action = 'drive'; stage = 2; at = now }
  else if (stuckSec >= STALL_UNBLOCK_SEC && stage < 1) { action = 'unblock'; stage = 1; at = now }
  return { next: { key, since, stage, at }, action, stuckSec }
}

/**
 * 跑图自检：用真实位移判断"是不是在顺畅前进"（纯函数，可回放）。
 * 为什么不能只看速度：`runner.py:147` 那段注释就写过——暂停/卡死时 `GetSpeed()` 会残留 12.1，
 * 速度>0 但车一步没动。所以这里以 `/action {type:'coach'}` 的 posX/posZ（Coach.cs:38-56）
 * 为主证据，速度只用来区分"空转"和"停着"。
 *
 * 状态：moving（在走）/ waiting（等按钮/事件，不修）/ spinning（有速度没位移）/
 *       jerky（来回蹭：路径远、净位移小）/ stalled（停着没速度）
 * 修复阶梯与 stallStep 一致：15s 摘锁 → 30s 脉冲 → 60s 重启托管进程。
 */
const TRAVEL_MOVE_M = 1.0      // 单次采样（5 s）位移 >= 1 m 算出在走
const TRAVEL_HIST = 6          // 保留最近 6 个位置点（约 30 s）
const TRAVEL_BLOCKED_FIRST_SEC = 10  // 明确被 CanDrive 锁住：10 s 就摘锁，不等 15 s
const TRAVEL_UNBLOCK_SEC = 15
const TRAVEL_DRIVE_SEC = 30
const TRAVEL_RESTART_SEC = 60

export function travelStep(prev, s, now) {
  const idle = { key: '', since: 0, stage: 0, at: 0, hist: [] }
  const done = (next, state, extra = {}) => ({ next, state, action: null, stuckSec: 0, blockers: [], net: 0, path: 0, speed: null, ...extra })
  if (!s || String(s.mode) !== 'map') return done(idle, 'n/a')

  const c = s.coach && s.coach.ok !== false ? s.coach : null
  const blockers = c && Array.isArray(c.canDrive) ? c.canDrive.filter((x) => /false\s*$/i.test(String(x))) : []
  const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : null }
  // 速度以 coach 的为准（它当场读 GetSpeed()），brief 的 spd 只在没有 coach 时用。
  const spd = (c && num(c.speed) !== null) ? num(c.speed) : (typeof s.spd === 'number' ? s.spd : null)
  // 真正"等 UI"的证据只有这几个：旁白面板 / 岔路口选择 / narration 面板。
  // 不能用 brief.btn 判断——map 上那个"最后一个地区…"按钮是常驻的，拿它当 UI 会永远不修（11:42 就是这样漏掉的）。
  const truthy = (v) => v === true || /^(true|1)$/i.test(String(v ?? ''))
  const uiWaiting = (c && Number(c.narrationCount) > 0)
    || (c && Number(c.intersectionCount) > 0)
    || (c && c.st && truthy(c.st.inNarrationPanel))
  if (uiWaiting) return done({ ...idle, key: `${s.scene}|${s.row}|ui` }, 'waiting', { blockers, speed: spd })

  const hist = ((prev && prev.hist) || []).slice(-TRAVEL_HIST)
  if (c && typeof c.posX === 'number' && typeof c.posZ === 'number') hist.push({ x: c.posX, z: c.posZ, t: now })
  let d = 0, net = 0, path = 0
  if (hist.length >= 2) {
    for (let i = 1; i < hist.length; i++) path += Math.hypot(hist[i].x - hist[i - 1].x, hist[i].z - hist[i - 1].z)
    const a = hist[0], z = hist[hist.length - 1]
    net = Math.hypot(z.x - a.x, z.z - a.z)
    d = Math.hypot(z.x - hist[hist.length - 2].x, z.z - hist[hist.length - 2].z)
  }

  const blocked = blockers.length > 0
  let state
  if (hist.length >= 3) {
    // 先判"来回蹭"：路径远大于净位移（撞墙/被挡时车轮原地打转的典型形状）
    if (path >= 3 && path > net * 3) state = 'jerky'
    else if (d >= TRAVEL_MOVE_M) state = 'moving'
    else if (blocked) state = 'blocked'          // canDrive 里有人返回 False = 明确被锁
    else if (spd !== null && spd > STALL_SPEED) state = 'spinning'
    else state = 'stalled'
  } else if (hist.length === 2) {
    state = d >= TRAVEL_MOVE_M ? 'moving' : (blocked ? 'blocked' : ((spd !== null && spd > STALL_SPEED) ? 'spinning' : 'stalled'))
  } else {
    // 还没有位置点（刚进 map / coach 读不到）：退化到"有速度就算在走"
    state = spd !== null && spd > STALL_SPEED ? 'moving' : (blocked ? 'blocked' : 'stalled')
  }

  if (state === 'moving') return done({ key: '', since: 0, stage: 0, at: 0, hist }, 'moving', { blockers, speed: spd, net, path })

  const key = `still|${s.scene}|${s.row}`
  const since = (prev && prev.key === key && Number.isFinite(prev.since)) ? prev.since : now
  const stuckSec = Math.round((now - since) / 1000)
  let stage = prev ? (prev.stage || 0) : 0
  let at = prev ? (prev.at || 0) : 0
  let action = null
  // 被 CanDrive 锁住：第一次 10 s 就摘锁；摘完还不走，每 20 s 再摘一次（锁可能是新挂上的）。
  if (blocked && stage >= 1 && now - at >= 20000) { action = 'unblock'; at = now }
  else if (stuckSec >= TRAVEL_RESTART_SEC && stage < 3) { action = 'restart'; stage = 3; at = now }
  else if (stuckSec >= TRAVEL_DRIVE_SEC && stage < 2) { action = 'drive'; stage = 2; at = now }
  else if (stuckSec >= (blocked ? TRAVEL_BLOCKED_FIRST_SEC : TRAVEL_UNBLOCK_SEC) && stage < 1) { action = 'unblock'; stage = 1; at = now }
  return { next: { key, since, stage, at, hist }, state, action, stuckSec, blockers, net, path, speed: spd }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function getJson(url, ms = 1500) {
  const ac = new AbortController()
  const t = setTimeout(() => ac.abort(), ms)
  try {
    const r = await fetch(url, { signal: ac.signal })
    return await r.json()
  } catch (e) {
    return { ok: false, error: String(e?.message ?? e) }
  } finally { clearTimeout(t) }
}

async function postJson(url, body, ms = 2500) {
  const ac = new AbortController()
  const t = setTimeout(() => ac.abort(), ms)
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
      signal: ac.signal,
    })
    return await r.json()
  } catch (e) {
    return { ok: false, error: String(e?.message ?? e) }
  } finally { clearTimeout(t) }
}

export function apply(ctx, config = {}) {
  if (config.gameDir) { GAME_DIR = String(config.gameDir); PO_PATH = poPathFor(GAME_DIR) }
  // workdir：默认用「包内引擎铺到可写目录」，这样装在任意机器上都能跑；
  // 开发时想直接用工作区，就把 workdir 指过去（那一份不会被覆盖）。
  const devWorkdir = String(config.workdir || '').trim()
  const workdir = devWorkdir || engineDirDefault()
  let engineSynced = false
  let engineError = null
  if (devWorkdir) {
    try { mkdirSync(join(workdir, 'logs'), { recursive: true }) } catch {}
  } else {
    try { engineSynced = syncEngine(workdir) } catch (e) { engineError = `${e?.message ?? e}（目标 ${workdir}）` }
  }
  const defaultHours = Number(config.defaultHours ?? 4)
  const autoLoadout = config.autoLoadout !== false
  const autoRepair = config.autoRepair !== false
  const token = panelToken(workdir)
  const trinketSlots = innTrinketSlots(workdir)

  let child = null
  let childKind = ''
  let launcherChild = null
  let cache = { at: 0, data: null }

  const alive = () => !!child && child.exitCode === null && !child.killed

  function start(kind, hours) {
    if (alive()) return { ok: true, text: `已在跑：${childKind} (pid ${child.pid})` }
    const py = pythonCmd()
    if (!py) return { ok: false, text: PY_HELP }
    heroStoryHold = false
    const args = kind === 'runner'
      ? ['runner.py', '--minutes', String(Math.max(5, Math.round(hours * 60))), '--report-json', '--quiet']
      : ['watchdog.py', '--hours', String(hours)]
    const env = { ...process.env }
    if (autoLoadout) env.DD2_AUTO_LOADOUT = '1'
    child = spawn(py[0], [...py.slice(1), ...args], { cwd: workdir, env, windowsHide: true, stdio: 'ignore' })
    childKind = kind
    child.on('exit', () => { child = null; childKind = '' })
    return { ok: true, text: `已启动 ${kind} (pid ${child.pid}) hours=${hours}` }
  }

  // 手上真在跑的挂机进程：只认 python.exe 且命令行含 watchdog.py / runner.py。
  // 不看 child —— 挂机常由后台 job / 人工起，插件没托管也必须停得掉（2026-11-25 作者定：停止按钮最高优先级）。
  // 不匹配 launcher.py / desk.py，也不碰游戏进程。
  function listHangProcs() {
    const ps = '$a=@(Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | Where-Object { $_.CommandLine -match \'watchdog\\.py|runner\\.py\' });'
      + "foreach($p in $a){ '{0}|{1}' -f $p.ProcessId, ($p.CommandLine -replace '\\s+',' ') }"
    return new Promise((resolve) => {
      execFile('powershell', ['-NoProfile', '-NonInteractive', '-Command', ps], { windowsHide: true, timeout: 8000 }, (err, stdout) => {
        const rows = String(stdout || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean).map((s) => {
          const i = s.indexOf('|')
          return { pid: Number(s.slice(0, i)), cmd: s.slice(i + 1) }
        }).filter((r) => Number.isFinite(r.pid) && r.pid > 0)
        resolve({ rows, err: err ? String(err.message || err) : null })
      })
    })
  }

  function killPid(pid) {
    return new Promise((resolve) => {
      execFile('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true }, (err, stdout, stderr) => {
        resolve({ ok: !err, out: String(stdout || stderr || (err && err.message) || '').trim() })
      })
    })
  }

  async function stop() {
    const wasChild = alive() ? child.pid : null
    const before = await listHangProcs()
    if (!before.rows.length) {
      if (wasChild) await killPid(wasChild)
      child = null; childKind = ''
      return { ok: true, text: wasChild ? `已停挂机 (pid ${wasChild})；游戏本身不动` : '当前没有在跑的挂机进程（runner/watchdog 都没找到）' }
    }
    // 先 watchdog 后 runner：反了会被 watchdog 判退出码后重拉（watchdog.py:828-837）。
    const isWd = (c) => (/watchdog\.py/.test(c) ? 0 : 1)
    const rows = [...before.rows].sort((x, y) => isWd(x.cmd) - isWd(y.cmd))
    const killed = []
    for (const r of rows) {
      const k = await killPid(r.pid)
      killed.push(`${r.pid}${k.ok ? '' : '(失败)'}`)
      if (isWd(r.cmd) === 0) await sleep(900)
    }
    child = null; childKind = ''
    await sleep(700)
    const after = await listHangProcs()
    const left = after.rows.map((r) => `${r.pid}:${r.cmd.slice(0, 80)}`)
    const tail = left.length ? `；仍存活 ${left.join(' / ')}` : '；复查已无 runner/watchdog'
    return { ok: !left.length, text: `已停挂机 pid ${killed.join(',')}${tail}；游戏本身不动` }
  }

  async function launcherOnline() {
    const r = await getJson(`${LAUNCHER}/status`, 700)
    return !!(r && r.ok !== false && !r.error)
  }

  // 关游戏只走 18083（红线）。18083 没起就把 launcher.py 这个"控制口"拉起来——它不是游戏本体。
  async function ensureLauncher() {
    if (await launcherOnline()) return true
    try {
      const py = pythonCmd()
      if (!py) return false
      launcherChild = spawn(py[0], [...py.slice(1), 'launcher.py'], { cwd: workdir, windowsHide: true, stdio: 'ignore' })
      launcherChild.on('exit', () => { launcherChild = null })
    } catch {}
    await sleep(1500)
    return await launcherOnline()
  }

  async function quitGame() {
    if (!(await ensureLauncher())) {
      return { ok: false, text: 'launcher(18083) 起不来；按红线不直接杀进程，请人工处理' }
    }
    try {
      const r = await fetch(`${LAUNCHER}/quit`, { method: 'POST' })
      const j = await r.json().catch(() => ({}))
      return { ok: true, text: `launcher /quit -> HTTP ${r.status}${j && j.ok === false ? ' ' + (j.err || '') : ''}` }
    } catch (e) {
      return { ok: false, text: `launcher /quit 失败：${e?.message ?? e}` }
    }
  }

  // 放弃本局：调 mod 的 abandonRun（照游戏 PauseMenuUiControllerBhv.OnAbandonRun，StateBuilder.cs:577-606）。
  // 之后不新造重开逻辑：本插件托管的 watchdog 会走「祭坛→altarEmbark」，没托管就起一个。
  async function restartRun() {
    const r = await postJson(`${GAME}/action`, { type: 'abandonRun', manual: true })
    if (!r || r.ok === false) return { ok: false, text: `abandonRun 失败：${(r && (r.error || r.why)) || '无响应'}` }
    if (alive()) return { ok: true, text: `已放弃本局（${r.reason || 'abandon'}）；托管的 ${childKind} 会在祭坛重开` }
    const s = start('watchdog', defaultHours)
    return { ok: true, text: `已放弃本局（${r.reason || 'abandon'}）；${s.text}，由它接手重开` }
  }

  async function snapshot(force = false) {
    if (!force && cache.data && Date.now() - cache.at < SNAPSHOT_TTL_MS) return cache.data
    const [brief, state, pinfo, tr, launcher] = await Promise.all([
      getJson(`${GAME}/brief`, 1200),
      getJson(`${GAME}/state`, 2000),
      postJson(`${GAME}/action`, { type: 'partyInfo' }),
      postJson(`${GAME}/action`, { type: 'trinkets' }),
      getJson(`${LAUNCHER}/status`, 600),
    ])
    const po = loadPo()
    const gameOk = !(brief && brief.ok === false) && !(state && state.ok === false)

    const members = Array.isArray(pinfo?.party) ? pinfo.party.filter((p) => String(p.status) === 'party') : []
    const skills = await Promise.all(members.map((p) => postJson(`${GAME}/action`, { type: 'heroSkills', actorGuid: Number(p.guid) })))

    const trByGuid = new Map()
    if (tr && tr.ok && Array.isArray(tr.actors)) {
      for (const a of tr.actors) {
        trByGuid.set(String(a.guid ?? ''), a)
        trByGuid.set(String(a.name ?? ''), a)
      }
    }
    const fightActors = new Map()
    if (state && state.ok !== false && Array.isArray(state.actors)) {
      for (const a of state.actors) fightActors.set(String(a.guid), a)
    }

    const party = members.map((p, i) => {
      const hs = skills[i]
      const baseOf = (hs && hs.baseOf) || {}
      const inFight = fightActors.get(String(p.guid)) || null
      const extra = trByGuid.get(String(p.guid)) || trByGuid.get(String(p.name ?? '')) || null
      const equipped = hs && Array.isArray(hs.equipped) ? hs.equipped : []
      const unlocked = hs && Array.isArray(hs.unlocked) ? hs.unlocked : []
      const path = (hs && hs.path) || p.path
      const hp = inFight ? inFight.hp : p.hp
      const hpMax = inFight ? inFight.hpMax : p.hpMax
      const tokens = inFight && Array.isArray(inFight.tokens)
        ? inFight.tokens.map((t) => ({
          id: t.id, dur: t.dur ?? null,
          zh: zhOf(po, `token_name_${t.id}`, `skill_name_${t.id}`, baseOf[t.id] ? `skill_name_${baseOf[t.id]}` : null) || t.id,
        }))
        : []
      const trkArr = extra && Array.isArray(extra.trinkets) ? extra.trinkets : []
      return {
        guid: String(p.guid), name: p.name || '', dataId: p.dataId,
        classZh: zhOf(po, String(p.dataId ?? '')) || p.dataId,
        path, pathZh: zhOf(po, `hero_path_name_${path}`, `hero_path_name_${path}_u`) || path || '—',
        hp, hpMax,
        hpPct: inFight && inFight.hpPct != null ? Math.round(inFight.hpPct) : (hpMax ? Math.round((hp / hpMax) * 100) : null),
        stress: inFight ? inFight.stress : (extra ? extra.stress : null),
        alive: inFight ? inFight.alive !== false : true,
        deathsDoor: tokens.some((t) => String(t.id).startsWith('deaths_door')),
        tokens,
        skills: equipped.map((id) => ({ id, zh: zhOf(po, `skill_name_${id}`, baseOf[id] ? `skill_name_${baseOf[id]}` : null, `skill_name_${id}_u`) || id })),
        skillCount: { equipped: equipped.length, unlocked: unlocked.length },
        trinkets: trkArr.map((id) => ({ id, zh: zhOf(po, `item_name_${id}`, `item_name_${id}_u`) || id })),
        trinketSlots,
        items: Array.isArray(p.combatItems) ? p.combatItems.length : 0,
      }
    })

    const inCombat = !!(state && state.ok !== false && String(state.mode) === 'combat')
    const combat = inCombat ? {
      round: state.round ?? null,
      turn: state.turn ?? null,
      battleState: state.battleState ?? null,
      currentActor: state.currentActor
        ? { name: state.currentActor.name || '', dataId: state.currentActor.dataId,
          zh: zhOf(po, String(state.currentActor.dataId)) || state.currentActor.dataId }
        : null,
      enemies: (state.actors || []).filter((a) => Number(a.team) === 1).map((a) => ({
        dataId: a.dataId, zh: zhOf(po, String(a.dataId)) || a.dataId,
        hp: a.hp, hpMax: a.hpMax, alive: a.alive !== false,
        deathsDoor: Array.isArray(a.tokens) && a.tokens.some((t) => String(t.id).startsWith('deaths_door')),
      })),
    } : null
    if (combat) combat.enemiesAlive = combat.enemies.filter((e) => e.alive).length

    const runnerLock = lockInfo(join(workdir, 'logs', 'runner.lock'))
    const watchdogLock = lockInfo(join(workdir, 'logs', 'watchdog.lock'))
    const alert = readJson(join(workdir, 'alert.json'))
    const warnings = []
    if (!gameOk) warnings.push({ level: 'red', text: '游戏不可达：18082 无响应' })
    if (alert) {
      warnings.push({
        level: 'amber',
        text: `alert: ${alert.kind || alert.type || 'present'} ${String(alert.msg || alert.reason || '').slice(0, 120)}`.trim(),
      })
    }
    if (!alive() && runnerLock.free && watchdogLock.free) {
      warnings.push({ level: 'amber', text: '挂机进程已停（本插件未托管，runner / watchdog 锁都是空的）' })
    }
    if (repairInfo.stuckSec >= STALL_UNBLOCK_SEC) {
      const kindZh = { stalled: '停着不动', spinning: '有速度没位移（空转）', jerky: '来回蹭' }[travelInfo.state] || '原地不动'
      warnings.push({
        level: 'amber',
        text: `马车${kindZh} ${repairInfo.stuckSec}s（第 ${repairInfo.stage} 档）${repairInfo.lastFix ? '：' + repairInfo.lastFix : ''}`,
      })
    }

    const data = {
      ok: gameOk,
      at: new Date().toISOString(),
      brief: brief && brief.ok === false
        ? { error: brief.error || '无响应' }
        : {
          mode: brief?.mode, gameMode: brief?.gameMode, scene: brief?.scene,
          row: brief?.row ?? null, gold: brief?.gold ?? null, speed: brief?.spd ?? null,
          btn: brief?.btn ?? null, n: brief?.n ?? null,
          hp: brief?.hp ?? null, hint: brief?.hint ?? null,
        },
      combat,
      party,
      bag: Array.isArray(tr?.bag) ? tr.bag.length : null,
      alert,
      launcher: { online: !!(launcher && launcher.ok !== false && !launcher.error) },
      runnerLock, watchdogLock,
      repair: { ...repairInfo, autoRepair },
      travel: { ...travelInfo, autoRepair },
      local: alive() ? { kind: childKind, pid: child.pid } : null,
      warnings,
      ttlMs: SNAPSHOT_TTL_MS,
      i18n: po.size,
      trinketSlots,
    }
    cache = { at: Date.now(), data }
    return data
  }

  // 跑图自检：判定见 travelStep()。每 5 s 采 /brief（约 30 ms）+ map 态再采一次 coach（取真实坐标），
  // 用位移而不是速度判断"是否顺畅在前进"。
  let travelPrev = null
  let travelInfo = { state: 'n/a', speed: null, stuckSec: 0, blockers: [], net: 0, path: 0 }
  let repairInfo = { stuckSec: 0, stage: 0, lastFix: null, at: null }
  let repairing = false
  // 神殿剧情（HERO_STORY_INTRO）一命中就停手并锁住自检，直到人工点「开始挂机」重新 start()。
  let heroStoryHold = false

  async function applyFix(how, info = null) {
    const at = new Date().toISOString()
    if (how === 'unblock') {
      const r = await postJson(`${GAME}/action`, { type: 'coachUnblock' })
      const bad = !!r && r.ok === false
      const who = info && info.blockers && info.blockers.length ? `（${info.blockers.join(';')}）` : ''
      repairInfo = { ...repairInfo, lastFix: `摘 CanDrive 锁${who}${bad ? ' 失败' : ' ok'}`, at }
      bridgeLog(workdir, `repair unblock -> ${JSON.stringify(r).slice(0, 200)}`)
      return
    }
    if (how === 'drive') {
      // 优先自动挡（新 DLL 的 driveAuto：每帧补前进标志，4 s 心跳，节点跑完立刻续速）；
      // 老 DLL 没这个 action 时退回 1.2 s 脉冲。
      let r = await postJson(`${GAME}/action`, { type: 'driveAuto', on: true })
      let howLbl = '自动挡 4s'
      if (!r || r.ok === false) {
        r = await postJson(`${GAME}/action`, { type: 'wagonDrive', ms: 1200 })
        howLbl = '脉冲前进 1.2s'
      }
      const bad = !!r && r.ok === false
      repairInfo = { ...repairInfo, lastFix: `${howLbl}${bad ? ' 失败' : ' ok'}`, at }
      bridgeLog(workdir, `repair drive(${howLbl}) -> ${JSON.stringify(r).slice(0, 200)}`)
      return
    }
    if (!alive()) {
      repairInfo = { ...repairInfo, lastFix: '需要重启 runner（本插件未托管，未自动拉起）', at }
      bridgeLog(workdir, 'repair restart skipped: no managed child')
      return
    }
    const kind = childKind || 'watchdog'
    await stop()
    const s = start(kind, defaultHours)
    repairInfo = { ...repairInfo, lastFix: `重启 ${kind}：${s.text}`, at }
    bridgeLog(workdir, `repair restart ${kind} -> ${s.text}`)
  }

  async function monitorTick() {
    if (!autoRepair || repairing || heroStoryHold) return
    const b = await getJson(`${GAME}/brief`, 900)
    if (!b || b.ok === false) return
    // 神殿剧情兜底：runner 侧已先手（runner.py 主循环判 gameMode 后 return 9），这里防 runner 没带这个改动、
    // 或剧情是在 runner 起来之前就进的。判据同 runner：/state.gameMode == HERO_STORY_INTRO
    // （mod\StateBuilder.cs:50）；命中就停掉挂机并锁住自检，别再去 driveAuto/coachUnblock 动画面。
    const st = await getJson(`${GAME}/state`, 1500)
    if (st && !st.error && String(st.gameMode) === 'HERO_STORY_INTRO') {
      heroStoryHold = true
      repairInfo = { ...repairInfo, lastFix: '神殿剧情：已停手等人工打', at: new Date().toISOString() }
      bridgeLog(workdir, 'hero-story = HERO_STORY_INTRO -> stop 挂机，等人工')
      const r = await stop()
      repairInfo = { ...repairInfo, lastFix: `神殿剧情：已停手等人工打（${r.text}）`, at: new Date().toISOString() }
      return
    }
    const coach = String(b.mode) === 'map' ? await postJson(`${GAME}/action`, { type: 'coach' }) : null
    const r = travelStep(travelPrev, { ...b, coach }, Date.now())
    travelPrev = r.next
    travelInfo = { state: r.state, speed: r.speed, stuckSec: r.stuckSec, blockers: r.blockers, net: r.net, path: r.path }
    repairInfo = { ...repairInfo, stuckSec: r.stuckSec, stage: r.next.stage }
    if (!r.action) return
    repairing = true
    try { await applyFix(r.action, r) } finally { repairing = false }
  }

  async function statusText() {
    const s = await snapshot()
    const b = s.brief || {}
    const parts = []
    parts.push(s.ok ? `游戏 ${b.mode ?? '?'}/${b.scene ?? '?'} row=${b.row ?? '?'} gold=${b.gold ?? '?'}` : `游戏不可达(${b.error || 'no reply'})`)
    if (s.combat) parts.push(`战斗 R${s.combat.round ?? '?'} 敌剩 ${s.combat.enemiesAlive}/${s.combat.enemies.length}`)
    parts.push(s.local ? `${s.local.kind}=本插件 pid ${s.local.pid}` : '本插件未托管进程')
    parts.push(`runner=${s.runnerLock.free ? 'FREE' : 'BUSY'} watchdog=${s.watchdogLock.free ? 'FREE' : 'BUSY'}`)
    parts.push(`launcher=${s.launcher.online ? '在线' : '离线'}`)
    if (s.bag !== null) parts.push(`饰品背包 ${s.bag}`)
    if (heroStoryHold) parts.push('等人工：神殿剧情（点「开始挂机」恢复）')
    if (engineError) parts.push(`引擎不可用：${engineError}`)
    return { kind: s.ok ? 'success' : 'error', text: parts.join(' | ') }
  }

  async function act(doWhat) {
    switch (doWhat) {
      case 'start': return start('watchdog', defaultHours)
      case 'runner': return start('runner', 1)
      case 'stop': return stop()
      case 'restart': return restartRun()
      case 'quitgame': return quitGame()
      default: return { ok: false, text: `未知动作 ${doWhat}` }
    }
  }

  // ---- 装/升级游戏内插件（DD2TextMod.dll）----
  function installDll() {
    const dir = GAME_DIR || discoverGameDir()
    if (!dir) return '没找到游戏目录：装好 Steam 版 DD2 后重试，或在插件 config 里写 gameDir / 设环境变量 DD2_GAME_DIR。'
    const src = join(PKG_DIR, 'assets', 'DD2TextMod.dll')
    if (!existsSync(src)) return '包里没有 assets/DD2TextMod.dll（发布包应自带；开发时先跑 tools/build.ps1）。'
    const bep = join(dir, 'BepInEx')
    if (!existsSync(join(bep, 'core', 'BepInEx.dll'))) {
      return `这台机器还没装 BepInEx 5.x（缺 ${join(bep, 'core', 'BepInEx.dll')}）。\n`
        + `先装 BepInEx 5.4.x：https://github.com/BepInEx/BepInEx/releases （解压进游戏目录 ${dir}），再执行一次。`
    }
    const sub = join(bep, 'plugins', 'DD2TextMod', 'DD2TextMod.dll')
    const root = join(bep, 'plugins', 'DD2TextMod.dll')
    const done = []
    try {
      mkdirSync(dirname(sub), { recursive: true })
      copyFileSync(src, sub)
      done.push(sub)
      // BepInEx 对同名插件只加载一份：老安装常在 plugins 根目录留副本，那份优先，
      // 不同步就会出现"改了等于没改"（deploy_mod.ps1:40-55 记过这个坑）。
      if (existsSync(root)) { copyFileSync(src, root); done.push(root) }
    } catch (e) {
      return `复制失败：${e?.message ?? e}\n常见原因：游戏正开着（DLL 被占用，先关游戏）、或没有写权限（用管理员身份重开 DSH）。`
    }
    return `已装到：\n${done.join('\n')}\n重启游戏后 18082 才生效；用 dd2_status 看游戏是否可达。`
  }

  // ---- agent 工具：三个就够（每个工具的定义都进每步上下文，能少则少）----
  const defTool = (o) => ({
    ...o,
    parameters: { type: 'object', properties: o.parameters || {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render: (_args, v) => [{ type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v, null, 2) }],
    },
  })

  try {
    ctx.tools.register(defTool({
      name: 'dd2_status',
      description: 'DD2 挂机的当前状态（游戏模式/场景/马车/金币、战斗回合与敌人、托管进程、runner/watchdog 锁、launcher 在线）。'
        + '要"看一眼"先调这个，不要读 report.json / 日志文件。',
      parameters: {},
      async execute() {
        try { return (await statusText()).text } catch (e) { return `读状态失败：${e?.message ?? e}` }
      },
    }))

    ctx.tools.register(defTool({
      name: 'dd2_run',
      description: '启停 DD2 挂机。start=起 watchdog（团灭/减员自动重开，长期挂机用这个）；'
        + 'runner=只跑一段 runner；stop=停掉本机所有 runner/watchdog（游戏本身不动）；'
        + 'restart=放弃本局并重开（破坏性，会丢当前进度）。',
      parameters: {
        action: { type: 'string', enum: ['start', 'runner', 'stop', 'restart'], description: '要执行的动作' },
        hours: { type: 'number', description: 'start 跑几小时（默认 4）；runner 是分钟数（默认 60）' },
      },
      async execute(args) {
        const a = String(args?.action || 'start').toLowerCase()
        const h = Number(args?.hours)
        try {
          if (a === 'start') return start('watchdog', h > 0 ? h : defaultHours).text
          if (a === 'runner') return start('runner', (h > 0 ? h : 60) / 60).text
          if (a === 'stop') return (await stop()).text
          if (a === 'restart') return (await restartRun()).text
          return `未知动作 ${a}（可选 start / runner / stop / restart）`
        } catch (e) { return `执行失败：${e?.message ?? e}` }
      },
    }))

    ctx.tools.register(defTool({
      name: 'dd2_install',
      description: '把本插件自带的 DD2TextMod.dll 装进游戏的 BepInEx\\plugins，并报告 BepInEx 是否已安装、装到了哪。装完需重启游戏才生效。',
      parameters: {},
      async execute() {
        try { return installDll() } catch (e) { return `安装失败：${e?.message ?? e}` }
      },
    }))
  } catch {
    // 工具注册失败不影响面板；面板本身还能用
  }

  ctx.effect(() => ctx.commands.register({
    name: 'dd2',
    description: 'DD2 挂机面板：status / start [hours] / runner / stop / restart / quitgame / install',
    input: { hint: 'status | start [hours] | runner | stop | restart | quitgame | install' },
    handler: async ({ rawInput }) => {
      const [verb = 'status', arg] = String(rawInput || '').trim().split(/\s+/)
      if (verb === 'status') return await statusText()
      if (verb === 'start') return { kind: 'success', text: start('watchdog', Number(arg) > 0 ? Number(arg) : defaultHours).text }
      if (verb === 'runner') return { kind: 'success', text: start('runner', Number(arg) > 0 ? Number(arg) : 1).text }
      if (verb === 'stop') return { kind: 'success', text: (await stop()).text }
      if (verb === 'install') return { kind: 'success', text: installDll() }
      if (verb === 'restart') { const r = await restartRun(); return { kind: r.ok ? 'success' : 'error', text: r.text } }
      if (verb === 'quitgame') { const r = await quitGame(); return { kind: r.ok ? 'success' : 'error', text: r.text } }
      return { kind: 'error', text: `未知子命令 ${verb}` }
    },
  }))

  const isLoopbackOrigin = (o) => !o || /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(String(o))

  // 内部桥：浏览器 → 宿主（绕开 commands remote 的 sessionId 要求）。
  // 动作必须带 token；token 只发给 loopback 页面（防别的网页/进程顺手点）。
  const server = createServer(async (req, res) => {
    const cors = {
      'access-control-allow-origin': '*',
      'access-control-allow-methods': 'GET,POST,OPTIONS',
      'access-control-allow-headers': 'content-type,x-dd2-token',
      'cache-control': 'no-store',
    }
    const send = (code, obj) => {
      res.writeHead(code, { ...cors, 'content-type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify(obj))
    }
    try {
      const u = new URL(req.url || '/', `http://127.0.0.1:${BRIDGE_PORT}`)
      if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return }
      if (req.method === 'GET' && u.pathname === '/state') {
        return send(200, await snapshot(u.searchParams.get('fresh') === '1'))
      }
      if (req.method === 'GET' && u.pathname === '/token') {
        bridgeLog(workdir, `token origin=${req.headers.origin ?? '(none)'} ua=${String(req.headers['user-agent'] || '').slice(0, 40)}`)
        return send(200, { ok: true, token })
      }
      if (req.method === 'POST' && u.pathname === '/act') {
        if (req.headers['x-dd2-token'] !== token) {
          bridgeLog(workdir, `act 403 do=${u.searchParams.get('do')} origin=${req.headers.origin ?? '(none)'} hasHeader=${!!req.headers['x-dd2-token']}`)
          return send(403, { ok: false, text: 'token 不对（页面需先取 /token）' })
        }
        const r = await act(u.searchParams.get('do') || '')
        return send(200, { ...r, snapshot: await snapshot(true) })
      }
      return send(404, { ok: false, error: 'not found' })
    } catch (e) {
      return send(500, { ok: false, error: String(e?.message ?? e) })
    }
  })
  server.on('error', (e) => { try { console.error('[dd2-panel] bridge error', e?.message ?? e) } catch {} })
  server.listen(BRIDGE_PORT, '127.0.0.1')

  const monitorTimer = setInterval(() => { monitorTick().catch(() => {}) }, 5000)
  if (typeof monitorTimer.unref === 'function') monitorTimer.unref()

  ctx.effect(() => () => {
    try { clearInterval(monitorTimer) } catch {}
    try { server.close() } catch {}
    stop()
    if (launcherChild) { try { execFile('taskkill', ['/PID', String(launcherChild.pid), '/T', '/F'], { windowsHide: true }, () => {}) } catch {}; launcherChild = null }
  })
}
