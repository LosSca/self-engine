import { useEffect } from 'react'

// Story progress is authored data rendered as-is. This panel reports what the
// deterministic engine already decided; it never changes a node's state.
const KIND_LABELS = { main: '主线', branch: '分支', side: '独立剧情' }

const STATUS_LABELS = {
  entered: '已进入',
  active: '当前',
  pending: '待解锁',
  locked: '未开放',
  disabled: '已停用',
}

function nodeStatus(story, node) {
  if ((story.entered || []).includes(node.id)) {
    return story.active === node.id ? 'active' : 'entered'
  }
  if ((story.pending || []).includes(node.id)) return 'pending'
  if ((story.locked || []).includes(node.id)) return 'locked'
  return 'locked'
}

const statusClass = {
  active: 'text-cyan-300 border-cyan-400/40 bg-cyan-400/10',
  entered: 'text-emerald-300/80 border-emerald-400/30 bg-emerald-400/5',
  pending: 'text-white/50 border-white/15',
  locked: 'text-white/25 border-white/10',
  disabled: 'text-amber-300/60 border-amber-400/20',
}

export default function StoryProgressPanel({ open, onClose, story, engineBound, onRefresh }) {
  useEffect(() => {
    if (open && onRefresh) onRefresh()
  }, [open])

  if (!open) return null

  const storyline = story?.storyline || { name: '', nodes: [], lines: [], packs: [] }
  const nodes = storyline.nodes || []
  const lines = storyline.lines || []
  const packs = storyline.packs || []
  const entered = story?.entered || []
  const armed = story?.armed_packs || []
  const fired = story?.fired_packs || []
  const reasons = story?.reasons || {}

  const byLine = (lineId) => nodes.filter((node) => (node.line || '') === lineId)

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div
        className="w-full max-w-md h-full bg-neutral-950/95 border-l border-white/10 overflow-y-auto p-5 flex flex-col gap-4"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-white font-bold text-sm tracking-wide">剧情进度</h2>
            <p className="text-[11px] text-white/35 mt-1">
              {engineBound ? '由规则引擎按权威状态推进，只读' : '本战役未绑定世界，暂无故事线'}
            </p>
          </div>
          <button onClick={onClose} className="text-white/40 hover:text-white text-xs">
            关闭
          </button>
        </div>

        {!engineBound || !story || story.mode !== 'storyline' ? (
          <p className="text-xs text-white/35">
            这个战役运行在自由模式下，没有预设故事线。可以在故事线工作台里给场景配置主线、分支和事件包。
          </p>
        ) : (
          <>
            <div className="rounded-xl border border-white/10 p-3">
              <div className="text-xs text-white/70 font-bold">{storyline.name || '未命名剧情线'}</div>
              <div className="text-[11px] text-white/35 mt-1">
                {entered.length} / {nodes.length} 节点已进入 · 已启用事件包 {armed.length} · 已触发 {fired.length}
              </div>
            </div>

            {lines.length === 0 && nodes.length === 0 && (
              <p className="text-xs text-white/35">故事线还没有节点。</p>
            )}

            {(lines.length > 0 ? lines : [{ id: '', title: '未分组', kind: '', enabled: true }]).map((line) => {
              const lineNodes = byLine(line.id)
              if (lineNodes.length === 0) return null
              return (
                <div key={line.id || 'ungrouped'} className="flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] uppercase tracking-[0.2em] text-white/40">
                      {line.title || '未分组'}
                    </span>
                    <span className="text-[10px] text-white/25">{KIND_LABELS[line.kind] || ''}</span>
                    {line.enabled === false && <span className="text-[10px] text-amber-300/60">已停用</span>}
                    {line.locked && <span className="text-[10px] text-amber-300/60">🔒</span>}
                  </div>
                  {lineNodes.map((node) => {
                    const status = nodeStatus(story, node)
                    const reason = reasons[node.id]
                    return (
                      <div key={node.id} className="rounded-xl border border-white/10 p-3">
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs text-white/80">{node.title || node.id}</span>
                          <span className={`text-[10px] px-2 py-0.5 rounded-full border ${statusClass[status] || statusClass.locked}`}>
                            {STATUS_LABELS[status]}
                          </span>
                        </div>
                        {node.summary && <p className="text-[11px] text-white/40 mt-1">{node.summary}</p>}
                        {node.requires_event_pack && (
                          <p className="text-[10px] text-amber-300/60 mt-1">需要事件包 {node.requires_event_pack}</p>
                        )}
                        {status !== 'entered' && status !== 'active' && reason && (
                          <p className="text-[10px] text-white/30 mt-1">{reason}</p>
                        )}
                        {status === 'pending' && !reason && (
                          <p className="text-[10px] text-white/30 mt-1">条件尚未满足</p>
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })}

            {packs.length > 0 && (
              <div className="flex flex-col gap-2">
                <span className="text-[11px] uppercase tracking-[0.2em] text-white/40">小事件包</span>
                {packs.map((pack) => (
                  <div key={pack.id} className="rounded-xl border border-white/10 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-white/80">{pack.title || pack.id}</span>
                      <span
                        className={`text-[10px] px-2 py-0.5 rounded-full border ${
                          armed.includes(pack.id)
                            ? 'text-cyan-300 border-cyan-400/40 bg-cyan-400/10'
                            : fired.includes(pack.id)
                              ? 'text-emerald-300/70 border-emerald-400/30'
                              : 'text-white/25 border-white/10'
                        }`}
                      >
                        {armed.includes(pack.id) ? '已启用' : fired.includes(pack.id) ? '已触发' : '未启用'}
                      </span>
                    </div>
                    {pack.summary && <p className="text-[11px] text-white/40 mt-1">{pack.summary}</p>}
                    <p className="text-[10px] text-white/25 mt-1">
                      事件 {(pack.event_ids || []).length} 个 · {pack.once === false ? '可重复' : '一次性'}
                      {pack.enabled === false ? ' · 已停用' : ''}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
