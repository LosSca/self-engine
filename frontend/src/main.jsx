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
// saved in settings. The token never leaves this browser's localStorage.
const nativeFetch = window.fetch.bind(window)
window.fetch = (input, init = {}) => {
  const token = localStorage.getItem('lunar_api_token')
  const url = typeof input === 'string' ? input : input?.url || ''
  if (token && url.startsWith('/api')) {
    const headers = new Headers(init.headers || undefined)
    if (!headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
    init = { ...init, headers }
  }
  return nativeFetch(input, init)
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
