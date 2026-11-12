import { useEffect, useMemo, useState } from 'react'
import { X, ChevronRight, ChevronLeft, Check, HelpCircle } from 'lucide-react'

const STORAGE_KEY = 'lunar_onboarding_v2'
const STORAGE_LEGACY = 'lunar_onboarding_v1'

/**
 * 新手引导。每一步都指向边栏 / 界面上一个真实存在的入口；不弹遮罩（会挡住叙事），
 * 而是嵌在边栏底部的卡片。进度存 localStorage，刷新不会从头再来。
 *
 * 五步覆盖从「选世界」到「把系统配置调顺手」的完整路径：
 * 1. 先选一个世界（首页世界卡片）
 * 2. 认识你的角色（边栏 角色档案）
 * 3. 背包与地图（边栏 背包 / 世界地图）
 * 4. 认识这个系统（讲清楚自指引擎由哪几块组成）
 * 5. 把系统配置好（边栏 设置：模型、温度、内容策略…）
 */

const SYSTEM_ITEMS = [
  { icon: '🧭', name: '左侧栏', desc: '所有功能都收在这里，点开在右侧展开，不打断叙事' },
  { icon: '✍️', name: '叙事引擎', desc: '每回合把行动、记忆、世界状态交给模型，续写下一段剧情' },
  { icon: '🧠', name: '记忆与时序', desc: 'NPC 心智、记忆结晶、剧情进度都可查看、可干预' },
  { icon: '🗺️', name: '世界编织', desc: '地点、势力、关系图在「世界工坊」里增删改' },
]

const CONFIG_ITEMS = [
  { icon: '🎛️', name: '模型服务商', desc: '决定谁写故事：云端 / 本地 NInfer / 自定义兼容端点' },
  { icon: '🌡️', name: '创造性（温度）', desc: '0 偏精准稳定，1 左右均衡，2 最天马行空' },
  { icon: '📏', name: '最大 Token 数', desc: '单回合回复长度上限，短文 1024、长篇 4096 起' },
  { icon: '🔒', name: '内容策略', desc: '标准走云端；NSFW 发给本地模型，内容不出本机' },
  { icon: '⚔️', name: '战斗模式', desc: '无对抗场景可关闭，省掉战斗相关的模型调用' },
]

const STEPS = [
  {
    key: 'world',
    title: '先选一个世界',
    body: '首页有 7 种预设世界，也可以从空白开始搭建。选好之后就能创建情景、开一局。',
    target: null,
    hint: '在「首页」点任意世界卡片',
    only: 'app',
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
  {
    key: 'system',
    title: '认识这个系统',
    body: '这是个「自指引擎」：你的每个行动都写进记忆，再由模型续写成下一段剧情。它由四块组成——',
    target: null,
    hint: '看完点下面的「去配置系统」',
    list: SYSTEM_ITEMS,
  },
  {
    key: 'config',
    title: '把系统配置好',
    body: '左侧「系统 → 设置」里有五个真正影响体验的选项，按口味调一遍就够用了——',
    target: 'settings',
    hint: '点左侧「设置」逐项调整，最后点「应用设置」',
    list: CONFIG_ITEMS,
  },
]

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || ''
    if (!raw) {
      // v1 只有三步且没有系统/配置两步，清掉让老用户也能看到新内容
      if (localStorage.getItem(STORAGE_LEGACY)) localStorage.removeItem(STORAGE_LEGACY)
      return { step: 0, done: false, skipped: false }
    }
    const parsed = JSON.parse(raw)
    return {
      step: Math.max(Number(parsed.step) || 0, 0),
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
 * @param {boolean} props.collapsed 边栏是否收起（收起时只渲染问号按钮）
 * @param {string?} props.activeTool 当前打开的工具，用户真的点开时自动完成该步
 * @param {(id: string|null) => void} [props.onHighlight] 高亮某个边栏条目（由 Sidebar 实现）
 * @param {'app'|'play'} [props.variant] 游玩界面跳过「选世界」这一步
 */
export default function OnboardingGuide({ collapsed, activeTool, onHighlight, variant = 'app' }) {
  const steps = useMemo(
    () => (variant === 'play' ? STEPS.filter((s) => s.only !== 'app') : STEPS),
    [variant],
  )
  const [state, setState] = useState(loadState)
  const [showAgain, setShowAgain] = useState(false)

  useEffect(() => {
    saveState({ ...state, key: steps[state.step]?.key })
  }, [state, steps])

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
  const jumpToConfig = () => {
    setState((s) => ({ ...s, step: Math.min(s.step + 1, steps.length - 1) }))
  }

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
              className={'h-1.5 w-4 rounded-full transition-colors ' + (filled ? 'bg-cyan-300' : i === state.step ? 'bg-cyan-300/50' : 'bg-white/15')}
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
            左侧每个图标都是一个工具，点开就在右边展开。设置随时能在「系统 → 设置」里改，这份引导也能从这里重新打开。
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

          {step.list && (
            <ul className="mb-2 space-y-1" data-testid={'onboarding-list-' + step.key}>
              {step.list.map((it) => (
                <li key={it.name} className="flex items-start gap-1.5 text-[10px] leading-relaxed">
                  <span className="w-3.5 shrink-0 text-center">{it.icon}</span>
                  <span className="text-white/75">
                    <span className="font-semibold text-white/90">{it.name}</span>
                    <span className="text-white/55"> · {it.desc}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}

          <p className="mb-2 rounded-lg bg-black/30 px-2 py-1 text-[10px] text-cyan-200/80">{step.hint}</p>

          {step.key === 'system' && (
            <button
              type="button"
              onClick={jumpToConfig}
              data-testid="onboarding-to-config"
              className="mb-2 w-full rounded-lg border border-cyan-400/40 bg-cyan-400/10 px-2 py-1.5 text-[11px] font-semibold text-cyan-100 hover:bg-cyan-400/20 transition-colors"
            >
              去配置系统 →
            </button>
          )}

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
