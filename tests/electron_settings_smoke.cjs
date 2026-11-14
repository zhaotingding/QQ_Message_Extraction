// 真窗口到本机设置桥接的验收，调用者必须先指定隔离数据根。
const assert = require('node:assert/strict')

module.exports = async function checkSettings(evaluate) {
  const wait = async (condition, label) => {
    for (let i = 0; i < 80; i++) {
      if (await evaluate(condition)) return
      await new Promise(resolve => setTimeout(resolve, 125))
    }
    const failure = await evaluate(`document.querySelector('#page-settings [role="status"]')?.textContent`)
    throw new Error(`${label}，当前反馈 ${failure}`)
  }
  const click = label => evaluate(`(() => {
    const button = [...document.querySelectorAll('#page-settings button')].find(b => b.textContent === ${JSON.stringify(label)});
    if (!button) return false;
    button.click();
    return true;
  })()`)
  const value = (label, content, change = false) => evaluate(`(() => {
    const input = [...document.querySelectorAll('#page-settings .settings-field')]
      .find(e => e.firstElementChild.textContent === ${JSON.stringify(label)})?.lastElementChild;
    if (!input) return false;
    input.value = ${JSON.stringify(content)};
    if (${change}) input.dispatchEvent(new Event('change'));
    return true;
  })()`)
  await evaluate(`document.querySelector('[data-page="settings"]').click(); true`)
  await wait(`!document.querySelector('#page-settings .settings-actions')?.disabled && document.querySelector('#page-settings [role="status"]')?.textContent.includes('本机偏好已加载')`, '设置未呈现')
  assert.ok(await value('主题', 'light', true))
  await wait(`document.documentElement.dataset.theme === 'light'`, '浅色主题未设置')
  assert.ok(await value('日报目标字数', '4567'))
  assert.ok(await click('保存设置'))
  await wait(`document.querySelector('#page-settings [role="status"]')?.textContent.startsWith('设置已保存')`, '保存设置失败')
  assert.ok(await value('日报目标字数', '9999'))
  assert.ok(await click('重新加载'))
  await wait(`!document.querySelector('#page-settings .settings-actions')?.disabled && [...document.querySelectorAll('#page-settings .settings-field')].find(e => e.firstElementChild.textContent === '日报目标字数')?.lastElementChild.value === '4567'`, '已保存设置未读回')
  assert.ok(await click('编辑图片主题与评分规则'))
  await wait(`document.querySelector('dialog.settings-rules')?.open`, '图片规则弹窗未打开')
  assert.ok(await evaluate(`(() => {
    const dialog = document.querySelector('dialog.settings-rules');
    dialog.querySelector('textarea').value = 'GPU 模型训练信息';
    [...dialog.querySelectorAll('button')].find(b => b.textContent === '试匹配').click();
    return true;
  })()`))
  await wait(`document.querySelector('dialog.settings-rules [role="status"]')?.textContent.includes('本地得分')`, '图片规则试匹配失败')
  await evaluate(`[...document.querySelectorAll('dialog.settings-rules button')].find(b => b.textContent === '关闭').click(); true`)
  assert.ok(await value('主题', 'dark', true))
  await wait(`document.documentElement.dataset.theme === 'dark'`, '深色主题未恢复')
  return '主题与 YAML 保存读回、图片规则本地试匹配通过'
}
