function createUpdates(updater, { notify, requestClose, restoreBackend }) {
  let state = {
    phase: updater ? 'idle' : 'unavailable',
    version: '',
    percent: null,
    canInstall: false,
    message: updater ? '可检查是否有新版本。' : '源码版请通过仓库更新。自动更新适用于安装版。',
  }
  let downloaded = false
  let handedOff = false
  const status = () => state
  const set = (phase, message, details = {}) => {
    state = { ...state, phase, message, percent: null, ...details }
    notify(state)
    return state
  }
  const busy = () => ['checking', 'downloading', 'restarting'].includes(state.phase)
  const failed = (error, message, details) => {
    console.warn('桌面更新失败：', error.code || error.message)
    return set('error', message, details)
  }
  const failInstall = error => {
    if (!handedOff) return
    handedOff = false
    failed(error, '未能启动更新安装。可以稍后重试。', { canInstall: true })
    restoreBackend()
  }

  if (updater) {
    // 发布源和校验配置由安装包内的 app-update.yml 提供。
    updater.autoDownload = false
    updater.autoInstallOnAppQuit = false
    updater.allowPrerelease = false
    updater.allowDowngrade = false
    updater.on('error', failInstall) // 检查、下载的异常由各自的 Promise 处理。
    updater.on('download-progress', progress => {
      if (state.phase !== 'downloading' || !Number.isFinite(progress.percent)) return
      const percent = Math.min(100, Math.max(0, progress.percent))
      set('downloading', percent === 100 ? '下载完成，正在校验安装包…' : '正在下载安装包。', { percent })
    })
  }

  async function check() {
    if (!updater || busy() || downloaded) return state
    set('checking', '正在检查公开发布版本…', { canInstall: false })
    try {
      const result = await updater.checkForUpdates()
      const next = result?.updateInfo?.version
      if (!result) return set('error', '当前安装版未启用更新检查。', { version: '' })
      if (result?.isUpdateAvailable && next && !next.split('+')[0].includes('-')) {
        return set('available', '发现可用的正式版。', { version: next, canInstall: true })
      }
      return set('current', '当前已是最新正式版。', { version: '' })
    } catch (error) {
      const messages = {
        ERR_UPDATER_LATEST_VERSION_NOT_FOUND: '暂未找到可更新的公开发布版本。',
        ERR_UPDATER_NO_PUBLISHED_VERSIONS: '暂未找到可更新的公开发布版本。',
        ERR_UPDATER_CHANNEL_FILE_NOT_FOUND: '公开发布暂时缺少更新清单。',
        ENOENT: '该安装版缺少更新配置。',
      }
      const message = messages[error.code] || '检查更新失败。请稍后重试。'
      return failed(error, message, { version: '' })
    }
  }

  async function updateAndRestart() {
    if (!updater || !state.canInstall || busy()) return state
    if (!downloaded) {
      set('downloading', '正在准备下载安装包…', { canInstall: false })
      try {
        // electron-updater 验证发布清单的摘要，并按安装包配置检查签名。
        await updater.downloadUpdate()
      } catch (error) {
        return failed(error, '下载或校验失败。可以稍后重试。', { canInstall: true })
      }
      downloaded = true
    }
    set('restarting', '校验已完成，正在准备安全退出并安装…', { canInstall: false })
    if (!requestClose()) cancelRestart('安装包已下载。请在当前操作完成后重试。')
    return state
  }

  function cancelRestart(message = '安装包已下载。可以继续使用，稍后再更新。') {
    if (state.phase === 'restarting' && !handedOff) set('ready', message, { canInstall: true })
  }

  function installAfterClose() {
    if (!downloaded || state.phase !== 'restarting' || handedOff) return
    handedOff = true
    try {
      // 更新后由 NSIS 的 per-user 安装段在同一用户上下文重开应用。
      updater.quitAndInstall(true, false)
    } catch (error) {
      failInstall(error)
    }
  }
  return { status, check, updateAndRestart, cancelRestart, installAfterClose }
}

module.exports = { createUpdates }
