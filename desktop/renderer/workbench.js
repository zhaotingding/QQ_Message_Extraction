import { button, el } from './controls.js'

const paths = {
  status: 'M3 12h4l2-6 4 12 2-6h6',
  messages: 'M4 4h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9l-5 4v-4H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1ZM7 8h10M7 12h7',
  usage: 'M4 3v17h17M8 16v-4m5 4V8m5 8V5',
  reader: 'M12 6C9 4 6 3 3 4v14c3-1 6 0 9 2 3-2 6-3 9-2V4c-3-1-6 0-9 2Zm0 0v14',
  settings: 'M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-6 2v4',
  back: 'm14 6-6 6 6 6',
  forward: 'm10 6 6 6-6 6',
  sidebar: 'M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm10 0v16',
  close: 'm6 6 12 12M6 18 18 6',
}

function glyph(name) {
  const ns = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(ns, 'svg')
  const shape = document.createElementNS(ns, 'path')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('aria-hidden', 'true')
  shape.setAttribute('d', paths[name])
  svg.append(shape)
  return svg
}

export function iconButton(name, label, action) {
  return el('button', { className: 'icon-button', type: 'button', title: label, 'aria-label': label, onclick: action },
    glyph(name))
}

export function setupWorkbench(app, pages, show) {
  const navigation = document.querySelector('#navigation')
  const controls = document.querySelector('#title-controls')
  const inspector = document.querySelector('#inspector')
  const content = document.querySelector('#inspector-content')
  const history = []
  let cursor = -1
  const narrow = matchMedia('(max-width: 1099px)')
  let collapsed = null
  let menu
  let pageFocus
  const footerStatus = document.querySelector('#connection')

  const back = iconButton('back', '后退', () => travel(-1))
  const forward = iconButton('forward', '前进', () => travel(1))
  controls.append(back, forward)
  const toggle = iconButton('sidebar', '切换右侧面板', () => setCollapsed(!inspector.hidden))
  toggle.id = 'inspector-toggle'
  toggle.setAttribute('aria-controls', 'inspector')
  document.querySelector('#window-tools').append(toggle)
  document.querySelector('#inspector-tools').append(iconButton('close', '收起右侧面板', () => {
    setCollapsed(true)
    toggle.focus()
  }))

  function updateInspector() {
    const available = content.childElementCount > 0
    inspector.hidden = !available || (collapsed ?? narrow.matches)
    toggle.disabled = !available
    toggle.setAttribute('aria-expanded', String(!inspector.hidden))
  }

  function setCollapsed(value) {
    collapsed = value
    updateInspector()
  }

  app.setInspector = (node, title = '') => {
    content.replaceChildren(...(node ? [node] : []))
    document.querySelector('#inspector-title').textContent = title
    updateInspector()
  }
  app.showInspector = () => setCollapsed(false)
  updateInspector()
  narrow.addEventListener('change', updateInspector)

  function updateNavigation() {
    back.disabled = cursor <= 0
    forward.disabled = cursor < 0 || cursor === history.length - 1
    for (const item of navigation.children) {
      item.setAttribute('aria-current', item.dataset.page === history[cursor] ? 'page' : 'false')
    }
  }

  async function travel(step) {
    const next = cursor + step
    if (next < 0 || next >= history.length || !(await show(history[next], false))) return
    cursor = next
    updateNavigation()
  }

  function closeMenu(restoreFocus = false) {
    if (!menu) return
    menu.trigger.setAttribute('aria-expanded', 'false')
    menu.list.hidden = true
    if (restoreFocus) menu.trigger.focus()
    menu = undefined
  }

  function addMenu(label, entries) {
    const list = el('div', { className: 'menu-list', hidden: true })
    const trigger = button(label, () => {
      const alreadyOpen = menu?.trigger === trigger
      closeMenu()
      if (alreadyOpen) return
      menu = { trigger, list }
      trigger.setAttribute('aria-expanded', 'true')
      list.hidden = false
      list.querySelector('button')?.focus()
    }, { className: 'menu-trigger', 'aria-expanded': 'false' })
    for (const [label, action] of entries) {
      list.append(button(label, async () => {
        closeMenu()
        if (pageFocus?.isConnected) pageFocus.focus({ preventScroll: true })
        else trigger.focus()
        try { await action() } catch (error) { app.notify(error.message) }
      }))
    }
    controls.append(el('div', { className: 'title-menu' }, trigger, list))
  }

  addMenu('文件', [
    ['打开数据目录', () => app.call('status.openData')],
    ['打开偏好文件', () => app.call('desktop.openPreferences')],
    ['关闭窗口', () => app.call('desktop.requestClose')],
  ])
  addMenu('编辑', [
    ['复制', () => app.call('desktop.edit', { command: 'copy' })],
    ['粘贴', () => app.call('desktop.edit', { command: 'paste' })],
    ['全选', () => app.call('desktop.edit', { command: 'selectAll' })],
  ])
  addMenu('视图', [
    ...pages.map(([key, label]) => [label, () => show(key)]),
    ['切换右侧面板', () => { if (content.childElementCount) setCollapsed(!inspector.hidden) }],
  ])
  addMenu('帮助', [
    ['关于消息萃取', () => app.call('desktop.about', { version: app.state.version })],
  ])
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('.title-menu')) closeMenu()
  })
  document.querySelector('#pages').addEventListener('focusin', event => { pageFocus = event.target })
  document.addEventListener('keydown', event => {
    if (!menu) return
    if (event.key === 'Escape') {
      closeMenu(true)
      event.preventDefault()
    } else if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      const entries = [...menu.list.querySelectorAll('button')]
      const step = event.key === 'ArrowDown' ? 1 : -1
      entries[(entries.indexOf(document.activeElement) + step + entries.length) % entries.length]?.focus()
      event.preventDefault()
    }
  })

  for (const [key, label] of pages) {
    const item = iconButton(key, label, () => show(key))
    item.dataset.page = key
    navigation.append(item)
  }
  const statusButton = navigation.querySelector('[data-page="status"]')
  const light = el('span', { className: 'service-light', 'aria-hidden': 'true', 'data-state': 'idle' })
  statusButton.append(light)
  app.updateServiceStatus = (state, error) => {
    const health = state?.health
    const protocol = health?.protocol
    let color = 'idle'
    let label = '服务已停止'
    let reason = ''
    if (error) {
      color = 'fault'
      label = '状态查询失败'
      reason = error.message
    } else if (state.stopping) {
      label = '正在停止'
    } else if (state.recovering || state.failed) {
      color = 'fault'
      label = state.recovering ? '等待自动恢复' : '接收服务故障'
      reason = state.note
    } else if (state.starting) {
      label = '接收服务启动中'
    } else if (state.serviceRunning) {
      reason = protocol?.message || state.note
      if (health?.ok && protocol?.state === 'connected') {
        color = 'healthy'
        label = '协议已连接'
      } else if (health?.ok && protocol?.state === 'disconnected' && !health.ws_connected_at) {
        label = '等待首次协议连接'
      } else {
        color = 'fault'
        label = health?.ok ? '接收连接异常' : '接收服务无响应'
      }
    }
    light.dataset.state = color
    footerStatus.dataset.state = color
    if (footerStatus.textContent !== label) footerStatus.textContent = label
    statusButton.title = ['运行状态', label, reason && reason !== label ? reason : ''].filter(Boolean).join(' · ')
    statusButton.setAttribute('aria-label', '运行状态 · ' + label)
    return label
  }
  updateNavigation()
  return key => {
    history.splice(cursor + 1, history.length, key)
    cursor = history.length - 1
    updateNavigation()
  }
}
