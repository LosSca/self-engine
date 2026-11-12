import { useEffect, useMemo, useState } from 'react'
import {
  fetchCharactersByScenario,
  fetchLibraryCharacters,
  saveSetupAnswers,
  setCampaignCharacter,
} from '../api'
import { interpolate } from '../lib/interpolate'

const inputBase =
  'w-full bg-white/[0.03] backdrop-blur-sm border border-white/10 rounded-2xl px-5 py-4 text-white placeholder-white/20 focus:outline-none focus:border-white/30 transition-all text-base font-light'

export default function SetupWizard({ scenario, campaignId, questions, onComplete }) {
  const ordered = useMemo(() => questions.filter(Boolean), [questions])
  const [step, setStep] = useState(0)
  const [answers, setAnswers] = useState({})
  const [textValue, setTextValue] = useState('')
  const [choiceLabel, setChoiceLabel] = useState(null)
  const [customValue, setCustomValue] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  // 角色形象：本世界的角色 + 全局角色库，开局时可以随便挑一个。
  const [libraryCharacters, setLibraryCharacters] = useState([])
  const [characters, setCharacters] = useState(null)
  const [characterId, setCharacterId] = useState('')
  const [lookId, setLookId] = useState('')
  const [pickerOpen, setPickerOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    const own = scenario?.id
      ? fetchCharactersByScenario(scenario.id).catch(() => [])
      : Promise.resolve([])
    Promise.all([own, fetchLibraryCharacters().catch(() => [])])
      .then(([scenarioList, libraryList]) => {
        if (cancelled) return
        // The per-scenario route already includes the library; keep the
        // world's own cast first so its protagonist stays the default pick.
        const seen = new Set()
        const merged = []
        ;[...(scenarioList || []), ...(libraryList || [])].forEach((c) => {
          if (!c || seen.has(c.id)) return
          seen.add(c.id)
          merged.push(c)
        })
        setLibraryCharacters(Array.isArray(libraryList) ? libraryList : [])
        setCharacters(merged)
        const player = merged.find((c) => c.is_player) || merged[0]
        if (player) {
          setCharacterId(player.id)
          const looks = player.looks || []
          const portrait = looks.find((l) => l.image_url) || looks[0]
          if (portrait) setLookId(portrait.id)
        }
      })
      .catch(() => { if (!cancelled) setCharacters([]) })
    return () => { cancelled = true }
  }, [scenario?.id])

  const libraryIds = useMemo(
    () => new Set(libraryCharacters.map((c) => c.id)),
    [libraryCharacters],
  )

  const pickCharacter = (choice) => {
    const c = choice.raw || choice
    setCharacterId(c.id)
    const looks = c.looks || []
    const portrait = looks.find((l) => l.image_url) || looks[0]
    setLookId(portrait?.id || '')
    setPickerOpen(false)
  }

  const character = useMemo(
    () => (characters || []).find((c) => c.id === characterId) || null,
    [characters, characterId],
  )
  const looks = character?.looks || []
  const selectedLook = looks.find((l) => l.id === lookId) || null
  // 服装与表情两档都用扁平 map：嵌套的筛选 + map 会让 JSX 降级器产不出语法。
  const outfits = Array.from(new Set(looks.map((l) => l.outfit)))
  // 降级器处理不了括号里的三元表达式，所以这一段用 && 而不写 `cond ? <a/> : <b/>`。
  const hasArt = Boolean(selectedLook?.image_url)
  const hasNoLook = looks.length === 0
  const shownLooks = looks.filter((l) => !selectedLook || l.outfit === selectedLook.outfit)
  // onChange 里只调一个函数：降级器会把内联的语句块和箭头搅在一起。
  const pickOutfit = (outfit) => {
    const match = looks.find((l) => l.outfit === outfit)
    if (match) setLookId(match.id)
  }
  // 角色卡片的派生数据放在渲染外：map 里直接读构造好的字段，避免嵌套 JSX。
  const characterList = Array.isArray(characters) ? characters : []
  const characterChoices = characterList.map((c) => {
    const art = (c.looks || []).find((l) => l.image_url) || (c.looks || [])[0] || null
    return {
      id: c.id,
      name: c.name,
      role: c.role || (c.is_player ? '玩家角色' : '角色'),
      art: art?.image_url || '',
      inLibrary: libraryIds.has(c.id),
      active: c.id === (character?.id || ''),
      raw: c,
    }
  })
  // 任何一个角色都能进这一步：没有立绘的角色照样可以让玩家点名。
  const showCharacterStage = Boolean(character)
  const total = ordered.length + (showCharacterStage ? 1 : 0)
  const isCharacterStage = showCharacterStage && step === 0
  const questionIndex = showCharacterStage ? step - 1 : step
  const current = isCharacterStage ? null : ordered[questionIndex]
  const resolvedPrompt = useMemo(
    () => interpolate(current?.prompt || '', answers),
    [current, answers],
  )
  const isLast = !showCharacterStage
    ? questionIndex === ordered.length - 1
    : questionIndex === ordered.length - 1 && ordered.length > 0
  const optionByLabel = (label) =>
    (current?.options || []).find((o) => o.label === label) || null

  const canAdvance = () => {
    if (isCharacterStage) return Boolean(character)
    if (!current) return false
    if (current.type === 'text') {
      return !current.required || textValue.trim().length > 0
    }
    if (choiceLabel === '__custom__') {
      return !current.required || customValue.trim().length > 0
    }
    return !current.required || choiceLabel !== null
  }

  const buildAnswer = () => {
    if (!current) return null
    if (current.type === 'text') {
      return {
        var_name: current.var_name,
        resolved_prompt: resolvedPrompt,
        type: 'text',
        value: textValue.trim(),
        description: '',
      }
    }
    if (choiceLabel === '__custom__') {
      return {
        var_name: current.var_name,
        resolved_prompt: resolvedPrompt,
        type: 'choice',
        value: customValue.trim(),
        description: '',
      }
    }
    const opt = optionByLabel(choiceLabel)
    return {
      var_name: current.var_name,
      resolved_prompt: resolvedPrompt,
      type: 'choice',
      value: opt?.label || '',
      description: opt?.description || '',
    }
  }

  const loadStepIntoLocalState = (nextStep, allAnswers) => {
    const qi = showCharacterStage ? nextStep - 1 : nextStep
    const q = ordered[qi]
    const existing = q ? allAnswers[q.var_name] : null
    if (q?.type === 'text') {
      setTextValue(existing?.value || '')
      setChoiceLabel(null)
      setCustomValue('')
    } else {
      setTextValue('')
      if (existing) {
        const knownOpt = (q.options || []).find((o) => o.label === existing.value)
        if (knownOpt) {
          setChoiceLabel(knownOpt.label)
          setCustomValue('')
        } else {
          setChoiceLabel('__custom__')
          setCustomValue(existing.value || '')
        }
      } else {
        setChoiceLabel(null)
        setCustomValue('')
      }
    }
  }

  const finish = async () => {
    setSubmitting(true)
    setError(null)
    try {
      await saveSetupAnswers(campaignId, answers)
      onComplete?.()
    } catch {
      setError('保存角色设定失败，请重试。')
      setSubmitting(false)
    }
  }

  const saveCharacterPicks = async () => {
    if (!character) return
    await setCampaignCharacter(campaignId, {
      character_id: character.id,
      look_id: selectedLook?.id || '',
      outfit: selectedLook?.outfit || '',
      expression: selectedLook?.expression || '',
      name: character.name,
    })
  }

  const handleNext = async () => {
    if (!canAdvance()) return

    if (isCharacterStage) {
      setSubmitting(true)
      setError(null)
      try {
        await saveCharacterPicks()
        if (ordered.length === 0) {
          await finish()
          return
        }
        const next = step + 1
        setStep(next)
        loadStepIntoLocalState(next, answers)
      } catch {
        setError('保存角色形象失败，请重试。')
      } finally {
        setSubmitting(false)
      }
      return
    }

    const answer = buildAnswer()
    if (!answer) return
    const updated = { ...answers, [current.var_name]: answer }
    setAnswers(updated)

    if (!isLast) {
      const next = step + 1
      setStep(next)
      loadStepIntoLocalState(next, updated)
      return
    }

    await finish()
  }

  const handleBack = () => {
    if (step === 0) return
    const prev = step - 1
    setStep(prev)
    loadStepIntoLocalState(prev, answers)
  }

  const handleSkipCharacter = async () => {
    const next = step + 1
    setStep(next)
    loadStepIntoLocalState(next, answers)
  }

  if (!Array.isArray(characters)) {
    return (
      <div className="min-h-screen bg-black text-white/40 flex items-center justify-center text-xs uppercase tracking-[0.3em]">
        载入角色…
      </div>
    )
  }

  if (!isCharacterStage && !current) {
    return (
      <div className="min-h-screen bg-black text-white flex items-center justify-center">
        <div className="text-center">
          <p className="text-white/40 text-sm mb-6">尚未配置初始化问题。</p>
          <button
            onClick={finish}
            className="px-8 py-3 bg-white text-black rounded-full text-xs font-bold tracking-[0.2em] uppercase"
          >
            直接开始
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-black text-white selection:bg-white/10">
      <div className="min-h-screen bg-black/80 backdrop-blur-sm py-12 px-4">
        <div className="max-w-3xl mx-auto">
          <div className="flex items-baseline justify-between mb-12 pb-4 border-b border-white/10">
            <div>
              <p className="text-[10px] uppercase tracking-[0.3em] text-white/40 font-mono mb-1">
                初始化
              </p>
              <h1 className="text-2xl md:text-3xl font-bold tracking-tight">
                {scenario?.title || '角色设定'}
              </h1>
            </div>
            <p className="text-[10px] uppercase tracking-[0.3em] text-white/40 font-mono">
              第 {step + 1} / {total} 步
            </p>
          </div>

          {isCharacterStage ? (
            <>
              <div className="mb-8">
                <p className="text-xl md:text-2xl font-light leading-relaxed text-white/90">
                  选择你的角色
                </p>
                <p className="text-sm text-white/40 mt-3 leading-relaxed">
                  角色独立于世界存在：这里既有本世界的角色，也有你角色库里带来的角色。服装与表情会影响之后叙事插图中的形象，随时可以在游戏内更换。
                </p>
              </div>

              {!pickerOpen && (
                <div className="mb-6 flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={() => setPickerOpen(true)}
                    className="px-5 py-2.5 rounded-xl border border-white/15 text-white/70 hover:text-white hover:border-white/40 text-[10px] font-bold tracking-[0.15em] uppercase transition-all"
                  >
                    {characterList.length > 1 ? `换一个角色（共 ${characterList.length} 个）` : '角色库快照'}
                  </button>
                  <span className="text-[10px] text-white/30">
                    {libraryIds.has(character.id) ? '来自角色库' : '本世界角色'}
                    {looks.length === 0 ? ' · 还没有立绘' : ''}
                  </span>
                </div>
              )}

              {pickerOpen && (
                <div className="mb-6 grid grid-cols-2 sm:grid-cols-3 gap-3">
                  {characterChoices.map((c) => (
                    <button
                      type="button"
                      key={c.id}
                      onClick={() => pickCharacter(c)}
                      className={
                        'text-left rounded-2xl border overflow-hidden transition-all ' +
                        (c.active
                          ? 'bg-white/[0.08] border-white/40'
                          : 'bg-white/[0.02] border-white/10 hover:border-white/25')
                      }
                    >
                      <span className="block h-28 bg-white/5 flex items-center justify-center">
                        {c.art && <img src={c.art} alt={c.name} className="w-full h-full object-cover" />}
                        {!c.art && <span className="text-[10px] text-white/30">无立绘</span>}
                      </span>
                      <span className="block px-3 py-2">
                        <span className="block text-xs text-white truncate">{c.name}</span>
                        <span className="block text-[10px] text-white/35 truncate">
                          {c.role}
                          {c.inLibrary ? ' · 库' : ''}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              )}

              <div className="flex flex-col sm:flex-row gap-8 mb-10">
                <div className="w-40 h-56 rounded-2xl border border-white/10 bg-white/[0.03] overflow-hidden flex items-center justify-center shrink-0 mx-auto sm:mx-0">
                  {hasArt && (
                    <img
                      src={selectedLook.image_url}
                      alt={character.name}
                      className="w-full h-full object-cover"
                    />
                  )}
                  {!hasArt && <span className="text-white/20 text-xs">暂无立绘</span>}
                </div>

                <div className="flex-1">
                  <div className="mb-5">
                    <div className="text-lg font-semibold text-white">{character.name}</div>
                    <div className="text-xs text-white/40 mt-1">
                      {character.role || '玩家角色'}
                    </div>
                    {hasNoLook && (
                      <p className="text-[10px] text-white/30 mt-2 leading-relaxed">
                        这个角色还没有立绘，叙事照常进行，只是不会被画进插图。可以在角色库里补一张。
                      </p>
                    )}
                    {character.description && (
                      <p className="text-sm text-white/50 mt-3 leading-relaxed">
                        {character.description}
                      </p>
                    )}
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                      <label className="block text-[10px] font-bold uppercase tracking-[0.2em] text-white/40 mb-2">
                        服装
                      </label>
                      <select
                        className={inputBase + ' text-sm py-3'}
                        value={selectedLook?.outfit || ''}
                        onChange={(e) => pickOutfit(e.target.value)}
                      >
                        {outfits.map((outfit) => (
                          <option key={outfit} value={outfit} className="bg-neutral-900">
                            {outfit}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="block text-[10px] font-bold uppercase tracking-[0.2em] text-white/40 mb-2">
                        表情
                      </label>
                      <select
                        className={inputBase + ' text-sm py-3'}
                        value={selectedLook?.id || ''}
                        onChange={(e) => setLookId(e.target.value)}
                      >
                        {shownLooks.map((look) => (
                          <option key={look.id} value={look.id} className="bg-neutral-900">
                            {look.expression || '默认'}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                </div>
              </div>
            </>

          ) : (
            <>
              <div className="mb-10">
                <p className="text-xl md:text-2xl font-light leading-relaxed text-white/90 whitespace-pre-line">
                  {resolvedPrompt}
                </p>
              </div>

              <div className="mb-10">
                {current.type === 'text' && (
                  <textarea
                    autoFocus
                    value={textValue}
                    onChange={(e) => setTextValue(e.target.value)}
                    placeholder="输入你的回答……"
                    rows={3}
                    className={inputBase + ' resize-none'}
                  />
                )}

                {current.type === 'choice' && (
                  <div className="space-y-3">
                    {(current.options || []).map((opt) => {
                      const selected = choiceLabel === opt.label
                      return (
                        <button
                          type="button"
                          key={opt.label}
                          onClick={() => setChoiceLabel(opt.label)}
                          className={
                            'w-full text-left p-5 rounded-2xl border transition-all flex items-start gap-4 ' +
                            (selected
                              ? 'bg-white/[0.08] border-white/40'
                              : 'bg-white/[0.02] border-white/10 hover:border-white/25')
                          }
                        >
                          <span
                            className={
                              'mt-1 inline-block w-3 h-3 rounded-full border ' +
                              (selected ? 'bg-white border-white' : 'border-white/30')
                            }
                          />
                          <span className="flex-1">
                            <span className="block font-semibold text-white text-base mb-1">
                              {opt.label}
                            </span>
                            {opt.description && (
                              <span className="block text-sm text-white/50 leading-relaxed font-light">
                                {opt.description}
                              </span>
                            )}
                          </span>
                        </button>
                      )
                    })}

                    {current.allow_custom && (
                      <div
                        className={
                          'p-5 rounded-2xl border transition-all ' +
                          (choiceLabel === '__custom__'
                            ? 'bg-white/[0.08] border-white/40'
                            : 'bg-white/[0.02] border-white/10 hover:border-white/25')
                        }
                      >
                        <button
                          type="button"
                          onClick={() => setChoiceLabel('__custom__')}
                          className="w-full flex items-center gap-4 text-left"
                        >
                          <span
                            className={
                              'inline-block w-3 h-3 rounded-full border ' +
                              (choiceLabel === '__custom__' ? 'bg-white border-white' : 'border-white/30')
                            }
                          />
                          <span className="font-semibold text-white text-base">自定义</span>
                        </button>
                        {choiceLabel === '__custom__' && (
                          <input
                            autoFocus
                            value={customValue}
                            onChange={(e) => setCustomValue(e.target.value)}
                            placeholder="输入自定义回答……"
                            className="mt-3 w-full bg-black/30 border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/20 focus:outline-none focus:border-white/30 text-sm font-light"
                          />
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </>
          )}

          {error && (
            <div className="mb-6 p-4 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-300 text-sm font-medium">
              {error}
            </div>
          )}

          <div className="flex items-center justify-between pt-6 border-t border-white/10">
            <div className="flex items-center gap-4">
              <button
                type="button"
                onClick={handleBack}
                disabled={step === 0 || submitting}
                className="px-6 py-3 text-xs uppercase tracking-[0.2em] text-white/50 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors font-bold"
              >
                ← 上一步
              </button>
              {isCharacterStage && (
                <button
                  type="button"
                  onClick={handleSkipCharacter}
                  disabled={submitting}
                  className="text-[10px] tracking-widest text-white/30 hover:text-white/70 transition-colors"
                >
                  这次不选角色
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={handleNext}
              disabled={!canAdvance() || submitting}
              className="px-10 py-4 bg-white text-black hover:bg-gray-200 disabled:bg-white/30 disabled:text-black/50 disabled:cursor-not-allowed uppercase text-xs tracking-[0.2em] font-bold rounded-full flex items-center gap-3 transition-all"
            >
              {submitting ? '保存中……' : isLast && !isCharacterStage ? (
                <>
                  开始
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M8 5v14l11-7z" />
                  </svg>
                </>
              ) : '下一步'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
