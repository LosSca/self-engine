const BASE = '/api'  // proxied to http://localhost:8000 via vite proxy

export async function checkNeo4j() {
  try {
    const r = await fetch(`${BASE}/health/neo4j`)
    if (!r.ok) return false
    const data = await r.json()
    return data.status === 'ok'
  } catch {
    return false
  }
}

export async function fetchScenarios() {
  const r = await fetch(`${BASE}/scenarios/`)
  if (!r.ok) throw new Error('Failed to fetch scenarios')
  return r.json()
}

export async function createScenario(data) {
  const r = await fetch(`${BASE}/scenarios/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!r.ok) throw new Error('Failed to create scenario')
  return r.json()
}

export async function getStoryCards(scenarioId) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/story-cards`)
  if (!r.ok) throw new Error('Failed to fetch story cards')
  return r.json()
}

export async function addStoryCard(scenarioId, data) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/story-cards`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!r.ok) throw new Error('Failed to add story card')
  return r.json()
}

export async function fetchCampaigns(scenarioId) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/campaigns`)
  if (!r.ok) throw new Error('Failed to fetch campaigns')
  return r.json()
}

export async function createCampaign(scenarioId, playerName = '旅人') {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/campaigns`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ player_name: playerName }),
  })
  if (!r.ok) throw new Error('Failed to create campaign')
  return r.json()
}

export async function fetchCharacters(campaignId, query = '') {
  const params = query ? `?q=${encodeURIComponent(query)}` : ''
  const r = await fetch(`${BASE}/game/${campaignId}/characters${params}`)
  if (!r.ok) return []
  return r.json()
}

export async function fetchHistory(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/history`)
  if (!r.ok) throw new Error('Failed to fetch history')
  return r.json()
}

export function streamAction({
  campaignId,
  scenarioTone,
  language,
  action,
  openingNarrative,
  maxTokens,
  provider = null,
  model = null,
  temperature,
  combatEnabled,
  onChunk,
  onJournal,
  onMode,
  onCrystal,
  onPlotAuto,
  onInventory,
  onImage,
  onSuggestions,
  onTruncateClean,
  onUsage,
  onTrace,
  onDone,
  onError,
}) {
  fetch(`${BASE}/game/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      campaign_id: campaignId,
      scenario_tone: scenarioTone,
      language,
      action,
      opening_narrative: openingNarrative || '',
      max_tokens: maxTokens || 2000,
      // Do not invent a model here: an empty value lets the server use its
      // configured provider. Hardcoding a model made every turn fall back.
      provider: provider || '',
      model: model || '',
      temperature: temperature ?? 0.85,
      combat_enabled: combatEnabled,
    }),
  })
    .then(async (res) => {
      if (!res.ok) throw new Error('Action request failed')
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      const handleData = (data) => {
        const control = data.trim()
        if (control === '[DONE]') {
          onDone?.()
          return true
        }
        if (control.startsWith('[JOURNAL]')) {
          try {
            const entry = JSON.parse(control.slice(9))
            onJournal?.(entry)
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[MODE]')) {
          onMode?.(control.slice(6))
          return false
        }
        if (control.startsWith('[CRYSTAL]')) {
          try {
            const crystal = JSON.parse(control.slice(9))
            onCrystal?.(crystal)
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[PLOT_AUTO]')) {
          try {
            const plot = JSON.parse(control.slice(11))
            onPlotAuto?.(plot)
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[INVENTORY]')) {
          try {
            const item = JSON.parse(control.slice(11))
            onInventory?.(item)
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[IMAGE]')) {
          try {
            onImage?.(JSON.parse(control.slice(7)))
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[SUGGESTIONS]')) {
          try {
            onSuggestions?.(JSON.parse(control.slice(13)))
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[TRUNCATE_CLEAN]')) {
          const cleanText = control.slice(16)
          onTruncateClean?.(cleanText)
          return false
        }
        if (control.startsWith('[USAGE]')) {
          try {
            const usage = JSON.parse(control.slice(7))
            onUsage?.(usage)
          } catch { /* optional */ }
          return false
        }
        if (control.startsWith('[TRACE]')) {
          try {
            const trace = JSON.parse(control.slice(7))
            onTrace?.(trace)
          } catch { /* optional */ }
          return false
        }
        // Strip inventory tags from narrative display
        const cleaned = data
          .replace(/\[ITEM_ADD:[^\]]+\]/g, '')
          .replace(/\[ITEM_USE:[^\]]+\]/g, '')
          .replace(/\[ITEM_LOSE:[^\]]+\]/g, '')
        if (cleaned.trim()) {
          onChunk?.(cleaned)
        }
        return false
      }

      const handleEventBlock = (eventBlock) => {
        if (!eventBlock) return false
        const dataLines = []
        const lines = eventBlock.split('\n')
        for (const rawLine of lines) {
          const line = rawLine.replace(/\r$/, '')
          if (!line.startsWith('data:')) continue
          let payload = line.slice(5)
          if (payload.startsWith(' ')) payload = payload.slice(1)
          dataLines.push(payload)
        }
        if (dataLines.length === 0) return false
        return handleData(dataLines.join('\n'))
      }

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let sepIndex = buffer.indexOf('\n\n')
        while (sepIndex !== -1) {
          const eventBlock = buffer.slice(0, sepIndex)
          buffer = buffer.slice(sepIndex + 2)
          if (handleEventBlock(eventBlock)) return
          sepIndex = buffer.indexOf('\n\n')
        }
      }

      buffer += decoder.decode()
      if (buffer && handleEventBlock(buffer)) {
        return
      }
      onDone?.()
    })
    .catch((err) => onError?.(err))
}

export async function rewindLastAction(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/rewind`, {
    method: 'POST',
  })
  if (!r.ok) throw new Error('Failed to rewind')
  return r.json()
}

export async function exportScenario(scenarioId, title) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/export`)
  if (!r.ok) throw new Error('Failed to export scenario')
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${title || scenarioId}.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export async function fetchJournal(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/journal`)
  if (!r.ok) throw new Error('Failed to fetch journal')
  return r.json()
}

export async function fetchWorldGraph(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/world-graph`)
  if (!r.ok) throw new Error('Failed to fetch world graph')
  return r.json()
}

export async function searchWorldGraph(campaignId, query) {
  const res = await fetch(`${BASE}/game/${campaignId}/graph-search?q=${encodeURIComponent(query)}`)
  if (!res.ok) throw new Error('Failed to search world graph')
  return res.json()
}

export async function fetchInventory(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/inventory`)
  if (!r.ok) throw new Error('Failed to fetch inventory')
  return r.json()
}

export async function fetchTraces(campaignId, limit = 25) {
  try {
    const r = await fetch(`${BASE}/game/${campaignId}/traces?limit=${limit}`)
    if (!r.ok) return []
    const data = await r.json()
    return data.traces
  } catch {
    return []
  }
}

export async function deleteTraces(campaignId) {
  try {
    const r = await fetch(`${BASE}/game/${campaignId}/traces`, { method: 'DELETE' })
    if (!r.ok) return { deleted: 0 }
    return r.json()
  } catch {
    return { deleted: 0 }
  }
}

export async function updateInventoryItem(campaignId, name, action) {
  const r = await fetch(`${BASE}/game/${campaignId}/inventory`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, action }),
  })
  if (!r.ok) throw new Error('Failed to update inventory')
  return r.json()
}

export async function deleteCampaign(scenarioId, campaignId) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/campaigns/${campaignId}`, {
    method: 'DELETE',
  })
  if (!r.ok) throw new Error('Failed to delete campaign')
  return r.json()
}

export async function deleteScenario(scenarioId) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}`, {
    method: 'DELETE',
  })
  if (!r.ok) throw new Error('Failed to delete scenario')
  return r.json()
}

export async function importScenario(data) {
  const r = await fetch(`${BASE}/scenarios/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!r.ok) {
    const err = new Error('创建情景失败')
    err.status = r.status
    try {
      const body = await r.json()
      err.detail = body.detail
      if (typeof body.detail === 'string') err.message = body.detail
      else if (Array.isArray(body.detail)) {
        err.message = body.detail
          .map((item) => (item.loc ? item.loc.slice(1).join('.') + '：' + item.msg : item.msg))
          .join('；')
      }
    } catch {
      // 响应不是 JSON 时使用默认文案
    }
    throw err
  }
  return r.json()
}

export async function fetchMemoryCrystals(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/memory-crystals`)
  if (!r.ok) throw new Error('Failed to fetch memory crystals')
  return r.json()
}

export async function crystallizeMemory(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/crystallize`, { method: 'POST' })
  if (!r.ok) throw new Error('Failed to crystallize')
  return r.json()
}

export async function fetchNpcMinds(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/npc-minds`)
  if (!r.ok) throw new Error('Failed to fetch NPC minds')
  return r.json()
}

export async function deleteNpcMind(campaignId, npcName) {
  const r = await fetch(`${BASE}/game/${campaignId}/npc-minds/${encodeURIComponent(npcName)}`, {
    method: 'DELETE',
  })
  if (!r.ok) throw new Error('Failed to delete NPC mind')
  return r.json()
}

export async function updateNpcMind(campaignId, npcName, thoughts) {
  const r = await fetch(`${BASE}/game/${campaignId}/npc-minds/${encodeURIComponent(npcName)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thoughts }),
  })
  if (!r.ok) throw new Error('Failed to update NPC mind')
  return r.json()
}

export async function generateContent(campaignId, type, language = 'zh') {
  const r = await fetch(`${BASE}/game/${campaignId}/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, language }),
  })
  if (!r.ok) throw new Error('Generation failed')
  return r.json()
}

export async function timeskip(campaignId, seconds) {
  const r = await fetch(`${BASE}/game/${campaignId}/timeskip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ seconds }),
  })
  if (!r.ok) throw new Error('Timeskip failed')
  return r.json()
}

export async function fetchSetupState(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/setup-state`)
  if (!r.ok) throw new Error('Failed to fetch setup state')
  return r.json()
}

export async function saveSetupAnswers(campaignId, answers) {
  const r = await fetch(`${BASE}/game/${campaignId}/setup-answers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers }),
  })
  if (!r.ok) throw new Error('Failed to save setup answers')
  return r.json()
}

export async function fetchScenarioView(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/scenario-view`)
  if (!r.ok) throw new Error('Failed to fetch scenario view')
  return r.json()
}

export async function updateCampaignSettings(campaignId, { combatEnabled }) {
  const r = await fetch(`${BASE}/game/${campaignId}/settings`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ combat_enabled: combatEnabled }),
  })
  if (!r.ok) throw new Error('Failed to update campaign settings')
  return r.json()
}

export async function previewOpening({
  language,
  tone,
  lore,
  directive,
  setup_questions,
  sample_answers,
}) {
  const r = await fetch(`${BASE}/scenarios/preview-opening`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      language: language || 'zh',
      tone: tone || '',
      lore: lore || '',
      directive: directive || '',
      setup_questions: setup_questions || [],
      sample_answers: sample_answers || {},
    }),
  })
  if (!r.ok) throw new Error('Failed to preview opening')
  return r.json()
}

export async function regenerateOpening(campaignId) {
  const r = await fetch(`${BASE}/game/${campaignId}/regenerate-opening`, {
    method: 'POST',
  })
  if (!r.ok) {
    const err = new Error('Failed to regenerate opening')
    err.status = r.status
    try {
      err.detail = (await r.json()).detail
    } catch { /* optional */ }
    throw err
  }
  return r.json()
}

// ---------- 角色包 / 立绘差分 ----------

async function jsonOrThrow(r, fallback) {
  if (!r.ok) {
    const err = new Error(fallback)
    err.status = r.status
    try {
      const data = await r.json()
      err.detail = data.detail
      if (data.detail) err.message = typeof data.detail === 'string' ? data.detail : fallback
    } catch {
      // 响应不是 JSON 时保留 fallback 文案
    }
    throw err
  }
  return r.json()
}

export async function fetchCharactersByScenario(scenarioId) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/characters`)
  return jsonOrThrow(r, '读取角色包失败')
}

export async function createCharacter(scenarioId, data) {
  const r = await fetch(`${BASE}/scenarios/${scenarioId}/characters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return jsonOrThrow(r, '创建角色失败')
}

export async function updateCharacter(characterId, patch) {
  const r = await fetch(`${BASE}/characters/${characterId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return jsonOrThrow(r, '更新角色失败')
}

export async function deleteCharacter(characterId) {
  const r = await fetch(`${BASE}/characters/${characterId}`, { method: 'DELETE' })
  if (!r.ok) throw new Error('删除角色失败')
  return r.json()
}

export async function addCharacterLook(characterId, look) {
  const r = await fetch(`${BASE}/characters/${characterId}/looks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(look),
  })
  return jsonOrThrow(r, '添加立绘差分失败')
}

export async function updateCharacterLook(lookId, patch) {
  const r = await fetch(`${BASE}/characters/looks/${lookId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return jsonOrThrow(r, '更新立绘失败')
}

export async function deleteCharacterLook(lookId) {
  const r = await fetch(`${BASE}/characters/looks/${lookId}`, { method: 'DELETE' })
  if (!r.ok) throw new Error('删除立绘失败')
  return r.json()
}

export async function uploadCharacterImage(file) {
  const form = new FormData()
  form.append('file', file)
  const r = await fetch(`${BASE}/characters/upload`, { method: 'POST', body: form })
  return jsonOrThrow(r, '上传图片失败')
}

export async function fetchCampaignCharacter(campaignId) {
  const r = await fetch(`${BASE}/campaigns/${campaignId}/character`)
  return jsonOrThrow(r, '读取角色形象失败')
}

export async function setCampaignCharacter(campaignId, picks) {
  const r = await fetch(`${BASE}/campaigns/${campaignId}/character`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(picks),
  })
  return jsonOrThrow(r, '保存角色形象失败')
}

// ---------------------------------------------------------------- knowledge / play mode
const V1 = '/api/v1'

export async function fetchKnowledgeLayers() {
  const r = await fetch(`${V1}/knowledge/layers`)
  return jsonOrThrow(r, '读取知识分层失败')
}

export async function fetchKnowledge(scenarioId, includeDisabled = true) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/knowledge?include_disabled=${includeDisabled}`)
  return jsonOrThrow(r, '读取知识库失败')
}

export async function createKnowledge(scenarioId, data) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/knowledge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return jsonOrThrow(r, '创建知识条目失败')
}

export async function updateKnowledge(entryId, patch) {
  const r = await fetch(`${V1}/knowledge/${entryId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return jsonOrThrow(r, '更新知识条目失败')
}

export async function deleteKnowledge(entryId) {
  const r = await fetch(`${V1}/knowledge/${entryId}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除知识条目失败')
}

export async function fetchPlayConfig(scenarioId) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/play-config`)
  return jsonOrThrow(r, '读取运行模式失败')
}

export async function savePlayConfig(scenarioId, data) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/play-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return jsonOrThrow(r, '保存运行模式失败')
}

export async function validateKnowledge(scenarioId) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/knowledge/validate`, { method: 'POST' })
  return jsonOrThrow(r, '校验知识库失败')
}

export async function previewKnowledgeContext(scenarioId, { mode, maxDepth, storyNode, storyNodes, focus } = {}) {
  const params = new URLSearchParams()
  if (mode) params.set('mode', mode)
  if (maxDepth !== undefined && maxDepth !== null) params.set('max_depth', String(maxDepth))
  if (storyNodes !== undefined) params.set('story_nodes', storyNodes.join(','))
  else if (storyNode) params.set('story_node', storyNode)
  if (focus && focus.length > 0) params.set('focus', focus.join(','))
  const query = params.toString()
  const r = await fetch(`${V1}/scenarios/${scenarioId}/context/preview${query ? `?${query}` : ''}`)
  return jsonOrThrow(r, '预览知识上下文失败')
}

export async function generateKnowledgeDraft(scenarioId, { prompt, count = 6, presetId = '' } = {}) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/generate/knowledge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, count, preset_id: presetId }),
  })
  return jsonOrThrow(r, 'AI 生成知识失败')
}

export async function generateStorylineDraft(scenarioId, { prompt, nodeCount = 6, presetId = '' } = {}) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/generate/storyline`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, node_count: nodeCount, preset_id: presetId }),
  })
  return jsonOrThrow(r, 'AI 生成剧情线失败')
}

export async function generateWorldFromPrompt({ scenarioId = '', prompt, presetId = '' } = {}) {
  const r = await fetch(`${V1}/worlds/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario_id: scenarioId, prompt, preset_id: presetId }),
  })
  return jsonOrThrow(r, 'AI 生成世界失败')
}

export async function fetchModelPresets() {
  const r = await fetch(`${V1}/model-presets`)
  return jsonOrThrow(r, '读取模型预设失败')
}

export async function createModelPreset(data) {
  const r = await fetch(`${V1}/model-presets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return jsonOrThrow(r, '创建模型预设失败')
}

export async function updateModelPreset(presetId, patch) {
  const r = await fetch(`${V1}/model-presets/${presetId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return jsonOrThrow(r, '更新模型预设失败')
}

export async function deleteModelPreset(presetId) {
  const r = await fetch(`${V1}/model-presets/${presetId}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除模型预设失败')
}

export async function fetchActionCatalogue() {
  const r = await fetch(`${V1}/actions`)
  return jsonOrThrow(r, '读取行动目录失败')
}

/** Dry run: the rules decide, nothing is written. */
export async function previewAction(campaignId, { action, targetId = null, params = {} } = {}) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/actions/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, target_id: targetId, params }),
  })
  return jsonOrThrow(r, '预演行动失败')
}

export async function fetchCampaignStory(campaignId) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/story`)
  return jsonOrThrow(r, '读取故事线进度失败')
}
// ---------------------------------------------------------------- 世界工坊 / v1 引擎
// ---------------------------------------------------------------------------
// Rules engine lane (/api/v1). Deterministic, transactional, idempotent: these
// calls never let the model decide an outcome, only describe one.
// ---------------------------------------------------------------------------

const ENGINE_V1 = '/api/v1'

/** A fresh request id: the engine treats a repeated id as a replay, not a retry. */
export function newRequestId() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  } catch { /* optional */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** Which lane should answer this campaign? 'engine' once a world is bound. */
export async function fetchEngineAvailability(campaignId) {
  try {
    const r = await fetch(`${ENGINE_V1}/campaigns/${campaignId}/lane`)
    if (!r.ok) return { bound: false, state: null, error: `HTTP ${r.status}` }
    const lane = await r.json()
    if (!lane.bound) return { bound: false, state: null }
    let state = null
    try {
      state = await fetchEngineState(campaignId)
    } catch { /* optional */ }
    return { bound: true, state, worldId: lane.world_id }
  } catch (err) {
    return { bound: false, state: null, error: String(err) }
  }
}

/** Bind a world to this campaign (the moment a campaign becomes rule-driven). */
export async function bindCampaignToWorld(campaignId, worldId, reset = false) {
  const r = await fetch(`${ENGINE_V1}/campaigns/${campaignId}/world`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ world_id: worldId, reset }),
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function fetchEngineState(campaignId) {
  const r = await fetch(`${ENGINE_V1}/campaigns/${campaignId}/state`)
  if (!r.ok) throw new Error('Failed to fetch engine state')
  return r.json()
}

/**
 * Send one utterance to the rules engine.
 *
 * Returns the full ActionResult (status / code / message / state_version /
 * effects / events / narrative). A non-2xx response is not swallowed: the
 * caller needs to distinguish "rejected by rules" from "request failed".
 */
export async function submitTextAction(campaignId, text, { requestId, narrate = true } = {}) {
  const r = await fetch(`${ENGINE_V1}/campaigns/${campaignId}/text-actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text,
      request_id: requestId || newRequestId(),
      narrate,
    }),
  })
  if (!r.ok) {
    const detail = await r.text().catch(() => '')
    throw new Error(`${r.status} ${detail.slice(0, 300)}`)
  }
  const response = await r.json()
  return response.result
}

/** Structured action (used by UI affordances that already know the target). */
export async function submitAction(campaignId, { action, targetId = null, params = {}, requestId, narrate = true }) {
  const r = await fetch(`${ENGINE_V1}/campaigns/${campaignId}/actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      campaign_id: campaignId,
      request_id: requestId || newRequestId(),
      action,
      target_id: targetId,
      params,
      narrate,
    }),
  })
  if (!r.ok) {
    const detail = await r.text().catch(() => '')
    throw new Error(`${r.status} ${detail.slice(0, 300)}`)
  }
  return r.json()
}

export async function authorScenario({
  prompt,
  scenarioId = '',
  title = '',
  presetId = '',
  language = '中文',
  locationCount = 6,
  save = true,
} = {}) {
  const r = await fetch(`${V1}/scenarios/author`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt,
      scenario_id: scenarioId,
      title,
      preset_id: presetId,
      language,
      location_count: locationCount,
      save,
    }),
  })
  return jsonOrThrow(r, '生成剧本失败')
}
export async function fetchWorlds(scenarioId) {
  const query = scenarioId ? `?scenario_id=${encodeURIComponent(scenarioId)}` : ''
  const r = await fetch(`${V1}/worlds${query}`)
  return jsonOrThrow(r, '读取世界列表失败')
}

export async function fetchWorks() {
  const r = await fetch(`${V1}/works`)
  return jsonOrThrow(r, '读取作品库失败')
}

export async function resolveWork(title, { presetId = '', allowModel = true } = {}) {
  const r = await fetch(`${V1}/works/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, preset_id: presetId, allow_model: allowModel }),
  })
  return jsonOrThrow(r, '解析作品失败')
}

export async function enterWork({
  workTitle,
  characterName,
  scenarioId = '',
  campaignId = '',
  worldId = '',
  presetId = '',
  allowModel = true,
} = {}) {
  const r = await fetch(`${V1}/works/enter`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      work_title: workTitle,
      character_name: characterName,
      scenario_id: scenarioId,
      campaign_id: campaignId,
      world_id: worldId,
      preset_id: presetId,
      allow_model: allowModel,
    }),
  })
  return jsonOrThrow(r, '进入作品世界失败')
}

export async function fetchWorld(worldId) {
  const r = await fetch(`${V1}/worlds/${worldId}`)
  return jsonOrThrow(r, '读取世界定义失败')
}

export async function createWorld(definition) {
  const r = await fetch(`${V1}/worlds`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(definition),
  })
  return jsonOrThrow(r, '创建世界失败')
}

export async function replaceWorld(worldId, definition) {
  const r = await fetch(`${V1}/worlds/${worldId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(definition),
  })
  return jsonOrThrow(r, '保存世界失败')
}

export async function validateWorld(definition) {
  const r = await fetch(`${V1}/worlds/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(definition),
  })
  return jsonOrThrow(r, '校验世界失败')
}

export async function fetchWorldMap(worldId) {
  const r = await fetch(`${V1}/worlds/${worldId}/map`)
  return jsonOrThrow(r, '读取世界地图失败')
}

function worldChild(worldId, kind, id, body) {
  const path = `${V1}/worlds/${worldId}/${kind}${id ? `/${encodeURIComponent(id)}` : ''}`
  return fetch(path, body === undefined ? {} : {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function saveWorldLocation(worldId, locationId, body) {
  return jsonOrThrow(await worldChild(worldId, 'locations', locationId, body), '保存地点失败')
}
export async function deleteWorldLocation(worldId, locationId) {
  const r = await fetch(`${V1}/worlds/${worldId}/locations/${encodeURIComponent(locationId)}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除地点失败')
}
export async function saveWorldItem(worldId, itemId, body) {
  return jsonOrThrow(await worldChild(worldId, 'items', itemId, body), '保存物品失败')
}
export async function deleteWorldItem(worldId, itemId) {
  const r = await fetch(`${V1}/worlds/${worldId}/items/${encodeURIComponent(itemId)}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除物品失败')
}
export async function saveWorldDoor(worldId, doorId, body) {
  return jsonOrThrow(await worldChild(worldId, 'doors', doorId, body), '保存门失败')
}
export async function deleteWorldDoor(worldId, doorId) {
  const r = await fetch(`${V1}/worlds/${worldId}/doors/${encodeURIComponent(doorId)}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除门失败')
}
export async function saveWorldEntity(worldId, entityId, body) {
  return jsonOrThrow(await worldChild(worldId, 'entities', entityId, body), '保存实体失败')
}
export async function deleteWorldEntity(worldId, entityId) {
  const r = await fetch(`${V1}/worlds/${worldId}/entities/${encodeURIComponent(entityId)}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除实体失败')
}
export async function saveWorldEvent(worldId, eventId, body) {
  return jsonOrThrow(await worldChild(worldId, 'events', eventId, body), '保存事件失败')
}
export async function deleteWorldEvent(worldId, eventId) {
  const r = await fetch(`${V1}/worlds/${worldId}/events/${encodeURIComponent(eventId)}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除事件失败')
}
export async function saveWorldQuest(worldId, questId, body) {
  return jsonOrThrow(await worldChild(worldId, 'quests', questId, body), '保存任务失败')
}
export async function deleteWorldQuest(worldId, questId) {
  const r = await fetch(`${V1}/worlds/${worldId}/quests/${encodeURIComponent(questId)}`, { method: 'DELETE' })
  return jsonOrThrow(r, '删除任务失败')
}

export async function saveWorldCustomAction(worldId, actionId, body) {
  return jsonOrThrow(
    await worldChild(worldId, 'custom-actions', actionId, body),
    '保存自定义行动失败'
  )
}
export async function deleteWorldCustomAction(worldId, actionId) {
  const r = await fetch(
    `${V1}/worlds/${worldId}/custom-actions/${encodeURIComponent(actionId)}`,
    { method: 'DELETE' }
  )
  return jsonOrThrow(r, '删除自定义行动失败')
}

export async function fetchActionTypes() {
  const r = await fetch(`${V1}/action-types`)
  return jsonOrThrow(r, '读取行动目录失败')
}

export async function fetchCampaignSnapshots(campaignId, limit = 50) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/snapshots?limit=${limit}`)
  return jsonOrThrow(r, '读取状态快照失败')
}

export async function fetchCampaignStateDiff(campaignId, fromVersion, toVersion) {
  const query = `?from_version=${fromVersion}&to_version=${toVersion}`
  const r = await fetch(`${V1}/campaigns/${campaignId}/state-diff${query}`)
  return jsonOrThrow(r, '对比状态差异失败')
}

export async function rollbackCampaign(campaignId, version, reason = '') {
  const r = await fetch(`${V1}/campaigns/${campaignId}/rollback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version, reason }),
  })
  return jsonOrThrow(r, '回滚状态失败')
}

export async function fetchActionDetail(requestId) {
  const r = await fetch(`${V1}/actions/${encodeURIComponent(requestId)}`)
  return jsonOrThrow(r, '读取行动详情失败')
}
export async function fetchCampaignWorldState(campaignId) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/state`)
  return jsonOrThrow(r, '读取冒险状态失败')
}

export async function bindCampaignWorld(campaignId, worldId, reset = false) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/world`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ world_id: worldId, reset }),
  })
  return jsonOrThrow(r, '绑定世界失败')
}

export async function validateWorldEvent(worldId, eventId) {
  const r = await fetch(`${V1}/worlds/${worldId}/events/${encodeURIComponent(eventId)}/validate`, { method: 'POST' })
  return jsonOrThrow(r, '校验事件失败')
}

export async function simulateWorldEvent(worldId, eventId, campaignId) {
  const query = campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ''
  const r = await fetch(`${V1}/worlds/${worldId}/events/${encodeURIComponent(eventId)}/simulate${query}`, { method: 'POST' })
  return jsonOrThrow(r, '模拟事件失败')
}

export async function fetchCampaignEngineState(campaignId) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/state`)
  return jsonOrThrow(r, '读取权威状态失败')
}

export async function exportCampaign(campaignId) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/export`)
  return jsonOrThrow(r, '导出存档失败')
}

export async function importCampaign(campaignId, state, reset = true, events = []) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state, reset, events }),
  })
  return jsonOrThrow(r, '导入存档失败')
}

export async function runCombatAction(campaignId, { action = 'attack', targetId = null, requestId = null, narrate = true } = {}) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/combat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      action,
      target_id: targetId,
      request_id: requestId || `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      narrate,
    }),
  })
  return jsonOrThrow(r, '战斗行动失败')
}

export async function fetchQuestProgress(campaignId) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/quest-progress`)
  return jsonOrThrow(r, '读取任务进度失败')
}

export async function progressQuestObjective(campaignId, questId, objectiveId, amount = 1, requestId = '') {
  const r = await fetch(
    `${V1}/campaigns/${campaignId}/quests/${encodeURIComponent(questId)}/objectives/${encodeURIComponent(objectiveId)}/progress`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount, request_id: requestId }),
    }
  )
  return jsonOrThrow(r, '推进任务目标失败')
}
export async function fetchEventExecutions(campaignId, { status = '', limit = 100, afterVersion = 0 } = {}) {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  params.set('limit', String(limit))
  if (afterVersion) params.set('after_version', String(afterVersion))
  const r = await fetch(`${V1}/campaigns/${campaignId}/event-executions?${params.toString()}`)
  return jsonOrThrow(r, '读取事件执行历史失败')
}

export async function fetchCampaignDiagnostics(campaignId) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/diagnostics`)
  return jsonOrThrow(r, '读取状态诊断失败')
}

/**
 * Push the content-policy switch to the backend router.
 *
 * Only the policy lives here: provider/model/temperature are applied to the
 * store and forwarded with each request, so sending them would overwrite the
 * live router configuration with whatever the panel happened to show.
 */
export async function updateContentPolicy({ nsfwMode, refusalFallback }) {
  const body = { content_policy: nsfwMode ? 'nsfw' : 'standard' }
  if (refusalFallback != null) body.local_fallback_on_refusal = !!refusalFallback
  const r = await fetch(`${BASE}/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return jsonOrThrow(r, '保存内容策略失败')
}

export async function fetchContentPolicy() {
  const r = await fetch(`${BASE}/settings`)
  return jsonOrThrow(r, '读取内容策略失败')
}

/** Which model answers each agent role (keys are never sent by the API). */
export async function getAgents() {
  const r = await fetch(`${V1}/agents`)
  return jsonOrThrow(r, '读取 Agent 路由失败')
}

/** Recent model calls for one campaign, newest first (telemetry only). */
export async function getAgentCalls(campaignId, { role = '', limit = 50 } = {}) {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  if (role) params.set('role', role)
  const r = await fetch(`${V1}/campaigns/${campaignId}/agent-calls?${params.toString()}`)
  return jsonOrThrow(r, '读取模型调用记录失败')
}
/** Domain events as the store recorded them (newest first). */
export async function fetchDomainEvents(campaignId, { limit = 100, afterVersion = 0 } = {}) {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  if (afterVersion) params.set('after_version', String(afterVersion))
  const r = await fetch(`${V1}/campaigns/${campaignId}/events?${params.toString()}`)
  return jsonOrThrow(r, '读取事件记录失败')
}

/**
 * The narration capture for one stored event row.
 *
 * `narrator.response` and `narrative.failed` both carry a `diagnostics`
 * payload; this normalises the shape so the panel never has to guess.
 */
export function narrativeDiagnosticsOf(row) {
  const payload = row?.payload
  if (!payload || typeof payload !== 'object') return null
  const diag = payload.diagnostics
  if (!diag || typeof diag !== 'object') return null
  return diag
}

/** The turns this campaign has recorded, newest first (index form). */
export async function fetchCampaignRequests(campaignId, limit = 50) {
  const r = await fetch(`${V1}/campaigns/${campaignId}/requests?limit=${limit}`)
  return jsonOrThrow(r, '读取行动记录失败')
}

/** One recorded turn in full, including the refusal code and its effects. */
export async function fetchActionRecord(requestId) {
  const r = await fetch(`${V1}/actions/${encodeURIComponent(requestId)}`)
  return jsonOrThrow(r, '读取行动详情失败')
}

/**
 * Propose knowledge entries from a campaign's committed narration.
 *
 * The backend treats this as a proposal lane: with apply === false nothing is
 * written and the candidates come back for review; with apply === true only
 * the scenario knowledge table is touched (no rule reads it).
 */
export async function extractKnowledge(
  scenarioId,
  { campaignId, apply = false, refine = false, presetId = '', limit = 20, language = 'zh' } = {},
) {
  const r = await fetch(`${V1}/scenarios/${scenarioId}/knowledge/extract`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      campaign_id: campaignId,
      apply,
      refine,
      preset_id: presetId,
      limit,
      language,
    }),
  })
  return jsonOrThrow(r, '提取记忆失败')
}

/**
 * Plan what every candidate action in the current room would do.
 *
 * Read-only: the backend evaluates each candidate with the real rules engine
 * against a throwaway copy of the state and reports why blocked actions fail.
 */
export async function planRoom(worldId, campaignId = '') {
  const qs = campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ''
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/plan${qs}`, { method: 'POST' })
  return jsonOrThrow(r, '规划行动失败')
}

/**
 * Simulate a short chain of actions without writing anything.
 * Stops at the first step the rules reject.
 */
export async function previewActions(worldId, steps, campaignId = '') {
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/preview-actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ campaign_id: campaignId, steps }),
  })
  return jsonOrThrow(r, '预演行动失败')
}

/** Explain why an action is blocked and how the requirement could be met. */
export async function explainAction(worldId, action, target, campaignId = '') {
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/explain`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ campaign_id: campaignId, action, target }),
  })
  return jsonOrThrow(r, '解释行动失败')
}

/**
 * Turn one sentence into staged world edits. Writes nothing; the reply and
 * the proposal come back for review, and only `commitWorldEdits` persists it.
 */
export async function proposeWorldEdits(worldId, message, { history = [], presetId = '', language = '中文' } = {}) {
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/authoring/propose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history, preset_id: presetId, language }),
  })
  return jsonOrThrow(r, '生成世界改动失败')
}

/** Persist a proposal the author approved. World document and knowledge only. */
export async function commitWorldEdits(worldId, proposal) {
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/authoring/commit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proposal }),
  })
  return jsonOrThrow(r, '保存世界改动失败')
}

/**
 * World evolution: what a turn claims about the world, as reviewable proposals.
 * `commit` defaults to false on the server, so proposing writes nothing.
 */
export async function proposeWorldEvolution(worldId, { statements = [], campaignId = '', observer = 'player', source = '', commit = false, autoCommit = null } = {}) {
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/evolution/propose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      statements,
      campaign_id: campaignId,
      observer,
      source,
      commit,
      auto_commit: autoCommit,
    }),
  })
  return jsonOrThrow(r, '生成世界演化提案失败')
}

/** The audit trail: every proposal, its status, and why. Read-only. */
export async function fetchEvolutionProposals(worldId, { status = '', kind = '', limit = 100 } = {}) {
  const q = new URLSearchParams()
  if (status) q.set('status', status)
  if (kind) q.set('kind', kind)
  q.set('limit', String(limit))
  const r = await fetch(`${V1}/worlds/${encodeURIComponent(worldId)}/evolution/proposals?${q}`)
  return jsonOrThrow(r, '读取演化提案失败')
}

/** Commit one pending proposal. Re-validates the world before writing. */
export async function approveEvolutionProposal(worldId, proposalId) {
  const r = await fetch(
    `${V1}/worlds/${encodeURIComponent(worldId)}/evolution/proposals/${encodeURIComponent(proposalId)}/approve`,
    { method: 'POST' }
  )
  return jsonOrThrow(r, '采纳演化提案失败')
}

/** Refuse a proposal and keep the reason next to it. */
export async function rejectEvolutionProposal(worldId, proposalId, reason = '') {
  const q = reason ? `?reason=${encodeURIComponent(reason)}` : ''
  const r = await fetch(
    `${V1}/worlds/${encodeURIComponent(worldId)}/evolution/proposals/${encodeURIComponent(proposalId)}/reject${q}`,
    { method: 'POST' }
  )
  return jsonOrThrow(r, '驳回演化提案失败')
}
