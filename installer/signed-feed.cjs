const { createPublicKey, verify } = require('node:crypto')

const releaseRoot = 'https://github.com/zhaotingding/QQ_Message_Extraction/releases/'

// 发布端只接收公钥 PEM：即使给错文件，也不能把私钥复制到安装包里。
function readPublicKey(pem) {
  if (!/^-----BEGIN PUBLIC KEY-----\r?\n[A-Za-z0-9+/=\r\n]+\n-----END PUBLIC KEY-----$/.test(pem.trim())) {
    throw new Error('需要仅含 Ed25519 公钥的 PEM。')
  }
  const key = createPublicKey(pem)
  if (key.asymmetricKeyType !== 'ed25519') throw new Error('更新公钥必须是 Ed25519。')
  return key
}

function manifestError() {
  return Object.assign(new Error('更新清单或发布签名无效。'), { code: 'ERR_MSGX_SIGNED_MANIFEST' })
}

function parseManifest(raw, parseUpdateInfo, test) {
  const info = parseUpdateInfo(raw, 'latest.yml', 'signed feed')
  if (!/^\d+\.\d+\.\d+$/.test(info?.version) || info.files?.length !== 1 || info.packages) throw manifestError()
  const [file] = info.files
  const name = `${test ? 'msgx-update-test' : 'msgx-setup'}-${info.version}-x64.exe`
  if (file?.url !== name || !Number.isSafeInteger(file.size) || file.size <= 0 ||
      typeof file.sha512 !== 'string' || !/^[A-Za-z0-9+/]{86}==$/.test(file.sha512)) throw manifestError()
  return { version: info.version, files: [{ url: name, sha512: file.sha512, size: file.size }] }
}

function verifyManifest(raw, signature, key) {
  if (typeof signature !== 'string' || !/^[A-Za-z0-9+/]{86}==$/.test(signature.trim()) ||
      !verify(null, Buffer.from(raw), key, Buffer.from(signature.trim(), 'base64'))) throw manifestError()
}

// electron-updater 的 custom provider 扩展点。只解析刚刚验过签的同一份文本，
// 再把受签名保护的版本、固定安装包名、大小和 SHA-512 交给原有下载器。
function configureSignedFeed(updater, { Provider, parseUpdateInfo }, loadPublicPem, testFeed) {
  const test = Boolean(testFeed)
  const base = new URL(test ? testFeed : 'latest/download/', releaseRoot)
  if (test && (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port)) throw manifestError()
  class SignedProvider extends Provider {
    constructor(_config, _updater, runtime) {
      super({ ...runtime, isUseMultipleRangeRequest: false })
    }
    async getLatestVersion() {
      let key
      try {
        key = readPublicKey(loadPublicPem())
      } catch (cause) {
        throw Object.assign(new Error('无法读取安装版的更新公钥。', { cause }), { code: 'ERR_MSGX_UPDATE_KEY' })
      }
      const headers = { 'Cache-Control': 'no-cache' }
      const [raw, signature] = await Promise.all([
        this.httpRequest(new URL('latest.yml', base), headers),
        this.httpRequest(new URL('latest.yml.sig', base), headers),
      ])
      verifyManifest(raw, signature, key)
      return parseManifest(raw, parseUpdateInfo, test)
    }
    resolveFiles(info) {
      // latest 可能在点击下载前改变；正式下载固定到此次已验证版本的 tag。
      const versionBase = test ? base : new URL(`download/v${info.version}/`, releaseRoot)
      return info.files.map(file => ({ url: new URL(file.url, versionBase), info: file }))
    }
  }
  updater.disableWebInstaller = true
  updater.setFeedURL({ provider: 'custom', updateProvider: SignedProvider })
}

module.exports = { configureSignedFeed, parseManifest, readPublicKey, verifyManifest }
