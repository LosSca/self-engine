import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './styles/light.css'
import { applyTheme, getStoredTheme } from './lib/theme'
import App from './App.jsx'

// 主题必须在首帧渲染前落到 <html data-theme>，否则会先闪一下深色。
// 支持 ?theme=dark|light 临时覆盖，便于分享和截图。
const requestedTheme = new URLSearchParams(window.location.search).get('theme')
const initialTheme = requestedTheme === 'dark' || requestedTheme === 'light' ? requestedTheme : getStoredTheme()
applyTheme(initialTheme)

// Optional API_TOKEN support: every same-origin /api call carries the token
// typed into settings.
//
// It is kept in sessionStorage, not localStorage: sessionStorage is not shared
// with other tabs, dies with the tab, and is not part of the "storage" surface a
// long-lived local page can read after the fact. The threat model is explicit -
// a token placed in browser storage is readable by any script running on this
// origin, so the only real defence is the API listening on loopback and the CORS
// allow-list. Anything more sensitive than a loopback call should not use the
// browser at all.
const TOKEN_KEY = "lunar_api_token"

export function readApiToken() {
  try {
    const current = sessionStorage.getItem(TOKEN_KEY)
    if (current) return current
    // Carry over anything saved before the move, then drop the old copy.
    const legacy = localStorage.getItem(TOKEN_KEY)
    if (legacy) {
      sessionStorage.setItem(TOKEN_KEY, legacy)
      localStorage.removeItem(TOKEN_KEY)
      return legacy
    }
    return ""
  } catch {
    // Storage can be disabled (private window, blocked cookies); no token then.
    return ""
  }
}

export function writeApiToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    // Nothing to do: the request simply goes out without a token.
  }
}

const nativeFetch = window.fetch.bind(window)
window.fetch = (input, init = {}) => {
  const token = readApiToken()
  const url = typeof input === "string" ? input : input?.url || ""
  if (token && url.startsWith("/api")) {
    const headers = new Headers(init.headers || undefined)
    if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`)
    init = { ...init, headers }
  }
  return nativeFetch(input, init)
}
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
