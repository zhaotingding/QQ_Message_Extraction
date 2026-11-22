import fs from 'node:fs'
import path from 'node:path'

if (process.platform === 'win32') {
  const { Data, NtExecutable, NtExecutableResource, Resource } = await import('resedit')
  const desktop = import.meta.dirname
  const { name, productName } = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8'))
  const dist = path.join(desktop, 'node_modules', 'electron', 'dist')
  const filename = `${name}.exe`
  const executable = NtExecutable.from(fs.readFileSync(path.join(dist, 'electron.exe')), { ignoreCert: true })
  const resources = NtExecutableResource.from(executable)

  // 写入 Windows 进程文件身份，并用项目默认图标替换 Electron 图标。
  for (const version of Resource.VersionInfo.fromEntries(resources.entries)) {
    for (const language of version.getAllLanguagesForStringValues()) {
      version.setStringValues(language, {
        FileDescription: productName,
        ProductName: productName,
        InternalName: filename,
        OriginalFilename: filename,
      })
    }
    version.outputToResourceEntries(resources.entries)
  }
  const icon = Data.IconFile.from(fs.readFileSync(path.join(desktop, '..', 'assets', 'app-icon.ico')))
  for (const group of Resource.IconGroupEntry.fromEntries(resources.entries)) {
    Resource.IconGroupEntry.replaceIconsForResource(resources.entries, group.id, group.lang,
      icon.icons.map(item => item.data))
  }
  resources.outputResource(executable)

  // 同目录复用运行时文件，并保留与 EXE 同名的 Windows 崩溃报告库。
  fs.copyFileSync(path.join(dist, 'electron_wer.dll'), path.join(dist, `${name}_wer.dll`))
  const pending = path.join(dist, `.${filename}.${process.pid}.tmp`)
  try {
    fs.writeFileSync(pending, Buffer.from(executable.generate()))
    fs.renameSync(pending, path.join(dist, filename))
  } finally {
    fs.rmSync(pending, { force: true })
  }
  console.log(`已准备 ${productName} 的界面进程：${filename}`)
}
