# 启动方式（统一入口）

本项目只有**一份**启动逻辑：`launch.py`。Windows 的 `start.bat` 和 Linux/macOS 的
`scripts/start.sh` 都只是"找到解释器 + 把参数原样透传"，它们不含任何编排代码，
所以两端行为不会漂移。

旧的 `start_backend.bat` / `start_frontend.bat` / `start_debug.bat` / `start-mobile.bat`
已经删除，它们对应的能力全部变成 `launch.py` 的参数（见下面的对照表）。

---

## 一、三种用法

### 1. 本地开发（默认）

    start.bat                      :: Windows
    ./scripts/start.sh             # Linux / macOS

拉起后端 `http://127.0.0.1:8000`（uvicorn，热重载需要 `--debug`）和前端
`http://127.0.0.1:5173`（vite dev），等 `/api/health` 返回 200 后自动打开浏览器。

默认**每个服务一个独立窗口**：关掉某个窗口就停掉那个服务；主窗口按回车关闭不会停服务。
只想用一个终端、让 Ctrl+C 一次停全部，加 `--no-window`。

### 2. 手机 / 局域网访问

    start.bat --mobile             :: 等价于 --host 0.0.0.0

同时监听所有网卡，并在就绪后打印本机的局域网地址（只列 192.168. / 10. / 172.16-31 网段）：

    局域网/手机访问（同一 WiFi 下）：
      前端    http://192.168.1.23:5173
      后端API http://192.168.1.23:8000/docs

手机浏览器打开上面那条"前端"地址即可。第一次连不上，通常是被 Windows 防火墙拦了，
需要在"允许应用通过防火墙"里给 `python.exe` 和 `node.exe` 勾上"专用网络"。

> 注意：`--host 0.0.0.0` 会把 API 暴露给同网段的所有设备。项目默认的 token 校验仍然生效
> （`/api/health`、`/docs` 等少数路径免校验），但在公共 WiFi（咖啡厅、酒店）下请勿使用。

### 3. 生产构建模式（--prod）

    start.bat --prod

先执行 `npm run build` 把前端编译进 `frontend/dist`，再由 `scripts/serve-dist.mjs`
（一个几十行的 node 静态服务器）在 **4173** 端口托管它。没有热更新，改完源码重跑本命令
（加 `--rebuild` 可强制重建）。`/api` 与 `/media` 由这个静态服务器反代到后端，所以
预览模式不需要额外配置代理。

为什么要自带静态服务器，而不是 `vite preview`：预览页出过一次"页面全白、但服务器返回
200"的事故——构建产物 `dist/index.html` 里残留了开发用的
`<script type="module" src="/src/main.jsx">`，浏览器把源码当模块再拉一次就崩了。
自带服务器在托管时把这一行过滤掉（`stripDevEntry`），行为可读、可测、可解释。
另外 `--prod` 下的依赖预打包、裸导入解析等容易出问题的环节全部不存在：浏览器拿到的
是一个已经打包好的 js 文件，这也是**桌面快捷方式默认走 --prod** 的原因——
它在各种环境下都能真正渲染。

只想改代码、要热更新时用默认的开发模式；只想看效果、给别人演示时用 `--prod`。

### 4. 桌面快捷方式

桌面上的 `Self-Reference Engine` 指向仓库根目录的 `start.bat`，并且**默认带 `--prod`**
（理由见上一节）。想临时回到 vite dev 就在快捷方式的参数里加 `--dev`：

    "Self-Reference Engine.bat" --dev

`Self-Reference Engine (debug)` 同样默认 `--prod`，额外加 `--no-browser` 并保留控制台
窗口（退出后打印退出码并等待按键），用来在页面出问题时看启动输出。
---

## 二、参数表

| 参数 | 作用 |
| --- | --- |
| `--host 地址` | 绑定地址，默认 `127.0.0.1`。填 `0.0.0.0` 允许局域网/手机访问。 |
| `--mobile` | `--host 0.0.0.0` 的快捷方式（取代已删除的 `start-mobile.bat`）。两者同时给时以 `--host` 为准。 |
| `--prod` | 先 `npm run build`，再由 `scripts/serve-dist.mjs` 在 4173 托管 `frontend/dist`。 |
| `--rebuild` | 配合 `--prod`，启动前强制重新构建（默认只在 dist 缺失时自动构建）。 |
| `--backend-only` | 只启动后端 8000（取代 `start_backend.bat`）。 |
| `--frontend-only` | 只启动前端 5173，假定后端已就绪（取代 `start_frontend.bat`）。 |
| `--debug` | 后端 `--reload` + `debug` 日志，并设 `LUNAR_DUMP_LLM_CALLS=1` 转储 LLM 调用（取代 `start_debug.bat`）。 |
| `--no-browser` | 不自动打开浏览器。 |
| `--no-window` | 不开新窗口：服务作为本进程的子进程运行，日志写 `.logs/`，Ctrl+C 一次停全部。自检/CI 用。 |
| `--help` | 打印中文帮助（含三种典型用法）。 |

参数可以组合，例如 `start.bat --backend-only --debug --no-window`。

### 旧入口 → 新写法

| 已删除 | 现在 |
| --- | --- |
| `start_backend.bat` | `start.bat --backend-only` |
| `start_frontend.bat` | `start.bat --frontend-only` |
| `start_debug.bat` | `start.bat --debug` |
| `start-mobile.bat` | `start.bat --mobile` |

---

## 三、端口、地址与日志

| 项 | 值 |
| --- | --- |
| 后端 | `http://127.0.0.1:8000`（Swagger: `/docs`） |
| 健康检查 | `http://127.0.0.1:8000/api/health` → `{"status":"ok"}` |
| 前端（dev） | `http://127.0.0.1:5173`（vite dev） |
| 前端（--prod） | `http://127.0.0.1:4173`（`scripts/serve-dist.mjs` 托管 `frontend/dist`） |
| 解释器 | `backend/.venv/Scripts/python.exe`（Windows）/ `backend/.venv/bin/python` |

`--no-window` 时日志写到项目根的 `.logs/backend.log`、`.logs/frontend.log`；
如果 `.logs/` 不可写（例如权限受限的机器），会自动改写到系统临时目录下的
`lunar-logs/`，启动时会打印实际路径。

---

## 四、常见错误与排查

### 1. `--prod` 构建失败

`--prod` 在 `frontend/dist/index.html` 缺失时会自动跑一次 `npm run build`。构建失败时
启动器会打印 `[错误] 前端构建失败` 并以非零退出码结束。手动复现：

    cd frontend
    npm run build

常见原因：`frontend/node_modules` 不存在（先 `npm install`）、源码有语法错误、磁盘满。
### 2. 页面一片空白

先确认地址：**4173** 是构建产物（能渲染），**5173** 是 vite dev。如果 5173 打开是白的，
换 `start.bat --prod`（或双击桌面快捷方式），这是已知的、只在开发服务器上出现的问题
（开发服务器依赖浏览器端预打包，环境不支持时裸导入会 404）。

仍为空白时按 F12 看 Console 第一条红色错误，并看 `.logs/frontend.log` 的最后几行。

### 3. 等待前端就绪超时

`--no-window` 模式下，超时后 launch.py 会直接把该服务日志的**最后几行**打出来，先看那里。
常见原因：

- `frontend/node_modules` 不存在 → `cd frontend && npm install`；
- 端口被别的进程占用（launch.py 用 `--strictPort`，占用了就启动失败而不是悄悄换端口；
  dev 是 5173，`--prod` 是 4173）：

      netstat -ano | findstr :5173
      taskkill /PID <PID> /T /F

- 日志里出现 `failed to load config from ... vite.config.js` 和 `spawn EPERM`，
  或者 `npm` 打出 `'CALL "C:\Program Files\nodejs\\node.exe" ...' 不是内部或外部命令`：
  说明当前环境的沙箱/安全软件拦住了 node 的子进程创建（vite 加载配置时要用 esbuild
  拉子进程）。请在自己的 cmd/PowerShell 窗口里运行 `start.bat`，不要放在受限沙箱、
  某些杀软的"受控文件夹"或 CI 沙箱里跑。

### 4. 后端起来但 `/api/health` 一直不 ok

- 8000 被别的程序占用：`netstat -ano | findstr :8000`，launch.py 在启动前会做这项检查，
  如果占用者不是本项目后端会直接报错退出。
- 依赖缺失：`backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt`
  （若提示 `No module named pip`，见下面第 8 条）
- 看 `.logs/backend.log` 最后几十行，或去掉 `--no-window` 打开独立窗口看报错。

### 5. 手机连不上（用了 `--mobile` 还是打不开）

1. 手机和电脑必须同一个 WiFi（很多路由器有"访客网络"隔离，隔离后互相访问不到）；
2. 用 `ipconfig` 确认打印出来的地址确实是电脑的局域网地址；
3. Windows 防火墙要允许 `python.exe`（8000）和 `node.exe`（5173）通过"专用网络"；
4. 公司/学校网络可能禁止设备之间互访，此时只能用回环地址本机调试。

### 6. 找不到 Python / 虚拟环境

`launch.py` 依次查找 `backend/.venv`（新）与 `backend/venv`（旧脚本留下的），
都找不到会提示先跑 `install.bat`（Windows）或 `./install.sh`（Linux/macOS）。
新环境请统一用 `backend/.venv`。

### 7. 安装脚本里的 Neo4j / Docker 步骤

`install.bat` / `install.sh` 里的 Neo4j 步骤是**可选**的：没有 Docker、Docker 没启动、
或者没有 compose 插件时，脚本会打印一行提示然后继续安装，不会中断。
本机没有 Neo4j 时，引擎、API 和网页界面照常工作，只是图谱/知识库相关功能不可用。

### 8. 关窗口后端口仍然被占

窗口模式下后端/前端各自在一个独立控制台里。关窗口等于结束该进程；如果某个进程没退干净，
用 `netstat -ano | findstr :5173`（同理 :8000）找到 PID，再 `taskkill /PID <PID> /T /F`。
`--no-window` 模式下 Ctrl+C 会按 PID 杀掉整棵进程树（cmd → npm → node），不会残留 vite。

### 9. 虚拟环境里没有 pip（install.bat 报 `No module named pip`）

本工程的 `backend/.venv` 可能是用 uv 之类不带 pip 的方式建的（`python -m pip` 报
`No module named pip`，但 `uvicorn`、`fastapi` 都能正常导入，服务照常启动）。这种环境
不需要重装，用 uv 装依赖即可：

    uv pip install --python backend/.venv/Scripts/python.exe -r backend/requirements.txt

也可以给 venv 补上 pip：`backend\.venv\Scripts\python.exe -m ensurepip --upgrade`。
`install.bat` 遇到这种情况会明确报错退出，不会把一个坏环境留在那里。

---

## 五、给贡献者

- 不要新增 `start_xxx.bat`。需要新能力就加 `launch.py` 的参数，并在这里补一行。
- `.bat` 文件**只能包含 ASCII**。cmd.exe 用 OEM 代码页解码批处理，UTF-8 中文会被读坏，
  甚至会错切行并报出与真实原因无关的"系统找不到指定的路径"。中文提示一律写在 `launch.py` 里。
- 窗口模式不做 `cmd /c "<长命令>"`：Windows 的 argv 转义会让 `cmd.exe` 把内部引号当成
  字面反引号并报"不是内部或外部命令"。改为 `python launch.py --run-backend` 自我重启，
  参数以列表传给 `CreateProcess`，完全绕开 `cmd.exe` 的引号解析。
