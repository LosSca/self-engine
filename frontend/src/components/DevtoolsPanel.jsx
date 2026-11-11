import { useState, useEffect } from 'react'
import { Terminal, X, Trash2, ChevronRight, Brain, AlertTriangle, RefreshCw, Cpu } from 'lucide-react'
import { agentsSummary, callLabel } from '../agentView'

const fmtTok = (n) => {
  const v = Number(n) || 0
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}

const TAG_COLORS = {
  narrator: 'text-emerald-400 border-emerald-400/30 bg-emerald-400/10',
  auditor: 'text-amber-400 border-amber-400/30 bg-amber-400/10',
  combat: 'text-rose-400 border-rose-400/30 bg-rose-400/10',
  npc_mind: 'text-indigo-400 border-indigo-400/30 bg-indigo-400/10',
  memory: 'text-sky-400 border-sky-400/30 bg-sky-400/10',
}
const tagColor = (tag) =>
  TAG_COLORS[tag] || 'text-white/60 border-white/15 bg-white/5'

const fmtDateTime = (iso) => {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

const truncate = (s, n) => {
  if (!s) return ''
  return s.length > n ? `${s.slice(0, n)}…` : s
}

function turnTotals(entries) {
  return (entries || []).reduce(
    (acc, e) => {
      const u = e.usage || {}
      acc.input += u.input || 0
      acc.output += u.output || 0
      acc.cacheRead += u.cache_read || 0
      acc.cacheWrite += u.cache_creation || 0
      return acc
    },
    { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }
  )
}

function CallCard({ entry }) {
  const [openInput, setOpenInput] = useState(false)
  const [openOutput, setOpenOutput] = useState(true)
  const u = entry.usage || {}
  return (
    <div className="rounded-xl bg-white/[0.03] border border-white/5 overflow-hidden">
      <div className="px-4 py-3 flex items-center gap-2 flex-wrap border-b border-white/5">
        <span className="text-white/30 text-xs font-mono">#{entry.seq}</span>
        <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border ${tagColor(entry.tag)}`}>
          {entry.tag}
        </span>
        <span className="text-white/60 text-xs font-mono truncate max-w-[280px]" title={entry.label}>
          {entry.label}
        </span>
        <span className="text-white/20 text-[10px] font-mono ml-auto">{entry.model}</span>
      </div>
      <div className="px-4 py-2 flex items-center gap-3 flex-wrap text-[11px] font-mono text-white/40">
        <span>in <span className="text-white/70">{fmtTok(u.input)}</span></span>
        <span>out <span className="text-white/70">{fmtTok(u.output)}</span></span>
        {u.cache_read > 0 && <span className="text-emerald-400/70">cache_r {fmtTok(u.cache_read)}</span>}
        {u.cache_creation > 0 && <span className="text-sky-400/70">cache_w {fmtTok(u.cache_creation)}</span>}
        <span>sys {fmtTok(entry.instructions_chars)}ch</span>
        <span className="text-white/25">{entry.elapsed_s}s</span>
      </div>

      <div className="px-4 pb-3 space-y-2">
        <button
          onClick={() => setOpenOutput((v) => !v)}
          className="flex items-center gap-1 text-[10px] uppercase tracking-widest font-bold text-white/50 hover:text-white transition-colors"
        >
          <ChevronRight size={12} className={`transition-transform ${openOutput ? 'rotate-90' : ''}`} />
          输出
        </button>
        {openOutput && (
          <pre className="text-[11px] leading-relaxed text-[#d1d1d1] font-mono whitespace-pre-wrap break-words bg-black/40 rounded-lg p-3 max-h-64 overflow-auto custom-scrollbar">
            {typeof entry.output === 'string' ? entry.output : JSON.stringify(entry.output, null, 2)}
          </pre>
        )}

        <button
          onClick={() => setOpenInput((v) => !v)}
          className="flex items-center gap-1 text-[10px] uppercase tracking-widest font-bold text-white/50 hover:text-white transition-colors"
        >
          <ChevronRight size={12} className={`transition-transform ${openInput ? 'rotate-90' : ''}`} />
          输入（{(entry.input || []).length} 段）
        </button>
        {openInput && (
          <div className="space-y-2">
            {(entry.input || []).map((s, i) => (
              <div key={i}>
                <div className="text-[10px] uppercase tracking-widest font-bold text-white/40 mb-1">
                  {s.title}{s.truncated ? ' · 已截断' : ''}
                </div>
                <pre className="text-[11px] leading-relaxed text-white/70 font-mono whitespace-pre-wrap break-words bg-black/40 rounded-lg p-3 max-h-72 overflow-auto custom-scrollbar">
                  {s.body}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}


const LAYER_LABELS = {
  global: '世界',
  story: '剧情',
  scene: '场景',
  character: '人物',
  event: '事件',
  history: '历史',
  longterm: '长期记忆',
  canon: '设定',
}
const layerLabel = (layer) => LAYER_LABELS[layer] || layer || '未标注'

function fmtCounts(counts) {
  const entries = Object.entries(counts || {})
  if (!entries.length) return '无上下文'
  return entries
    .map(([k, v]) => `${layerLabel(k)}×${v}`)
    .join(' · ')
}

/**
 * One narration event, as the engine stored it.
 *
 * This is the read-back the trace console cannot give: it is written by the
 * committed turn itself, so it survives a restart and includes the narrator
 * failures that never made it into a trace.
 */
function NarrationCard({ row }) {
  const [openBlocks, setOpenBlocks] = useState(false)
  const payload = row.payload && typeof row.payload === 'object' ? row.payload : {}
  const diag = payload.diagnostics && typeof payload.diagnostics === 'object' ? payload.diagnostics : null
  const failed = row.event_type === 'narrative.failed'
  return (
    <div className={`rounded-xl bg-white/[0.03] border overflow-hidden ${failed ? 'border-rose-400/20' : 'border-white/5'}`}>
      <div className="px-4 py-3 flex items-center gap-2 flex-wrap border-b border-white/5">
        <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border ${
          failed
            ? 'text-rose-400 border-rose-400/30 bg-rose-400/10'
            : 'text-emerald-400 border-emerald-400/30 bg-emerald-400/10'
        }`}>
          {failed ? '叙事失败' : '叙事'}
        </span>
        <span className="text-white/25 text-[10px] font-mono">v{row.state_version}</span>
        {payload.model && (
          <span className="text-white/40 text-[11px] font-mono truncate max-w-[200px]" title={payload.model}>
            {payload.model}
          </span>
        )}
        {row.created_at && (
          <span className="text-white/25 text-[10px] font-mono ml-auto">{fmtDateTime(row.created_at)}</span>
        )}
      </div>

      {payload.error && (
        <div className="px-4 py-2 text-[11px] font-mono text-rose-300/90 bg-rose-400/5 break-words">
          {payload.degraded ? '已降级：' : ''}{payload.error}
        </div>
      )}

      {diag ? (
        <div className="px-4 py-2 space-y-1.5">
          <div className="flex items-center gap-3 flex-wrap text-[11px] font-mono text-white/50">
            <span>{diag.provider || '未知路由'}{diag.model ? ` / ${diag.model}` : ''}</span>
            {diag.preset_routed && <span className="text-sky-400/70">预设路由</span>}
            {diag.skipped && <span className="text-amber-400/70">未调用模型</span>}
            {diag.content_policy && diag.content_policy !== 'standard' && (
              <span className="text-fuchsia-400/70">{diag.content_policy}</span>
            )}
          </div>
          <div className="flex items-center gap-3 flex-wrap text-[11px] font-mono text-white/35">
            {diag.temperature != null && <span>temp {diag.temperature}</span>}
            {diag.max_tokens != null && <span>max_tokens {diag.max_tokens}</span>}
            {diag.timeout ? <span>timeout {diag.timeout}s</span> : null}
            {diag.language && <span>{diag.language}</span>}
          </div>
          <div className="text-[11px] font-mono text-white/35">
            上下文：<span className="text-white/60">{fmtCounts(diag.fact_counts)}</span>
          </div>
          {(diag.blocks || []).length > 0 && (
            <>
              <button
                onClick={() => setOpenBlocks((v) => !v)}
                className="flex items-center gap-1 text-[10px] uppercase tracking-widest font-bold text-white/50 hover:text-white transition-colors"
              >
                <ChevronRight size={12} className={`transition-transform ${openBlocks ? 'rotate-90' : ''}`} />
                模型看到的 {(diag.blocks || []).length} 段上下文
              </button>
              {openBlocks && (
                <div className="space-y-2 pt-1">
                  {(diag.blocks || []).map((b, i) => (
                    <div key={`${b.id || i}`} className="rounded-lg bg-black/40 p-3">
                      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold text-white/40 mb-1">
                        <span>{layerLabel(b.layer)}</span>
                        <span className="text-white/60 normal-case tracking-normal">{b.title || b.id}</span>
                      </div>
                      <p className="text-[11px] leading-relaxed text-white/60 font-mono whitespace-pre-wrap break-words">
                        {b.excerpt}
                      </p>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      ) : (
        <div className="px-4 py-2 text-[11px] text-white/25">该回合没有记录叙事上下文。</div>
      )}
    </div>
  )
}

function NarrationTab({ rows, loading, onRefresh }) {
  if (loading) {
    return <div className="text-white/25 text-sm font-light py-10 text-center">正在读取本回合的叙事记录…</div>
  }
  if (!rows.length) {
    return (
      <div className="py-14 text-center">
        <Brain size={28} className="text-white/20 mb-3 mx-auto" />
        <p className="text-white/25 text-sm font-light">还没有叙事记录。执行一个行动后再回来。</p>
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold text-white/40">
        <span>{rows.length} 条叙事事件</span>
        <button onClick={onRefresh} className="text-white/40 hover:text-white transition-colors p-1" title="重新读取">
          <RefreshCw size={12} />
        </button>
      </div>
      {rows.map((row, i) => (
        <NarrationCard key={`${row.state_version}-${row.event_type}-${i}`} row={row} />
      ))}
    </div>
  )
}

const STATUS_STYLES = {
  applied: 'text-emerald-400 border-emerald-400/30 bg-emerald-400/10',
  rejected: 'text-amber-400 border-amber-400/30 bg-amber-400/10',
  duplicate: 'text-sky-400 border-sky-400/30 bg-sky-400/10',
  conflict: 'text-rose-400 border-rose-400/30 bg-rose-400/10',
}

/**
 * The action log: every recorded turn, and the one you open in full.
 *
 * A rejected turn is as important as an applied one here - it is the only
 * place that says *why* the rules refused, with the code the player saw.
 */
function ActionLogTab({ rows, loading, onRefresh, onOpen, selected, detail }) {
  if (loading) {
    return <div className="text-white/25 text-sm font-light py-10 text-center">正在读取行动记录…</div>
  }
  if (!rows.length) {
    return (
      <div className="py-14 text-center">
        <p className="text-white/25 text-sm font-light">还没有记录的行动。</p>
      </div>
    )
  }
  const result = detail?.result && typeof detail.result === 'object' ? detail.result : {}
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold text-white/40">
        <span>{rows.length} 条记录</span>
        <button onClick={onRefresh} className="text-white/40 hover:text-white transition-colors p-1" title="重新读取">
          <RefreshCw size={12} />
        </button>
        <span className="text-white/20 normal-case tracking-normal">点击一行查看完整请求与结果</span>
      </div>
      <div className="rounded-xl border border-white/5 overflow-hidden">
        {rows.map((row) => (
          <button
            key={row.request_id}
            onClick={() => onOpen(row.request_id)}
            className={`w-full text-left px-4 py-2 flex items-center gap-3 text-[11px] font-mono transition-colors ${
              selected === row.request_id ? 'bg-white/10 text-white' : 'text-white/50 hover:bg-white/5'
            }`}
          >
            <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border ${
              STATUS_STYLES[row.status] || 'text-white/50 border-white/15 bg-white/5'
            }`}>
              {row.status || '?'}
            </span>
            <span className="truncate flex-1" title={row.request_id}>{row.request_id}</span>
            <span className="text-white/25">v{row.state_version}</span>
            <span className="text-white/25">{fmtDateTime(row.created_at)}</span>
          </button>
        ))}
      </div>

      {selected && detail && (
        <div className="rounded-xl bg-white/[0.03] border border-white/5 p-4 space-y-2">
          <div className="text-[10px] uppercase tracking-widest font-bold text-white/40">请求</div>
          <pre className="text-[11px] leading-relaxed text-white/70 font-mono whitespace-pre-wrap break-words bg-black/40 rounded-lg p-3 max-h-48 overflow-auto custom-scrollbar">
            {JSON.stringify(detail.request || {}, null, 2)}
          </pre>
          <div className="text-[10px] uppercase tracking-widest font-bold text-white/40">结果</div>
          <div className="flex items-center gap-3 flex-wrap text-[11px] font-mono text-white/50">
            <span>status {result.status || '?'}</span>
            {result.code && <span className="text-amber-400/80">code {result.code}</span>}
            {result.message && <span className="text-white/40">{result.message}</span>}
            {result.state_version != null && <span>v{result.state_version}</span>}
          </div>
          {(result.effects_applied || []).length > 0 && (
            <pre className="text-[11px] leading-relaxed text-white/60 font-mono whitespace-pre-wrap break-words bg-black/40 rounded-lg p-3 max-h-48 overflow-auto custom-scrollbar">
              {JSON.stringify(result.effects_applied, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
export default function DevtoolsPanel({
  open,
  onClose,
  traces,
  onClear,
  campaignId = '',
  fetchDomainEvents,
  fetchRequests,
  fetchRequestDetail,
  fetchAgents,
  fetchAgentCalls,
}) {
  const [selectedKey, setSelectedKey] = useState(null)
  const [tab, setTab] = useState('traces')
  const [narration, setNarration] = useState([])
  const [narrationLoading, setNarrationLoading] = useState(false)
  const [narrationError, setNarrationError] = useState('')
  const [requests, setRequests] = useState([])
  const [requestsLoading, setRequestsLoading] = useState(false)
  const [requestsError, setRequestsError] = useState('')
  const [openRequestId, setOpenRequestId] = useState('')
  const [requestDetail, setRequestDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [roles, setRoles] = useState([])
  const [calls, setCalls] = useState([])
  const [agentsError, setAgentsError] = useState('')
  const [agentsLoading, setAgentsLoading] = useState(false)

  useEffect(() => {
    if (open && traces.length) setSelectedKey(traces[traces.length - 1].key)
  }, [open, traces])

  const loadNarration = async () => {
    if (!campaignId || typeof fetchDomainEvents !== 'function') return
    setNarrationLoading(true)
    setNarrationError('')
    try {
      const rows = await fetchDomainEvents(campaignId, { limit: 200 })
      const list = Array.isArray(rows) ? rows : rows?.events || []
      setNarration(
        list
          .filter((row) =>
            ['narrator.response', 'narrative.failed'].includes(row?.event_type)
          )
          .reverse()
      )
    } catch (err) {
      setNarrationError(err?.message || '读取叙事记录失败')
      setNarration([])
    } finally {
      setNarrationLoading(false)
    }
  }

  const loadRequests = async () => {
    if (!campaignId || typeof fetchRequests !== 'function') return
    setRequestsLoading(true)
    setRequestsError('')
    try {
      const rows = await fetchRequests(campaignId, 100)
      setRequests(Array.isArray(rows) ? rows : [])
    } catch (err) {
      setRequestsError(err?.message || '读取行动记录失败')
      setRequests([])
    } finally {
      setRequestsLoading(false)
    }
  }

  const openRequest = async (requestId) => {
    if (!requestId) return
    setOpenRequestId(requestId)
    if (typeof fetchRequestDetail !== 'function') return
    setDetailLoading(true)
    try {
      setRequestDetail(await fetchRequestDetail(requestId))
    } catch (err) {
      setRequestsError(err?.message || '读取行动详情失败')
      setRequestDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }

  const loadAgents = async () => {
    setAgentsLoading(true)
    setAgentsError('')
    try {
      const payload =
        typeof fetchAgents === 'function' ? await fetchAgents() : { roles: [] }
      setRoles(agentsSummary(payload?.roles))
      const rows =
        campaignId && typeof fetchAgentCalls === 'function'
          ? await fetchAgentCalls(campaignId, { limit: 100 })
          : []
      setCalls(Array.isArray(rows) ? rows : [])
    } catch (err) {
      setAgentsError(err?.message || '读取 Agent 信息失败')
    } finally {
      setAgentsLoading(false)
    }
  }

  useEffect(() => {
    if (open && tab === 'narration') loadNarration()
    if (open && tab === 'actions') loadRequests()
    if (open && tab === 'agents') loadAgents()
  }, [open, tab, campaignId])

  if (!open) return null
  const current = traces.find((t) => t.key === selectedKey) || traces[traces.length - 1] || null
  const tot = current ? turnTotals(current.entries) : null
  const currentEntries = current?.entries || []

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="bg-white/[0.03] backdrop-blur-xl border border-white/5 rounded-[2rem] w-full max-w-5xl mx-4 overflow-hidden max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5 flex-none">
          <div className="flex items-center gap-3">
            <Terminal size={16} className="text-white" />
            <h2 className="text-white font-bold text-sm uppercase tracking-widest">LLM 调试台</h2>
            <div className="flex items-center gap-1 ml-2">
              {[
                ['traces', `调用追踪 ${traces.length}`],
                ['narration', '叙事与上下文'],
                ['actions', '行动记录'],
                ['agents', '模型调用'],
              ].map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => setTab(key)}
                  className={`text-[11px] font-mono px-2.5 py-1 rounded-full border transition-colors ${
                    tab === key
                      ? 'text-white border-white/20 bg-white/10'
                      : 'text-white/40 border-transparent hover:text-white hover:bg-white/5'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {traces.length > 0 && (
              <button
                onClick={onClear}
                className="text-white/40 hover:text-rose-400 transition-colors p-1"
                title="清除已捕获的调用记录"
              >
                <Trash2 size={14} />
              </button>
            )}
            <button onClick={onClose} className="text-white/40 hover:text-white transition-colors">
              <X size={18} />
            </button>
          </div>
        </div>

        {tab === 'actions' ? (
          <div className="flex-1 overflow-y-auto custom-scrollbar p-6">
            {requestsError && (
              <div className="mb-3 flex items-center gap-2 text-[11px] font-mono text-rose-300/90">
                <AlertTriangle size={12} /> {requestsError}
              </div>
            )}
            <ActionLogTab
              rows={requests}
              loading={requestsLoading || detailLoading}
              onRefresh={loadRequests}
              onOpen={openRequest}
              selected={openRequestId}
              detail={requestDetail}
            />
          </div>
        ) : tab === 'agents' ? (
          <div className="flex-1 overflow-y-auto custom-scrollbar p-6">
            {agentsError && (
              <div className="mb-3 flex items-center gap-2 text-[11px] font-mono text-rose-300/90">
                <AlertTriangle size={12} /> {agentsError}
              </div>
            )}
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-2 text-[11px] font-mono text-white/40">
                <Cpu size={12} /> 每个角色由哪个模型回答
              </div>
              <button
                onClick={loadAgents}
                className="text-white/40 hover:text-white transition-colors p-1"
                title="刷新"
              >
                <RefreshCw size={14} />
              </button>
            </div>
            <div className="space-y-1 mb-6">
              {roles.length === 0 && !agentsLoading && (
                <p className="text-white/25 text-[11px] font-mono">没有读到 Agent 路由。</p>
              )}
              {roles.map((entry) => (
                <div
                  key={entry.role}
                  className="flex items-center gap-3 text-[11px] font-mono px-2.5 py-1.5 rounded-lg border border-white/5 bg-white/[0.02]"
                >
                  <span className="text-white/70 w-12">{entry.label}</span>
                  <span className="text-white/40">{entry.text}</span>
                </div>
              ))}
            </div>
            <div className="text-[11px] font-mono text-white/40 mb-2">模型调用记录</div>
            {calls.length === 0 && !agentsLoading && (
              <p className="text-white/25 text-[11px] font-mono">
                本战役还没有记录模型调用。
              </p>
            )}
            <div className="space-y-1">
              {calls.map((call, index) => (
                <div
                  key={`${call.created_at}-${index}`}
                  className={`text-[11px] font-mono px-2.5 py-1.5 rounded-lg border ${
                    call.status === "error"
                      ? "border-rose-400/30 bg-rose-400/5 text-rose-300/90"
                      : "border-white/5 bg-white/[0.02] text-white/50"
                  }`}
                >
                  {callLabel(call)}
                </div>
              ))}
            </div>
          </div>
        ) : tab === 'narration' ? (
          <div className="flex-1 overflow-y-auto custom-scrollbar p-6">
            {narrationError && (
              <div className="mb-3 flex items-center gap-2 text-[11px] font-mono text-rose-300/90">
                <AlertTriangle size={12} /> {narrationError}
              </div>
            )}
            <NarrationTab rows={narration} loading={narrationLoading} onRefresh={loadNarration} />
          </div>
        ) : traces.length === 0 ? (
          <div className="flex-1 flex flex-col items-center justify-center py-16">
            <Terminal size={32} className="text-white/20 mb-3" />
            <p className="text-white/20 text-sm font-light">
              尚未捕获调用。执行一个行动以追踪本回合的 LLM 调用。
            </p>
          </div>
        ) : (
          <div className="flex flex-1 overflow-hidden">
            {/* Turn list */}
            <div className="w-40 flex-none border-r border-white/5 overflow-y-auto custom-scrollbar py-2">
              {[...traces].reverse().map((t) => (
                <button
                  key={t.key}
                  onClick={() => setSelectedKey(t.key)}
                  className={`w-full text-left px-4 py-2 text-xs font-mono transition-colors ${
                    (current && t.key === current.key)
                      ? 'text-white bg-white/10'
                      : 'text-white/40 hover:text-white hover:bg-white/5'
                  }`}
                >
                  {t.label}
                  {t.created_at && (
                    <span className="block text-[10px] text-white/25">{fmtDateTime(t.created_at)}</span>
                  )}
                  {t.action && (
                    <span className="block text-[10px] text-white/25 truncate" title={t.action}>
                      {truncate(t.action, 40)}
                    </span>
                  )}
                  <span className="block text-[10px] text-white/25">{(t.entries || []).length} 次调用</span>
                </button>
              ))}
            </div>

            {/* Calls of selected turn */}
            <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-3">
              {current && (
                <div className="flex items-center gap-3 flex-wrap text-[11px] font-mono text-white/40 pb-2">
                  <span className="text-white/60 uppercase tracking-widest font-bold text-[10px]">本回合合计</span>
                  <span>in <span className="text-white/70">{fmtTok(tot.input)}</span></span>
                  <span>out <span className="text-white/70">{fmtTok(tot.output)}</span></span>
                  {tot.cacheRead > 0 ? (
                    <span className="text-emerald-400/70">cache_r {fmtTok(tot.cacheRead)} · cache_w {fmtTok(tot.cacheWrite)}</span>
                  ) : (
                    <span className="text-amber-400/60">缓存关闭</span>
                  )}
                  <span className="text-white/25">{currentEntries.length} 次调用</span>
                </div>
              )}
              {currentEntries.map((e) => (
                <CallCard key={e.seq} entry={e} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
