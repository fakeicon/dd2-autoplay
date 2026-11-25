/**
 * DD2 挂机面板 —— Client 半边（v3）。
 * 整页挂在 main 槽（key=dd2）、侧栏入口挂 sidebar.panellist；
 * 数据全走宿主 127.0.0.1:18084 的桥（commands remote 需要 sessionId，全局页面拿不到）。
 * 颜色一律走 DSH 主题 token（--dsw-alias-*），取不到就退化到中性灰。
 */
window.__ModuleLoader__.load({
  id: 'dsh-dd2-autoplay',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const BRIDGE = 'http://127.0.0.1:18084'
    const POLL_MS = 5000

    const T = {
      fg: 'var(--dsw-alias-text-primary, #e8e8e8)',
      muted: 'var(--dsw-alias-text-secondary, rgba(232,232,232,.62))',
      surface: 'var(--dsw-alias-bg-secondary, rgba(128,128,128,.08))',
      surface2: 'var(--dsw-alias-bg-tertiary, rgba(128,128,128,.14))',
      border: '1px solid var(--dsw-alias-border, rgba(128,128,128,.26))',
      accent: 'var(--dsw-alias-accent, #c9a227)',
    }

    async function readState(fresh) {
      const r = await fetch(`${BRIDGE}/state${fresh ? '?fresh=1' : ''}`, { cache: 'no-store' })
      return await r.json()
    }
    async function readToken() {
      const r = await fetch(`${BRIDGE}/token`, { cache: 'no-store' })
      const j = await r.json()
      return j && j.token ? j.token : ''
    }
    async function act(doWhat, token) {
      const r = await fetch(`${BRIDGE}/act?do=${encodeURIComponent(doWhat)}`, {
        method: 'POST',
        headers: { 'x-dd2-token': token || '' },
      })
      return await r.json()
    }

    const btn = (primary) => ({
      padding: '5px 13px', borderRadius: 8, cursor: 'pointer', fontSize: 13,
      border: primary ? '1px solid transparent' : T.border,
      background: primary ? 'var(--dsw-alias-accent, #c9a227)' : 'transparent',
      color: primary ? 'var(--dsw-alias-on-accent, #1a1a1a)' : 'inherit',
      opacity: 1,
    })
    const chip = {
      display: 'inline-block', padding: '1px 8px', margin: '2px 4px 2px 0', borderRadius: 999,
      background: T.surface2, border: T.border, fontSize: 12, whiteSpace: 'nowrap',
    }

    function Pill(props) {
      const tone = props.tone || 'ok'
      const map = {
        ok: ['rgba(76,175,110,.16)', 'rgba(76,175,110,.55)', '#6fce8f'],
        warn: ['rgba(224,163,58,.14)', 'rgba(224,163,58,.55)', '#e0a33a'],
        bad: ['rgba(217,83,79,.14)', 'rgba(217,83,79,.55)', '#e5776f'],
        calm: [T.surface, 'transparent', T.muted],
      }
      const c = map[tone] || map.calm
      return h('span', {
        style: {
          display: 'inline-flex', alignItems: 'center', gap: 6, padding: '2px 10px',
          borderRadius: 999, fontSize: 12, background: c[0], border: `1px solid ${c[1]}`, color: c[2],
        },
      }, [h('span', { key: 'd', style: { width: 6, height: 6, borderRadius: 999, background: c[2], display: 'inline-block' } }), props.children])
    }

    function Bar(props) {
      const pct = Math.max(0, Math.min(100, Number(props.pct) || 0))
      const color = props.tone === 'bad' ? '#d05353' : props.tone === 'warn' ? '#d9a441' : '#5aa86a'
      return h('div', { style: { height: 6, borderRadius: 999, background: T.surface2, overflow: 'hidden' } },
        h('div', { style: { width: `${pct}%`, height: '100%', background: color, borderRadius: 999 } }))
    }

    const chips = (list, emptyText, color) => (list && list.length)
      ? list.map((it) => h('span', { key: it.id, style: { ...chip, color: color || undefined }, title: it.id }, it.zh || it.id))
      : h('span', { style: { opacity: .5, fontSize: 12 } }, emptyText)

    function HeroCard(p) {
      const hpTone = p.hpPct == null ? 'warn' : p.hpPct <= 25 ? 'bad' : p.hpPct <= 55 ? 'warn' : 'ok'
      const stress = p.stress == null ? null : Number(p.stress) || 0
      return h('div', { style: { border: T.border, borderRadius: 12, padding: '12px 14px', background: T.surface } },
        h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' } },
          h('span', { style: { fontWeight: 650, fontSize: 15 } }, p.name || '(无 actor)'),
          h('span', { style: { fontSize: 12, color: T.muted } }, p.classZh),
          p.deathsDoor ? h(Pill, { tone: 'bad' }, '死门') : null,
          !p.alive ? h(Pill, { tone: 'bad' }, '已阵亡') : null,
          h('span', { style: { marginLeft: 'auto', fontSize: 12, color: T.muted } },
            `道途 ${p.pathZh}`),
        ),
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 } },
          h('div', { style: { flex: 1 } }, h(Bar, { pct: p.hpPct, tone: hpTone })),
          h('span', { style: { fontSize: 12, minWidth: 74, textAlign: 'right', fontVariantNumeric: 'tabular-nums' } },
            `HP ${p.hp ?? '?'}/${p.hpMax ?? '?'}`),
        ),
        stress === null ? null : h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 } },
          h('div', { style: { flex: 1 } }, h(Bar, { pct: stress * 10, tone: stress >= 8 ? 'bad' : stress >= 5 ? 'warn' : 'ok' })),
          h('span', { style: { fontSize: 12, minWidth: 74, textAlign: 'right', color: T.muted } }, `压力 ${stress}`),
        ),
        p.tokens && p.tokens.length
          ? h('div', { style: { marginTop: 8, fontSize: 12 } }, [h('span', { key: 'l', style: { color: T.muted } }, '状态 '), chips(p.tokens, '')])
          : null,
        h('div', { style: { marginTop: 8, fontSize: 12 } }, [
          h('span', { key: 'l', style: { color: T.muted } },
            `技能 ${p.skillCount ? `${p.skillCount.equipped}/${p.skillCount.unlocked}` : ''} `),
          chips(p.skills, '（未读到）'),
        ]),
        h('div', { style: { marginTop: 6, fontSize: 12 } }, [
          h('span', { key: 'l', style: { color: T.muted } },
            `饰品 ${p.trinkets ? p.trinkets.length : 0}/${p.trinketSlots ?? '?'} `),
          chips(p.trinkets, '空', p.trinkets && p.trinkets.length ? undefined : T.muted),
        ]),
      )
    }

    function CombatCard(c) {
      return h('div', { style: { border: T.border, borderRadius: 12, padding: '12px 14px', background: T.surface, marginTop: 12 } },
        h('div', { style: { display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' } },
          h('span', { style: { fontWeight: 650 } }, '战斗'),
          h(Pill, { tone: 'calm' }, `第 ${c.round ?? '?'} 回合`),
          c.currentActor && (c.currentActor.zh || c.currentActor.name)
            ? h(Pill, { tone: 'calm' }, `当前 ${c.currentActor.zh || c.currentActor.name}`) : null,
          h(Pill, { tone: c.enemiesAlive ? 'warn' : 'ok' }, `敌人 ${c.enemiesAlive}/${c.enemies.length}`),
          h('span', { style: { fontSize: 12, color: T.muted } }, c.battleState || ''),
        ),
        h('div', { style: { marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' } },
          c.enemies.map((e, i) => h('span', {
            key: `${e.dataId}_${i}`,
            style: {
              ...chip,
              opacity: e.alive ? 1 : .45,
              textDecoration: e.alive ? 'none' : 'line-through',
              borderColor: e.deathsDoor ? 'rgba(217,83,79,.5)' : undefined,
            },
            title: e.dataId,
          }, `${e.zh} ${e.hp ?? '?'}/${e.hpMax ?? '?'}${e.deathsDoor ? ' 死门' : ''}`)),
        ),
      )
    }

    function summaryText(st) {
      if (!st) return '（还未读到状态）'
      const b = st.brief || {}
      const lines = []
      lines.push(`DD2 ${b.mode || '?'}/${b.scene || '?'} row=${b.row ?? '?'} gold=${b.gold ?? '?'}`)
      if (st.combat) lines.push(`战斗 第${st.combat.round}回合 敌${st.combat.enemiesAlive}/${st.combat.enemies.length}`)
      for (const p of st.party || []) {
        lines.push(`${p.name}(${p.classZh}) ${p.hp}/${p.hpMax} 压力${p.stress ?? '?'} ${p.pathZh} 饰品${(p.trinkets || []).length}/${p.trinketSlots ?? '?'}`)
      }
      if (st.warnings && st.warnings.length) lines.push('⚠ ' + st.warnings.map((w) => w.text).join(' / '))
      return lines.join('\n')
    }

    function Body() {
      const [st, setSt] = React.useState(null)
      const [tok, setTok] = React.useState('')
      const [err, setErr] = React.useState('')
      const [busy, setBusy] = React.useState('')
      const [note, setNote] = React.useState('')
      const tokRef = React.useRef('')

      const refresh = React.useCallback(async (fresh) => {
        try { setSt(await readState(fresh)); setErr('') } catch (e) { setErr(String((e && e.message) || e)) }
      }, [])

      // token 只在首次动作前才要；插件热重载后旧 token 会失效，遇 403 就重取一次再试。
      // 注意：必须定义在下面两个 useEffect 之前，否则依赖数组会触发 TDZ（页面直接白屏）。
      const ensureToken = React.useCallback(async (force) => {
        if (!force && tokRef.current) return tokRef.current
        try {
          const t = await readToken()
          if (t) { tokRef.current = t; setTok(t) }
        } catch {}
        return tokRef.current
      }, [])

      React.useEffect(() => {
        let stop = false
        ;(async () => {
          // 插件刚热重载时桥可能还没开始 listen：多试几次，别让页面带着空 token 活着
          for (let i = 0; i < 5 && !stop; i++) {
            const t = await readToken().catch(() => '')
            if (t) { tokRef.current = t; setTok(t); break }
            await new Promise((r) => setTimeout(r, 400))
          }
        })()
        refresh(true)
        return () => { stop = true }
      }, [refresh])

      React.useEffect(() => {
        const tick = () => { if (!document.hidden && !busy) refresh(false) }
        const t = setInterval(tick, POLL_MS)
        const onVis = () => { if (!document.hidden) { refresh(true); if (!tokRef.current) ensureToken(true) } }
        document.addEventListener('visibilitychange', onVis)
        return () => { clearInterval(t); document.removeEventListener('visibilitychange', onVis) }
      }, [refresh, busy, ensureToken])

      const fire = React.useCallback(async (doWhat, confirmText) => {
        if (confirmText && !window.confirm(confirmText)) return
        setBusy(doWhat); setNote('')
        try {
          let t = await ensureToken(false)
          let r = await act(doWhat, t)
          if (r && r.ok === false && /token/i.test(String(r.text || r.error || ''))) {
            t = await ensureToken(true)
            r = await act(doWhat, t)
          }
          if (r && r.snapshot) setSt(r.snapshot)
          else await refresh(true)
          const ok = !(r && r.ok === false)
          setNote(ok ? String((r && r.text) || '完成') : '')
          setErr(ok ? '' : String((r && (r.text || r.error)) || '失败'))
        } catch (e) { setErr(String((e && e.message) || e)) }
        finally { setBusy('') }
      }, [refresh, ensureToken])

      const copy = React.useCallback(async () => {
        const txt = summaryText(st)
        try { await navigator.clipboard.writeText(txt); setNote('已复制摘要，可直接粘给 AI') }
        catch { setNote(txt) }
      }, [st])

      const b = (label, doWhat, primary, confirmText) => h('button', {
        key: doWhat, style: { ...btn(primary), opacity: busy && busy !== doWhat ? .6 : 1 },
        disabled: !!busy,
        onClick: (e) => { e.stopPropagation(); fire(doWhat, confirmText) },
      }, busy === doWhat ? '处理中…' : label)

      const brief = (st && st.brief) || {}
      const online = !!(st && st.ok)
      const head = st
        ? (online
          ? `${brief.mode || '?'} / ${brief.scene || '?'}`
          : `游戏不可达：${brief.error || '无响应'}`)
        : '读取中…'
      const tv = st && st.travel
      const tvZh = tv ? ({ moving: '顺畅前进', waiting: '等按钮', spinning: '空转', jerky: '来回蹭', stalled: '停着' }[tv.state] || null) : null
      const travelTxt = tvZh
        ? `跑图 ${tvZh}${tv.stuckSec >= 5 ? ` ${tv.stuckSec}s` : ''}${tv.blockers && tv.blockers.length ? ` · 拦路 ${tv.blockers[0]}` : ''}`
        : null
      const sub = st && online
        ? [`row ${brief.row ?? '?'}`, `gold ${brief.gold ?? '?'}`, brief.speed != null ? `速度 ${brief.speed}` : null,
          travelTxt,
          st.repair && st.repair.stuckSec >= 5 && !travelTxt ? `马车停 ${st.repair.stuckSec}s` : null].filter(Boolean).join(' · ')
        : ''
      const local = st && st.local ? `${st.local.kind} pid ${st.local.pid}` : '本插件未托管进程'
      const locks = st ? `runner ${st.runnerLock && st.runnerLock.free ? 'FREE' : 'BUSY'} · watchdog ${st.watchdogLock && st.watchdogLock.free ? 'FREE' : 'BUSY'}` : ''
      const launcher = st && st.launcher ? (st.launcher.online ? 'launcher 在线' : 'launcher 离线') : ''

      return h('div', null,
        h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
          h('button', {
            key: 'refresh', style: btn(false),
            onClick: (e) => { e.stopPropagation(); refresh(true) },
          }, '刷新'),
          b('开始挂机', 'start', true),
          b('停止挂机', 'stop', false, '停掉本插件托管的挂机进程？游戏本身不动。'),
          b('放弃本局并重开', 'restart', false, '放弃本局会让本局以失败结算，然后在祭坛重开。继续？'),
          b('关闭游戏', 'quitgame', false, '通过 launcher(18083) 关闭游戏？'),
          h('button', {
            key: 'copy', style: btn(false),
            onClick: (e) => { e.stopPropagation(); copy() },
          }, '复制摘要'),
          busy ? h('span', { key: 'busy', style: { fontSize: 12, color: T.muted } }, '…') : null,
        ),
        h('div', { style: { display: 'flex', gap: 10, alignItems: 'center', marginTop: 14, flexWrap: 'wrap' } },
          h(Pill, { tone: online ? 'ok' : 'bad' }, head),
          sub ? h('span', { style: { fontSize: 13, color: T.muted } }, sub) : null,
        ),
        h('div', { style: { marginTop: 8, fontSize: 12, color: T.muted } },
          [local, locks, launcher, st && st.bag != null ? `饰品背包 ${st.bag}` : ''].filter(Boolean).join('  ·  ')),
        (st && st.warnings || []).map((w, i) => h('div', {
          key: `w${i}`,
          style: {
            marginTop: 8, padding: '8px 12px', borderRadius: 10, fontSize: 13,
            background: w.level === 'red' ? 'rgba(217,83,79,.12)' : 'rgba(224,163,58,.12)',
            border: `1px solid ${w.level === 'red' ? 'rgba(217,83,79,.4)' : 'rgba(224,163,58,.4)'}`,
          },
        }, w.text)),
        note ? h('div', { style: { marginTop: 8, fontSize: 12, color: T.muted } }, note) : null,
        err ? h('div', { style: { marginTop: 8, fontSize: 12, color: '#e5776f' } }, err) : null,
        st && st.combat ? h(CombatCard, st.combat) : null,
        st && st.party && st.party.length
          ? h('div', {
            style: { marginTop: 14, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 12 },
          }, st.party.map((p) => h(HeroCard, { key: p.guid, ...p })))
          : h('div', { style: { marginTop: 12, fontSize: 12, color: T.muted } },
            '队伍未读到（游戏里没有可读名册时正常）'),
        h('div', { style: { marginTop: 16, fontSize: 12, color: T.muted } },
          st ? `更新于 ${new Date(st.at).toLocaleTimeString()} · 数据源 18082 · 中文取自游戏 zh_CN.po（${st.i18n} 条）· 缓存 ${st.ttlMs}ms · ui v3.2` : ''),
      )
    }

    function SideIcon() {
      return h('span', {
        style: {
          width: 16, height: 16, borderRadius: 4, display: 'inline-block',
          background: 'linear-gradient(135deg,#c9a227,#7a4b1e)',
        },
      })
    }

    function Page(props) {
      return h('div', { style: { height: '100%', overflow: 'auto', background: 'var(--dsw-alias-bg, transparent)' } },
        h('div', { style: { maxWidth: 1000, margin: '0 auto', padding: '26px 32px 48px' } },
          h('div', { style: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 } },
            h('h1', { style: { margin: 0, fontSize: 22, fontWeight: 650 } }, 'DD2 挂机'),
            h('span', { style: { fontSize: 12, color: T.muted } }, '游戏 18082 · 开关游戏 18083 · 开游戏归人'),
          ),
          h('div', { style: { margin: '6px 0 16px', fontSize: 13, color: T.muted } },
            '启停只作用于 watchdog/runner 子进程，不碰游戏本体；开游戏仍归人。'),
          h(Body, { ctx: props.ctx }),
        ),
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'dd2' },
          (slotProps) => h(Page, { ...slotProps, ctx })))

        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist', id: 'dd2', order: 9, label: () => 'DD2 挂机',
        }, SideIcon))
      },
    }
  },
})
