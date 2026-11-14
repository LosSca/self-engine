import { Pencil, ArrowUp } from 'lucide-react'

/**
 * Next-action options offered after a turn. Each option is sent as-is or
 * loaded into the input to edit.
 *
 * A suggestion may name the character it addresses (`target`, `target_id`).
 * The pick is handed back to the caller so the turn can be bound to that
 * character instead of relying on the prose being parsed back into a name.
 */
export default function SuggestionList({ suggestions, disabled, onSend, onEdit }) {
  if (!suggestions?.length) return null

  const toInput = (s) => (s.speech ? `[SAY] ${s.speech}` : `[DO] ${s.action}`)
  const toTarget = (s) => (s?.target ? { id: s.target_id || '', name: s.target } : null)

  return (
    <div className="flex flex-col gap-2 mb-3">
      {suggestions.map((s, i) => (
        <div
          key={`${s.action}-${s.speech}-${i}`}
          className="group flex items-center gap-2 rounded-xl border border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.05] transition-colors"
          data-testid="suggestion-row"
        >
          <button
            type="button"
            disabled={disabled}
            onClick={() => onEdit?.(toInput(s), toTarget(s))}
            title="发送前编辑"
            className="p-2.5 text-white/25 hover:text-white/70 disabled:opacity-30 transition-colors"
          >
            <Pencil size={14} />
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onSend?.(toInput(s), toTarget(s))}
            className="flex-1 text-left py-2.5 pr-1 text-sm font-light leading-relaxed disabled:opacity-40"
          >
            {s.target && (
              <span className="mr-1.5 rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 text-[11px] text-cyan-100">
                {s.target}
              </span>
            )}
            {s.action && <span className="text-white/40 italic mr-1.5">{s.action}</span>}
            {s.speech && <span className="text-white">&ldquo;{s.speech}&rdquo;</span>}
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onSend?.(toInput(s), toTarget(s))}
            title="发送"
            className="p-2.5 text-white/25 group-hover:text-white/70 disabled:opacity-30 transition-colors"
          >
            <ArrowUp size={14} />
          </button>
        </div>
      ))}
    </div>
  )
}