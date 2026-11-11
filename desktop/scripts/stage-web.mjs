#!/usr/bin/env node
// 把 frontend/dist 复制到 desktop/web，作为 Tauri 的 frontendDist。
//
// 为什么不在 tauri.conf.json 里直接指 ../frontend/dist：
// Tauri 要求 frontendDist 在 src-tauri 目录之外时必须是构建期存在的静态目录，
// 而 frontend/dist 是 npm run build 的产物、默认被 .gitignore 忽略。复制一份到
// desktop/web（同样被忽略）让「先 npm run build 再 tauri build」的顺序显式可见，
// 也让壳可以顺手对 index.html 做一次安全检查。
import { cp, mkdir, readFile, rm, writeFile, stat } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktop = path.resolve(here, '..')
const repo = path.resolve(desktop, '..')
const dist = path.join(repo, 'frontend', 'dist')
const out = path.join(desktop, 'web')

if (!existsSync(dist)) {
  console.error('[stage-web] frontend/dist 不存在：先执行  cd frontend && npm run build')
  process.exit(1)
}

await rm(out, { recursive: true, force: true })
await mkdir(out, { recursive: true })
await cp(dist, out, { recursive: true })

// 构建产物里若残留开发期的 <script src="/src/main.jsx">，浏览器会把它当模块再拉一次，
// 拿到 index.html 当 JS 执行 → 白屏。vite build 本身会删掉它，这里兜底一次。
const indexPath = path.join(out, 'index.html')
let html = await readFile(indexPath, 'utf-8')
const cleaned = html.replace(/\s*<script[^>]*src="\/src\/main\.jsx"[^>]*><\/script>/g, '')
if (cleaned !== html) {
  await writeFile(indexPath, cleaned, 'utf-8')
  console.log('[stage-web] 已移除 index.html 里残留的 /src/main.jsx 入口')
}

const size = (await stat(path.join(out, 'index.html'))).size
console.log('[stage-web] desktop/web 就绪（index.html ' + size + ' bytes）')
