import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchKnowledgeLayers,
  fetchKnowledge,
  createKnowledge,
  updateKnowledge,
  deleteKnowledge,
  fetchPlayConfig,
  savePlayConfig,
  validateKnowledge,
  previewKnowledgeContext,
  generateKnowledgeDraft,
  fetchModelPresets,
  createModelPreset,
  updateModelPreset,
  deleteModelPreset,
} from '../api'

const EMPTY_ENTRY = {
  layer: 'global',
  title: '',
  content: '',
  tags: [],
  depth: 5,
  always_on: false,
  mode_scope: 'both',
  enabled: true,
  sort_order: 0,
  audience_npc_ids: [],
}


const EMPTY_PRESET = {
  name: '',
  role: 'narrator',
  provider: '',
  model: '',
  temperature: 0.8,
  max_tokens: 800,
  top_p: null,
  top_k: null,
  min_p: null,
  repetition_penalty: null,
  presence_penalty: null,
  frequency_penalty: null,
  seed: null,
  stop: [],
  system_prompt: '',
  context_window: 0,
  notes: '',
}

const SCOPE_LABELS = { both: '两种模式', storyline: '仅故事线', free: '仅自由模式' }
const ROLE_LABELS = { narrator: '叙事', intent: '意图解析', judge: '判定', dialogue: '对话', summary: '摘要', any: '通用' }
const CONDITION_KINDS = [
  ['flag', '剧情标记'],
  ['has_item', '持有物品'],
  ['item_count', '物品数量'],
  ['door_open', '门已开启'],
  ['at_location', '位于地点'],
  ['quest_status', '任务状态'],
  ['entity_at', '实体位于地点'],
  ['entity_alive', '实体存活'],
  ['combatant_hp', '实体生命值'],
]
const CONDITION_OPS = [
  ['eq', '等于'],
  ['ne', '不等于'],
  ['gte', '大于等于'],
  ['lte', '小于等于'],
  ['gt', '大于'],
  ['lt', '小于'],
]

function splitList(text) {
  return text
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean)
}



function TabButton({ active, children, onClick }) {
  return (
    <button
      onClick={onClick}
      className={
        'px-4 py-2 rounded-xl text-xs font-bold tracking-[0.15em] transition-all border ' +
        (active
          ? 'bg-white text-black border-white'
          : 'bg-white/5 text-white/60 border-white/5 hover:border-white/20 hover:text-white')
      }
    >
      {children}
    </button>
  )
}

function FieldLabel({ children }) {
  return (
    <span className="block text-[10px] font-mono uppercase tracking-[0.2em] text-white/40 mb-2">
      {children}
    </span>
  )
}

const inputClass =
  'w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2 text-sm text-white placeholder:text-white/20 focus:outline-none focus:border-white/30'

function KnowledgeEntryForm({ draft, setDraft, layers, onSubmit, onCancel, saving, submitLabel }) {
  const [tagsText, setTagsText] = useState((draft.tags || []).join(', '))

  const update = (patch) => setDraft({ ...draft, ...patch })

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 flex flex-col gap-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <label>
          <FieldLabel>知识分层</FieldLabel>
          <select
            className={inputClass}
            value={draft.layer}
            onChange={(e) => update({ layer: e.target.value })}
          >
            {layers.map((layer) => (
              <option key={layer.id} value={layer.id}>
                {layer.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <FieldLabel>标题</FieldLabel>
          <input
            className={inputClass}
            value={draft.title}
            onChange={(e) => update({ title: e.target.value })}
            placeholder="例如：北境的地理与传闻"
          />
        </label>
      </div>
      <label>
        <FieldLabel>内容（会被注入模型上下文，只作背景，不改变权威状态）</FieldLabel>
        <textarea
          className={inputClass + ' min-h-[120px] leading-relaxed'}
          value={draft.content}
          onChange={(e) => update({ content: e.target.value })}
          placeholder="写下玩家应当知道的世界设定、场景细节或人物小传。"
        />
      </label>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <label>
          <FieldLabel>深度（0 最外层，9 最内层）</FieldLabel>
          <input
            type="number"
            min="0"
            max="9"
            className={inputClass}
            value={draft.depth}
            onChange={(e) => update({ depth: Number(e.target.value) })}
          />
        </label>
        <label>
          <FieldLabel>标签（逗号分隔）</FieldLabel>
          <input
            className={inputClass}
            value={tagsText}
            onChange={(e) => {
              setTagsText(e.target.value)
              update({ tags: splitList(e.target.value) })
            }}
            placeholder="world, npc"
          />
        </label>
        <label>
          <FieldLabel>限定 NPC（逗号分隔，留空 = 所有场景）</FieldLabel>
          <input
            className={inputClass}
            value={(draft.audience_npc_ids || []).join(', ')}
            onChange={(e) => update({ audience_npc_ids: splitList(e.target.value) })}
            placeholder="guard, innkeeper"
          />
        </label>
        <label>
          <FieldLabel>模式范围</FieldLabel>
          <select
            className={inputClass}
            value={draft.mode_scope}
            onChange={(e) => update({ mode_scope: e.target.value })}
          >
            <option value="both">两种模式</option>
            <option value="storyline">仅故事线模式</option>
            <option value="free">仅自由模式</option>
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-5 text-xs text-white/60">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={draft.always_on}
            onChange={(e) => update({ always_on: e.target.checked })}
          />
          常驻（不受深度上限与模式过滤影响；事件层的小事件在自由模式下也始终开启）
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => update({ enabled: e.target.checked })}
          />
          启用
        </label>
      </div>
      <div className="flex items-center gap-3">
        <button
          onClick={onSubmit}
          disabled={saving}
          className="px-5 py-2 rounded-xl bg-white text-black text-xs font-bold tracking-[0.15em] hover:bg-gray-200 disabled:opacity-50"
        >
          {saving ? '保存中…' : submitLabel}
        </button>
        <button
          onClick={onCancel}
          className="px-5 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:border-white/30 hover:text-white"
        >
          取消
        </button>
      </div>
    </div>
  )
}

export default function KnowledgeStudio({ open, onClose, scenarioId, scenarioTitle, initialTab = 'knowledge' }) {
  const [tab, setTab] = useState(initialTab)
  const [layers, setLayers] = useState([])
  const [entries, setEntries] = useState([])
  const [config, setConfig] = useState(null)
  const [presets, setPresets] = useState([])
  const [draft, setDraft] = useState(EMPTY_ENTRY)
  const [editingId, setEditingId] = useState(null)
  const [creating, setCreating] = useState(false)
  const [savingEntry, setSavingEntry] = useState(false)
  const [savingConfig, setSavingConfig] = useState(false)
  const [errors, setErrors] = useState([])
  const [notice, setNotice] = useState('')
  const [preview, setPreview] = useState(null)
  const [previewFocus, setPreviewFocus] = useState('')
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiCount, setAiCount] = useState(6)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiDrafts, setAiDrafts] = useState([])
  const [presetDraft, setPresetDraft] = useState(EMPTY_PRESET)
  const [editingPresetId, setEditingPresetId] = useState(null)
  const [savingPreset, setSavingPreset] = useState(false)

  const loadAll = useCallback(async () => {
    setErrors([])
    try {
      const [layerData, entryData, configData, presetData] = await Promise.all([
        fetchKnowledgeLayers(),
        fetchKnowledge(scenarioId),
        fetchPlayConfig(scenarioId),
        fetchModelPresets(),
      ])
      setLayers(layerData.layers || [])
      setEntries(entryData || [])
      setConfig(configData)
      setPresets(presetData || [])
    } catch (err) {
      setErrors([err.message || '读取配置失败'])
    }
  }, [scenarioId])

  useEffect(() => {
    if (!open || !scenarioId) return
    setTab(initialTab)
    setPreview(null)
    setPreviewFocus('')
    setNotice('')
    setCreating(false)
    setEditingId(null)
    loadAll()
  }, [open, scenarioId, loadAll, initialTab])

  const grouped = useMemo(() => {
    const map = {}
    entries.forEach((entry) => {
      if (!map[entry.layer]) map[entry.layer] = []
      map[entry.layer].push(entry)
    })
    return map
  }, [entries])



  if (!open) return null

  const reportError = (err) => {
    if (Array.isArray(err.detail)) setErrors(err.detail)
    else if (typeof err.detail === 'string') setErrors([err.detail])
    else setErrors([err.message || '操作失败'])
  }

  const saveEntry = async () => {
    setSavingEntry(true)
    setErrors([])
    try {
      if (editingId) await updateKnowledge(editingId, draft)
      else await createKnowledge(scenarioId, draft)
      setCreating(false)
      setEditingId(null)
      setDraft(EMPTY_ENTRY)
      setNotice('知识条目已保存')
      await loadAll()
    } catch (err) {
      reportError(err)
    } finally {
      setSavingEntry(false)
    }
  }

  const removeEntry = async (entry) => {
    if (!confirm('删除知识条目「' + entry.title + '」？')) return
    setErrors([])
    try {
      await deleteKnowledge(entry.id)
      setNotice('已删除')
      await loadAll()
    } catch (err) {
      reportError(err)
    }
  }

  const saveConfig = async (patch) => {
    setSavingConfig(true)
    setErrors([])
    try {
      const next = { ...config, ...patch }
      const saved = await savePlayConfig(scenarioId, {
        mode: next.mode,
        storyline: next.storyline,
        max_depth: next.max_depth,
        context_budget_chars: next.context_budget_chars ?? 0,
        model_preset_id: next.model_preset_id,
        intent_preset_id: next.intent_preset_id ?? '',
        dialogue_preset_id: next.dialogue_preset_id ?? '',
        summary_preset_id: next.summary_preset_id ?? '',
        narrator_prompt: next.narrator_prompt ?? '',
      })
      setConfig(saved)
      setPreview(null)
      setNotice('运行模式已保存')
    } catch (err) {
      reportError(err)
    } finally {
      setSavingConfig(false)
    }
  }

  const runValidate = async () => {
    setErrors([])
    try {
      const result = await validateKnowledge(scenarioId)
      if (result.valid) setNotice('配置校验通过')
      else {
        setNotice('')
        setErrors(result.errors || [])
      }
    } catch (err) {
      reportError(err)
    }
  }

  const runPreview = async () => {
    setErrors([])
    try {
      const context = await previewKnowledgeContext(scenarioId, {
        mode: config.mode,
        maxDepth: config.max_depth,
        focus: splitList(previewFocus),
      })
      setPreview({
        ...context,
        blocks: Array.isArray(context?.blocks) ? context.blocks : [],
        stats: {
          ...(context?.stats || {}),
          excluded: Array.isArray(context?.stats?.excluded) ? context.stats.excluded : [],
        },
      })
    } catch (err) {
      reportError(err)
    }
  }

  const runAiKnowledge = async () => {
    if (!aiPrompt.trim() || aiBusy) return
    setAiBusy(true)
    setErrors([])
    setNotice('')
    try {
      const result = await generateKnowledgeDraft(scenarioId, {
        prompt: aiPrompt,
        count: aiCount,
      })
      setAiDrafts(result.entries || [])
      setNotice(`已生成 ${result.valid ?? 0} 条草稿，确认后再写入知识库`)
    } catch (err) {
      reportError(err)
    } finally {
      setAiBusy(false)
    }
  }

  const applyAiDrafts = async () => {
    const valid = aiDrafts.filter((item) => !item._error)
    if (valid.length === 0 || aiBusy) return
    setAiBusy(true)
    setErrors([])
    const pending = [...valid]
    let written = 0
    try {
      for (const item of valid) {
        await createKnowledge(scenarioId, item)
        pending.shift()
        written += 1
        setAiDrafts([...pending])
      }
      setAiDrafts([])
      setAiPrompt('')
      setNotice(`已写入 ${written} 条知识`)
      await loadAll()
    } catch (err) {
      setAiDrafts(pending)
      reportError(err)
      if (written > 0) {
        setNotice(`已写入 ${written} 条，剩余草稿保留；可直接重试`)
        await loadAll()
      }
    } finally {
      setAiBusy(false)
    }
  }

  const savePreset = async () => {
    setSavingPreset(true)
    setErrors([])
    try {
      if (editingPresetId) await updateModelPreset(editingPresetId, presetDraft)
      else await createModelPreset(presetDraft)
      setEditingPresetId(null)
      setPresetDraft(EMPTY_PRESET)
      setNotice('模型预设已保存')
      await loadAll()
    } catch (err) {
      reportError(err)
    } finally {
      setSavingPreset(false)
    }
  }

  const removePreset = async (preset) => {
    if (!confirm('删除预设「' + preset.name + '」？')) return
    setErrors([])
    try {
      await deleteModelPreset(preset.id)
      setNotice('已删除预设')
      await loadAll()
    } catch (err) {
      reportError(err)
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/80 flex items-start justify-center overflow-y-auto p-4 sm:p-8">
      <div className="w-full max-w-5xl bg-[#0b0b0b] border border-white/10 rounded-[2rem] shadow-2xl">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-6 border-b border-white/10">
          <div className="flex items-start gap-4">
            <img
              src="/self-reference-mark.png"
              alt=""
              aria-hidden="true"
              width="44"
              height="44"
              className="mt-1 opacity-70 shrink-0"
              draggable="false"
            />
            <div>
              <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-white/30 mb-2">
                知识 · 模式 · 预设
              </div>
              <h2 className="text-2xl font-bold text-white">{scenarioTitle || '情景配置'}</h2>
              <p className="text-xs text-white/40 mt-2">
                知识只作为叙事背景注入；模式的推进由权威状态与规则决定。
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="px-5 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:border-white/30 hover:text-white self-start"
          >
            关闭
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3 px-6 pt-5">
          <TabButton active={tab === 'knowledge'} onClick={() => setTab('knowledge')}>
            知识库
          </TabButton>
          <TabButton active={tab === 'mode'} onClick={() => setTab('mode')}>
            运行模式与故事线
          </TabButton>
          <TabButton active={tab === 'presets'} onClick={() => setTab('presets')}>
            模型预设
          </TabButton>
        </div>

        {(notice || errors.length > 0) && (
          <div className="px-6 pt-4">
            {notice && (
              <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 text-emerald-300 text-xs px-4 py-2">
                {notice}
              </div>
            )}
            {errors.length > 0 && (
              <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 text-rose-300 text-xs px-4 py-3 mt-2">
                <div className="font-bold mb-1">操作未通过校验</div>
                <ul className="list-disc pl-5 space-y-1">
                  {errors.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <div className="p-6">
          {tab === 'knowledge' && (
            <div className="flex flex-col gap-5">
              <div className="rounded-2xl border border-cyan-400/20 bg-cyan-400/[0.04] p-4 flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-sm font-bold text-cyan-100">AI 生成知识草稿</span>
                  <span className="text-[10px] text-white/35">生成后先预览，确认再写入</span>
                </div>
                <textarea
                  className={inputClass + ' min-h-[80px]'}
                  value={aiPrompt}
                  onChange={(e) => setAiPrompt(e.target.value)}
                  placeholder="例如：为雾港写一组世界总览、酒馆场景、守夜人和一个夜间事件。"
                />
                <div className="flex flex-wrap items-center gap-3">
                  <label className="text-xs text-white/50 flex items-center gap-2">
                    条数
                    <input
                      type="number"
                      min="1"
                      max="20"
                      className={inputClass + ' w-20'}
                      value={aiCount}
                      onChange={(e) => setAiCount(Number(e.target.value))}
                    />
                  </label>
                  <button
                    onClick={runAiKnowledge}
                    disabled={aiBusy || !aiPrompt.trim()}
                    className="px-4 py-2 rounded-xl bg-cyan-400 text-black text-xs font-bold hover:bg-cyan-300 disabled:opacity-40"
                  >
                    {aiBusy ? '生成中…' : '生成草稿'}
                  </button>
                  {aiDrafts.length > 0 && (
                    <>
                      <button onClick={applyAiDrafts} disabled={aiBusy} className="px-4 py-2 rounded-xl border border-emerald-400/40 text-emerald-200 text-xs hover:bg-emerald-400/10">
                        全部写入知识库
                      </button>
                      <button onClick={() => setAiDrafts([])} className="text-xs text-white/40 hover:text-white">清空</button>
                    </>
                  )}
                </div>
                {aiDrafts.length > 0 && (
                  <div className="flex flex-col gap-2 max-h-72 overflow-auto">
                    {aiDrafts.map((item, index) => (
                      <div key={index} className="rounded-xl border border-white/10 bg-black/20 p-3 text-xs">
                        {item._error ? (
                          <span className="text-rose-300">{item._error}</span>
                        ) : (
                          <>
                            <div className="flex flex-wrap items-center gap-2 text-white/70">
                              <span className="font-bold">{item.title}</span>
                              <span className="text-white/30">[{item.layer}] 深度 {item.depth}</span>
                              {(item.tags || []).map((tag) => <span key={tag} className="text-white/30">#{tag}</span>)}
                            </div>
                            <p className="mt-1 text-white/45 whitespace-pre-wrap">{item.content}</p>
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-white/40 leading-relaxed max-w-2xl">
                  深度链从 0 到 9：数值越小越先载入，超出运行模式上限的条目会被跳过（常驻条目除外）。
                  自由模式下「仅故事线」条目不会被载入，但事件层的小事件只要设为常驻就始终开启。
                </p>
                <button
                  onClick={() => {
                    setCreating(true)
                    setEditingId(null)
                    setDraft(EMPTY_ENTRY)
                  }}
                  className="px-4 py-2 rounded-xl bg-white text-black text-xs font-bold tracking-[0.15em] hover:bg-gray-200"
                >
                  新增知识
                </button>
              </div>

              {(creating || editingId) && (
                <KnowledgeEntryForm
                  key={editingId || 'new'}
                  draft={draft}
                  setDraft={setDraft}
                  layers={layers}
                  saving={savingEntry}
                  submitLabel={editingId ? '保存修改' : '创建条目'}
                  onSubmit={saveEntry}
                  onCancel={() => {
                    setCreating(false)
                    setEditingId(null)
                    setErrors([])
                  }}
                />
              )}

              {entries.length === 0 && (
                <div className="rounded-2xl border border-dashed border-white/10 py-10 text-center text-white/30 text-xs">
                  还没有知识条目。先写一条世界总览，再逐步补充场景与人物。
                </div>
              )}

              {layers.map((layer) => {
                const list = grouped[layer.id] || []
                if (list.length === 0) return null
                return (
                  <div key={layer.id} className="flex flex-col gap-3">
                    <div className="flex items-center gap-3 text-[10px] font-mono uppercase tracking-[0.25em] text-white/40">
                      <span>{layer.label}</span>
                      <span className="text-white/20">{list.length}</span>
                      <div className="flex-1 h-px bg-white/5" />
                    </div>
                    {list.map((entry) => (
                      <div
                        key={entry.id}
                        className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 flex flex-col gap-3"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-bold text-white">{entry.title}</span>
                          <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-white/10 text-white/50">
                            深度 {entry.depth}
                          </span>
                          <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-white/10 text-white/50">
                            {SCOPE_LABELS[entry.mode_scope] || entry.mode_scope}
                          </span>
                          {entry.always_on && (
                            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-amber-500/40 text-amber-400">
                              常驻
                            </span>
                          )}
                          {(entry.audience_npc_ids || []).length > 0 && (
                            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-cyan-500/40 text-cyan-300">
                              仅 {entry.audience_npc_ids.join('、')}
                            </span>
                          )}
                          {!entry.enabled && (
                            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-rose-500/40 text-rose-400">
                              已停用
                            </span>
                          )}
                          <div className="flex-1" />
                          <button
                            onClick={() => {
                              setDraft({
                                layer: entry.layer,
                                title: entry.title,
                                content: entry.content,
                                tags: entry.tags || [],
                                depth: entry.depth,
                                always_on: entry.always_on,
                                mode_scope: entry.mode_scope,
                                enabled: entry.enabled,
                                sort_order: entry.sort_order,
                                audience_npc_ids: entry.audience_npc_ids || [],
                              })
                              setEditingId(entry.id)
                              setCreating(false)
                              setErrors([])
                            }}
                            className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
                          >
                            编辑
                          </button>
                          <button
                            onClick={() => removeEntry(entry)}
                            className="text-[10px] text-white/30 hover:text-rose-400 tracking-[0.15em] uppercase"
                          >
                            删除
                          </button>
                        </div>
                        <p className="text-xs text-white/50 leading-relaxed whitespace-pre-wrap">
                          {entry.content}
                        </p>
                        {entry.tags && entry.tags.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {entry.tags.map((tag) => (
                              <span
                                key={tag}
                                className="text-[10px] font-mono text-white/30 border border-white/5 rounded-md px-2 py-0.5"
                              >
                                {tag}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )
              })}
            </div>
          )}

          {tab === 'mode' && config && (
            <div className="flex flex-col gap-6">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <button
                  onClick={() => setConfig({ ...config, mode: 'free' })}
                  className={
                    'text-left p-5 rounded-2xl border transition-all ' +
                    (config.mode === 'free'
                      ? 'border-white/40 bg-white/[0.07]'
                      : 'border-white/10 bg-white/[0.02] hover:border-white/20')
                  }
                >
                  <div className="text-sm font-bold text-white mb-1">自由模式</div>
                  <p className="text-xs text-white/50 leading-relaxed">
                    不设主故事线，玩家可以自由探索；世界知识与常驻小事件仍然生效。
                  </p>
                </button>
                <button
                  onClick={() => setConfig({ ...config, mode: 'storyline' })}
                  className={
                    'text-left p-5 rounded-2xl border transition-all ' +
                    (config.mode === 'storyline'
                      ? 'border-white/40 bg-white/[0.07]'
                      : 'border-white/10 bg-white/[0.02] hover:border-white/20')
                  }
                >
                  <div className="text-sm font-bold text-white mb-1">故事线模式</div>
                  <p className="text-xs text-white/50 leading-relaxed">
                    按主干线与分支推进；节点只由前置节点与已满足条件进入，模型无法自行跳转。
                  </p>
                </button>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                <label>
                  <FieldLabel>深度上限（0-9）</FieldLabel>
                  <input
                    type="number"
                    min="0"
                    max="9"
                    className={inputClass}
                    value={config.max_depth}
                    onChange={(e) => setConfig({ ...config, max_depth: Number(e.target.value) })}
                  />
                </label>
                <label>
                  <FieldLabel>上下文预算（字符，0 = 不限）</FieldLabel>
                  <input
                    type="number"
                    min="0"
                    className={inputClass}
                    value={config.context_budget_chars ?? 0}
                    onChange={(e) => setConfig({ ...config, context_budget_chars: Number(e.target.value) })}
                  />
                </label>
                <label>
                  <FieldLabel>对话模型预设（talk，role = dialogue/any）</FieldLabel>
                  <select
                    className={inputClass}
                    value={config.dialogue_preset_id || ''}
                    onChange={(e) => setConfig({ ...config, dialogue_preset_id: e.target.value })}
                  >
                    <option value="">跟随全局设置</option>
                    {presets
                      .filter((preset) => preset.role === 'dialogue' || preset.role === 'any')
                      .map((preset) => (
                        <option key={preset.id} value={preset.id}>
                          {preset.name}（{ROLE_LABELS[preset.role] || preset.role}）
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  <FieldLabel>摘要模型预设（每 4 回合，role = summary/any）</FieldLabel>
                  <select
                    className={inputClass}
                    value={config.summary_preset_id || ''}
                    onChange={(e) => setConfig({ ...config, summary_preset_id: e.target.value })}
                  >
                    <option value="">不启用模型摘要</option>
                    {presets
                      .filter((preset) => preset.role === 'summary' || preset.role === 'any')
                      .map((preset) => (
                        <option key={preset.id} value={preset.id}>
                          {preset.name}（{ROLE_LABELS[preset.role] || preset.role}）
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  <FieldLabel>叙事模型预设</FieldLabel>
                  <select
                    className={inputClass}
                    value={config.model_preset_id || ''}
                    onChange={(e) => setConfig({ ...config, model_preset_id: e.target.value })}
                  >
                    <option value="">跟随全局设置</option>
                    {presets.map((preset) => (
                      <option key={preset.id} value={preset.id}>
                        {preset.name}（{ROLE_LABELS[preset.role] || preset.role}）
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <label>
                  <FieldLabel>意图解析预设（role = intent/any）</FieldLabel>
                  <select
                    className={inputClass}
                    value={config.intent_preset_id || ''}
                    onChange={(e) => setConfig({ ...config, intent_preset_id: e.target.value })}
                  >
                    <option value="">跟随全局设置</option>
                    {presets
                      .filter((preset) => preset.role === 'intent' || preset.role === 'any')
                      .map((preset) => (
                        <option key={preset.id} value={preset.id}>
                          {preset.name}（{ROLE_LABELS[preset.role] || preset.role}）
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  <FieldLabel>叙事提示词模板（追加到系统规则前，不能推翻规则结果）</FieldLabel>
                  <textarea
                    className={inputClass + ' min-h-[80px]'}
                    value={config.narrator_prompt || ''}
                    onChange={(e) => setConfig({ ...config, narrator_prompt: e.target.value })}
                    placeholder="例如：使用冷峻、短句的第三人称叙述。"
                  />
                </label>
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4 text-xs text-white/50">
                故事线节点、分组、停用与 AI 生成已移至独立「故事线工坊」；
                这里保留运行模式、知识预算与模型预设。
              </div>

              <div className="flex flex-wrap items-end gap-3">
                <label className="flex-1 min-w-[240px]">
                  <FieldLabel>预览聚焦词（逗号分隔，如当前位置/人物 id）</FieldLabel>
                  <input
                    className={inputClass}
                    value={previewFocus}
                    onChange={(e) => setPreviewFocus(e.target.value)}
                    placeholder="tavern, guard"
                  />
                </label>
                <button
                  onClick={() => saveConfig({})}
                  disabled={savingConfig}
                  className="px-5 py-2 rounded-xl bg-white text-black text-xs font-bold tracking-[0.15em] hover:bg-gray-200 disabled:opacity-50"
                >
                  {savingConfig ? '保存中…' : '保存运行模式'}
                </button>
                <button
                  onClick={runValidate}
                  className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:border-white/30 hover:text-white"
                >
                  校验配置
                </button>
                <button
                  onClick={runPreview}
                  className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:border-white/30 hover:text-white"
                >
                  预览已保存配置的知识
                </button>
              </div>

              {preview && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 flex flex-col gap-3">
                  <div className="text-xs text-white/60">
                    模式 {preview.stats.mode === 'free' ? '自由' : '故事线'} · 载入{' '}
                    {preview.stats.included}/{preview.stats.total} 条 · 深度上限{' '}
                    {preview.stats.max_depth}
                  </div>
                  <p className="text-xs text-white/40">优先知识来源节点：{(preview.story_nodes || []).join('、') || '无'}</p>
                  <div className="flex flex-col gap-2">
                    {preview.blocks.map((block) => (
                      <div key={block.entry_id} className="text-xs text-white/70">
                        <span className="text-white/30 font-mono mr-2">[{block.layer}]</span>
                        {block.title}
                        <span className="text-white/30 font-mono ml-2">深度 {block.depth}</span>
                      </div>
                    ))}
                    {preview.blocks.length === 0 && (
                      <span className="text-xs text-white/30">没有条目会被载入</span>
                    )}
                  </div>
                  {preview.stats.excluded.length > 0 && (
                    <div className="text-xs text-white/40">
                      <p>另有 {preview.stats.excluded.length} 条被排除</p>
                      <ul className="mt-2 list-disc pl-5 space-y-1">
                        {preview.stats.excluded.map((entry) => <li key={entry.entry_id}>{entry.title}：{entry.reason}</li>)}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {tab === 'presets' && (
            <div className="flex flex-col gap-5">
              <p className="text-xs text-white/40 leading-relaxed">
                预设参考 SillyTavern 的采样器：温度、top_p/top_k、min_p、重复与存在惩罚、停止词和系统提示。所有参数只影响生成，不会绕过规则判定。
              </p>

              <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 flex flex-col gap-4">
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                  <label>
                    <FieldLabel>名称</FieldLabel>
                    <input
                      className={inputClass}
                      value={presetDraft.name}
                      onChange={(e) => setPresetDraft({ ...presetDraft, name: e.target.value })}
                    />
                  </label>
                  <label>
                    <FieldLabel>用途</FieldLabel>
                    <select
                      className={inputClass}
                      value={presetDraft.role}
                      onChange={(e) => setPresetDraft({ ...presetDraft, role: e.target.value })}
                    >
                      <option value="narrator">叙事</option>
                      <option value="intent">意图解析</option>
                      <option value="dialogue">对话</option>
                      <option value="summary">摘要</option>
                      <option value="judge">判定</option>
                      <option value="any">通用</option>
                    </select>
                  </label>
                  <label>
                    <FieldLabel>供应商（留空跟随全局）</FieldLabel>
                    <input
                      className={inputClass}
                      value={presetDraft.provider}
                      onChange={(e) => setPresetDraft({ ...presetDraft, provider: e.target.value })}
                      placeholder="openai / local / anthropic"
                    />
                  </label>
                  <label>
                    <FieldLabel>模型（留空跟随全局）</FieldLabel>
                    <input
                      className={inputClass}
                      value={presetDraft.model}
                      onChange={(e) => setPresetDraft({ ...presetDraft, model: e.target.value })}
                    />
                  </label>
                  <label>
                    <FieldLabel>温度</FieldLabel>
                    <input
                      type="number"
                      step="0.05"
                      min="0"
                      max="2"
                      className={inputClass}
                      value={presetDraft.temperature}
                      onChange={(e) =>
                        setPresetDraft({ ...presetDraft, temperature: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label>
                    <FieldLabel>最大输出 token</FieldLabel>
                    <input
                      type="number"
                      min="1"
                      className={inputClass}
                      value={presetDraft.max_tokens}
                      onChange={(e) =>
                        setPresetDraft({ ...presetDraft, max_tokens: Number(e.target.value) })
                      }
                    />
                  </label>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
                  {[
                    ['top_p', 'top_p（0-1）', 0, 1, 0.05],
                    ['top_k', 'top_k（0-1000）', 0, 1000, 1],
                    ['min_p', 'min_p（0-1）', 0, 1, 0.01],
                    ['repetition_penalty', '重复惩罚（0-3）', 0, 3, 0.05],
                    ['presence_penalty', '存在惩罚（-2~2）', -2, 2, 0.1],
                    ['frequency_penalty', '频率惩罚（-2~2）', -2, 2, 0.1],
                    ['seed', '随机种子', 0, 2147483647, 1],
                    ['context_window', '上下文窗口', 0, 2000000, 1024],
                  ].map(([key, label, min, max, step]) => (
                    <label key={key}>
                      <FieldLabel>{label}</FieldLabel>
                      <input
                        type="number"
                        className={inputClass}
                        min={min}
                        max={max}
                        step={step}
                        value={presetDraft[key] ?? ''}
                        placeholder="留空跟随默认"
                        onChange={(e) => setPresetDraft({
                          ...presetDraft,
                          [key]: e.target.value === '' ? null : Number(e.target.value),
                        })}
                      />
                    </label>
                  ))}
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <label>
                    <FieldLabel>停止词（每行一个）</FieldLabel>
                    <textarea
                      className={inputClass + ' min-h-[80px]'}
                      value={(presetDraft.stop || []).join('\n')}
                      onChange={(e) => setPresetDraft({
                        ...presetDraft,
                        stop: e.target.value.split('\n').map((line) => line.trim()).filter(Boolean),
                      })}
                      placeholder={'\nUser:\n###'}
                    />
                  </label>
                  <label>
                    <FieldLabel>预设系统提示（追加到规则前，不能推翻规则）</FieldLabel>
                    <textarea
                      className={inputClass + ' min-h-[80px]'}
                      value={presetDraft.system_prompt || ''}
                      onChange={(e) => setPresetDraft({ ...presetDraft, system_prompt: e.target.value })}
                      placeholder="保持冷峻、短句、第三人称。"
                    />
                  </label>
                </div>
                <label>
                  <FieldLabel>备注</FieldLabel>
                  <input
                    className={inputClass}
                    value={presetDraft.notes}
                    onChange={(e) => setPresetDraft({ ...presetDraft, notes: e.target.value })}
                  />
                </label>
                <div className="flex items-center gap-3">
                  <button
                    onClick={savePreset}
                    disabled={savingPreset}
                    className="px-5 py-2 rounded-xl bg-white text-black text-xs font-bold tracking-[0.15em] hover:bg-gray-200 disabled:opacity-50"
                  >
                    {savingPreset ? '保存中…' : editingPresetId ? '保存预设' : '创建预设'}
                  </button>
                  {editingPresetId && (
                    <button
                      onClick={() => {
                        setEditingPresetId(null)
                        setPresetDraft(EMPTY_PRESET)
                      }}
                      className="px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:border-white/30 hover:text-white"
                    >
                      取消编辑
                    </button>
                  )}
                </div>
              </div>

              <div className="flex flex-col gap-3">
                {presets.map((preset) => (
                  <div
                    key={preset.id}
                    className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 flex flex-wrap items-center gap-3"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-bold text-white">{preset.name}</span>
                        {preset.is_builtin && (
                          <span className="text-[10px] font-mono px-2 py-0.5 rounded-full border border-white/10 text-white/40">
                            内置
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] font-mono text-white/40 mt-1">
                        {ROLE_LABELS[preset.role] || preset.role} · {preset.provider || '全局供应商'} ·{' '}
                        {preset.model || '全局模型'} · 温度 {preset.temperature} · 上限{' '}
                        {preset.max_tokens}
                      </div>
                    </div>
                    <div className="flex-1" />
                    <button
                      onClick={() => {
                        setEditingPresetId(preset.id)
                        setPresetDraft({
                          name: preset.name,
                          role: preset.role,
                          provider: preset.provider,
                          model: preset.model,
                          temperature: preset.temperature,
                          max_tokens: preset.max_tokens,
                          top_p: preset.top_p ?? null,
                          top_k: preset.top_k ?? null,
                          min_p: preset.min_p ?? null,
                          repetition_penalty: preset.repetition_penalty ?? null,
                          presence_penalty: preset.presence_penalty ?? null,
                          frequency_penalty: preset.frequency_penalty ?? null,
                          seed: preset.seed ?? null,
                          stop: preset.stop || [],
                          system_prompt: preset.system_prompt || '',
                          context_window: preset.context_window ?? 0,
                          notes: preset.notes || '',
                        })
                      }}
                      className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
                    >
                      编辑
                    </button>
                    {!preset.is_builtin && (
                      <button
                        onClick={() => removePreset(preset)}
                        className="text-[10px] text-white/30 hover:text-rose-400 tracking-[0.15em] uppercase"
                      >
                        删除
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}