const fs = require('node:fs')
const path = require('node:path')

function installedProfile(app, testBuild) {
  const base = path.join(process.env.LOCALAPPDATA || app.getPath('appData'),
    testBuild ? 'msgx-update-test' : 'msgx')
  const defaultRoot = path.join(base, 'profile')
  const selectionFile = path.join(base, 'profile-path.yaml')
  const index = process.argv.indexOf('--msgx-data-root')
  let selected
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

  const root = selected === undefined ? defaultRoot : path.resolve(selected)
  if (root === defaultRoot) {
    fs.mkdirSync(root, { recursive: true })
  } else if (!['.ui', 'data', 'config.yaml'].some(name => fs.existsSync(path.join(root, name)))) {
    throw new Error('找不到原资料根目录。请用 --msgx-data-root 指定包含 .ui、data 或 config.yaml 的目录。')
  }

  return {
    root,
    saveSelection() {
      if (index < 0) return
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
