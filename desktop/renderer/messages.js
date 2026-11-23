// 实时消息、历史定位与逐群操作。所有群消息和 OCR 内容只作为文本绘制。
import { decorateAction } from './workbench.js';
const OPERATION_LABELS = {
  pending: '待发送', sending: '已提交，等待结果', sent: '发送成功',
  unknown: '结果未知', failed: '明确失败', not_sent: '未发送',
  left: '已退群，清理未完成', cleaned: '已退群并清理',
};
const time = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function control(label, action) {
  const button = node('button', label);
  button.type = 'button';
  button.onclick = action;
  return button;
}

function choice(caption, choices, initial) {
  const label = node('label', caption);
  const input = document.createElement('select');
  for (const [value, text] of choices) input.add(new Option(text, value));
  input.value = initial;
  label.append(input);
  return { label, input };
}

function toggle(caption, checked = false) {
  const label = node('label', null, 'msgx-check');
  const input = node('input');
  input.type = 'checkbox';
  input.checked = checked;
  label.append(input, caption);
  return { label, input };
}

function formatTime(seconds) {
  return Number(seconds) > 0 ? time.format(Number(seconds) * 1000) : '—';
}

function groupName(row) {
  return row.group_name || row.group_id || '—';
}

function snippet(row) {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const original = clean(row.plain_text || row.text);
  const picture = clean(row.vision_text || row.ocr_text);
  if (row.has_media && picture) {
    return `${original.slice(0, 100)} [图片] ${row.vision_text ? '语义' : 'OCR'}：${picture.slice(0, 180)}`.trim();
  }
  return original.slice(0, 300) || (row.has_media ? '[图片]' : '（无文本）');
}

function mediaLabel(row) {
  if (row.auto_state === 'running') return '识别中';
  if (row.media_state === 'filtered') return row.media_path ? '已过滤' : 'OCR 处理中';
  if (row.vision_state === 'download_failed' || row.media_state === 'failed') return '下载失败';
  if (row.media_state === 'pending') return '待处理';
  if (row.vision_state === 'failed') return '图片未识别';
  if (row.vision_state === 'not_sent') return '识别请求未提交';
  if (row.vision_state === 'rejected') return '请求被拒绝';
  if (row.vision_text) return '已识别';
  if (row.ocr_text) return '仅 OCR';
  return row.has_media ? '已跳过' : '—';
}

// 匹配及截取在 Python 完成，浏览器仅将命中片段作为纯文本标绿。
function keywordExcerpt(row, action) {
  const item = control('', action);
  item.className = 'keyword-excerpt';
  for (const [text, hit] of row.keyword_excerpt) {
    item.append(hit ? node('mark', text) : document.createTextNode(text));
  }
  return item;
}

function keywordLabels(row) {
  const group = node('div');
  for (const { rule, examples } of row.keyword_labels) {
    const label = examples.join('、') || (rule.startsWith('re:') ? '正则匹配' : rule);
    group.append(Object.assign(node('span', label, 'keyword-label'), { title: rule }));
  }
  return group;
}

function imageCell(row) {
  const state = mediaLabel(row);
  if (!row.image_id) return state;
  const recognition = row.vision_text ? '已识别' :
    ['已过滤', '仅 OCR', '已跳过'].includes(state) ? '待识别' : '';
  const labels = [...new Set([state, recognition, row.include_vision ? '已纳入摘要' : '未纳入摘要'].filter(Boolean))];
  const text = labels.join(' · ');
  return Object.assign(node('span', text), { title: text });
}

function expandImages(rows) {
  // 每张图片保留自己的 OCR、状态及当前纳入摘要的选择。
  return rows.flatMap(row => row.images?.length
    ? row.images.map((image, index) => ({
      ...row, ...image, id: `${row.id}:${image.id}`, parent_id: row.id, image_id: image.id,
      image_index: index + 1, image_count: row.images.length,
      media_state: row.media_state === 'filtered' ? 'filtered' : image.media_state,
    }))
    : [{ ...row, id: String(row.id), parent_id: row.id }]);
}

function grid(headers) {
  const wrap = node('div', null, 'msgx-table-scroll');
  const table = node('table', null, 'msgx-table');
  const head = table.createTHead().insertRow();
  for (const heading of headers) {
    const th = node('th');
    th.scope = 'col';
    th.append(heading);
    head.append(th);
  }
  const body = table.createTBody();
  wrap.append(table);
  return { wrap, body, count: headers.length };
}

function fill(gridView, rows, empty) {
  const previousTop = gridView.wrap.scrollTop;
  const anchor = previousTop > 0
    ? [...gridView.body.rows].find(row => row.offsetTop + row.offsetHeight > previousTop)
    : null;
  const anchorId = anchor?.dataset.key;
  const offset = anchor ? previousTop - anchor.offsetTop : 0;
  const focused = gridView.body.contains(document.activeElement) ? document.activeElement.closest('td') : null;
  const focusedRow = focused?.parentElement.dataset.key;
  const fragment = document.createDocumentFragment();
  if (!rows.length) {
    const row = node('tr');
    const td = node('td', empty, 'msgx-empty');
    td.colSpan = gridView.count;
    row.append(td);
    fragment.append(row);
  }
  let nextAnchor, nextFocus;
  for (const { id, cells, className } of rows) {
    const row = node('tr', null, className);
    row.dataset.key = id;
    if (String(id) === anchorId) nextAnchor = row;
    for (const cell of cells) {
      const td = node('td');
      td.append(cell instanceof Node ? cell : String(cell ?? ''));
      row.append(td);
    }
    if (focused && row.dataset.key === focusedRow) nextFocus = row.cells[focused.cellIndex]?.querySelector('button');
    fragment.append(row);
  }
  gridView.body.replaceChildren(fragment);
  gridView.wrap.scrollTop = nextAnchor ? nextAnchor.offsetTop + offset : previousTop;
  nextFocus?.focus({ preventScroll: true });
}

function makeDialog(container, title) {
  const dialog = node('dialog', null, 'msgx-modal');
  const heading = node('h2', title);
  dialog.append(heading);
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  container.append(dialog);
  return dialog;
}

function drawTrend(canvas, trend) {
  const width = 720, height = 160;
  canvas.width = width * 2;
  canvas.height = height * 2;
  const ctx = canvas.getContext('2d');
  ctx.scale(2, 2);
  const colors = getComputedStyle(canvas);
  const foreground = colors.color;
  const values = trend.counts || [];
  if (!trend.group_id || !values.length) return;
  canvas.setAttribute('aria-label', trend.dates.map((day, i) => `${day}：${values[i]} 条`).join('，'));
  const peak = Math.max(1, ...values);
  const left = 46, right = width - 24, upper = 18, lower = height - 30;
  const x = i => left + (right - left) * i / Math.max(1, values.length - 1);
  const y = value => lower - (lower - upper) * value / peak;
  ctx.font = '12px system-ui';
  ctx.fillStyle = foreground;
  ctx.strokeStyle = foreground;
  for (let i = 0; i <= 4; i++) {
    const value = Math.round(peak * i / 4);
    const line = y(value);
    ctx.globalAlpha = 0.18;
    ctx.beginPath();
    ctx.moveTo(left, line);
    ctx.lineTo(right, line);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillText(value, 5, line + 4);
  }
  ctx.strokeStyle = colors.getPropertyValue('--accent').trim() || '#5c8ff5';
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((value, i) => {
    if (i === 0) ctx.moveTo(x(i), y(value));
    else ctx.lineTo(x(i), y(value));
  });
  ctx.stroke();
  trend.dates.forEach((day, i) => {
    if (i !== values.length - 1 && i % Math.ceil(values.length / 7)) return;
    ctx.fillText(day.slice(5), x(i) - 16, height - 8);
  });
}

export function mount(container, app, { listOnly = false, onSelect } = {}) {
  const stateKey = listOnly ? 'homeMessages' : 'messages';
  const saved = app.state[stateKey] || {};
  let disposed = false;
  let rows = [];
  let relatedRows = [];
  let keywordRows = [];
  let activity = {};
  let account = '';
  const imageBusy = saved.imageBusy || new Set();
  let activeView = listOnly ? 'list' : ['related', 'keywords', 'broadcast'].includes(saved.activeView) ? saved.activeView : 'activity';
  let selectedId = null;
  let selectedDetails;
  let contextId = null;
  let groupForTrend = saved.trend || '';
  let ascending = Boolean(saved.ascending);
  let keywordOffset = saved.keywordOffset || 0;
  let loadVersion = 0;
  let contextVersion = 0;
  let keywordVersion = 0;
  let keywordRequest = null;
  let keywordQueued = false;
  let pendingKey = '';
  let refreshQueued = false;
  let liveAvailable = true;
  let job = null;
  let operationBusy = true;
  let jobInitialized = false;
  let jobVersion = 0;
  let jobTimer;
  const page = node('section', null, 'msgx-messages');
  container.append(page);

  const filter = choice('显示', [['all', '全部消息'], ['images', '仅看图片']], saved.filter || 'all');
  const group = choice('群', [['', '全部群'], ...(saved.group ? [[saved.group, saved.group]] : [])], saved.group || '');
  let sender = saved.sender || null;
  const highlight = { group: '', sender: '' };
  const senderChip = control('', () => { sender = null; highlight.sender = ''; changeFilter(); });
  senderChip.className = 'filter-chip';
  senderChip.title = '移除发送者筛选';
  const clearFilters = decorateAction(control('清除筛选', () => {
    group.input.value = '';
    sender = null;
    highlight.group = highlight.sender = '';
    changeFilter();
  }), 'filter');
  const auto = toggle('自动刷新', saved.auto !== false);
  const limit = node('input');
  limit.type = 'number';
  limit.min = 1;
  limit.max = 300;
  limit.value = saved.limit || 200;
  limit.className = 'msgx-limit';
  const countLabel = node('label', '条数');
  countLabel.append(limit);
  const refreshButton = decorateAction(control('立即刷新', () => refresh(true)), 'refresh');
  const backLive = control('返回关键词消息', () => switchView('keywords'));
  backLive.hidden = true;
  const status = node('p', '正在读取消息…', 'msgx-muted');
  status.setAttribute('aria-live', 'polite');
  const toolbar = node('div', null, 'toolbar msgx-toolbar');
  toolbar.append(auto.label, refreshButton, countLabel, filter.label, group.label, senderChip, clearFilters, backLive);

  const messageGrid = grid(['时间', '群', '发送者', '内容', '图片与识别']);
  messageGrid.wrap.classList.add('msgx-current');
  const tabBar = node('div', null, 'msgx-tab-bar');
  const tabs = node('nav', null, 'msgx-message-tabs');
  tabs.setAttribute('aria-label', '消息与群操作视图');
  const panel = node('div', null, 'msgx-message-panel');
  const names = { list: '消息列表', activity: '群活跃度', related: '与我相关', keywords: '关键词消息', broadcast: '群发喊话' };
  const views = new Map();
  const tabButtons = new Map();
  for (const [id, name] of Object.entries(names)) {
    const view = node('section', null, `msgx-message-view msgx-view-${id}`);
    panel.append(view);
    views.set(id, view);
    if (id === 'list') continue; // 主页显示实时列表；此处仅在定位同群上下文时进入列表。
    const icons = { list: 'messages', activity: 'usage', related: 'person', keywords: 'tag', broadcast: 'send' };
    const button = decorateAction(control(name, () => switchView(id)), icons[id]);
    tabButtons.set(id, button);
    tabs.append(button);
  }
  views.get('list').append(messageGrid.wrap);
  const details = node('section', null, 'msgx-detail-view');
  const openDetails = decorateAction(control('消息详情', () => app.showInspector?.()), 'sidebar');
  openDetails.hidden = !app.showInspector;
  tabBar.append(tabs, openDetails);
  tabBar.hidden = listOnly;
  page.append(tabBar, toolbar, status, panel);
  app.setInspector?.(details, '消息详情');

  const activityView = views.get('activity');
  const relatedView = views.get('related');
  const keywordView = views.get('keywords');
  const broadcastView = views.get('broadcast');

  const activitySummary = node('p', null, 'msgx-muted');
  const period = choice('趋势天数', [['7', '7'], ['14', '14'], ['30', '30']], saved.period || '14');
  period.input.onchange = () => refresh(true);
  const leave = control('退出所选群…', () => showOperation('leave'));
  const activityBar = node('div', null, 'toolbar msgx-toolbar');
  activityBar.append(period.label, leave);
  const sort = control(ascending ? '综合日均 ↑' : '综合日均 ↓', () => {
    ascending = !ascending;
    sort.textContent = ascending ? '综合日均 ↑' : '综合日均 ↓';
    renderActivity();
  });
  sort.title = '今日 × 10%＋近 3 天日均 × 20%＋近 7 天日均 × 30%＋近 30 天日均 × 40%';
  const activityGrid = grid(['群名', '群号', '今日有效', '近 3 天', '近 7 天', '近 30 天', sort, '今日最近消息']);
  const trendLabel = node('p', null, 'msgx-muted');
  const trendCanvas = node('canvas', null, 'msgx-trend');
  trendCanvas.role = 'img';
  const trendNote = node('p', null, 'msgx-muted');
  activityView.append(activitySummary, activityBar, activityGrid.wrap, trendLabel, trendCanvas, trendNote);

  const relatedNote = node('p', null, 'msgx-muted');
  const relatedGrid = grid(['时间', '群', '关系', '发送者', '内容']);
  relatedView.append(relatedNote, relatedGrid.wrap);
  const keywordNote = node('p', null, 'msgx-muted');
  const ruleEdit = decorateAction(control('配置关键词', editRules), 'settings');
  const previousKeyword = control('上一页', () => {
    keywordOffset -= 200;
    loadKeywords();
  });
  const nextKeyword = control('下一页', () => {
    keywordOffset += 200;
    loadKeywords();
  });
  const keywordBar = node('div', null, 'toolbar msgx-toolbar');
  keywordBar.append(ruleEdit, previousKeyword, nextKeyword);
  const keywordGrid = grid(['时间', '内容', '命中关键词', '群', '发送者']);
  keywordView.append(keywordBar, keywordNote, keywordGrid.wrap);

  const compose = node('textarea');
  compose.rows = 4;
  compose.maxLength = 10000;
  compose.placeholder = '输入群发消息正文';
  compose.setAttribute('aria-label', '群发消息正文');
  compose.value = saved.draft || '';
  const interval = Object.assign(node('input'), { type: 'number', min: '1', max: '3600', step: '1', required: true, value: saved.interval || '30' });
  const intervalLabel = node('label', '群间隔（秒）');
  intervalLabel.append(interval);
  const send = control('预览群发范围…', () => showOperation('broadcast'));
  const history = control('读取最近操作记录', () => loadJob(''));
  const stop = control('停止后续发送', async () => {
    const id = job.id;
    stop.disabled = true;
    jobVersion++;
    clearTimeout(jobTimer);
    try {
      const response = await app.call('messages.group-cancel', { job_id: id });
      if (!disposed) app.notify(response.result.note);
    } catch (error) {
      reportError(error);
    }
    if (!disposed) loadJob(id);
  });
  const operationBar = node('div', null, 'toolbar msgx-toolbar');
  operationBar.append(intervalLabel, send, stop, history);
  const operationStatus = node('p', '尚未执行群操作', 'msgx-muted');
  operationStatus.setAttribute('role', 'status');
  const results = grid(['群名', '群号', '结果', '说明 / 消息 ID']);
  broadcastView.append(
    node('p', '从已保存的统计群中逐群勾选接收者，核对账号和正文后确认发送。', 'msgx-muted'),
    compose, operationBar, operationStatus, results.wrap,
  );

  function reportError(error) {
    if (!disposed) app.notify(error.message || String(error));
  }

  function switchView(id, fetch = true) {
    if (id !== 'list' && contextId) {
      contextVersion++;
      contextId = null;
      backLive.hidden = true;
    }
    activeView = id;
    for (const element of [countLabel, filter.label]) element.hidden = id !== 'list' || !!contextId;
    toolbar.hidden = id === 'broadcast';
    group.label.hidden = !!contextId;
    for (const [key, view] of views) {
      view.hidden = key !== id;
      tabButtons.get(key)?.setAttribute('aria-pressed', String(key === id));
    }
    if (!jobInitialized && (id === 'activity' || id === 'broadcast')) {
      jobInitialized = true;
      void loadJob('').then(() => {
        if (disposed) return;
        operationBusy = false;
        updateOperationButtons();
      });
    }
    if (id === 'activity') renderActivity();
    if (id === 'related') renderRelated();
    if (fetch) void refresh(true);
  }

  function renderFilters() {
    senderChip.hidden = !sender;
    senderChip.textContent = sender ? `发送者：${sender.label} ×` : '';
    clearFilters.hidden = !group.input.value && !sender;
  }

  function identityCell(kind, value, label) {
    if (!value) return label || '—';
    const id = String(value);
    const cell = control(label || id, () => {
      if (highlight[kind] !== id) {
        highlight[kind] = id;
        for (const other of page.querySelectorAll(`[data-identity="${kind}"]`)) {
          other.setAttribute('aria-pressed', String(other.dataset.id === id));
        }
        return;
      }
      if (kind === 'group') {
        if (![...group.input.options].some(o => o.value === id)) group.input.add(new Option(label || id, id));
        group.input.value = id;
      } else sender = { id, label: label || id };
      changeFilter();
    });
    cell.dataset.identity = kind;
    cell.dataset.id = id;
    cell.setAttribute('aria-pressed', String(highlight[kind] === id));
    cell.title = `${label || id}（${id}）· 单击高亮，再点筛选`;
    return cell;
  }

  function renderMessages() {
    fill(messageGrid, rows.map(row => ({
      id: row.id,
      className: [
        row.vision_requested_at || row.vision_text ? 'msgx-vision-row' : '',
        row.relation === 'self' || row.relation === 'reply' ? 'msgx-related-row' : '',
        selectedId === row.id ? 'msgx-selected-row' : '',
      ].join(' '),
      cells: [
        formatTime(row.ts), identityCell('group', row.group_id, groupName(row)),
        identityCell('sender', row.sender_id, row.sender_name),
        Object.assign(control(snippet(row), () => select(row)), { title: snippet(row) }),
        imageCell(row),
      ],
    })), '没有符合筛选条件的消息');
  }

  function select(row) {
    selectedId = row.id;
    selectedDetails = row;
    // 更新选中样式时保留正在使用的键盘焦点。
    for (const element of messageGrid.body.rows) {
      element.classList.toggle('msgx-selected-row', element.dataset.key === String(selectedId));
    }
    renderDetails();
    onSelect?.();
  }

  async function imageAction(action, image, values = {}) {
    const imageId = image.image_id ?? image.id;
    const key = `${imageId}:${action}`;
    if (imageBusy.has(key)) return;
    imageBusy.add(key);
    renderDetails();
    try {
      const result = await app.call(`messages.${action}`, { image_id: imageId, ...values });
      if (disposed) return;
      if (action === 'vision') {
        app.notify(result.state === 'ok' ? '图片识别完成' : result.state === 'running' ? '图片已在识别中' : '图片识别未完成');
      } else if (action === 'summary-include') {
        image.include_vision = result.include;
        app.notify('已更新图片纳入摘要状态');
      }
      if (action !== 'image-open') {
        if (contextId) {
          await locate(contextId, selectedId);
        } else {
          const relatedId = selectedDetails?.id;
          await refresh();
          if (!disposed && typeof relatedId === 'number' && selectedDetails?.id === relatedId) {
            selectedDetails = relatedRows.find(row => row.id === relatedId) || selectedDetails;
          }
        }
      }
    } catch (error) {
      reportError(error);
    } finally {
      imageBusy.delete(key);
      if (!disposed) renderDetails();
    }
  }

  function renderDetails() {
    if (!selectedDetails) {
      details.replaceChildren(node('p', '选择消息查看完整正文、OCR 和图片语义', 'msgx-muted'));
      return;
    }
    const row = selectedDetails;
    const metadata = [groupName(row), formatTime(row.ts)].filter(Boolean);
    const header = node('div', null, 'msgx-detail-header');
    header.append(node('strong', row.sender_name || '未知发送者'), node('p', metadata.join(' · '), 'msgx-muted'));
    const receiver = row.receiver_accounts || row.received_by;
    if (receiver) header.append(node('p', `接收账号 ${receiver}`, 'msgx-muted'));
    details.replaceChildren(header, node('pre', row.text || '（无文本）', 'msgx-detail-body'));
    if (row.keyword_matches?.length) details.append(node('p', `命中：${row.keyword_matches.join('、')}`, 'msgx-muted'));
    const images = row.image_id || !row.images?.length ? (row.has_media ? [row] : []) : row.images;
    images.forEach((image, index) => {
      const heading = `图片 ${image.image_index && row.image_id ? row.image_index : index + 1} / ${row.image_count || images.length}`;
      details.append(node('h3', `${heading} · ${mediaLabel(image)}`));
      const id = image.image_id ?? (row.images?.length ? image.id : null);
      if (id) {
        const controls = node('div', null, 'toolbar msgx-toolbar');
        if (image.media_path) controls.append(control('打开原图', () => imageAction('image-open', image)));
        const vision = control(imageBusy.has(`${id}:vision`) ? '识别中…' : '手动识别', async () => {
          if (await app.confirm('识别这张图片', '提交给当前图片识别服务，可能产生 API 费用。') && !disposed) {
            imageAction('vision', image);
          }
        });
        vision.disabled = !image.media_path || Boolean(image.vision_text) || image.auto_state === 'running' || imageBusy.has(`${id}:vision`);
        const include = toggle('纳入摘要', Boolean(image.include_vision));
        include.input.disabled = imageBusy.has(`${id}:summary-include`);
        include.input.onchange = () => imageAction('summary-include', image, { include: include.input.checked });
        controls.append(vision, include.label);
        details.append(controls);
      }
      details.append(node('h4', 'OCR'), node('pre', image.ocr_text || '（无文字）'),
        node('h4', '图片语义'), node('pre', image.vision_text || '（尚未生成）'));
      if (image.screen_state === 'done' || image.screen_state === 'blocked') {
        details.append(node('p', `本地重要度 ${image.screen_score || 0}/100 · ${image.screen_reason || '未提供理由'}`, 'msgx-muted'));
      }
      if (image.auto_state === 'waiting') details.append(node('p', `等待上下文，最早分析时间 ${formatTime(image.auto_ready_at)}`, 'msgx-muted'));
      if (image.auto_state === 'unknown') details.append(node('p', '自动分析结果未知，请核对记录', 'msgx-muted'));
      if (image.vision_requested_at) details.append(node('p', `最近尝试图片识别：${formatTime(image.vision_requested_at)}`, 'msgx-muted'));
    });
  }

  function renderRelated() {
    relatedNote.textContent = account ? `QQ ${account} · 本人发出、被单独 @ 或引用回复` : '尚未确定当前 QQ 账号';
    fill(relatedGrid, relatedRows.map(row => ({
      id: row.id, cells: [formatTime(row.ts), identityCell('group', row.group_id, groupName(row)), row.relation === 'self' ? '我发出的' : '回复我的',
        identityCell('sender', row.sender_id, row.sender_name), control(snippet(row), () => select(row))],
    })), '没有与当前 QQ 相关的消息');
  }

  function renderActivity() {
    const groups = activity.groups || [];
    const sorted = ascending ? [...groups].reverse() : groups;
    const trend = activity.trend || {};
    if (!groupForTrend || !groups.some(row => row.group_id === groupForTrend)) groupForTrend = trend.group_id || '';
    activitySummary.textContent = activity.date
      ? `${activity.date} · ${groups.length} 个群 · 今日有效消息 ${groups.reduce((sum, row) => sum + row.count, 0)} 条`
      : '没有可统计的活动记录';
    fill(activityGrid, sorted.map(row => ({
      id: row.group_id, className: row.group_id === groupForTrend ? 'msgx-selected-row' : '',
      cells: [
        control(groupName(row), () => {
          groupForTrend = String(row.group_id);
          group.input.value = groupForTrend;
          changeFilter();
        }), row.group_id, row.count, row.count3, row.count7, row.count30,
        Number(row.score).toFixed(2), formatTime(row.last_ts),
      ],
    })), '没有可显示的群');
    trendLabel.textContent = trend.group_id ? `${trend.group_name || trend.group_id} · 近 ${trend.days} 天` : '';
    trendCanvas.hidden = !trend.group_id;
    trendNote.textContent = trend.note || '';
    if (!trendCanvas.hidden && activeView === 'activity') drawTrend(trendCanvas, trend);
    updateOperationButtons();
  }

  async function loadKeywords(force = false) {
    if (disposed) return;
    const requestedOffset = keywordOffset;
    const requestedGroup = group.input.value;
    const key = JSON.stringify([requestedGroup, sender?.id, requestedOffset]);
    // 历史正则搜索可能长于实时节拍，不叠加同一查询。
    if (keywordRequest !== null) {
      if (force || key !== keywordRequest) {
        keywordVersion++;
        keywordQueued = true;
      }
      return;
    }
    keywordRequest = key;
    const version = ++keywordVersion;
    previousKeyword.disabled = true;
    nextKeyword.disabled = true;
    keywordNote.textContent = '正在检索已保存的消息…';
    try {
      const result = await app.call('messages.keywords', { offset: requestedOffset, group_id: requestedGroup, sender_id: sender?.id || '' });
      if (disposed || version !== keywordVersion) return;
      keywordRows = result.items;
      keywordNote.textContent = `第 ${requestedOffset / 200 + 1} 页 · ${keywordRows.length} 条 · 点击消息定位同群历史`;
      previousKeyword.disabled = requestedOffset === 0;
      nextKeyword.disabled = !result.more;
      fill(keywordGrid, keywordRows.map(row => ({
        id: row.id, className: 'msgx-hit-row',
        cells: [formatTime(row.ts), keywordExcerpt(row, () => locate(row.id)), keywordLabels(row),
          identityCell('group', row.group_id, groupName(row)), identityCell('sender', row.sender_id, row.sender_name)],
      })), '没有关键词命中，可配置普通词或正则表达式');
    } catch (error) {
      if (disposed || version !== keywordVersion) return;
      keywordNote.textContent = `读取失败：${error.message || error}`;
      previousKeyword.disabled = requestedOffset === 0;
    } finally {
      keywordRequest = null;
      if (keywordQueued && !disposed) {
        keywordQueued = false;
        loadKeywords();
      }
    }
  }

  async function editRules() {
    ruleEdit.disabled = true;
    try {
      const { rules } = await app.call('messages.rules-read');
      if (disposed) return;
      const dialog = makeDialog(page, '配置关键词');
      const input = node('textarea');
      input.rows = 10;
      input.value = rules.join('\n');
      input.setAttribute('aria-label', '关键词规则，每行一条');
      const feedback = node('p', '每行一条。普通词匹配文本，以 re: 开头使用正则。', 'msgx-muted');
      const actions = node('div', null, 'toolbar msgx-toolbar');
      const save = control('保存并生效', async () => {
        save.disabled = true;
        try {
          await app.call('messages.rules-save', { text: input.value });
          if (disposed) return;
          dialog.close();
          keywordOffset = 0;
          loadKeywords(true);
          if (contextId) locate(contextId, selectedId);
          else refresh();
        } catch (error) {
          feedback.textContent = error.message || String(error);
          save.disabled = false;
        }
      });
      save.classList.add('confirm-yes');
      actions.append(save, Object.assign(control('取消', () => dialog.close()), { className: 'confirm-no' }));
      dialog.append(feedback, input, actions);
      dialog.showModal();
    } catch (error) {
      reportError(error);
    } finally {
      ruleEdit.disabled = false;
    }
  }

  async function locate(id, anchorId = null) {
    const token = ++contextVersion;
    contextId = id;
    switchView('list', false);
    backLive.hidden = false;
    status.textContent = '正在定位同群消息…';
    try {
      const result = await app.call('messages.context', { id });
      if (disposed || token !== contextVersion) return;
      if (!result.items.length) throw new Error('消息已清理或不在当前账号范围');
      rows = expandImages(result.items);
      renderMessages();
      const anchor = rows.find(row => row.id === anchorId) || rows.find(row => row.parent_id === id);
      if (anchor) {
        select(anchor);
        const element = [...messageGrid.body.rows].find(row => row.dataset.key === anchor.id);
        element?.scrollIntoView({ block: 'nearest' });
      }
      status.textContent = `同群历史上下文 · ${result.items.length} 条消息 · 实时滚动已暂停`;
    } catch (error) {
      if (!disposed && token === contextVersion) status.textContent = `定位失败：${error.message || error}`;
    }
  }

  async function refresh(force = false) {
    if (disposed || activeView === 'broadcast') return;
    if (activeView === 'keywords') return loadKeywords(force);
    if (contextId && activeView === 'list') return;
    const pickedGroup = group.input.value;
    const args = {view: activeView, sender_id: sender?.id || '', limit: Number(limit.value || 200),
      images: filter.input.value === 'images', group_id: pickedGroup,
      activity_group: groupForTrend, activity_days: Number(period.input.value)};
    const key = JSON.stringify(args);
    if (pendingKey === key) {
      if (force) refreshQueued = true;
      return;
    }
    pendingKey = key;
    refreshQueued = false;
    const token = ++loadVersion;
    try {
      const data = await app.call('messages.recent', {...args, force_activity: force});
      if (disposed || token !== loadVersion || activeView !== args.view) return;
      liveAvailable = data.live;
      account = data.related_account;
      relatedRows = data.related;
      if (data.activity) activity = data.activity;
      const choices = [['', '全部群'], ...(data.groups || activity.groups || []).map(row => [row.group_id, row.group_name && row.group_name !== row.group_id ? `${row.group_name}（${row.group_id}）` : String(row.group_id)])];
      if (pickedGroup && !choices.some(([id]) => id === pickedGroup)) choices.push([pickedGroup, group.input.selectedOptions[0]?.text || pickedGroup]);
      if (choices.length !== group.input.options.length || choices.some(([value, text], index) =>
        group.input.options[index].value !== value || group.input.options[index].text !== text)) {
        group.input.replaceChildren(...choices.map(([value, text]) => new Option(text, value)));
      }
      group.input.value = [...group.input.options].some(option => option.value === pickedGroup) ? pickedGroup : '';
      if (activeView === 'activity') renderActivity();
      if (activeView === 'related') renderRelated();
      if (!contextId && activeView === 'list') {
        rows = expandImages(data.items);
        renderMessages();
        status.textContent = `${data.live ? '实时消息' : '接收服务未运行 · 本地历史'} · ${data.items.length} 条消息`
          + (data.keyword_error ? ` · 关键词配置有误：${data.keyword_error}` : '');
        const refreshed = rows.find(row => row.id === selectedId);
        if (refreshed && JSON.stringify(selectedDetails) !== JSON.stringify(refreshed)) {
          selectedDetails = refreshed;
          renderDetails();
        }
      }
      if (activeView === 'activity' || activeView === 'related') status.textContent = '';
    } catch (error) {
      if (!disposed && token === loadVersion) status.textContent = `刷新失败：${error.message || error}`;
    } finally {
      if (token === loadVersion) {
        pendingKey = '';
        if (refreshQueued && !disposed) void refresh(true);
      }
    }
  }

  function changeFilter() {
    contextVersion++;
    contextId = null;
    selectedId = null;
    selectedDetails = null;
    keywordOffset = 0;
    backLive.hidden = true;
    renderDetails();
    renderFilters();
    refresh(true);
  }

  function updateOperationButtons() {
    const running = job?.state === 'running';
    send.disabled = Boolean(operationBusy || running);
    leave.disabled = Boolean(operationBusy || running || !groupForTrend);
    compose.disabled = Boolean(operationBusy || running);
    stop.disabled = Boolean(operationBusy || !running);
    history.disabled = operationBusy;
  }

  function renderJob(response) {
    clearTimeout(jobTimer);
    job = response.result;
    operationStatus.textContent = job ? `QQ ${job.account} · ${job.note || job.state}` : '没有已确认的操作记录';
    const running = job?.state === 'running';
    if (running && response.offline) {
      operationStatus.textContent += ' · 服务已停止，可重新启动后核对记录';
      // 磁盘中的 running 可能是退出前最后一个快照。
      job = { ...job, state: 'interrupted' };
    }
    fill(results, (job?.groups || []).map(row => ({ id: row.group_id,
      cells: [row.group_name, row.group_id, OPERATION_LABELS[row.state] || row.state, row.error || row.note || row.message_id || ''],
    })), '没有逐群结果');
    updateOperationButtons();
    if (running && !response.offline) jobTimer = setTimeout(() => loadJob(job.id), 700);
  }

  async function loadJob(id) {
    clearTimeout(jobTimer);
    const version = ++jobVersion;
    try {
      const response = await app.call('messages.group-status', { job_id: id });
      if (disposed || version !== jobVersion) return;
      const finishedLeave = job?.id === response.result?.id && job?.state === 'running'
        && response.result.kind === 'leave' && response.result.groups.some(row => row.state === 'cleaned');
      renderJob(response);
      if (finishedLeave) changeFilter();
    } catch (error) {
      if (disposed || version !== jobVersion) return;
      operationStatus.textContent = `未能读取群操作结果：${error.message || error}`;
      // 连接失败后仅允许人工读取持久化结果，不擅自重新提交任务。
      job = null;
      updateOperationButtons();
    }
  }

  async function showOperation(kind) {
    if (operationBusy || job?.state === 'running') return;
    const broadcast = kind === 'broadcast';
    if (broadcast && !interval.reportValidity()) return;
    if (broadcast && !compose.value.trim()) {
      app.notify('请输入要群发的正文');
      compose.focus();
      return;
    }
    operationBusy = true;
    jobVersion++;
    clearTimeout(jobTimer);
    updateOperationButtons();
    operationStatus.textContent = '正在核对账号与群范围…';
    try {
      const response = await app.call('messages.group-preview', broadcast
        ? { kind, text: compose.value, interval_seconds: Number(interval.value) }
        : { kind, group_id: groupForTrend });
      if (disposed) return;
      const preview = response.result;
      const dialog = makeDialog(page, broadcast ? '二次确认群发' : '二次确认退群');
      dialog.append(node('p', `操作账号：QQ ${preview.account}`));
      if (preview.excluded.length) dialog.append(node('p', `不在当前账号群列表中：${preview.excluded.join('、')}`, 'msgx-muted'));
      const checkboxes = [];
      if (broadcast) {
        const candidates = node('fieldset', null, 'msgx-group-options');
        candidates.append(node('legend', '接收群'));
        const remembered = new Set(response.remembered.map(String));
        for (const candidate of preview.groups) {
          const item = toggle(`${candidate.group_name}（${candidate.group_id}）`, remembered.has(String(candidate.group_id)));
          item.input.value = candidate.group_id;
          checkboxes.push(item.input);
          candidates.append(item.label);
        }
        dialog.append(candidates, node('p', `每个群之间间隔 ${preview.interval_seconds} 秒`), node('h3', '完整正文'), node('pre', preview.text, 'msgx-preview-text'));
      } else {
        const target = preview.groups[0];
        dialog.append(node('h3', `${target.group_name}（${target.group_id}）`),
          node('p', `退出后清理本地该群全部账号的消息（当前 ${preview.message_count} 条）及 OCR、图片语义记录。已生成的报告、批注、费用记录和原图继续保留。`));
      }
      const ack = toggle(broadcast ? '我已核对账号、勾选的全部群号与正文' : '我已核对账号、群号及清理范围');
      const actions = node('div', null, 'toolbar msgx-toolbar');
      const chosen = () => checkboxes.filter(input => input.checked).map(input => input.value);
      let submitting = false;
      const submit = control('', async () => {
        const groups = chosen();
        if (submitting || !ack.input.checked || (broadcast && !groups.length)) return;
        submitting = true;
        submit.disabled = true;
        dialog.close();
        switchView('broadcast');
        operationStatus.textContent = '正在执行已确认的群操作…';
        try {
          const body = { token: preview.token, confirmed: true };
          if (broadcast) body.groups = groups;
          const result = await app.call('messages.group-confirm', body);
          if (disposed) return;
          if (result.preference_warning) app.notify(result.preference_warning);
          renderJob(result);
          if (job?.kind === 'leave' && job.groups.some(row => row.state === 'cleaned')) changeFilter();
        } catch (error) {
          if (!disposed) {
            operationStatus.textContent = `提交结果需核对：${error.message || error}`;
            app.notify('请读取最近操作记录核对执行结果');
          }
        } finally {
          operationBusy = false;
          if (!disposed) updateOperationButtons();
        }
      });
      function updateSelection() {
        const count = chosen().length;
        submit.disabled = !ack.input.checked || (broadcast && !count);
        submit.textContent = broadcast ? `确认发送至 ${count} 个群` : '确认退出并清理消息';
      }
      ack.input.onchange = updateSelection;
      for (const input of checkboxes) input.onchange = () => {
        ack.input.checked = false;
        updateSelection();
      };
      submit.classList.add('confirm-yes');
      actions.append(submit, Object.assign(control('取消', () => dialog.close()), { className: 'confirm-no' }));
      dialog.append(node('p', '预览 5 分钟内有效。账号或群范围变化后请重新预览。', 'msgx-muted'), ack.label, actions);
      dialog.addEventListener('close', () => {
        if (!submitting) {
          operationBusy = false;
          operationStatus.textContent = '已取消群操作';
          updateOperationButtons();
        }
      }, { once: true });
      updateSelection();
      dialog.showModal();
    } catch (error) {
      operationBusy = false;
      operationStatus.textContent = error.message || String(error);
      updateOperationButtons();
      reportError(error);
    }
  }

  filter.input.onchange = changeFilter;
  group.input.onchange = () => { highlight.group = group.input.value; changeFilter(); };
  limit.onchange = changeFilter;
  switchView(activeView, false);
  renderFilters();
  renderDetails();
  updateOperationButtons();
  void refresh().then(() => {
    if (!disposed && saved.contextId) return locate(saved.contextId, saved.selectedId);
  });
  const timer = setInterval(() => {
    if (auto.input.checked && (liveAvailable || app.receiverRunning?.()) && !contextId && !document.hidden) {
      refresh();
    }
  }, 3000);
  return () => {
    app.state[stateKey] = {
      activeView, contextId, selectedId, trend: groupForTrend, ascending, keywordOffset,
      filter: filter.input.value, group: group.input.value, sender, auto: auto.input.checked,
      limit: limit.value, period: period.input.value, draft: compose.value, interval: interval.value, imageBusy,
    };
    disposed = true;
    clearInterval(timer);
    clearTimeout(jobTimer);
    app.setInspector?.(null);
    page.remove();
  };
}
