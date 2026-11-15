import fs from 'node:fs'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { createRequire } from 'node:module'

const installer = import.meta.dirname
const root = path.dirname(installer)
const desktop = path.join(root, 'desktop')
// 依赖仍由 desktop/package.json 唯一维护，移动入口不另建 node_modules。
const require = createRequire(path.join(desktop, 'package.json'))
const { Arch, Platform, build } = require('electron-builder')
const manifest = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8'))
const { values: options } = parseArgs({ options: {
  'test-feed': { type: 'string' },
  'test-version': { type: 'string' },
  'electron-zip': { type: 'string' },
} })
const test = Boolean(options['test-feed'])
const version = fs.readFileSync(path.join(root, 'src', 'msgx', '__init__.py'), 'utf8')
  .match(/^__version__\s*=\s*"([^"]+)"/m)?.[1]
if (!version) throw new Error('读取消息萃取的源码版本失败。')

let publish = { provider: 'github', owner: 'zhaotingding', repo: 'QQ_Message_Extraction' }
if (test) {
  const feed = new URL(options['test-feed'])
  if (feed.protocol !== 'http:' || feed.hostname !== '127.0.0.1' || !feed.port ||
      feed.username || feed.password || feed.search || feed.hash ||
      !/^\d+\.\d+\.\d+$/.test(options['test-version'] || '')) {
    throw new Error('本地验收仅使用 127.0.0.1 HTTP 更新源及独立的三段式 --test-version。')
  }
  publish = { provider: 'generic', url: feed.href }
} else if (options['test-version']) {
  throw new Error('--test-version 必须与 --test-feed 一起使用。')
}

const appVersion = test ? options['test-version'] : version
const backend = path.join(installer, 'dist', 'backend', 'msgx-backend')
if (!fs.existsSync(path.join(backend, 'msgx-backend.exe'))) {
  throw new Error('请先在仓库根目录用项目 Conda 环境运行 python -s installer/build_backend.py。')
}
const output = path.join(installer, 'dist', test ? 'test' : 'release', appVersion)
const icon = path.join(root, 'assets', 'app-icon.ico')

// 本地双版本验收用独立安装标识和下载源。正式包必须由发布者签名，并只读 GitHub 正式版本。
const config = {
  appId: test ? 'msgx.desktop.update-test' : 'msgx.desktop',
  productName: test ? '消息萃取（更新验收）' : '消息萃取',
  executableName: test ? 'msgx-update-test' : 'msgx-desktop',
  extraMetadata: { name: test ? 'msgx-update-test' : manifest.name,
    version: appVersion, msgxInstalled: true, msgxLocalUpdateTest: test },
  electronVersion: manifest.devDependencies.electron,
  ...(options['electron-zip'] ? { electronDist: path.resolve(options['electron-zip']) } : {}),
  directories: { output, buildResources: path.dirname(icon) },
  files: ['main.cjs', 'preload.cjs', 'renderer/**',
    { from: installer, to: 'installer', filter: ['update.cjs', 'installed-profile.cjs'] }],
  extraResources: [
    { from: backend, to: 'backend' },
    { from: path.join(root, 'config.yaml'), to: 'config.yaml' },
    { from: icon, to: 'assets/app-icon.ico' },
    { from: path.join(root, 'tools', 'restart_napcat_target.ps1'), to: 'tools/restart_napcat_target.ps1' },
  ],
  publish,
  npmRebuild: false,
  forceCodeSigning: !test,
  win: { icon, verifyUpdateCodeSignature: !test },
  nsis: {
    include: path.join(installer, 'installer.nsh'),
    oneClick: true,
    perMachine: false,
    deleteAppDataOnUninstall: false,
    artifactName: test ? 'msgx-update-test-${version}-${arch}.exe' : 'msgx-setup-${version}-${arch}.exe',
  },
}

await build({ projectDir: desktop, targets: Platform.WINDOWS.createTarget(['nsis'], Arch.x64),
  publish: 'never', config })

if (!fs.existsSync(path.join(output, 'latest.yml'))) {
  throw new Error('安装包未生成更新清单 latest.yml。')
}
if (!test) {
  const updaterConfig = fs.readFileSync(path.join(output, 'win-unpacked', 'resources', 'app-update.yml'), 'utf8')
  if (!/^publisherName:(?:[ \t]+\S|\r?\n[ \t]*-[ \t]+\S)/m.test(updaterConfig)) {
    throw new Error('正式安装包缺少更新发布者身份，不能作为更新产物分发。')
  }
}
