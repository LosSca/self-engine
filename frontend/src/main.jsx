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

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
