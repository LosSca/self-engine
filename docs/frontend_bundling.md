# 前端打包策略（代码分割）

改造前，`npm run build` 只产出一个 783 kB（gzip 241 kB）的 bundle，浏览器必须下载
并解析完整个编辑器、地图图谱和 Markdown 渲染链，才能显示首页。现在首页只需要
下载自己用到的那部分。

## 现在的分块

| 产物 | 大小（gzip） | 什么时候下载 |
| --- | --- | --- |
| `index-*.js` | 52.9 kB（17.7 kB） | 首屏必下 |
| `vendor-react-*.js` | 192.5 kB（60.3 kB） | 首屏必下 |
| `vendor-router-*.js` | 36.3 kB（13.1 kB） | 首屏必下 |
| `vendor-state-*.js` | 0.7 kB（0.4 kB） | 首屏必下 |
| `GameCanvas-*.js` | 71.0 kB（18.7 kB） | 进入 `/play` |
| `vendor-markdown-*.js` | 111.3 kB（33.7 kB） | 进入 `/play` |
| `vendor-*.js` | 88.2 kB（30.3 kB） | 进入 `/play` |
| `ScenarioBuilder-*.js` | 18.4 kB（5.9 kB） | 进入 `/create` |
| `SetupWizard-*.js` | 9.7 kB（3.5 kB） | 需要开场问答时 |
| `KnowledgeStudio-*.js` | 24.0 kB（6.7 kB） | 打开「知识与模式」 |
| `WorldStudio-*.js` | 54.5 kB（14.8 kB） | 打开「世界工坊」 |
| `WorldMapModal-*.js` | 9.5 kB（3.8 kB） | 点开地图 |
| `vendor-graph-*.js` | 106.8 kB（34.3 kB） | 点开地图（force-graph + d3） |

首屏总量从 783 kB / 241 kB(gzip) 降到约 282 kB / 92 kB(gzip)。

## 做法

1. **按屏幕切分**（`frontend/src/App.jsx`）：`GameCanvas`、`ScenarioBuilder`、
   `SetupWizard`、`KnowledgeStudio`、`WorldStudio` 全部改用 `React.lazy`。
2. **弹窗按需挂载**：两个工作室改成 `{studioScenario && <Suspense>…}`，只有真的点
   开时才请求对应的 chunk，首页不预取。`Suspense` 的占位是同款全屏底色
   （`ChunkFallback` / `ModalFallback`），避免闪白。
3. **地图单独懒加载**（`frontend/src/components/GameCanvas.jsx`）：`WorldMapModal`
   依赖 `react-force-graph-2d` 和整条 d3，之前是静态 import，会让游玩首屏多背
   106 kB。现在只在 `mapOpen` 为真时挂载。
4. **第三方库分块**（`frontend/vite.config.js` 的 `manualChunks`）：react / router /
   zustand / graph / markdown / icons 各自成块。库的版本变化远慢于业务代码，
   缓存命中率更高，也避免把 React 复制进每个懒加载 chunk。

## 验证

```bash
cd frontend
npm run build
```

产物里不应再出现「chunks are larger than 500 kB」警告；`dist/index.html` 的
`modulepreload` 只应包含 `vendor-react`、`vendor-router`、`vendor-state` 三个。

开发态（`npm run dev`）不受影响，Vite 本来按模块提供文件。

## 已知遗留

- `npx eslint src` 仍有 38 个历史错误（`no-empty`、未使用的变量、fast-refresh
  只导出组件等），本次改造前就存在，与代码分割无关。
- `frontend/src/api.js` 被多处「既静态又动态」导入，rollup 会警告它无法移动到
  独立 chunk；这是既有结构，暂不处理。
