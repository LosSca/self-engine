import { lazy, Suspense, useState, useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import { Sparkles, Undo2, RefreshCw, UserRound, PanelRightOpen } from 'lucide-react'
import { useGameStore } from '../store'
import {
  streamAction,
  fetchEngineAvailability,
  submitTextAction,
  submitAction,
  fetchJournal,
  fetchHistory,
  fetchInventory as fetchInventoryApi,
  rewindLastAction,
  fetchScenarioView,
  regenerateOpening,
  fetchTraces,
  fetchDomainEvents,
  fetchCampaignRequests,
  fetchActionRecord,
  getAgents,
  getAgentCalls,
  deleteTraces,
  fetchCampaignCharacter,
  fetchCampaignStory,
} from '../api'
import ActionInput from './ActionInput'
import Sidebar from './Sidebar'
import ToolDock from './ToolDock'
import SuggestionList from './SuggestionList'
import JournalPanel from './JournalPanel'
import StoryProgressPanel from './StoryProgressPanel'
import ActionPreviewPanel from './ActionPreviewPanel'
import CombatOverlay from './CombatOverlay'
import SettingsPanel from './SettingsPanel'
import PlotGeneratorPanel from './PlotGeneratorPanel'
import TimeskipModal from './TimeskipModal'
import NpcInspector from './NpcInspector'
import MemoryInspector from './MemoryInspector'
import MiniMap from './MiniMap'
// 地图看一眼才用到，ForceGraph2D 连同 d3 有 300KB+，不能让它压在游玩首屏上。
const WorldMapModal = lazy(() => import('./WorldMapModal'))
import InventoryPanel from './InventoryPanel'
import DevtoolsPanel from './DevtoolsPanel'
import CharacterProfilePanel from './CharacterProfilePanel'
import WorldStudio from './WorldStudio'

/** Render text with @mentions highlighted in indigo */
function MentionText({ children }) {
  if (typeof children !== 'string') return children
  // Match @Name (captures multi-word names like @Satoru Gojo until punctuation or end)
  const parts = children.split(/(@[A-Z\u00C0-\u024F][A-Za-z\u00C0-\u024F]*(?:\s+[A-Z\u00C0-\u024F][A-Za-z\u00C0-\u024F]*)*)/g)
  if (parts.length === 1) return children
  return parts.map((part, i) => {
    if (part.startsWith('@')) {
      return (
        <span key={i} className="text-indigo-400 font-medium" title={part.slice(1)}>
          {part}
        </span>
      )
    }
    return part
  })
}

/** ReactMarkdown components override to render @mentions in all text */
const mentionComponents = {
  p: ({ children }) => <p>{processChildren(children)}</p>,
  li: ({ children }) => <li>{processChildren(children)}</li>,
  em: ({ children }) => <em>{processChildren(children)}</em>,
  strong: ({ children }) => <strong>{processChildren(children)}</strong>,
}

function processChildren(children) {
  if (!children) return children
  if (typeof children === 'string') return <MentionText>{children}</MentionText>
  if (Array.isArray(children)) {
    return children.map((child, i) => {
      if (typeof child === 'string') return <MentionText key={i}>{child}</MentionText>
      return child
    })
  }
  return children
}

/** Split narrative at the paragraph where a character first appears, so their
 *  art sits right above that beat instead of on top of the whole message. */
function splitAtCharacter(text, name) {
  if (!text || !name) return ['', text]
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = new RegExp(`\\b${escaped}\\b`).exec(text)
  if (!match) return ['', text]
  const breakAt = text.lastIndexOf('\n\n', match.index)
  if (breakAt <= 0) return ['', text]
  return [text.slice(0, breakAt), text.slice(breakAt + 2)]
}

/** Character art shown when an NPC enters the scene */
function CharacterCard({ image }) {
  const [failed, setFailed] = useState(false)
  if (failed) return null
  return (
    <figure className="mb-4 overflow-hidden rounded-xl border border-white/10 shadow-[0_8px_30px_rgba(0,0,0,0.4)]">
      <img
        src={image.url}
        alt={image.name}
        loading="lazy"
        onError={() => setFailed(true)}
        className="w-full block"
      />
      {(image.caption || image.name) && (
        <figcaption className="px-4 py-2 bg-[#fffdf9] text-[10px] font-bold uppercase tracking-widest font-mono text-white/50">
          {image.caption ? `${image.caption} · ${image.name}` : image.name}
        </figcaption>
      )}
    </figure>
  )
}

function NarratorSkeleton() {
  return (
    <div className="max-w-3xl pr-4 animate-pulse">
      <div className="flex items-center gap-2 mb-2 ml-1 opacity-60">
        <div className="w-1.5 h-1.5 rounded-full bg-white/[0.03]" />
        <span className="text-[10px] font-bold uppercase tracking-widest font-mono text-white/60">Narrator Core</span>
      </div>
      <div className="space-y-3">
        <div className="h-3 bg-white/[0.03] rounded w-[85%]" />
        <div className="h-3 bg-white/[0.03] rounded w-[70%]" />
        <div className="h-3 bg-white/[0.03] rounded w-[55%]" />
      </div>
    </div>
  )
}

/** Compact token count: 133200 -> "133.2k" (FASE 0 devtools readout) */
function fmtTok(n) {
  const v = Number(n) || 0
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}

/** Per-call breakdown tooltip for the usage readout */
function usageTitle(u) {
  if (!u) return ''
  const lines = (u.calls || []).map(
    (c) =>
      `${c.caller}: in ${c.input_tokens} out ${c.output_tokens}` +
      (c.cache_read ? ` cache_r ${c.cache_read}` : '') +
      ` (${c.elapsed_s}s)`
  )
  return [
    `${u.call_count} 次同步调用，共 ${u.total_time_s}s`,
    `输入 ${u.total_input_tokens} · 输出 ${u.total_output_tokens}`,
    `缓存读取 ${u.total_cache_read_tokens} · 缓存写入 ${u.total_cache_creation_tokens}`,
    '',
    ...lines,
  ].join('\n')
}

// Storyline progress is authored data rendered as-is; nothing here decides an outcome.
function storyNodeTitle(story, nodeId) {
  if (!nodeId || !story || !story.storyline) return ''
  const node = (story.storyline.nodes || []).find((item) => item.id === nodeId)
  return node ? node.title || node.id : nodeId
}

function storyTitle(story) {
  const armed = (story.armed_packs || []).join('、') || '无'
  const fired = (story.fired_packs || []).join('、') || '无'
  const pending = storyNodeTitle(story, (story.pending || [])[0]) || '无'
  return `当前剧情线：${story.storyline?.name || '未命名'}\n已启用事件包：${armed}\n已触发事件包：${fired}\n下一步：${pending}`
}

export default function GameCanvas() {
  const {
    messages,
    isStreaming,
    combatMode,
    appendMessage,
    appendToLastMessage,
    setLastMessageImage,
    setStreaming,
    setCombatMode,
    activeCampaignId,
    activeScenario,
    journal,
    addJournalEntry,
    setJournal,
    inventory,
    setInventory,
    addInventoryItem,
    updateInventoryItem: updateInventoryStore,
    restoreSession,
    maxTokens,
    llmProvider,
    llmModel,
    temperature,
    combatEnabled,
    updateSettings,
    replaceLastAssistantMessage,
    popLastPair,
    lastUsage,
    setLastUsage,
    traces,
    pushTrace,
    clearTraces,
    setTraces,
  } = useGameStore()
  const bottomRef = useRef(null)
  // 所有工具面板共用右侧停靠区的同一个槽位：null 表示收起。
  // 'character' | 'inventory' | 'map' | 'journal' | 'story' | 'plotgen'
  // 'timeskip' | 'npc' | 'memory' | 'worldstudio' | 'devtools' | 'settings'
  const [activeTool, setActiveTool] = useState(null)
  const [playerCharacter, setPlayerCharacter] = useState(null)
  const [rewinding, setRewinding] = useState(false)
  const [scenarioView, setScenarioView] = useState(null)
  const [regenerating, setRegenerating] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [draft, setDraft] = useState(null)
  // 'engine' = the campaign has an authoritative world bound, so rules decide
  // every outcome; 'narrative' = the legacy free-form LLM lane.
  const [engineLane, setEngineLane] = useState({ checked: false, bound: false, state: null })
  // Storyline progress for the bound campaign: entered nodes, armed event
  // packs. Read-only; the engine advances it, the UI only reports it.
  const [campaignStory, setCampaignStory] = useState(null)


  // Restore session on mount if state is missing
  useEffect(() => {
    if (!activeCampaignId) restoreSession()
  }, [])

  // Pull the per-campaign resolved scenario surface (interpolated opening,
  // tone, lore) so we never render raw {var} tokens in-game. Falls back to
  // the raw activeScenario when the request fails.
  useEffect(() => {
    if (!activeCampaignId) {
      setScenarioView(null)
      return
    }
    let cancelled = false
    fetchScenarioView(activeCampaignId)
      .then((view) => {
        if (cancelled) return
        setScenarioView(view)
        if (view && typeof view.combat_enabled === 'boolean') {
          updateSettings({ combatEnabled: view.combat_enabled })
        }
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [activeCampaignId])

  // Restore chat history from backend when campaign has no local messages.
  // The same effect runs twice under StrictMode, so every async result is
  // canceled by its cleanup before the second pass can append it.
  useEffect(() => {
    if (!activeCampaignId) return undefined
    let cancelled = false
    if (messages.length === 0) {
      fetchHistory(activeCampaignId)
        .then(({ messages: history }) => {
          if (cancelled) return
          if (history && history.length > 0) {
            history.forEach((msg) => appendMessage(msg))
          }
        })
        .catch(() => {})
    }
    fetchInventoryApi(activeCampaignId)
      .then((items) => {
        if (!cancelled) setInventory(items)
      })
      .catch(() => {})
    fetchJournal(activeCampaignId)
      .then((entries) => {
        if (!cancelled) setJournal(entries)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [activeCampaignId])

  const loadPlayerCharacter = async () => {
    if (!activeCampaignId) {
      setPlayerCharacter(null)
      return
    }
    try {
      const data = await fetchCampaignCharacter(activeCampaignId)
      const look = data?.look || null
      setPlayerCharacter(
        data?.character
          ? { ...data.character, currentLook: look }
          : null
      )
    } catch {
      setPlayerCharacter(null)
    }
  }

  useEffect(() => {
    loadPlayerCharacter()
  }, [activeCampaignId])

  const loadTraces = async () => {
    if (!activeCampaignId) return
    const list = await fetchTraces(activeCampaignId)
    setTraces(list)
  }

  useEffect(() => {
    loadTraces()
  }, [activeCampaignId])

  const resolvedOpening = scenarioView?.opening_narrative ?? activeScenario?.opening_narrative ?? ''
  const resolvedTone = scenarioView?.tone_instructions ?? activeScenario?.tone_instructions ?? ''
  const canRerollOpening =
    !!scenarioView &&
    scenarioView.opening_mode === 'ai' &&
    messages.length === 0 &&
    !isStreaming &&
    !regenerating

  const handleRerollOpening = async () => {
    if (!activeCampaignId || !canRerollOpening) return
    setRegenerating(true)
    try {
      const result = await regenerateOpening(activeCampaignId)
      setScenarioView((prev) => ({
        ...(prev || {}),
        opening_narrative: result.opening_narrative,
        has_generated_opening: true,
        opening_mode: prev?.opening_mode ?? 'ai',
      }))
    } catch (err) {
      const detail = err?.detail || '无法重新生成开场。'
      window.alert(detail)
    } finally {
      setRegenerating(false)
    }
  }

  const formatAutoPlotMessage = (plot) => {
    const kind = String(plot?.kind || '').toLowerCase()
    const data = plot?.data || {}

    if (kind === 'npc') {
      return [
        '### 自动剧情：新 NPC 登场',
        `**${data.name || '未知'}** (战力 ${data.power_level || 5}/10)`,
        data.appearance || '',
        data.goal ? `目标：${data.goal}` : '',
        data.secret ? `秘密：${data.secret}` : '',
      ].filter(Boolean).join('\n\n')
    }

    if (kind === 'event') {
      const choices = Array.isArray(data.choices)
        ? data.choices.map((choice, index) => `${index + 1}. ${choice}`).join('\n')
        : ''

      return [
        '### 自动剧情：随机事件',
        `**${data.title || '意外事件'}**`,
        data.description || '',
        choices ? `选项：\n${choices}` : '',
      ].filter(Boolean).join('\n\n')
    }

    return [
      '### 自动剧情：剧情线',
      typeof data === 'string' ? data : (data.text || '一条新的剧情支线浮现。'),
    ].join('\n\n')
  }

  const scrollContainerRef = useRef(null)
  const userScrolledUp = useRef(false)

  useEffect(() => {
    const container = scrollContainerRef.current
    if (!container) return
    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container
      userScrolledUp.current = scrollHeight - scrollTop - clientHeight > 120
    }
    container.addEventListener('scroll', handleScroll)
    return () => container.removeEventListener('scroll', handleScroll)
  }, [])

  useEffect(() => {
    if (!userScrolledUp.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  const refreshStory = async () => {
    if (!activeCampaignId) return
    try {
      setCampaignStory(await fetchCampaignStory(activeCampaignId))
    } catch {
      /* 剧情进度是可选数据 */
    }
  }

  const loadJournal = async () => {
    if (activeCampaignId) {
      try {
        const entries = await fetchJournal(activeCampaignId)
        setJournal(entries)
      } catch {
        /* journal is optional */
      }
    }
  }

  // 打开工具前先把数据取回来（日志、调试台依赖每次实时的后端数据），
  // 然后再把面板切到停靠区。
  const openTool = async (id) => {
    if (!id) { setActiveTool(null); return }
    if (id === 'journal') loadJournal()
    if (id === 'story') refreshStory()
    if (id === 'devtools') loadTraces()
    if (id === 'character') loadPlayerCharacter()
    if (id === 'worldstudio') setActiveTool(id)
    setActiveTool(id)
  }

  const refreshJournal = async () => {
    if (activeCampaignId) {
      try {
        const entries = await fetchJournal(activeCampaignId)
        setJournal(entries)
      } catch {
        /* journal is optional */
      }
    }
  }

  // A campaign is rule-driven as soon as it has a bound world. Probed once per
  // campaign; a 404 from /state means the legacy narrative lane still owns it.
  useEffect(() => {
    let cancelled = false
    if (!activeCampaignId) {
      setEngineLane({ checked: false, bound: false, state: null })
      return () => { cancelled = true }
    }
    fetchEngineAvailability(activeCampaignId).then((availability) => {
      if (cancelled) return
      setEngineLane({ checked: true, ...availability })
      if (availability && availability.bound) {
        fetchCampaignStory(activeCampaignId).then((story) => { if (!cancelled) setCampaignStory(story) }).catch(() => {})
      }
    })
    return () => { cancelled = true }
  }, [activeCampaignId])

  const applyEngineResult = (result) => {
    const narrative = result?.narrative
    const text = narrative?.text?.trim()
    const parts = []
    const fallback = []
    if (result?.ok) {
      ;(result.effects_applied || []).forEach((effect) => {
        if (effect.kind === 'give_item') fallback.push(`获得 ${effect.target}。`)
        else if (effect.kind === 'open_door') fallback.push(`${effect.target} 打开了。`)
        else if (effect.kind === 'move_player') fallback.push(`你来到了 ${effect.target}。`)
      })
      ;(result.events_fired || []).forEach((execution) => {
        if (execution?.name) fallback.push(`（触发了事件：${execution.name}）`)
      })
    }
    if (text) {
      parts.push(text)
      if (result?.narrative?.model === 'fallback') {
        // Say it out loud: the model was unavailable, this text is the
        // deterministic description of what the engine actually committed.
        parts.push('（叙事模型不可用，以上为规则结果的机器描述，状态已提交。）')
      }
    } else if (result?.status === 'rejected') {
      parts.push(`（${result.message || result.code || '行动被规则拒绝'}）`)
    } else if (result?.status === 'conflict') {
      parts.push(`（${result.message || '状态已变化，请重试'}）`)
    } else if (result?.status === 'duplicate') {
      parts.push('（这条行动之前已经处理过，本次没有产生新的变化。）')
    } else if (narrative?.status === 'failed') {
      if (result?.ok && fallback.length > 0) {
        parts.push(fallback.join(' '))
        parts.push('（叙事模型不可用，以上为规则结果的机器描述，状态已提交。）')
      } else {
        parts.push(`（叙事生成失败：${narrative.error || '未知错误'}。行动结果与状态已经提交，当前状态版本 ${result.state_version}。）`)
      }
    }
    if (parts.length === 0) parts.push('（无叙事输出）')
    appendMessage({ role: 'assistant', content: parts.join('\n\n') })
    if (result?.state_version != null) {
      setEngineLane((prev) => ({ ...prev, checked: true, bound: true, version: result.state_version }))
    }
    if (result?.state) {
      setEngineLane((prev) => ({ ...prev, checked: true, bound: true, state: result.state }))
      setCombatMode(Boolean(result.state.combat?.active))
      fetchInventoryApi(activeCampaignId).then(setInventory).catch((err) => console.warn('Inventory refresh failed:', err))
    }
    if (engineLane.bound || result?.state_version != null) {
      fetchCampaignStory(activeCampaignId).then(setCampaignStory).catch(() => {})
    }
    if (result?.events_fired?.length) {
      pushTrace([{
        label: '规则事件',
        entries: result.events_fired.map((event) => `${event.event_id} ${event.status}${event.reason ? ` (${event.reason})` : ''}`),
      }])
    }
  }

  const handleStructuredAction = async ({ action, targetId, label, params }) => {
    if (isStreaming) return null
    setSuggestions([])
    appendMessage({ role: 'user', content: label || `${action} ${targetId || ''}`.trim() })
    setStreaming(true)
    try {
      const result = await submitAction(activeCampaignId, { action, targetId, params })
      applyEngineResult(result)
      return result
    } catch (err) {
      appendMessage({ role: 'assistant', content: `（行动请求未成功返回：${err.message}。请刷新确认当前状态，避免重复提交。）` })
      return null
    } finally {
      setStreaming(false)
    }
  }

  const handleEngineAction = async (action, target) => {
    setSuggestions([])
    appendMessage({ role: 'user', content: action })
    setStreaming(true)
    try {
      // 规则链路也认 @目标：把「对谁说话」写进行动文本，解析器才能绑定人物。
      const spoken = target?.name && !String(action).includes(`@${target.name}`)
        ? `${action} @${target.name}`
        : action
      const result = await submitTextAction(activeCampaignId, spoken)
      applyEngineResult(result)
      // 规则链路把三选一放在响应体里（SSE 链路走 [SUGGESTIONS] 标签）。
      if (Array.isArray(result?.suggestions) && result.suggestions.length) {
        setSuggestions(result.suggestions)
      }
    } catch (err) {
      appendMessage({
        role: 'assistant',
        content: `（规则引擎请求未成功返回：${err.message || err}。请刷新确认当前状态，避免重复提交。）`,
      })
    } finally {
      setStreaming(false)
    }
  }

  const handleAction = (action, target) => {
    if (engineLane.checked && engineLane.bound) {
      return handleEngineAction(action, target)
    }
    setSuggestions([])
    appendMessage({ role: 'user', content: action })
    setStreaming(true)
    streamAction({
      campaignId: activeCampaignId,
      scenarioTone: resolvedTone,
      language: scenarioView?.language ?? activeScenario?.language ?? 'zh',
      action,
      targetName: target?.name || '',
      targetId: target?.id || '',
      openingNarrative: resolvedOpening,
      maxTokens: maxTokens || 2000,
      provider: llmProvider,
      model: llmModel,
      temperature,
      combatEnabled,
      onChunk: appendToLastMessage,
      onJournal: addJournalEntry,
      onMode: (mode) => {
        const normalized = String(mode || '').trim().split('.').pop()?.trim().toUpperCase()
        setCombatMode(normalized === 'COMBAT')
      },
      onInventory: (item) => {
        if (item.action === 'add') addInventoryItem(item)
        else if (item.action === 'use') updateInventoryStore(item.name, 'used')
        else if (item.action === 'lose') updateInventoryStore(item.name, 'lost')
      },
      onPlotAuto: (plot) => {
        appendMessage({ role: 'assistant', content: formatAutoPlotMessage(plot) })
      },
      onImage: setLastMessageImage,
      onSuggestions: setSuggestions,
      onTruncateClean: (cleanText) => {
        replaceLastAssistantMessage(cleanText)
      },
      onUsage: setLastUsage,
      onTrace: pushTrace,
      onDone: () => {
        setStreaming(false)
        // Clear combat mode after response completes
        const COMBAT_CLEAR_DELAY_MS = 3000
        setTimeout(() => setCombatMode(false), COMBAT_CLEAR_DELAY_MS)
      },
      onError: () => setStreaming(false),
    })
  }

  const handleRewind = async () => {
    if (!activeCampaignId || engineLane.bound || isStreaming || rewinding || messages.length === 0) return
    if (!window.confirm('撤销上一步行动？此操作不可恢复。')) return
    setRewinding(true)
    try {
      await rewindLastAction(activeCampaignId)
      popLastPair()
      setSuggestions([])
      setDraft(null)
    } catch (err) {
      console.error('Rewind failed:', err)
    } finally {
      setRewinding(false)
    }
  }

  const handleTimeskip = (data) => {
    if (data.summary) {
      appendMessage({ role: 'assistant', content: `*时间流逝……*\n\n${data.summary}` })
    }
  }

  return (
    <div className="flex h-screen bg-[#f6f5f1] bg-cover bg-center bg-fixed overflow-hidden">
      <Sidebar variant="play" activeTool={activeTool} onTool={openTool} traces={traces.length} />
      <div className="flex-1 min-w-0 flex flex-col h-full bg-[#fbfaf8]/90 backdrop-blur-md">

        {/* Combat Overlay */}
        <CombatOverlay
          active={engineLane.bound && combatMode}
          campaignId={activeCampaignId}
          onResolved={applyEngineResult}
        />

        {/* 常驻小地图：固定在右上角，点击展开完整地图。 */}
        <MiniMap campaignId={activeCampaignId} onOpen={() => setActiveTool('map')} />

        {/* Header */}
        <header className="flex-none flex items-center justify-between px-3 sm:px-6 py-3 sm:py-4 bg-[#fbfaf8]/90 backdrop-blur-xl border-b border-white/5 z-10 shadow-lg gap-2">
          <div className="flex items-center gap-4">
            <button
              onClick={() => setActiveTool(activeTool === 'character' ? null : 'character')}
              title="角色档案"
              aria-label="打开角色档案"
              className="w-9 h-9 rounded-full overflow-hidden border border-white/15 bg-white/5 hover:border-white/40 transition-all shrink-0"
            >
              {playerCharacter?.currentLook?.image_url ? (
                <img
                  src={playerCharacter.currentLook.image_url}
                  alt={playerCharacter?.name || '角色'}
                  className="w-full h-full object-cover"
                />
              ) : (
                <UserRound size={16} className="text-white/50 m-auto" />
              )}
            </button>
            <div className={`w-2 h-2 rounded-full animate-pulse ${combatMode ? 'bg-rose-400 shadow-[0_0_8px_rgba(244,63,94,0.8)]' : 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]'}`} />
            <div>
              <h1 className="text-white font-bold text-base tracking-wide drop-shadow-[0_0_15px_rgba(255,255,255,0.4)]">
                {activeScenario?.title ?? '未知坐标'}
              </h1>
              <div className="flex items-center gap-2 text-white/40 text-xs font-mono uppercase">
                <span>存档 ID: {activeCampaignId?.slice(0, 8) || '离线'}</span>
                {engineLane.checked && (
                  <span
                    className={`normal-case border-l border-white/10 pl-2 ${engineLane.bound ? 'text-emerald-400/70' : 'text-white/25'}`}
                    title={
                      engineLane.bound
                        ? '规则引擎接管：结果由确定性规则判定，叙事只负责描述'
                        : '自由叙事模式：本战役尚未绑定世界，结果由 LLM 驱动'
                    }
                  >
                    {engineLane.bound
                      ? `规则引擎 v${engineLane.state?.version ?? engineLane.version ?? 0}`
                      : '自由叙事'}
                  </span>
                )}
                {engineLane.bound && campaignStory && campaignStory.mode === 'storyline' && (
                  <button
                    onClick={() => setActiveTool('story')}
                    data-testid="story-badge"
                    className="normal-case border-l border-white/10 pl-2 text-cyan-300/70 hover:text-cyan-200 cursor-pointer"
                    title={storyTitle(campaignStory)}
                  >
                    {`剧情 ${storyNodeTitle(campaignStory, campaignStory.active) || '未进入'}`}
                    {campaignStory.armed_packs && campaignStory.armed_packs.length > 0
                      ? ` · 事件包 ${campaignStory.armed_packs.length}`
                      : ''}
                  </button>
                )}
                {engineLane.bound && (
                  <button
                    onClick={() => setActiveTool('preview')}
                    className="normal-case border-l border-white/10 pl-2 text-white/40 hover:text-cyan-300"
                    title="预演一次行动：规则判定，不提交存档"
                  >
                    预演
                  </button>
                )}
                {lastUsage && (
                  <span
                    className="text-white/30 normal-case cursor-help border-l border-white/10 pl-2"
                    title={usageTitle(lastUsage)}
                  >
                    ⛽ {fmtTok(lastUsage.total_input_tokens)}↓ {fmtTok(lastUsage.total_output_tokens)}↑
                    {lastUsage.total_cache_read_tokens > 0 && (
                      <span className="text-emerald-400/50"> {fmtTok(lastUsage.total_cache_read_tokens)}⚡</span>
                    )}
                    <span className="text-white/20"> · {lastUsage.call_count} 次调用</span>
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 flex-wrap justify-end">
            <button
              type="button"
              onClick={() => setActiveTool(activeTool === 'character' ? null : 'character')}
              title="角色档案"
              aria-label="打开角色档案"
              className="flex items-center gap-2 px-3 py-2 rounded-xl bg-white/5 border border-white/5 text-white/50 hover:text-white hover:border-white/25 transition-all text-[10px] font-bold tracking-[0.15em] uppercase"
            >
              角色档案
            </button>
            <button
              type="button"
              onClick={() => setActiveTool(activeTool ? null : 'settings')}
              title={activeTool ? '收起工具面板' : '展开工具面板'}
              aria-label={activeTool ? '收起工具面板' : '展开工具面板'}
              data-testid="tool-dock-toggle"
              className="p-2 rounded-xl bg-white/5 border border-white/5 text-white/50 hover:text-white hover:border-white/25 transition-all"
            >
              <PanelRightOpen size={14} />
            </button>
          </div>
        </header>

        {/* Main content area */}
        <div className="flex flex-1 overflow-hidden">
          {/* Message feed */}
          <div ref={scrollContainerRef} className="flex-1 overflow-y-auto p-4 md:p-8 custom-scrollbar relative">
            <div className="max-w-4xl mx-auto space-y-8 pb-4">
              {resolvedOpening && (
                <div className="relative bg-white/[0.03] backdrop-blur-xl border border-white/5 rounded-[2rem] p-6 md:p-8 shadow-[0_0_40px_rgba(255,255,255,0.15)]">
                  <div className="prose prose-invert max-w-none prose-p:leading-relaxed prose-p:font-light prose-p:text-white font-serif">
                    <ReactMarkdown components={mentionComponents}>{resolvedOpening}</ReactMarkdown>
                  </div>
                  {canRerollOpening && (
                    <button
                      onClick={handleRerollOpening}
                      title="重新生成 AI 开场"
                      aria-label="重新生成开场"
                      className="absolute top-4 right-4 p-2 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-white/60 hover:text-white transition-colors disabled:opacity-30"
                    >
                      <RefreshCw size={14} className={regenerating ? 'animate-spin' : ''} />
                    </button>
                  )}
                </div>
              )}

              {messages.map((msg, i) => {
                const isUser = msg.role === 'user';
                const displayContent = isUser ? msg.content : msg.content
                  .replace(/\[ITEM_ADD:[^\]]+\]/g, '')
                  .replace(/\[ITEM_USE:[^\]]+\]/g, '')
                  .replace(/\[ITEM_LOSE:[^\]]+\]/g, '');
                return (
                  <div key={i} className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
                    {isUser ? (
                      <div className={`max-w-2xl bg-white/10 backdrop-blur-md border border-white/20 text-white px-5 py-3.5 rounded-2xl rounded-tr-sm shadow-[0_4px_20px_rgba(0,0,0,0.2)] ${/^\[META\]/i.test(msg.content) ? 'font-mono' : ''}`}>
                        <p className="text-sm md:text-base font-light tracking-wide leading-relaxed">
                          {(() => {
                            const typeMatch = msg.content.match(/^\[(DO|SAY|CONTINUE|META)\]\s*/i);
                            const actionType = typeMatch ? typeMatch[1] : null;
                            const body = typeMatch ? msg.content.slice(typeMatch[0].length) : msg.content;
                            return (
                              <>
                                {actionType && (
                                  <span className="text-xs font-bold text-white uppercase mr-2 tracking-widest opacity-80">
                                    {{ DO: '行动', SAY: '对话', CONTINUE: '继续', META: '旁白' }[actionType] || actionType}
                                  </span>
                                )}
                                <MentionText>{body}</MentionText>
                              </>
                            );
                          })()}
                        </p>
                      </div>
                    ) : (
                      <div className="max-w-3xl pr-4">
                        {/* System/Narrator indicator */}
                        <div className="flex items-center gap-2 mb-2 ml-1 opacity-60">
                           <div className={`w-1.5 h-1.5 rounded-full ${combatMode ? 'bg-rose-400' : 'bg-white/[0.03]'}`} />
                           <span className={`text-[10px] font-bold uppercase tracking-widest font-mono ${combatMode ? 'text-rose-300' : 'text-white/60'}`}>
                             {combatMode ? '战斗叙事' : '叙事核心'}
                           </span>
                        </div>
                        <div className="prose prose-invert max-w-none prose-p:leading-relaxed prose-p:font-light prose-p:text-white prose-headings:text-white prose-a:text-white font-serif">
                          {(() => {
                            if (!msg.image?.url) {
                              return <ReactMarkdown components={mentionComponents}>{displayContent}</ReactMarkdown>
                            }
                            const [before, after] = splitAtCharacter(displayContent, msg.image.name)
                            return (
                              <>
                                {before && <ReactMarkdown components={mentionComponents}>{before}</ReactMarkdown>}
                                <CharacterCard image={msg.image} />
                                {after && <ReactMarkdown components={mentionComponents}>{after}</ReactMarkdown>}
                              </>
                            )
                          })()}
                          {isStreaming && i === messages.length - 1 && (
                            <span className={`inline-block w-2 h-4 animate-pulse ml-2 align-middle ${combatMode ? 'bg-rose-400 shadow-[0_0_8px_rgba(244,63,94,0.8)]' : 'bg-white/10 shadow-[0_0_8px_rgba(99,102,241,0.8)]'}`} />
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
              {/* Shimmer skeleton while waiting for first assistant token */}
              {isStreaming && messages.length > 0 && messages[messages.length - 1].role === 'user' && (
                <NarratorSkeleton />
              )}
              <div ref={bottomRef} className="h-4" />
            </div>
          </div>

        </div>

        {/* Input Area */}
        <div className="flex-none bg-[#fbfaf8]/90 backdrop-blur-2xl border-t border-white/5 relative z-20 shadow-[0_-10px_40px_rgba(0,0,0,0.5)]">
          {engineLane.bound && engineLane.state?.pending_choice && (
            <div className="max-w-4xl mx-auto w-full px-4 md:px-6 pt-3">
              <div className="rounded-2xl border border-amber-400/30 bg-amber-400/[0.06] p-4 flex flex-col gap-3">
                <p className="text-sm text-amber-100">
                  {engineLane.state.pending_choice.prompt || '请选择：'}
                </p>
                <div className="flex flex-wrap gap-2">
                  {(engineLane.state.pending_choice.options || []).map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      disabled={isStreaming}
                      onClick={() => handleStructuredAction({
                        action: 'choose',
                        targetId: option.id,
                        label: option.label,
                      })}
                      className="px-3 py-1.5 rounded-xl text-xs text-amber-100 bg-amber-500/15 border border-amber-400/30 hover:bg-amber-500/25 disabled:opacity-40"
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
          <div className="max-w-4xl mx-auto w-full px-4 md:px-6 pt-3">
            <SuggestionList
              suggestions={suggestions}
              disabled={isStreaming}
              onSend={(input, picked) => { setSuggestions([]); handleAction(input, picked) }}
              onEdit={(input, picked) => {
                setSuggestions([])
                setDraft({ text: input, target: picked, at: Date.now() })
              }}
            />
          </div>
          <div className="flex items-end max-w-4xl mx-auto w-full">
            <div className="flex-1">
              <ActionInput onSubmit={handleAction} disabled={isStreaming} draft={draft} />
            </div>
            <div className="pb-4 pr-4 md:pr-6">
              <button
                onClick={handleRewind}
                disabled={engineLane.bound || isStreaming || rewinding || messages.length === 0}
                title={engineLane.bound ? '规则模式请在世界编辑器的调试面板回滚状态版本' : '撤销上一步'}
                aria-label="撤销上一步"
                className="p-3 rounded-xl bg-white/5 border border-white/10 text-white/40 hover:text-rose-400 hover:border-rose-400/30 hover:bg-rose-400/10 transition-all disabled:opacity-20 disabled:cursor-not-allowed disabled:hover:text-white/40 disabled:hover:border-white/10 disabled:hover:bg-white/5"
              >
                <Undo2 size={18} />
              </button>
            </div>
          </div>
        </div>
      </div>

      <ToolDock
        tool={activeTool}
        onClose={() => setActiveTool(null)}
        campaignId={activeCampaignId}
        context={{
          inventory,
          setInventory,
          engineBound: engineLane.bound,
          engineState: engineLane.state,
          onEngineAction: handleStructuredAction,
          busy: isStreaming,
          journal,
          refreshJournal,
          campaignStory,
          refreshStory,
          language: activeScenario?.language ?? 'zh',
          scenarioId: activeScenario?.id,
          onTimeskip: handleTimeskip,
          traces,
          fetchDomainEvents,
          fetchRequests: fetchCampaignRequests,
          fetchRequestDetail: fetchActionRecord,
          fetchAgents: getAgents,
          fetchAgentCalls: getAgentCalls,
          clearTraces: async () => { await deleteTraces(activeCampaignId); clearTraces() },
        }}
      />
    </div>
  )
}
