import { Component } from 'react'

// 重新部署后旧的 chunk 文件会被删除，此时动态 import 返回 404/HTML，
// 浏览器把它当成脚本语法错误抛出。只有硬刷新能拿到新的文件名。
const RETRY_KEY = 'lunar.chunk-retry'

const canAutoRetry = () => {
  try {
    return window.sessionStorage.getItem(RETRY_KEY) !== '1'
  } catch {
    return false
  }
}

const markAutoRetry = () => {
  try {
    window.sessionStorage.setItem(RETRY_KEY, '1')
  } catch {
    /* 隐私模式下 sessionStorage 不可写，那就交给用户手动刷新 */
  }
}

const clearAutoRetry = () => {
  try {
    window.sessionStorage.removeItem(RETRY_KEY)
  } catch {
    /* ignore */
  }
}

const CHUNK_ERROR = /dynamically imported module|Importing a module script failed|Loading chunk|Unable to preload CSS/i

const isChunkLoadError = (error) =>
  Boolean(error) && CHUNK_ERROR.test(String(error.message || error))

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, errorInfo) {
    if (isChunkLoadError(error) && canAutoRetry()) {
      markAutoRetry()
      window.location.reload()
      return
    }
    console.error('ErrorBoundary caught:', error, errorInfo)
  }

  componentDidMount() {
    // 页面正常挂载即说明这一次加载成功了，下次部署后仍可自动重试。
    clearAutoRetry()
  }

  render() {
    if (this.state.hasError) {
      const chunkFailure = isChunkLoadError(this.state.error)
      if (chunkFailure) {
        return (
          <div className="min-h-screen bg-black flex items-center justify-center p-8">
            <div className="max-w-md text-center">
              <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-amber-500/10 flex items-center justify-center">
                <span className="text-amber-300 text-2xl">↻</span>
              </div>
              <h1 className="text-white text-xl font-bold mb-3">界面资源已更新</h1>
              <p className="text-white/40 text-sm mb-2 leading-relaxed">
                这通常是重新部署后旧的脚本文件被替换导致的，刷新一次即可。
              </p>
              <p className="text-white/25 text-xs mb-6 break-all">
                {this.state.error?.message}
              </p>
              <button
                onClick={() => {
                  clearAutoRetry()
                  window.location.reload()
                }}
                className="px-6 py-3 bg-white/10 hover:bg-white/20 text-white rounded-xl text-sm font-medium transition-colors"
              >
                刷新页面
              </button>
            </div>
          </div>
        )
      }
      return (
        <div className="min-h-screen bg-black flex items-center justify-center p-8">
          <div className="max-w-md text-center">
            <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-rose-500/10 flex items-center justify-center">
              <span className="text-rose-400 text-2xl">!</span>
            </div>
            <h1 className="text-white text-xl font-bold mb-3">服务出现异常</h1>
            <p className="text-white/40 text-sm mb-6 leading-relaxed">
              {this.state.error?.message || '发生了意外错误。'}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="px-6 py-3 bg-white/10 hover:bg-white/20 text-white rounded-xl text-sm font-medium transition-colors"
            >
              重新加载
            </button>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
