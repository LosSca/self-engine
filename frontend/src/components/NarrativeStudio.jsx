import { useCallback, useEffect, useState } from 'react'
import { X, RefreshCw, Trash2, Plus, Save } from 'lucide-react'
import {
  fetchPrefs, savePrefs,
  fetchIntentLearnings, addIntentLearning, updateIntentLearning,
  deleteIntentLearning, clearIntentLearnings,
} from '../api'

// 这一面板是叙事层与选项层的「参数面」：以前这些值写死在代码里，现在全部
// 落在后端 prefs，改完作用于下一次行动，不需要重启也不需要改配置。
// 学习表是意图顾问的持久化结果，在这里可以直接看、改、删。
const TABS = [
  { id: 'narrator', label: '叙事' },
  { id: 'options', label: '选项' },
  { id: 'learned', label: '学习表' },
]

const STYLE_OPTIONS = [
  { id: '', label: '跟随世界' },
  { id: 'concise', label: '简洁' },
  { id: 'balanced', label: '均衡' },
  { id: 'literary', label: '文学' },
  { id: 'gothic', label: '阴郁' },
  { id: 'wuxia', label: '武侠' },
  { id: 'pulp', label: '通俗冒险' },
]
const PERSPECTIVE_OPTIONS = [
  { id: '', label: '跟随世界' },
  { id: 'second', label: '第二人称' },
  { id: 'third', label: '第三人称' },
  { id: 'first', label: '第一人称' },
]
const LENGTH_OPTIONS = [
  { id: '', label: '跟随世界' },
  { id: 'short', label: '简短' },
  { id: 'medium', label: '标准' },
  { id: 'long', label: '铺陈' },
]

// 常见动作名，作为学习表「动作」一栏的提示（引擎的动作名就是这些英文键）。
const ACTION_HINTS = 'move 移动 / open 打开 / take 拿起 / use 使用 / talk 说话 / attack 攻击 / look 查看 / investigate 检查 / equip 装备 / give 给出'

const C = {
  label: 'block text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c] mb-1.5',
  input:
    'w-full bg-white border border-[#d8d4cc] rounded-lg px-3 py-2 text-sm text-[#1a1a1a] '
    + 'placeholder-[#a8a29a] focus:outline-none focus:border-[#8a857c] transition-colors',
  card: 'bg-white border border-[#e2ded6] rounded-xl p-3',
  help: 'text-[10px] leading-relaxed text-[#8a857c] mt-1.5',
  primary:
    'bg-[#3f3a34] text-white hover:bg-[#3a3733] rounded-lg px-4 py-2 text-xs font-bold '
    + 'tracking-[0.15em] transition-colors disabled:opacity-50',
  ghost:
    'border border-[#d8d4cc] text-[#5c574f] hover:border-[#8a857c] hover:text-[#1a1a1a] '
    + 'rounded-lg px-3 py-2 text-xs transition-colors disabled:opacity-50',
  tab: (on) =>
    'px-3 py-1.5 rounded-lg text-xs font-bold tracking-wide transition-colors '
    + (on ? 'bg-[#3f3a34] text-white' : 'text-[#6b665e] hover:bg-[#e6e2da]'),
}

// 空串表示「交给世界/预设决定」；填了就 clamp 到安全范围，避免出现 0 token 这类值。
const toNum = (raw, min, max) => {
  const s = String(raw === undefined || raw === null ? '' : raw).trim()
  if (!s) return ''
  const v = Number.parseFloat(s)
  if (!Number.isFinite(v)) return ''
  return Math.min(max, Math.max(min, v))
}

const DEFAULTS = {
  suggestions_count: 3,
  suggestions_temperature: 0.8,
  suggestions_max_tokens: 400,
  suggestions_timeout: 12,
  suggestions_allow_speech: true,
  suggestions_allow_investigate: true,
  suggestions_use_story: true,
  narrator_history_turns: 6,
}

function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className={C.label}>{label}</span>
      {children}
      {hint ? <span className={C.help}>{hint}</span> : null}
    </label>
  )
}

function Toggle({ label, hint, checked, onChange }) {
  const cls =
    'flex items-start gap-3 rounded-lg border px-3 py-2 transition-colors cursor-pointer '
    + (checked ? 'border-[#8a857c] bg-white' : 'border-[#e2ded6] bg-[#fbfaf7]')
  return (
    <label className={cls}>
      <input
        type="checkbox"
        className="mt-0.5 accent-[#3f3a34]"
        checked={!!checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        <span className="block text-xs font-semibold text-[#3f3a34]">{label}</span>
        {hint ? <span className="block text-[10px] leading-relaxed text-[#8a857c] mt-1">{hint}</span> : null}
      </span>
    </label>
  )
}

export default function NarrativeStudio({ open = true, onClose, context }) {
  const [tab, setTab] = useState('narrator')
  const [draft, setDraft] = useState({ ...DEFAULTS })
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  // 学习表
  const worldId = context?.engineState?.world_id || ''
  const [scope, setScope] = useState(worldId)
  const [rows, setRows] = useState([])
  const [rowsBusy, setRowsBusy] = useState(false)
  const [rowsError, setRowsError] = useState('')
  const [editingId, setEditingId] = useState(null)
  const [editAction, setEditAction] = useState('')
  const [editFragment, setEditFragment] = useState('')
  const [newRow, setNewRow] = useState({ phrase: '', action: '', fragment: '' })
  const [confirmClear, setConfirmClear] = useState(false)

  const setField = (key, value) => setDraft((prev) => ({ ...prev, [key]: value }))

  useEffect(() => {
    if (!worldId) return
    setScope((prev) => prev || worldId)
  }, [worldId])

  useEffect(() => {
    let alive = true
    fetchPrefs()
      .then((data) => {
        if (!alive) return
        const prefs = data?.prefs || {}
        const next = { ...DEFAULTS }
        Object.keys(prefs).forEach((key) => { next[key] = prefs[key] })
        setDraft(next)
      })
      .catch((err) => {
        if (!alive) return
        setError(String(err?.message || err))
      })
    return () => { alive = false }
  }, [])

  const flash = (text) => {
    setNotice(text)
    setError('')
    setTimeout(() => setNotice(''), 2600)
  }

  const save = async (payload) => {
    setBusy(true)
    setError('')
    try {
      await savePrefs(payload)
      flash('已保存，下一次行动生效')
    } catch (err) {
      setError(String(err?.message || err))
    } finally {
      setBusy(false)
    }
  }

  const saveNarrator = () => save({
    narrator_style: draft.narrator_style || '',
    narrator_perspective: draft.narrator_perspective || '',
    narrator_length: draft.narrator_length || '',
    narrator_language_hint: draft.narrator_language_hint || '',
    narrator_extra_rules: draft.narrator_extra_rules || '',
    narrator_max_tokens: toNum(draft.narrator_max_tokens, 60, 4000),
    narrator_temperature: toNum(draft.narrator_temperature, 0, 2),
    narrator_timeout: toNum(draft.narrator_timeout, 2, 120),
    narrator_history_turns: toNum(draft.narrator_history_turns, 1, 40) || 6,
    narrator_show_rules: !!draft.narrator_show_rules,
    narrator_allow_ooc: !!draft.narrator_allow_ooc,
    top_p: toNum(draft.top_p, 0, 1),
    presence_penalty: toNum(draft.presence_penalty, -2, 2),
    frequency_penalty: toNum(draft.frequency_penalty, -2, 2),
  })

  const saveOptions = () => save({
    suggestions_count: toNum(draft.suggestions_count, 2, 5) || 3,
    suggestions_temperature: toNum(draft.suggestions_temperature, 0, 2),
    suggestions_max_tokens: toNum(draft.suggestions_max_tokens, 60, 2000),
    suggestions_timeout: toNum(draft.suggestions_timeout, 2, 120),
    suggestions_extra_rules: draft.suggestions_extra_rules || '',
    suggestions_allow_speech: !!draft.suggestions_allow_speech,
    suggestions_allow_investigate: !!draft.suggestions_allow_investigate,
    suggestions_use_story: !!draft.suggestions_use_story,
  })

  const loadRows = useCallback(async (target) => {
    setRowsBusy(true)
    setRowsError('')
    try {
      const list = await fetchIntentLearnings(target || '')
      setRows(list)
    } catch (err) {
      setRowsError(String(err?.message || err))
      setRows([])
    } finally {
      setRowsBusy(false)
    }
  }, [])

  useEffect(() => {
    if (tab === 'learned' && open) loadRows(scope.trim())
  }, [tab, open, scope, loadRows])

  const addRow = async () => {
    const phrase = newRow.phrase.trim()
    const action = newRow.action.trim()
    if (!phrase || !action) {
      setRowsError('手写一条记录至少要填「玩家怎么说」和「动作名」')
      return
    }
    if (!scope.trim()) {
      setRowsError('先填世界 id：学习按世界隔离')
      return
    }
    setRowsBusy(true)
    setRowsError('')
    try {
      await addIntentLearning({
        scope_key: scope.trim(),
        phrase,
        action,
        fragment: newRow.fragment.trim(),
        lang: /[\u4e00-\u9fff]/.test(phrase) ? 'zh' : '',
        confidence: 1.0,
      })
      setNewRow({ phrase: '', action: '', fragment: '' })
      await loadRows(scope.trim())
    } catch (err) {
      setRowsError(String(err?.message || err))
    } finally {
      setRowsBusy(false)
    }
  }

  const startEdit = (row) => {
    setEditingId(row.id)
    setEditAction(row.action || '')
    setEditFragment(row.fragment || '')
  }

  const commitEdit = async () => {
    if (editingId === null) return
    setRowsBusy(true)
    setRowsError('')
    try {
      await updateIntentLearning(editingId, {
        action: editAction.trim(),
        fragment: editFragment.trim(),
      })
      setEditingId(null)
      await loadRows(scope.trim())
    } catch (err) {
      setRowsError(String(err?.message || err))
    } finally {
      setRowsBusy(false)
    }
  }

  const removeRow = async (id) => {
    setRowsBusy(true)
    setRowsError('')
    try {
      await deleteIntentLearning(id)
      await loadRows(scope.trim())
    } catch (err) {
      setRowsError(String(err?.message || err))
    } finally {
      setRowsBusy(false)
    }
  }

  const clearScope = async () => {
    if (!confirmClear) {
      setConfirmClear(true)
      setTimeout(() => setConfirmClear(false), 4000)
      return
    }
    setConfirmClear(false)
    if (!scope.trim()) return
    setRowsBusy(true)
    setRowsError('')
    try {
      await clearIntentLearnings(scope.trim())
      await loadRows(scope.trim())
    } catch (err) {
      setRowsError(String(err?.message || err))
    } finally {
      setRowsBusy(false)
    }
  }

  if (!open) return null

  return (
    <div className="p-3 flex flex-col gap-3" data-testid="narrative-studio">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-bold tracking-wide text-[#1a1a1a]">叙事与选项</h2>
          <p className="text-[10px] text-[#8a857c] mt-1">改完即用，作用于下一次行动</p>
        </div>
        {onClose ? (
          <button type="button" onClick={onClose} className="p-1.5 rounded-lg text-[#8a857c] hover:text-[#1a1a1a] hover:bg-[#e6e2da]">
            <X size={14} />
          </button>
        ) : null}
      </div>

      <div className="flex items-center gap-1 bg-[#e6e2da] rounded-xl p-1" data-testid="narrative-tabs">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            data-testid={'narrative-tab-' + item.id}
            className={C.tab(tab === item.id)}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {notice ? <div className="text-[11px] text-emerald-700">{notice}</div> : null}
      {error ? <div className="text-[11px] text-red-600">{error}</div> : null}

      {tab === 'narrator' && (
        <div className="flex flex-col gap-3">
          <div className={C.card}>
            <div className="text-[11px] font-bold text-[#3f3a34] mb-2">叙述风格</div>
            <div className="flex flex-col gap-2.5">
              <Field label="文风" hint="只影响语气与句式，不改变规则判定。">
                <select className={C.input} value={draft.narrator_style || ''}
                  onChange={(e) => setField('narrator_style', e.target.value)}>
                  {STYLE_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="人称">
                <select className={C.input} value={draft.narrator_perspective || ''}
                  onChange={(e) => setField('narrator_perspective', e.target.value)}>
                  {PERSPECTIVE_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="篇幅">
                <select className={C.input} value={draft.narrator_length || ''}
                  onChange={(e) => setField('narrator_length', e.target.value)}>
                  {LENGTH_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="语言提示" hint="例如「用简洁的中文」「每段不超过三句」。">
                <input className={C.input} value={draft.narrator_language_hint || ''}
                  placeholder="跟随世界"
                  onChange={(e) => setField('narrator_language_hint', e.target.value)} />
              </Field>
              <Field label="额外写作要求" hint="追加在风格说明之后，与预设同等重要；最多 800 字。">
                <textarea className={C.input} rows={4} value={draft.narrator_extra_rules || ''}
                  placeholder="例如：多用短句，少用形容词，不要替玩家决定感受。"
                  onChange={(e) => setField('narrator_extra_rules', e.target.value)} />
              </Field>
            </div>
          </div>

          <div className={C.card}>
            <div className="text-[11px] font-bold text-[#3f3a34] mb-2">模型参数</div>
            <div className="flex flex-col gap-2.5">
              <Field label="最大输出 token" hint="留空＝沿用世界/预设；故事越长可以调大。">
                <input type="number" min={60} max={4000} step={50} className={C.input}
                  value={draft.narrator_max_tokens ?? ''} placeholder="跟随预设"
                  onChange={(e) => setField('narrator_max_tokens', e.target.value)} />
              </Field>
              <Field label="温度（0–2）" hint="越高越会发挥，越低越贴事实。">
                <input type="number" min={0} max={2} step={0.05} className={C.input}
                  value={draft.narrator_temperature ?? ''} placeholder="跟随预设"
                  onChange={(e) => setField('narrator_temperature', e.target.value)} />
              </Field>
              <Field label="超时（秒）" hint="单回合等待上限，超时会退回兜底叙述。">
                <input type="number" min={2} max={120} step={1} className={C.input}
                  value={draft.narrator_timeout ?? ''} placeholder="默认 30"
                  onChange={(e) => setField('narrator_timeout', e.target.value)} />
              </Field>
              <Field label="记忆轮数" hint="本次行动带给模型的前文回合数，默认 6。">
                <input type="number" min={1} max={40} step={1} className={C.input}
                  value={draft.narrator_history_turns ?? ''} placeholder="6"
                  onChange={(e) => setField('narrator_history_turns', e.target.value)} />
              </Field>
              <div className="grid grid-cols-3 gap-2">
                <Field label="top_p">
                  <input type="number" min={0} max={1} step={0.05} className={C.input}
                    value={draft.top_p ?? ''} placeholder="预设"
                    onChange={(e) => setField('top_p', e.target.value)} />
                </Field>
                <Field label="存在惩罚">
                  <input type="number" min={-2} max={2} step={0.1} className={C.input}
                    value={draft.presence_penalty ?? ''} placeholder="预设"
                    onChange={(e) => setField('presence_penalty', e.target.value)} />
                </Field>
                <Field label="频率惩罚">
                  <input type="number" min={-2} max={2} step={0.1} className={C.input}
                    value={draft.frequency_penalty ?? ''} placeholder="预设"
                    onChange={(e) => setField('frequency_penalty', e.target.value)} />
                </Field>
              </div>
              <Toggle label="显示规则判定" hint="在叙述后附带引擎的判定结果，便于调世界。"
                checked={draft.narrator_show_rules} onChange={(v) => setField('narrator_show_rules', v)} />
              <Toggle label="允许出戏发言" hint="允许叙述里出现玩家视角之外的评论。"
                checked={draft.narrator_allow_ooc} onChange={(v) => setField('narrator_allow_ooc', v)} />
            </div>
          </div>

          <button type="button" className={C.primary} disabled={busy} onClick={saveNarrator}>
            {busy ? '保存中…' : '保存叙事设置'}
          </button>
        </div>
      )}

      {tab === 'options' && (
        <div className="flex flex-col gap-3">
          <div className={C.card}>
            <div className="text-[11px] font-bold text-[#3f3a34] mb-2">生成参数</div>
            <div className="flex flex-col gap-2.5">
              <Field label="选项条数（2–5）" hint="默认 3；少一点更像岔路口，多一点更像清单。">
                <input type="number" min={2} max={5} step={1} className={C.input}
                  value={draft.suggestions_count ?? ''} placeholder="3"
                  onChange={(e) => setField('suggestions_count', e.target.value)} />
              </Field>
              <Field label="温度（0–2）" hint="默认 0.8；越高越会想出意外的动作。">
                <input type="number" min={0} max={2} step={0.05} className={C.input}
                  value={draft.suggestions_temperature ?? ''} placeholder="0.8"
                  onChange={(e) => setField('suggestions_temperature', e.target.value)} />
              </Field>
              <Field label="最大输出 token" hint="默认 400，只装三条短句，通常够用。">
                <input type="number" min={60} max={2000} step={50} className={C.input}
                  value={draft.suggestions_max_tokens ?? ''} placeholder="400"
                  onChange={(e) => setField('suggestions_max_tokens', e.target.value)} />
              </Field>
              <Field label="超时（秒）" hint="默认 12；超时就用规则层给出的选项补位。">
                <input type="number" min={2} max={120} step={1} className={C.input}
                  value={draft.suggestions_timeout ?? ''} placeholder="12"
                  onChange={(e) => setField('suggestions_timeout', e.target.value)} />
              </Field>
              <Field label="额外生成要求" hint="追加到选项提示词，例如「优先给能推动剧情的动作，少给纯观察」。">
                <textarea className={C.input} rows={4} value={draft.suggestions_extra_rules || ''}
                  placeholder="例如：其中至少一条要和在场的人说话。"
                  onChange={(e) => setField('suggestions_extra_rules', e.target.value)} />
              </Field>
            </div>
          </div>

          <div className={C.card}>
            <div className="text-[11px] font-bold text-[#3f3a34] mb-2">选项类型开关</div>
            <div className="flex flex-col gap-2">
              <Toggle label="允许「说话」类选项" hint="关掉后每条都必须是一个动作。"
                checked={draft.suggestions_allow_speech !== false}
                onChange={(v) => setField('suggestions_allow_speech', v)} />
              <Toggle label="允许「检查／调查」类选项" hint="关掉后不再出现纯观察项。"
                checked={draft.suggestions_allow_investigate !== false}
                onChange={(v) => setField('suggestions_allow_investigate', v)} />
              <Toggle label="跟随故事线" hint="开启时三条里会有一条直接推进到下一章。"
                checked={draft.suggestions_use_story !== false}
                onChange={(v) => setField('suggestions_use_story', v)} />
            </div>
          </div>

          <button type="button" className={C.primary} disabled={busy} onClick={saveOptions}>
            {busy ? '保存中…' : '保存选项设置'}
          </button>
        </div>
      )}

      {tab === 'learned' && (
        <div className="flex flex-col gap-3">
          <div className={C.card}>
            <Field label="世界 id（作用范围）" hint="学习按世界隔离：同一句话在不同世界里指向不同的门。留空则列出全部。">
              <input className={C.input} value={scope} placeholder="世界 id，例如 starter_xxx"
                onChange={(e) => setScope(e.target.value)} data-testid="intent-scope" />
            </Field>
            <div className="flex items-center gap-2 mt-2">
              <button type="button" className={C.ghost} disabled={rowsBusy} onClick={() => loadRows(scope.trim())}>
                <span className="inline-flex items-center gap-1.5"><RefreshCw size={12} />刷新</span>
              </button>
              <button type="button" className={C.ghost} disabled={rowsBusy || !scope.trim()} onClick={clearScope}>
                <span className="inline-flex items-center gap-1.5"><Trash2 size={12} />{confirmClear ? '再点一次确认清空' : '清空这个世界'}</span>
              </button>
              <span className="text-[10px] text-[#8a857c]">{rows.length} 条</span>
            </div>
          </div>

          {rowsError ? <div className="text-[11px] text-red-600">{rowsError}</div> : null}

          <div className={C.card}>
            <div className="text-[11px] font-bold text-[#3f3a34] mb-2">手写一条</div>
            <div className="flex flex-col gap-2">
              <input className={C.input} placeholder="玩家怎么说，例如：推开门"
                value={newRow.phrase} onChange={(e) => setNewRow({ ...newRow, phrase: e.target.value })} data-testid="intent-new-phrase" />
              <input className={C.input} placeholder="动作名，例如：open"
                value={newRow.action} onChange={(e) => setNewRow({ ...newRow, action: e.target.value })} data-testid="intent-new-action" />
              <input className={C.input} placeholder="指向目标的片段，例如：气闸内门（可空）"
                value={newRow.fragment} onChange={(e) => setNewRow({ ...newRow, fragment: e.target.value })} />
              <div className={C.help}>{ACTION_HINTS}</div>
              <button type="button" className={C.primary} disabled={rowsBusy} onClick={addRow} data-testid="intent-add">
                <span className="inline-flex items-center gap-1.5"><Plus size={12} />加入学习表</span>
              </button>
            </div>
          </div>

          {rowsBusy && rows.length === 0 ? <div className="text-[11px] text-[#8a857c]">读取中…</div> : null}
          {!rowsBusy && rows.length === 0 ? (
            <div className="text-[11px] text-[#8a857c] leading-relaxed">
              这个世界还没有学习记录。顾问模型读懂一句自由输入后会写一行；也可以用上面的表单手写。
            </div>
          ) : null}

          <div className="flex flex-col gap-2" data-testid="intent-rows">
            {rows.map((row) => (
              <div key={row.id} className={C.card}>
                <div className="text-xs font-semibold text-[#1a1a1a] break-words">{row.phrase}</div>
                {editingId === row.id ? (
                  <div className="flex flex-col gap-2 mt-2">
                    <input className={C.input} value={editAction} placeholder="动作名"
                      onChange={(e) => setEditAction(e.target.value)} />
                    <input className={C.input} value={editFragment} placeholder="目标片段"
                      onChange={(e) => setEditFragment(e.target.value)} />
                    <div className="flex items-center gap-2">
                      <button type="button" className={C.primary} disabled={rowsBusy} onClick={commitEdit}>保存</button>
                      <button type="button" className={C.ghost} onClick={() => setEditingId(null)}>取消</button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="text-[10px] text-[#6b665e] mt-1 break-words">
                      动作 <span className="font-mono text-[#1a1a1a]">{row.action}</span>
                      {row.fragment ? <span> · 目标 <span className="font-mono">{row.fragment}</span></span> : null}
                    </div>
                    <div className="text-[10px] text-[#a8a29a] mt-1">
                      命中 {row.hits} 次 · 来源 {row.source === 'manual' ? '手写' : '顾问'} · 置信度 {Number(row.confidence || 0).toFixed(2)}
                    </div>
                    <div className="flex items-center gap-2 mt-2">
                      <button type="button" className={C.ghost} onClick={() => startEdit(row)}>修改</button>
                      <button type="button" className={C.ghost} disabled={rowsBusy} onClick={() => removeRow(row.id)}>删除</button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export { TABS as NARRATIVE_TABS }