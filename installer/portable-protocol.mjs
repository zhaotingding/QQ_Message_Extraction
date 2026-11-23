import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { execFileSync } from 'node:child_process'

const tag = 'v4.18.42'
const commit = '9335843246e3b7a17944861f3ac8731a259732c2'
const archive = 'NapCat.Shell.Windows.Node.zip'
const mirror = `https://github.com/XingYunStar/NapCatQQ/releases/download/${tag}/${archive}`

export const componentName = `msgx-napcat-${tag}-x64.zip`
export const componentHash = '77397ac38d66f46383e48f08f35d5f9d37ec575c22069f62d6c600e73b4521ed'
// 固定上游资产只作为独立 Release 附件；用户可以预下载，也可在安装过程中领取。
async function cached(dir, name, url, digest) {
  const target = path.join(dir, name)
  if (!fs.existsSync(target)) {
    const pending = target + '.' + randomUUID() + '.tmp'
    try {
      execFileSync('curl.exe', ['--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
        '--retry', '2', '--output', pending, url], { stdio: 'inherit', windowsHide: true })
      if (await sha256(pending) !== digest) throw new Error(`${name} 下载校验失败。`)
      fs.renameSync(pending, target)
    } finally { fs.rmSync(pending, { force: true }) }
  }
  if (await sha256(target) !== digest) throw new Error(`组件缓存校验失败：${target}`)
  return target
}

export async function sha256(file) {
  const hash = createHash('sha256')
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk)
  return hash.digest('hex')
}

export async function withPortableProtocol(installer, backend, output, pack) {
  const cache = path.join(installer, 'dist', 'components', tag)
  fs.mkdirSync(cache, { recursive: true })
  const zip = await cached(cache, archive, mirror, componentHash)
  const license = await cached(cache, 'napcat-LICENSE.txt',
    `https://raw.githubusercontent.com/NapNeko/NapCatQQ/${commit}/LICENSE`,
    '2bbc0dba0c62fcde4adfe38ebadad0b7d4e23b06b88d9551904bbe07769dc46f')
  const nodeLicense = await cached(cache, 'node-LICENSE.txt', 'https://raw.githubusercontent.com/nodejs/node/v22.11.0/LICENSE',
    'f0cb1ad806ba5d681f378a8cc2e539fe4b54839290eda3c814ec8994af2b8422')
  const stage = fs.mkdtempSync(path.join(cache, 'pack-'))
  try {
    fs.mkdirSync(output, { recursive: true })
    fs.copyFileSync(zip, path.join(output, componentName))
    fs.copyFileSync(license, path.join(stage, 'NapCat-LICENSE.txt'))
    fs.copyFileSync(nodeLicense, path.join(stage, 'Node-LICENSE.txt'))
    fs.writeFileSync(path.join(stage, 'NOTICE.txt'),
      `NapCatQQ ${tag} (c) Mlikiowa. Non-commercial use. https://github.com/NapNeko/NapCatQQ\nArchive: ${mirror}\nNode.js 22.11.0: Node.js contributors. NTQQ runtime: Tencent.\n`)
    // 上游原生模块依赖 VC 运行库；复用后台分发的 x64 运行库副本，支持干净电脑。
    for (const file of ['msvcp140.dll', 'MSVCP140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']) {
      fs.copyFileSync(path.join(backend, '_internal', file), path.join(stage, file))
    }
    await pack(stage)
  } finally {
    fs.rmSync(stage, { recursive: true, force: true })
  }
}
