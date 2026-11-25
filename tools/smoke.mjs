// tools\smoke.mjs —— 不进 DSH、不装插件，直接把 index.js 的 apply() 跑起来自检。
//   node tools/smoke.mjs                    # 注册工具 + 调 dd2_status
//   set DD2_SMOKE_INSTALL=1 && node tools/smoke.mjs   # 额外真跑一次 dd2_install（会写游戏目录）
// 用 18085 当桥端口，避免和已装的旧面板抢 18084。
process.env.DD2_BRIDGE_PORT ||= '18085'

const { apply } = await import('../index.js')

const tools = new Map()
let cleaned = null
const ctx = {
  tools: { register: (t) => { tools.set(t.name, t); return () => tools.delete(t.name) } },
  commands: { register: () => () => {} },
  effect: (fn) => { const r = fn(); if (typeof r === 'function' && !cleaned) cleaned = r; return r },
  on: () => () => {},
  logger: { info() {}, warn() {}, error() {} },
}

apply(ctx, {})
console.log('注册的工具 :', [...tools.keys()].join(', ') || '（空）')

for (const need of ['dd2_status', 'dd2_run', 'dd2_install']) {
  if (!tools.has(need)) { console.error('缺工具:', need); process.exit(1) }
}

console.log('\n--- dd2_status ---')
console.log(await tools.get('dd2_status').execute({}))

if (process.env.DD2_SMOKE_INSTALL === '1') {
  console.log('\n--- dd2_install ---')
  console.log(await tools.get('dd2_install').execute({}))
}

console.log('\nSMOKE OK')
process.exit(0)
