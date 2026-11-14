import { useCallback, useEffect, useRef, useState } from 'react'
import { Map as MapIcon, Minus, Plus, RefreshCw, X } from 'lucide-react'
import { fetchCampaignWorldState, fetchWorld, fetchWorldGraph } from '../api'

// 右上角常驻的 2D 地图：只画 LOCATION 节点。数据来自
// GET /api/game/{id}/world-graph（规则世界会把地点写进图）。
// 完整可交互的地图留给「展开」；常驻小窗要的是「我在哪、四周有什么」，
// 圆盘手绘足够，也省掉 force-graph + d3 那 300KB 的依赖。
//
// 约束：这个组件必须在任何返回结构下都不崩。后端/图谱/剧情三层都可能是
// null 或形态不符，一律在这里归一化成数组，绝不把 null 交给 .map。

const SIZE = 208
const RING = 62
const SIDES = 10

const PLACE_TYPES = new Set(['LOCATION', 'PLACE', 'AREA', 'ROOM', 'REGION', 'CITY'])

const asArray = (value) => (Array.isArray(value) ? value : [])

function isPlace(node) {
  const type = String(node?.node_type || node?.type || '').toUpperCase()
  return PLACE_TYPES.has(type)
}

function hashAngle(id) {
  // 同一个地点每次渲染都落在同一角度：位置稳定，玩家才能形成空间记忆。
  const text = String(id ?? '')
  let h = 0
  for (let i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) % 360
  return (h * Math.PI) / 180
}

function layout(places) {
  const cx = SIZE / 2
  const cy = SIZE / 2
  return places.map((place, i) => {
    const angle = i < 8 ? (i / 8) * Math.PI * 2 - Math.PI / 2 : hashAngle(place.id)
    const radius = i < 8 ? RING : RING * 0.46
    return { ...place, x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius }
  })
}

/**
 * 规则世界的可用地图：从权威状态拿到 world_id，再把世界定义里的地点、出口翻成图谱形状。
 * 没绑定世界的战役返回空，调用方据此显示空态。
 */
async function loadWorldPlaces(campaignId) {
  const state = await fetchCampaignWorldState(campaignId)
  if (!state?.world_id) return { nodes: [], links: [] }
  const world = await fetchWorld(state.world_id)
  const locations = Array.isArray(world?.locations) ? world.locations : []
  const nodes = locations.map((loc) => ({
    id: loc.id,
    name: loc.name || loc.id,
    node_type: 'LOCATION',
    attributes: { current: loc.id === state.location_id },
  }))
  const links = []
  for (const loc of locations) {
    for (const exitDef of Array.isArray(loc.exits) ? loc.exits : []) {
      if (!exitDef?.target_id) continue
      links.push({ source: loc.id, target: exitDef.target_id, rel_type: 'EXIT', strength: 1 })
    }
  }
  return { nodes, links }
}

export default function MiniMap({ campaignId, onOpen }) {
  const [graph, setGraph] = useState({ nodes: [], links: [] })
  const [status, setStatus] = useState('idle')
  const [collapsed, setCollapsed] = useState(false)
  const [zoom, setZoom] = useState(1)
  const alive = useRef(true)

  // Kept out of an inline setState: the effect below only kicks off the
  // request, and every state write happens after the await.
  const load = useCallback(async () => {
    if (!campaignId) return
    setStatus('loading')
    try {
      const data = await fetchWorldGraph(campaignId)
      const nodes = asArray(data?.nodes).filter((n) => n && (n.id || n.name))
      if (!alive.current) return
      if (nodes.some(isPlace)) {
        setGraph({ nodes, links: asArray(data?.links) })
        setStatus('ready')
        return
      }
      // 时序图谱只在配了 Neo4j 时才有内容；规则世界的真实地点在 world 定义里。
      const places = await loadWorldPlaces(campaignId)
      if (!alive.current) return
      setGraph({
        nodes: nodes.length ? nodes : places.nodes,
        links: nodes.length ? asArray(data?.links) : places.links,
      })
      setStatus(places.nodes.length ? 'ready' : 'empty')
    } catch {
      if (alive.current) {
        setGraph({ nodes: [], links: [] })
        setStatus('error')
      }
    }
  }, [campaignId])

  useEffect(() => {
    alive.current = true
    const tick = async () => { await load() }
    tick()
    return () => { alive.current = false }
  }, [load])

  const places = graph.nodes.filter(isPlace).slice(0, SIDES)
  const nodes = layout(places)
  const byId = new Map(nodes.map((n) => [String(n.id ?? n.name), n]))
  // 当前位置：图谱里 PLAYER 节点连到的第一个地点；都取不到就取第一个地点。
  const player = graph.nodes.find((n) => /player|pc|主角|玩家/i.test(String(n.node_type || n.name || '')))
  const hereLink = player
    ? graph.links.find((l) => String(l.source) === String(player.id) || String(l.target) === String(player.id))
    : null
  const hereId = hereLink
    ? (String(hereLink.source) === String(player.id) ? String(hereLink.target) : String(hereLink.source))
    : ''
  // 规则世界会在地点节点上标 current；时序图谱则靠玩家节点连线推断。
  const currentPlace = nodes.find((n) => n.attributes?.current) || null
  const here = currentPlace || byId.get(hereId) || nodes[0] || null

  return (
    <div
      data-testid="mini-map"
      className="absolute z-20 hidden lg:block select-none"
      style={{ top: 68, right: 12 }}
    >
      <div className="rounded-2xl border border-[#e0dcd4] bg-[#fffdf9]/95 backdrop-blur-xl shadow-[0_10px_30px_rgba(40,34,26,0.14)] overflow-hidden">
        <div className="flex items-center gap-2 px-2.5 py-1.5 border-b border-[#eae6de]">
          <MapIcon size={12} className="text-[#0e7c8c] shrink-0" />
          <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-[#5c574f]">地图</span>
          <span
            data-testid="mini-map-here"
            className="ml-auto max-w-[96px] truncate text-[10px] text-[#0e5a66]"
            title={here?.name || '位置未知'}
          >
            {here?.name || '位置未知'}
          </span>
          <button
            type="button" onClick={() => load()} title="刷新地图" aria-label="刷新地图"
            className="p-1 rounded-md text-[#8a857c] hover:text-[#1a1a1a] hover:bg-[#efede8]"
          >
            <RefreshCw size={11} className={status === 'loading' ? 'animate-spin' : ''} />
          </button>
          <button
            type="button" onClick={() => setCollapsed((value) => !value)}
            title={collapsed ? '展开地图' : '收起地图'}
            aria-label={collapsed ? '展开地图' : '收起地图'}
            className="p-1 rounded-md text-[#8a857c] hover:text-[#1a1a1a] hover:bg-[#efede8]"
          >
            <X size={11} />
          </button>
        </div>

        {!collapsed && (
          <>
            <div className="relative" style={{ width: SIZE, height: SIZE }}>
              <svg
                width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`}
                style={{ transform: `scale(${zoom})`, transformOrigin: 'center' }}
              >
                <circle
                  cx={SIZE / 2} cy={SIZE / 2} r={RING}
                  fill="none" stroke="rgba(58,54,48,0.20)" strokeDasharray="2 4"
                />
                {nodes.map((node) => (
                  <line
                    key={`spoke-${node.id}`}
                    x1={SIZE / 2} y1={SIZE / 2} x2={node.x} y2={node.y}
                    stroke="rgba(58,54,48,0.14)" strokeWidth={1}
                  />
                ))}
                {nodes.map((node) => {
                  const isHere = here && String(node.id) === String(here.id)
                  return (
                    <circle
                      key={String(node.id)}
                      cx={node.x} cy={node.y} r={isHere ? 5.5 : 3.5}
                      fill={isHere ? '#0e7c8c' : '#b3ada3'}
                      stroke={isHere ? 'rgba(14,124,140,0.28)' : 'none'} strokeWidth={isHere ? 5 : 0}
                    >
                      <title>{node.name || '地点'}</title>
                    </circle>
                  )
                })}
                {here && (
                  <text x={here.x} y={here.y - 9} textAnchor="middle" fontSize={8} fill="#0e7c8c">
                    你在这里
                  </text>
                )}
                <circle cx={SIZE / 2} cy={SIZE / 2} r={2} fill="rgba(58,54,48,0.35)" />
              </svg>
              {status === 'ready' && nodes.length === 0 && (
                <div className="absolute inset-0 flex items-center justify-center px-4 text-center text-[10px] leading-relaxed text-[#a8a29a]">
                  还没有登记地点
                </div>
              )}
              {status === 'error' && (
                <div className="absolute inset-0 flex items-center justify-center text-[10px] text-[#b4553f]">
                  地图读取失败
                </div>
              )}
            </div>

            <div className="flex items-center gap-1 px-2 py-1.5 border-t border-[#eae6de]">
              <button
                type="button" onClick={() => setZoom((z) => Math.max(0.7, +(z - 0.15).toFixed(2)))}
                aria-label="缩小" title="缩小"
                className="p-1 rounded-md text-[#8a857c] hover:text-[#1a1a1a] hover:bg-[#efede8]"
              >
                <Minus size={11} />
              </button>
              <button
                type="button" onClick={() => setZoom(1)}
                className="px-1.5 text-[10px] font-mono text-[#8a857c] hover:text-[#1a1a1a]" title="复位"
              >
                {Math.round(zoom * 100)}%
              </button>
              <button
                type="button" onClick={() => setZoom((z) => Math.min(1.6, +(z + 0.15).toFixed(2)))}
                aria-label="放大" title="放大"
                className="p-1 rounded-md text-[#8a857c] hover:text-[#1a1a1a] hover:bg-[#efede8]"
              >
                <Plus size={11} />
              </button>
              <button
                type="button" onClick={() => onOpen?.()}
                className="ml-auto px-1.5 py-1 rounded-md text-[10px] text-[#0e5a66] hover:text-[#0b4d57] hover:bg-[#efede8]"
                title="打开完整世界地图"
              >
                展开
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
