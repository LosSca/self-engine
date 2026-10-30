import { useState, useEffect } from 'react'
import { X, Sword, Shield, FlaskConical, Key, Wrench, Package } from 'lucide-react'
import { fetchInventory, updateInventoryItem } from '../api'

const CATEGORY_ICONS = {
  weapon: Sword,
  armor: Shield,
  consumable: FlaskConical,
  quest: Key,
  tool: Wrench,
  misc: Package,
}

const STATUS_COLORS = {
  carried: 'text-emerald-400',
  used: 'text-white/20',
  lost: 'text-rose-400',
}

export default function InventoryPanel({ open, onClose, campaignId, inventory, setInventory, engineBound = false, onEngineAction, busy = false }) {
  const [loading, setLoading] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || !campaignId) return
    let cancelled = false
    setLoading(true)
    setError('')
    fetchInventory(campaignId)
      .then((items) => { if (!cancelled) setInventory(items) })
      .catch((err) => { if (!cancelled) setError(err.message || '背包读取失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [open, campaignId, engineBound, setInventory])

  if (!open) return null

  const handleAction = async (item, action) => {
    if (busy || pending) return
    setPending(true)
    setError('')
    try {
      if (engineBound) {
        const result = await onEngineAction({ action: 'use', targetId: item.id, label: `使用 ${item.name}` })
        if (!result) setError('请求未成功返回，请刷新确认当前背包。')
        else if (!result.ok) setError(result.message || '使用物品被规则拒绝。')
      } else {
        await updateInventoryItem(campaignId, item.name, action)
      }
      const updated = await fetchInventory(campaignId)
      setInventory(updated)
    } catch (err) {
      setError(err.message || '物品操作失败')
    } finally {
      setPending(false)
    }
  }

  const carriedItems = inventory.filter((i) => i.status === 'carried')
  const expiredItems = inventory.filter((i) => i.status !== 'carried')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="bg-white/[0.03] border border-white/10 rounded-2xl shadow-[0_0_40px_rgba(255,255,255,0.15)] w-full max-w-lg mx-2 sm:mx-4 max-h-[85vh] sm:max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
          <h2 className="text-white font-bold text-base tracking-wide">背包</h2>
          <button onClick={onClose} aria-label="关闭背包" className="text-white/40 hover:text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/50">
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-2">
          {engineBound && <p className="text-white/40 text-xs">物品来自权威存档，使用效果由规则执行；当前没有丢弃动作。</p>}
          {error && <p role="alert" className="text-rose-300 text-xs">{error}</p>}
          {loading && <p className="text-white/40 text-sm text-center py-8">加载中……</p>}
          {!loading && inventory.length === 0 && (
            <p className="text-white/20 text-sm text-center py-8">暂无物品。</p>
          )}

          {carriedItems.length > 0 && (
            <>
              <p className="text-white/40 text-[10px] font-bold uppercase tracking-widest font-mono px-2">携带中</p>
              {carriedItems.map((item) => {
                const Icon = CATEGORY_ICONS[item.category] || Package
                return (
                  <div key={item.id || item.name} className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-white/5 hover:bg-white/10 transition-colors">
                    <Icon size={16} className="text-white/60 flex-none" />
                    <div className="flex-1 min-w-0">
                      <p className="text-white text-sm font-medium truncate">{item.name}{item.quantity != null && ` × ${item.quantity}`}</p>
                      <p className="text-white/40 text-xs truncate">{item.source}</p>
                    </div>
                    <div className="flex gap-1">
                      <button
                        onClick={() => handleAction(item, 'use')}
                        disabled={loading || pending || busy}
                        aria-label={`使用 ${item.name}`}
                        className="px-2 py-1 text-[10px] uppercase tracking-wider bg-amber-500/10 text-amber-400 rounded hover:bg-amber-500/20 transition-colors disabled:opacity-40"
                      >
                        使用
                      </button>
                      {!engineBound && (
                        <button
                          onClick={() => handleAction(item, 'discard')}
                          disabled={loading || pending || busy}
                          className="px-2 py-1 text-[10px] uppercase tracking-wider bg-rose-500/10 text-rose-400 rounded hover:bg-rose-500/20 transition-colors disabled:opacity-40"
                        >
                          丢弃
                        </button>
                      )}
                    </div>
                  </div>
                )
              })}
            </>
          )}

          {expiredItems.length > 0 && (
            <>
              <p className="text-white/40 text-[10px] font-bold uppercase tracking-widest font-mono px-2 mt-4">已使用 / 已丢失</p>
              {expiredItems.map((item) => {
                const Icon = CATEGORY_ICONS[item.category] || Package
                return (
                  <div key={item.name} className="flex items-center gap-3 px-3 py-2 rounded-lg bg-white/[0.03] opacity-50">
                    <Icon size={16} className="text-white/20 flex-none" />
                    <div className="flex-1 min-w-0">
                      <p className="text-white/60 text-sm truncate">{item.name}</p>
                      <p className="text-white/20 text-xs">{item.status}</p>
                    </div>
                  </div>
                )
              })}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
