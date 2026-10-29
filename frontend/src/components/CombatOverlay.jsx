import { useEffect, useState } from 'react'
import { Swords, Shield, Footprints, Loader2 } from 'lucide-react'

/**
 * Combat HUD. It only *displays* the rules engine's verdict: every button sends
 * a structured action to /api/v1/campaigns/{id}/combat, and the returned
 * combat_round is what the panel renders. The LLM never decides the outcome.
 */
export default function CombatOverlay({ active, campaignId, onResolved, onNarrate }) {
  const [visible, setVisible] = useState(false)
  const [busy, setBusy] = useState(null)
  const [round, setRound] = useState(null)
  const [error, setError] = useState('')
  const [target, setTarget] = useState(null)

  useEffect(() => {
    if (active) {
      setVisible(true)
    } else {
      const timer = setTimeout(() => setVisible(false), 500)
      return () => clearTimeout(timer)
    }
  }, [active])

  useEffect(() => {
    if (!campaignId || !active) return
    let cancelled = false
    import('../api')
      .then(({ fetchCampaignEngineState }) => fetchCampaignEngineState(campaignId))
      .then((state) => {
        if (cancelled || !state) return
        const targetId = state.combat?.target_id
        const combatant = targetId ? state.combatants?.[targetId] : null
        setTarget(targetId ? { id: targetId, ...combatant } : null)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [campaignId, active])

  const act = async (action) => {
    if (!campaignId || busy) return
    setBusy(action)
    setError('')
    try {
      const { runCombatAction } = await import('../api')
      const result = await runCombatAction(campaignId, {
        action,
        targetId: target?.id || null,
        narrate: true,
      })
      if (result.status === 'rejected') {
        setError(result.message || '行动被规则拒绝。')
      } else {
        setRound(result.combat_round || null)
        if (result.narrative?.text && onNarrate) onNarrate(result.narrative.text)
        if (onResolved) onResolved(result)
      }
    } catch (err) {
      setError(err?.detail || err?.message || '战斗行动失败。')
    } finally {
      setBusy(null)
    }
  }

  if (!visible) return null

  const hpText = round
    ? `${round.target_hp_before ?? '?'} → ${round.target_hp_after ?? '?'}`
    : target
      ? `${target.hp} / ${target.max_hp}`
      : null

  return (
    <>
      <div className={`fixed top-0 left-0 right-0 h-1 z-40 transition-all duration-500 ${active ? 'opacity-100' : 'opacity-0'}`}>
        <div className="h-full bg-gradient-to-r from-transparent via-rose-500 to-transparent animate-pulse" />
      </div>
      <div className={`fixed bottom-0 left-0 right-0 h-1 z-40 transition-all duration-500 ${active ? 'opacity-100' : 'opacity-0'}`}>
        <div className="h-full bg-gradient-to-r from-transparent via-rose-500 to-transparent animate-pulse" />
      </div>

      <div className={`fixed top-16 left-1/2 -translate-x-1/2 z-50 transition-all duration-500 ${active ? 'opacity-100 translate-y-0' : 'opacity-0 -translate-y-4'}`}>
        <div className="flex flex-col gap-2 px-3 py-2 rounded-2xl bg-rose-950/85 backdrop-blur-xl border border-rose-500/40 shadow-[0_0_20px_rgba(244,63,94,0.3)]">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 bg-rose-400 rounded-full animate-pulse shadow-[0_0_8px_rgba(244,63,94,0.8)]" />
            <span className="text-rose-300 text-[10px] font-bold uppercase tracking-[0.2em]">战斗中</span>
            {target && (
              <span className="text-rose-100/80 text-[11px] font-mono">
                {target.name || target.id}
                {hpText ? ` · HP ${hpText}` : ''}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            {[
              { id: 'attack', label: '攻击', Icon: Swords },
              { id: 'defend', label: '防御', Icon: Shield },
              { id: 'flee', label: '逃跑', Icon: Footprints },
            ].map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                disabled={!!busy}
                onClick={() => act(id)}
                className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-bold text-rose-100 bg-rose-500/20 hover:bg-rose-500/35 disabled:opacity-40 border border-rose-400/30 transition-colors"
              >
                {busy === id ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
                {label}
              </button>
            ))}
          </div>
          {error && <div className="text-[10px] text-rose-200/90 max-w-[260px]">{error}</div>}
          {round && (
            <div className="text-[10px] font-mono text-rose-100/70">
              第 {round.round} 回合 · {round.code || (round.target_alive ? '命中' : '击败')}
            </div>
          )}
        </div>
      </div>

      <div
        className={`fixed inset-0 pointer-events-none z-30 transition-opacity duration-500 ${active ? 'opacity-100' : 'opacity-0'}`}
        style={{ background: 'radial-gradient(ellipse at center, transparent 50%, rgba(159,18,57,0.08) 100%)' }}
      />
    </>
  )
}
