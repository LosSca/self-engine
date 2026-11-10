import { useCallback, useEffect, useState } from 'react'
import {
  fetchPlayConfig,
  savePlayConfig,
  fetchKnowledge,
  generateStorylineDraft,
} from '../api'

const KIND_LABELS = { main: '主线', branch: '分支', side: '独立剧情' }

const CONDITION_KINDS = [
  ['flag', '剧情标记'],
  ['has_item', '持有物品'],
  ['item_count', '物品数量'],
  ['door_open', '门已开启'],
  ['at_location', '位于地点'],
  ['quest_status', '任务状态'],
  ['entity_at', '实体位于地点'],
  ['entity_alive', '实体存活'],
  ['entity_item_count', '实体持有物品数'],
  ['entity_relation', '实体关系'],
  ['entity_alert', '实体警戒'],
  ['skill', '角色技能'],
  ['combatant_hp', '实体生命值'],
  ['combatant_alive', '实体战斗存活'],
  ['currency', '货币数量'],
  ['level', '角色等级'],
]
const OPS = [
  ['eq', '等于'], ['ne', '不等于'], ['gte', '大于等于'],
  ['lte', '小于等于'], ['gt', '大于'], ['lt', '小于'],
]
// These compare condition.count; item_count/combatant_hp compare condition.value.
const COUNT_KINDS = new Set([
  'entity_item_count', 'entity_relation', 'entity_alert',
  'skill', 'currency', 'level',
])
const VALUE_UNUSED = new Set(['entity_alert', 'skill', 'currency', 'level'])
const TARGET_UNUSED = new Set(['currency', 'level'])
const VALUE_HINTS = {
  flag: '期望值',
  has_item: 'true / false',
  item_count: '数量阈值',
  door_open: 'true / false',
  at_location: 'true / false',
  quest_status: 'inactive / active / completed / failed',
  entity_at: '地点 ID',
  entity_alive: 'true / false',
  entity_item_count: '物品 ID',
  entity_relation: '对方实体 ID',
  combatant_hp: '生命值阈值',
  combatant_alive: 'true / false',
}
const COUNT_HINTS = {
  entity_item_count: '数量阈值',
  entity_relation: '关系阈值',
  entity_alert: '警戒阈值',
  skill: '技能等级阈值',
  currency: '货币阈值',
  level: '等级阈值',
}
const RESTRICTED_OPS = new Set(['has_item', 'item_count'])

const inputClass =
  'w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2 text-sm text-white placeholder:text-white/20 focus:outline-none focus:border-white/30'
const tinyInput =
  'bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-xs text-white placeholder:text-white/20 focus:outline-none focus:border-white/30'

function splitList(text) {
  return String(text || '').split(',').map((value) => value.trim()).filter(Boolean)
}

function nextLineId(lines) {
  let highest = 0
  for (const line of lines) {
    const match = /^line_(\d+)$/.exec(line.id || '')
    if (match) highest = Math.max(highest, Number(match[1]))
  }
  return `line_${highest + 1}`
}

function nextPackId(packs) {
  let highest = 0
  for (const pack of packs) {
    const match = /^pack_(\d+)$/.exec(pack.id || '')
    if (match) highest = Math.max(highest, Number(match[1]))
  }
  return `pack_${highest + 1}`
}

function showValue(value) {
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function parseValue(text) {
  const trimmed = String(text).trim()
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false
  if (trimmed !== '' && !Number.isNaN(Number(trimmed))) return Number(trimmed)
  return trimmed
}

function FieldLabel({ children }) {
  return (
    <span className="block text-[10px] font-mono uppercase tracking-[0.2em] text-white/40 mb-2">
      {children}
    </span>
  )
}

function ConditionRows({ value, onChange }) {
  const update = (index, patch) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, index) => (
        <div key={index} className="flex flex-wrap items-center gap-2 rounded-lg border border-white/5 bg-black/20 p-2">
          <select className={tinyInput} value={row.kind} onChange={(e) => update(index, { kind: e.target.value })}>
            {CONDITION_KINDS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
          {!TARGET_UNUSED.has(row.kind) && (
            <input
              className={tinyInput + ' w-40'}
              value={row.target || ''}
              onChange={(e) => update(index, { target: e.target.value })}
              placeholder="目标 ID"
            />
          )}
          <select className={tinyInput} value={row.op || 'eq'} onChange={(e) => update(index, { op: e.target.value })}>
            {(RESTRICTED_OPS.has(row.kind)
              ? OPS.filter(([id]) => id === 'eq' || id === 'gte')
              : OPS
            ).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
          {!VALUE_UNUSED.has(row.kind) && (
            <input
              className={tinyInput + ' w-28'}
              value={showValue(row.value)}
              onChange={(e) => update(index, { value: parseValue(e.target.value) })}
              placeholder={VALUE_HINTS[row.kind] || '值'}
            />
          )}
          {COUNT_KINDS.has(row.kind) && (
            <input
              className={tinyInput + ' w-24'}
              type="number"
              value={row.count ?? 1}
              onChange={(e) => update(index, { count: Number(e.target.value) })}
              placeholder={COUNT_HINTS[row.kind] || '阈值'}
            />
          )}
          <button
            onClick={() => onChange(value.filter((_, i) => i !== index))}
            className="text-[10px] text-white/30 hover:text-rose-400"
          >
            移除
          </button>
        </div>
      ))}
      <button
        onClick={() => onChange([...value, { kind: 'flag', target: '', op: 'eq', value: true, count: 1 }])}
        className="self-start text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
      >
        + 添加条件
      </button>
    </div>
  )
}

export default function StorylineStudio({ open, onClose, scenarioId, scenarioTitle }) {
  const [config, setConfig] = useState(null)
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState([])
  const [notice, setNotice] = useState('')
  const [editing, setEditing] = useState(null)
  const [prompt, setPrompt] = useState('')
  const [warnings, setWarnings] = useState([])

  const report = useCallback((err) => {
    if (Array.isArray(err && err.detail)) setErrors(err.detail)
    else if (typeof (err && err.detail) === 'string') setErrors([err.detail])
    else setErrors([(err && err.message) || '操作失败'])
    setNotice('')
  }, [])

  useEffect(() => {
    if (!open || !scenarioId) return undefined
    let cancelled = false
    setLoading(true)
    setErrors([])
    setNotice('')
    setEditing(null)
    setWarnings([])
    setConfig(null)
    setEntries([])
    Promise.all([fetchPlayConfig(scenarioId), fetchKnowledge(scenarioId)])
      .then(([playConfig, knowledge]) => {
        if (cancelled) return
        setConfig(playConfig)
        setEntries(knowledge || [])
      })
      .catch((err) => { if (!cancelled) report(err) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, scenarioId, report])

  if (!open) return null

  const story = config?.storyline || { name: '', description: '', lines: [], nodes: [] }
  const lines = story.lines || []
  const nodes = story.nodes || []
  const packs = story.packs || []

  const updateStoryline = (patch) =>
    setConfig((prev) => ({ ...prev, storyline: { ...(prev.storyline || {}), ...patch } }))

  const updateNode = (index, patch) =>
    updateStoryline({ nodes: nodes.map((node, i) => (i === index ? { ...node, ...patch } : node)) })

  const updatePack = (index, patch) =>
    updateStoryline({ packs: packs.map((pack, i) => (i === index ? { ...pack, ...patch } : pack)) })

  const save = async () => {
    if (!config || saving) return
    setSaving(true)
    setErrors([])
    setNotice('')
    try {
      const saved = await savePlayConfig(scenarioId, {
        mode: config.mode,
        storyline: config.storyline,
        max_depth: config.max_depth,
        context_budget_chars: config.context_budget_chars ?? 0,
        model_preset_id: config.model_preset_id || '',
        intent_preset_id: config.intent_preset_id || '',
        dialogue_preset_id: config.dialogue_preset_id || '',
        summary_preset_id: config.summary_preset_id || '',
        narrator_prompt: config.narrator_prompt || '',
      })
      setConfig(saved)
      setNotice('故事线已保存')
    } catch (err) {
      report(err)
    } finally {
      setSaving(false)
    }
  }

  const generate = async () => {
    if (!prompt.trim() || saving) return
    if (nodes.length > 0 && !confirm('AI 将替换当前故事线的全部节点，是否继续？')) return
    setSaving(true)
    setErrors([])
    setNotice('')
    setWarnings([])
    try {
      const result = await generateStorylineDraft(scenarioId, { prompt, nodeCount: 6 })
      setConfig((prev) => ({ ...prev, storyline: result.storyline }))
      setWarnings(result.warnings || [])
      setNotice('AI 草稿已载入，检查后点击保存故事线')
    } catch (err) {
      report(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/85 flex items-start justify-center overflow-y-auto p-4 sm:p-8">
      <div className="w-full max-w-6xl bg-[#080b0d] border border-white/10 rounded-[2rem] shadow-2xl">
        <div className="flex flex-wrap items-center justify-between gap-4 p-6 border-b border-white/10">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-white/30 mb-2">
              故事线工坊
            </div>
            <h2 className="text-2xl font-bold text-white">{scenarioTitle || '故事线'}</h2>
            <p className="text-xs text-white/40 mt-2">
              组织主线、分支与独立剧情；节点只由前置、条件和事件推进。
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={save} disabled={saving || !config} className="px-5 py-2 rounded-xl bg-white text-black text-xs font-bold tracking-[0.15em] hover:bg-gray-200 disabled:opacity-40">
              {saving ? '处理中…' : '保存故事线'}
            </button>
            <button onClick={onClose} className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:text-white">
              关闭
            </button>
          </div>
        </div>

        {(notice || errors.length > 0) && (
          <div className="px-6 pt-4">
            {notice && <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 text-emerald-300 text-xs px-4 py-2">{notice}</div>}
            {errors.length > 0 && (
              <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 text-rose-300 text-xs px-4 py-3 mt-2">
                <div className="font-bold mb-1">未通过校验</div>
                <ul className="list-disc pl-5 space-y-1">{errors.map((item, index) => <li key={index}>{item}</li>)}</ul>
              </div>
            )}
          </div>
        )}

        <div className="p-6 flex flex-col gap-5">
          {loading && <p className="text-xs text-white/40">读取中……</p>}
          {!loading && !scenarioId && <p className="text-xs text-white/40">缺少情景，无法编辑故事线。</p>}
          {config && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <label>
                  <FieldLabel>运行模式</FieldLabel>
                  <select className={inputClass + ' max-w-[12rem]'} value={config.mode} onChange={(e) => setConfig({ ...config, mode: e.target.value })}>
                    <option value="storyline">故事线模式</option>
                    <option value="free">自由模式</option>
                  </select>
                </label>
                <span className="text-[10px] text-white/30 mt-6">自由模式不推进节点；故事线可随时切回。</span>
              </div>

              <div className="rounded-2xl border border-cyan-400/20 bg-cyan-400/[0.04] p-4 flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-sm font-bold text-cyan-100">AI 生成剧情线草稿</span>
                  <span className="text-[10px] text-white/35">覆盖当前编辑中的节点，保存前不会写入</span>
                </div>
                <textarea className={inputClass + ' min-h-[70px]'} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="描述主线、关键地点、人物冲突与想要的结局。" />
                <div className="flex flex-wrap items-center gap-3">
                  <button onClick={generate} disabled={saving || !prompt.trim()} className="px-4 py-2 rounded-xl bg-cyan-400 text-black text-xs font-bold hover:bg-cyan-300 disabled:opacity-40">
                    {saving ? '生成中…' : '生成剧情线'}
                  </button>
                  {warnings.length > 0 && <span className="text-[11px] text-amber-300">{warnings.length} 条校验提示</span>}
                </div>
                {warnings.length > 0 && (
                  <ul className="text-[11px] text-amber-200/80 list-disc pl-5 space-y-1">
                    {warnings.slice(0, 6).map((warning, index) => <li key={index}>{warning}</li>)}
                  </ul>
                )}
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4 flex flex-col gap-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="text-sm font-bold text-white">剧情线分组</span>
                  <button
                    onClick={() => updateStoryline({
                      lines: [...lines, { id: nextLineId(lines), title: '新剧情线', kind: 'main', description: '', enabled: true }],
                    })}
                    className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:text-white"
                  >
                    新增剧情线
                  </button>
                </div>
                {lines.length === 0 && <p className="text-xs text-white/30">尚无分组；节点仍可正常推进。</p>}
                {lines.map((line, index) => (
                  <div key={index} className={line.locked ? 'rounded-xl border border-amber-400/30 bg-amber-400/[0.03] p-3 grid grid-cols-1 sm:grid-cols-4 gap-3' : 'rounded-xl border border-white/10 p-3 grid grid-cols-1 sm:grid-cols-4 gap-3'}>
                    {line.locked ? (
                      <div className="sm:col-span-4 text-[11px] text-amber-200/70">
                        🔒 这条剧情线已锁定：{line.title}（{line.id}）· {nodes.filter((node) => node.line === line.id).length} 节点。解锁后才能修改。
                      </div>
                    ) : null}
                    <label>
                      <FieldLabel>名称</FieldLabel>
                      <input className={inputClass} value={line.title} disabled={line.locked} onChange={(e) => updateStoryline({ lines: lines.map((row, i) => i === index ? { ...row, title: e.target.value } : row) })} />
                    </label>
                    <label>
                      <FieldLabel>类型</FieldLabel>
                      <select
                        className={inputClass}
                        value={line.kind}
                        onChange={(e) => updateStoryline({
                          lines: lines.map((row, i) => i === index ? { ...row, kind: e.target.value } : row),
                          nodes: nodes.map((node) => node.line === line.id ? { ...node, kind: e.target.value } : node),
                        })}
                      >
                        {Object.entries(KIND_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                      </select>
                    </label>
                    <label>
                      <FieldLabel>说明</FieldLabel>
                      <input className={inputClass} value={line.description || ''} onChange={(e) => updateStoryline({ lines: lines.map((row, i) => i === index ? { ...row, description: e.target.value } : row) })} />
                    </label>
                    <div className="flex flex-wrap items-center gap-3 text-xs text-white/50 pt-5">
                      <label className="flex items-center gap-2">
                        <input type="checkbox" checked={line.enabled !== false} disabled={line.locked} onChange={(e) => updateStoryline({ lines: lines.map((row, i) => i === index ? { ...row, enabled: e.target.checked } : row) })} />
                        启用
                      </label>
                      <label className="flex items-center gap-2">
                        <input type="checkbox" checked={line.locked === true} onChange={(e) => updateStoryline({ lines: lines.map((row, i) => i === index ? { ...row, locked: e.target.checked } : row) })} />
                        锁定
                      </label>
                      <span className="text-white/25">{line.id} · {nodes.filter((node) => node.line === line.id).length} 节点</span>
                      {line.locked ? null : (
                        <button
                          onClick={() => updateStoryline({
                            lines: lines.filter((_, i) => i !== index),
                            nodes: nodes.map((node) => node.line === line.id ? { ...node, line: '' } : node),
                          })}
                          className="ml-auto text-[10px] text-white/30 hover:text-rose-400"
                        >
                          移除
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4 flex flex-col gap-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="text-sm font-bold text-white">故事线节点</span>
                  <button
                    onClick={() => {
                      updateStoryline({ nodes: [...nodes, { id: '', title: '', kind: 'main', line: '', summary: '', prereq: [], conditions: [], requires_event: null, knowledge_ids: [] }] })
                      setEditing(nodes.length)
                    }}
                    className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:text-white"
                  >
                    新增节点
                  </button>
                </div>

                {nodes.map((node, index) => {
                  const nodeLine = lines.find((row) => row.id === node.line)
                  const nodeLocked = node.locked === true || (nodeLine ? nodeLine.locked === true : false)
                  return (
                  <div key={index} className={nodeLocked ? 'rounded-xl border border-amber-400/30 bg-amber-400/[0.03] p-3 flex flex-col gap-3' : 'rounded-xl border border-white/10 p-3 flex flex-col gap-3'}>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-bold text-white">{node.title || node.id || '未命名节点'}</span>
                      <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-white/10 text-white/50">{node.id || '—'}</span>
                      <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-white/10 text-white/50">{KIND_LABELS[node.kind] || node.kind}</span>
                      {node.line && <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-white/10 text-white/50">{node.line}</span>}
                      {node.prereq.length > 0 && <span className="text-[10px] text-white/30">前置 {node.prereq.length}</span>}
                      {node.conditions.length > 0 && <span className="text-[10px] text-white/30">条件 {node.conditions.length}</span>}
                      {node.knowledge_ids.length > 0 && <span className="text-[10px] text-white/30">知识 {node.knowledge_ids.length}</span>}
                      {node.requires_event_pack && <span className="text-[10px] text-amber-300/70">事件包 {node.requires_event_pack}</span>}
                      {nodeLocked && <span className="text-[10px] text-amber-300/80">🔒 已锁定</span>}
                      <div className="flex-1" />
                      <label className="flex items-center gap-1 text-[10px] text-white/40">
                        <input type="checkbox" checked={node.locked === true} onChange={(e) => updateNode(index, { locked: e.target.checked })} />
                        锁定
                      </label>
                      <button onClick={() => setEditing(index)} disabled={nodeLocked} className="text-[10px] text-white/50 hover:text-white uppercase tracking-[0.15em] disabled:opacity-30">编辑</button>
                      {nodeLocked ? null : (
                        <button
                          onClick={() => {
                            updateStoryline({ nodes: nodes.filter((_, i) => i !== index) })
                            setEditing(null)
                          }}
                          className="text-[10px] text-white/30 hover:text-rose-400 uppercase tracking-[0.15em]"
                        >
                          移除
                        </button>
                      )}
                    </div>
                    {node.summary && <p className="text-xs text-white/40">{node.summary}</p>}
                    {editing === index && (
                      <div className="rounded-xl border border-white/10 bg-black/20 p-3 flex flex-col gap-3">
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                          <label>
                            <FieldLabel>节点 ID</FieldLabel>
                            <input className={inputClass} value={node.id} onChange={(e) => updateNode(index, { id: e.target.value })} />
                          </label>
                          <label>
                            <FieldLabel>标题</FieldLabel>
                            <input className={inputClass} value={node.title} onChange={(e) => updateNode(index, { title: e.target.value })} />
                          </label>
                          <label>
                            <FieldLabel>所属剧情线</FieldLabel>
                            <select
                              className={inputClass}
                              value={node.line || ''}
                              onChange={(e) => {
                                const line = lines.find((row) => row.id === e.target.value)
                                updateNode(index, { line: e.target.value, ...(line ? { kind: line.kind } : {}) })
                              }}
                            >
                              <option value="">未分组</option>
                              {lines.map((line) => <option key={line.id} value={line.id}>{line.title} · {KIND_LABELS[line.kind]}</option>)}
                            </select>
                          </label>
                        </div>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                          <label>
                            <FieldLabel>类型（未分组时可改）</FieldLabel>
                            <select className={inputClass} value={node.kind} disabled={Boolean(node.line)} onChange={(e) => updateNode(index, { kind: e.target.value })}>
                              {Object.entries(KIND_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                            </select>
                          </label>
                          <label>
                            <FieldLabel>前置节点（逗号分隔）</FieldLabel>
                            <input className={inputClass} value={(node.prereq || []).join(', ')} onChange={(e) => updateNode(index, { prereq: splitList(e.target.value) })} />
                          </label>
                        </div>
                        <label>
                          <FieldLabel>需要先触发的事件包</FieldLabel>
                          <select className={inputClass} value={node.requires_event_pack || ''} onChange={(e) => updateNode(index, { requires_event_pack: e.target.value || null })}>
                            <option value="">不依赖事件包</option>
                            {packs.map((pack) => <option key={pack.id} value={pack.id}>{pack.title || pack.id}</option>)}
                          </select>
                        </label>
                        <label>
                          <FieldLabel>摘要</FieldLabel>
                          <input className={inputClass} value={node.summary || ''} onChange={(e) => updateNode(index, { summary: e.target.value })} />
                        </label>
                        <label>
                          <FieldLabel>需要先触发的事件 ID</FieldLabel>
                          <input className={inputClass} value={node.requires_event || ''} onChange={(e) => updateNode(index, { requires_event: e.target.value || null })} />
                        </label>
                        <div>
                          <FieldLabel>进入条件（全部满足）</FieldLabel>
                          <ConditionRows value={node.conditions || []} onChange={(conditions) => updateNode(index, { conditions })} />
                        </div>
                        <div>
                          <FieldLabel>关联知识（进入后优先载入）</FieldLabel>
                          <div className="flex flex-wrap gap-2 max-h-36 overflow-auto">
                            {entries.length === 0 && <span className="text-xs text-white/30">没有知识条目</span>}
                            {entries.map((entry) => {
                              const checked = (node.knowledge_ids || []).includes(entry.id)
                              return (
                                <label key={entry.id} className={'text-[10px] px-2 py-1 rounded-lg border cursor-pointer ' + (checked ? 'border-white/40 text-white' : 'border-white/10 text-white/40')}>
                                  <input
                                    type="checkbox"
                                    className="hidden"
                                    checked={checked}
                                    onChange={() => updateNode(index, {
                                      knowledge_ids: checked
                                        ? node.knowledge_ids.filter((id) => id !== entry.id)
                                        : [...(node.knowledge_ids || []), entry.id],
                                    })}
                                  />
                                  {entry.title}
                                </label>
                              )
                            })}
                          </div>
                        </div>
                        <div className="flex items-center gap-3">
                          <button onClick={() => setEditing(null)} className="px-5 py-2 rounded-xl bg-white text-black text-xs font-bold">完成编辑</button>
                          <span className="text-[10px] text-white/30">修改在“保存故事线”时统一提交</span>
                        </div>
                      </div>
                    )}
                  </div>
                  )
                })}
              </div>

              <div className="rounded-2xl border border-amber-400/20 bg-amber-400/[0.03] p-4 flex flex-col gap-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <span className="text-sm font-bold text-amber-100">小事件包</span>
                  <button
                    onClick={() => updateStoryline({
                      packs: [...packs, { id: nextPackId(packs), title: '新事件包', summary: '', event_ids: [], requires: [], knowledge_ids: [], enabled: true, once: true }],
                    })}
                    className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:text-white"
                  >
                    新增事件包
                  </button>
                </div>
                <p className="text-[11px] text-white/35">事件包由世界里的若干事件组成；触发条件全部满足时启用，一次性包在任一事件触发后不再启用。</p>
                {packs.length === 0 && <p className="text-xs text-white/30">尚无事件包；节点可用「需要先触发的事件包」等待它们。</p>}
                {packs.map((pack, index) => (
                  <div key={index} className="rounded-xl border border-white/10 p-3 flex flex-col gap-3">
                    <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
                      <label>
                        <FieldLabel>ID</FieldLabel>
                        <input className={inputClass} value={pack.id} onChange={(e) => updatePack(index, { id: e.target.value })} />
                      </label>
                      <label>
                        <FieldLabel>名称</FieldLabel>
                        <input className={inputClass} value={pack.title} onChange={(e) => updatePack(index, { title: e.target.value })} />
                      </label>
                      <label>
                        <FieldLabel>摘要</FieldLabel>
                        <input className={inputClass} value={pack.summary || ''} onChange={(e) => updatePack(index, { summary: e.target.value })} />
                      </label>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-white/50 pt-5">
                        <label className="flex items-center gap-2">
                          <input type="checkbox" checked={pack.enabled !== false} onChange={(e) => updatePack(index, { enabled: e.target.checked })} />
                          启用
                        </label>
                        <label className="flex items-center gap-2">
                          <input type="checkbox" checked={pack.once !== false} onChange={(e) => updatePack(index, { once: e.target.checked })} />
                          一次性
                        </label>
                        <button
                          onClick={() => updateStoryline({
                            packs: packs.filter((_, i) => i !== index),
                            nodes: nodes.map((node) => node.requires_event_pack === pack.id ? { ...node, requires_event_pack: null } : node),
                          })}
                          className="ml-auto text-[10px] text-white/30 hover:text-rose-400"
                        >
                          移除
                        </button>
                      </div>
                    </div>
                    <label>
                      <FieldLabel>包含的事件 ID（逗号分隔）</FieldLabel>
                      <input className={inputClass} value={(pack.event_ids || []).join(', ')} onChange={(e) => updatePack(index, { event_ids: splitList(e.target.value) })} />
                    </label>
                    <div>
                      <FieldLabel>触发条件（全部满足时启用）</FieldLabel>
                      <ConditionRows value={pack.requires || []} onChange={(requires) => updatePack(index, { requires })} />
                    </div>
                  </div>
                ))}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <label>
                  <FieldLabel>故事线名称</FieldLabel>
                  <input className={inputClass} value={story.name || ''} onChange={(e) => updateStoryline({ name: e.target.value })} />
                </label>
                <label>
                  <FieldLabel>故事线说明</FieldLabel>
                  <input className={inputClass} value={story.description || ''} onChange={(e) => updateStoryline({ description: e.target.value })} />
                </label>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
