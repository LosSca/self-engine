import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { importScenario, previewOpening, uploadCharacterImage } from '../api'
import QuickDraft from './QuickDraft'
import {
  ALL_TYPES,
  DEFAULT_EXPRESSIONS,
  DEFAULT_OUTFITS,
  WORLD_TYPE_LABELS,
  getWorldType,
} from '../lib/scenarioTypes'

const CARD_TYPES = [
  { id: 'NPC', label: '角色' },
  { id: 'LOCATION', label: '地点' },
  { id: 'FACTION', label: '阵营' },
  { id: 'ITEM', label: '物品' },
  { id: 'LORE', label: '设定' },
]

const QUESTION_TYPES = [
  { id: 'text', label: '文本回答' },
  { id: 'choice', label: '单选' },
]

const LANGUAGES = [
  { id: 'zh', label: '中文' },
  { id: 'en', label: 'English' },
]

// Paper palette, shared with the character library: the light theme cannot
// invert a black canvas into something readable, so nothing here is black.
const inputClass =
  'w-full bg-[#fbfaf7] border border-[#dcd8d0] rounded-xl px-4 py-3 text-sm text-[#1a1a1a] placeholder:text-[#9a958c] focus:outline-none focus:border-[#8a857c] transition-colors'
const labelClass = 'block text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c] mb-2'
const labelRowClass = 'flex items-center justify-between gap-3 mb-2'
const sectionClass =
  'bg-white border border-[#e4e0d8] rounded-3xl p-6 sm:p-8 shadow-[0_1px_2px_rgba(24,20,16,0.04),0_18px_40px_-28px_rgba(24,20,16,0.35)]'
const tinyBtnClass =
  'px-3 py-1.5 rounded-lg border border-[#dcd8d0] text-[10px] tracking-widest uppercase text-[#5c574f] hover:text-[#1a1a1a] hover:border-[#8a857c] transition-all'

let uid = 0
const nextUid = () => 'local-' + ++uid

function emptyCard() {
  return { uid: nextUid(), card_type: 'NPC', name: '', contentText: '' }
}

function emptyQuestion() {
  const uid = nextUid()
  return {
    uid,
    id: 'q_' + uid,
    var_name: '',
    prompt: '',
    type: 'text',
    allow_custom: true,
    required: true,
    optionText: '选项一\n选项二',
  }
}

function emptyCharacter() {
  return {
    uid: nextUid(),
    name: '',
    role: '',
    description: '',
    portrait_url: '',
    is_player: false,
    looks: [],
  }
}

function emptyLook() {
  return {
    uid: nextUid(),
    outfit: DEFAULT_OUTFITS[0],
    expression: DEFAULT_EXPRESSIONS[0],
    image_url: '',
  }
}

export default function ScenarioBuilder({ onCreated }) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const selectedType = searchParams.get('type') || ''
  const preset = selectedType ? getWorldType(selectedType) : null

  const [form, setForm] = useState({
    title: '',
    description: '',
    tone_instructions: '',
    language: 'zh',
    lore_text: '',
    opening_mode: 'fixed',
    opening_narrative: '',
    ai_opening_directive: '',
  })
  const [cards, setCards] = useState([])
  const [questions, setQuestions] = useState([])
  const [characters, setCharacters] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [uploadingUid, setUploadingUid] = useState('')

  // 首次进入带 ?type= 时，用该类型的起手素材预填（只执行一次，避免覆盖用户输入）
  const presetApplied = useRef(false)
  useEffect(() => {
    if (presetApplied.current) return
    presetApplied.current = true
    if (!preset || preset.id === 'custom') return
    setForm((prev) => ({
      ...prev,
      title: prev.title || preset.name + '·未命名情景',
      description: prev.description || preset.tagline,
      tone_instructions: prev.tone_instructions || preset.tone,
      lore_text: prev.lore_text || preset.lore,
      opening_mode: preset.opening ? 'fixed' : 'ai',
      opening_narrative: prev.opening_narrative || preset.opening,
      ai_opening_directive: prev.ai_opening_directive || preset.aiDirective,
    }))
    setCards(
      (preset.cards || []).map((card) => ({
        uid: nextUid(),
        card_type: card.card_type,
        name: card.name,
        contentText: JSON.stringify(card.content || {}, null, 2),
      }))
    )
    setQuestions(
      (preset.questions || []).map((q) => ({
        uid: nextUid(),
        id: q.id || q.var_name || ('q_' + nextUid()),
        var_name: q.var_name || '',
        prompt: q.prompt || '',
        type: q.type || 'text',
        allow_custom: q.allow_custom !== false,
        required: q.required !== false,
        optionText: (q.options || []).map((o) => o.label).join('\n'),
      }))
    )
    setCharacters(
      (preset.characters || []).map((c) => ({
        ...emptyCharacter(),
        ...c,
        looks: [],
      }))
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const setField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }))

  const typeLabel = WORLD_TYPE_LABELS[selectedType] || '自定义世界'

  const addCard = () => setCards((prev) => [...prev, emptyCard()])
  const updateCard = (id, patch) =>
    setCards((prev) => prev.map((c) => (c.uid === id ? { ...c, ...patch } : c)))
  const removeCard = (id) => setCards((prev) => prev.filter((c) => c.uid !== id))

  const addQuestion = () => setQuestions((prev) => [...prev, emptyQuestion()])
  const updateQuestion = (id, patch) =>
    setQuestions((prev) => prev.map((q) => (q.uid === id ? { ...q, ...patch } : q)))
  const removeQuestion = (id) => setQuestions((prev) => prev.filter((q) => q.uid !== id))

  const addCharacter = () => setCharacters((prev) => [...prev, emptyCharacter()])
  const updateCharacterField = (id, patch) =>
    setCharacters((prev) => prev.map((c) => (c.uid === id ? { ...c, ...patch } : c)))
  const removeCharacter = (id) => setCharacters((prev) => prev.filter((c) => c.uid !== id))

  const addLook = (characterId) =>
    setCharacters((prev) =>
      prev.map((c) => (c.uid === characterId ? { ...c, looks: [...c.looks, emptyLook()] } : c))
    )
  const updateLook = (characterId, lookId, patch) =>
    setCharacters((prev) =>
      prev.map((c) =>
        c.uid === characterId
          ? { ...c, looks: c.looks.map((l) => (l.uid === lookId ? { ...l, ...patch } : l)) }
          : c
      )
    )
  const removeLook = (characterId, lookId) =>
    setCharacters((prev) =>
      prev.map((c) =>
        c.uid === characterId ? { ...c, looks: c.looks.filter((l) => l.uid !== lookId) } : c
      )
    )

  const uploadImage = async (characterId, lookId, file) => {
    if (!file) return
    setUploadingUid(lookId)
    try {
      const data = await uploadCharacterImage(file)
      updateLook(characterId, lookId, { image_url: data.url })
    } catch (err) {
      setError(err.message || '图片上传失败')
    } finally {
      setUploadingUid('')
    }
  }

  const playerCharacter = useMemo(() => characters.find((c) => c.is_player), [characters])

  const buildQuestionsPayload = () => {
    const payload = []
    for (const q of questions) {
      const varName = q.var_name.trim()
      if (!varName) throw new Error('问答缺少变量名（只能使用小写字母、数字与下划线，且以字母开头）')
      if (!/^[a-z][a-z0-9_]*$/.test(varName)) {
        throw new Error('变量名「' + varName + '」不合法：只能使用小写字母、数字与下划线，且以字母开头')
      }
      if (!q.prompt.trim()) throw new Error('问答缺少问题文本')
      const options =
        q.type === 'choice'
          ? String(q.optionText || '')
              .split('\n')
              .map((line) => line.trim())
              .filter(Boolean)
              .map((label) => ({ label, description: '' }))
          : []
      if (q.type === 'choice' && options.length < 2) {
        throw new Error('问题「' + q.prompt + '」是单选类型，至少需要两个选项')
      }
      payload.push({
        id: q.id || ('q_' + varName),
        var_name: varName,
        prompt: q.prompt.trim(),
        type: q.type,
        options,
        allow_custom: q.allow_custom,
        required: q.required,
      })
    }
    return payload
  }

  const handlePreviewOpening = async () => {
    setPreviewing(true)
    setError('')
    try {
      const data = await previewOpening({
        language: form.language,
        tone: form.tone_instructions,
        lore: form.lore_text,
        directive: form.ai_opening_directive,
      })
      setPreview(data.opening || data.narrative || data.text || JSON.stringify(data))
    } catch (err) {
      setError(err.message || '生成开场失败，请检查模型配置')
    } finally {
      setPreviewing(false)
    }
  }

  const handleSubmit = async () => {
    setError('')
    if (!form.title.trim()) {
      setError('请先填写情景名称。')
      return
    }
    // 后端也校验这一条（CreateScenarioRequest），这里先拦一次，别让玩家提交后才看到报错。
    if (!form.opening_narrative.trim() && !form.ai_opening_directive.trim()) {
      setError('这个世界的开场白还不能空：写一段开场，或让 AI 按指令生成。')
      return
    }
    setSubmitting(true)
    try {
      const questionsPayload = buildQuestionsPayload()
      const storyCards = cards.map((card) => {
        if (!card.name.trim()) throw new Error('故事卡片缺少名称')
        let content = {}
        if (card.contentText.trim()) {
          try {
            content = JSON.parse(card.contentText)
          } catch {
            throw new Error('故事卡片「' + card.name + '」的内容不是合法 JSON')
          }
        }
        return { card_type: card.card_type, name: card.name.trim(), content }
      })

      // 一次性导入：情景 + 故事卡片 + 角色包，避免留下半成品情景。
      const payload = {
        version: '1.0',
        scenario: {
          title: form.title.trim(),
          description: form.description.trim(),
          tone_instructions: form.tone_instructions.trim(),
          language: form.language,
          lore_text: form.lore_text.trim(),
          setup_questions: questionsPayload,
          opening_mode: form.opening_mode,
          ai_opening_directive: form.ai_opening_directive.trim(),
          opening_narrative: form.opening_narrative.trim(),
          world_type: selectedType,
        },
        story_cards: storyCards,
        campaigns: [],
        characters: characters
          .filter((c) => c.name.trim())
          .map((c) => ({
            name: c.name.trim(),
            role: c.role.trim(),
            description: c.description.trim(),
            portrait_url: c.portrait_url,
            is_player: c.is_player,
            looks: c.looks
              .filter((l) => l.image_url)
              .map((l) => ({
                outfit: l.outfit.trim() || '默认',
                expression: l.expression.trim() || '平静',
                image_url: l.image_url,
              })),
          })),
      }
      await importScenario(payload)
      if (onCreated) onCreated()
      else navigate('/')
    } catch (err) {
      setError(err.message || '创建情景失败。')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-[#f6f5f1] text-[#3a3630] font-sans selection:bg-[#1a1a1a]/10 selection:text-[#1a1a1a] relative overflow-hidden">
      <div
        className="page-bg absolute inset-0 bg-cover bg-center bg-no-repeat opacity-25"
        style={{ backgroundImage: "url('/lunar-bg.jpg')" }}
      ></div>
      <div className="page-mask absolute inset-0 bg-gradient-to-b from-[#f6f5f1] via-[#f6f5f1]/85 to-[#f6f5f1] pointer-events-none"></div>

      <div className="relative z-10 max-w-4xl mx-auto px-4 sm:px-8 py-10">
        <button
          onClick={() => navigate('/')}
          className="text-[10px] uppercase tracking-[0.3em] text-[#8a857c] hover:text-[#1a1a1a] transition-colors mb-8"
        >
          ← 返回首页
        </button>

        <header className="mb-10">
          <h1 className="text-3xl sm:text-5xl font-bold text-[#1a1a1a] tracking-tight mb-3">构建情景</h1>
          <p className="text-sm text-[#5c574f] max-w-2xl leading-relaxed">
            先定义世界类型与基调，再补充角色、地点和开场。所有配置都会成为之后规则与叙事的事实来源。
          </p>
        </header>

        {error && (
          <div className="mb-6 px-5 py-4 rounded-2xl border border-rose-200 bg-rose-50 text-rose-700 text-sm whitespace-pre-wrap">
            {error}
          </div>
        )}

        <div className="flex flex-col gap-6">
          {/* 世界类型 */}
          <section className={sectionClass}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xs font-bold uppercase tracking-[0.25em] text-[#5c574f]">世界类型</h2>
              <span className="text-[10px] text-[#8a857c]">已选：{typeLabel}</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {ALL_TYPES.map((t) => (
                <button
                  key={t.id}
                  onClick={() => {
                    if (t.id === selectedType) return
                    navigate('/create?type=' + t.id)
                    window.location.reload()
                  }}
                  className={
                    'px-4 py-2 rounded-xl border text-xs transition-all ' +
                    (t.id === selectedType
                      ? 'border-[#8a857c] text-[#1a1a1a] bg-[#efede8]'
                      : 'border-[#dcd8d0] text-[#8a857c] hover:text-[#1a1a1a] hover:border-[#8a857c]')
                  }
                >
                  <span className="mr-2">{t.icon}</span>
                  {t.name}
                </button>
              ))}
            </div>
            {preset && preset.id !== 'custom' && (
              <p className="text-[11px] text-[#8a857c] mt-4 leading-relaxed">
                已按「{preset.name}」预填基调、背景、开场与示例卡片，可自由修改。
              </p>
            )}
          </section>

          {/* 基础设定 */}
          <section className={sectionClass}>
            <h2 className="text-xs font-bold uppercase tracking-[0.25em] text-[#5c574f] mb-6">基础设定</h2>
            <div className="flex flex-col gap-5">
              <div>
                <label className={labelClass}>情景名称 *</label>
                <input
                  className={inputClass}
                  value={form.title}
                  onChange={(e) => setField('title', e.target.value)}
                  placeholder="例如：北境石门"
                />
              </div>
              <div>
                <div className={labelRowClass}>
                  <label className={labelClass + ' mb-0'}>简介</label>
                  <QuickDraft
                    kind="world"
                    topic={form.title}
                    context={form.lore_text}
                    value={form.description}
                    language={form.language}
                    label="快速生成"
                    testId="draft-world-description"
                    onDraft={(text) => setField('description', text)}
                    onAppend={(text) => setField('description', [form.description?.trim(), text].filter(Boolean).join(''))}
                  />
                </div>
                <textarea
                  className={inputClass + ' min-h-[80px]'}
                  value={form.description}
                  onChange={(e) => setField('description', e.target.value)}
                  placeholder="一句话说明这是一个什么样的世界。"
                />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
                <div>
                  <label className={labelClass}>叙事语言</label>
                  <select
                    className={inputClass}
                    value={form.language}
                    onChange={(e) => setField('language', e.target.value)}
                  >
                    {LANGUAGES.map((l) => (
                      <option key={l.id} value={l.id} className="bg-neutral-900">
                        {l.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className={labelClass}>基调与写作指令</label>
                  <input
                    className={inputClass}
                    value={form.tone_instructions}
                    onChange={(e) => setField('tone_instructions', e.target.value)}
                    placeholder="例如：冷峻、克制、少形容词"
                  />
                </div>
              </div>
              <div>
                <div className={labelRowClass}>
                  <label className={labelClass + ' mb-0'}>世界背景</label>
                  <QuickDraft
                    kind="world"
                    topic={form.title}
                    context={form.description}
                    value={form.lore_text}
                    language={form.language}
                    label="快速生成"
                    testId="draft-world-lore"
                    onDraft={(text) => setField('lore_text', text)}
                    onAppend={(text) => setField('lore_text', [form.lore_text?.trim(), text].filter(Boolean).join('\n\n'))}
                  />
                </div>
                <textarea
                  className={inputClass + ' min-h-[120px]'}
                  value={form.lore_text}
                  onChange={(e) => setField('lore_text', e.target.value)}
                  placeholder="世界的历史、地理与规则背景，会作为叙事上下文。"
                />
              </div>
            </div>
          </section>

          {/* 开场 */}
          <section className={sectionClass}>
            <h2 className="text-xs font-bold uppercase tracking-[0.25em] text-[#5c574f] mb-6">开场</h2>
            <div className="flex gap-2 mb-5">
              {[
                { id: 'fixed', label: '固定文本' },
                { id: 'ai', label: '由模型生成' },
              ].map((mode) => (
                <button
                  key={mode.id}
                  onClick={() => setField('opening_mode', mode.id)}
                  className={
                    'px-4 py-2 rounded-xl border text-xs transition-all ' +
                    (form.opening_mode === mode.id
                      ? 'border-[#8a857c] text-[#1a1a1a] bg-[#efede8]'
                      : 'border-[#dcd8d0] text-[#8a857c] hover:text-[#1a1a1a]')
                  }
                >
                  {mode.label}
                </button>
              ))}
            </div>

            {form.opening_mode === 'fixed' ? (
              <div>
                <div className="flex justify-end mb-2">
                  <QuickDraft
                    kind="world"
                    topic={form.title}
                    context={[form.description, form.lore_text].filter(Boolean).join('\n\n')}
                    value={form.opening_narrative}
                    language={form.language}
                    label="快速生成开场"
                    testId="draft-opening"
                    onDraft={(text) => setField('opening_narrative', text)}
                    onAppend={(text) => setField('opening_narrative', [form.opening_narrative?.trim(), text].filter(Boolean).join('\n\n'))}
                  />
                </div>
                <textarea
                  className={inputClass + ' min-h-[160px] w-full'}
                  value={form.opening_narrative}
                  onChange={(e) => setField('opening_narrative', e.target.value)}
                  placeholder="玩家进入游戏时看到的开场段落。"
                />
              </div>
            ) : (
              <div className="flex flex-col gap-4">
                <textarea
                  className={inputClass + ' min-h-[100px]'}
                  value={form.ai_opening_directive}
                  onChange={(e) => setField('ai_opening_directive', e.target.value)}
                  placeholder="告诉模型如何写开场，例如：从一次具体的边境异常开始，不要先解释世界观。"
                />
                <div className="flex items-center gap-3">
                  <button onClick={handlePreviewOpening} disabled={previewing} className={tinyBtnClass}>
                    {previewing ? '生成中…' : '预览生成的开场'}
                  </button>
                  {preview && (
                    <span className="text-[10px] text-[#8a857c]">预览只做展示，不会保存</span>
                  )}
                </div>
                {preview && (
                  <div className="rounded-2xl border border-[#e4e0d8] bg-[#fbfaf7] p-4 text-sm text-[#3a3630] whitespace-pre-wrap leading-relaxed">
                    {preview}
                  </div>
                )}
              </div>
            )}
          </section>

          {/* 故事卡片 */}
          <section className={sectionClass}>
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xs font-bold uppercase tracking-[0.25em] text-[#5c574f]">
                故事卡片 · {cards.length} 张
              </h2>
              <button onClick={addCard} className={tinyBtnClass}>+ 添加卡片</button>
            </div>
            <div className="flex flex-col gap-4">
              {cards.length === 0 && (
                <p className="text-xs text-[#8a857c]">还没有卡片。角色、地点、阵营和物品都可以先写成卡片。</p>
              )}
              {cards.map((card) => (
                <div key={card.uid} className="rounded-2xl border border-[#e4e0d8] bg-[#fbfaf7] p-4">
                  <div className="grid grid-cols-1 sm:grid-cols-[120px_1fr_auto] gap-3 mb-3">
                    <select
                      className={inputClass}
                      value={card.card_type}
                      onChange={(e) => updateCard(card.uid, { card_type: e.target.value })}
                    >
                      {CARD_TYPES.map((t) => (
                        <option key={t.id} value={t.id} className="bg-neutral-900">
                          {t.label}
                        </option>
                      ))}
                    </select>
                    <input
                      className={inputClass}
                      value={card.name}
                      onChange={(e) => updateCard(card.uid, { name: e.target.value })}
                      placeholder="名称"
                    />
                    <button onClick={() => removeCard(card.uid)} className={tinyBtnClass}>删除</button>
                  </div>
                  <textarea
                    className={inputClass + ' min-h-[90px] font-mono text-xs'}
                    value={card.contentText}
                    onChange={(e) => updateCard(card.uid, { contentText: e.target.value })}
                    placeholder='{"description": "..."}'
                  />
                </div>
              ))}
            </div>
          </section>

          {/* 角色包 */}
          <section className={sectionClass}>
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-xs font-bold uppercase tracking-[0.25em] text-[#5c574f]">
                角色包 · {characters.length} 位
              </h2>
              <button onClick={addCharacter} className={tinyBtnClass}>+ 添加角色</button>
            </div>
            <p className="text-[11px] text-[#8a857c] mb-6 leading-relaxed">
              每位角色可以配置多套「服装 × 表情」立绘。玩家开局时从中选择一套，之后的叙事插图会跟随当前形象。
            </p>

            <div className="flex flex-col gap-5">
              {characters.length === 0 && (
                <p className="text-xs text-[#8a857c]">还没有角色。添加一位玩家角色后才能开始冒险。</p>
              )}
              {characters.map((character) => (
                <div key={character.uid} className="rounded-2xl border border-[#e4e0d8] bg-[#fbfaf7] p-5">
                  <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center gap-3">
                      <span className="text-sm text-[#1a1a1a]">{character.name || '未命名角色'}</span>
                      {character.is_player && (
                        <span className="text-[9px] px-2 py-0.5 rounded-full border border-emerald-400/30 text-emerald-300/80">
                          玩家角色
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => updateCharacterField(character.uid, { is_player: !character.is_player })}
                        className={tinyBtnClass}
                      >
                        {character.is_player ? '取消玩家' : '设为玩家'}
                      </button>
                      <button onClick={() => removeCharacter(character.uid)} className={tinyBtnClass}>删除</button>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                    <input
                      className={inputClass}
                      value={character.name}
                      onChange={(e) => updateCharacterField(character.uid, { name: e.target.value })}
                      placeholder="角色名"
                    />
                    <input
                      className={inputClass}
                      value={character.role}
                      onChange={(e) => updateCharacterField(character.uid, { role: e.target.value })}
                      placeholder="身份 / 定位"
                    />
                  </div>
                  <textarea
                    className={inputClass + ' min-h-[70px] mb-4'}
                    value={character.description}
                    onChange={(e) => updateCharacterField(character.uid, { description: e.target.value })}
                    placeholder="外貌、性格与背景，会进入叙事上下文。"
                  />

                  <div className="flex items-center justify-between mb-3">
                    <span className="text-[10px] uppercase tracking-[0.2em] text-[#8a857c]">
                      立绘差分 · {character.looks.length} 套
                    </span>
                    <button onClick={() => addLook(character.uid)} className={tinyBtnClass}>+ 差分</button>
                  </div>

                  <div className="flex flex-col gap-3">
                    {character.looks.map((look) => (
                      <div key={look.uid} className="flex flex-wrap items-center gap-3">
                        <input
                          className={inputClass + ' flex-1 min-w-[110px]'}
                          value={look.outfit}
                          onChange={(e) => updateLook(character.uid, look.uid, { outfit: e.target.value })}
                          placeholder="服装"
                          list={'outfits-' + character.uid}
                        />
                        <datalist id={'outfits-' + character.uid}>
                          {DEFAULT_OUTFITS.map((o) => <option key={o} value={o} />)}
                        </datalist>
                        <input
                          className={inputClass + ' flex-1 min-w-[110px]'}
                          value={look.expression}
                          onChange={(e) => updateLook(character.uid, look.uid, { expression: e.target.value })}
                          placeholder="表情"
                          list={'expressions-' + character.uid}
                        />
                        <datalist id={'expressions-' + character.uid}>
                          {DEFAULT_EXPRESSIONS.map((e) => <option key={e} value={e} />)}
                        </datalist>
                        <label className={tinyBtnClass + ' cursor-pointer'}>
                          {uploadingUid === look.uid ? '上传中…' : '上传立绘'}
                          <input
                            type="file"
                            accept="image/png,image/jpeg,image/webp,image/gif"
                            className="hidden"
                            onChange={(e) => uploadImage(character.uid, look.uid, e.target.files && e.target.files[0])}
                          />
                        </label>
                        {look.image_url && (
                          <img
                            src={look.image_url}
                            alt={look.outfit + look.expression}
                            className="w-10 h-14 object-cover rounded-lg border border-[#e4e0d8]"
                          />
                        )}
                        <button onClick={() => removeLook(character.uid, look.uid)} className={tinyBtnClass}>移除</button>
                      </div>
                    ))}
                    {character.looks.length === 0 && (
                      <p className="text-[11px] text-[#a8a29a]">还没有立绘差分，可以先添加「常服 / 平静」。</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* 首次游玩问答 */}
          <section className={sectionClass}>
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-xs font-bold uppercase tracking-[0.25em] text-[#5c574f]">
                首次游玩问答 · {questions.length} 题
              </h2>
              <button onClick={addQuestion} className={tinyBtnClass}>+ 添加问题</button>
            </div>
            <p className="text-[11px] text-[#8a857c] mb-6 leading-relaxed">
              这些问答会在冒险开始前询问玩家，答案会作为开局上下文交给叙事模型。
            </p>

            <div className="flex flex-col gap-4">
              {questions.length === 0 && (
                <p className="text-xs text-[#8a857c]">没有问答也可以，玩家会直接进入开场。</p>
              )}
              {questions.map((q) => (
                <div key={q.uid} className="rounded-2xl border border-[#e4e0d8] bg-[#fbfaf7] p-4 flex flex-col gap-3">
                  <div className="grid grid-cols-1 sm:grid-cols-[160px_1fr] gap-3">
                    <input
                      className={inputClass}
                      value={q.var_name}
                      onChange={(e) => updateQuestion(q.uid, { var_name: e.target.value })}
                      placeholder="变量名 origin"
                    />
                    <input
                      className={inputClass}
                      value={q.prompt}
                      onChange={(e) => updateQuestion(q.uid, { prompt: e.target.value })}
                      placeholder="问题文本"
                    />
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-[160px_1fr] gap-3">
                    <select
                      className={inputClass}
                      value={q.type}
                      onChange={(e) => updateQuestion(q.uid, { type: e.target.value })}
                    >
                      {QUESTION_TYPES.map((t) => (
                        <option key={t.id} value={t.id} className="bg-neutral-900">{t.label}</option>
                      ))}
                    </select>
                    {q.type === 'choice' ? (
                      <textarea
                        className={inputClass + ' min-h-[70px]'}
                        value={q.optionText}
                        onChange={(e) => updateQuestion(q.uid, { optionText: e.target.value })}
                        placeholder="每行一个选项"
                      />
                    ) : (
                      <div className="flex items-center gap-4 text-[11px] text-[#8a857c]">
                        <label className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            checked={q.allow_custom}
                            onChange={(e) => updateQuestion(q.uid, { allow_custom: e.target.checked })}
                          />
                          允许自定义回答
                        </label>
                        <label className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            checked={q.required}
                            onChange={(e) => updateQuestion(q.uid, { required: e.target.checked })}
                          />
                          必答
                        </label>
                      </div>
                    )}
                  </div>
                  <div className="flex justify-end">
                    <button onClick={() => removeQuestion(q.uid)} className={tinyBtnClass}>删除</button>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>

        <div className="mt-10 flex flex-col sm:flex-row items-center gap-4 justify-between">
          <p className="text-[11px] text-[#a8a29a]">
            {playerCharacter
              ? '玩家角色：' + playerCharacter.name
              : '提示：还没有设置玩家角色，开局时会使用默认旅人。'}
          </p>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="px-10 py-4 rounded-full bg-[#3f3a34] text-white font-bold text-xs uppercase tracking-[0.2em] hover:bg-[#3a3733] disabled:opacity-40 disabled:cursor-not-allowed border border-[#dcd8d0] transition-all"
          >
            {submitting ? '正在构建世界…' : '创建情景'}
          </button>
        </div>
      </div>
    </div>
  )
}
