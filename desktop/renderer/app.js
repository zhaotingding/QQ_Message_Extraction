import { button, el, confirmAction } from './controls.js'
import { setupWorkbench } from './workbench.js'

const pages = [
  ['status', '主页'],
  ['messages', '实时消息'],
  ['logs', '运行日志'],
  ['usage', '用量明细'],
  ['reader', '摘要阅读'],
  ['settings', '设置'],
]
const container = document.querySelector('#pages')
const feedback = document.querySelector('#feedback')
let noticeTimer
let active
let switching = false
const mounted = new Map()
const app = {
  state: {},
  async call(action, payload = {}) {
    try {
      return await window.msgx.call(action, payload)
    } catch (error) {
      throw new Error(error.message.replace(/^Error invoking remote method '[^']+': Error: /, ''))
    }
  },
  navigate: key => show(key),
  confirm: confirmAction,
  setTheme(name) {
    app.state.theme = name === 'light' ? 'light' : 'dark'
    document.documentElement.dataset.theme = app.state.theme
    void app.call('desktop.windowTheme', { theme: app.state.theme }).catch(error => app.notify(error.message))
  },
  notify(text) {
    feedback.textContent = String(text)
    feedback.hidden = false
    clearTimeout(noticeTimer)
    noticeTimer = setTimeout(() => { feedback.hidden = true }, 7000)
  },
}
const recordPage = setupWorkbench(app, pages, show)
let readingService = false

// 运行状态页已有轮询，离开后由工作台继续刷新图标。
async function refreshServiceLight() {
  if (!active || active === 'status' || readingService) return
  readingService = true
  try {
    const state = await app.call('status.read')
    if (active !== 'status') app.updateServiceStatus(state)
  } catch (error) {
    if (active !== 'status') app.updateServiceStatus(null, error)
  } finally {
    readingService = false
  }
}

async function show(key, record = true) {
  if (switching || active === key) return false
  switching = true
  try {
    if (active && active !== 'settings') {
      const previous = mounted.get(active)
      await previous.cleanup?.()
      previous.node.remove()
      mounted.delete(active)
    }
    for (const page of mounted.values()) page.node.hidden = true
    app.setInspector(null)
    active = key
    document.querySelector('#title').textContent = pages.find(([id]) => id === key)[1]
    if (record) recordPage(key)
    if (mounted.has(key)) {
      mounted.get(key).node.hidden = false
      return true
    }
    const node = el('section', { className: 'page', id: 'page-' + key })
    container.replaceChildren(...Array.from(mounted.values(), page => page.node), node)
    const page = { node }
    mounted.set(key, page)
    try {
      const module = await import('./' + key + '.js')
      page.cleanup = await module.mount(node, app)
    } catch (error) {
      node.replaceChildren(el('p', { className: 'error' }, error.message),
        button('重新打开页面', () => { active = undefined; node.remove(); mounted.delete(key); void show(key, false) }))
    }
    return true
  } catch (error) {
    app.notify(error.message)
    return false
  } finally {
    switching = false
  }
}

window.msgx.onClose(async ({ forUpdate } = {}) => {
  try {
    if (switching) throw new Error('页面正在切换，请稍后关闭。')
    if (!forUpdate) {
      const status = await app.call('status.read')
      if (status.serviceRunning && !(await app.confirm('接收服务仍在运行', '停止接收服务并关闭界面？'))) {
        await app.call('desktop.cancelClose')
        return
      }
    }
    if (active === 'reader') await mounted.get(active)?.cleanup?.({ keepMounted: true })
    await app.call('desktop.finishClose')
  } catch (error) {
    app.notify(error.message)
    await app.call('desktop.cancelClose')
  }
})

async function main() {
  app.state = await app.call('desktop.bootstrap')
  app.setTheme(app.state.theme)
  document.querySelector('#version').textContent = 'v' + app.state.version + ' · Electron'
  if (app.state.setupPending) {
    const { showFirstRun } = await import('./first-run.js')
    showFirstRun(app)
  }
  await show('status')
  setInterval(refreshServiceLight, 2000)
}

main().catch(error => { container.textContent = error.message })
