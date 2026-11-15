import { el as make } from './controls.js';

const apiFields = [
  ['provider', '服务平台'], ['base_url', 'API 基础地址'], ['model', '模型'],
  ['protocol', '接口格式', ['responses']],
  ['reasoning', '思考强度', ['none', 'low', 'medium', 'high', 'xhigh', 'max']],
  ['service_tier', 'OpenAI 服务档位', ['flex', 'default']],
  ['context_tokens', '上下文 Token 上限'], ['max_output_tokens', '输出 Token 上限'],
  ['currency', '费率币种', ['USD', 'CNY']], ['input_price', '输入单价 / 百万 Token'],
  ['cached_price', '缓存输入单价 / 百万 Token'], ['output_price', '输出单价 / 百万 Token'],
  ['usd_cny', '美元折人民币（估算）'], ['long_context_threshold', '长输入加价阈值（0 关闭）'],
  ['long_input_multiplier', '长输入价格倍数'], ['long_output_multiplier', '长输出价格倍数'],
  ['peak_multiplier', '高峰价格倍数'], ['peak_windows_utc', '工作日高峰时段（UTC）'],
];

const imageFields = [
  ['threshold', '重要度阈值（1～100）'], ['daily_limit', '每日最多分析张数'],
  ['daily_budget_cny', '每日估算预算（元）'], ['history_days', '图片范围（最近天数）'],
];

const filterFields = [
  ['skip_face_only', '纯表情 / 表情包'], ['skip_image_only', '纯图片'],
  ['skip_forward', '合并转发'], ['skip_record', '语音消息'],
];

function options(select, choices, selected) {
  select.replaceChildren(...choices.map(([value, label]) => make('option', { value }, label)));
  select.value = selected;
}

function field(label, input) {
  return make('label', { className: 'settings-field' }, make('span', {}, label), input);
}

function check(label, checked = false) {
  const input = make('input', { type: 'checkbox', checked });
  return [input, make('label', { className: 'settings-check' }, input, label)];
}

function row(...children) {
  return make('div', { className: 'toolbar' }, ...children);
}

function explain(text) {
  return make('p', { className: 'muted' }, text);
}

function readFields(controls) {
  return Object.fromEntries(Object.entries(controls).map(([key, input]) => [key, input.value]));
}

function setFields(controls, values) {
  for (const [key, input] of Object.entries(controls)) input.value = values[key];
}

export function mount(container, app) {
  let disposed = false;
  let busy = false;
  let view;
  const drafts = new Map();
  const sections = new Map();
  const status = make('p', { className: 'muted', role: 'status' }, '正在读取本机设置…');
  const page = make('div', { className: 'settings-page' }, status);
  container.replaceChildren(page);

  function notify(message) {
    if (disposed) return;
    status.textContent = message;
    app.notify(message);
  }

  // 串行处理当前页面的操作，锁定表单避免请求返回后覆盖正在编辑的值。
  async function perform(work, { loading = false } = {}) {
    if (busy || disposed) return;
    if (!view && !loading) {
      notify('当前设置未加载，请重新加载。');
      return;
    }
    busy = true;
    const fields = [...page.querySelectorAll('fieldset')];
    for (const element of fields) element.disabled = true;
    try {
      await work();
    } catch (error) {
      notify(error.message || String(error));
    } finally {
      for (const element of fields) element.disabled = false;
      busy = false;
    }
  }

  const button = (label, work, options) => make('button', { type: 'button', onclick: () => perform(work, options) }, label);

  function section(title, ...children) {
    const node = make('fieldset', { className: 'panel settings-section' }, make('legend', {}, title), ...children);
    sections.set(title, node);
    return node;
  }

  function collectSettings() {
    return {
      id: view.active, profile: readFields(profileInputs), imageProfile: imageProfile.value,
      image: { ...readFields(imageInputs), enabled: imageEnabled.checked },
      summaryPrompt: summaryPrompt.value, targetChars: targetChars.value,
      promptEvolution: { enabled: promptEnabled.checked, days: promptDays.value, interval_days: promptInterval.value },
      groups: [...selectedGroups],
      filters: { ...Object.fromEntries(Object.entries(filterInputs).map(([key, input]) => [key, input.checked])),
        blocked_senders: [...senderList.options].map(option => option.value), min_text_length: minLength.value },
      account: qqAccount.value, napcatDir: napcatDir.value, qqPath: qqPath.value, hidden: hidden.checked,
    };
  }

  async function save() {
    view = await app.call('settings.save', collectSettings());
    drafts.delete(view.active);
    refreshProfiles();
    setFields(profileInputs, view.profiles[view.active]);
    void updateKey();
    notify('设置已保存。接收范围、过滤与协议端设置在服务重启后生效。');
  }

  const profile = make('select', { ariaLabel: '摘要 API 配置' });
  const profileInputs = Object.fromEntries(apiFields.map(([key, _, choices]) => {
    const input = choices ? make('select', {}) : make('input', { type: 'text', autocomplete: 'off' });
    if (choices) options(input, choices.map(choice => [choice, choice]), choices[0]);
    return [key, input];
  }));
  const secret = make('input', { type: 'password', autocomplete: 'new-password' });
  const keyStatus = explain('');
  const profileLocation = explain('');
  let keyRequest = 0;

  function refreshProfiles(imageChoice = view.imageProfile) {
    const entries = Object.entries(view.profiles).map(([id, value]) => [id, `${value.provider} / ${value.model} [${id}]`]);
    options(profile, entries, view.active);
    options(imageProfile, [['', '跟随摘要 API'], ...entries], Object.hasOwn(view.profiles, imageChoice) ? imageChoice : '');
    profileLocation.textContent = `摘要与提示词使用 ${view.active}。图片分析使用 ${view.imageProfile || view.active}。`;
  }

  function showProfile(imageChoice) {
    refreshProfiles(imageChoice);
    const draft = drafts.get(view.active);
    setFields(profileInputs, draft?.fields || view.profiles[view.active]);
    secret.value = draft?.key || '';
    void updateKey();
  }

  const credentialsArgs = () => ({ id: view.active, base_url: profileInputs.base_url.value });

  async function updateKey() {
    const seq = ++keyRequest;
    if (secret.value.trim()) {
      keyStatus.textContent = '有未保存的新密钥。';
      return;
    }
    try {
      const result = await app.call('settings.key.status', credentialsArgs());
      if (seq === keyRequest && !disposed) {
        keyStatus.textContent = `${result.configured ? '已配置密钥' : '此地址未配置密钥'} · Windows 凭据 ${result.target}`;
      }
    } catch (error) {
      if (seq === keyRequest && !disposed) keyStatus.textContent = `读取密钥状态失败：${error.message}`;
    }
  }

  function rememberProfile() {
    drafts.set(view.active, { fields: readFields(profileInputs), key: secret.value });
  }

  profile.onchange = () => {
    if (!view) return;
    const id = profile.value;
    profile.value = view.active;
    void perform(async () => {
      rememberProfile();
      Object.assign(view, await app.call('settings.profile.select', { id }));
      showProfile(imageProfile.value);
      notify(`已切换到 ${id}。`);
    });
  };
  secret.oninput = updateKey;
  profileInputs.base_url.onblur = updateKey;

  const api = section('API 配置',
    row(profile,
      button('以当前参数新增', async () => {
        rememberProfile();
        Object.assign(view, await app.call('settings.profile.add', { profile: readFields(profileInputs) }));
        showProfile(imageProfile.value);
        notify(`已新增配置 ${view.active}，可单独设置密钥。`);
      }),
      button('删除此配置', async () => {
        const id = view.active;
        if (!await app.confirm('删除 API 配置', `删除 ${id}？历史用量、系统凭据及在途任务快照会保留。此配置上的图片分析将改为跟随摘要。`)) return;
        Object.assign(view, await app.call('settings.profile.delete', { id }));
        drafts.delete(id);
        showProfile(imageProfile.value === id ? '' : imageProfile.value);
        notify(`已删除配置 ${id}。`);
      })),
    make('div', { className: 'settings-grid' }, ...apiFields.map(([key, label]) => field(label, profileInputs[key]))),
    row(
      button('保存 API 参数', async () => {
        Object.assign(view, await app.call('settings.profile.save', { id: view.active, profile: readFields(profileInputs) }));
        drafts.delete(view.active);
        refreshProfiles(imageProfile.value);
        setFields(profileInputs, view.profiles[view.active]);
        void updateKey();
        notify('API 参数已保存。');
      }),
      button('导入 YAML', async () => {
        const path = await app.call('desktop.pickFile', { kind: 'api' });
        if (!path) return;
        setFields(profileInputs, await app.call('settings.api.import', { path }));
        secret.value = '';
        void updateKey();
        notify('API 参数已载入，保存后生效。');
      }),
      button('导出 YAML', async () => {
        const path = await app.call('desktop.saveFile', { kind: 'api' });
        if (!path) return;
        await app.call('settings.api.export', { path, profile: readFields(profileInputs) });
        notify('已导出公开 API 参数。');
      })),
    field('新密钥', secret), keyStatus,
    row(
      button('保存 / 替换密钥', async () => {
        const result = await app.call('settings.key.save', { ...credentialsArgs(), key: secret.value });
        secret.value = '';
        ++keyRequest;
        keyStatus.textContent = `已保存到 Windows 凭据 ${result.target}`;
        notify('已保存密钥，可在实际调用中验证。');
      }),
      button('删除此地址的密钥', async () => {
        if (!await app.confirm('删除 API 密钥', `删除配置 ${view.active} 当前已保存地址的 Windows 凭据？`)) return;
        await app.call('settings.key.delete', credentialsArgs());
        secret.value = '';
        void updateKey();
        notify('已删除此地址的密钥。');
      })), profileLocation,
    explain('OpenAI 费率填写 Standard 基准价。Flex 仅在明确返回容量不足时回退到 Standard。'));

  const summaryPrompt = make('textarea', { rows: 10, spellcheck: false });
  const targetChars = make('input', { type: 'number', min: '1', max: '100000' });
  const [promptEnabled, promptToggle] = check('启用提示词自更新');
  const promptDays = make('input', { type: 'number', min: '2', max: '30' });
  const promptInterval = make('input', { type: 'number', min: '1', max: '30' });
  const promptHistory = make('select', { ariaLabel: '动态提示词历史' });
  const promptPreview = make('pre', { className: 'settings-preview' });
  const promptStatus = explain('尚未读取动态提示词记录。');
  let promptRecords = [];
  promptHistory.onchange = () => {
    const record = promptRecords.find(item => String(item.id) === promptHistory.value);
    promptPreview.textContent = record ? JSON.stringify(record.data, null, 2) : '';
  };

  function showPrompt(result) {
    const active = result.active?.id;
    const current = promptHistory.value;
    promptStatus.textContent = [result.queued ? '更新已排队' : result.busy ? '更新进行中' : result.state || '就绪',
      active ? `当前版本 #${active}` : '无动态补充', result.error].filter(Boolean).join(' · ');
    if (!Object.hasOwn(result, 'history')) return;
    promptRecords = result.history || [];
    options(promptHistory, promptRecords.map(v => [String(v.id), `#${v.id} · ${v.source_start} 至 ${v.source_end}`]),
      promptRecords.some(record => String(record.id) === current) ? current : String(active ?? promptRecords[0]?.id ?? ''));
    promptHistory.onchange();
  }

  const summary = section('摘要与动态提示词',
    row(field('日报目标字数', targetChars), button('恢复默认提示词', async () => {
      summaryPrompt.value = view.defaultPrompt;
      notify('已恢复默认提示词，保存后生效。');
    })),
    explain('周报使用日报字数的两倍，月报使用三倍。'), field('基础提示词', summaryPrompt), promptToggle,
    row(field('取最近日报天数', promptDays), field('更新间隔（天）', promptInterval)), promptStatus,
    row(button('刷新记录', async () => showPrompt(await app.call('settings.prompt.status'))),
      button('立即更新（调用 API）', async () => {
        if (!await app.confirm('更新动态提示词', '先保存当前设置，再调用摘要 API。若上次提交结果未知且没有响应 ID，再次确认可能重复计费。')) return;
        await save();
        showPrompt(await app.call('settings.prompt.update', { confirmed: true }));
        notify(promptStatus.textContent);
      }),
      button('应用选中版本', async () => {
        if (!promptHistory.value) throw new Error('请先选择一条动态提示词历史记录。');
        showPrompt(await app.call('settings.prompt.restore', { id: Number(promptHistory.value) }));
      }),
      button('清空动态补充', async () => showPrompt(await app.call('settings.prompt.restore', { id: 0 })))),
    field('历史版本', promptHistory), promptPreview);

  const imageProfile = make('select', {});
  const [imageEnabled, imageToggle] = check('评分达阈值后，等待 15 分钟自动分析');
  const imageInputs = Object.fromEntries(imageFields.map(([key]) => [key, make('input', { type: 'number', step: key === 'daily_budget_cny' ? 'any' : '1' })]));
  const imageStatus = explain('启动接收服务后显示图片队列状态。');
  let readingHealth = false;
  async function readHealth() {
    if (disposed || readingHealth || container.hidden) return;
    readingHealth = true;
    try {
      const { health, serviceRunning } = await app.call('status.read');
      if (!disposed) {
        imageStatus.textContent = health.image_analysis?.message || (serviceRunning ? '图片队列状态暂不可用。' : '启动接收服务后显示图片队列状态。');
        if (!busy && health.prompt_evolution) showPrompt(health.prompt_evolution);
      }
    } catch (error) {
      if (!disposed) imageStatus.textContent = `读取图片队列失败：${error.message}`;
    } finally {
      readingHealth = false;
    }
  }
  const image = section('图片初筛与独立 API',
    field('图片分析配置', imageProfile), imageToggle,
    make('div', { className: 'settings-grid' }, ...imageFields.map(([key, label]) => field(label, imageInputs[key]))),
    explain('表情包直接过滤。筛选使用本地 OCR，自动分析调用图片 API 并受每日预算与次数限制。'), imageStatus,
    row(button('保存图片分析设置', async () => {
      const values = { imageProfile: imageProfile.value, image: { ...readFields(imageInputs), enabled: imageEnabled.checked } };
      view.image = await app.call('settings.image.save', values);
      view.imageProfile = imageProfile.value;
      refreshProfiles();
      notify('图片分析设置已保存。');
    }), button('编辑图片主题与评分规则', () => editRules(view, app, notify))));

  const selectedGroups = new Set();
  const groupList = make('div', { className: 'settings-group-list' });
  const groupStatus = explain('');
  async function readGroups(action = 'settings.groups') {
    const result = await app.call(action);
    if (disposed) return;
    const found = new Set(result.rows.map(([id]) => String(id)));
    const rows = [...result.rows, ...[...selectedGroups].filter(id => !found.has(id)).map(id => [id, '已保存，当前群列表无记录', '', null])];
    groupList.replaceChildren(...rows.map(([id, title, owner, count]) => {
      id = String(id);
      const [box, label] = check(`${title || '未命名'} (${id})${owner ? ` · QQ ${owner}` : ''}${count === null ? '' : ` · ${count} 条`}`, selectedGroups.has(id));
      box.onchange = () => box.checked ? selectedGroups.add(id) : selectedGroups.delete(id);
      return label;
    }));
    groupStatus.textContent = result.warning || `${result.account ? `QQ ${result.account} · ` : ''}${result.rows.length} 个群${result.deleted ? ` · 已删除 ${result.deleted} 个空群` : ''}`;
  }
  const groups = section('统计哪些群', explain('全不选时统计所有群。群发接收范围由群发页单独确认。'),
    row(button('刷新群列表', () => readGroups('settings.groups.refresh')),
      button('只保留当前账号的群', async () => {
        if (await app.confirm('同步当前账号群列表', '将重新同步当前登录账号的群并清理其他账号的空群。历史消息和报告会保留。')) {
          await readGroups('settings.groups.prune');
        }
      })), groupStatus, groupList);

  const filterInputs = {};
  const filterChecks = filterFields.map(([key, label]) => {
    const [input, toggle] = check(label);
    filterInputs[key] = input;
    return toggle;
  });
  const minLength = make('input', { type: 'number', min: '0' });
  const sender = make('input', { type: 'text', placeholder: '输入完整的发言人名称', ariaLabel: '过滤发言人名称' });
  const senderList = make('select', { multiple: true, size: 5, ariaLabel: '过滤发言人名单' });
  function addSender() {
    const name = sender.value.trim();
    if (name && ![...senderList.options].some(option => option.value === name)) {
      senderList.append(make('option', { value: name }, name));
    }
    sender.value = '';
  }
  sender.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); addSender(); } };
  const filters = section('接收与发言人过滤', row(...filterChecks), field('消息字数下限（0 不限制）', minLength),
    row(sender, button('添加发言人', async () => addSender()),
      button('移除选中项', async () => { for (const option of [...senderList.selectedOptions]) option.remove(); })), senderList,
    explain('按完整名称匹配，Q群管家同时匹配已知账号。保存后重启消息服务。'));

  const qqAccount = make('input', { type: 'text', inputMode: 'numeric' });
  const napcatDir = make('input', { type: 'text' });
  const qqPath = make('input', { type: 'text' });
  const [hidden, hiddenToggle] = check('隐藏 NapCat 控制台窗口');
  const protocol = section('协议端（NapCat）', field('目标 QQ 号', qqAccount),
    row(field('本机 NapCat 目录', napcatDir), button('选择目录', async () => {
      const path = await app.call('desktop.pickDirectory', { kind: 'napcat' });
      if (path) napcatDir.value = path;
    })), row(field('QQ.exe 路径（可选）', qqPath), button('选择文件', async () => {
      const path = await app.call('desktop.pickFile', { kind: 'qq' });
      if (path) qqPath.value = path;
    })), hiddenToggle);

  const theme = make('select', {});
  options(theme, [['dark', '深色'], ['light', '浅色']], 'dark');
  theme.onchange = () => {
    if (!view) return;
    const name = theme.value;
    theme.value = view.theme;
    void perform(async () => {
      const result = await app.call('settings.theme', { name });
      view.theme = result.theme;
      theme.value = result.theme;
      app.setTheme(result.theme);
      notify('外观设置已保存。');
    });
  };
  const iconPath = explain('');
  const paths = explain('');
  const appearance = section('外观与本机配置', row(field('主题', theme),
    button('选择程序图标', async () => {
      const path = await app.call('desktop.pickFile', { kind: 'icon' });
      if (!path) return;
      const result = await app.call('settings.icon', { path });
      iconPath.textContent = result.iconPath;
      notify('已保存程序图标。');
    }), button('打开偏好文件', async () => {
      await app.call('desktop.openPreferences');
    })), iconPath, paths);

  async function load() {
    view = await app.call('settings.get');
    if (disposed) return;
    drafts.clear();
    populateForms();
    status.textContent = '本机偏好已加载。';
    await readGroups();
  }

  function populateForms() {
    showProfile();
    summaryPrompt.value = view.summaryPrompt;
    targetChars.value = view.targetChars;
    promptEnabled.checked = view.promptEvolution.enabled;
    promptDays.value = view.promptEvolution.days;
    promptInterval.value = view.promptEvolution.interval_days;
    imageEnabled.checked = view.image.enabled;
    setFields(imageInputs, view.image);
    selectedGroups.clear();
    for (const id of view.groups) selectedGroups.add(id);
    for (const [key, input] of Object.entries(filterInputs)) input.checked = view.filters[key];
    options(senderList, view.filters.blocked_senders.map(name => [name, name]), '');
    minLength.value = view.filters.min_text_length;
    qqAccount.value = view.account;
    napcatDir.value = view.napcatDir;
    qqPath.value = view.qqPath;
    hidden.checked = view.hidden;
    theme.value = view.theme;
    app.setTheme(view.theme);
    iconPath.textContent = view.iconPath ? `当前图标 ${view.iconPath}` : '';
    paths.textContent = `主配置 ${view.configPath} · 个人偏好 ${view.prefsPath}`;
  }

  const top = make('fieldset', { className: 'settings-actions' }, row(
    button('保存设置', save), button('重新加载', load, { loading: true }),
    ...[...sections].map(([title, section]) => make('button', { type: 'button', onclick: () => section.scrollIntoView({ block: 'start' }) }, title))));
  page.append(top, api, summary, image, groups, filters, protocol, appearance);
  void perform(load, { loading: true });
  void readHealth();
  const healthTimer = setInterval(readHealth, 3000);
  return () => { disposed = true; clearInterval(healthTimer); };
}

function editRules(view, app, notify) {
  const dialog = make('dialog', { className: 'settings-rules' });
  const list = make('div', { className: 'settings-rule-list' });
  const sample = make('textarea', { rows: 4, maxLength: 12000, placeholder: '粘贴 OCR 文字试匹配' });
  const feedback = make('p', { role: 'status' });
  const editors = [];
  let busy = false;

  function add(rule) {
    const controls = {};
    const [enabled, toggle] = check('启用', rule.enabled);
    const mode = make('select', {});
    options(mode, [['literal', '普通词'], ['regex', '正则']], rule.mode);
    const editor = { controls, enabled, mode };
    const fields = ['name', 'pattern', 'score', 'max_hits', 'repeat_factor'];
    for (const key of fields) controls[key] = make('input', { type: 'text', value: String(rule[key]) });
    const card = make('fieldset', { className: 'panel' }, make('legend', {}, '主题规则'),
      make('div', { className: 'settings-grid' }, field('名称', controls.name), field('匹配方式', mode), field('表达式', controls.pattern),
        field('分数', controls.score), field('最多计分次数', controls.max_hits), field('重复计分倍率', controls.repeat_factor)),
      row(toggle, make('button', { type: 'button', onclick: () => { editors.splice(editors.indexOf(editor), 1); card.remove(); } }, '删除此规则')));
    editors.push(editor);
    list.append(card);
  }

  function render(rules) {
    editors.length = 0;
    list.replaceChildren();
    rules.forEach(add);
  }

  function rules() {
    return editors.map(({ controls, enabled, mode }) => ({ ...readFields(controls), enabled: enabled.checked, mode: mode.value }));
  }

  function action(label, execute) {
    return make('button', { type: 'button', onclick: async () => {
      if (busy) return;
      busy = true;
      try { await execute(); } catch (error) { feedback.textContent = error.message; }
      finally { busy = false; }
    } }, label);
  }

  render(view.image.keyword_rules);
  dialog.append(make('h2', {}, '图片主题与评分规则'),
    row(action('新增规则', async () => add({ name: '', pattern: '', mode: 'literal', score: 20, max_hits: 2, repeat_factor: 0.5, enabled: true })),
      action('载入初始规则', async () => render(await app.call('settings.image.defaults')))),
    list, field('本地试匹配', sample), feedback,
    row(action('试匹配', async () => {
      const result = await app.call('settings.image.test', { sample: sample.value, rules: rules() });
      feedback.textContent = `本地得分 ${result.score}/100 · ${result.reason}`;
    }), action('保存主题规则', async () => {
      view.image.keyword_rules = await app.call('settings.image.rules', { rules: rules() });
      notify(`已保存 ${view.image.keyword_rules.length} 条图片主题规则。`);
      dialog.close();
    }), action('关闭', async () => dialog.close())));
  dialog.oncancel = event => { if (busy) event.preventDefault(); };
  dialog.onclose = () => dialog.remove();
  document.body.append(dialog);
  dialog.showModal();
}
