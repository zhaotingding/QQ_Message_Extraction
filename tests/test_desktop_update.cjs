const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { test } = require('node:test')
const { createUpdates } = require('../installer/update.cjs')

function setup(version = '1.0.6') {
  const updater = new EventEmitter()
  updater.checkForUpdates = async () => ({ isUpdateAvailable: true, updateInfo: { version } })
  const effects = []
  let downloaded
  updater.downloadUpdate = () => new Promise((resolve, reject) => {
    effects.push('download')
    downloaded = { resolve, reject }
  })
  updater.quitAndInstall = (...args) => effects.push(['install', ...args])
  const manager = createUpdates(updater, {
    notify: () => {},
    requestClose: () => { effects.push('close'); return true },
    restoreBackend: () => effects.push('restore'),
  })
  return { updater, effects, manager, downloaded: () => downloaded }
}

test('发现正式版后等待用户下载，校验结束后才请求安全关闭', async () => {
  const { updater, effects, manager, downloaded } = setup()
  assert.equal(updater.autoDownload, false)
  assert.equal(updater.autoInstallOnAppQuit, false)
  assert.equal(updater.allowPrerelease, false)
  assert.equal(updater.allowDowngrade, false)
  assert.equal((await manager.check()).version, '1.0.6')
  assert.deepEqual(effects, [])
  const request = manager.updateAndRestart()
  updater.emit('download-progress', { percent: 100 })
  assert.deepEqual(effects, ['download'])
  assert.match(manager.status().message, /校验/)
  await manager.updateAndRestart()
  downloaded().resolve()
  await request
  assert.deepEqual(effects, ['download', 'close'])
  // 取消关窗后保留验证过的下载，下一次确认可继续安装。
  manager.cancelRestart()
  manager.installAfterClose()
  assert.equal(manager.status().phase, 'ready')
  assert.deepEqual(effects, ['download', 'close'])
  await manager.updateAndRestart()
  manager.installAfterClose()
  assert.deepEqual(effects, ['download', 'close', 'close', ['install', true, false]])
  updater.emit('error', new Error('模拟安装器启动失败'))
  assert.equal(manager.status().canInstall, true)
  assert.equal(effects.at(-1), 'restore')
})

test('下载校验失败不停止应用或启动安装程序', async () => {
  const { effects, manager, downloaded } = setup()
  await manager.check()
  const request = manager.updateAndRestart()
  downloaded().reject(new Error('模拟校验失败'))
  assert.equal((await request).phase, 'error')
  manager.installAfterClose()
  assert.deepEqual(effects, ['download'])
})

test('源码版禁用更新，预发布不成为可用更新', async () => {
  const { manager, effects } = setup('1.0.6-beta.1')
  assert.equal((await manager.check()).canInstall, false)
  assert.equal(manager.status().version, '')
  await manager.updateAndRestart()
  assert.deepEqual(effects, [])
  const local = createUpdates(null, {})
  assert.equal((await local.check()).phase, 'unavailable')
})
