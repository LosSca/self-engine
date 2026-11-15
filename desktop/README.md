# 桌面壳 / Desktop shell

把自指引擎装进一个独立窗口的最薄一层。**这里没有第二套应用**：窗口加载的就是
`frontend/dist`，后端就是 `backend/`，和浏览器里打开的是同一份代码、同一个数据库。

```
launch.py --app
  ├─ 起后端（uvicorn，127.0.0.1:8000）
  │    └─ SELF_ENGINE_DESKTOP_DIST=frontend/dist
  │         → 同一个端口既答 /api 也答界面
  └─ electron desktop/  LUNAR_APP_URL=http://127.0.0.1:8000/
```

一个端口是刻意的：开发模式那套「前端 5173 + 后端 8000 + 一条反代」只要有一处不同步，
窗口里就是白屏。桌面壳里没有这条链，也就没有这类故障。

## 用

```bash
cd desktop && npm install --registry=https://registry.npmmirror.com   # 首次
npm --prefix ../frontend run build                                    # 首次和每次改前端后
python launch.py --app
```

Electron 的二进制默认从 GitHub 下载，国内网络装不动时设一个镜像：

```bash
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npm install
```

## 边界

- 窗口不读存档、不拼地址、不注入脚本；端口由 `launch.py` 算好再传进来（它知道端口被顺延到哪）。
  所以 `npm start` 直接启动时只会连默认的 8000。
- 渲染进程保持默认隔离（`contextIsolation`、`nodeIntegration: false`、`sandbox: true`）：
  页面拿不到 Node，桌面端因此不比浏览器端多任何权限。
- 关掉窗口就退出，后端由 `launch.py` 在 `finally` 里回收。
- 现在只是「双击能跑」的最小版：还没有安装包、没有自动更新、没有签名。
