const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { test } = require('node:test')
const { createUpdates } = require('../installer/update.cjs')
const { createHash, generateKeyPairSync, sign } = require('node:crypto')
const { createRequire } = require('node:module')
const { join } = require('node:path')
const { configureSignedFeed, readPublicKey } = require('../installer/signed-feed.cjs')
const providerApi = createRequire(join(__dirname, '../desktop/package.json'))('electron-updater/out/providers/Provider')

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

test('清单验签绑定实际解析内容与下载版本；错误清单不提供安装候选', async () => {
  const owner = generateKeyPairSync('ed25519')
  const other = generateKeyPairSync('ed25519')
  const pem = owner.publicKey.export({ format: 'pem', type: 'spki' })
  const digest = createHash('sha512').update('fixture').digest('base64')
  const raw = `version: 1.0.8\nfiles:\n  - url: msgx-setup-1.0.8-x64.exe\n    size: 7\n    sha512: ${digest}\n`
  const signed = (body, key = owner.privateKey) => sign(null, Buffer.from(body), key).toString('base64')
  let body = raw
  let signature = signed(body)
  const { updater, manager, effects } = setup()
  let provider
  const requests = []
  updater.setFeedURL = config => {
    provider = new config.updateProvider(config, updater, { platform: 'win32' })
    provider.httpRequest = async url => {
      requests.push(url.href)
      return url.pathname.endsWith('.sig') ? signature : body
    }
  }
  configureSignedFeed(updater, providerApi, () => pem)
  updater.checkForUpdates = async () => ({ isUpdateAvailable: true, updateInfo: await provider.getLatestVersion() })
  assert.equal((await manager.check()).canInstall, true)
  const info = await provider.getLatestVersion()
  assert.equal(info.files[0].sha512, digest)
  assert.equal(provider.resolveFiles(info)[0].url.href,
    'https://github.com/zhaotingding/QQ_Message_Extraction/releases/download/v1.0.8/msgx-setup-1.0.8-x64.exe')
  assert(requests.every(url => url.includes('/releases/latest/download/latest.yml')))
  for (const [nextBody, nextSig] of [
    [raw, ''], [raw, signed(raw, other.privateKey)], [raw + '\n', signed(raw)],
    [raw.replace('1.0.8', '1.0.9'), signed(raw.replace('1.0.8', '1.0.9'))],
    [raw.replace(digest, ''), signed(raw.replace(digest, ''))],
  ]) {
    body = nextBody
    signature = nextSig
    assert.equal((await manager.check()).phase, 'error')
    assert.equal(manager.status().canInstall, false)
    await manager.updateAndRestart()
    assert.deepEqual(effects, [])
  }
  assert.throws(() => readPublicKey(pem + owner.privateKey.export({ type: 'pkcs8', format: 'pem' })))
  requests.length = 0
  configureSignedFeed(updater, providerApi, () => { throw new Error('missing key') })
  assert.match((await manager.check()).message, /公钥/)
  assert.deepEqual(requests, [])
})
