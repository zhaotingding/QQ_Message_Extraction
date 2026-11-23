// 在已导入隔离夹具的 Electron 窗口中走通读取、批注、切换保存与用量来源。
const assert = require('node:assert/strict')

module.exports = async function checkReaderAndUsage(evaluate, capture) {
  const wait = async (condition, label) => {
    for (let i = 0; i < 80; i++) {
      if (await evaluate(condition)) return
      await new Promise(resolve => setTimeout(resolve, 125))
    }
    throw new Error(label)
  }
  const go = page => evaluate(`document.querySelector('[data-page="${page}"]').click(); true`)
  await go('reader')
  await wait(`document.querySelector('.reader-article')?.textContent.includes('桌面验收摘要')`, '摘要未呈现')
  assert.equal(await evaluate(`document.querySelector('.reader-mark-blue')?.textContent`), '原有批注')
  assert.equal(await evaluate(`document.querySelector('select[aria-label="阅读状态"]').value`), 'skimmed')
  assert.equal(await evaluate(`document.querySelectorAll('.reader-outline button').length`), 2)
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('.reader-filters input:checked')].map(b => b.value)`),
    ['daily', 'weekly', 'monthly'])
  await capture?.('reader-colors.png')
  const palette = await evaluate(`[...document.querySelectorAll('.reader-entry')].map(b => {
    const rgb = getComputedStyle(b).backgroundColor.match(/\\d+/g).map(Number);
    return [b.dataset.readState, rgb];
  })`)
  assert.deepEqual(palette.map(([state]) => state), ['skimmed', 'deep', 'unread'])
  assert.ok(palette[0][1][2] > palette[0][1][1], '粗读卡片未呈现蓝色背景')
  assert.ok(palette[1][1][1] > palette[1][1][2], '精读卡片未呈现绿色背景')

  // Unicode 选区跨业务边界确认保存时没有被前面的 emoji 错位。
  await evaluate(`(() => {
    const mark = document.querySelector('.reader-mark-blue');
    const range = document.createRange();
    range.selectNodeContents(mark);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    [...document.querySelectorAll('#page-reader button')].find(b => b.textContent === '红色重点').click();
    return true;
  })()`)
  await wait(`document.querySelector('.reader-mark-red')?.textContent === '原有批注'`, '批注未写入')
  await evaluate(`(() => {
    const field = document.querySelector('textarea[aria-label="我的心得"]');
    field.value = 'Electron 验收心得 🧪';
    return true;
  })()`)
  await go('usage')
  await wait(`document.querySelector('.usage-page')?.textContent.includes('$0.000100')`, '用量明细未呈现')
  assert.equal(await evaluate(`document.querySelectorAll('.usage-chart svg').length`), 1)
  assert.ok(await evaluate(`!document.querySelector('.usage-page').textContent.includes('次未知') && document.querySelector('.spend-cards').textContent.includes('¥0.000000')`))
  // 未知条目可能排在前面，按关联消息选中已知账目。
  await evaluate(`[...document.querySelectorAll('.usage-source')].find(b => b.textContent.includes('消息 #')).click(); true`)
  await wait(`document.querySelector('.usage-sources')?.textContent.includes('关联消息验收文本')`, '账目关联消息未呈现')
  assert.ok(await evaluate(`(() => {
    const detail = [...document.querySelectorAll('.usage-table')].at(-1);
    const b = [...detail.querySelectorAll('th button')].find(b => b.textContent === '美元 USD');
    b.click();
    return detail.tBodies[0].rows[0].cells[7].textContent === '$0.000100' &&
           detail.tBodies[0].rows[1].cells[7].textContent === '—';
  })()`), '金额排序未区分未知费用')
  await go('reader')
  await wait(`document.querySelector('textarea[aria-label="我的心得"]')?.value === 'Electron 验收心得 🧪'`, '切换后心得未恢复')
  assert.equal(await evaluate(`document.querySelector('.reader-mark-red')?.textContent`), '原有批注')
  await evaluate(`document.querySelector('.reader-filters input[value="daily"]').click(); true`)
  await wait(`document.querySelector('.reader-article')?.textContent.includes('周报验收')
    && !document.querySelector('.reader-filters input').disabled`, '周报月报多选未生效')
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('.reader-entry strong')].map(b => b.textContent)`), ['周报验收', '月报验收'])
  assert.deepEqual(await evaluate(`window.msgx.call('reader.filters')`), ['weekly', 'monthly'])
  await go('status')
  await wait(`!document.querySelector('#page-reader')`, '阅读页未退出')
  await go('reader')
  await wait(`!document.querySelector('.reader-filters input').disabled
    && document.querySelectorAll('.reader-entry').length === 2`, '重新打开阅读页未恢复已保存筛选')
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('.reader-filters input:checked')].map(b => b.value)`), ['weekly', 'monthly'])
  return '双币账单、摘要多选及偏好读回、粗读精读卡片、Unicode 批注、切换保存、费用图表与来源消息通过'
}
