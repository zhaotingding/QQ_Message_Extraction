const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { test } = require('node:test')
const { installedProfile } = require('../installer/installed-profile.cjs')

function fixture(t) {
  // 每项测试单独使用合成资料根，不读取当前用户资料。
  const tempRoot = path.resolve(os.tmpdir())
  const root = fs.mkdtempSync(path.join(tempRoot, 'msgx-profile-test-'))
  const previousLocalAppData = process.env.LOCALAPPDATA
  const previousArgv = process.argv
  const localAppData = path.join(root, 'LocalAppData')
  const resources = path.join(root, 'installed', 'resources')
  const defaultRoot = path.join(localAppData, 'msgx', 'profile')
  process.env.LOCALAPPDATA = localAppData
  process.argv = ['msgx-desktop.exe']
  t.after(() => {
    process.argv = previousArgv
    if (previousLocalAppData === undefined) delete process.env.LOCALAPPDATA
    else process.env.LOCALAPPDATA = previousLocalAppData
    assert.equal(path.dirname(root), tempRoot)
    assert.match(path.basename(root), /^msgx-profile-test-[A-Za-z0-9]+$/)
    fs.rmSync(root, { recursive: true, force: true })
  })

  function write(file, contents = '') {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, contents)
  }

  function repository() {
    const repo = path.join(root, '原消息萃取项目')
    const installerFolder = path.join(repo, 'installer', 'dist', 'release', 'local')
    fs.mkdirSync(installerFolder, { recursive: true })
    for (const file of ['src/msgx/__init__.py', 'desktop/package.json', 'installer/build-installer.mjs', 'config.yaml']) {
      write(path.join(repo, file))
    }
    return { repo, installerFolder }
  }

  // NSIS 写入的是 UTF-16LE 程序目录来源，路径可能含中文或空格。
  const recordOrigin = dir => write(path.join(resources, 'installer-origin.txt'), Buffer.from(dir, 'utf16le'))
  const open = (testBuild = false) => installedProfile({ getPath: () => localAppData }, testBuild, resources)
  return { resourceRoot: root, write, repository, recordOrigin, open, defaultRoot }
}

test('显式资料根和已有资料选择优先于安装包来源及默认资料', t => {
  const { resourceRoot, repository, recordOrigin, write, open, defaultRoot } = fixture(t)
  const { repo, installerFolder } = repository()
  const previousRoot = path.join(resourceRoot, '之前指定的资料目录')
  write(path.join(previousRoot, '.ui', 'preferences.yaml'), 'theme: light\n')
  write(path.join(repo, '.ui', 'preferences.yaml'), 'theme: dark\n')
  write(path.join(defaultRoot, 'data', 'msgx.db'), 'default history')
  recordOrigin(installerFolder)

  process.argv.push('--msgx-data-root', previousRoot)
  const selected = open()
  assert.equal(selected.root, previousRoot)
  selected.saveSelection()
  process.argv = ['msgx-desktop.exe']
  assert.equal(open().root, previousRoot)
  assert.equal(fs.readFileSync(path.join(defaultRoot, 'data', 'msgx.db'), 'utf8'), 'default history')
})

for (const file of ['.ui/preferences.yaml', '.ui/config.local.yaml', 'data/msgx.db']) {
  test(`默认资料目录中的 ${file} 优先于源码目录`, t => {
    const { repository, recordOrigin, write, open, defaultRoot } = fixture(t)
    const { repo, installerFolder } = repository()
    write(path.join(repo, '.ui', 'preferences.yaml'), 'theme: dark\n')
    write(path.join(defaultRoot, file), 'existing data')
    recordOrigin(installerFolder)

    assert.equal(open().root, defaultRoot)
    assert.equal(fs.readFileSync(path.join(defaultRoot, file), 'utf8'), 'existing data')
  })
}

test('直接从源码项目安装时默认原地沿用资料并记住路径', t => {
  const { repository, recordOrigin, write, open, defaultRoot } = fixture(t)
  const { repo, installerFolder } = repository()
  const prefs = path.join(repo, '.ui', 'preferences.yaml')
  const history = path.join(repo, 'data', 'msgx.db')
  write(prefs, 'theme: light\n')
  write(history, 'source history')
  write(path.join(defaultRoot, '.ui', 'electron', 'Cache', 'sample'), 'browser cache')
  recordOrigin(installerFolder)

  const selected = open()
  assert.equal(selected.root, repo)
  selected.saveSelection()
  recordOrigin(path.dirname(repo))
  assert.equal(open().root, repo)
  assert.equal(fs.readFileSync(prefs, 'utf8'), 'theme: light\n')
  assert.equal(fs.readFileSync(history, 'utf8'), 'source history')
  assert.equal(fs.existsSync(path.join(defaultRoot, 'data', 'msgx.db')), false)
})

test('来源不在项目 installer 目录下时使用默认资料目录', t => {
  const { repository, recordOrigin, write, open, defaultRoot } = fixture(t)
  const { repo } = repository()
  const download = path.join(repo, 'downloads')
  fs.mkdirSync(download)
  write(path.join(repo, 'data', 'msgx.db'), 'outside installer history')
  recordOrigin(download)

  assert.equal(open().root, defaultRoot)
})

test('普通文件夹仅含同名 installer 目录和偏好时使用默认资料目录', t => {
  const { resourceRoot, recordOrigin, write, open, defaultRoot } = fixture(t)
  const other = path.join(resourceRoot, '外部目录')
  const download = path.join(other, 'installer', 'packages')
  fs.mkdirSync(download, { recursive: true })
  write(path.join(other, '.ui', 'preferences.yaml'), 'theme: dark\n')
  recordOrigin(download)

  assert.equal(open().root, defaultRoot)
})

test('验收身份只使用隔离资料，首次设置完成状态按身份保留', t => {
  const { repository, recordOrigin, write, open } = fixture(t)
  const { repo, installerFolder } = repository()
  const prefs = path.join(repo, '.ui', 'preferences.yaml')
  write(prefs, 'theme: light\n')
  recordOrigin(installerFolder)

  const production = open()
  const updateTest = open(true)
  assert.equal(production.root, repo)
  assert.match(updateTest.root, /[\\/]msgx-update-test[\\/]profile$/)
  assert.equal(production.setupPending(), true)
  assert.equal(updateTest.setupPending(), true)

  production.completeSetup()
  assert.equal(open().setupPending(), false)
  assert.equal(open(true).setupPending(), true)
  updateTest.completeSetup()
  assert.equal(open(true).setupPending(), false)
  assert.equal(fs.readFileSync(prefs, 'utf8'), 'theme: light\n')
})
