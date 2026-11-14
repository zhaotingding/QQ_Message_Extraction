// 从项目 Conda 环境运行，使用合成资料检查实际 Electron、IPC 和接收进程。
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')

const root = path.resolve(__dirname, '..')
const desktop = path.join(root, 'desktop')
const runtime = path.join(desktop, 'node_modules', 'electron', 'dist', 'electron.exe')
const python = path.join(process.env.CONDA_PREFIX || '', 'python.exe')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))

async function port() {
  const server = net.createServer()
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve))
  const assigned = server.address().port
  await new Promise(resolve => server.close(resolve))
  return assigned
}

async function wait(check, label, attempts = 160) {
  for (let i = 0; i < attempts; i++) {
    const value = await check()
    if (value) return value
    await pause(150)
  }
  throw new Error(label)
}

async function run() {
  const diagnostics = path.join(root, 'data', 'diagnostics')
  await fs.mkdir(diagnostics, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(diagnostics, 'electron-smoke-'))
  const env = { ...process.env, MSGX_ROOT: isolated, MSGX_DESKTOP_PYTHON: python,
    PYTHONPATH: path.join(root, 'src'), PYTHONUTF8: '1', PYTHONNOUSERSITE: '1',
    DEEPSEEK_API_KEY: '', OPENAI_API_KEY: '' }
  delete env.ELECTRON_RUN_AS_NODE
  execFileSync(python, ['-s', path.join(__dirname, 'electron_reader_fixture.py'), isolated], { env, stdio: 'inherit' })
  const serviceLog = path.join(isolated, 'data', 'logs', 'service.log')
  const napcatLog = path.join(isolated, 'data', 'logs', 'napcat.log')
  await fs.appendFile(serviceLog, Array.from({ length: 120 }, (_, i) =>
    '2026-11-14 05:01:00 INFO    msgx 验收日志第 ' + (i + 1) + ' 行\n').join(''))
  await fs.writeFile(napcatLog, '2026-11-14 05:02:00 隔离协议端验收日志\n')
  const ws = await port()
  const http = await port()
  await fs.writeFile(path.join(isolated, 'config.yaml'), [
    'onebot:', '  host: 127.0.0.1', '  ws_port: ' + ws, '  http_port: ' + http,
    'llm:', '  enabled: false', 'billing:', '  platform_enabled: false',
    'watchdog:', '  enabled: false', 'napcat:', '  account: "100001"', '  dir: isolated-napcat',
    'api:', '  provider: "Electron fixture"', '  base_url: https://electron-fixture.invalid/v1',
    '  model: fixture-model'
  ].join('\n'))
  const gui = spawn(runtime, [desktop, '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0'], {
    env, cwd: root, windowsHide: false, stdio: ['ignore', 'ignore', 'pipe'],
  })
  let startup = ''
  gui.stderr.setEncoding('utf8').on('data', value => { startup += value })
  const exited = new Promise(resolve => gui.once('exit', resolve))
  let socket
  let protocol
  let evaluate
  try {
    const address = await wait(async () => {
      const marker = path.join(isolated, '.ui', 'electron', 'DevToolsActivePort')
      const value = await fs.readFile(marker, 'utf8').catch(() => '')
      return value.split('\n')[0].trim()
    }, '未取得 Electron 验收调试端口：' + isolated)
    const page = await wait(async () => {
      const pages = await (await fetch('http://127.0.0.1:' + address + '/json/list')).json()
      return pages.find(item => item.url === 'msgx://app/index.html')
    }, '界面未加载：' + startup)
    socket = new WebSocket(page.webSocketDebuggerUrl)
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true })
      socket.addEventListener('error', reject, { once: true })
    })
    let id = 0
    const pending = new Map()
    socket.addEventListener('message', event => {
      const result = JSON.parse(event.data)
      const resolve = pending.get(result.id)
      if (resolve) { pending.delete(result.id); resolve(result) }
    })
    const send = (method, params = {}) => new Promise(resolve => {
      const key = ++id
      pending.set(key, resolve)
      socket.send(JSON.stringify({ id: key, method, params }))
    })
    evaluate = async expression => {
      const response = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
      assert.ok(!response.error, JSON.stringify(response.error))
      const { result, exceptionDetails } = response.result
      if (exceptionDetails) throw new Error(result.description || exceptionDetails.text)
      return result.value
    }
    const light = state => wait(
      () => evaluate(`document.querySelector('[data-page="status"] .service-light')?.dataset.state === '${state}'`),
      '运行状态提示灯未切换到 ' + state,
    )
    async function capture(filename) {
      if (!process.argv.includes('--screenshots')) return
      const shot = await send('Page.captureScreenshot', { format: 'png' })
      await fs.writeFile(path.join(isolated, filename), Buffer.from(shot.result.data, 'base64'))
    }
    await wait(() => evaluate("document.querySelectorAll('#navigation button').length === 5"), '主导航未构建')
    await wait(() => evaluate("document.querySelector('#inspector-title').textContent === '自动摘要'"), '运行状态未挂载摘要侧栏')
    await light('idle')
    const activeLog = 'document.querySelector(".status-console .log:not([hidden])")'
    const atEnd = `(() => {
      const log = ${activeLog}
      return log.scrollHeight > log.clientHeight && log.scrollHeight - log.scrollTop - log.clientHeight < 2
    })()`
    await wait(() => evaluate(atEnd), '首次打开未定位到最后一屏日志')
    await capture('light-idle.png')
    assert.equal(await evaluate("navigator.windowControlsOverlay.visible"), true)
    assert.equal(await evaluate("document.querySelector('#navigation').offsetWidth"), 49)
    await evaluate("[...document.querySelectorAll('.menu-trigger')].find(b => b.textContent === '视图').click()")
    assert.equal(await evaluate("document.querySelector('.menu-trigger[aria-expanded=true]').textContent"), '视图')
    await evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
    assert.equal(await evaluate("document.querySelector('.menu-trigger[aria-expanded=true]') === null"), true)
    assert.equal(await evaluate('typeof require'), 'undefined')
    assert.equal(await evaluate("window.msgx.call('desktop.bootstrap').then(s => s.version)"), '1.0.5')
    // 用现有服务入口在隔离端口启动，随后走实际消息入库和在线消息列表。
    await evaluate("window.msgx.call('status.start')")
    await wait(() => evaluate("window.msgx.call('status.read').then(s => !!s.health.ok)"), '接收服务未就绪')
    await wait(() => evaluate("!!document.querySelector('.status-summary-heading .summary-failure-light')"), '摘要失败灯未出现在报告标题旁')
    assert.equal(await evaluate("document.querySelector('.summary-retry').closest('li').querySelector('strong').textContent"), '日报')
    assert.equal(await evaluate("document.querySelector('.summary-retry button').textContent"), '确认补生成')
    assert.match(await evaluate("document.querySelector('.summary-retry small').textContent"), /^\d{4}-\d{2}-\d{2} — \d{4}-\d{2}-\d{2}$/)
    assert.equal(await evaluate("document.querySelector('.status-summary').textContent.includes('隔离请求失败示例')"), false)
    await wait(() => evaluate("document.querySelector('.status-console .log').textContent.includes('模拟')"), '验收日志未加载')
    assert.ok(await evaluate(`(() => {
      const log = document.querySelector('.status-console .log')
      const has = (tone, text) => [...log.querySelectorAll('[data-tone=' + tone + ']')].some(s => s.textContent.includes(text))
      return has('pending', 'HTTP 200') && has('success', 'API 请求成功') &&
        has('failure', 'LLM 请求最终失败') && log.textContent.includes('正文中含有') &&
        ![...log.querySelectorAll('[data-tone]')].some(s => s.textContent.includes('正文中含有'))
    })()`), '日志蓝绿红状态与正文边界不正确')
    assert.deepEqual(await evaluate("[...document.querySelector('select[aria-label=日志来源]').options].map(o => o.text)"),
      ['服务日志', '协议端日志'])
    const selectLog = source => evaluate(`(() => {
      const selector = document.querySelector('select[aria-label=日志来源]')
      selector.value = '${source}'
      selector.dispatchEvent(new Event('change'))
    })()`)
    await selectLog('napcat')
    await wait(() => evaluate(`${activeLog}.textContent.includes('隔离协议端验收日志')`), '协议端日志未出现')
    assert.equal(await evaluate(`${activeLog}.textContent.includes('验收日志第 120 行')`), false)
    await selectLog('service')
    await wait(() => evaluate(atEnd), '切回服务日志未定位末尾')

    // 用窄窗和滚轮复现整页卷走的场景，同时验证历史位置与手动刷新。
    await send('Emulation.setDeviceMetricsOverride', { width: 900, height: 590, deviceScaleFactor: 1, mobile: false })
    const top = 'document.querySelector(".status-overview").getBoundingClientRect().top'
    const originalY = await evaluate(top)
    await evaluate(`${activeLog}.scrollTop = 0`)
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: 200, y: 360, deltaX: 0, deltaY: 480 })
    await wait(() => evaluate(`${activeLog}.scrollTop > 10`), '滚轮未滚动日志内容')
    const historyTop = await evaluate(`${activeLog}.scrollTop`)
    await fs.appendFile(serviceLog, '2026-11-14 05:03:00 INFO    msgx 验收新增日志\n')
    await wait(() => evaluate(`${activeLog}.textContent.includes('验收新增日志')`), '自动刷新未读到追加日志')
    assert.equal(await evaluate(`${activeLog}.scrollTop`), historyTop)
    assert.equal(await evaluate(top), originalY)
    assert.equal(await evaluate("document.querySelector('#pages').scrollTop"), 0)
    await evaluate("[...document.querySelectorAll('.status-console-bar button')].find(b => b.textContent === '刷新日志').click()")
    await wait(() => evaluate(atEnd), '手动刷新未回到最新')
    await fs.appendFile(serviceLog, '2026-11-14 05:03:01 INFO    msgx 验收贴底跟随\n')
    await wait(() => evaluate(`${activeLog}.textContent.includes('验收贴底跟随') && ${atEnd}`), '贴底时未跟随最新')
    await send('Emulation.clearDeviceMetricsOverride')
    console.log('两种日志切换、最新定位、滚轮固定顶部与保留历史位置通过')
    const response = await fetch('http://127.0.0.1:' + http + '/inject', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ post_type: 'message', message_type: 'group', self_id: 100001,
        group_id: 90001, user_id: 100002, message_id: 888, time: Math.trunc(Date.now() / 1000),
        sender: { nickname: '隔离发送者' }, message: [{ type: 'text', data: { text: 'Electron 实时验收入库' } }] }),
    })
    assert.equal(response.status, 200)
    assert.equal((await response.json()).results[0].ok, true)
    await evaluate("document.querySelector('[data-page=messages]').click()")
    try {
      await wait(() => evaluate("document.querySelector('#page-messages')?.textContent.includes('Electron 实时验收入库')"), '实时消息未显示')
    } catch (error) {
      throw new Error(error.message + '\n' + await evaluate("document.body.innerText.slice(0, 1500)") + '\n' + startup)
    }
    console.log('实际接收进程、隔离入库和消息页面通过')
    // 在消息页模拟协议连接与断开，检查顶层提示灯能跨页面更新。
    protocol = new WebSocket('ws://127.0.0.1:' + ws + '/onebot/ws', {
      headers: { 'X-Self-ID': '100001' },
    })
    await new Promise((resolve, reject) => {
      protocol.addEventListener('open', resolve, { once: true })
      protocol.addEventListener('error', () => reject(new Error('隔离协议连接失败')), { once: true })
    })
    await light('healthy')
    await capture('light-healthy.png')
    protocol.close()
    await light('fault')
    await capture('light-fault.png')
    await evaluate("[...document.querySelectorAll('.msgx-current tbody button')].find(b => b.textContent.includes('Electron 实时验收入库')).click()")
    await wait(() => evaluate("document.querySelector('#inspector-content').textContent.includes('Electron 实时验收入库')"), '消息详情未显示')
    await evaluate("document.querySelector('#inspector-toggle').click()")
    assert.equal(await evaluate("document.querySelector('#inspector').hidden"), true)
    await evaluate("[...document.querySelectorAll('.msgx-tab-bar button')].find(b => b.textContent === '消息详情').click()")
    assert.equal(await evaluate("document.querySelector('#inspector').hidden"), false)
    await capture('messages.png')
    await send('Emulation.setDeviceMetricsOverride', { width: 900, height: 640, deviceScaleFactor: 1, mobile: false })
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#inspector')).position"), 'absolute')
    await capture('narrow.png')
    await send('Emulation.clearDeviceMetricsOverride')
    await evaluate("document.querySelector('[aria-label=后退]').click()")
    await wait(() => evaluate("document.querySelector('#inspector-title').textContent === '自动摘要'"), '后退未切回状态页')
    await evaluate("document.querySelector('[aria-label=前进]').click()")
    await wait(() => evaluate("document.querySelector('#inspector-title').textContent === '消息详情'"), '前进未切回消息页')
    console.log('标题栏、菜单、前进后退、详情切换与窄窗侧栏通过')
    console.log(await require('./electron_reader_smoke.cjs')(evaluate, capture))
    assert.match(await fs.readFile(path.join(isolated, '.ui', 'preferences.yaml'), 'utf8'),
      /reader_kinds:\s*\r?\n- weekly\r?\n- monthly/)
    await capture('reader-filters.png')
    console.log(await require('./electron_settings_smoke.cjs')(evaluate))
    console.log(await require('./electron_messages_smoke.cjs')(evaluate))
    await evaluate("document.querySelector('[data-page=status]').click()")
    await wait(() => evaluate("document.querySelector('#page-status .metrics strong')?.textContent !== '—' && !!document.querySelector('#page-status .metrics strong')"), '运行状态未显示')
    await capture('status.png')
    await evaluate("window.msgx.call('status.stop')")
    await wait(() => evaluate("window.msgx.call('status.read').then(s => !s.serviceRunning)"), '接收服务未停止')
    await light('idle')
    console.log('运行状态提示灯白、绿、红、白切换通过')
    void send('Page.close')
    await Promise.race([exited, pause(5000).then(() => { if (gui.exitCode == null) throw new Error('窗口未退出') })])
    console.log('隔离接收进程已停止，窗口已退出：' + isolated)
  } finally {
    protocol?.close()
    if (gui.exitCode == null) {
      if (evaluate) await evaluate("window.msgx.call('status.stop')").catch(() => {})
      gui.kill()
    }
    socket?.close()
  }
}

run().catch(error => { console.error(error); process.exitCode = 1 })
