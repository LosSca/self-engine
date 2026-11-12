import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchCampaignEngineState } from '../api'

const NODE_W = 176
const NODE_H = 74
const GAP_X = 96
const GAP_Y = 74
const PAD = 56
const CELL_X = 220
const CELL_Y = 150

const REGION_THEMES = [
  { fill: 'rgba(34,197,94,0.13)', stroke: 'rgba(74,222,128,0.38)', text: '#bbf7d0' },
  { fill: 'rgba(14,165,233,0.13)', stroke: 'rgba(56,189,248,0.38)', text: '#bae6fd' },
  { fill: 'rgba(245,158,11,0.12)', stroke: 'rgba(251,191,36,0.38)', text: '#fde68a' },
  { fill: 'rgba(168,85,247,0.12)', stroke: 'rgba(192,132,252,0.38)', text: '#e9d5ff' },
  { fill: 'rgba(244,63,94,0.10)', stroke: 'rgba(251,113,133,0.36)', text: '#fecdd3' },
]

function regionTheme(region) {
  let hash = 0
  for (const char of String(region || '')) hash = (hash * 31 + char.charCodeAt(0)) % 997
  return REGION_THEMES[hash % REGION_THEMES.length]
}

function positionsKey(worldId) {
  return 'lunar.map.positions.' + (worldId || '')
}

function blobPath(cx, cy, rx, ry, seed) {
  const count = 10
  const points = []
  for (let index = 0; index < count; index += 1) {
    const angle = (Math.PI * 2 * index) / count
    const wobble = 0.84 + (((seed + index * 7) % 19) / 55)
    points.push({
      x: cx + Math.cos(angle) * rx * wobble,
      y: cy + Math.sin(angle) * ry * wobble,
    })
  }
  const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 })
  const first = mid(points[0], points[1])
  let path = `M ${first.x} ${first.y}`
  for (let index = 1; index <= count; index += 1) {
    const current = points[index % count]
    const next = points[(index + 1) % count]
    const end = mid(current, next)
    path += ` Q ${current.x} ${current.y}, ${end.x} ${end.y}`
  }
  return path + ' Z'
}

function layoutNodes(locations, startId) {
  const byId = new Map(locations.map((location) => [location.id, location]))
  const depth = new Map()
  const queue = []
  if (startId && byId.has(startId)) {
    depth.set(startId, 0)
    queue.push(startId)
  }
  while (queue.length > 0) {
    const current = queue.shift()
    const node = byId.get(current)
    if (!node) continue
    for (const exit of node.exits || []) {
      if (!byId.has(exit.target_id) || depth.has(exit.target_id)) continue
      depth.set(exit.target_id, (depth.get(current) || 0) + 1)
      queue.push(exit.target_id)
    }
  }
  const orphans = locations.filter((location) => !depth.has(location.id))
  const maxDepth = depth.size > 0 ? Math.max(...depth.values()) : -1
  orphans.forEach((location) => depth.set(location.id, maxDepth + 1))

  const columns = new Map()
  for (const location of locations) {
    const level = depth.get(location.id) || 0
    if (!columns.has(level)) columns.set(level, [])
    columns.get(level).push(location)
  }
  const nodes = new Map()
  for (const [level, items] of columns) {
    items.forEach((location, index) => {
      nodes.set(location.id, {
        id: location.id,
        x: PAD + level * (NODE_W + GAP_X),
        y: PAD + index * (NODE_H + GAP_Y),
      })
    })
  }
  for (const location of locations) {
    if (typeof location.x === 'number' && typeof location.y === 'number') {
      nodes.set(location.id, {
        id: location.id,
        x: PAD + location.x * CELL_X,
        y: PAD + location.y * CELL_Y,
      })
    }
  }
  return nodes
}

function readOverrides(worldId) {
  try {
    const raw = window.localStorage.getItem(positionsKey(worldId))
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function writeOverrides(worldId, value) {
  try {
    window.localStorage.setItem(positionsKey(worldId), JSON.stringify(value))
  } catch {
    /* storage is optional */
  }
}

export default function WorldMapGraph({
  worldId,
  mapData,
  onEditLocation,
  onDeleteExit,
  onSavePosition,
  campaignId = '',
}) {
  const locations = useMemo(() => (mapData && mapData.locations) || [], [mapData])
  const startId = mapData && mapData.start_location_id
  const [overrides, setOverrides] = useState(() => readOverrides(worldId))
  // Where the selected playthrough actually stands. Read-only: the map shows
  // the committed state and never writes it.
  const [cursor, setCursor] = useState(null)
  const [selectedEdge, setSelectedEdge] = useState(null)
  const [selectedNode, setSelectedNode] = useState(null)
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 })
  const dragRef = useRef(null)
  const panRef = useRef(null)
  const [dragging, setDragging] = useState(null)
  const [panning, setPanning] = useState(false)

  useEffect(() => {
    if (!campaignId) {
      setCursor(null)
      return undefined
    }
    let cancelled = false
    fetchCampaignEngineState(campaignId)
      .then((state) => {
        if (cancelled) return
        setCursor(
          state && state.location_id
            ? { locationId: state.location_id, version: state.version ?? 0 }
            : null,
        )
      })
      .catch(() => {
        if (!cancelled) setCursor(null)
      })
    return () => {
      cancelled = true
    }
  }, [campaignId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset local layout when the world changes
    setOverrides(readOverrides(worldId))
    setSelectedEdge(null)
    setSelectedNode(null)
    setView({ x: 0, y: 0, scale: 1 })
  }, [worldId])

  const baseNodes = useMemo(() => layoutNodes(locations, startId), [locations, startId])
  const nodes = useMemo(() => {
    const merged = new Map()
    for (const [id, node] of baseNodes) {
      const override = overrides[id]
      merged.set(id, override ? { ...node, ...override } : node)
    }
    return merged
  }, [baseNodes, overrides])

  const edges = useMemo(() => {
    const list = []
    for (const location of locations) {
      const from = nodes.get(location.id)
      if (!from) continue
      for (const exit of location.exits || []) {
        const to = nodes.get(exit.target_id)
        if (!to) continue
        list.push({
          key: location.id + '->' + exit.target_id,
          sourceId: location.id,
          targetId: exit.target_id,
          label: exit.label || '',
          door: exit.door || null,
          requires: exit.requires_item || null,
          from,
          to,
        })
      }
    }
    return list
  }, [locations, nodes])

  const regions = useMemo(() => {
    const groups = new Map()
    for (const location of locations) {
      const region = location.region || '未分组'
      if (!groups.has(region)) groups.set(region, [])
      const node = nodes.get(location.id)
      if (node) groups.get(region).push(node)
    }
    return [...groups.entries()].map(([region, members]) => {
      const theme = regionTheme(region)
      const cx = members.reduce((sum, node) => sum + node.x + NODE_W / 2, 0) / members.length
      const cy = members.reduce((sum, node) => sum + node.y + NODE_H / 2, 0) / members.length
      const rx = Math.max(150, (Math.max(...members.map((node) => node.x)) - Math.min(...members.map((node) => node.x))) / 2 + 170)
      const ry = Math.max(120, (Math.max(...members.map((node) => node.y)) - Math.min(...members.map((node) => node.y))) / 2 + 120)
      let seed = 0
      for (const char of region) seed += char.charCodeAt(0)
      return {
        region,
        cx,
        cy,
        rx,
        ry,
        theme,
        count: members.length,
        path: blobPath(cx, cy, rx, ry, seed),
      }
    })
  }, [locations, nodes])

  const clampScale = (value) => Math.min(2.4, Math.max(0.45, value))
  const zoom = (factor) => setView((prev) => ({ ...prev, scale: clampScale(prev.scale * factor) }))

  const onWheel = (event) => {
    event.preventDefault()
    zoom(event.deltaY > 0 ? 0.9 : 1.1)
  }

  const onPointerDown = (event, id) => {
    event.stopPropagation()
    const node = nodes.get(id)
    if (!node) return
    event.currentTarget.setPointerCapture(event.pointerId)
    dragRef.current = {
      id,
      startX: event.clientX,
      startY: event.clientY,
      originX: node.x,
      originY: node.y,
      moved: false,
    }
    setDragging(id)
    setSelectedNode(id)
    setSelectedEdge(null)
  }

  const beginPan = (event) => {
    if (event.button !== 0 || dragRef.current) return
    panRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: view.x,
      originY: view.y,
    }
    setPanning(true)
  }

  const onPointerMove = (event) => {
    if (panRef.current) {
      const pan = panRef.current
      setView((prev) => ({
        ...prev,
        x: pan.originX + (event.clientX - pan.startX),
        y: pan.originY + (event.clientY - pan.startY),
      }))
      return
    }
    const drag = dragRef.current
    if (!drag) return
    const dx = (event.clientX - drag.startX) / view.scale
    const dy = (event.clientY - drag.startY) / view.scale
    if (!drag.moved && Math.abs(dx) < 3 && Math.abs(dy) < 3) return
    drag.moved = true
    setOverrides((prev) => ({
      ...prev,
      [drag.id]: {
        x: Math.max(0, drag.originX + dx),
        y: Math.max(0, drag.originY + dy),
      },
    }))
  }

  const endInteraction = () => {
    const drag = dragRef.current
    dragRef.current = null
    setDragging(null)
    if (drag && drag.moved) {
      let latest = null
      setOverrides((prev) => {
        latest = prev
        writeOverrides(worldId, prev)
        return prev
      })
      // A moved node is a layout decision the author made, so it belongs in the
      // world document, not in this browser's localStorage: persist it (when the
      // host can) and keep localStorage only as the immediate preview.
      const placed = latest && latest[drag.id]
      if (placed && onSavePosition) {
        onSavePosition(drag.id, {
          x: Math.max(0, Math.round((placed.x - PAD) / CELL_X)),
          y: Math.max(0, Math.round((placed.y - PAD) / CELL_Y)),
        })
      }
    }
    panRef.current = null
    setPanning(false)
  }

  const resetLayout = () => {
    setOverrides({})
    writeOverrides(worldId, {})
    setView({ x: 0, y: 0, scale: 1 })
  }

  const saveSelectedPosition = () => {
    if (!selectedNode || !onSavePosition) return
    const node = nodes.get(selectedNode)
    if (!node) return
    onSavePosition(selectedNode, {
      x: Math.max(0, Math.round((node.x - PAD) / CELL_X)),
      y: Math.max(0, Math.round((node.y - PAD) / CELL_Y)),
    })
  }

  if (locations.length === 0) return null
  const selected = selectedEdge ? edges.find((edge) => edge.key === selectedEdge) : null
  const markerPoint = (node) => ({ x: node.x + NODE_W / 2, y: node.y + NODE_H / 2 })
  // Where the selected playthrough stands, if it is on this map at all.
  const cursorNode = cursor ? nodes.get(cursor.locationId) : null
  const cursorLocation = cursorNode
    ? locations.find((item) => item.id === cursor.locationId) || null
    : null

  return (
    <div className="rounded-2xl border border-[#e4e0d8] bg-[#f6f5f1] overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-[#e4e0d8] bg-[#fbfaf7]">
        <span className="text-[10px] font-mono uppercase tracking-[0.25em] text-[#8a857c]">世界地图</span>
        <span className="text-[10px] text-[#a8a29a]">
          {regions.length} 区域 · {locations.length} 地点 · {edges.length} 路线
        </span>
        {cursor && cursorLocation && (
          <span className="text-[10px] text-cyan-300/80">
            ● 玩家：{cursorLocation.name} · v{cursor.version}
          </span>
        )}
        <div className="flex-1" />
        <button onClick={() => zoom(1.15)} className="px-2 py-1 rounded-lg text-xs text-[#5c574f] border border-[#e4e0d8] hover:text-[#1a1a1a]" title="放大">＋</button>
        <span className="text-[10px] font-mono text-[#8a857c] w-12 text-center">{Math.round(view.scale * 100)}%</span>
        <button onClick={() => zoom(0.87)} className="px-2 py-1 rounded-lg text-xs text-[#5c574f] border border-[#e4e0d8] hover:text-[#1a1a1a]" title="缩小">－</button>
        <button onClick={() => setView({ x: 0, y: 0, scale: 1 })} className="px-2 py-1 rounded-lg text-xs text-[#5c574f] border border-[#e4e0d8] hover:text-[#1a1a1a]" title="居中">⌂</button>
        <button onClick={resetLayout} className="px-2 py-1 rounded-lg text-xs text-[#5c574f] border border-[#e4e0d8] hover:text-[#1a1a1a]" title="清除本地布局">重置</button>
        {selectedNode && onSavePosition && (
          <button onClick={saveSelectedPosition} className="px-2.5 py-1 rounded-lg text-[10px] text-cyan-200 border border-cyan-400/30 bg-cyan-500/10 hover:bg-cyan-500/20" title="把当前显示位置写回地点坐标">
            写入坐标
          </button>
        )}
      </div>

      <div
        className="relative overflow-hidden h-[600px] cursor-grab active:cursor-grabbing"
        style={{ background: 'radial-gradient(ellipse at 20% 10%, #0d2830 0%, #071319 45%, #03080b 100%)' }}
        onPointerDown={beginPan}
        onPointerMove={onPointerMove}
        onPointerUp={endInteraction}
        onPointerLeave={endInteraction}
        onWheel={onWheel}
      >
        <svg width="100%" height="100%" className={panning ? 'select-none' : ''}>
          <defs>
            <pattern id="map-grid" width="44" height="44" patternUnits="userSpaceOnUse">
              <path d="M 44 0 L 0 0 0 44" fill="none" stroke="rgba(148,163,184,0.07)" strokeWidth="1" />
            </pattern>
            <pattern id="map-contours" width="160" height="160" patternUnits="userSpaceOnUse">
              <path d="M0 80 Q 40 50 80 80 T 160 80" fill="none" stroke="rgba(148,163,184,0.05)" strokeWidth="1" />
              <path d="M0 130 Q 45 100 90 130 T 180 130" fill="none" stroke="rgba(148,163,184,0.04)" strokeWidth="1" />
            </pattern>
            <marker id="road-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(226,232,240,0.65)" />
            </marker>
            <marker id="road-arrow-locked" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(251,113,133,0.85)" />
            </marker>
            <radialGradient id="map-vignette" cx="50%" cy="45%" r="75%">
              <stop offset="55%" stopColor="rgba(0,0,0,0)" />
              <stop offset="100%" stopColor="rgba(0,0,0,0.55)" />
            </radialGradient>
          </defs>
          <rect width="100%" height="100%" fill="url(#map-grid)" />
          <rect width="100%" height="100%" fill="url(#map-contours)" />
          <rect width="100%" height="100%" fill="url(#map-vignette)" pointerEvents="none" />
          <g transform={`translate(${view.x},${view.y}) scale(${view.scale})`}>
            {regions.map((region) => (
              <g key={region.region}>
                <path d={region.path} fill={region.theme.fill} stroke={region.theme.stroke} strokeWidth={1.4} />
                <text
                  x={region.cx - region.rx * 0.55}
                  y={region.cy - region.ry * 0.5}
                  fill={region.theme.text}
                  style={{ fontSize: 15, fontFamily: 'Georgia, serif', letterSpacing: 3, opacity: 0.85 }}
                >
                  {region.region}
                </text>
                <text
                  x={region.cx - region.rx * 0.55}
                  y={region.cy - region.ry * 0.5 + 18}
                  fill="rgba(226,232,240,0.35)"
                  style={{ fontSize: 9, fontFamily: 'monospace', letterSpacing: 1 }}
                >
                  {region.count} LOCATIONS
                </text>
              </g>
            ))}

            {edges.map((edge) => {
              const locked = edge.door && edge.door.locked
              const active = selectedEdge === edge.key
              const from = markerPoint(edge.from)
              const to = markerPoint(edge.to)
              const bend = Math.max(30, Math.abs(to.x - from.x) * 0.35)
              const path = `M ${from.x} ${from.y} C ${from.x + bend} ${from.y}, ${to.x - bend} ${to.y}, ${to.x} ${to.y}`
              const midX = (from.x + to.x) / 2
              const midY = (from.y + to.y) / 2 - 10
              return (
                <g key={edge.key}>
                  <path d={path} fill="none" stroke="rgba(2,8,12,0.9)" strokeWidth={5} strokeLinecap="round" />
                  <path
                    d={path} fill="none"
                    stroke={active ? 'rgba(129,140,248,0.95)' : locked ? 'rgba(251,113,133,0.75)' : 'rgba(226,232,240,0.45)'}
                    strokeWidth={active ? 2.4 : 1.6}
                    strokeDasharray={locked ? '7 5' : '2 4'}
                    markerEnd={locked ? 'url(#road-arrow-locked)' : 'url(#road-arrow)'}
                  />
                  <path d={path} fill="none" stroke="transparent" strokeWidth={18} className="cursor-pointer" onClick={() => { setSelectedEdge(edge.key); setSelectedNode(null) }} />
                  {(edge.label || edge.door) && (
                    <>
                      <rect x={midX - 28} y={midY - 10} width={56} height={17} rx={8} fill="rgba(3,8,11,0.9)" stroke="rgba(148,163,184,0.25)" />
                      <text x={midX} y={midY + 2.5} textAnchor="middle" fill="rgba(226,232,240,0.8)" style={{ fontSize: 9, fontFamily: 'monospace' }}>
                        {(edge.door ? '⛓ ' : '') + (edge.label || edge.targetId)}
                      </text>
                    </>
                  )}
                </g>
              )
            })}

            {[...nodes.values()].map((node) => {
              const location = locations.find((item) => item.id === node.id)
              if (!location) return null
              const point = markerPoint(node)
              const isStart = location.is_start
              const isSelected = selectedNode === node.id
              const theme = regionTheme(location.region || '未分组')
              const glyph = location.region ? '⌂' : '◆'
              return (
                <g
                  key={node.id}
                  transform={`translate(${point.x},${point.y})`}
                  onPointerDown={(event) => onPointerDown(event, node.id)}
                  onDoubleClick={() => onEditLocation && onEditLocation(location.id)}
                  className={dragging === node.id ? 'cursor-grabbing' : 'cursor-pointer'}
                >
                  {isStart && (
                    <circle r={26} fill="none" stroke="rgba(129,140,248,0.5)" strokeWidth={1.4} strokeDasharray="3 4" />
                  )}
                  {isSelected && <circle r={22} fill="none" stroke={theme.text} strokeWidth={2} />}
                  {cursor && cursor.locationId === node.id && (
                    <g>
                      <circle r={30} fill="none" stroke="rgba(34,211,238,0.35)" strokeWidth={1.2}>
                        <animate attributeName="r" values="22;34;22" dur="2.4s" repeatCount="indefinite" />
                        <animate attributeName="opacity" values="0.9;0.15;0.9" dur="2.4s" repeatCount="indefinite" />
                      </circle>
                      <circle r={20} fill="none" stroke="rgba(34,211,238,0.9)" strokeWidth={2} />
                      <circle r={6} cy={-26} fill="#22d3ee" />
                      <text textAnchor="middle" y={-23} fill="#04141a" style={{ fontSize: 8, fontWeight: 800 }}>
                        P
                      </text>
                    </g>
                  )}
                  <circle r={15} fill="rgba(6,14,20,0.96)" stroke={theme.text} strokeWidth={2} />
                  <text textAnchor="middle" y={5} fill={theme.text} style={{ fontSize: 14 }}>
                    {glyph}
                  </text>
                  <text
                    textAnchor="middle" y={36} fill="#e2e8f0"
                    stroke="rgba(2,8,12,0.92)" strokeWidth={3} paintOrder="stroke"
                    style={{ fontSize: 12, fontWeight: 700 }}
                  >
                    {location.name}
                  </text>
                  <text textAnchor="middle" y={50} fill="rgba(148,163,184,0.75)" style={{ fontSize: 8, fontFamily: 'monospace' }}>
                    {location.region ? location.region + ' · ' : ''}{location.id}
                  </text>
                  <text textAnchor="middle" y={62} fill="rgba(148,163,184,0.5)" style={{ fontSize: 8 }}>
                    ◈ {location.items.length}  ☺ {location.npcs.length}  ⇢ {(location.exits || []).length}
                  </text>
                </g>
              )
            })}
          </g>
        </svg>

        <div className="absolute right-5 top-5 w-14 h-14 rounded-full border border-[#dcd8d0] bg-[#ffffff] flex items-center justify-center pointer-events-none">
          <div className="text-center leading-none">
            <div className="text-[10px] font-mono text-[#5c574f]">N</div>
            <div className="text-lg text-emerald-200">↑</div>
          </div>
        </div>
        <div className="absolute right-5 bottom-4 flex items-center gap-2 pointer-events-none">
          <div className="h-px w-20 bg-white/35" />
          <span className="text-[9px] font-mono text-[#8a857c]">1 GRID ≈ 1 REGION STEP</span>
        </div>
        <div className="absolute left-3 bottom-3 rounded-lg border border-[#e4e0d8] bg-[#f6f5f1]/55 px-2 py-1 text-[10px] font-mono text-[#1a1a1a]/35">
          拖拽地点移动 · 空白拖拽平移 · 滚轮缩放 · 双击编辑
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-[10px] font-mono text-[#1a1a1a]/35 border-t border-[#e4e0d8]">
        <span>道路：虚线为小径</span>
        <span>红色虚线：门未开启</span>
        <span>区域：地形色块</span>
        <span>写入坐标：保存到世界定义</span>
      </div>

      {selected && (
        <div className="px-4 pb-4">
          <div className="rounded-xl border border-[#e4e0d8] bg-[#fbfaf7] p-3 flex flex-wrap items-center gap-3">
            <span className="text-xs text-[#3a3630]">
              {selected.sourceId} → {selected.targetId}
              {selected.label ? '（' + selected.label + '）' : ''}
            </span>
            {selected.door && (
              <span className="text-[10px] font-mono text-amber-300/80">
                门：{selected.door.name}{selected.door.locked ? '（上锁）' : '（已开）'}
              </span>
            )}
            {selected.requires && <span className="text-[10px] font-mono text-amber-300/80">需要：{selected.requires.name}</span>}
            <button onClick={() => onEditLocation && onEditLocation(selected.sourceId)} className="text-[10px] text-[#5c574f] hover:text-[#1a1a1a] tracking-[0.15em] uppercase">编辑来源地点</button>
            <button onClick={() => onDeleteExit && onDeleteExit(selected.sourceId, selected.targetId)} className="text-[10px] text-red-300/70 hover:text-red-200 tracking-[0.15em] uppercase">删除这条出口</button>
            <button onClick={() => setSelectedEdge(null)} className="text-[10px] text-[#8a857c] hover:text-[#3a3630] tracking-[0.15em] uppercase">取消选择</button>
          </div>
        </div>
      )}
    </div>
  )
}
