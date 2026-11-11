import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { Map as MapIcon, X, RefreshCw, Search } from 'lucide-react'
import { fetchWorld, fetchWorldGraph, searchWorldGraph } from '../api'

const NODE_COLORS = {
  NPC: '#a78bfa',
  LOCATION: '#34d399',
  FACTION: '#fbbf24',
  ITEM: '#22d3ee',
  EVENT: '#fb7185',
}

const NODE_TYPE_LABELS = {
  NPC: 'NPC',
  LOCATION: '地点',
  FACTION: '阵营',
  ITEM: '物品',
  EVENT: '事件',
}

const NODE_RADIUS = {
  NPC: 6,
  LOCATION: 8,
  FACTION: 7,
  ITEM: 5,
  EVENT: 5,
}

const TYPE_POSITIONS = {
  LOCATION: { x: 0, y: 0 },
  NPC: { x: 80, y: -60 },
  FACTION: { x: -80, y: -60 },
  ITEM: { x: 60, y: 80 },
  EVENT: { x: -60, y: 80 },
}

function presentEntitiesFrom(world, engineState) {
  return (world?.entities || []).filter((entity) => {
    const runtime = engineState?.entities?.[entity.id]
    const locationId = runtime?.location_id || entity.location_id
    return locationId === engineState?.location_id && runtime?.alive !== false
  })
}

export default function WorldMapModal({ open, onClose, campaignId, engineState, onEngineAction, busy = false }) {
  const [graphData, setGraphData] = useState({ nodes: [], links: [] })
  const [world, setWorld] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [pending, setPending] = useState(false)
  const [remoteTarget, setRemoteTarget] = useState('')
  const [hoverNode, setHoverNode] = useState(null)
  const [hoverLink, setHoverLink] = useState(null)
  const [selectedNode, setSelectedNode] = useState(null)
  const [tooltipPos, setTooltipPos] = useState({ x: 0, y: 0 })
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchHighlightSet, setSearchHighlightSet] = useState(null)
  const graphRef = useRef()
  const containerRef = useRef()

  const loadGraph = async () => {
    if (!campaignId) return
    setLoading(true)
    try {
      const data = await fetchWorldGraph(campaignId)
      setGraphData(data)
    } catch {
      setGraphData({ nodes: [], links: [] })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) {
      loadGraph()
      setSelectedNode(null)
      setSearchHighlightSet(null)
    }
  }, [open, campaignId])

  useEffect(() => {
    if (!open || !engineState?.world_id) {
      setWorld(null)
      return undefined
    }
    let cancelled = false
    setError('')
    fetchWorld(engineState.world_id)
      .then((definition) => { if (!cancelled) setWorld(definition) })
      .catch((err) => { if (!cancelled) setError(err.message || '世界定义读取失败') })
    return () => { cancelled = true }
  }, [open, engineState?.world_id])

  const currentLocation = world?.locations?.find((item) => item.id === engineState?.location_id) || null
  const exits = (currentLocation?.exits || [])
    .map((exitDef) => ({ exit: exitDef, location: world?.locations?.find((item) => item.id === exitDef.target_id) }))
    .filter((entry) => entry.location)
  const shopOffers = []
  for (const entity of presentEntitiesFrom(world, engineState)) {
    if (!entity.shop_items || entity.shop_items.length === 0) continue
    for (const itemId of entity.shop_items) {
      const item = (world?.items || []).find((entry) => entry.id === itemId)
      if (item) shopOffers.push({ merchant: entity, item })
    }
  }

  const presentEntities = (world?.entities || []).filter((entity) => {
    const runtime = engineState?.entities?.[entity.id]
    const locationId = runtime?.location_id || entity.location_id
    return locationId === engineState?.location_id && runtime?.alive !== false
  })

  const routeTo = (destinationId) => {
    if (!world || !engineState?.location_id || !destinationId) return null
    if (destinationId === engineState.location_id) return []
    const byId = new Map((world.locations || []).map((item) => [item.id, item]))
    const queue = [[engineState.location_id]]
    const seen = new Set([engineState.location_id])
    while (queue.length > 0) {
      const path = queue.shift()
      const current = byId.get(path[path.length - 1])
      if (!current) continue
      for (const exit of current.exits || []) {
        if (seen.has(exit.target_id)) continue
        const nextPath = [...path, exit.target_id]
        if (exit.target_id === destinationId) return nextPath.slice(1)
        seen.add(exit.target_id)
        queue.push(nextPath)
      }
    }
    return null
  }

  const remoteRoutes = (world?.locations || [])
    .filter((location) => location.id !== engineState?.location_id)
    .map((location) => ({ location, path: routeTo(location.id) }))
    .filter((entry) => entry.path && entry.path.length > 1)

  const runAction = async (action, targetId, label, params) => {
    if (!onEngineAction || busy || pending) return
    setPending(true)
    setError('')
    try {
      const result = await onEngineAction({ action, targetId, label, params })
      if (result && !result.ok) setError(result.message || '行动被规则拒绝。')
    } catch (err) {
      setError(err.message || '行动请求失败')
    } finally {
      setPending(false)
    }
  }

  // Clustering forces — apply after graph mounts / data changes
  useEffect(() => {
    if (!graphRef.current || graphData.nodes.length === 0) return

    const STRENGTH = 0.05
    // Custom force function that nudges nodes toward type-based positions
    const clusterForce = (alpha) => {
      graphData.nodes.forEach((node) => {
        const pos = TYPE_POSITIONS[node.node_type]
        if (!pos) return
        node.vx += (pos.x - (node.x || 0)) * STRENGTH * alpha
        node.vy += (pos.y - (node.y || 0)) * STRENGTH * alpha
      })
    }
    clusterForce.initialize = () => {}
    graphRef.current.d3Force('cluster', clusterForce)
    graphRef.current.d3ReheatSimulation()
  }, [graphData])

  // Compute link counts per node
  const linkCountMap = useMemo(() => {
    const counts = {}
    graphData.links.forEach((link) => {
      const srcId = typeof link.source === 'object' ? link.source.id : link.source
      const tgtId = typeof link.target === 'object' ? link.target.id : link.target
      counts[srcId] = (counts[srcId] || 0) + 1
      counts[tgtId] = (counts[tgtId] || 0) + 1
    })
    return counts
  }, [graphData.links])

  const highlightSet = useMemo(() => {
    if (!hoverNode) return null
    const neighbors = new Set([hoverNode.id])
    graphData.links.forEach((link) => {
      const srcId = typeof link.source === 'object' ? link.source.id : link.source
      const tgtId = typeof link.target === 'object' ? link.target.id : link.target
      if (srcId === hoverNode.id) neighbors.add(tgtId)
      if (tgtId === hoverNode.id) neighbors.add(srcId)
    })
    return neighbors
  }, [hoverNode, graphData.links])

  // Build search highlight set from search results
  useEffect(() => {
    if (searchResults.length === 0) {
      setSearchHighlightSet(null)
      return
    }
    const matchedIds = new Set()
    const nodeNames = graphData.nodes.map((n) => ({
      id: n.id,
      nameLower: (n.name || '').toLowerCase(),
    }))
    searchResults.forEach((r) => {
      const factLower = (r.fact || '').toLowerCase()
      nodeNames.forEach(({ id, nameLower }) => {
        if (nameLower && factLower.includes(nameLower)) {
          matchedIds.add(id)
        }
      })
    })
    setSearchHighlightSet(matchedIds.size > 0 ? matchedIds : null)
  }, [searchResults, graphData.nodes])

  const nodeCanvasObject = useCallback(
    (node, ctx, globalScale) => {
      const isHighlighted = !highlightSet || highlightSet.has(node.id)
      const isSearchMatch = searchHighlightSet && searchHighlightSet.has(node.id)
      const radius = NODE_RADIUS[node.node_type] || 5
      const color = NODE_COLORS[node.node_type] || '#94a3b8'
      const nodeLinks = linkCountMap[node.id] || 0
      const isImportant = nodeLinks >= 3 || node.node_type === 'LOCATION'

      // Search highlight — pulsing glow ring
      if (isSearchMatch) {
        const pulse = Math.sin(Date.now() / 300) * 0.3 + 0.7 // 0.4 to 1.0
        const glowRadius = radius + 5
        ctx.beginPath()
        ctx.arc(node.x, node.y, glowRadius, 0, 2 * Math.PI)
        ctx.strokeStyle = color
        ctx.lineWidth = 2.5
        ctx.globalAlpha = pulse
        ctx.stroke()
        ctx.globalAlpha = 1
      }

      // Node circle
      ctx.beginPath()
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI)
      ctx.fillStyle = isHighlighted ? color : color + '33'
      ctx.fill()

      if (isHighlighted && globalScale > 0.8) {
        ctx.strokeStyle = color + '66'
        ctx.lineWidth = 1.5
        ctx.stroke()
      }

      // Label — always show for important nodes, otherwise only when zoomed or hovered
      const showLabel =
        isImportant || globalScale > 1.2 || (hoverNode && hoverNode.id === node.id)

      if (showLabel) {
        const label = node.name
        const baseFontSize = isImportant ? 12 : 10
        const fontSize = Math.max(baseFontSize / globalScale, 3)
        ctx.font = `${isImportant ? 'bold ' : ''}${fontSize}px sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'top'
        ctx.fillStyle = isHighlighted ? '#e2e8f0' : '#64748b'
        ctx.fillText(label, node.x, node.y + radius + 2)
      }
    },
    [hoverNode, highlightSet, linkCountMap, searchHighlightSet],
  )

  // Link canvas object — draw edge label on hover
  const linkCanvasObject = useCallback(
    (link, ctx, globalScale) => {
      if (link !== hoverLink) return
      const relType = link.rel_type
      if (!relType) return

      const src = link.source
      const tgt = link.target
      if (!src || !tgt || src.x == null || tgt.x == null) return

      const midX = (src.x + tgt.x) / 2
      const midY = (src.y + tgt.y) / 2

      const fontSize = Math.max(10 / globalScale, 3)
      ctx.font = `${fontSize}px sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'

      // Background for readability
      const text = relType.replace(/_/g, ' ')
      const textWidth = ctx.measureText(text).width
      const padding = 2 / globalScale
      ctx.fillStyle = 'rgba(15, 23, 42, 0.85)'
      ctx.fillRect(
        midX - textWidth / 2 - padding,
        midY - fontSize / 2 - padding,
        textWidth + padding * 2,
        fontSize + padding * 2,
      )

      ctx.fillStyle = '#cbd5e1'
      ctx.fillText(text, midX, midY)
    },
    [hoverLink],
  )

  const linkColor = useCallback(
    (link) => {
      if (link === hoverLink) return 'rgba(148, 163, 184, 0.8)'
      if (!hoverNode) return 'rgba(148, 163, 184, 0.2)'
      const srcId = typeof link.source === 'object' ? link.source.id : link.source
      const tgtId = typeof link.target === 'object' ? link.target.id : link.target
      if (srcId === hoverNode.id || tgtId === hoverNode.id) {
        return 'rgba(148, 163, 184, 0.6)'
      }
      return 'rgba(148, 163, 184, 0.05)'
    },
    [hoverNode, hoverLink],
  )

  const handleNodeClick = useCallback((node, event) => {
    setSelectedNode(node)
    const rect = containerRef.current?.getBoundingClientRect()
    if (rect) {
      setTooltipPos({
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      })
    }
  }, [])

  const searchTimeoutRef = useRef(null)

  const handleSearch = async () => {
    if (!searchQuery.trim() || !campaignId) return
    const data = await searchWorldGraph(campaignId, searchQuery)
    setSearchResults(data.facts || [])
  }

  const handleSearchInputChange = (e) => {
    setSearchQuery(e.target.value)
    clearTimeout(searchTimeoutRef.current)
    if (e.target.value.trim()) {
      searchTimeoutRef.current = setTimeout(handleSearch, 400)
    }
  }

  const handleBackgroundClick = useCallback(() => {
    setSelectedNode(null)
  }, [])

  // Zoom to fit when simulation stabilizes
  const handleEngineStop = useCallback(() => {
    graphRef.current?.zoomToFit(400, 50)
  }, [])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div
        ref={containerRef}
        className="bg-white/[0.03] backdrop-blur-xl border border-white/5 rounded-2xl sm:rounded-[2rem] w-full max-w-4xl mx-2 sm:mx-4 overflow-hidden flex flex-col"
        style={{ height: 'min(80vh, 100dvh - 2rem)' }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5 flex-none">
          <div className="flex items-center gap-3">
            <MapIcon size={16} className="text-emerald-400" />
            <h2 className="text-white font-bold text-sm uppercase tracking-widest">世界地图</h2>
            {graphData.nodes.length > 0 && (
              <span className="text-white/20 text-xs ml-2">
                {graphData.nodes.length} 个节点 &middot; {graphData.links.length} 条连线
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={loadGraph}
              disabled={loading}
              className="text-white/40 hover:text-white transition-colors p-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/50"
              title="刷新"
              aria-label="刷新世界地图"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
            <button onClick={onClose} className="text-white/40 hover:text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/50" aria-label="关闭世界地图">
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Legend */}
        <div className="flex items-center gap-4 px-6 py-2 border-b border-white/5 flex-none">
          {Object.entries(NODE_COLORS).map(([type, color]) => (
            <div key={type} className="flex items-center gap-1.5">
              <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }} />
              <span className="text-[10px] text-white/40 uppercase tracking-wider">{NODE_TYPE_LABELS[type] || type}</span>
            </div>
          ))}
        </div>

        {/* Rules-world navigator: the same action pipeline as typed text */}
        {engineState && (
          <div className="px-6 py-3 border-b border-white/5 flex-none flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2 text-xs text-white/70">
              <span className="text-white/40">当前位置</span>
              <span className="font-bold text-white">{currentLocation ? currentLocation.name : engineState.location_id}</span>
              {currentLocation?.region && <span className="text-white/40 text-[10px] font-mono">{currentLocation.region}</span>}
              {currentLocation?.description && <span className="text-white/40 truncate max-w-lg">{currentLocation.description}</span>}
            </div>
            {error && <p role="alert" className="text-rose-300 text-xs">{error}</p>}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-white/40 text-xs">前往</span>
              {exits.length === 0 && <span className="text-white/30 text-xs">这里没有可通行的出口</span>}
              {exits.map(({ exit: exitDef, location }) => (
                <button
                  key={location.id}
                  type="button"
                  disabled={busy || pending}
                  onClick={() => runAction('move', location.id, `前往 ${location.name}`)}
                  className="px-2.5 py-1 rounded-lg text-[11px] text-emerald-200 bg-emerald-500/15 border border-emerald-400/30 hover:bg-emerald-500/25 disabled:opacity-40"
                >
                  前往 {location.name}{location.region ? `（${location.region}）` : ''}{exitDef.door_id ? '（有门）' : ''}
                </button>
              ))}
            </div>
            {remoteRoutes.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-white/40 text-xs">沿路线前往</span>
                <select
                  className="bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-xs text-white"
                  value={remoteTarget}
                  onChange={(e) => setRemoteTarget(e.target.value)}
                >
                  <option value="">选择目的地</option>
                  {remoteRoutes.map(({ location, path }) => (
                    <option key={location.id} value={location.id}>
                      {location.name}{location.region ? `（${location.region}）` : ''} · {path.length} 段
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={!remoteTarget || busy || pending}
                  onClick={() => {
                    const route = remoteRoutes.find((entry) => entry.location.id === remoteTarget)
                    if (!route) return
                    runAction('move', remoteTarget, `前往 ${route.location.name}`, { path: route.path })
                  }}
                  className="px-2.5 py-1 rounded-lg text-[11px] text-cyan-200 bg-cyan-500/15 border border-cyan-400/30 hover:bg-cyan-500/25 disabled:opacity-40"
                >
                  沿路线前往
                </button>
              </div>
            )}
            {shopOffers.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-white/40 text-xs">商店</span>
                {shopOffers.map(({ merchant, item }) => (
                  <button
                    key={merchant.id + ':' + item.id}
                    type="button"
                    disabled={busy || pending}
                    onClick={() => runAction(
                      'buy', item.id, `购买 ${item.name}（${item.price}）`,
                      { merchant_id: merchant.id },
                    )}
                    className="px-2.5 py-1 rounded-lg text-[11px] text-amber-200 bg-amber-500/15 border border-amber-400/30 hover:bg-amber-500/25 disabled:opacity-40"
                  >
                    {item.name} · {item.price}
                  </button>
                ))}
              </div>
            )}
            {presentEntities.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-white/40 text-xs">在场</span>
                {presentEntities.map((entity) => (
                  <button
                    key={entity.id}
                    type="button"
                    disabled={busy || pending || entity.kind === 'object'}
                    onClick={() => runAction('talk', entity.id, `交谈 ${entity.name}`)}
                    className="px-2.5 py-1 rounded-lg text-[11px] text-violet-200 bg-violet-500/15 border border-violet-400/30 hover:bg-violet-500/25 disabled:opacity-40"
                  >
                    {entity.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Search */}
        <div className="px-6 py-2 border-b border-white/5 flex-none">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={searchQuery}
              onChange={handleSearchInputChange}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder="搜索世界事实……"
              className="flex-1 bg-white/[0.03] text-gray-200 text-sm rounded-lg px-3 py-1.5 border border-white/10 focus:outline-none focus:border-white/40 focus:bg-white/[0.05] placeholder-white/20"
            />
            <button
              onClick={handleSearch}
              className="text-white/40 hover:text-white transition-colors p-1.5"
              title="搜索"
            >
              <Search size={14} />
            </button>
          </div>
          {searchResults.length > 0 && (
            <div className="mt-2 mb-1 p-2 bg-gray-800/50 rounded text-sm max-h-32 overflow-y-auto relative">
              <button
                onClick={() => setSearchResults([])}
                className="absolute top-1 right-1 text-white/20 hover:text-white transition-colors"
                title="清除结果"
              >
                <X size={12} />
              </button>
              {searchResults.map((r, i) => (
                <div key={i} className="text-gray-300 py-1 border-b border-gray-700/50 last:border-0">
                  {r.fact}
                  {r.valid_at && <span className="text-gray-500 text-xs ml-2">起始 {r.valid_at}</span>}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Graph */}
        <div className="flex-1 relative" onClick={handleBackgroundClick}>
          {graphData.nodes.length === 0 ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <MapIcon size={32} className="text-white/20 mx-auto mb-3" />
                <p className="text-white/20 text-sm font-light">
                  {loading
                    ? '正在绘制世界拓扑……'
                    : '暂无世界数据。开始游玩以填充图谱。'}
                </p>
              </div>
            </div>
          ) : (
            <ForceGraph2D
              ref={graphRef}
              graphData={graphData}
              nodeCanvasObject={nodeCanvasObject}
              linkColor={linkColor}
              linkWidth={(link) => Math.max((link.strength || 0.5) * 2, 0.5)}
              linkCanvasObjectMode={() => 'after'}
              linkCanvasObject={linkCanvasObject}
              linkDirectionalParticles={0}
              onNodeHover={setHoverNode}
              onLinkHover={setHoverLink}
              onNodeClick={handleNodeClick}
              onBackgroundClick={handleBackgroundClick}
              onEngineStop={handleEngineStop}
              backgroundColor="transparent"
              width={containerRef.current?.clientWidth || 800}
              height={(containerRef.current?.clientHeight || 600) - 100}
              cooldownTicks={100}
              d3AlphaDecay={0.02}
              d3VelocityDecay={0.3}
            />
          )}

          {/* Tooltip */}
          {selectedNode && (
            <div
              className="absolute z-10 bg-white/[0.03] backdrop-blur-xl border border-white/5 rounded-[2rem] p-4 min-w-[200px] max-w-[280px] shadow-[0_0_40px_rgba(255,255,255,0.15)] pointer-events-none"
              style={{
                left: Math.min(tooltipPos.x + 10, (containerRef.current?.clientWidth || 800) - 300),
                top: Math.min(tooltipPos.y - 10, (containerRef.current?.clientHeight || 600) - 200),
              }}
            >
              <div className="flex items-center gap-2 mb-2">
                <div
                  className="w-3 h-3 rounded-full"
                  style={{ backgroundColor: NODE_COLORS[selectedNode.node_type] || '#94a3b8' }}
                />
                <span className="text-white font-bold text-sm">{selectedNode.name}</span>
              </div>
              <span
                className="inline-block text-[10px] uppercase tracking-widest font-bold px-2 py-0.5 rounded-full mb-2"
                style={{
                  backgroundColor: (NODE_COLORS[selectedNode.node_type] || '#94a3b8') + '22',
                  color: NODE_COLORS[selectedNode.node_type] || '#94a3b8',
                }}
              >
                {NODE_TYPE_LABELS[selectedNode.node_type] || selectedNode.node_type}
              </span>
              {selectedNode.attributes && Object.keys(selectedNode.attributes).length > 0 && (
                <div className="space-y-1 mt-2 border-t border-white/5 pt-2">
                  {Object.entries(selectedNode.attributes)
                    .slice(0, 4)
                    .map(([key, val]) => (
                      <div key={key} className="flex justify-between text-xs">
                        <span className="text-white/40">{key.replace(/_/g, ' ')}</span>
                        <span className="text-[#d1d1d1] font-medium ml-3 truncate max-w-[120px]">
                          {String(val)}
                        </span>
                      </div>
                    ))}
                </div>
              )}
              {selectedNode.node_type === 'NPC' && selectedNode.attributes?.power_level != null && (
                <div className="mt-2 border-t border-white/5 pt-2">
                  <div className="flex items-center justify-between text-xs mb-1">
                    <span className="text-white/40">战力等级</span>
                    <span className="text-white font-bold">{selectedNode.attributes.power_level}/10</span>
                  </div>
                  <div className="w-full h-1.5 bg-white/[0.03] rounded-full overflow-hidden">
                    <div
                      className="h-full bg-white/10 rounded-full"
                      style={{ width: `${(selectedNode.attributes.power_level / 10) * 100}%` }}
                    />
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
