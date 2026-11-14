const { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, net, protocol, session } = require('electron')
const { spawn } = require('node:child_process')
const { createInterface } = require('node:readline')
const { pathToFileURL } = require('node:url')
const path = require('node:path')

const entry = 'msgx://app/index.html'
const resources = path.join(__dirname, 'renderer')
const root = path.dirname(__dirname)
const dataRoot = process.env.MSGX_ROOT || root
const executable = process.env.MSGX_DESKTOP_PYTHON
const configIndex = process.argv.indexOf('--msgx-config')
const configArgs = configIndex < 0 ? [] : ['--config', process.argv[configIndex + 1]]

// 数据根不同的验收实例有自己的 Electron 配置与单实例锁。
app.setName('消息萃取')
app.setPath('userData', path.resolve(dataRoot, '.ui', 'electron'))
protocol.registerSchemesAsPrivileged([{ scheme: 'msgx', privileges: {
  standard: true, secure: true, supportFetchAPI: true
} }])

let window
let child
let serial = 0
let stderr = ''
let closing = false
let shutdown = false
const pending = new Map()
const picked = new Map()

function titleBar(theme) {
  return theme === 'light'
    ? { color: '#ececec', symbolColor: '#242424', height: 36 }
    : { color: '#242424', symbolColor: '#dedede', height: 36 }
}

function backend(action, payload = {}) {
  if (!child || child.exitCode !== null || !child.stdin.writable) {
    return Promise.reject(new Error(stderr.trim() || '桌面服务已退出。'))
  }
  return new Promise((resolve, reject) => {
    const id = ++serial
    pending.set(id, { resolve, reject })
    child.stdin.write(JSON.stringify({ id, action, payload }) + '\n', error => {
      if (error) {
        pending.delete(id)
        reject(error)
      }
    })
  })
}

function startBackend() {
  if (!executable || !path.isAbsolute(executable)) {
    throw new Error('请通过项目 Conda 环境运行 python -s -m msgx.main gui。')
  }
  child = spawn(executable, ['-s', '-u', path.join(__dirname, 'bridge.py'), ...configArgs], {
    cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']
  })
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000) })
  child.stdin.on('error', () => {}) // 写入回调及退出事件负责通知待完成请求。
  const fail = error => {
    for (const request of pending.values()) request.reject(error)
    pending.clear()
  }
  child.on('error', fail)
  child.on('close', code => fail(new Error(stderr.trim() || '桌面服务退出（' + code + '）。')))
  createInterface({ input: child.stdout }).on('line', line => {
    const response = JSON.parse(line)
    const request = pending.get(response.id)
    if (!request) return
    pending.delete(response.id)
    if (response.ok) request.resolve(response.value)
    else request.reject(new Error(response.error))
  })
}

const filters = {
  api: [{ name: 'YAML 接口配置', extensions: ['yaml', 'yml'] }],
  icon: [{ name: '应用图标', extensions: ['png', 'ico'] }],
  qq: [{ name: 'QQ 程序', extensions: ['exe'] }]
}
const fileActions = {
  'settings.api.import': 'desktop.pickFile:api',
  'settings.api.export': 'desktop.saveFile:api',
  'settings.icon': 'desktop.pickFile:icon'
}

async function desktopCall(event, action, payload) {
  if (event.sender !== window.webContents || event.senderFrame?.url !== entry) {
    throw new Error('仅接受应用主窗口操作。')
  }
  if (typeof action !== 'string' || !payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('无效的桌面请求。')
  }
  if (action === 'desktop.confirm') {
    const value = await dialog.showMessageBox(window, {
      type: 'question', title: String(payload.title), message: String(payload.text),
      buttons: ['取消', '确认'], defaultId: 0, cancelId: 0, noLink: true
    })
    return value.response === 1
  }
  if (action === 'desktop.copy') {
    clipboard.writeText(String(payload.text))
    return true
  }
  if (action === 'desktop.requestClose') {
    window.close()
    return
  }
  if (action === 'desktop.windowTheme') {
    window.setTitleBarOverlay(titleBar(payload.theme))
    return
  }
  if (action === 'desktop.edit') {
    const command = payload.command
    if (['copy', 'paste', 'selectAll'].includes(command)) event.sender[command]()
    return
  }
  if (action === 'desktop.about') {
    await dialog.showMessageBox(window, { title: '关于消息萃取', message: '消息萃取',
      detail: '版本 ' + String(payload.version) + ' · Electron ' + process.versions.electron })
    return
  }
  if (action === 'desktop.pickDirectory') {
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory'] })
    return result.canceled ? null : result.filePaths[0]
  }
  if (action === 'desktop.pickFile' || action === 'desktop.saveFile') {
    if (!Object.hasOwn(filters, payload.kind) || (action === 'desktop.saveFile' && payload.kind !== 'api')) {
      throw new Error('该文件类型不适用。')
    }
    const options = { filters: filters[payload.kind] }
    const result = action === 'desktop.pickFile'
      ? await dialog.showOpenDialog(window, { ...options, properties: ['openFile'] })
      : await dialog.showSaveDialog(window, { ...options, defaultPath: 'msgx-api.yaml' })
    const selected = result.canceled ? null : result.filePath || result.filePaths[0]
    picked.set(action + ':' + payload.kind, selected)
    return selected
  }
  if (action === 'desktop.finishClose') {
    if (!closing || shutdown) return
    shutdown = true
    try {
      await backend('desktop.close')
    } catch (error) {
      if (child.exitCode === null && child.stdin.writable) {
        dialog.showErrorBox('关闭未完成', error.message)
        shutdown = closing = false
        return
      }
    }
    window.destroy()
    app.quit()
    return
  }
  if (action === 'desktop.cancelClose') {
    closing = false
    return
  }
  if (!/^(messages|status|settings|reader|usage)\.[a-zA-Z][\w.-]*$/.test(action)
      && !['desktop.bootstrap', 'desktop.openPreferences'].includes(action)) {
    throw new Error('未知的桌面操作。')
  }
  if (Object.hasOwn(fileActions, action)) {
    const key = fileActions[action]
    if (!payload.path || picked.get(key) !== payload.path) throw new Error('请通过文件对话框选择文件。')
    picked.delete(key)
  }
  const result = await backend(action, payload)
  if (action === 'settings.icon' && result.iconPath) window.setIcon(result.iconPath)
  return result
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!window) return
    if (window.isMinimized()) window.restore()
    window.focus()
  })
  app.whenReady().then(async () => {
    // 自定义协议只提供界面目录中的静态资源，不允许目录跳转或访问用户文件。
    protocol.handle('msgx', request => {
      const url = new URL(request.url)
      const file = url.pathname.slice(1)
      if (url.hostname !== 'app' || !/^[a-z][\w-]*\.(html|js|css)$/.test(file)) {
        return new Response('', { status: 404 })
      }
      return net.fetch(pathToFileURL(path.join(resources, file)).href)
    })
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
    Menu.setApplicationMenu(null)
    app.setAppUserModelId('msgx.desktop')
    startBackend()
    const state = await backend('desktop.bootstrap')
    window = new BrowserWindow({
      title: '消息萃取', width: 1350, height: 860, minWidth: 820, minHeight: 590,
      icon: state.iconPath || path.join(root, 'assets', 'app-icon.ico'),
      backgroundColor: state.theme === 'light' ? '#fafafa' : '#1b1b1b',
      titleBarStyle: 'hidden', titleBarOverlay: titleBar(state.theme), show: false,
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
        sandbox: true, nodeIntegration: false }
    })
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    window.webContents.on('will-navigate', event => event.preventDefault())
    ipcMain.handle('msgx:call', desktopCall)
    window.on('close', event => {
      if (shutdown) return
      // 桥接进程已退出时，直接关闭窗口。否则渲染层无法完成保存握手。
      if (child.exitCode !== null || !child.stdin.writable) return
      event.preventDefault()
      if (closing) return
      closing = true
      window.webContents.send('msgx:close')
    })
    window.once('ready-to-show', () => window.show())
    await window.loadURL(entry)
  }).catch(error => {
    child?.stdin.end()
    dialog.showErrorBox('消息萃取启动失败', error.message)
    app.exit(1)
  })
  app.on('window-all-closed', () => app.quit())
}
