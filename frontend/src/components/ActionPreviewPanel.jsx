import { useEffect, useMemo, useState } from 'react'
import { fetchActionCatalogue, previewAction } from '../api'

// Dry run for authors and players: ask the rules "would this work?" without
// spending a request id or touching the campaign. The answer comes from the
// same RulesEngine the real turn uses, so the preview can never disagree with
// what actually happens next (unless the state changes in between).
export default function ActionPreviewPanel({ open, onClose, campaignId }) {
  const [catalogue, setCatalogue] = useState([])
  const [action, setAction] = useState('')
  const [targetId, setTargetId] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setError('')
    fetchActionCatalogue()
      .then((rows) => {
        if (cancelled) return
        setCatalogue(rows || [])
        setAction((current) => current || (rows && rows[0] ? rows[0].name : ''))
      })
      .catch((err) => { if (!cancelled) setError(err.message || '行动目录读取失败') })
    return () => { cancelled = true }
  }, [open])

  const spec = useMemo(
    () => catalogue.find((row) => row.name === action) || null,
    [catalogue, action],
  )

  if (!open) return null

  const run = async () => {
    if (!campaignId || !action || busy) return
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const body = await previewAction(campaignId, {
        action,
        targetId: targetId.trim() ? targetId.trim() : null,
      })
      setResult(body)
    } catch (err) {
      setError(err.message || '预演失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="bg-white/[0.03] border border-white/10 rounded-2xl shadow-[0_0_40px_rgba(255,255,255,0.15)] w-full max-w-lg mx-2 sm:mx-4 max-h-[85vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
          <div>
            <h2 className="text-white font-bold text-base tracking-wide">行动预演</h2>
            <p className="text-[11px] text-white/35 mt-1">规则判定，不提交：不消耗 request_id，也不改变存档</p>
          </div>
          <button onClick={onClose} aria-label="关闭预演" className="text-white/40 hover:text-white transition-colors">✕</button>
        </div>

        <div className="p-4 flex flex-col gap-3 overflow-y-auto">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] uppercase tracking-[0.2em] text-white/40">行动</span>
            <select
              value={action}
              onChange={(event) => { setAction(event.target.value); setResult(null) }}
              className="bg-black/40 border border-white/10 rounded-xl px-3 py-2 text-sm text-white"
            >
              {catalogue.map((row) => (
                <option key={row.name} value={row.name}>
                  {row.label || row.name}（{row.name}）
                </option>
              ))}
            </select>
          </label>

          {spec && spec.description && (
            <p className="text-[11px] text-white/35">{spec.description}</p>
          )}

          <label className="flex flex-col gap-1">
            <span className="text-[11px] uppercase tracking-[0.2em] text-white/40">
              目标 id {spec ? `（${spec.target === 'required' ? '必填' : spec.target === 'optional' ? '可选' : '不需要'}）` : ''}
            </span>
            <input
              value={targetId}
              onChange={(event) => setTargetId(event.target.value)}
              placeholder="例如 iron_key / warehouse_door / forest_edge"
              className="bg-black/40 border border-white/10 rounded-xl px-3 py-2 text-sm text-white placeholder:text-white/20"
            />
          </label>

          <button
            onClick={run}
            disabled={busy || !action}
            className="self-start px-4 py-2 rounded-xl bg-cyan-400 text-black text-xs font-bold hover:bg-cyan-300 disabled:opacity-40"
          >
            {busy ? '判定中…' : '预演这次行动'}
          </button>

          {error && <p className="text-xs text-rose-400">{error}</p>}

          {result && (
            <div className="rounded-xl border border-white/10 p-3 flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <span className={`text-[10px] px-2 py-0.5 rounded-full border ${result.ok ? 'text-emerald-300 border-emerald-400/40 bg-emerald-400/10' : 'text-rose-300 border-rose-400/40 bg-rose-400/10'}`}>
                  {result.ok ? '允许' : '拒绝'}
                </span>
                {result.code && <span className="text-[10px] font-mono text-white/40">{result.code}</span>}
                <span className="text-[10px] text-white/30 ml-auto">状态版本 v{result.state_version}</span>
              </div>
              {result.message && <p className="text-xs text-white/60">{result.message}</p>}
              {result.effects && result.effects.length > 0 && (
                <div className="text-[11px] text-white/45">
                  <span className="text-white/30">将产生的效果：</span>
                  {result.effects.map((effect, index) => (
                    <div key={index} className="font-mono">
                      {effect.kind}
                      {effect.target ? ` → ${effect.target}` : ''}
                      {effect.amount != null ? ` × ${effect.amount}` : ''}
                    </div>
                  ))}
                </div>
              )}
              {result.ok && (!result.effects || result.effects.length === 0) && (
                <p className="text-[11px] text-white/30">这条行动只产生描述，不会改变状态。</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
