// 构建完成后由发布者单独运行。口令由 OpenSSL 在终端询问，私钥不进入构建和打包流程。
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import signing from './signed-feed.cjs'

const [directory, keyFile] = process.argv.slice(2)
if (!directory || !keyFile) throw new Error('用法：node installer/sign-release.mjs <构建输出目录> <私钥 PEM 路径>')
const output = path.resolve(directory)
const key = signing.readPublicKey(fs.readFileSync(path.join(output, 'win-unpacked/resources/update-public.pem'), 'utf8'))
const manifest = path.join(output, 'latest.yml')
const raw = fs.readFileSync(manifest)
const require = createRequire(new URL('../desktop/package.json', import.meta.url))
const { parseUpdateInfo } = require('electron-updater/out/providers/Provider')
const test = fs.existsSync(path.join(output, 'win-unpacked/msgx-update-test.exe'))
const info = signing.parseManifest(raw.toString('utf8'), parseUpdateInfo, test)
const file = info.files[0]
const asset = path.join(output, file.url)
const hash = createHash('sha512')
for await (const chunk of fs.createReadStream(asset)) hash.update(chunk)
if (fs.statSync(asset).size !== file.size || hash.digest('base64') !== file.sha512) {
  throw new Error('安装包与 latest.yml 不匹配，请重新构建。')
}
const result = spawnSync('openssl', ['pkeyutl', '-sign', '-rawin', '-inkey', path.resolve(keyFile), '-in', manifest],
  { stdio: ['inherit', 'pipe', 'inherit'] })
if (result.error) throw result.error
if (result.status !== 0) throw new Error('OpenSSL 签名失败。')
const signature = result.stdout.toString('base64')
// 私钥必须与本次安装包嵌入的公钥配对，文件也不能在摘要检查后被替换。
signing.verifyManifest(raw, signature, key)
if (!fs.readFileSync(manifest).equals(raw)) throw new Error('清单在签名期间发生变化。')
fs.writeFileSync(manifest + '.sig', signature + '\n')
console.log(`已验证并签署 ${file.url} 的更新清单：${manifest}.sig`)
