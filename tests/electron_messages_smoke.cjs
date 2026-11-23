// 真实窗口验证消息读取/定位；群发交互使用内存替身，不向服务提交。
const assert = require('node:assert/strict');

module.exports = async function checkMessages(evaluate, capture) {
  const wait = async (condition, label) => {
    for (let i = 0; i < 80; i++) {
      if (await evaluate(condition)) return;
      await new Promise(resolve => setTimeout(resolve, 125));
    }
    throw new Error(label);
  };
  const click = (label, scope = '#page-messages') => evaluate(`[...document.querySelectorAll(${JSON.stringify(scope + ' button')})].find(b => b.textContent === ${JSON.stringify(label)}).click(); true`);
  await evaluate(`document.querySelector('[data-page="messages"]').click(); true`);
  await wait(`document.querySelector('.msgx-current')?.textContent.includes('Electron 实时验收入库')`, '接收消息未显示');
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('.msgx-current th')].map(th=>th.textContent)`),
    ['时间', '群', '发送者', '内容', '图片与识别']);
  for (const kind of ['group', 'sender']) {
    const target = `document.querySelector('.msgx-current tbody [data-identity="${kind}"]')`;
    const id = await evaluate(`${target}.dataset.id`);
    await evaluate(`${target}.click(); true`);
    assert.equal(await evaluate(`${target}.getAttribute('aria-pressed')`), 'true');
    await evaluate(`${target}.click(); true`);
    const condition = kind === 'group'
      ? `[...document.querySelectorAll('#page-messages select')].some(s=>s.value===${JSON.stringify(id)})`
      : `!document.querySelector('#page-messages .filter-chip').hidden`;
    await wait(condition, '二次点击未应用筛选');
  }
  await wait(`[...document.querySelectorAll('.msgx-current tbody tr')].every(r=>r.querySelector('[data-identity=group]')?.dataset.id==='90001' && r.querySelector('[data-identity=sender]')?.dataset.id==='100002')`, '同名发送者或其它群混入组合筛选');
  if (capture) await capture('message-filters.png');
  await click('清除筛选');
  await wait(`document.querySelectorAll('.msgx-current tbody [data-identity=group][data-id="90002"]').length >= 1`, '清除筛选未恢复跨群记录');
  await click('关键词消息');
  await click('配置关键词');
  await wait(`document.querySelector('dialog[open] textarea')?.getAttribute('aria-label') === '关键词规则，每行一条'`, '规则窗口未打开');
  await evaluate(`document.querySelector('dialog[open] textarea').value='实时验收入库'; true`);
  await click('保存并生效', 'dialog[open]');
  await wait(`[...document.querySelectorAll('.msgx-view-keywords .msgx-hit-row button')].some(b => b.textContent === 'Electron 实时验收入库')`, '关键词筛选未生效');
  await evaluate(`[...document.querySelectorAll('.msgx-view-keywords .msgx-hit-row button')].find(b => b.textContent === 'Electron 实时验收入库').click(); true`);
  await wait(`[...document.querySelectorAll('#page-messages p')].some(p => p.textContent.includes('同群历史上下文'))`, '关键词未定位到同群历史');
  await click('返回实时');
  await wait(`[...document.querySelectorAll('#page-messages p')].some(p => p.textContent.startsWith('实时消息 ·'))`, '无法返回实时列表');

  const result = await evaluate(`(async () => {
    const { mount } = await import('msgx://app/messages.js');
    const host = document.createElement('section');
    document.querySelector('#pages').prepend(host);
    const submitted = [];
    const fake = {
      state: {}, notify() {},
      async call(action, payload) {
        if (action === 'messages.recent') return { live:false, items:[], related:[], related_account:'888', activity:{} };
        if (action === 'messages.group-status') return { result:null, offline:true };
        if (action === 'messages.group-preview') return { remembered:[], result:{
          token:'example-token', account:'888', excluded:[], text:payload.text,
          groups:[{group_id:'90001', group_name:'甲群'}, {group_id:'90002', group_name:'乙群'}]
        } };
        if (action === 'messages.group-confirm') { submitted.push(payload); return { result:null }; }
        throw new Error('unexpected synthetic action: ' + action);
      }
    };
    const clean = mount(host, fake);
    const settle = () => new Promise(resolve => setTimeout(resolve, 0));
    const button = (prefix, within = host) => [...within.querySelectorAll('button')].find(b => b.textContent.startsWith(prefix));
    const change = input => input.dispatchEvent(new Event('change', { bubbles:true }));
    try {
      await settle();
      button('群发喊话').click();
      await settle();
      host.querySelector('textarea').value = '<img src=x onerror=evil> 核对正文';
      button('预览群发范围').click();
      await settle();
      const preview = host.querySelector('dialog[open]');
      const groups = [...preview.querySelectorAll('fieldset input')];
      const ack = preview.querySelector('input:not(fieldset input)');
      const submit = button('确认发送', preview);
      const initial = submit.disabled && groups.every(input => !input.checked)
        && preview.textContent.includes('QQ 888') && preview.textContent.includes('甲群（90001）')
        && preview.textContent.includes('乙群（90002）')
        && preview.querySelector('pre').textContent === '<img src=x onerror=evil> 核对正文'
        && !preview.querySelector('img');
      ack.checked = true;
      change(ack);
      const emptyRejected = submit.disabled;
      groups[1].checked = true;
      change(groups[1]);
      const selectionRequiresAck = !ack.checked && submit.disabled;
      ack.checked = true;
      change(ack);
      submit.click();
      await settle();
      button('预览群发范围').click();
      await settle();
      button('取消', host.querySelector('dialog[open]')).click();
      await settle();
      return { initial, emptyRejected, selectionRequiresAck, submitted };
    } finally {
      clean();
      host.remove();
    }
  })()`);
  assert.equal(result.initial, true, '预览未如实展示账号、正文或候选群，或初始群被预选');
  assert.equal(result.emptyRejected, true, '空选仍可以发送');
  assert.equal(result.selectionRequiresAck, true, '修改勾选后没有重新要求核对');
  assert.deepEqual(result.submitted, [{ token: 'example-token', confirmed: true, groups: ['90002'] }]);
  return '本地消息、关键词保存及同群定位通过。模拟群发验证了空选拦截、勾选后重新确认、精确群号和取消。';
};
