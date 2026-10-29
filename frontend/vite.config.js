import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')

  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        output: {
          // 第三方库单独成块：它们的版本变化远慢于业务代码，缓存命中率更高，
          // 也避免把 React 打进每个懒加载 chunk 里。
          manualChunks(id) {
            if (!id.includes('node_modules')) return undefined
            if (id.includes('react-router')) return 'vendor-router'
            if (id.includes('react-dom') || id.includes('/react/') || id.includes('scheduler')) return 'vendor-react'
            if (id.includes('zustand')) return 'vendor-state'
            // 图谱（force-graph + d3）只在打开地图时下载，别和游玩首屏绑在一起。
            if (/react-force-graph|force-graph|\/d3-|\/d3\/|internmap|delaunator|robust-predicates/.test(id)) {
              return 'vendor-graph'
            }
            // Markdown 渲染链（remark/rehype/micromark 全家桶）单独成块。
            if (/react-markdown|remark-|rehype-|micromark|mdast|hast-util|unist|unified|vfile|bail|trough|devlop|comma-separated|decode-named|character-entities|property-information|space-separated|style-to-|html-url|zwitch|longest-streak|ccount|markdown-table|escape-string-regexp/.test(id)) {
              return 'vendor-markdown'
            }
            if (id.includes('lucide-react')) return 'vendor-icons'
            return 'vendor'
          },
        },
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: env.LUNAR_API_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
        '/media': {
          target: env.LUNAR_API_TARGET || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
