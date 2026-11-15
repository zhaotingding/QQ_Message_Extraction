[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param([switch]$TestBuild)

$ErrorActionPreference = 'Stop'
$productName = if ($TestBuild) { '消息萃取（更新验收）' } else { '消息萃取' }
$executableName = if ($TestBuild) { 'msgx-update-test.exe' } else { 'msgx-desktop.exe' }
$registryRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'

# 只定位当前用户注册的指定产品；不猜安装位置，也不触碰源码版。
$entries = @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue |
    ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } |
    Where-Object { $_.DisplayName -eq $productName -or $_.DisplayName -eq "$productName $($_.DisplayVersion)" })
if ($entries.Count -ne 1) { throw "应找到一个当前用户安装的 $productName，实际找到 $($entries.Count) 个。" }
$entry = $entries[0]
# NSIS 的卸载注册项不含 InstallLocation；DisplayIcon 指向实际主程序。
$icon = [string]$entry.DisplayIcon
if (-not $icon.EndsWith(',0')) { throw '无法从注册项确认主程序位置。' }
$registeredExe = [IO.Path]::GetFullPath($icon.Substring(0, $icon.Length - 2).Trim('"'))
$installRoot = [IO.Path]::GetDirectoryName($registeredExe)
$command = [string]$entry.UninstallString
if ($command -notmatch '^"([^"\r\n]+)"(?:\s+/currentuser)?\s*$') {
    throw '卸载注册命令不是预期的当前用户 NSIS 入口。'
}
$uninstaller = [IO.Path]::GetFullPath($Matches[1])
if ([IO.Path]::GetDirectoryName($uninstaller) -ne $installRoot -or
    [IO.Path]::GetFileName($uninstaller) -ne "Uninstall $executableName" -or
    [IO.Path]::GetFileName($registeredExe) -ne $executableName -or
    -not (Test-Path -LiteralPath $uninstaller -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $installRoot $executableName) -PathType Leaf)) {
    throw '安装位置、主程序或卸载器不匹配；停止操作。'
}

# 用户确认后交给 NSIS 卸载；个人资料由既有保留策略保护，不自行删除目录。
if ($PSCmdlet.ShouldProcess("$productName $($entry.DisplayVersion) [$installRoot]", '卸载程序，保留个人资料')) {
    # NSIS 在部分执行策略下按进程名查找，源码版曾使用与正式安装版相同的名称。
    $processName = [IO.Path]::GetFileNameWithoutExtension($executableName)
    if (Get-Process -Name $processName -ErrorAction SilentlyContinue) {
        throw "还有 $executableName 正在运行。请正常退出同名应用后再卸载。"
    }
    $result = Start-Process -FilePath $uninstaller -ArgumentList '/S', '/currentuser' -WindowStyle Hidden -Wait -PassThru
    if ($result.ExitCode -ne 0) { throw "卸载器退出码：$($result.ExitCode)" }
}
