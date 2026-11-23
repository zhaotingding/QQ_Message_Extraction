import { el, button } from './controls.js'
import { decorateAction } from './workbench.js'

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
  let live = true, logReading = false, logRequest = 0
  const logLabels = { service: '服务日志', napcat: '协议端日志' }
  const logSource = el('select', { 'aria-label': '日志来源' },
    Object.entries(logLabels).map(([value, title]) => el('option', { value }, title)))
  const logs = Object.fromEntries(Object.entries(logLabels).map(([source, title]) => [
    source, { node: el('pre', { className: 'log', tabIndex: 0, 'aria-label': title,
      hidden: source !== logSource.value }, '正在读取日志…'), text: undefined },
  ]))
  logSource.onchange = () => {
    for (const [source, view] of Object.entries(logs)) view.node.hidden = source !== logSource.value
    const log = logs[logSource.value].node
    log.scrollTop = log.scrollHeight
    void readLog(true)
  }

  container.classList.add('logs-page')
  container.append(
    el('section', { className: 'status-console', 'aria-label': '运行日志' },
      el('div', { className: 'status-console-bar' },
        el('div', { className: 'toolbar' }, logSource, decorateAction(button('刷新日志', () => readLog(true)), 'refresh'),
          decorateAction(button('打开日志目录', () => app.call('status.openLogs').catch(error => app.notify(error.message))), 'folder'))),
      Object.values(logs).map(view => view.node)),
  )
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

  void readLog(true)
  const timer = setInterval(() => { if (!document.hidden) void readLog() }, 2000)
  return () => { live = false; clearInterval(timer) }
}
