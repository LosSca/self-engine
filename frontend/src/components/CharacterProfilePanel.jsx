import { useEffect, useMemo, useState } from 'react'
import {
  fetchCampaignCharacter,
  fetchCharactersByScenario,
  setCampaignCharacter,
} from '../api'

/** 角色档案：查看角色立绘差分，切换服装与表情并保存到当前存档。 */
export default function CharacterProfilePanel({ open, onClose, campaignId }) {
  const [characters, setCharacters] = useState([])
  const [characterId, setCharacterId] = useState('')
  const [lookId, setLookId] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || !campaignId) return
    let cancelled = false
    setLoading(true)
    setError('')
    ;(async () => {
      try {
        const view = await fetchCampaignCharacter(campaignId)
        const picks = view?.picks || {}
        const scenarioId = view?.character?.scenario_id
        let list = []
        if (scenarioId) {
          list = await fetchCharactersByScenario(scenarioId)
        } else {
          list = view?.character ? [view.character] : []
        }
        if (cancelled) return
        setCharacters(Array.isArray(list) ? list : [])
        const initial = list.find((c) => c.id === picks.character_id)
          || list.find((c) => c.is_player)
          || list[0]
        setCharacterId(initial?.id || '')
        const look = initial?.looks?.find((l) => l.id === picks.look_id)
          || initial?.looks?.find((l) => l.image_url)
          || initial?.looks?.[0]
        setLookId(look?.id || '')
      } catch {
        if (!cancelled) setError('读取角色形象失败。')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [open, campaignId])

  const character = useMemo(
    () => characters.find((c) => c.id === characterId) || null,
    [characters, characterId],
  )
  const looks = character?.looks || []
  const selectedLook = looks.find((l) => l.id === lookId) || null
  const outfits = Array.from(new Set(looks.map((l) => l.outfit)))

  const handleSave = async () => {
    if (!character || !selectedLook) return
    setSaving(true)
    setError('')
    try {
      await setCampaignCharacter(campaignId, {
        character_id: character.id,
        look_id: selectedLook.id,
        outfit: selectedLook.outfit,
        expression: selectedLook.expression,
        name: character.name,
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch {
      setError('保存失败，请重试。')
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
      <div className="w-full max-w-2xl max-h-[85vh] overflow-y-auto rounded-3xl border border-white/10 bg-neutral-950/95 shadow-2xl">
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/10">
          <div>
            <h2 className="text-sm font-bold tracking-widest uppercase text-white/80">角色档案</h2>
            <p className="text-[10px] text-white/30 mt-1">切换服装与表情，立绘会随剧情插图更新</p>
          </div>
          <button
            onClick={onClose}
            className="text-white/40 hover:text-white text-xs tracking-widest"
          >
            关闭
          </button>
        </div>

        <div className="p-6">
          {loading && <p className="text-xs text-white/30">载入中…</p>}
          {error && (
            <p className="text-xs text-rose-300 mb-4">{error}</p>
          )}
          {!loading && characters.length === 0 && (
            <p className="text-xs text-white/30">这个情景还没有配置角色包。</p>
          )}

          {!loading && characters.length > 0 && character && (
            <div className="flex flex-col sm:flex-row gap-6">
              <div className="w-44 h-64 shrink-0 mx-auto sm:mx-0 rounded-2xl border border-white/10 bg-white/[0.03] overflow-hidden flex items-center justify-center">
                {selectedLook?.image_url ? (
                  <img
                    src={selectedLook.image_url}
                    alt={character.name}
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <span className="text-white/20 text-xs">暂无立绘</span>
                )}
              </div>

              <div className="flex-1">
                <div className="mb-4">
                  <div className="text-lg font-semibold text-white">{character.name}</div>
                  <div className="text-xs text-white/40 mt-1">{character.role || '角色'}</div>
                  {character.description && (
                    <p className="text-xs text-white/45 mt-3 leading-relaxed">{character.description}</p>
                  )}
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-5">
                  <div>
                    <label className="block text-[10px] font-bold uppercase tracking-[0.2em] text-white/40 mb-2">
                      服装
                    </label>
                    <select
                      value={selectedLook?.outfit || ''}
                      onChange={(e) => {
                        const match = looks.find((l) => l.outfit === e.target.value)
                        if (match) setLookId(match.id)
                      }}
                      className="w-full bg-black/40 border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white focus:outline-none focus:border-white/30"
                    >
                      {outfits.map((outfit) => (
                        <option key={outfit} value={outfit} className="bg-neutral-900">
                          {outfit}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-[10px] font-bold uppercase tracking-[0.2em] text-white/40 mb-2">
                      表情
                    </label>
                    <select
                      value={selectedLook?.id || ''}
                      onChange={(e) => setLookId(e.target.value)}
                      className="w-full bg-black/40 border border-white/10 rounded-xl px-3 py-2.5 text-sm text-white focus:outline-none focus:border-white/30"
                    >
                      {looks
                        .filter((l) => !selectedLook || l.outfit === selectedLook.outfit)
                        .map((look) => (
                          <option key={look.id} value={look.id} className="bg-neutral-900">
                            {look.expression || '默认'}
                          </option>
                        ))}
                    </select>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <button
                    onClick={handleSave}
                    disabled={saving || !selectedLook}
                    className="px-6 py-2.5 rounded-xl bg-white text-black text-xs font-bold tracking-widest uppercase hover:bg-gray-200 disabled:opacity-40 transition-all"
                  >
                    {saving ? '保存中…' : '保存形象'}
                  </button>
                  {saved && <span className="text-[10px] text-emerald-300">已保存</span>}
                </div>
              </div>
            </div>
          )}

          {!loading && characters.length > 1 && (
            <div className="mt-6 pt-4 border-t border-white/10">
              <div className="text-[10px] uppercase tracking-[0.2em] text-white/30 mb-3">其他角色</div>
              <div className="flex flex-wrap gap-2">
                {characters
                  .filter((c) => c.id !== characterId)
                  .map((c) => (
                    <button
                      key={c.id}
                      onClick={() => {
                        setCharacterId(c.id)
                        const look = c.looks?.find((l) => l.image_url) || c.looks?.[0]
                        setLookId(look?.id || '')
                      }}
                      className="px-3 py-1.5 rounded-lg border border-white/10 text-xs text-white/50 hover:text-white hover:border-white/30 transition-all"
                    >
                      {c.name}
                    </button>
                  ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
