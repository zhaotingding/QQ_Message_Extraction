import fs from 'node:fs'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import signing from './signed-feed.cjs'
import { withPortableProtocol, componentName, componentHash, sha256 } from './portable-protocol.mjs'

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
  'test-public-key': { type: 'string' },
  'electron-zip': { type: 'string' },
  'local-preview': { type: 'boolean' },
} })
const test = Boolean(options['test-feed'])
const preview = Boolean(options['local-preview'])
if (preview && (test || options['test-version'])) throw new Error('本地安装验收与双版本更新验收请分别构建。')
if (options['test-public-key'] && !test) throw new Error('--test-public-key 只适用于回环测试包。')
if (preview || test) {
  process.env.CSC_IDENTITY_AUTO_DISCOVERY = 'false'
  for (const key of ['WIN_CSC_LINK', 'WIN_CSC_KEY_PASSWORD', 'CSC_LINK', 'CSC_KEY_PASSWORD']) delete process.env[key]
}
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
// 正式信任根来自仓库中的发布者公钥；测试密钥必须显式给出，不能混入正式包。
const publicFile = test ? options['test-public-key'] : path.join(installer, 'update-public.pem')
if (!preview) {
  if (!publicFile || !fs.existsSync(publicFile)) throw new Error('缺少更新公钥。请按 README 首次发布步骤设置公钥。')
  signing.readPublicKey(fs.readFileSync(publicFile, 'utf8'))
}
const backend = path.join(installer, 'dist', 'backend', 'msgx-backend')
if (!fs.existsSync(path.join(backend, 'msgx-backend.exe'))) {
  throw new Error('请先在仓库根目录用项目 Conda 环境运行 python -s installer/build_backend.py。')
}
const output = path.join(installer, 'dist', test ? 'test' : 'release', preview ? appVersion + '-project-local' : appVersion)
const icon = path.join(root, 'assets', 'app-icon.ico')

// 回环测试独立安装。EXE 的系统代码签名可选，在线更新必须验证发布清单签名。
const config = {
  appId: test ? 'msgx.desktop.update-test' : 'msgx.desktop',
  productName: test ? '消息萃取（更新验收）' : '消息萃取',
  executableName: test ? 'msgx-update-test' : 'msgx-desktop',
  extraMetadata: { name: test ? 'msgx-update-test' : manifest.name,
    version: appVersion, msgxInstalled: true, msgxLocalUpdateTest: test, msgxLocalPreview: preview,
    ...(test ? { msgxTestFeed: publish.url } : {}) },
  electronVersion: manifest.devDependencies.electron,
  ...(options['electron-zip'] ? { electronDist: path.resolve(options['electron-zip']) } : {}),
  directories: { output, buildResources: path.dirname(icon) },
  files: ['main.cjs', 'preload.cjs', 'renderer/**',
    { from: installer, to: 'installer', filter: ['update.cjs', 'signed-feed.cjs', 'installed-profile.cjs'] }],
  extraResources: [
    ...(!preview ? [{ from: path.resolve(publicFile), to: 'update-public.pem' }] : []),
    { from: backend, to: 'backend' },
    { from: path.join(root, 'config.yaml'), to: 'config.yaml' },
    { from: icon, to: 'assets/app-icon.ico' },
    { from: path.join(root, 'tools', 'restart_napcat_target.ps1'), to: 'tools/restart_napcat_target.ps1' },
    { from: path.join(root, 'tools', 'napcat-lifetime.cjs'), to: 'tools/napcat-lifetime.cjs' },
  ],
  publish: preview ? null : publish,
  npmRebuild: false,
  win: { icon },
  nsis: {
    include: path.join(installer, 'installer.nsh'),
    oneClick: false,
    perMachine: false,
    deleteAppDataOnUninstall: false,
    artifactName: preview ? 'msgx-setup-project-${version}-${arch}-unsigned.exe'
      : test ? 'msgx-update-test-${version}-${arch}.exe' : 'msgx-setup-${version}-${arch}.exe',
  },
}

const releaseBase = test ? publish.url.replace(/\/?$/, '/') :
  `https://github.com/${publish.owner}/${publish.repo}/releases/download/v${appVersion}/`
const nsis = require('app-builder-lib/out/toolsets/windows')
const plugins = path.join(await nsis.getNsisPluginsPath(), 'x86-unicode')
await withPortableProtocol(installer, backend, output, async support => {
  const include = path.join(support, 'component-config.nsh')
  fs.writeFileSync(include, [
    `!addplugindir /x86-unicode "${plugins}"`,
    `!define PROTOCOL_NAME "${componentName}"`, `!define PROTOCOL_HASH "${componentHash}"`,
    `!define PROTOCOL_URL "${releaseBase}${componentName}"`, `!define PROTOCOL_SUPPORT "${support}"`,
    `!include "${config.nsis.include}"`,
  ].join('\n'))
  await build({ projectDir: desktop, targets: Platform.WINDOWS.createTarget(['nsis'], Arch.x64),
    publish: 'never', config: { ...config, nsis: { ...config.nsis, include } } })
})

// 更新器继续下载签名清单指定的主安装包；首次安装的轻量入口只信任本次构建的哈希。
const core = config.nsis.artifactName.replace('${version}', appVersion).replace('${arch}', 'x64')
const compiler = await nsis.getMakeNsisPath()
execFileSync(compiler.path, ['/V2', '/INPUTCHARSET', 'UTF8',
  `/DBOOT_PLUGINS=${plugins}`,
  `/DBOOT_OUTPUT=${path.join(output, 'Setup.exe')}`, `/DBOOT_ICON=${icon}`,
  `/DCORE_NAME=${core}`, `/DCORE_URL=${releaseBase}${core}`,
  `/DCORE_HASH=${await sha256(path.join(output, core))}`, path.join(installer, 'bootstrap.nsi')],
  { env: { ...process.env, ...compiler.env }, stdio: 'inherit', windowsHide: true })

if (!preview && !fs.existsSync(path.join(output, 'latest.yml'))) {
  throw new Error('安装包未生成更新清单 latest.yml。')
}
