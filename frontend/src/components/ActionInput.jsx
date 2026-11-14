import { useState, useEffect, useRef, useCallback } from 'react'
import { Send } from 'lucide-react'
import { fetchCharacters } from '../api'
import { useGameStore } from '../store'

const ACTION_TYPES = [
  { id: 'DO', label: '行动', description: '执行一个动作' },
  { id: 'SAY', label: '对话', description: '说话或交流' },
  { id: 'CONTINUE', label: '继续', description: '让故事自然推进' },
  { id: 'META', label: '旁白', description: '与 AI 叙事者对话' },
]

export default function ActionInput({ onSubmit, disabled, draft }) {
  const [text, setText] = useState('')
  const [type, setType] = useState('DO')
  // Who this turn is aimed at. Carried from suggestion buttons (which know the
  // entity id) and from typing @name, so the server can bind the reaction to a
  // specific character instead of guessing from prose.
  const [target, setTarget] = useState(null) // { id, name }
  const [mentionState, setMentionState] = useState(null) // { startIndex, query }
  const [mentionResults, setMentionResults] = useState([])
  const [mentionIndex, setMentionIndex] = useState(0)
  const textareaRef = useRef(null)
  const dropdownRef = useRef(null)
  const lastSpaceRef = useRef(0)
  const campaignId = useGameStore((s) => s.activeCampaignId)

  // Load an externally supplied "[TYPE] text" draft into the field for editing.
  // The props are tracked by stamp and applied during render (the lint rule
  // forbids mirroring props into state from an effect); focusing is the only
  // side effect left for the effect below.
  const [seenDraftAt, setSeenDraftAt] = useState(0)
  if (draft?.text && draft.at && draft.at !== seenDraftAt) {
    const match = draft.text.match(/^\[(DO|SAY|CONTINUE|META)\]\s*/i)
    setSeenDraftAt(draft.at)
    setType(match ? match[1].toUpperCase() : 'DO')
    setText(match ? draft.text.slice(match[0].length) : draft.text)
    setTarget(draft.target?.name ? draft.target : null)
  }
  useEffect(() => {
    if (!seenDraftAt) return
    textareaRef.current?.focus()
  }, [seenDraftAt])

  // Listen to keyboard shortcuts for continue. Space-space is the main one:
  // "keep going" is the most common move, and typing it is a distraction.
  useEffect(() => {
    const handleGlobalKey = (e) => {
      const tag = (e.target?.tagName || '').toLowerCase()
      const typing = tag === 'input' || tag === 'textarea' || e.target?.isContentEditable
      // Double space pauses the story anywhere on the page (inside the input it
      // would fight with real typing, so there it is ignored).
      if (e.code === 'Space' && !typing && !e.repeat) {
        if (disabled) return
        const now = Date.now()
        if (now - lastSpaceRef.current < 450) {
          lastSpaceRef.current = 0
          e.preventDefault()
          onSubmit('[CONTINUE]')
          return
        }
        lastSpaceRef.current = now
        return
      }
      // Ctrl+Enter or Cmd+Enter defaults to continue if empty text
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        if (!text.trim() && !disabled) {
          e.preventDefault()
          onSubmit('[CONTINUE]')
        }
      }
    }
    window.addEventListener('keydown', handleGlobalKey)
    return () => window.removeEventListener('keydown', handleGlobalKey)
  }, [text, disabled, onSubmit])

  // Fetch characters when mention query changes
  useEffect(() => {
    if (!mentionState || !campaignId) return
    let cancelled = false
    const timer = setTimeout(async () => {
      try {
        const chars = await fetchCharacters(campaignId, mentionState.query)
        if (!cancelled) {
          setMentionResults(chars)
          setMentionIndex(0)
        }
      } catch {
        if (!cancelled) setMentionResults([])
      }
    }, 100) // Small debounce
    return () => { cancelled = true; clearTimeout(timer) }
  }, [mentionState, campaignId])

  // Closing the dropdown is derived, not stored: no mention, no results.
  const visibleResults = mentionState ? mentionResults : []

  const closeMention = useCallback(() => {
    setMentionState(null)
    setMentionResults([])
    setMentionIndex(0)
  }, [])

  const insertMention = useCallback((char) => {
    if (!mentionState) return
    const charName = typeof char === 'string' ? char : char?.name || ''
    setTarget(typeof char === 'string' ? { id: '', name: charName } : { id: char?.entity_id || char?.id || '', name: charName })
    const before = text.slice(0, mentionState.startIndex)
    const after = text.slice(mentionState.startIndex + mentionState.query.length + 1) // +1 for @
    const newText = `${before}@${charName}${after}`
    setText(newText)
    closeMention()
    // Refocus textarea
    setTimeout(() => textareaRef.current?.focus(), 0)
  }, [mentionState, text, closeMention])

  const handleTextChange = (e) => {
    const value = e.target.value
    setText(value)

    // Detect @ mention trigger
    const cursorPos = e.target.selectionStart
    const textBeforeCursor = value.slice(0, cursorPos)
    const atMatch = textBeforeCursor.match(/@([^\s@]*)$/)

    if (atMatch) {
      const startIndex = cursorPos - atMatch[0].length
      setMentionState({ startIndex, query: atMatch[1] })
    } else {
      closeMention()
    }
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (visibleResults.length > 0) return // Don't submit while picking mention
    const trimmed = text.trim()
    if (!trimmed && type !== 'CONTINUE' || disabled) return
    onSubmit(type === 'CONTINUE' ? '[CONTINUE]' : `[${type}] ${trimmed}`, target)
    setText('')
    setTarget(null)
    closeMention()
  }

  const handleKeyDown = (e) => {
    // Handle mention dropdown navigation
    if (visibleResults.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setMentionIndex((prev) => Math.min(prev + 1, visibleResults.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setMentionIndex((prev) => Math.max(prev - 1, 0))
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        insertMention(visibleResults[mentionIndex])
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        closeMention()
        return
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="p-4 md:p-6 max-w-4xl mx-auto w-full">
      {target?.name && type !== 'CONTINUE' && (
        <div className="mb-3 flex items-center gap-2 text-xs">
          <span className="text-white/40">本回合指向</span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-cyan-400/40 bg-cyan-400/10 px-2.5 py-1 text-cyan-100" data-testid="action-target">
            {target.name}
            <button
              type="button"
              onClick={() => setTarget(null)}
              className="text-white/50 hover:text-white"
              aria-label="取消目标"
            >
              ×
            </button>
          </span>
        </div>
      )}
      <div className="flex gap-2 mb-3">
        {ACTION_TYPES.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => {
              setType(t.id)
              if (t.id === 'CONTINUE') setText('')
            }}
            title={t.description}
            className={`px-4 py-2.5 sm:py-1.5 rounded-lg text-xs font-bold uppercase tracking-widest transition-all duration-200 min-h-[44px] sm:min-h-0
              ${type === t.id
                ? 'bg-white/10 text-white border border-white/20 shadow-[0_0_10px_rgba(99,102,241,0.2)]'
                : 'bg-transparent text-white/20 hover:text-white/60 hover:bg-white/5 border border-transparent'
              }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex gap-3 items-end">
        {type === 'CONTINUE' ? (
          <button
            type="button"
            onClick={handleSubmit}
            disabled={disabled}
            className="flex-1 text-left bg-white/[0.03] border border-white/20 rounded-xl px-5 py-3.5 text-white hover:bg-white/[0.03] hover:border-white/20 transition-all font-light text-sm group disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            <span className="opacity-70 group-hover:opacity-100 transition-opacity">按 Enter 继续推进故事……</span>
          </button>
        ) : (
          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={handleTextChange}
              onKeyDown={handleKeyDown}
              disabled={disabled}
              placeholder={
                disabled
                  ? '正在接收回应……'
                  : target?.name
                  ? `对${target.name}说什么……（双击空格继续）`
                  : type === 'DO'
                  ? '输入你的行动……（用 @ 提及角色）'
                  : type === 'SAY'
                  ? '输入你想说的话……（用 @ 提及角色）'
                  : '输入系统或旁白指令……'
              }
              rows={2}
              className="w-full bg-white/[0.03] border border-white/10 rounded-xl px-5 py-3.5 text-white placeholder-white/20 focus:outline-none focus:border-white/40 focus:bg-white/[0.05] resize-none text-sm font-light transition-all custom-scrollbar leading-relaxed"
            />
            {/* @-mention autocomplete dropdown */}
            {visibleResults.length > 0 && (
              <div
                ref={dropdownRef}
                className="absolute bottom-full left-0 mb-2 w-72 max-h-48 overflow-y-auto bg-lunar-900 border border-white/20 rounded-lg shadow-xl z-50"
              >
                {visibleResults.map((char, i) => (
                  <button
                    key={char.name}
                    type="button"
                    onClick={() => insertMention(char.name)}
                    className={`w-full text-left px-4 py-2.5 text-sm flex items-center gap-3 transition-colors
                      ${i === mentionIndex
                        ? 'bg-indigo-500/20 text-white'
                        : 'text-white/70 hover:bg-white/5 hover:text-white'
                      }`}
                  >
                    <span className="text-indigo-400 text-xs font-mono shrink-0">@</span>
                    <div className="min-w-0">
                      <div className="font-medium truncate">{char.name}</div>
                      {char.aliases?.length > 0 && (
                        <div className="text-xs text-white/30 truncate">
                          别名 {char.aliases.join(', ')}
                        </div>
                      )}
                    </div>
                    <span className="ml-auto text-xs text-white/20 uppercase shrink-0">{char.type}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <button
          type="submit"
          disabled={disabled || (type !== 'CONTINUE' && !text.trim())}
          className="bg-white text-black hover:bg-gray-200 uppercase text-sm tracking-[0.2em] font-bold rounded-full px-5 py-3.5 h-[52px] flex items-center justify-center shrink-0 border border-white/20"
          title="发送（Enter）"
        >
          <Send size={18} className="transform translate-x-[-1px] translate-y-[1px]" />
        </button>
      </div>
    </form>
  )
}
