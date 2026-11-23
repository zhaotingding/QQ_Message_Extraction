import { el, button } from './controls.js'
import { glyph, decorateAction } from './workbench.js'
import { mount as mountMessages } from './messages.js'

const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short' })
const date = value => value ? dateFormat.format(new Date(value * 1000)) : '—'
const yuan = value => value == null ? '—' : '¥' + Number(value).toFixed(2)
const sources = { platform: '平台', balance_diff: '余额差值', estimate: '估算' }
const summaries = { daily: '日报', weekly: '周报', monthly: '月报' }

export function mount(container, app) {
  let live = true
  let loading = false
  let busy = false
  let pendingAction = ''
  let statusEpoch = 0
  let qrMtime
  let status
  let billing
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
  container.classList.add('status-page')
  const messages = el('section', { className: 'home-feed', 'aria-label': '最近消息' })
  const folder = button('打开数据目录', () => operate('status.openData'))
  const platform = button('官方平台', () => operate('status.openBilling'))
  decorateAction(bill, 'refresh'); decorateAction(folder, 'folder'); decorateAction(platform, 'external')
  const openLogs = decorateAction(button('运行日志', () => app.navigate('logs')), 'logs')
  container.append(el('div', { className: 'status-commandbar' },
    el('div', { className: 'toolbar', role: 'group', 'aria-label': '接收服务' }, powerControl, restart), openLogs), messages, qr)
  const detail = el('section', { className: 'home-detail', hidden: true })
  app.setInspector(el('div', { className: 'home-inspector' },
    el('section', { className: 'status-overview' }, note, metrics, mode,
      el('div', { className: 'toolbar' }, bill, platform, folder)), detail,
    el('section', { className: 'status-summary', 'aria-label': '自动摘要' }, el('h3', {}, '自动摘要'), schedule, reports)), '接收概览')
  const cleanupMessages = mountMessages(messages, { ...app, setInspector: content => detail.replaceChildren(...(content ? [content] : [])) },
    { listOnly: true, onSelect: () => { detail.hidden = false; app.showInspector() } })

  function enable() {
    const running = Boolean(status?.serviceRunning || status?.recovering)
    serviceSwitch.setAttribute('aria-checked', String(running))
    serviceSwitch.disabled = busy || !status || status.starting || status.stopping || (!running && status.protocolSkipped)
    switchLabel.textContent = pendingAction === 'status.stop' || status?.stopping ? '正在停止…'
      : pendingAction === 'status.start' || status?.starting ? '正在启动…' : running ? '停止服务' : '启动服务'
    serviceSwitch.title = switchLabel.textContent
    restart.disabled = busy || !status || status.stopping || status.protocolSkipped || (!status.account && !status.managedProtocol)
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
      const accountHint = state.protocolSkipped ? '未安装 QQ 接收组件，可在安装向导补装' : state.managedProtocol ? '扫码后自动识别 QQ' : '请在设置中保存目标 QQ 号'
      const protocolHint = state.managedProtocol && !h.ws_connected_at ? '' : protocol.message || protocol.detail
      note.textContent = [connection, state.account && !state.protocolSkipped ? '目标 QQ ' + state.account : accountHint,
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
  return () => { live = false; clearInterval(timer); cleanupMessages() }
}
