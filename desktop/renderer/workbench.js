import { button, el } from './controls.js'

const paths = {
  power: 'M12 2.5v9M6.7 5.6a8.4 8.4 0 1 0 10.6 0',
  rocket: 'M14 6c3-3 7-3 7-3s0 4-3 7l-5 5-4-4 5-5ZM9 11H4l4-4h5m0 8v5l4-4v-5M7 14c-3 0-4 4-4 6 2 0 6-1 6-4M16 7h.01',
  status: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',
  logs: 'M4 3h12l4 4v14H4z M15 3v5h5 M7 12h2m2 0h6M7 16h2m2 0h6',
  folder: 'M3 6h7l2 2h9v12H3z',
  refresh: 'M20 5v5h-5M4 19v-5h5M5 9a8 8 0 0 1 14-3l1 4M4 14l1 4a8 8 0 0 0 14-3',
  external: 'M14 3h7v7m0-7L10 14M11 5H4v15h15v-7',
  search: 'M16 16l5 5M18 10a8 8 0 1 1-16 0 8 8 0 1 1 16 0',
  send: 'm2 11 19-8-6 18-4-7-9-3zm9 3L21 3',
  person: 'M16 7a4 4 0 1 1-8 0 4 4 0 1 1 8 0M4 21v-2a8 8 0 0 1 16 0v2',
  tag: 'M3 4h9l9 9-8 8L3 11zM7 8h.01',
  connection: 'M8 3v6m8-6v6M5 9h14v3a7 7 0 0 1-14 0zM12 19v3',
  groups: 'M15 7a3 3 0 1 1-6 0 3 3 0 1 1 6 0M5 20v-2a7 7 0 0 1 14 0v2M18 5a3 3 0 0 1 0 6m2 3a4 4 0 0 1 2 4',
  image: 'M3 4h18v16H3zM4 17l5-6 4 4 3-3 5 5M17 8a1 1 0 1 1-2 0 1 1 0 0 1 2 0',
  theme: 'M12 3a9 9 0 1 0 0 18V3zm0 0a9 9 0 0 1 0 18',
  save: 'M4 3h13l4 4v14H3V3zM7 3v6h9V3M7 21v-8h10v8',
  filter: 'M3 4h18l-7 8v7l-4 2v-9z',
  key: 'M15 4a5 5 0 1 1-2 9l-7 7H3v-4l7-7a5 5 0 0 1 5-5zM16 7h.01',
  messages: 'M4 4h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9l-5 4v-4H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1ZM7 8h10M7 12h7',
  usage: 'M4 3v17h17M8 16v-4m5 4V8m5 8V5',
  reader: 'M12 6C9 4 6 3 3 4v14c3-1 6 0 9 2 3-2 6-3 9-2V4c-3-1-6 0-9 2Zm0 0v14',
  settings: 'm10 2 4 0 .7 2.6 2 .9 2.4-.8 2 3.5-1.8 1.9.2 2.3 1.6 1.8-2 3.5-2.5-.6-1.9 1-1 2.6h-4l-.8-2.6-2-1-2.5.7-2-3.5 1.8-1.8v-2.3L2.5 8.3l2-3.5 2.6.8 2-.9z M15.3 11.8a3.3 3.3 0 1 1-6.6 0 3.3 3.3 0 1 1 6.6 0',
  back: 'm14 6-6 6 6 6',
  forward: 'm10 6 6 6-6 6',
  sidebar: 'M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm10 0v16',
  close: 'm6 6 12 12M6 18 18 6',
}

export function glyph(name) {
  const ns = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(ns, 'svg')
  const shape = document.createElementNS(ns, 'path')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('aria-hidden', 'true')
  shape.setAttribute('d', paths[name])
  svg.append(shape)
  return svg
}

export function decorateAction(element, icon) {
  element.classList.add('with-glyph')
  element.prepend(glyph(icon))
  return element
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
      list.querySelector('button:not([hidden])')?.focus()
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
    return { trigger, list }
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
  const help = addMenu('帮助', [
    ['检查更新', () => openUpdateDialog(true)],
    ['关于消息萃取', () => app.call('desktop.about', { version: app.state.version })],
  ])
  const updateDialog = el('dialog', { className: 'update-dialog', 'aria-labelledby': 'update-dialog-title' })
  const updateVersion = el('p', { className: 'update-version', hidden: true })
  const updateMessage = el('p', { className: 'update-message', role: 'status' })
  const updateProgress = el('progress', { max: 100, hidden: true, 'aria-label': '安装包下载进度' })
  const installUpdate = button('更新并重启', async () => {
    installUpdate.disabled = true
    try { showUpdate(await app.call('desktop.update.install')) }
    catch (error) { updateMessage.textContent = error.message; installUpdate.disabled = false }
  }, { className: 'primary', hidden: true })
  const checkUpdate = button('重新检查', () => openUpdateDialog(true))
  const closeUpdate = button('关闭', () => updateDialog.close())
  const updateShortcut = button('查看可用更新', () => {
    closeMenu()
    void openUpdateDialog(false)
  }, { hidden: true })
  help.list.insertBefore(updateShortcut, help.list.firstChild)
  updateDialog.append(
    el('h2', { id: 'update-dialog-title' }, '检查更新'),
    updateVersion, updateMessage, updateProgress,
    el('div', { className: 'update-actions' },
      checkUpdate, closeUpdate, installUpdate),
  )
  document.body.append(updateDialog)
  updateDialog.addEventListener('cancel', event => { if (closeUpdate.disabled) event.preventDefault() })

  function showUpdate(state) {
    const hasUpdate = Boolean(state.version)
    help.trigger.dataset.update = hasUpdate ? 'available' : ''
    help.trigger.setAttribute('aria-label', hasUpdate ? '帮助，有可用更新' : '帮助')
    updateShortcut.hidden = !hasUpdate
    updateVersion.hidden = !hasUpdate
    updateVersion.textContent = hasUpdate ? `可用版本 ${state.version}` : ''
    const downloading = state.phase === 'downloading'
    updateProgress.hidden = !downloading
    if (state.percent === null) updateProgress.removeAttribute('value')
    else updateProgress.value = state.percent
    updateMessage.textContent = downloading && state.percent !== null && state.percent < 100
      ? `${state.message} ${Math.floor(state.percent)}%`
      : state.message
    if (state.canInstall) updateMessage.textContent += ' 安装时会安全停止接收服务并重启界面。'
    installUpdate.hidden = !hasUpdate
    installUpdate.disabled = !state.canInstall
    checkUpdate.hidden = state.phase === 'unavailable' || hasUpdate
    checkUpdate.disabled = state.phase === 'checking'
    closeUpdate.disabled = state.phase === 'restarting'
    if (closeUpdate.disabled && !updateDialog.open) updateDialog.showModal()
  }

  async function openUpdateDialog(check) {
    if (!updateDialog.open) updateDialog.showModal()
    try { showUpdate(await app.call(check ? 'desktop.update.check' : 'desktop.update.status')) }
    catch (error) { updateMessage.textContent = error.message }
  }

  window.msgx.onUpdate(showUpdate)
  void app.call('desktop.update.status').then(showUpdate).catch(() => {})
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
      const entries = [...menu.list.querySelectorAll('button:not([hidden])')]
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
  let receiverRunning = false
  app.receiverRunning = () => receiverRunning
  app.updateServiceStatus = (state, error) => {
    receiverRunning = Boolean(state?.serviceRunning)
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
    statusButton.title = ['主页', label, reason && reason !== label ? reason : ''].filter(Boolean).join(' · ')
    statusButton.setAttribute('aria-label', '主页 · ' + label)
    return label
  }
  updateNavigation()
  return key => {
    history.splice(cursor + 1, history.length, key)
    cursor = history.length - 1
    updateNavigation()
  }
}
