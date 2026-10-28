const STORAGE_KEY = 'selfref-theme'
export const THEMES = ['light', 'dark']
export const DEFAULT_THEME = 'light'

export const THEME_LABELS = {
  light: '纸白',
  dark: '墨黑',
}

/** 读取上次选择的主题；首次访问默认浅色（纸白）。 */
export function getStoredTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return THEMES.includes(saved) ? saved : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

/** 把主题写到 <html data-theme>，light.css / 深色样式据此生效。 */
export function applyTheme(theme) {
  const next = THEMES.includes(theme) ? theme : DEFAULT_THEME
  document.documentElement.dataset.theme = next
  document.documentElement.style.colorScheme = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 隐私模式下 localStorage 不可用：主题只在本次会话生效。
  }
  return next
}

export function currentTheme() {
  const active = document.documentElement.dataset.theme
  return THEMES.includes(active) ? active : DEFAULT_THEME
}

export function toggleTheme() {
  return applyTheme(currentTheme() === 'light' ? 'dark' : 'light')
}
