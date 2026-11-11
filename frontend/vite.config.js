import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')

  return {
    // React（以及 @vitejs/plugin-react 注入的自动运行时）依赖 `process.env.NODE_ENV` 来判断
    // 该导出开发版还是生产版。浏览器里没有 process，构建时不替换就会在首屏直接抛
    // `ReferenceError: process is not defined`（白屏，且 React 内部的开发期分支也会被留下）。
    // 这里显式定义它；同时 frontend/index.html 里还有一行 window.process 垫片，
    // 因为本机某些构建链会绕开本文件（配置由外部 stub 提供），define 不生效。
    define: {
      // 注意：这里**不要**再单独写 "process.env": "{}" —— 更长的键优先，它会把上面那条
      // NODE_ENV 的替换整个吃掉，React 又会退回用 process 判断。
      "process.env.NODE_ENV": JSON.stringify(process.env.NODE_ENV || mode),
    },
    plugins: [
      react(),
    ],
    build: {
      // 注意：`npm run build` 的产物 dist/index.html 里曾残留源文件的
      // <script type="module" src="/src/main.jsx">。浏览器会再请求一次 /src/main.jsx，
      // 拿到 index.html 当模块执行 → 首屏白屏。本机沙箱会重排插件顺序，构建钩子不可靠，
      // 所以「生产预览」交给 scripts/serve-dist.mjs：它托管 dist 时也会过滤掉这一行
      // （见该脚本的 stripDevEntry）。正常 Vite 构建本身就会删掉它。
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
