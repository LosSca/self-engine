import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  addCharacterLook,
  createLibraryCharacter,
  deleteCharacter,
  deleteCharacterLook,
  fetchLibraryCharacters,
  importCharacterToLibrary,
  updateCharacter,
  updateCharacterLook,
  uploadCharacterImage,
} from '../api'
import QuickDraft from './QuickDraft'

const inputBase =
  'w-full bg-[#fbfaf7] border border-[#dcd8d0] rounded-xl px-4 py-3 text-[#1a1a1a] placeholder-[#9a958c] focus:outline-none focus:border-[#8a857c] focus:bg-white transition-all text-sm font-light'

const emptyDraft = { name: '', role: '', description: '', is_player: false }

/**
 * 角色库：角色不再隶属于某个世界，在这里创建一次，任何世界都能挑走它。
 *
 * 立绘按「服装 / 表情」拆分，游戏内的角色档案面板只负责挑选与保存到存档，
 * 这里负责增删角色与差分（相当于角色的配置源）。
 */
export default function CharacterLibrary({ open, onClose }) {
  const [characters, setCharacters] = useState(null)
  const [selectedId, setSelectedId] = useState('')
  const [draft, setDraft] = useState(emptyDraft)
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [lookForm, setLookForm] = useState({ outfit: '默认', expression: '平静', image_url: '' })
  const fileRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const list = await fetchLibraryCharacters()
      const arr = Array.isArray(list) ? list : []
      setCharacters(arr)
      setSelectedId((prev) => (arr.some((c) => c.id === prev) ? prev : arr[0]?.id || ''))
    } catch (err) {
      setCharacters([])
      setError(err?.message || '读取角色库失败。')
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setError('')
    load()
  }, [open, load])

  const selected = useMemo(
    () => (characters || []).find((c) => c.id === selectedId) || null,
    [characters, selectedId],
  )

  const flash = (text) => {
    setNotice(text)
    setTimeout(() => setNotice(''), 2200)
  }

  const handleCreate = async () => {
    const name = draft.name.trim()
    if (!name) return
    setBusy(true)
    setError('')
    try {
      const created = await createLibraryCharacter({
        name,
        role: draft.role.trim(),
        description: draft.description.trim(),
        is_player: draft.is_player,
      })
      setDraft(emptyDraft)
      setCreating(false)
      await load()
      setSelectedId(created.id)
      flash('已创建，去加一张立绘吧。')
    } catch (err) {
      setError(err?.message || '创建角色失败。')
    } finally {
      setBusy(false)
    }
  }

  const handlePatch = async (patch) => {
    if (!selected) return
    setBusy(true)
    setError('')
    try {
      await updateCharacter(selected.id, patch)
      await load()
      flash('已保存。')
    } catch (err) {
      setError(err?.message || '保存失败。')
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!selected) return
    if (!window.confirm(`删除角色「${selected.name}」？已绑定这个角色的存档会保留名字但失去立绘。`)) return
    setBusy(true)
    try {
      await deleteCharacter(selected.id)
      await load()
    } catch (err) {
      setError(err?.message || '删除失败。')
    } finally {
      setBusy(false)
    }
  }

  const handleUpload = async (file) => {
    if (!file) return
    setBusy(true)
    setError('')
    const reader = new FileReader()
    reader.onload = () => setLookForm((prev) => ({ ...prev, image_url: String(reader.result || '') }));
    reader.readAsDataURL(file)
    try {
      const uploaded = await uploadCharacterImage(file)
      setLookForm((prev) => ({ ...prev, image_url: uploaded.url || uploaded.image_url || '' }))
    } catch (err) {
      setError(err?.message || '上传图片失败。')
    } finally {
      setBusy(false)
    }
  }

  const handleAddLook = async () => {
    if (!selected) return
    setBusy(true)
    setError('')
    try {
      await addCharacterLook(selected.id, {
        outfit: lookForm.outfit.trim() || '默认',
        expression: lookForm.expression.trim() || '平静',
        image_url: lookForm.image_url,
      })
      setLookForm({ outfit: lookForm.outfit, expression: '平静', image_url: '' })
      await load()
      flash('立绘已添加。')
    } catch (err) {
      setError(err?.message || '添加立绘失败。')
    } finally {
      setBusy(false)
    }
  }

  const handleDeleteLook = async (lookId) => {
    setBusy(true)
    try {
      await deleteCharacterLook(lookId)
      await load()
    } catch (err) {
      setError(err?.message || '删除立绘失败。')
    } finally {
      setBusy(false)
    }
  }

  const handleRenameLook = async (look, patch) => {
    setBusy(true)
    try {
      await updateCharacterLook(look.id, patch)
      await load()
    } catch (err) {
      setError(err?.message || '更新立绘失败。')
    } finally {
      setBusy(false)
    }
  }

  const handleImport = async () => {
    const raw = window.prompt('把某个世界的专属角色收进角色库，输入角色 ID：')
    if (!raw) return
    setBusy(true)
    setError('')
    try {
      const clone = await importCharacterToLibrary(raw.trim())
      await load()
      setSelectedId(clone.id)
      flash('已收录到角色库。')
    } catch (err) {
      setError(err?.message || '收录失败（检查角色 ID 是否正确）。')
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null

  const list = characters || []
  const looks = selected?.looks || []
  const cover = looks.find((l) => l.image_url) || looks[0] || null

  return (
    <div className="character-library fixed inset-0 z-[70] bg-[#f6f5f1] overflow-y-auto overflow-x-hidden">
      {/* 纸白底上一层极淡的暖光，避免整块面板像一张空白 A4；
          只做背景，不参与交互，也不影响子元素的对比度。 */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_80%_at_50%_0%,rgba(255,252,246,0.9),transparent_65%)]"></div>

      <div className="relative max-w-6xl mx-auto px-4 sm:px-8 py-10">
        <div className="flex items-start justify-between mb-8 pb-5 border-b border-[#dcd8d0]">
          <div>
            <p className="text-[10px] uppercase tracking-[0.35em] text-[#8a857c] font-mono mb-2">Character Library</p>
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-[#1a1a1a]">角色库</h1>
            <p className="text-xs sm:text-sm text-[#5c574f] mt-2 leading-relaxed max-w-xl">
              角色独立于世界存在：在这里创建，任意世界开局时都能挑走。
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={handleImport}
              disabled={busy}
              className="px-4 py-2.5 rounded-xl border border-[#dcd8d0] text-[#5c574f] hover:text-[#1a1a1a] hover:border-[#a8a29a] text-[10px] font-bold tracking-[0.15em] uppercase transition-all"
            >
              收录世界角色
            </button>
            <button
              onClick={onClose}
              className="px-5 py-2.5 rounded-xl bg-[#1a1a1a] text-white text-[10px] font-bold tracking-[0.15em] uppercase hover:bg-[#333] transition-all"
            >
              关闭
            </button>
          </div>
        </div>

        {error && (
          <div className="mb-5 p-3 rounded-xl bg-rose-50 border border-rose-200 text-rose-700 text-xs">{error}</div>
        )}
        {notice && (
          <div className="mb-5 p-3 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs">{notice}</div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-6">
          <aside className="space-y-3">
            <button
              onClick={() => setCreating((v) => !v)}
              className="w-full p-4 rounded-2xl border border-dashed border-[#cfcabf] text-[#5c574f] hover:text-[#1a1a1a] hover:border-[#a8a29a] text-xs font-bold tracking-[0.15em] uppercase transition-all"
            >
              {creating ? '取消新建' : '+ 新建角色'}
            </button>

            {creating && (
              <div className="p-4 rounded-2xl border border-[#dcd8d0] bg-white space-y-3 shadow-[0_1px_2px_rgba(24,20,16,0.04)]">
                <input
                  autoFocus
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="名字"
                  className={inputBase}
                />
                <input
                  value={draft.role}
                  onChange={(e) => setDraft({ ...draft, role: e.target.value })}
                  placeholder="身份 / 职业"
                  className={inputBase}
                />
                <textarea
                  value={draft.description}
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                  placeholder="外貌、性格、来历……会进入叙事上下文"
                  rows={3}
                  className={inputBase + ' resize-none'}
                />
                <label className="flex items-center gap-2 text-[11px] text-[#5c574f]">
                  <input
                    type="checkbox"
                    checked={draft.is_player}
                    onChange={(e) => setDraft({ ...draft, is_player: e.target.checked })}
                  />
                  作为玩家角色候选
                </label>
                <QuickDraft
                  kind="character"
                  topic={draft.name}
                  tone="light"
                  label="按名字快速生成设定"
                  testId="draft-new-character"
                  onDraft={(text) => setDraft((d) => ({ ...d, description: text }))}
                />
                <button
                  onClick={handleCreate}
                  disabled={busy || !draft.name.trim()}
                  className="w-full py-2.5 rounded-xl bg-[#1a1a1a] text-white text-[10px] font-bold tracking-[0.2em] uppercase disabled:opacity-30 hover:bg-[#333] transition-all"
                >
                  创建
                </button>
              </div>
            )}

            {characters === null && <p className="text-xs text-[#8a857c] px-1">载入中…</p>}
            {characters !== null && list.length === 0 && (
              <p className="text-xs text-[#5c574f] px-1 leading-relaxed">角色库还是空的。先新建一个，或在开局向导里挑本世界的角色后点「收录世界角色」。</p>
            )}

            {list.map((c) => {
              const lk = (c.looks || []).find((l) => l.image_url) || (c.looks || [])[0] || null;
              const active = c.id === selectedId;
              return (
                <button
                  key={c.id}
                  onClick={() => setSelectedId(c.id)}
                  className={
                    'w-full flex items-center gap-3 p-3 rounded-2xl border text-left transition-all ' +
                    (active
                      ? 'bg-white border-[#1a1a1a]/25 shadow-[0_1px_3px_rgba(24,20,16,0.08)]'
                      : 'bg-white/70 border-[#dcd8d0] hover:border-[#bdb7ad]')
                  }
                >
                  <span className="w-11 h-14 rounded-lg overflow-hidden bg-[#efede8] flex items-center justify-center shrink-0 text-[10px] text-[#a8a29a]">
                    {lk?.image_url ? (
                      <img src={lk.image_url} alt={c.name} className="w-full h-full object-cover" />
                    ) : '无图'}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm text-[#1a1a1a] truncate">{c.name}</span>
                    <span className="block text-[10px] text-[#8a857c] truncate">
                      {c.role || (c.is_player ? '玩家角色' : '角色')} · {(c.looks || []).length} 张
                    </span>
                  </span>
                </button>
              )
            })}
          </aside>

          <section className="rounded-3xl border border-[#dcd8d0] bg-white p-5 sm:p-7 shadow-[0_1px_2px_rgba(24,20,16,0.04),0_18px_40px_-28px_rgba(24,20,16,0.35)]">
            {!selected && (
              <p className="text-xs text-[#5c574f]">左侧选一个角色，或者新建一个。</p>
            )}

            {selected && (
              <div className="space-y-7">
                <div className="flex flex-col sm:flex-row gap-6">
                  <div className="w-40 h-56 rounded-2xl border border-[#dcd8d0] bg-[#efede8] overflow-hidden flex items-center justify-center shrink-0 mx-auto sm:mx-0">
                    {cover?.image_url ? (
                      <img src={cover.image_url} alt={selected.name} className="w-full h-full object-cover" />
                    ) : (
                      <span className="text-[#a8a29a] text-[10px]">还没有立绘</span>
                    )}
                  </div>
                  <div className="flex-1 space-y-3">
                    <NamedField
                      label="名字"
                      value={selected.name}
                      onCommit={(v) => v.trim() && handlePatch({ name: v.trim() })}
                      busy={busy}
                    />
                    <NamedField
                      label="身份"
                      value={selected.role}
                      onCommit={(v) => handlePatch({ role: v })}
                      busy={busy}
                    />
                    <label className="flex items-center gap-2 text-[11px] text-[#5c574f]">
                      <input
                        type="checkbox"
                        checked={!!selected.is_player}
                        onChange={(e) => handlePatch({ is_player: e.target.checked })}
                      />
                      作为玩家角色候选
                    </label>
                    <div>
                      <div className="flex items-center justify-between mb-2">
                        <span className="block text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c]">设定</span>
                        <QuickDraft
                          kind="character"
                          topic={selected.name}
                          context={selected.description || ''}
                          tone="light"
                          label="快速生成"
                          testId="draft-character-description"
                          onDraft={(text) => {
                            setCharacters((prev) => (prev || []).map((c) => (c.id === selected.id ? { ...c, description: text } : c)))
                            handlePatch({ description: text })
                          }}
                        />
                      </div>
                      <textarea
                        value={selected.description || ''}
                        onChange={(e) => setCharacters((prev) => (prev || []).map((c) => (c.id === selected.id ? { ...c, description: e.target.value } : c)))}
                        onBlur={(e) => handlePatch({ description: e.target.value })}
                        rows={4}
                        className={inputBase + ' resize-none'}
                      />
                    </div>
                    <button
                      onClick={handleDelete}
                      disabled={busy}
                      className="text-[10px] tracking-[0.2em] uppercase text-[#a8a29a] hover:text-rose-600 transition-colors"
                    >
                      删除这个角色
                    </button>
                  </div>
                </div>

                <div className="pt-6 border-t border-[#dcd8d0]">
                  <div className="flex items-baseline gap-3 mb-4">
                    <h3 className="text-xs font-bold uppercase tracking-[0.25em] text-[#1a1a1a]">立绘差分</h3>
                    <span className="text-[10px] text-[#8a857c] font-mono">{looks.length} 张</span>
                  </div>

                  {looks.length > 0 && (
                    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3 mb-6">
                      {looks.map((l) => (
                        <div key={l.id} className="rounded-xl border border-[#dcd8d0] bg-white overflow-hidden">
                          <div className="h-32 bg-[#efede8] flex items-center justify-center">
                            {l.image_url ? (
                              <img src={l.image_url} alt={l.expression} className="w-full h-full object-cover" />
                            ) : (
                              <span className="text-[10px] text-[#a8a29a]">无图</span>
                            )}
                          </div>
                          <div className="p-2 space-y-1">
                            <input
                              value={l.outfit}
                              onChange={(e) => setCharacters((prev) => (prev || []).map((c) => (c.id !== selected.id ? c : { ...c, looks: c.looks.map((x) => (x.id === l.id ? { ...x, outfit: e.target.value } : x)) })))}
                              onBlur={(e) => handleRenameLook(l, { outfit: e.target.value })}
                              className="w-full bg-transparent text-[11px] text-[#1a1a1a] focus:outline-none border-b border-transparent focus:border-[#dcd8d0]"
                            />
                            <input
                              value={l.expression}
                              onChange={(e) => setCharacters((prev) => (prev || []).map((c) => (c.id !== selected.id ? c : { ...c, looks: c.looks.map((x) => (x.id === l.id ? { ...x, expression: e.target.value } : x)) })))}
                              onBlur={(e) => handleRenameLook(l, { expression: e.target.value })}
                              className="w-full bg-transparent text-[10px] text-[#8a857c] focus:outline-none border-b border-transparent focus:border-[#dcd8d0]"
                            />
                            <button
                              onClick={() => handleDeleteLook(l.id)}
                              className="text-[9px] tracking-widest uppercase text-[#a8a29a] hover:text-rose-600 transition-colors"
                            >
                              删除
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="rounded-2xl border border-[#dcd8d0] bg-[#fbfaf7] p-4 space-y-3">
                    <div className="grid grid-cols-2 gap-3">
                      <input
                        value={lookForm.outfit}
                        onChange={(e) => setLookForm({ ...lookForm, outfit: e.target.value })}
                        placeholder="服装（如 旅装）"
                        className={inputBase}
                      />
                      <input
                        value={lookForm.expression}
                        onChange={(e) => setLookForm({ ...lookForm, expression: e.target.value })}
                        placeholder="表情（如 平静）"
                        className={inputBase}
                      />
                    </div>
                    <div className="flex items-center gap-3 flex-wrap">
                      <input
                        ref={fileRef}
                        type="file"
                        accept="image/*"
                        onChange={(e) => handleUpload(e.target.files?.[0])}
                        className="hidden"
                      />
                      <button
                        onClick={() => fileRef.current?.click()}
                        disabled={busy}
                        className="px-4 py-2 rounded-xl border border-[#dcd8d0] text-[#5c574f] hover:text-[#1a1a1a] hover:border-[#a8a29a] text-[10px] font-bold tracking-[0.15em] uppercase transition-all"
                      >
                        上传图片
                      </button>
                      {lookForm.image_url && (
                        <span className="w-10 h-10 rounded-lg overflow-hidden bg-[#efede8]">
                          <img src={lookForm.image_url} alt="预览" className="w-full h-full object-cover" />
                        </span>
                      )}
                      <button
                        onClick={handleAddLook}
                        disabled={busy || !lookForm.image_url}
                        className="ml-auto px-5 py-2 rounded-xl bg-[#1a1a1a] text-white text-[10px] font-bold tracking-[0.15em] uppercase disabled:opacity-30 hover:bg-[#333] transition-all"
                      >
                        添加这张立绘
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

function NamedField({ label, value, onCommit, busy }) {
  const [text, setText] = useState(value || '')
  // 外部值变了（切角色、快速生成回填）就重算草稿：用「渲染期比对上一次的 value」
  // 的方式同步，而不是在 effect 里 setState——后者会多跑一轮渲染。
  const [seen, setSeen] = useState(value || '')
  const next = value || ''
  if (next !== seen) {
    setSeen(next)
    setText(next)
  }
  return (
    <label className="block">
      <span className="block text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c] mb-2">{label}</span>
      <input
        value={text}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => { if (text !== value) onCommit(text) }}
        className={inputBase}
      />
    </label>
  )
}