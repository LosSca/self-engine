import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const NODE_W = 150
const NODE_H = 58
const GAP_X = 96
const GAP_Y = 74
const PAD = 28
const CELL_X = 190
const CELL_Y = 120

function positionsKey(worldId) {
  return 'lunar.map.positions.' + (worldId || '')
}

/**
 * Deterministic layered layout: breadth-first from the start location, so the
 * village sits on the left and each ring of neighbours forms a column. Nodes
 * the start cannot reach get their own trailing column instead of vanishing.
 */
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
  } catch (err) {
    return {}
  }
}

function writeOverrides(worldId, value) {
  try {
    window.localStorage.setItem(positionsKey(worldId), JSON.stringify(value))
  } catch (err) {
    /* a full or disabled storage must not break the editor */
  }
}

export default function WorldMapGraph({ worldId, mapData, onEditLocation, onDeleteExit }) {
  const locations = (mapData && mapData.locations) || []
  const startId = mapData && mapData.start_location_id
  const [overrides, setOverrides] = useState(() => readOverrides(worldId))
  const [selectedEdge, setSelectedEdge] = useState(null)
  const dragRef = useRef(null)
  const [dragging, setDragging] = useState(null)

  useEffect(() => {
    setOverrides(readOverrides(worldId))
    setSelectedEdge(null)
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
          missing: !locations.some((item) => item.id === exit.target_id),
        })
      }
    }
    return list
  }, [locations, nodes])

  const width = useMemo(() => {
    let max = 0
    for (const node of nodes.values()) max = Math.max(max, node.x + NODE_W)
    return Math.max(max + PAD, 360)
  }, [nodes])

  const height = useMemo(() => {
    let max = 0
    for (const node of nodes.values()) max = Math.max(max, node.y + NODE_H)
    return Math.max(max + PAD, 220)
  }, [nodes])

  const onPointerDown = useCallback(
    (event, id) => {
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
    },
    [nodes]
  )

  const onPointerMove = useCallback((event) => {
    const drag = dragRef.current
    if (!drag) return
    const dx = event.clientX - drag.startX
    const dy = event.clientY - drag.startY
    if (!drag.moved && Math.abs(dx) < 3 && Math.abs(dy) < 3) return
    drag.moved = true
    setOverrides((prev) => ({
      ...prev,
      [drag.id]: { x: Math.max(0, drag.originX + dx), y: Math.max(0, drag.originY + dy) },
    }))
  }, [])

  const endDrag = useCallback(() => {
    const drag = dragRef.current
    dragRef.current = null
    setDragging(null)
    if (!drag) return
    setOverrides((prev) => {
      if (drag.moved) writeOverrides(worldId, prev)
      return prev
    })
  }, [worldId])

  const resetLayout = useCallback(() => {
    setOverrides({})
    writeOverrides(worldId, {})
  }, [worldId])

  if (locations.length === 0) return null

  const selected = selectedEdge ? edges.find((edge) => edge.key === selectedEdge) : null

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4 flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-white/40">
          地图视图 · 拖动节点可调整布局（只影响本地显示）
        </div>
        <button
          onClick={resetLayout}
          className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
        >
          重置布局
        </button>
      </div>

      <div className="overflow-auto rounded-xl bg-black/30">
        <svg
          width={width}
          height={height}
          viewBox={'0 0 ' + width + ' ' + height}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerLeave={endDrag}
          className="select-none"
        >
          <defs>
            <marker
              id="lunar-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(255,255,255,0.35)" />
            </marker>
            <marker
              id="lunar-arrow-locked"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(248,113,113,0.8)" />
            </marker>
          </defs>

          {edges.map((edge) => {
            const locked = edge.door && edge.door.locked
            const active = selectedEdge === edge.key
            const x1 = edge.from.x + NODE_W
            const y1 = edge.from.y + NODE_H / 2
            const x2 = edge.to.x
            const y2 = edge.to.y + NODE_H / 2
            const midX = (x1 + x2) / 2
            const midY = (y1 + y2) / 2
            return (
              <g key={edge.key}>
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke={active ? 'rgba(129,140,248,0.9)' : locked ? 'rgba(248,113,113,0.55)' : 'rgba(255,255,255,0.22)'}
                  strokeWidth={active ? 2.5 : 1.5}
                  strokeDasharray={locked ? '6 4' : undefined}
                  markerEnd={locked ? 'url(#lunar-arrow-locked)' : 'url(#lunar-arrow)'}
                />
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke="transparent"
                  strokeWidth={12}
                  className="cursor-pointer"
                  onClick={() => setSelectedEdge(edge.key)}
                />
                {(edge.label || edge.door || edge.requires) && (
                  <text
                    x={midX}
                    y={midY - 4}
                    textAnchor="middle"
                    className="fill-white/40"
                    style={{ fontSize: 9, fontFamily: 'monospace' }}
                  >
                    {(edge.door ? '🔒 ' : '') + (edge.label || edge.targetId)}
                  </text>
                )}
              </g>
            )
          })}

          {[...nodes.values()].map((node) => {
            const location = locations.find((item) => item.id === node.id)
            if (!location) return null
            const isStart = location.is_start
            return (
              <g
                key={node.id}
                transform={'translate(' + node.x + ',' + node.y + ')'}
                onPointerDown={(event) => onPointerDown(event, node.id)}
                onDoubleClick={() => onEditLocation && onEditLocation(location.id)}
                className={dragging === node.id ? 'cursor-grabbing' : 'cursor-grab'}
              >
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={12}
                  fill={isStart ? 'rgba(99,102,241,0.22)' : 'rgba(255,255,255,0.05)'}
                  stroke={isStart ? 'rgba(129,140,248,0.8)' : 'rgba(255,255,255,0.16)'}
                  strokeWidth={1.5}
                />
                <text x={12} y={22} className="fill-white" style={{ fontSize: 12, fontWeight: 700 }}>
                  {location.name.length > 12 ? location.name.slice(0, 12) + '…' : location.name}
                </text>
                <text
                  x={12}
                  y={38}
                  className="fill-white/40"
                  style={{ fontSize: 9, fontFamily: 'monospace' }}
                >
                  {(location.region ? location.region + ' · ' : '') +
                    (location.id.length > 18 ? location.id.slice(0, 18) + '…' : location.id)}
                </text>
                <text x={12} y={50} className="fill-white/35" style={{ fontSize: 8 }}>
                  {location.items.length} 物品 · {location.npcs.length} 实体 ·{' '}
                  {(location.exits || []).length} 出口
                </text>
              </g>
            )
          })}
        </svg>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[10px] font-mono text-white/35">
        <span>实线：可通行</span>
        <span>虚线红：门未开启</span>
        <span>双击节点：编辑地点</span>
        <span>点击连线：查看出口</span>
      </div>

      {selected && (
        <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 flex flex-wrap items-center gap-3">
          <span className="text-xs text-white/70">
            {selected.sourceId} → {selected.targetId}
            {selected.label ? '（' + selected.label + '）' : ''}
          </span>
          {selected.door && (
            <span className="text-[10px] font-mono text-amber-300/80">
              门：{selected.door.name}
              {selected.door.locked ? '（上锁）' : '（已开）'}
            </span>
          )}
          {selected.requires && (
            <span className="text-[10px] font-mono text-amber-300/80">
              需要：{selected.requires.name}
            </span>
          )}
          <button
            onClick={() => onEditLocation && onEditLocation(selected.sourceId)}
            className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
          >
            编辑来源地点
          </button>
          <button
            onClick={() => onDeleteExit && onDeleteExit(selected.sourceId, selected.targetId)}
            className="text-[10px] text-red-300/70 hover:text-red-200 tracking-[0.15em] uppercase"
          >
            删除这条出口
          </button>
          <button
            onClick={() => setSelectedEdge(null)}
            className="text-[10px] text-white/40 hover:text-white/70 tracking-[0.15em] uppercase"
          >
            取消选择
          </button>
        </div>
      )}
    </div>
  )
}
