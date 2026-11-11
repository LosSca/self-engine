import { useState, useEffect } from 'react'
import { BookOpen, RefreshCw, X } from 'lucide-react'

const CATEGORY_LABELS = {
  DISCOVERY: '发现',
  COMBAT: '战斗',
  DECISION: '抉择',
  RELATIONSHIP_CHANGE: '关系',
  WORLD_EVENT: '世界',
}

const CATEGORIES = [
  { id: null, label: '全部' },
  { id: 'DISCOVERY', label: '发现' },
  { id: 'COMBAT', label: '战斗' },
  { id: 'DECISION', label: '抉择' },
  { id: 'RELATIONSHIP_CHANGE', label: '关系' },
  { id: 'WORLD_EVENT', label: '世界' },
]

const CATEGORY_COLORS = {
  DISCOVERY: 'text-amber-400 bg-amber-400/10 border-amber-400/30',
  COMBAT: 'text-rose-400 bg-rose-400/10 border-rose-400/30',
  DECISION: 'text-white bg-white/10 border-white/20',
  RELATIONSHIP_CHANGE: 'text-emerald-400 bg-emerald-400/10 border-emerald-400/30',
  WORLD_EVENT: 'text-white bg-white/10 border-white/20',
}

export default function JournalPanel({ open, onClose, entries, onRefresh }) {
  const [filter, setFilter] = useState(null)

  // Auto-refresh when panel opens with no entries (fixes stale data on first open)
  useEffect(() => {
    if (open && entries.length === 0 && onRefresh) {
      onRefresh()
    }
  }, [open])

  if (!open) return null

  const filtered = filter
    ? entries.filter((e) => e.category === filter)
    : entries

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="bg-white/[0.03] border border-white/10 rounded-2xl shadow-[0_0_40px_rgba(255,255,255,0.15)] w-full max-w-[33rem] mx-2 sm:mx-4 max-h-[85vh] sm:max-h-[70vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
          <div className="flex items-center gap-3">
            <BookOpen size={16} className="text-yellow-400" />
            <h2 className="text-white font-bold text-sm uppercase tracking-widest flex items-center gap-2">
              任务日志
              {entries.length > 0 && (
                <span className="text-white/40 font-normal text-xs">{entries.length}</span>
              )}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onRefresh}
              title="刷新"
              aria-label="刷新日志"
              className="text-white/40 hover:text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/50"
            >
              <RefreshCw size={14} />
            </button>
            <button onClick={onClose} aria-label="关闭日志" className="text-white/40 hover:text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/50">
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Category filters */}
        <div className="flex flex-wrap gap-1.5 px-6 py-3 border-b border-white/5">
          {CATEGORIES.map((c) => (
            <button
              key={c.id ?? 'all'}
              onClick={() => setFilter(c.id)}
              className={`px-3 py-1 rounded-lg text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 whitespace-nowrap transition-all
                ${filter === c.id
                  ? 'bg-white/10 text-white border border-white/20'
                  : 'text-white/20 hover:text-white/60 hover:bg-white/5 border border-transparent'
                }`}
            >
              {c.label}
            </button>
          ))}
        </div>

        {/* Entries */}
        <div className="flex-1 overflow-y-auto custom-scrollbar p-4 space-y-2">
          {filtered.length === 0 ? (
            <p className="text-white/40 text-sm font-light text-center py-8">暂无记录。</p>
          ) : (
            [...filtered].reverse().map((entry, i) => (
              <div key={i} className="p-3.5 rounded-xl bg-white/[0.03] border border-white/5">
                <div className="flex items-center gap-2 mb-2">
                  <span className={`text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 px-2 py-0.5 rounded border ${CATEGORY_COLORS[entry.category] || 'text-white/40 bg-white/[0.03] border-white/5'}`}>
                    {CATEGORY_LABELS[entry.category] || '日志'}
                  </span>
                  <span className="text-[10px] text-white/20 font-mono">
                    {entry.created_at?.slice(11, 19) || ''}
                  </span>
                </div>
                <p className="text-white text-sm font-light leading-relaxed">
                  {entry.summary}
                </p>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
