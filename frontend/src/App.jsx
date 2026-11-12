import { BrowserRouter, Routes, Route, Link, useNavigate } from 'react-router-dom'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { useGameStore } from './store'
import { fetchScenarios, exportScenario, fetchCampaigns, createCampaign, checkNeo4j, deleteCampaign, deleteScenario, fetchSetupState } from './api'
import ErrorBoundary from './components/ErrorBoundary'
import Sidebar from './components/Sidebar'
import SettingsPanel from './components/SettingsPanel'
import { ALL_TYPES, WORLD_TYPE_LABELS } from './lib/scenarioTypes'

// 编辑器与游戏画布占了主包一半以上，首屏只需要首页。
// 这些模块拆成独立 chunk，只有真正进入对应界面时才下载。
const GameCanvas = lazy(() => import('./components/GameCanvas'))
const ScenarioBuilder = lazy(() => import('./components/ScenarioBuilder'))
const SetupWizard = lazy(() => import('./components/SetupWizard'))
const KnowledgeStudio = lazy(() => import('./components/KnowledgeStudio'))
const WorldStudio = lazy(() => import('./components/WorldStudio'))
const StorylineStudio = lazy(() => import('./components/StorylineStudio'))
const CharacterLibrary = lazy(() => import('./components/CharacterLibrary'))

// 懒加载期间的占位：同款全屏底色与文案，避免空白闪一下。
const ChunkFallback = () => (
  <div className="min-h-screen bg-black text-white/40 flex items-center justify-center text-xs uppercase tracking-[0.3em]">
    载入中…
  </div>
)

const ModalFallback = () => (
  <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-md flex items-center justify-center text-white/40 text-xs uppercase tracking-[0.3em]">
    载入中…
  </div>
)

const ArchiveIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="21 8 21 21 3 21 3 8"></polyline>
    <rect x="1" y="3" width="22" height="5"></rect>
    <line x1="10" y1="12" x2="14" y2="12"></line>
  </svg>
)

const PowerIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round">
    <path d="M18.36 6.64a9 9 0 1 1-12.73 0"></path>
    <line x1="12" y1="2" x2="12" y2="12"></line>
  </svg>
)

function Home() {
  const {
    scenarios, setScenarios, setActiveScenario, setActiveCampaignId,
    activeCampaignId, clearMessages,
  } = useGameStore()
  const navigate = useNavigate()
  const [campaignsMap, setCampaignsMap] = useState({})
  const [studioScenario, setStudioScenario] = useState(null)
  const [studioTab, setStudioTab] = useState('knowledge')
  // 世界工坊：打开时带上情景 id（可为空，表示看全部世界）
  const [worldStudio, setWorldStudio] = useState(null)
  const [storylineStudio, setStorylineStudio] = useState(null)

  useEffect(() => {
    fetchScenarios()
      .then((data) => {
        setScenarios(data)
        // Deep link for the studio: /?studio=<scenario id>
        const params = new URLSearchParams(window.location.search)
        const studioId = params.get('studio')
        if (studioId) {
          const target = data.find((s) => s.id === studioId)
          if (target) {
            setStudioScenario(target)
            const tab = params.get('tab')
            if (tab === 'mode' || tab === 'presets' || tab === 'knowledge') setStudioTab(tab)
          }
        }
        data.forEach((s) =>
          fetchCampaigns(s.id)
            .then((camps) => setCampaignsMap((prev) => ({ ...prev, [s.id]: camps })))
            .catch(() => {})
        )
      })
      .catch(() => {})
  }, [])

  // Neo4j 是可选依赖：地图与地点本身存在 SQLite 里，只有图谱增强
  // （实体关系写入、图谱视图）需要它。缺它不影响开局，所以这里只做一次
  // 非阻塞探测，在首页出一条可关闭的提示，不再每次点「开始冒险」都弹 alert。
  const [graphHint, setGraphHint] = useState(false)
  const graphHintChecked = useRef(false)

  useEffect(() => {
    if (graphHintChecked.current) return
    graphHintChecked.current = true
    let dismissed = false
    try {
      dismissed = localStorage.getItem('lunar_graphHintDismissed') === '1'
    } catch {
      // 本地存储不可用时当作没关过
    }
    if (dismissed) return
    checkNeo4j()
      .then((ok) => { if (!ok) setGraphHint(true) })
      .catch(() => setGraphHint(true))
  }, [])

  const dismissGraphHint = () => {
    setGraphHint(false)
    try {
      localStorage.setItem('lunar_graphHintDismissed', '1')
    } catch {
      // 存不进去也照样关掉这一次
    }
  }

  const playCampaign = async (scenario) => {
    try {
      const campaigns = campaignsMap[scenario.id] || []
      if (campaigns.length > 0) {
        // 继续最近一次冒险；切到另一段冒险时必须丢弃上一段的聊天缓存。
        const campaign = campaigns[0]
        setActiveScenario(scenario)
        if (campaign.id !== activeCampaignId) clearMessages()
        setActiveCampaignId(campaign.id)
        navigate('/play')
      } else {
        const campaign = await createCampaign(scenario.id)
        setActiveScenario(scenario)
        setActiveCampaignId(campaign.id)
        clearMessages()
        navigate('/play')
      }
    } catch {
      alert('无法开始冒险，请检查后端服务是否已启动。')
    }
  }

  const startNewCampaign = async (scenario) => {
    if (!confirm('开始一段新的冒险？当前进度会被单独保存，不会被覆盖。')) return
    try {
      const campaign = await createCampaign(scenario.id)
      setActiveScenario(scenario)
      setActiveCampaignId(campaign.id)
      clearMessages()
      navigate('/play')
    } catch {
      alert('创建冒险失败。')
    }
  }

  const handleDeleteAdventures = async (scenarioId) => {
    const campaigns = campaignsMap[scenarioId] || []
    if (campaigns.length === 0) return
    if (!confirm(`删除这个情景下的全部 ${campaigns.length} 段冒险记录？所有进度都会丢失。`)) return
    try {
      for (const c of campaigns) {
        await deleteCampaign(scenarioId, c.id)
      }
      setCampaignsMap((prev) => ({ ...prev, [scenarioId]: [] }))
    } catch {
      alert('删除冒险记录失败。')
    }
  }

  const handleDeleteScenario = async (scenarioId) => {
    if (!confirm('删除这个情景以及它名下的全部冒险？此操作无法撤销。')) return
    try {
      await deleteScenario(scenarioId)
      setScenarios(scenarios.filter((s) => s.id !== scenarioId))
      try {
        const stored = localStorage.getItem('lunar_activeScenario')
        if (stored) {
          const parsed = JSON.parse(stored)
          if (parsed.id === scenarioId) {
            useGameStore.getState().clearSession()
          }
        }
      } catch {
        // 本地存档不可解析时忽略，不影响删除
      }
    } catch {
      alert('删除情景失败。')
    }
  }

  return (
    <div className="min-h-screen bg-black text-[#d1d1d1] font-sans selection:bg-white/20 selection:text-white relative overflow-hidden flex flex-col items-center justify-center py-16">

      {/* 背景层：深色主题铺山峦照片，浅色主题由 .hero-bg 换成纸面暖光 */}
      <div
        className="hero-bg absolute inset-0 bg-cover bg-center bg-no-repeat opacity-60 mix-blend-screen animate-pulse-slow"
        style={{ backgroundImage: "url('/lunar-bg.jpg')" }}
      ></div>

      <div className="hero-mask absolute inset-0 bg-gradient-to-t from-black via-black/60 to-black/90 pointer-events-none"></div>

      <div className="w-full max-w-5xl px-4 sm:px-8 relative z-10 flex flex-col items-center">

        {/* Neo4j 缺失提示：可选依赖，不阻塞任何玩法，可永久关闭 */}
        {graphHint && (
          <div
            role="status"
            className="hero-emblem mb-8 w-full max-w-2xl flex items-start gap-4 rounded-2xl border border-amber-400/20 bg-amber-400/[0.06] px-5 py-4 text-left backdrop-blur-xl"
          >
            <div className="flex-1 min-w-0">
              <p className="text-[11px] font-bold tracking-[0.2em] uppercase text-amber-300/80 mb-2">
                图谱服务未连接
              </p>
              <p className="text-xs leading-relaxed text-white/50">
                没有检测到 Neo4j。地图、地点、物品与任务都照常工作，只有「实体关系图谱」这一层不会写入。
                需要的话可以用 Docker 启动它；不启动也能完整游玩。
              </p>
            </div>
            <button
              onClick={dismissGraphHint}
              className="shrink-0 px-3 py-1.5 rounded-xl border border-white/10 text-white/40 hover:text-white hover:border-white/40 transition-all text-[10px] font-bold tracking-[0.15em] uppercase"
              title="不再提示"
            >
              知道了
            </button>
          </div>
        )}

        <header className="hero-header mb-12 flex flex-col items-center text-center">
          {/* 引擎标识：自指螺旋。深色用白螺旋 / 浅色用黑螺旋，均为透明底 PNG，
              由 CSS 按 data-theme 切换，不依赖 filter 或混合模式。
              实际尺寸由 .hero-emblem .hero-logo-img 的 clamp() 决定（桌面 220px / 窄屏 148px），
              这里的 width/height 属性只用于首帧占位，避免图片加载前的高度跳动。 */}
          <div className="hero-emblem hero-emblem-mark mb-7">
            <img
              src="/self-reference-logo-dark.png"
              alt="自指引擎"
              width="220"
              height="220"
              className="hero-logo-img logo-spiral logo-spiral-dark"
              draggable="false"
            />
            <img
              src="/self-reference-logo.png"
              alt=""
              aria-hidden="true"
              width="220"
              height="220"
              className="hero-logo-img logo-spiral logo-spiral-light"
              draggable="false"
            />
          </div>

          <p className="hero-emblem hero-emblem-kicker text-[10px] font-mono tracking-[0.55em] uppercase text-white/40 mb-4">
            Self-Reference Engine
          </p>
          <h1 className="hero-title text-4xl sm:text-6xl md:text-7xl font-bold text-white mb-5 tracking-tighter drop-shadow-2xl">
            自指引擎
          </h1>

          <p className="text-gray-400 max-w-2xl text-base sm:text-lg font-light leading-relaxed mb-4 drop-shadow-md px-2">
            规则决定事实，事件记录变化，状态保存结果，模型只负责叙事。
          </p>
          <p className="text-white/30 max-w-xl text-xs sm:text-sm font-light leading-relaxed px-2">
            先选择一种世界类型，或从空白开始搭建你自己的文字冒险。
          </p>
          <div className="hero-emblem mt-6 flex items-center gap-3">
            <Link
              to="/characters"
              className="px-6 py-3 rounded-2xl border border-white/10 text-white/60 hover:text-white hover:border-white/40 transition-all text-[10px] font-bold tracking-[0.2em] uppercase"
            >
              角色库
            </Link>
            <span className="text-[10px] text-white/25 font-mono tracking-widest">
              角色独立于世界，开局时任意挑选
            </span>
          </div>
        </header>

        {/* 世界类型选择 */}
        <section className="w-full mb-12">
          <div className="flex items-center gap-4 mb-6 pb-3 border-b border-white/10">
            <h2 className="text-xs font-bold uppercase tracking-[0.3em] text-white/60">选择世界类型</h2>
            <div className="w-1.5 h-1.5 rounded-full bg-white/20"></div>
            <span className="text-[10px] text-white/40 font-mono tracking-widest">
              {ALL_TYPES.length} 种预设
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {ALL_TYPES.map((type) => (
              <button
                key={type.id}
                onClick={() => navigate(`/create?type=${type.id}`)}
                className="group text-left p-5 rounded-3xl bg-white/[0.03] border border-white/5 hover:border-white/25 hover:bg-white/[0.07] transition-all backdrop-blur-xl"
              >
                <div className="flex items-start gap-4">
                  <span className="world-type-icon text-3xl leading-none opacity-80 group-hover:opacity-100 transition-opacity">
                    {type.icon}
                  </span>
                  <div className="min-w-0">
                    <div className="text-white font-bold text-lg mb-1">{type.name}</div>
                    <p className="text-xs text-gray-400 leading-relaxed line-clamp-2">{type.tagline}</p>
                  </div>
                </div>
              </button>
            ))}
          </div>
        </section>

        {/* 情景档案 */}
        <div className="w-full max-w-3xl flex flex-col items-center">
          <div className="flex items-center gap-4 mb-8 pb-4 border-b border-white/10 w-full justify-center">
            <ArchiveIcon />
            <h2 className="text-xs font-bold uppercase tracking-[0.3em] text-white/60">情景档案</h2>
            <div className="w-1.5 h-1.5 rounded-full bg-white/20"></div>
            <span className="text-[10px] text-white/40 font-mono tracking-widest">
              共 {scenarios.length} 个
            </span>
          </div>

          {scenarios.length === 0 ? (
            <div className="py-16 w-full flex flex-col items-center justify-center text-white/30 bg-white/[0.02] backdrop-blur-md rounded-[2rem] border border-white/5">
              <p className="mb-4 tracking-[0.2em] text-xs font-semibold">还没有任何情景</p>
              <Link to="/create" className="text-white hover:underline underline-offset-8 transition-all text-[10px] tracking-widest font-bold">
                从空白开始构建
              </Link>
            </div>
          ) : (
            <div className="flex flex-col gap-6 w-full">
              {scenarios.map((s) => (
                <div key={s.id} className="bg-white/[0.03] backdrop-blur-xl p-5 sm:p-8 rounded-[2rem] border border-white/5 transition-all hover:bg-white/[0.06] hover:border-white/20 group">
                  <div className="flex flex-col md:flex-row items-center gap-8">
                    <div className="flex-1 text-center md:text-left">
                      <div className="flex items-center gap-3 justify-center md:justify-start mb-2">
                        <span className="text-[10px] text-white/20 tracking-widest uppercase font-mono">ID: {s.id.split('-')[0]}</span>
                        <span className="text-[10px] px-2 py-0.5 rounded-full border border-white/10 text-white/40">
                          {WORLD_TYPE_LABELS[s.world_type] || '自定义世界'}
                        </span>
                      </div>
                      <h3 className="text-xl sm:text-3xl font-bold text-white mb-3 group-hover:text-white transition-colors">
                        {s.title}
                      </h3>
                      <p className="text-sm text-gray-500 line-clamp-2 leading-relaxed italic">
                        {s.description || s.tone_instructions || '这个情景还没有填写简介。'}
                      </p>
                    </div>

                    <div className="flex flex-col gap-3 items-end">
                      <div className="flex gap-3 flex-wrap justify-end">
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDeleteScenario(s.id) }}
                          className="p-3 rounded-2xl border border-white/5 hover:border-rose-500/40 text-white/20 hover:text-rose-400 transition-all"
                          title="删除情景"
                        >
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                        </button>

                        {(campaignsMap[s.id] || []).length > 0 && (
                          <button
                            onClick={(e) => { e.stopPropagation(); handleDeleteAdventures(s.id) }}
                            className="p-3 rounded-2xl border border-white/5 hover:border-rose-500/40 text-white/20 hover:text-rose-400 transition-all"
                            title="删除全部冒险记录"
                          >
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="M5 6l1 14h12l1-14"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>
                          </button>
                        )}

                        <button
                          onClick={(e) => { e.stopPropagation(); setWorldStudio({ scenarioId: s.id }) }}
                          className="px-4 py-3 rounded-2xl border border-white/10 hover:border-white/30 text-white/50 hover:text-white transition-all text-[10px] font-bold tracking-[0.15em] uppercase"
                          title="编辑世界地图、地点、物品、门、实体、事件与任务"
                        >
                          世界工坊
                        </button>

                        <button
                          onClick={(e) => { e.stopPropagation(); setStorylineStudio(s) }}
                          className="px-4 py-3 rounded-2xl border border-white/10 hover:border-white/30 text-white/50 hover:text-white transition-all text-[10px] font-bold tracking-[0.15em] uppercase"
                          title="编辑主线、分支、独立剧情与 AI 生成剧情线"
                        >
                          故事线
                        </button>

                        <button
                          onClick={(e) => { e.stopPropagation(); setStudioScenario(s) }}
                          className="px-4 py-3 rounded-2xl border border-white/10 hover:border-white/30 text-white/50 hover:text-white transition-all text-[10px] font-bold tracking-[0.15em] uppercase"
                          title="编辑知识库、运行模式与模型预设"
                        >
                          知识与模式
                        </button>

                        <button
                          onClick={(e) => { e.stopPropagation(); exportScenario(s.id, s.title).catch(() => alert('导出失败。')) }}
                          className="p-3 rounded-2xl border border-white/5 hover:border-white/20 text-white/20 hover:text-white transition-all"
                          title="导出"
                        >
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                        </button>

                        {(campaignsMap[s.id] || []).length > 0 && (
                          <button
                            onClick={() => startNewCampaign(s)}
                            className="px-5 py-3 rounded-2xl border border-white/10 hover:border-white/30 text-white/40 hover:text-white transition-all text-[10px] font-bold tracking-[0.15em] uppercase"
                            title="开始一段全新的冒险"
                          >
                            新冒险
                          </button>
                        )}

                        <button
                          onClick={() => playCampaign(s)}
                          className="px-8 py-3 rounded-2xl bg-white/5 hover:bg-white text-white/80 hover:text-black transition-all text-xs font-bold tracking-[0.2em] uppercase flex items-center gap-3"
                        >
                          继续冒险
                          <PowerIcon />
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {worldStudio && (
        <Suspense fallback={<ModalFallback />}>
          <WorldStudio
            open
            scenarioId={worldStudio?.scenarioId}
            campaignId={undefined}
            onClose={() => setWorldStudio(null)}
          />
        </Suspense>
      )}

      {storylineStudio && (
        <Suspense fallback={<ModalFallback />}>
          <StorylineStudio
            open
            scenarioId={storylineStudio?.id}
            scenarioTitle={storylineStudio?.title}
            onClose={() => setStorylineStudio(null)}
          />
        </Suspense>
      )}

      {studioScenario && (
        <Suspense fallback={<ModalFallback />}>
          <KnowledgeStudio
            open
            scenarioId={studioScenario?.id}
            scenarioTitle={studioScenario?.title}
            initialTab={studioTab}
            onClose={() => setStudioScenario(null)}
          />
        </Suspense>
      )}
    </div>
  )
}

function PlayGate() {
  const { activeScenario, activeCampaignId } = useGameStore()
  const [state, setState] = useState({ status: 'loading', questions: [] })
  // 没有进行中的冒险时直接可用，避免在 effect 中同步 setState。
  const effectiveState = activeCampaignId ? state : { status: 'ready', questions: [] }

  useEffect(() => {
    if (!activeCampaignId) return
    let cancelled = false
    fetchSetupState(activeCampaignId)
      .then((data) => {
        if (cancelled) return
        if (data.needs_setup) {
          setState({ status: 'wizard', questions: data.questions || [] })
        } else {
          setState({ status: 'ready', questions: [] })
        }
      })
      .catch(() => {
        // 后端不可用或存档缺失时交给 GameCanvas 显示错误
        if (!cancelled) setState({ status: 'ready', questions: [] })
      })
    return () => { cancelled = true }
  }, [activeCampaignId])

  if (effectiveState.status === 'loading') {
    return (
      <div className="min-h-screen bg-black text-white/40 flex items-center justify-center text-xs uppercase tracking-[0.3em]">
        载入中…
      </div>
    )
  }
  if (effectiveState.status === 'wizard') {
    return (
      <Suspense fallback={<ChunkFallback />}>
        <SetupWizard
          scenario={activeScenario}
          campaignId={activeCampaignId}
          questions={effectiveState.questions}
          onComplete={() => setState({ status: 'ready', questions: [] })}
        />
      </Suspense>
    )
  }
  return (
    <Suspense fallback={<ChunkFallback />}>
      <GameCanvas key={activeCampaignId || 'unbound'} />
    </Suspense>
  )
}

// 首页 / 编辑器 / 角色库共用「左侧边栏 + 右侧内容」的外壳。
// 边栏自身滚动，内容区独立滚动，两者互不影响。
const AppShell = ({ children }) => {
  // 首页 / 编辑器 / 角色库也要能随时打开设置，所以入口和弹层放在外壳里，
  // 而不是只挂在游戏内的工具面板上。
  const [settingsOpen, setSettingsOpen] = useState(false)
  const activeScenarioId = useGameStore((s) => s.activeScenario?.id)
  return (
    <div className="flex h-screen bg-black overflow-hidden">
      <Sidebar
        variant="app"
        onSettings={setSettingsOpen}
        settingsOpen={settingsOpen}
      />
      <div className="flex-1 min-w-0 h-full overflow-y-auto custom-scrollbar">{children}</div>
      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          activeScenarioId={activeScenarioId}
        />
      )}
    </div>
  )
}

export default function App() {
  useEffect(() => {
    const store = useGameStore.getState()
    store.restoreSettings()
    // 后端保存的设置优先：localStorage 只在后端不可达时兜底。
    store.restoreServerSettings?.()
  }, [])

  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<AppShell><Home /></AppShell>} />
          <Route
            path="/create"
            element={
              <AppShell>
                <Suspense fallback={<ChunkFallback />}>
                  <ScenarioBuilder onCreated={() => window.location.href = '/'} />
                </Suspense>
              </AppShell>
            }
          />
          <Route path="/play" element={<PlayGate />} />
          <Route
            path="/characters"
            element={
              <AppShell>
                <Suspense fallback={<ChunkFallback />}>
                  <CharacterLibrary open onClose={() => window.location.href = '/'} />
                </Suspense>
              </AppShell>
            }
          />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  )
}