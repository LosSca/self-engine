import { useEffect, useState } from 'react'
import { useGameStore } from '../store'
import {
  updateCampaignSettings, updateContentPolicy, updateModelRoute, fetchContentPolicy,
  fetchPrefs, savePrefs, fetchPlayConfig, savePlayConfig,
} from '../api'
import { Settings, X, PenLine, Save } from 'lucide-react'
import { THEME_LABELS, applyTheme, currentTheme } from '../lib/theme'

const PROVIDERS = [
  { id: 'deepseek', label: 'DeepSeek', models: ['deepseek-v4-flash'] },
  { id: 'anthropic', label: 'Anthropic', models: ['claude-opus-5', 'claude-sonnet-5'] },
  { id: 'openai', label: 'OpenAI', models: ['gpt-5.6-sol'] },
  // Local OpenAI-compatible server (NInfer). The alias must match the server's
  // --model-id and .env LOCAL_LLM_MODEL; start it with E:\ninfer\start-local.ps1.
  { id: 'local', label: '本地 NInfer', models: ['qwen3.8-27b'] },
  // A custom OpenAI-compatible vendor. Listed only when the backend reports a
  // configured base URL + key (.env CUSTOM_LLM_*), because there is nothing the
  // panel could fill in to make it work.
  { id: 'custom', label: '自定义兼容接口', models: [] },
]

const TABS = [
  { id: 'general', label: '通用' },
  { id: 'model', label: '模型' },
  { id: 'narrative', label: '叙事' },
  { id: 'writing', label: '写作指令' },
]

// 叙事风格开关：都只是给模型的语气提示，不改变规则判定。
const STYLE_OPTIONS = [
  { id: '', label: '跟随世界' },
  { id: 'concise', label: '简洁' },
  { id: 'balanced', label: '均衡' },
  { id: 'literary', label: '文学' },
  { id: 'gothic', label: '阴郁' },
  { id: 'wuxia', label: '武侠' },
  { id: 'pulp', label: '通俗冒险' },
]
const PERSPECTIVE_OPTIONS = [
  { id: '', label: '跟随世界' },
  { id: 'second', label: '第二人称' },
  { id: 'third', label: '第三人称' },
  { id: 'first', label: '第一人称' },
]
const LENGTH_OPTIONS = [
  { id: '', label: '跟随世界' },
  { id: 'short', label: '简短' },
  { id: 'medium', label: '标准' },
  { id: 'long', label: '铺陈' },
]

// 面板统一走纸白配色：旧的「白字」类在浅色主题下会被 light.css 反向映射成墨底，
// 这里直接写死纸面颜色，保证无论主题如何切换、从哪个入口打开都清晰可读。
const C = {
  panel: 'bg-[#f6f5f1] text-[#1a1a1a] border-[#e2ded6]',
  card: 'bg-white border-[#e2ded6]',
  label: 'block text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c] mb-2',
  input:
    'w-full bg-[#fbfaf7] border border-[#d8d4cc] rounded-lg px-3 py-2.5 text-sm text-[#1a1a1a] '
    + 'placeholder-[#9a958c] focus:outline-none focus:border-[#8a857c] transition-colors',
  help: 'text-[10px] leading-relaxed text-[#8a857c] mt-2',
  primary:
    'bg-[#4a453f] text-white hover:bg-[#5c564e] rounded-lg px-5 py-2.5 text-xs font-bold '
    + 'tracking-[0.15em] transition-colors disabled:opacity-50',
  ghost:
    'border border-[#d8d4cc] text-[#5c574f] hover:border-[#8a857c] hover:text-[#1a1a1a] '
    + 'rounded-lg px-4 py-2 text-xs transition-colors',
  option: (on) => (
    'px-3 py-2.5 rounded-lg border text-xs font-semibold tracking-wide transition-colors '
    + (on
      ? 'bg-[#4a453f] text-white border-[#4a453f]'
      : 'bg-white text-[#5c574f] border-[#d8d4cc] hover:border-[#8a857c]')
  ),
}

const numOrNull = (raw, min, max) => {
  const value = Number.parseFloat(raw)
  if (!Number.isFinite(value)) return undefined
  return Math.min(max, Math.max(min, value))
}

/**
 * 设置面板。两个用途共用一个组件：
 * - 游戏内（sidebar / tool dock）：可以额外保存「战斗模式」，因为那是战役级开关；
 * - 首页 / 新建世界 / 角色库：只有全局设置，不显示战役开关。
 */
export default function SettingsPanel({ onClose, activeScenarioId }) {
  const {
    llmProvider, llmModel, temperature, maxTokens, combatEnabled,
    nsfwMode, refusalFallback, activeCampaignId, updateSettings,
  } = useGameStore()
  const [tab, setTab] = useState('general')
  const [provider, setProvider] = useState(llmProvider)
  const [model, setModel] = useState(llmModel)
  const [temp, setTemp] = useState(temperature)
  const [tokens, setTokens] = useState(maxTokens)
  const [combat, setCombat] = useState(combatEnabled)
  const [nsfw, setNsfw] = useState(!!nsfwMode)
  const [refusalFallbackOn, setRefusalFallbackOn] = useState(refusalFallback !== false)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [customModel, setCustomModel] = useState(llmModel || '')
  const [customReady, setCustomReady] = useState(false)
  const [modelError, setModelError] = useState('')
  const [theme, setTheme] = useState(currentTheme())
  const [apiToken, setApiToken] = useState(
    () => localStorage.getItem('lunar_api_token') || '',
  )
  // 叙事可调项：风格/人称/长度/两个开关/采样参数。空串代表「跟随世界」。
  const [narrStyle, setNarrStyle] = useState('')
  const [narrPerspective, setNarrPerspective] = useState('')
  const [narrLength, setNarrLength] = useState('')
  const [narrHint, setNarrHint] = useState('')
  const [showRules, setShowRules] = useState(false)
  const [allowOoc, setAllowOoc] = useState(false)
  const [topP, setTopP] = useState('')
  const [presence, setPresence] = useState('')
  const [frequency, setFrequency] = useState('')
  // 写作指令：全局一份（所有世界都吃），世界自己的一行覆盖全局。
  const [directive, setDirective] = useState('')
  const [worldDirective, setWorldDirective] = useState('')
  const [directiveNotice, setDirectiveNotice] = useState('')

  const availableProviders = customReady
    ? PROVIDERS
    : PROVIDERS.filter((p) => p.id !== 'custom')
  const currentProviderModels = PROVIDERS.find((p) => p.id === provider)?.models || []

  const handleProviderChange = (newProvider) => {
    setProvider(newProvider)
    const providerModels = PROVIDERS.find((p) => p.id === newProvider)?.models || []
    if (newProvider === 'custom') setModel(customModel)
    else setModel(providerModels[0] || '')
  }

  useEffect(() => {
    let alive = true
    fetchContentPolicy()
      .then((data) => {
        if (!alive) return
        setCustomReady(!!data?.custom_route_available)
        if (data?.custom_route_available) {
          if (data.custom_model) setCustomModel(data.custom_model)
          const current = useGameStore.getState().llmProvider
          if (current === 'openai' && data.custom_model) {
            setProvider('custom')
            setModel(data.custom_model)
          }
        }
      })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  // 后端保存的设置与写作指令：换浏览器之后仍然读得到。
  useEffect(() => {
    let alive = true
    fetchPrefs()
      .then((data) => {
        if (!alive) return
        const prefs = data?.prefs || {}
        if (typeof prefs.default_narrator_prompt === 'string') {
          setDirective(prefs.default_narrator_prompt)
        }
        if (prefs.theme) {
          setTheme(applyTheme(prefs.theme))
        }
        if (typeof prefs.narrator_style === 'string') setNarrStyle(prefs.narrator_style)
        if (typeof prefs.narrator_perspective === 'string') setNarrPerspective(prefs.narrator_perspective)
        if (typeof prefs.narrator_length === 'string') setNarrLength(prefs.narrator_length)
        if (typeof prefs.narrator_language_hint === 'string') setNarrHint(prefs.narrator_language_hint)
        setShowRules(!!prefs.narrator_show_rules)
        setAllowOoc(!!prefs.narrator_allow_ooc)
        if (prefs.top_p != null) setTopP(String(prefs.top_p))
        if (prefs.presence_penalty != null) setPresence(String(prefs.presence_penalty))
        if (prefs.frequency_penalty != null) setFrequency(String(prefs.frequency_penalty))
      })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!activeScenarioId) return
    let alive = true
    fetchPlayConfig(activeScenarioId)
      .then((cfg) => { if (alive) setWorldDirective(cfg?.narrator_prompt || '') })
      .catch(() => {})
    return () => { alive = false }
  }, [activeScenarioId])

  const handleSave = async () => {
    const token = apiToken.trim()
    if (token) localStorage.setItem('lunar_api_token', token)
    else localStorage.removeItem('lunar_api_token')
    setBusy(true)
    setModelError('')
    const prefs = {
      provider,
      model: provider === 'custom' ? customModel.trim() : model,
      temperature: temp,
      max_tokens: tokens,
      content_policy: nsfw ? 'nsfw' : 'standard',
      nsfw_mode: nsfw,
      local_fallback_on_refusal: refusalFallbackOn,
      combat_enabled: combat,
      theme,
      custom_model: provider === 'custom' ? customModel.trim() : undefined,
      default_narrator_prompt: directive,
      // 空串表示「跟随世界」：后端看到空值就不加任何提示词。
      narrator_style: narrStyle,
      narrator_perspective: narrPerspective,
      narrator_length: narrLength,
      narrator_language_hint: narrHint,
      narrator_show_rules: showRules,
      narrator_allow_ooc: allowOoc,
      top_p: topP === '' ? undefined : numOrNull(topP, 0, 1),
      presence_penalty: presence === '' ? undefined : numOrNull(presence, -2, 2),
      frequency_penalty: frequency === '' ? undefined : numOrNull(frequency, -2, 2),
    }
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
      // 先落后端：内容策略要让路由立刻按新策略发请求。
      await updateContentPolicy({ nsfwMode: nsfw, refusalFallback: refusalFallbackOn })
    } catch {
      // Kept locally regardless; the backend keeps its previous policy.
    }
    try {
      // provider=openai is how the router reaches a custom compatible endpoint;
      // it decides which base URL and key to use from .env.
      await updateModelRoute({
        provider: provider === 'custom' ? 'openai' : provider,
        model: provider === 'custom' ? customModel.trim() : model,
        temperature: temp,
        maxTokens: tokens,
      })
    } catch (err) {
      setModelError(err?.message || '保存模型设置失败')
    }
    try {
      await savePrefs(prefs)
    } catch {
      // 后端不可用时设置仍留在本机 localStorage，不会丢。
    }
    if (activeCampaignId) {
      try {
        await updateCampaignSettings(activeCampaignId, { combatEnabled: combat })
      } catch {
        // Persistence failure is non-fatal: the per-request flag still
        // applies for the current session.
      }
    }
    if (activeScenarioId) {
      try {
        // 只提交写作指令：后端会沿用未传字段，免得把用户没动过的深度/预算一起改写。
        await savePlayConfig(activeScenarioId, { narrator_prompt: worldDirective })
        setDirectiveNotice('世界专属指令已保存')
      } catch {
        setDirectiveNotice('世界专属指令保存失败（不影响全局指令）')
      }
    }
    setBusy(false)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4">
      <div
        data-testid="settings-panel"
        className={`w-full max-w-2xl max-h-[88vh] flex flex-col rounded-2xl border shadow-[0_24px_60px_-30px_rgba(24,20,16,0.5)] ${C.panel}`}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[#e2ded6]">
          <div className="flex items-center gap-3">
            <Settings size={16} />
            <h2 className="text-sm font-bold tracking-[0.18em] uppercase">设置</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭设置" data-testid="settings-close" className="text-[#8a857c] hover:text-[#1a1a1a] transition-colors">
            <X size={18} />
          </button>
        </div>

        <div className="flex gap-1 px-6 pt-3">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              data-testid={`settings-tab-${t.id}`}
              className={`px-3 py-2 text-xs rounded-lg transition-colors ${
                tab === t.id ? 'bg-[#e8e5de] text-[#1a1a1a] font-semibold' : 'text-[#8a857c] hover:text-[#1a1a1a]'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-6">
          {tab === 'general' && (
            <>
              <div>
                <span className={C.label}>界面主题</span>
                <div className="grid grid-cols-2 gap-2">
                  {['light', 'dark'].map((t) => (
                    <button
                      key={t}
                      type="button"
                      onClick={() => setTheme(applyTheme(t))}
                      className={C.option(theme === t)}
                    >
                      {THEME_LABELS[t]}（{t === 'light' ? '默认' : '夜间'}）
                    </button>
                  ))}
                </div>
                <p className={C.help}>切换立刻生效，并写入后端设置，换设备后仍是同一主题。</p>
              </div>

              <div>
                <span className={C.label}>内容策略</span>
                <div className="grid grid-cols-2 gap-2">
                  {[{ id: false, label: '标准' }, { id: true, label: 'NSFW（本地）' }].map((mode) => (
                    <button
                      key={String(mode.id)}
                      type="button"
                      onClick={() => setNsfw(mode.id)}
                      className={C.option(nsfw === mode.id)}
                    >
                      {mode.label}
                    </button>
                  ))}
                </div>
                <label className="flex items-center gap-2 mt-3 text-[11px] text-[#5c574f]">
                  <input
                    type="checkbox"
                    checked={refusalFallbackOn}
                    onChange={(e) => setRefusalFallbackOn(e.target.checked)}
                    className="accent-[#4a453f]"
                  />
                  上游拒绝时自动改走本地模型重试一次
                </label>
              </div>

              <div>
                <span className={C.label}>API 访问令牌（可选）</span>
                <input
                  type="password"
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  placeholder="后端未启用 API_TOKEN 时留空"
                  autoComplete="off"
                  className={C.input}
                />
                <p className={C.help}>
                  仅当后端以 API_TOKEN 启动时填写；令牌只留在本机浏览器，不会被后端保存或回传。
                </p>
              </div>

              {activeCampaignId && (
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <span className={C.label}>战斗模式</span>
                    <p className="text-[10px] leading-relaxed text-[#8a857c]">
                      无对抗场景可关闭，将跳过战斗相关的模型调用。
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={combat}
                    onClick={() => setCombat((v) => !v)}
                    className={`relative inline-flex shrink-0 items-center w-11 h-6 rounded-full border transition-colors ${
                      combat ? 'bg-[#4a453f] border-[#4a453f]' : 'bg-[#e8e5de] border-[#d8d4cc]'
                    }`}
                  >
                    <span
                      className={`inline-block w-4 h-4 rounded-full transition-transform ${
                        combat ? 'translate-x-6 bg-white' : 'translate-x-1 bg-[#8a857c]'
                      }`}
                    />
                  </button>
                </div>
              )}
            </>
          )}

          {tab === 'model' && (
            <>
              <div>
                <span className={C.label}>模型服务商</span>
                <select
                  value={provider}
                  onChange={(e) => handleProviderChange(e.target.value)}
                  className={C.input}
                >
                  {availableProviders.map((p) => (
                    <option key={p.id} value={p.id}>{p.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <span className={C.label}>模型</span>
                {provider === 'custom' ? (
                  <input
                    type="text"
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                    placeholder="deepseek-v4.1-flash"
                    spellCheck={false}
                    className={C.input}
                  />
                ) : (
                  <select value={model} onChange={(e) => setModel(e.target.value)} className={C.input}>
                    {currentProviderModels.map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </select>
                )}
                {provider === 'custom' && (
                  <p className={C.help}>
                    地址与密钥来自 .env：CUSTOM_LLM_BASE_URL / CUSTOM_LLM_API_KEY。填什么模型就发什么模型名，密钥不会回到浏览器。
                  </p>
                )}
                {provider === 'local' && (
                  <p className={C.help}>
                    需要本地 NInfer 服务：127.0.0.1:8094（运行 start-local.ps1）。地址与密钥来自 .env：LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY。
                  </p>
                )}
                {modelError && <p className="text-[10px] leading-relaxed mt-2 text-rose-600">{modelError}</p>}
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c]">创造性（温度）</span>
                  <span className="text-xs font-mono">{temp.toFixed(2)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={2}
                  step={0.05}
                  value={temp}
                  onChange={(e) => setTemp(parseFloat(e.target.value))}
                  className="w-full accent-[#4a453f]"
                />
                <div className="flex justify-between text-[9px] text-[#a8a29a] mt-1">
                  <span>精准</span><span>均衡</span><span>天马行空</span>
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c]">最大 Token 数</span>
                  <span className="text-xs font-mono">{tokens}</span>
                </div>
                <input
                  type="range"
                  min={256}
                  max={8192}
                  step={256}
                  value={tokens}
                  onChange={(e) => setTokens(parseInt(e.target.value, 10))}
                  className="w-full accent-[#4a453f]"
                />
                <div className="flex justify-between text-[9px] text-[#a8a29a] mt-1">
                  <span>简短</span><span>标准</span><span>扩展</span>
                </div>
              </div>
            </>
          )}

          {tab === 'narrative' && (
            <>
              <div>
                <span className={C.label}>叙事风格</span>
                <div className="grid grid-cols-3 gap-2">
                  {STYLE_OPTIONS.map((o) => (
                    <button
                      key={o.id || 'auto'}
                      type="button"
                      onClick={() => setNarrStyle(o.id)}
                      data-testid={`settings-style-${o.id || 'auto'}`}
                      className={C.option(narrStyle === o.id)}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
                <p className={C.help}>只是语气与句式提示，和写作指令叠加使用，不会改变规则判定。</p>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <span className={C.label}>人称</span>
                  <select
                    value={narrPerspective}
                    onChange={(e) => setNarrPerspective(e.target.value)}
                    className={C.input}
                  >
                    {PERSPECTIVE_OPTIONS.map((o) => (
                      <option key={o.id || 'auto'} value={o.id}>{o.label}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <span className={C.label}>篇幅</span>
                  <select
                    value={narrLength}
                    onChange={(e) => setNarrLength(e.target.value)}
                    className={C.input}
                  >
                    {LENGTH_OPTIONS.map((o) => (
                      <option key={o.id || 'auto'} value={o.id}>{o.label}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <span className={C.label}>语气参考（可选）</span>
                <input
                  type="text"
                  value={narrHint}
                  maxLength={200}
                  onChange={(e) => setNarrHint(e.target.value)}
                  placeholder="例如：像说书人，句子短促，少用成语"
                  className={C.input}
                />
              </div>

              <div className="space-y-2">
                <label className="flex items-center gap-2 text-[11px] text-[#5c574f]">
                  <input
                    type="checkbox"
                    checked={showRules}
                    onChange={(e) => setShowRules(e.target.checked)}
                    className="accent-[#4a453f]"
                  />
                  在叙事里点出判定结果（例如「察觉检定失败」）
                </label>
                <label className="flex items-center gap-2 text-[11px] text-[#5c574f]">
                  <input
                    type="checkbox"
                    checked={allowOoc}
                    onChange={(e) => setAllowOoc(e.target.checked)}
                    className="accent-[#4a453f]"
                  />
                  允许场外提示（用 [] 包裹，用于提问或调整方向）
                </label>
              </div>

              <div>
                <span className={C.label}>采样参数（留空跟随世界/预设）</span>
                <div className="grid grid-cols-3 gap-2">
                  {[
                    { label: 'top_p', value: topP, set: setTopP, placeholder: '0-1' },
                    { label: '存在惩罚', value: presence, set: setPresence, placeholder: '-2~2' },
                    { label: '频率惩罚', value: frequency, set: setFrequency, placeholder: '-2~2' },
                  ].map((field) => (
                    <label key={field.label} className="block">
                      <span className="block text-[9px] uppercase tracking-[0.15em] text-[#a8a29a] mb-1">
                        {field.label}
                      </span>
                      <input
                        type="text"
                        inputMode="decimal"
                        value={field.value}
                        onChange={(e) => field.set(e.target.value)}
                        placeholder={field.placeholder}
                        className={C.input}
                      />
                    </label>
                  ))}
                </div>
                <p className={C.help}>
                  只有填了才会覆盖世界自带的采样值；温度与最大 token 在「模型」页。
                </p>
              </div>
            </>
          )}

          {tab === 'writing' && (
            <>
              <div className={`rounded-xl border p-4 ${C.card}`}>
                <div className="flex items-center gap-2 mb-2">
                  <PenLine size={14} />
                  <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c]">全局写作指令</span>
                </div>
                <textarea
                  data-testid="settings-directive"
                  value={directive}
                  onChange={(e) => setDirective(e.target.value)}
                  rows={7}
                  maxLength={4000}
                  placeholder="例如：第三人称、冷峻短句，不写内心独白；每次回应不超过三段。"
                  className={C.input + ' leading-relaxed resize-y min-h-[140px]'}
                />
                <p className={C.help}>
                  追加在系统规则之后、随每次叙事发送，立刻生效：作为文风与人称约束，但不会推翻规则判定
                  （奖励、伤害、事件结果仍由规则决定）。{directive.length}/4000
                </p>
              </div>

              <div className={`rounded-xl border p-4 ${C.card}`}>
                <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-[#8a857c] mb-2">
                  本世界专属指令（可选）
                </div>
                {activeScenarioId ? (
                  <>
                    <textarea
                      data-testid="settings-world-directive"
                      value={worldDirective}
                      onChange={(e) => setWorldDirective(e.target.value)}
                      rows={5}
                      maxLength={4000}
                      placeholder="留空则跟随全局写作指令。"
                      className={C.input + ' leading-relaxed resize-y min-h-[110px]'}
                    />
                    <p className={C.help}>
                      只影响当前世界；填写后覆盖全局指令。在「世界工坊 → 运行模式」里也能改。
                    </p>
                  </>
                ) : (
                  <p className={C.help}>
                    当前不在某个世界里：从首页选一个世界（或进入游戏）后，这里会多出一份只属于那个世界的指令。
                  </p>
                )}
                {directiveNotice && <p className="text-[10px] mt-2 text-emerald-700">{directiveNotice}</p>}
              </div>
            </>
          )}
        </div>

        <div className="flex items-center justify-between gap-3 px-6 py-4 border-t border-[#e2ded6]">
          <span className="text-[10px] text-[#a8a29a]">
            设置保存到后端，换浏览器或换设备后依然生效。
          </span>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onClose} className={C.ghost}>关闭</button>
            <button
              type="button"
              onClick={handleSave}
              disabled={busy}
              data-testid="settings-save"
              className={C.primary + ' inline-flex items-center gap-2'}
            >
              <Save size={13} />
              {saved ? '已保存' : busy ? '保存中…' : '保存设置'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
