import { useState, useEffect } from 'react'
import { NavLink } from 'react-router-dom'
import {
  Backpack, Map, Sparkles, Clock, Brain, Gem, BookOpen, Terminal,
  Globe, Settings, UserRound, PanelLeftClose, PanelLeftOpen, Home, Plus, Power,
} from 'lucide-react'
import { useGameStore } from '../store'
import OnboardingGuide from './OnboardingGuide'

const TOOL_GROUPS = [
  {
    label: '游玩',
    items: [
      { id: 'character', icon: UserRound, label: '角色档案' },
      { id: 'inventory', icon: Backpack, label: '背包' },
      { id: 'map', icon: Map, label: '世界地图' },
      { id: 'journal', icon: BookOpen, label: '任务日志' },
    ],
  },
  {
    label: '叙事',
    items: [
      { id: 'story', icon: Sparkles, label: '剧情进度' },
      { id: 'plotgen', icon: Sparkles, label: '剧情生成' },
      { id: 'timeskip', icon: Clock, label: '时间流逝' },
      { id: 'npc', icon: Brain, label: 'NPC 心智' },
      { id: 'memory', icon: Gem, label: '记忆结晶' },
    ],
  },
  {
    label: '系统',
    items: [
      { id: 'worldstudio', icon: Globe, label: '世界工坊' },
      { id: 'devtools', icon: Terminal, label: 'LLM 调试台' },
      { id: 'settings', icon: Settings, label: '设置' },
    ],
  },
]

const STORAGE_KEY = 'lunar_sidebarCollapsed'

function useCollapsed() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === '1'
    } catch {
      return false
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0')
    } catch {
      // 本地存储不可用时只在本次会话里折叠
    }
  }, [collapsed])
  return [collapsed, setCollapsed]
}

/**
 * 全局左侧边栏。两种形态共用一个组件：
 * - 游玩界面（variant="play"）：除了导航，还承载游戏内的所有工具面板入口，
 *   点条目直接在右侧面板区展开，而不是弹全屏模态。
 * - 首页 / 编辑器 / 角色库（variant="app"）：只做导航。
 */
export default function Sidebar({ variant = 'app', activeTool, onTool, traces = 0 }) {
  const [collapsed, setCollapsed] = useCollapsed()
  // 新手引导会点亮对应的边栏条目，提示「下一步点这里」
  const [highlight, setHighlight] = useState(null)
  useEffect(() => {
    const expand = () => setCollapsed(false)
    window.addEventListener('lunar:expand-sidebar', expand)
    return () => window.removeEventListener('lunar:expand-sidebar', expand)
  }, [setCollapsed])
  const activeCampaignId = useGameStore((s) => s.activeCampaignId)
  const clearSession = useGameStore((s) => s.clearSession)

  const itemClass = (on, id) => [
    'group relative flex items-center gap-3 rounded-xl transition-all duration-150 select-none',
    collapsed ? 'justify-center px-0 py-2.5' : 'px-3 py-2.5 w-full',
    on
      ? 'bg-white/[0.16] text-white border border-white/25'
      : highlight && highlight === id
        ? 'text-white bg-cyan-400/[0.12] border border-cyan-400/40 animate-pulse-slow'
        : 'text-white/75 hover:text-white hover:bg-white/[0.10] border border-transparent',
  ].join(' ')

  const navItem = (to, Icon, label) => (
    <NavLink
      key={to}
      to={to}
      title={collapsed ? label : undefined}
      aria-label={label}
      className={({ isActive }) => {
        return [
          'group relative flex items-center gap-3 rounded-xl transition-all duration-150',
          collapsed ? 'justify-center px-0 py-2.5' : 'px-3 py-2.5',
          isActive
            ? 'bg-white/[0.16] text-white border border-white/25'
            : 'text-white/75 hover:text-white hover:bg-white/[0.10] border border-transparent',
        ].join(' ')
      }}
    >
      <Icon size={16} className="shrink-0" />
      {!collapsed && <span className="text-xs font-medium tracking-wide truncate">{label}</span>}
    </NavLink>
  )

  const toolButton = ({ id, icon: Icon, label }) => {
    const on = activeTool === id
    return (
      <button
        key={id}
        type="button"
        onClick={() => onTool?.(on ? null : id)}
        title={collapsed ? label : undefined}
        aria-label={label}
        aria-pressed={on}
        data-testid={`sidebar-tool-${id}`}
        className={itemClass(on, id)}
      >
        <Icon size={16} className="shrink-0" />
        {!collapsed && <span className="text-xs font-medium tracking-wide truncate flex-1 text-left">{label}</span>}
        {!collapsed && id === 'devtools' && traces > 0 && (
          <span className="min-w-[16px] h-[16px] px-1 rounded-full bg-cyan-500 text-black text-[9px] font-bold flex items-center justify-center">
            {traces}
          </span>
        )}
        {collapsed && on && (
          <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[2px] h-5 rounded-full bg-white" />
        )}
      </button>
    )
  }

  return (
    <aside
      data-testid="app-sidebar"
      className={`flex-none flex flex-col h-full bg-[#0b0d10] border-r border-white/10 transition-[width] duration-200 ${
        collapsed ? 'w-[60px]' : 'w-[196px]'
      }`}
    >
      {/* 品牌区 */}
      <div className={`flex items-center gap-2 py-4 border-b border-white/10 ${collapsed ? 'justify-center px-0' : 'px-4'}`}>
        <div className="w-7 h-7 rounded-lg overflow-hidden shrink-0 border border-white/10 bg-white/5">
          <img src="/favicon.png" alt="" className="w-full h-full object-cover" draggable="false" />
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <div className="text-[11px] font-bold text-white tracking-[0.18em] uppercase truncate">自指引擎</div>
            <div className="text-[9px] font-mono text-white/55 tracking-[0.12em] truncate">Self-Reference</div>
          </div>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto custom-scrollbar px-2 py-3 space-y-1">
        {navItem('/', Home, '首页')}
        {navItem('/create', Plus, '新建世界')}
        {navItem('/characters', UserRound, '角色库')}
        {activeCampaignId && navItem('/play', Power, '继续冒险')}

        {variant === 'play' && (
          <>
            {TOOL_GROUPS.map((group) => (
              <div key={group.label} className="pt-3">
                {!collapsed && (
                  <div className="px-3 pb-1.5 text-[9px] font-bold tracking-[0.22em] uppercase text-white/50">
                    {group.label}
                  </div>
                )}
                {collapsed && <div className="mx-2 mb-1.5 h-px bg-white/15" />}
                <div className="space-y-1">{group.items.map(toolButton)}</div>
              </div>
            ))}
          </>
        )}
      </nav>

      <div className="flex-none p-2 space-y-1">
        <OnboardingGuide collapsed={collapsed} activeTool={activeTool} onHighlight={setHighlight} variant={variant} />
        {variant === 'play' && (
          <button
            type="button"
            onClick={() => { clearSession(); window.location.href = '/' }}
            title={collapsed ? '断开连接' : undefined}
            aria-label="断开连接"
            className={`flex items-center gap-3 rounded-xl text-white/40 hover:text-rose-400 hover:bg-rose-500/10 transition-all ${
              collapsed ? 'justify-center px-0 py-2.5 w-full' : 'px-3 py-2.5 w-full'
            }`}
          >
            <Power size={16} className="shrink-0" />
            {!collapsed && <span className="text-xs font-medium">断开连接</span>}
          </button>
        )}
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          title={collapsed ? '展开边栏' : '收起边栏'}
          aria-label={collapsed ? '展开边栏' : '收起边栏'}
          data-testid="sidebar-toggle"
          className={`flex items-center gap-3 rounded-xl text-white/30 hover:text-white hover:bg-white/[0.05] transition-all ${
            collapsed ? 'justify-center px-0 py-2.5 w-full' : 'px-3 py-2.5 w-full'
          }`}
        >
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
          {!collapsed && <span className="text-xs font-medium">收起边栏</span>}
        </button>
      </div>
    </aside>
  )
}
