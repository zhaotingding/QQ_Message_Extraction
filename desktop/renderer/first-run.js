import { button, el, field } from './controls.js'

export function showFirstRun(app) {
  const dialog = el('dialog', { className: 'first-run-dialog', id: 'first-run-dialog',
    'aria-labelledby': 'first-run-title' })
  const theme = el('select', { id: 'first-run-theme' },
    el('option', { value: 'dark' }, '深色'), el('option', { value: 'light' }, '浅色'))
  const profile = el('select', { id: 'first-run-profile' })
  const key = el('input', { id: 'first-run-secret', type: 'password', autocomplete: 'new-password',
    spellcheck: false, 'aria-describedby': 'first-run-key-status' })
  const keyStatus = el('p', { id: 'first-run-key-status', className: 'muted', role: 'status' })
  const status = el('p', { id: 'first-run-status', className: 'muted', role: 'status' }, '正在读取设置…')
  const form = el('form', { className: 'first-run-form' })
  const skip = button('跳过', () => void finish(false))
  const save = button('保存并继续', undefined, { type: 'submit', className: 'primary' })
  let view
  let saving = false
  let statusRequest = 0

  function setControls() {
    theme.disabled = key.disabled = save.disabled = !view || saving
    // 已输入的密钥只属于此档案，清空输入后才能切换。
    profile.disabled = key.disabled || Boolean(key.value)
    skip.disabled = saving
  }

  function credentialArgs() {
    return { id: profile.value, base_url: view.profiles[profile.value].base_url }
  }

  async function refreshCredential() {
    const request = ++statusRequest
    const args = credentialArgs()
    keyStatus.textContent = `${args.base_url} · 正在读取密钥状态…`
    try {
      const result = await app.call('settings.key.status', args)
      if (request === statusRequest) {
        keyStatus.textContent = `${args.base_url} · ${result.configured ? '已配置密钥，留空保留' : '未配置密钥，可稍后填写'}`
      }
    } catch {
      if (request === statusRequest) keyStatus.textContent = `${args.base_url} · 无法读取密钥状态`
    }
  }

  async function finish(apply) {
    if (saving || (apply && !view)) return
    saving = true
    setControls()
    status.className = 'muted'
    status.textContent = apply ? '正在保存…' : '正在打开工作台…'
    try {
      if (apply && theme.value !== view.theme) {
        const result = await app.call('settings.theme', { name: theme.value })
        view.theme = result.theme
        app.setTheme(result.theme)
      }
      if (apply && key.value.trim()) {
        await app.call('settings.key.save', { ...credentialArgs(), key: key.value })
        key.value = ''
        void refreshCredential()
      }
      await app.call('desktop.setup.complete')
      app.state.setupPending = false
      statusRequest++
      key.value = ''
      dialog.close()
      dialog.remove()
    } catch (error) {
      status.className = 'error'
      status.textContent = '未能完成设置：' + error.message
    } finally {
      saving = false
      setControls()
    }
  }

  theme.value = app.state.theme
  profile.onchange = () => void refreshCredential()
  key.oninput = setControls
  form.onsubmit = event => {
    event.preventDefault()
    void finish(true)
  }
  dialog.addEventListener('cancel', event => event.preventDefault())
  form.append(field('主题', theme), field('API 档案', profile), field('新 API 密钥（可选）', key), keyStatus,
    status, el('div', { className: 'first-run-actions' }, skip, save))
  dialog.append(el('h2', { id: 'first-run-title' }, '首次设置'), form)
  setControls()
  document.body.append(dialog)
  dialog.showModal()

  void app.call('settings.get').then(settings => {
    if (!dialog.open) return
    view = settings
    theme.value = view.theme
    profile.replaceChildren(...Object.entries(view.profiles).map(([id, details]) =>
      el('option', { value: id }, `${details.provider} / ${details.model}${id === view.active ? '（当前摘要）' : ''}`)))
    profile.value = view.active
    if (!saving) status.textContent = '主题和密钥也可以稍后在设置中修改。'
    setControls()
    void refreshCredential()
  }).catch(error => {
    if (!dialog.open || saving) return
    status.className = 'error'
    status.textContent = '无法读取设置：' + error.message
  })
}
