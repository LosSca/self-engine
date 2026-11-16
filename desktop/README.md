# desktop/ — Tauri 桌面壳

壳只负责「像桌面软件一样双击打开」，**不重写任何界面**：窗口里渲染的就是
`frontend/dist`（React 19 + Vite 7）构建出来的同一套页面。

> **定位**：`desktop/` 是仓库里唯一能产出安装程序（NSIS / MSI / dmg / deb）的路径，也是
> 唯一「双击即用」的窗口。另外两条容易和它混淆的启动方式各管别的事，不要当成它的替代品：
>
> | 路径 | 是什么 | 与本目录的关系 |
> |------|--------|----------------|
> | `python launch.py --app` | 后端在**同一个端口**托管 `frontend/dist`，再用 Chrome/Edge 的 `--app` 模式开一个窗口 | 没有 Rust 工具链时的预览退路；不打包、不产出安装程序 |
> | `start.bat --prod` | `serve-dist.mjs` 在 4173 托管构建产物，并把 `/api` 反代到 8000 | 纯开发期预览，浏览器里带地址栏 |
>
> 三条路径里只有 `--app` 是单端口单来源；本壳固定是「`tauri://localhost` 页面 +
> `127.0.0.1:8000` 后端」两个来源，靠下面第 2 步的注入和 CORS 来源打通。

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
（Win10/11 一般已自带），以及 MSVC 工具链——MinGW 能编译通过，但创建 WebView2 窗口时会返回
`HRESULT 0x8000FFFF`，那是 Tauri 的限制，不是本壳的 bug。

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
  `tauri.conf.json` 的 `bundle` 段目前没有 `resources`，所以装好的程序**不带后端**。
- 目前每次启动都会尝试拉起后端；如果 8000 端口已经有本项目后端在跑，启动器会直接复用。
  复用的是本机已有的那一份——它的数据目录和配置未必与本次启动的意图一致。
- 后端进程的 stdout/stderr 被丢弃（`lib.rs` 里是 `Stdio::null()`），日志由 `launch.py`
  写进仓库根的 `.logs/backend.log`。后端起不来时窗口只会空等，排查要看那个文件。
