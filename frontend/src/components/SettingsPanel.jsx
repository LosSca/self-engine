import { useState } from 'react'
import { useGameStore } from '../store'
import { updateCampaignSettings, updateContentPolicy } from '../api'
import { Settings, X } from 'lucide-react'
import { THEME_LABELS, applyTheme, currentTheme } from '../lib/theme'

const PROVIDERS = [
  { id: 'deepseek', label: 'DeepSeek', models: ['deepseek-v4-flash'] },
  { id: 'anthropic', label: 'Anthropic', models: ['claude-opus-5', 'claude-sonnet-5'] },
  { id: 'openai', label: 'OpenAI', models: ['gpt-5.6-sol'] },
  // Local OpenAI-compatible server (NInfer). The alias must match the server's
  // --model-id and .env LOCAL_LLM_MODEL; start it with E:\ninfer\start-local.ps1.
  { id: 'local', label: '本地 NInfer', models: ['qwen3.8-27b'] },
]

export default function SettingsPanel({ onClose }) {
  const {
    llmProvider, llmModel, temperature, maxTokens, combatEnabled,
    nsfwMode, refusalFallback, activeCampaignId, updateSettings,
  } = useGameStore()
  const [provider, setProvider] = useState(llmProvider)
  const [model, setModel] = useState(llmModel)
  const [temp, setTemp] = useState(temperature)
  const [tokens, setTokens] = useState(maxTokens)
  const [combat, setCombat] = useState(combatEnabled)
  const [nsfw, setNsfw] = useState(!!nsfwMode)
  const [refusalFallbackOn, setRefusalFallbackOn] = useState(refusalFallback !== false)
  const [saved, setSaved] = useState(false)
  const [theme, setTheme] = useState(currentTheme())
  const [apiToken, setApiToken] = useState(
    () => localStorage.getItem('lunar_api_token') || '',
  )

  const currentProviderModels = PROVIDERS.find((p) => p.id === provider)?.models || []

  const handleProviderChange = (newProvider) => {
    setProvider(newProvider)
    const providerModels = PROVIDERS.find((p) => p.id === newProvider)?.models || []
    setModel(providerModels[0] || '')
  }

  const handleSave = async () => {
    const token = apiToken.trim()
    if (token) localStorage.setItem('lunar_api_token', token)
    else localStorage.removeItem('lunar_api_token')
    updateSettings({
      llmProvider: provider,
      llmModel: model,
      temperature: temp,
      maxTokens: tokens,
      combatEnabled: combat,
      nsfwMode: nsfw,
      refusalFallback: refusalFallbackOn,
    })
    try {
      // The switch has to reach the backend router: it decides where a narrated
      // turn is sent, and which model picks it up after a refusal.
      await updateContentPolicy({ nsfwMode: nsfw, refusalFallback: refusalFallbackOn })
    } catch {
      // Kept locally regardless; the backend keeps its previous policy.
    }
    if (activeCampaignId) {
      try {
        await updateCampaignSettings(activeCampaignId, { combatEnabled: combat })
      } catch {
        // Persistence failure is non-fatal: the per-request flag still
        // applies for the current session.
      }
    }
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="bg-white/[0.03] backdrop-blur-xl border border-white/5 rounded-[2rem] w-full max-w-md mx-4 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
          <div className="flex items-center gap-3">
            <Settings size={16} className="text-white" />
            <h2 className="text-white font-bold text-sm uppercase tracking-widest">设置</h2>
          </div>
          <button onClick={onClose} className="text-white/40 hover:text-white transition-colors">
            <X size={18} />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* 界面主题：立即生效并写入 localStorage */}
          <div>
            <label className="block text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 mb-2">
              界面主题
            </label>
            <div className="grid grid-cols-2 gap-2">
              {['light', 'dark'].map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setTheme(applyTheme(t))}
                  className={`px-3 py-2.5 rounded-lg border text-xs font-semibold tracking-wide transition-all ${
                    theme === t
                      ? 'bg-white text-black border-white'
                      : 'bg-white/[0.03] text-white/60 border-white/10 hover:border-white/30'
                  }`}
                >
                  {THEME_LABELS[t]}（{t === 'light' ? '默认' : '夜间'}）
                </button>
              ))}
            </div>
            <p className="text-[10px] text-white/40 leading-relaxed mt-2">
              纸白主题为默认配色；切换会立刻生效，并在下次打开时记住。
            </p>
          </div>

          {/* Provider */}
          <div>
            <label className="block text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 mb-2">
              模型服务商
            </label>
            <select
              value={provider}
              onChange={(e) => handleProviderChange(e.target.value)}
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-2.5 text-sm text-white focus:outline-none focus:border-white/40 focus:bg-white/[0.05] transition-all"
            >
              {PROVIDERS.map((p) => (
                <option key={p.id} value={p.id} className="bg-[#1a1a1a] text-white">{p.label}</option>
              ))}
            </select>
          </div>

          {/* Model */}
          <div>
            <label className="block text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 mb-2">
              模型
            </label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-2.5 text-sm text-white focus:outline-none focus:border-white/40 focus:bg-white/[0.05] transition-all"
            >
              {currentProviderModels.map((m) => (
                <option key={m} value={m} className="bg-[#1a1a1a] text-white">{m}</option>
              ))}
            </select>
            {provider === 'local' && (
              <p className="text-[10px] text-white/40 leading-relaxed mt-2">
                需要本地 NInfer 服务：127.0.0.1:8094（运行 start-local.ps1）。
                地址与密钥来自 .env：LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY。
              </p>
            )}
          </div>

          {/* Temperature */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-[10px] font-bold uppercase tracking-widest font-mono text-white/40">
                创造性（温度）
              </label>
              <span className="text-xs text-white font-mono">{temp.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min={0}
              max={2}
              step={0.05}
              value={temp}
              onChange={(e) => setTemp(parseFloat(e.target.value))}
              className="w-full accent-white h-1.5 bg-white/[0.03] rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white/10 [&::-webkit-slider-thumb]:shadow-[0_0_8px_rgba(99,102,241,0.6)]"
            />
            <div className="flex justify-between text-[9px] text-white/20 mt-1">
              <span>精准</span>
              <span>均衡</span>
              <span>天马行空</span>
            </div>
          </div>

          {/* 最大 Token 数 */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-[10px] font-bold uppercase tracking-widest font-mono text-white/40">
                最大 Token 数
              </label>
              <span className="text-xs text-white font-mono">{tokens}</span>
            </div>
            <input
              type="range"
              min={256}
              max={8192}
              step={256}
              value={tokens}
              onChange={(e) => setTokens(parseInt(e.target.value))}
              className="w-full accent-white h-1.5 bg-white/[0.03] rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white/10 [&::-webkit-slider-thumb]:shadow-[0_0_8px_rgba(99,102,241,0.6)]"
            />
            <div className="flex justify-between text-[9px] text-white/20 mt-1">
              <span>简短</span>
              <span>标准</span>
              <span>扩展</span>
            </div>
          </div>

          {/* 内容策略 / NSFW 路由 */}
          <div>
            <label className="block text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 mb-2">
              内容策略
            </label>
            <div className="grid grid-cols-2 gap-2">
              {[{ id: false, label: '标准' }, { id: true, label: 'NSFW（本地）' }].map((mode) => (
                <button
                  key={String(mode.id)}
                  type="button"
                  onClick={() => setNsfw(mode.id)}
                  className={`px-3 py-2.5 rounded-lg border text-xs font-semibold tracking-wide transition-all ${
                    nsfw === mode.id
                      ? 'bg-white text-black border-white'
                      : 'bg-white/[0.03] text-white/60 border-white/10 hover:border-white/30'
                  }`}
                >
                  {mode.label}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 mt-3 text-[11px] text-white/60">
              <input
                type="checkbox"
                checked={refusalFallbackOn}
                onChange={(e) => setRefusalFallbackOn(e.target.checked)}
                className="accent-white"
              />
              上游拒绝时自动改走本地模型重试一次
            </label>
            <p className="text-[10px] text-white/40 leading-relaxed mt-2">
              NSFW 模式把叙事请求直接发给本地模型（NInfer），内容不出本机；
              本地不可用时会回落到当前服务商。
            </p>
          </div>

          {/* Optional API token */}
          <div>
            <label className="block text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 mb-2">
              API 访问令牌（可选）
            </label>
            <input
              type="password"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder="后端未启用 API_TOKEN 时留空"
              autoComplete="off"
              className="w-full bg-white/[0.03] border border-white/10 rounded-lg px-4 py-2.5 text-sm text-white placeholder:text-white/20 focus:outline-none focus:border-white/40 focus:bg-white/[0.05] transition-all"
            />
            <p className="text-[10px] text-white/40 leading-relaxed mt-2">
              仅当后端以 API_TOKEN 启动时填写；令牌只保存在本机浏览器，不会上传。
            </p>
          </div>

          {/* 战斗模式 */}
          <div>
            <div className="flex items-center justify-between gap-3">
              <div>
                <label className="block text-[10px] font-bold uppercase tracking-widest font-mono text-white/40 mb-1">
                  战斗模式
                </label>
                <p className="text-[10px] text-white/40 leading-relaxed">
                  无对抗场景可关闭，将跳过战斗相关的模型调用。
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={combat}
                onClick={() => setCombat((v) => !v)}
                className={`relative inline-flex shrink-0 items-center w-11 h-6 rounded-full border transition-colors ${
                  combat
                    ? 'bg-white/20 border-white/40'
                    : 'bg-white/[0.03] border-white/10'
                }`}
              >
                <span
                  className={`inline-block w-4 h-4 rounded-full bg-white transition-transform ${
                    combat ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>
          </div>

          {/* Save */}
          <button
            onClick={handleSave}
            className="w-full bg-white text-black hover:bg-gray-200 uppercase text-sm tracking-[0.2em] font-bold rounded-full px-6 py-3 rounded-lg font-semibold tracking-wide border border-white/20"
          >
            {saved ? '已保存！' : '应用设置'}
          </button>
        </div>
      </div>
    </div>
  )
}
