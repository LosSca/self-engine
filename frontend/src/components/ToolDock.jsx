import { useState, lazy, Suspense } from 'react'
import { X, ArrowRight } from 'lucide-react'
import CharacterProfilePanel from './CharacterProfilePanel'
import PlotGeneratorPanel from './PlotGeneratorPanel'
import TimeskipModal from './TimeskipModal'
import NpcInspector from './NpcInspector'
import MemoryInspector from './MemoryInspector'
import InventoryPanel from './InventoryPanel'
import JournalPanel from './JournalPanel'
import StoryProgressPanel from './StoryProgressPanel'
import DevtoolsPanel from './DevtoolsPanel'
import WorldStudio from './WorldStudio'
import SettingsPanel from './SettingsPanel'
import ActionPreviewPanel from './ActionPreviewPanel'

// 地图看一眼才用到，ForceGraph2D 连同 d3 有 300KB+，不能压在游玩首屏上。
const WorldMapModal = lazy(() => import('./WorldMapModal'))

const TITLES = {
  character: '角色档案',
  inventory: '背包',
  map: '世界地图',
  journal: '任务日志',
  story: '剧情进度',
  plotgen: '剧情生成',
  timeskip: '时间流逝',
  npc: 'NPC 心智',
  memory: '记忆结晶',
  worldstudio: '世界工坊',
  devtools: 'LLM 调试台',
  settings: '设置',
  preview: '行动预演',
}

/**
 * 右侧工具面板区。所有游戏内工具都在这里就地展开，不再弹全屏模态，
 * 因此边栏点一下就换一个面板，不打断正在读的叙事。
 * 各工具组件原本就是「open 为 false 时 return null」的写法，这里给它们套一层
 * 停靠外壳：面板本身照常按模态样式渲染，只是外层容器改成侧边抽屉。
 */
export default function ToolDock({ tool, onClose, campaignId, context }) {
  const open = !!tool
  // 关闭动画期间保留最后一个工具，避免内容在收起过程中闪一下空白
  const [lastTool, setLastTool] = useState(tool)
  if (tool && tool !== lastTool) setLastTool(tool)
  const active = tool || lastTool

  const closeAll = () => onClose?.()
  const panelProps = { open: false }

  return (
    <aside
      data-testid="tool-dock"
      className={`flex-none flex flex-col h-full border-l border-white/10 bg-[#0b0d10] overflow-hidden transition-[width] duration-200 ${
        open ? 'w-[380px] xl:w-[420px]' : 'w-0'
      }`}
    >
      {open && (
        <>
          <div className="flex-none flex items-center justify-between px-4 py-3 border-b border-white/10">
            <div className="text-[11px] font-bold uppercase tracking-[0.2em] text-white/85">
              {TITLES[active] || '面板'}
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={closeAll}
                title="关闭面板"
                aria-label="关闭面板"
                className="p-1.5 rounded-lg text-white/55 hover:text-white hover:bg-white/15 transition-colors"
              >
                <X size={14} />
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar">
            {/* 组件自身仍是模态形态：这里把它压缩成停靠内容 */}
            <div className="tool-dock-host">
              {active === 'character' && <CharacterProfilePanel {...panelProps} campaignId={campaignId} onClose={closeAll} />}
              {active === 'inventory' && (
                <InventoryPanel
                  {...panelProps}
                  campaignId={campaignId}
                  inventory={context.inventory}
                  setInventory={context.setInventory}
                  engineBound={context.engineBound}
                  onEngineAction={context.onEngineAction}
                  busy={context.busy}
                  onClose={closeAll}
                />
              )}
              {active === 'map' && (
                <Suspense fallback={null}>
                  <WorldMapModal
                    {...panelProps}
                    campaignId={campaignId}
                    engineState={context.engineState}
                    onEngineAction={context.onEngineAction}
                    busy={context.busy}
                    onClose={closeAll}
                  />
                </Suspense>
              )}
              {active === 'journal' && (
                <JournalPanel {...panelProps} entries={context.journal} onRefresh={context.refreshJournal} onClose={closeAll} />
              )}
              {active === 'story' && (
                <StoryProgressPanel
                  {...panelProps}
                  story={context.campaignStory}
                  engineBound={context.engineBound}
                  onRefresh={context.refreshStory}
                  onClose={closeAll}
                />
              )}
              {active === 'plotgen' && (
                <PlotGeneratorPanel {...panelProps} campaignId={campaignId} language={context.language} onClose={closeAll} />
              )}
              {active === 'timeskip' && (
                <TimeskipModal {...panelProps} campaignId={campaignId} onTimeskip={context.onTimeskip} onClose={closeAll} />
              )}
              {active === 'npc' && <NpcInspector {...panelProps} campaignId={campaignId} onClose={closeAll} />}
              {active === 'memory' && <MemoryInspector {...panelProps} campaignId={campaignId} onClose={closeAll} />}
              {active === 'worldstudio' && (
                <WorldStudio {...panelProps} scenarioId={context.scenarioId} campaignId={campaignId} onClose={closeAll} />
              )}
              {active === 'devtools' && (
                <DevtoolsPanel
                  {...panelProps}
                  traces={context.traces}
                  campaignId={campaignId}
                  fetchDomainEvents={context.fetchDomainEvents}
                  fetchRequests={context.fetchRequests}
                  fetchRequestDetail={context.fetchRequestDetail}
                  fetchAgents={context.fetchAgents}
                  fetchAgentCalls={context.fetchAgentCalls}
                  onClear={context.clearTraces}
                  onClose={closeAll}
                />
              )}
              {active === 'settings' && (
        <div className="p-3">
          <SettingsPanel
            onClose={closeAll}
            activeScenarioId={context?.activeScenarioId || context?.scenarioId}
          />
        </div>
      )}
              {active === 'preview' && <ActionPreviewPanel {...panelProps} campaignId={campaignId} onClose={closeAll} />}
            </div>
          </div>
        </>
      )}
    </aside>
  )
}

export { TITLES }
