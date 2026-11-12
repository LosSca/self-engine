import { useState } from 'react'
import { Sparkles, Loader2 } from 'lucide-react'
import { generateDraft } from '../api'

/**
 * 「快速生成」按钮：把一句提示交给后端，写出一段草稿填进目标字段。
 *
 * 两处作者界面（角色库 / 世界库）都用它，保证同一种交互、同一套降级策略：
 * 后端没配模型时返回模板文本，按钮不会变成死路。
 *
 * @param {object} props
 * @param {string} props.kind character | world | location | faction | item | lore
 * @param {string} [props.topic] 名字 / 主题，作为生成的第一句锚点
 * @param {string} [props.context] 已有上下文，生成会与之保持一致
 * @param {string} [props.language] 'zh' | 'en'
 * @param {(text: string, provider: string) => void} props.onDraft 拿到草稿后回填
 * @param {string} [props.label] 按钮文案，默认「快速生成」
 * @param {'dark'|'light'} [props.tone] 按钮配色，跟随所在页面
 * @param {string} [props.testId] 便于无头验证
 */
export default function QuickDraft({
  kind,
  topic = '',
  context = '',
  language = 'zh',
  onDraft,
  label = '快速生成',
  tone = 'dark',
  testId,
  disabled = false,
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const run = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const res = await generateDraft({ kind, topic, context, language })
      onDraft?.(res.text || '', res.provider || 'llm')
    } catch (err) {
      setError(err?.message || '生成失败')
    } finally {
      setBusy(false)
    }
  }

  // 亮色页面用墨色描边按钮，暗色页面用半透明描边按钮；主按钮留给「保存 / 创建」
  const base =
    'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-semibold tracking-wide transition-all disabled:opacity-40'
  const palette =
    tone === 'light'
      ? 'border border-[#d8d4cc] bg-white text-[#4a463f] hover:border-[#a8a29a] hover:text-[#1a1a1a]'
      : 'border border-white/20 text-white/75 hover:text-white hover:border-white/40'

  return (
    <span className="inline-flex flex-col items-start gap-1">
      <button
        type="button"
        onClick={run}
        disabled={disabled || busy}
        data-testid={testId}
        className={base + ' ' + palette}
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
        {busy ? '生成中…' : label}
      </button>
      {error && <span className="text-[10px] text-rose-500">{error}</span>}
    </span>
  )
}
