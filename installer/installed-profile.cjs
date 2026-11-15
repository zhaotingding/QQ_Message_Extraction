const fs = require('node:fs')
const path = require('node:path')

function hasProfileData(root) {
  return ['.ui/preferences.yaml', '.ui/config.local.yaml', 'data/msgx.db']
    .some(name => fs.existsSync(path.join(root, name)))
}

function sourceProfile(programRoot) {
  let from
  try {
    from = fs.readFileSync(path.join(programRoot, 'installer-origin.txt'), 'utf16le').trim()
  } catch (error) {
    if (error.code === 'ENOENT') return undefined
    throw error
  }
  if (!path.isAbsolute(from)) return undefined
  // 只有从原源码仓库的 installer/ 目录运行安装包，才有依据自动沿用该仓库资料。
  let dir = path.resolve(from)
  while (dir !== path.dirname(dir)) {
    if (path.basename(dir).toLowerCase() === 'installer') {
      const root = path.dirname(dir)
      if (fs.existsSync(path.join(root, 'src/msgx/__init__.py')) && hasProfileData(root)) return root
    }
    dir = path.dirname(dir)
  }
  return undefined
}

function installedProfile(app, testBuild, programRoot) {
  const base = path.join(process.env.LOCALAPPDATA || app.getPath('appData'),
    testBuild ? 'msgx-update-test' : 'msgx')
  const defaultRoot = path.join(base, 'profile')
  const selectionFile = path.join(base, 'profile-path.yaml')
  const setupFile = path.join(base, 'setup-complete.yaml')
  const index = process.argv.indexOf('--msgx-data-root')
  let selected
  let remember = index >= 0
  if (index >= 0) {
    selected = process.argv[index + 1]
    if (!selected || !path.isAbsolute(selected)) {
      throw new Error('--msgx-data-root 需要已有资料根目录的绝对路径。')
    }
  } else {
    try {
      selected = JSON.parse(fs.readFileSync(selectionFile, 'utf8'))
      if (typeof selected !== 'string' || !path.isAbsolute(selected)) {
        throw new Error('资料根必须是绝对路径。')
      }
    } catch (error) {
      if (error.code !== 'ENOENT') {
        throw new Error('无法读取已选择的资料目录：' + selectionFile, { cause: error })
      }
    }
  }

  // 已记住的选择和已在使用的安装版资料都优先于安装器所在位置。
  if (selected === undefined && !testBuild && !hasProfileData(defaultRoot)) {
    selected = sourceProfile(programRoot)
    remember = selected !== undefined
  }

  const root = selected === undefined ? defaultRoot : path.resolve(selected)
  if (root === defaultRoot) {
    fs.mkdirSync(root, { recursive: true })
  } else if (!['.ui', 'data', 'config.yaml'].some(name => fs.existsSync(path.join(root, name)))) {
    throw new Error('找不到原资料根目录。请用 --msgx-data-root 指定包含 .ui、data 或 config.yaml 的目录。')
  }

  return {
    root,
    setupPending: () => !fs.existsSync(setupFile),
    completeSetup() {
      fs.mkdirSync(base, { recursive: true })
      fs.writeFileSync(setupFile, 'true\n', 'utf8')
    },
    saveSelection() {
      if (!remember) return
      fs.mkdirSync(base, { recursive: true })
      // JSON 带引号字符串也是 YAML 标量。升级后的首次启动无需重传启动参数。
      const pending = selectionFile + '.' + process.pid + '.tmp'
      fs.writeFileSync(pending, JSON.stringify(root) + '\n', 'utf8')
      try {
        fs.renameSync(pending, selectionFile)
      } finally {
        fs.rmSync(pending, { force: true })
      }
    },
  }
}

module.exports = { installedProfile }
