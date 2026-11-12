import { useEffect, useMemo, useState } from 'react'
import { X, ChevronRight, ChevronLeft, Check, HelpCircle } from 'lucide-react'

const STORAGE_KEY = 'lunar_onboarding_v1'

/**
 * 新手引导。三步走，每一步都指向边栏上的一个真实入口：
 * 1. 从首页开始（选一个世界类型）
 * 2. 进入游玩后先看角色档案
 * 3. 用背包 / 地图推进剧情
 *
 * 设计取舍：
 * - 不做全屏遮罩浮层（会挡住正在读的叙事），而是嵌在边栏里的卡片；
 * - 进度存 localStorage，刷新页面不会从头再来；
 * - 用户可以随时点「跳过」永久关闭，也可以从边栏底部的问号重新打开。
 */

const STEPS = [
  {
    key: 'world',
    title: '先选一个世界',
    body: '首页有 7 种预设世界，也可以从空白开始搭建。选好之后就能创建情景、开一局。',
    target: null,
    hint: '在「首页」点任意世界卡片',
  },
  {
    key: 'character',
    title: '认识你的角色',
    body: '开局后点左边的「角色档案」，可以查看属性和外观，也能随时更换。',
    target: 'character',
    hint: '点左侧「角色档案」',
  },
  {
    key: 'inventory',
    title: '背包与地图',
    body: '「背包」管道具，「世界地图」看地点与关系图。这两个面板都从左侧打开，不会打断叙事。',
    target: 'inventory',
    hint: '点左侧「背包」或「世界地图」',
  },
]

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { step: 0, done: false, skipped: false }
    const parsed = JSON.parse(raw)
    return {
      step: Math.min(Math.max(Number(parsed.step) || 0, 0), STEPS.length - 1),
      done: !!parsed.done,
      skipped: !!parsed.skipped,
    }
  } catch {
    return { step: 0, done: false, skipped: false }
  }
}

function saveState(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    // 本地存储不可用时只在本次会话里记住
  }
}

/**
 * @param {object} props
 * @param {boolean} props.collapsed 边栏是否收起（收起时不渲染卡片）
 * @param {string?} props.activeTool 当前打开的工具，用于在完成一步时自动前进
 * @param {() => void} [props.onHighlight] 高亮某个边栏条目（由 Sidebar 实现）
 */
export default function OnboardingGuide({ collapsed, activeTool, onHighlight, variant = 'app' }) {
  // 游玩界面里已经没有「选世界」这一步了，直接从角色档案讲起
  const steps = useMemo(() => (variant === 'play' ? STEPS.slice(1) : STEPS), [variant])
  const [state, setState] = useState(loadState)
  const [showAgain, setShowAgain] = useState(false)

  useEffect(() => {
    saveState(state)
  }, [state])

  // 用户在引导指向某个工具时真的点开了它，就自动算作完成这一步。
  // 不用 effect 监听 activeTool（react-hooks/set-state-in-effect 禁止在 effect 里同步 setState），
  // 而是渲染时比对上一步的值，用「派生 + 渲染期修正」的方式推进。
  const expected = steps[state.step]?.target
  const [seenTool, setSeenTool] = useState(activeTool)
  if (activeTool !== seenTool) {
    setSeenTool(activeTool)
    if (expected && activeTool === expected && !state.done && !state.skipped) {
      setState((s) => (s.step >= steps.length - 1 ? { ...s, done: true } : { ...s, step: s.step + 1 }))
    }
  }

  useEffect(() => {
    onHighlight?.(state.done || state.skipped ? null : steps[state.step]?.target || null)
  }, [state.step, state.done, state.skipped, onHighlight, steps])

  const step = steps[Math.min(state.step, steps.length - 1)]
  const visible = !collapsed && (showAgain || (!state.done && !state.skipped))

  const advance = () => {
    setState((s) => {
      if (s.step >= steps.length - 1) return { ...s, done: true }
      return { ...s, step: s.step + 1 }
    })
  }
  const back = () => setState((s) => ({ ...s, step: Math.max(0, s.step - 1) }))
  const skip = () => setState((s) => ({ ...s, skipped: true, done: false }))
  const restart = () => setState({ step: 0, done: false, skipped: false })

  const progress = useMemo(() => {
    const filled = state.done ? steps.length : state.step
    return Array.from({ length: steps.length }, (_, i) => i < filled)
  }, [state.step, state.done, steps])

  // 收起状态下只留一个问号按钮，不占空间
  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => { setShowAgain(true); window.dispatchEvent(new Event('lunar:expand-sidebar')) }}
        title="新手引导"
        aria-label="新手引导"
        data-testid="onboarding-compact"
        className="mx-auto mb-1 flex h-9 w-9 items-center justify-center rounded-xl text-white/60 hover:text-white hover:bg-white/[0.10] transition-all"
      >
        <HelpCircle size={16} />
      </button>
    )
  }

  if (!visible) {
    return (
      <button
        type="button"
        onClick={() => { setShowAgain(true); restart() }}
        data-testid="onboarding-reopen"
        className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-white/60 hover:text-white hover:bg-white/[0.10] transition-all"
      >
        <HelpCircle size={16} className="shrink-0" />
        <span className="text-xs font-medium">新手引导</span>
      </button>
    )
  }

  return (
    <div
      data-testid="onboarding-card"
      data-step={step.key}
      className="mx-2 mb-2 rounded-2xl border border-cyan-400/25 bg-cyan-400/[0.07] p-3"
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5">
          {progress.map((filled, i) => (
            <span
              key={i}
              className={`h-1.5 w-4 rounded-full transition-colors ${filled ? 'bg-cyan-300' : i === state.step ? 'bg-cyan-300/50' : 'bg-white/15'}`}
            />
          ))}
        </div>
        <div className="flex items-center gap-1">
          {state.done && <Check size={13} className="text-cyan-300" />}
          <button
            type="button"
            onClick={() => { skip(); setShowAgain(false) }}
            title="跳过引导"
            aria-label="跳过引导"
            className="rounded-md p-0.5 text-white/45 hover:text-white hover:bg-white/10 transition-colors"
          >
            <X size={13} />
          </button>
        </div>
      </div>

      {state.done ? (
        <>
          <p className="mb-1 text-xs font-bold text-white">引导完成</p>
          <p className="mb-2 text-[11px] leading-relaxed text-white/65">
            左侧每个图标都是一个工具，点开就在右边展开。随时可以从这里重新打开这份引导。
          </p>
          <button
            type="button"
            onClick={() => { setShowAgain(false); restart() }}
            className="w-full rounded-lg border border-white/15 px-2 py-1.5 text-[11px] font-medium text-white/80 hover:border-white/35 hover:text-white transition-colors"
          >
            再看一遍
          </button>
        </>
      ) : (
        <>
          <p className="mb-1 text-xs font-bold text-white">
            {state.step + 1}. {step.title}
          </p>
          <p className="mb-2 text-[11px] leading-relaxed text-white/65">{step.body}</p>
          <p className="mb-2 rounded-lg bg-black/30 px-2 py-1 text-[10px] text-cyan-200/80">{step.hint}</p>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={back}
              disabled={state.step === 0}
              className="flex items-center gap-1 rounded-lg border border-white/15 px-2 py-1 text-[11px] text-white/75 enabled:hover:border-white/35 enabled:hover:text-white disabled:opacity-35 transition-colors"
            >
              <ChevronLeft size={12} />
              上一步
            </button>
            <button
              type="button"
              onClick={advance}
              data-testid="onboarding-next"
              className="ml-auto flex items-center gap-1 rounded-lg bg-cyan-400/90 px-2.5 py-1 text-[11px] font-semibold text-black hover:bg-cyan-300 transition-colors"
            >
              {state.step === steps.length - 1 ? '完成' : '下一步'}
              <ChevronRight size={12} />
            </button>
          </div>
        </>
      )}
    </div>
  )
}
