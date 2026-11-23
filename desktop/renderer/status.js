import { el, button } from './controls.js'
import { glyph } from './workbench.js'

const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short' })
const date = value => value ? dateFormat.format(new Date(value * 1000)) : '—'
const yuan = value => value == null ? '—' : '¥' + Number(value).toFixed(2)
const sources = { platform: '平台', balance_diff: '余额差值', estimate: '估算' }
const summaries = { daily: '日报', weekly: '周报', monthly: '月报' }

function logTone(line) {
  // 只识别日志记录头。聊天和摘要的续行可能包含“失败”等词。
  const match = /^(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}\s+(?:launcher_pid=\d+\s+)?(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\S+\s+(.*)$/.exec(line)
  if (!match) return ''
  const [, level, body] = match
  if (level === 'ERROR' || level === 'CRITICAL') return 'failure'
  if (/^(?:API 内容摘要 ·|已把 .*重新排入处理队列)/.test(body)) return ''
  if (/^(?:LLM 请求(?:最终)?失败|API (?:请求(?:失败|被拒绝|中断)|输出未完成)|自动(?:日|周|月)报失败)/.test(body)
      || /^API 响应头已收到：HTTP [45]\d\d\b/.test(body)
      || (level === 'WARNING' && /失败|中断|超时|异常|错误|离线/.test(body))) return 'failure'
  if (/^(?:API (?:开始上传|请求体已提交|响应头已收到)|自动摘要后台任务已接受|摘要生成进度：|正在(?:请求|生成|接收))/.test(body)) return 'pending'
  if (/^(?:API 请求成功|自动(?:日|周|月)报已完成|消息萃取服务已启动|协议端已连接)/.test(body)
      || /^(?:日摘要|日报|周报|月报).*已生成（/.test(body)) return 'success'
  return ''
}

export function mount(container, app) {
  let live = true
  let loading = false
  let busy = false
  let pendingAction = ''
  let statusEpoch = 0
  let qrMtime
  let status
  let billing
  let logRequest = 0
  let logReading = false
  let shownReports
  const note = el('p', { className: 'muted', role: 'status' })
  const qr = el('dialog', { className: 'qr-dialog' })
  const qrHint = el('p', { className: 'muted' })
  const qrImage = el('img', { alt: '目标 QQ 登录二维码', width: 220, height: 220 })
  qr.append(el('h2', {}, '扫描登录二维码'), qrImage, qrHint, button('关闭', () => qr.close()))
  const metrics = el('div', { className: 'metrics' })
  const mode = el('p', { className: 'muted' })
  const reports = el('ul', { className: 'status-summary-list' })
  const schedule = el('p', { className: 'muted', role: 'status' })
  const logLabels = { service: '服务日志', napcat: '协议端日志' }
  const logSource = el('select', { 'aria-label': '日志来源' },
    Object.entries(logLabels).map(([value, title]) => el('option', { value }, title)))
  const logs = Object.fromEntries(Object.entries(logLabels).map(([source, title]) => [
    source, { node: el('pre', { className: 'log', tabIndex: 0, 'aria-label': title,
      hidden: source !== logSource.value }, '正在读取日志…'), text: undefined },
  ]))
  const switchLabel = el('span', { id: 'service-power-action', className: 'service-power-label' }, '启动服务')
  const serviceSwitch = el('button', { type: 'button', className: 'service-switch', role: 'switch',
    'aria-label': '接收服务电源', 'aria-describedby': 'service-power-action',
    onclick: () => operate(status?.serviceRunning || status?.recovering ? 'status.stop' : 'status.start') }, glyph('power'))
  const powerControl = el('div', { className: 'service-power-control' }, serviceSwitch, switchLabel)
  const restart = button('强制重启协议端', async () => {
    if (!status?.account && !status?.managedProtocol) return app.notify('请先保存目标 QQ 号。')
    const account = status.account
    const message = status.managedProtocol ? '重启本应用的内置协议端，重新显示登录二维码。'
      : '重启目标 QQ ' + account + ' 的 NapCat，会先检查接收服务，随后可能需要 UAC 授权。'
    if (await app.confirm('强制重启协议端', message)) {
      await operate('status.restart', { account })
    }
  })
  restart.classList.add('service-restart')
  restart.prepend(glyph('rocket'))
  const bill = button('刷新账单', () => operate('status.billing'))
  logSource.onchange = () => {
    for (const [source, view] of Object.entries(logs)) view.node.hidden = source !== logSource.value
    const log = logs[logSource.value].node
    log.scrollTop = log.scrollHeight
    void readLog(true)
  }

  // 日志占据主要工作区，摘要调度随状态页显示在右侧。
  container.classList.add('status-page')
  container.append(
    el('section', { className: 'status-overview', 'aria-label': '消息接收' },
      el('div', { className: 'status-commandbar' },
        el('div', { className: 'toolbar', role: 'group', 'aria-label': '接收服务' }, powerControl, restart),
        el('div', { className: 'toolbar', role: 'group', 'aria-label': '账单与资料' },
          bill, button('官方平台', () => operate('status.openBilling')),
          button('打开数据目录', () => operate('status.openData')))),
      note, metrics, mode),
    el('section', { className: 'status-console', 'aria-label': '运行日志' },
      el('div', { className: 'status-console-bar' }, el('h2', {}, '运行日志'),
        el('div', { className: 'toolbar' }, logSource, button('刷新日志', () => readLog(true)),
          button('打开日志目录', () => operate('status.openLogs')))),
      Object.values(logs).map(view => view.node)),
    qr,
  )
  app.setInspector(el('section', { className: 'status-summary', 'aria-label': '自动摘要' },
    schedule, reports), '自动摘要')

  function enable() {
    const running = Boolean(status?.serviceRunning || status?.recovering)
    serviceSwitch.setAttribute('aria-checked', String(running))
    serviceSwitch.disabled = busy || !status || status.starting || status.stopping
    switchLabel.textContent = pendingAction === 'status.stop' || status?.stopping ? '正在停止…'
      : pendingAction === 'status.start' || status?.starting ? '正在启动…' : running ? '停止服务' : '启动服务'
    serviceSwitch.title = switchLabel.textContent
    restart.disabled = busy || !status || status.stopping || (!status.account && !status.managedProtocol)
    bill.disabled = busy || !status?.health?.ok
  }

  async function operate(action, payload) {
    if (busy) return
    busy = true
    pendingAction = action
    statusEpoch++
    enable()
    try {
      const result = await app.call(action, payload)
      if (action === 'status.start' || action === 'status.stop') {
        statusEpoch++
        status = null
      }
      if (action === 'status.billing') billing = result
      if (result.note || result.message) app.notify(result.note || result.message)
      await refresh()
    } catch (error) {
      app.notify(error.message)
    } finally {
      busy = false
      pendingAction = ''
      enable()
    }
  }

  async function readLog(showLatest = false) {
    if (logReading && !showLatest) return
    const source = logSource.value
    const request = ++logRequest
    const view = logs[source]
    const log = view.node
    logReading = true
    try {
      const text = await app.call('status.log', { source })
      if (!live || request !== logRequest) return
      const position = log.scrollTop
      // 贴底时跟随新日志，浏览历史时保留滚动位置。
      const atBottom = log.scrollHeight - position - log.clientHeight < 20
      const follow = showLatest || view.text === undefined || atBottom
      const current = text || '暂无日志'
      if (view.text !== current) {
        view.text = current
        const fragment = document.createDocumentFragment()
        const lines = current.split(/\r?\n/)
        for (const [index, line] of lines.entries()) {
          const tone = logTone(line)
          fragment.append(tone ? el('span', { 'data-tone': tone }, line) : line)
          if (index < lines.length - 1) fragment.append('\n')
        }
        log.replaceChildren(fragment)
      }
      log.scrollTop = follow ? log.scrollHeight : position
    } catch (error) {
      if (live && request === logRequest) {
        view.text = undefined
        log.replaceChildren(el('span', { 'data-tone': 'failure' }, error.message))
      }
    } finally {
      if (request === logRequest) logReading = false
    }
  }

  function renderReports(health) {
    const task = health.summary_schedule || {}
    const { failures = [], next_runs: nextRuns = {} } = task
    const latest = health.latest_reports || {}
    const signature = JSON.stringify([health.ok, task.state, task.kind, latest, nextRuns, failures])
    if (shownReports === signature) return
    shownReports = signature
    // 右栏顶部只显示调度进度，任务错误和补生成归到对应报告。
    const pending = { pending: '等待调度', waiting: '等待前置摘要', settling: '正在等待消息入库',
      running: '正在生成' }
    schedule.textContent = health.ok ? pending[task.state] || '' : '服务停止时暂停调度。'
    schedule.dataset.tone = pending[task.state] && health.ok ? 'pending' : ''
    reports.replaceChildren(...Object.entries(summaries).map(([kind, label]) => {
      const item = latest[kind]
      const next = nextRuns[kind]
      const blocked = failures.filter(job => job.kind === kind && job.state === 'blocked')
        .sort((a, b) => b.end_date.localeCompare(a.end_date))
      const latestFailure = blocked.find(job => !item || job.end_date > item.end_date)
      const failureTitle = (job) => ['生成失败', job.start_date + ' — ' + job.end_date, job.error].filter(Boolean).join(' · ')
      const marker = job => el('span', { className: 'summary-failure-light', role: 'img',
        title: failureTitle(job), 'aria-label': failureTitle(job) })
      const waitingRetry = failures.find(job => job.kind === kind && job.state === 'retry')
      const progress = task.state === 'running' && task.kind === kind ? '正在生成' : waitingRetry ? '等待自动重试' : ''
      return el('li', {},
        el('div', { className: 'status-summary-heading' },
          el('strong', {}, label, latestFailure && marker(latestFailure)),
          item && el('small', {}, item.chars + ' 字')),
        el('span', { className: 'status-summary-period' }, item ? [
          el('time', {}, date(item.window_start)), ' — ', el('time', {}, date(item.window_end)),
        ] : '暂无完成记录'),
        progress && el('small', { 'data-tone': 'pending' }, progress),
        next && el('small', {}, '下次 ' + dateFormat.format(new Date(next))),
        blocked.map(job => el('div', { className: 'summary-retry', title: failureTitle(job) },
          el('small', { 'data-tone': 'failure' }, job === latestFailure ? null : marker(job),
            job.start_date + ' — ' + job.end_date),
          button('确认补生成', async () => {
            if (await app.confirm('补生成摘要', '补生成 ' + job.start_date + ' — ' + job.end_date + ' 的摘要。原请求可能已计费，重新生成可能重复收费。')) {
              await operate('status.retrySummary', job)
            }
          }))))
    }))
  }

  async function refresh() {
    if (!live || loading) return
    loading = true
    const epoch = statusEpoch
    try {
      const state = await app.call('status.read', { qrMtime })
      if (!live || epoch !== statusEpoch) return
      status = state
      const h = state.health
      const b = h.billing?.ts >= (billing?.ts || 0) ? h.billing : billing || h.billing || {}
      const protocol = h.protocol || {}
      const connection = app.updateServiceStatus(state)
      const accountHint = state.managedProtocol ? '扫码后自动识别 QQ' : '请在设置中保存目标 QQ 号'
      const protocolHint = state.managedProtocol && !h.ws_connected_at ? '' : protocol.message || protocol.detail
      note.textContent = [connection, state.account ? '目标 QQ ' + state.account : accountHint,
        protocolHint, h.ok ? '' : state.note, state.login.note].filter(Boolean).join(' · ')
      const entries = [
        ['入库消息', h.stats?.messages ?? '—'],
        ['当前连接', h.ws_conns ?? '—'],
        ['今日消费', yuan(b.today_cny ?? h.spent_today_cny), sources[b.today_source || b.source] || (h.ok ? '估算' : '')],
        ['累计消费', yuan(b.total_cny), sources[b.total_source || b.source]],
        ['账户余额', b.balance?.ok ? yuan(b.balance.total) : '—'],
      ]
      metrics.replaceChildren(...entries.map(([label, value, detail]) => el('div', {},
        el('span', { className: 'muted' }, label), el('strong', {}, value), detail && el('small', {}, detail))))
      mode.textContent = [
        h.ok ? [h.api_provider, h.api_model, h.llm_available ? '模型可用' : '模型未配置'].filter(Boolean).join(' · ') : '',
        b.platform_error, h.image_analysis?.message,
      ].filter(Boolean).join(' · ')
      renderReports(h)
      if (state.login.qr) {
        qrImage.src = state.login.qr.data
        qrMtime = state.login.qr.mtime
        if (!qr.open) qr.showModal()
      } else if (!state.login.pending && qr.open) qr.close()
      qrHint.textContent = state.login.note
      enable()
      await readLog()
    } catch (error) {
      if (live) {
        app.updateServiceStatus(null, error)
        note.textContent = error.message
      }
    } finally {
      loading = false
    }
  }

  enable()
  void refresh()
  const timer = setInterval(refresh, 2000)
  return () => { live = false; clearInterval(timer) }
}
