// 自指引擎的桌面壳：窗口里只有一个东西 —— 本机后端。
//
// 这里刻意不做任何"应用逻辑"：不读存档、不拼 URL、不注入脚本。URL 由
// launch.py 算好（它知道端口被顺延到哪里）后再交进来，窗口只负责显示。
// 于是桌面端与浏览器端跑的是同一份代码、同一个后端，不存在第二套状态。

const { app, BrowserWindow, shell } = require('electron')

// 由 launch.py 通过环境变量传入；直接用 electron 启动（npm start）时回落到默认端口。
const TARGET = process.env.LUNAR_APP_URL || 'http://127.0.0.1:8000/'

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: '#f5f3ee',
    title: '自指引擎',
    autoHideMenuBar: true,
    webPreferences: {
      // 渲染进程就是前端页面本身：它不需要 Node，也不该有。保持默认的隔离设置。
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  // 站外链接交给系统浏览器：应用窗口只承载引擎自己的界面。
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(TARGET)) shell.openExternal(url)
    return { action: 'deny' }
  })

  win.loadURL(TARGET)
  return win
}

app.whenReady().then(() => {
  createWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

// 单窗口应用：关掉窗口就退出，后端由 launch.py 负责回收。
app.on('window-all-closed', () => app.quit())
