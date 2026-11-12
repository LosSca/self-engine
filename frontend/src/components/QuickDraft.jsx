import { useState } from 'react'
import { Sparkles, Loader2, Globe, Wand2, ChevronDown } from 'lucide-react'
import { generateDraft, expandDraft, searchWeb } from '../api'

/**
 * 「快速生成 / 联网生成 / 继续扩展」按钮组：把一句提示交给后端，写出一段草稿。
 *
 * 两处作者界面（角色库 / 世界库）都用它，保证同一种交互、同一套降级策略：
 * 后端没配模型时返回模板文本，按钮不会变成死路。
 *
 * 三个动作共用一条链路，区别只在提示词和是否先检索：
 *  - 生成：按 kind + topic 重写整段；
 *  - 联网：先检索主题（真人、神话、现成世界观），再把要点交给模型；
 *  - 扩展：在作者已有的文字后面续写，不覆盖原文。
 *
 * @param {object} props
 * @param {string} props.kind character | world | location | faction | item | lore
 * @param {string} [props.topic] 名字 / 主题，作为生成的第一句锚点，也用于检索
 * @param {string} [props.context] 已有上下文，生成会与之保持一致
 * @param {string} [props.language] 'zh' | 'en'
 * @param {string} [props.value] 当前字段文字；只有它非空时才显示「扩展」
 * @param {(text: string, provider: string) => void} props.onDraft 拿到草稿后整段回填
 * @param {(text: string, provider: string) => void} [props.onAppend] 拿到续写后追加回填
 * @param {string} [props.label] 生成按钮文案，默认「快速生成」
 * @param {'dark'|'light'} [props.tone] 按钮配色，跟随所在页面
 * @param {string} [props.testId] 便于无头验证
 * @param {boolean} [props.allowSearch] 是否显示「联网生成」，默认显示
 */
export default function QuickDraft({
  kind,
  topic = '',
  context = '',
  language = 'zh',
  value = '',
  onDraft,
  onAppend,
  label = '快速生成',
  tone = 'dark',
  testId,
  disabled = false,
  allowSearch = true,
}) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [search, setSearch] = useState(false)
  const [hits, setHits] = useState(null)
  const [peekBusy, setPeekBusy] = useState(false)

  const runDraft = async (withSearch) => {
    if (busy) return
    setBusy(withSearch ? 'search' : 'draft')
    setError('')
    try {
      const res = await generateDraft({ kind, topic, context, language, search: withSearch })
      onDraft?.(res.text || '', res.provider || 'llm')
    } catch (err) {
      setError(err?.message || '生成失败')
    } finally {
      setBusy('')
    }
  }

  const runExpand = async () => {
    if (busy || !value?.trim()) return
    setBusy('expand')
    setError('')
    try {
      const res = await expandDraft({ kind, text: value, topic, language, search })
      const more = res.text || ''
      if (onAppend) onAppend(more, res.provider || 'llm')
      else onDraft?.([value.trim(), more].filter(Boolean).join(''), res.provider || 'llm')
    } catch (err) {
      setError(err?.message || '续写失败')
    } finally {
      setBusy('')
    }
  }

  // 先让作者看到「会读到什么」，再决定是否消耗一次生成。
  const peek = async () => {
    if (peekBusy || !topic.trim()) return
    setPeekBusy(true)
    setError('')
    try {
      const res = await searchWeb({ query: topic, limit: 4 })
      setHits(res.results || [])
      if (!res.count) setError('没有检索到结果，去掉「联网」直接生成即可')
    } catch (err) {
      setError(err?.message || '检索失败')
    } finally {
      setPeekBusy(false)
    }
  }

  // 亮色页面用墨色描边按钮，暗色页面用半透明描边按钮；主按钮留给「保存 / 创建」
  const base =
    'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-semibold tracking-wide transition-all disabled:opacity-40'
  const palette =
    tone === 'light'
      ? 'border border-[#d8d4cc] bg-white text-[#4a463f] hover:border-[#a8a29a] hover:text-[#1a1a1a]'
      : 'border border-white/20 text-white/75 hover:text-white hover:border-white/40'
  const btn = base + ' ' + palette
  const muted = tone === 'light' ? 'text-[#8a857c]' : 'text-white/40'

  return (
    <span className="inline-flex flex-col items-start gap-1 max-w-full">
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          onClick={() => runDraft(search)}
          disabled={disabled || !!busy}
          data-testid={testId}
          className={btn}
        >
          {busy === 'draft' || busy === 'search' ? (
            <Loader2 size={12} className="animate-spin" />
          ) : search ? (
            <Globe size={12} />
          ) : (
            <Sparkles size={12} />
          )}
          {busy === 'draft' ? '生成中…' : busy === 'search' ? '检索并生成…' : search ? '联网生成' : label}
        </button>

        {allowSearch && (
          <>
            <button
              type="button"
              onClick={() => {
                setSearch((s) => !s)
                setHits(null)
              }}
              data-testid={testId ? testId + '-search-toggle' : undefined}
              title="先联网检索主题，再基于检索结果生成"
              aria-pressed={search}
              className={
                btn +
                (search ? (tone === 'light' ? ' !border-[#1a1a1a] !text-[#1a1a1a]' : ' !border-white/60 !text-white') : '')
              }
            >
              <Globe size={12} />
              联网
            </button>
            <button
              type="button"
              onClick={peek}
              disabled={peekBusy || !topic.trim()}
              data-testid={testId ? testId + '-peek' : undefined}
              title="预览检索到的条目"
              className={btn}
            >
              {peekBusy ? <Loader2 size={12} className="animate-spin" /> : <ChevronDown size={12} />}
              预览
            </button>
          </>
        )}

        {value?.trim() && (
          <button
            type="button"
            onClick={runExpand}
            disabled={disabled || !!busy}
            data-testid={testId ? testId + '-expand' : undefined}
            title="在现有文字后面继续写，不覆盖原文"
            className={btn}
          >
            {busy === 'expand' ? <Loader2 size={12} className="animate-spin" /> : <Wand2 size={12} />}
            {busy === 'expand' ? '续写中…' : '扩展'}
          </button>
        )}
      </span>

      {hits && hits.length > 0 && (
        <ul className={'w-full max-w-[320px] text-[10px] leading-snug space-y-0.5 ' + muted} data-testid={testId ? testId + '-hits' : undefined}>
          {hits.map((h, i) => (
            <li key={i} className="truncate">
              <span className={tone === 'light' ? 'text-[#5c574f]' : 'text-white/65'}>· {h.title}</span>
            </li>
          ))}
        </ul>
      )}
      {error && <span className="text-[10px] text-rose-500">{error}</span>}
    </span>
  )
}
