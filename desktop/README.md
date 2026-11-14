# desktop/ — Tauri 桌面壳

壳只负责「像桌面软件一样双击打开」，**不重写任何界面**：窗口里渲染的就是
`frontend/dist`（React 19 + Vite 7）构建出来的同一套页面。

## 它做了什么

1. 以子进程方式运行仓库的启动器：`python launch.py --backend-only --no-window --no-browser`
   （它会自动找到 `backend/.venv`，起 FastAPI/uvicorn 在 `127.0.0.1:8000`）；
2. 轮询 `/api/health`，就绪后向页面注入 `window.__LUNAR_API_BASE__ = 'http://127.0.0.1:8000'`，
   并派发 `lunar:backend-ready` 事件；
3. 窗口关闭时杀掉后端子进程，不留孤儿 uvicorn。

前端这一侧只加了一处适配：`frontend/src/api.js` 的 `BASE` 优先读 `window.__LUNAR_API_BASE__`，
读不到才退回相对路径 `/api`（浏览器开发模式行为完全不变）。后端 CORS 增加了
`tauri://localhost`（生产环境 WebView2 下是 `http://tauri.localhost`），可通过
`LUNAR_CORS_ORIGINS` 追加。

## 构建

需要 Rust 工具链（`rustup` → stable）+ Node 18+；Windows 还需要 WebView2 运行时
（Win10/11 一般已自带）。

```bash
cd frontend && npm install && npm run build   # 1. 先产出 dist
cd ../desktop && npm install                  # 2. 装 Tauri CLI
npm run dev                                   # 3a. 开发：起后端 + 原生窗口
npm run build                                 # 3b. 出安装包（NSIS/MSI/dmg/deb）
```

`npm run dev` / `npm run build` 都会先跑 `scripts/stage-web.mjs`，把 `frontend/dist`
复制到 `desktop/web`（Tauri 的 `frontendDist`）。

产物：`desktop/src-tauri/target/release/bundle/`。

## 环境变量（可选）

| 变量 | 作用 |
|------|------|
| `LUNAR_PYTHON` | 指定后端解释器（默认 `backend/.venv`，再退回 PATH 里的 python） |
| `LUNAR_REPO_ROOT` | 指定仓库根目录（默认从 exe 往上找，开发期从当前目录往上找） |
| `LUNAR_BACKEND_PORT` | 后端端口（默认 8000，`launch.py` 也认这个变量） |

## 已知限制

- 壳依赖本机的 Python 与已安装的后端依赖；要把「用户什么都不用装」做到位，
  下一步是 `PyInstaller --onedir` 把 `launch.py` + backend 打成 `resources/backend/`，
  再用 `bundle.resources` 分发（壳的代码不需要改，只是 `repo_root()` 会解析到资源目录）。
- 目前每次启动都会尝试拉起后端；如果 8000 端口已经有本项目后端在跑，启动器会直接复用。
- **必须用 MSVC 工具链（`rustup default stable-x86_64-pc-windows-msvc` + Visual Studio
  "使用 C++ 的桌面开发"）。** 用 MinGW（`x86_64-pc-windows-gnu`）虽然能编译通过，但
  WebView2 会在创建窗口时返回 `HRESULT 0x8000FFFF（灾难性故障）`——这是 Tauri 的已知
  限制，不是本壳的 bug。`src-tauri/.cargo/config.toml` 里给 gnu 目标准备了 gcc 链接器
  配置，仅用于在没装 VS 的机器上确认代码能编译，不用于发布。
