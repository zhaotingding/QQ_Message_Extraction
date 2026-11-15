# 使用临时空文件和注册表替身，绝不调用真实卸载器。
$ErrorActionPreference = 'Stop'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('msgx-uninstall-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $scratch | Out-Null
$product = '消息萃取（更新验收）'
$global:MsgxUninstallTestEntry = [pscustomobject]@{
    DisplayName = "$product 1.0.10"
    DisplayVersion = '1.0.10'
    DisplayIcon = (Join-Path $scratch 'msgx-update-test.exe') + ',0'
    UninstallString = '"' + (Join-Path $scratch 'Uninstall msgx-update-test.exe') + '" /currentuser'
}
New-Item -ItemType File -Path (Join-Path $scratch 'msgx-update-test.exe') | Out-Null
New-Item -ItemType File -Path (Join-Path $scratch 'Uninstall msgx-update-test.exe') | Out-Null
$global:MsgxUninstallTestLaunches = 0
$global:MsgxUninstallTestRunning = $false
function Get-ChildItem { [pscustomobject]@{ PSPath = 'fixture' } }
function Get-ItemProperty { $global:MsgxUninstallTestEntry }
function Get-Process { if ($global:MsgxUninstallTestRunning) { [pscustomobject]@{ Id = 123 } } }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$Wait, [switch]$PassThru)
    if ($FilePath -ne (Join-Path $scratch 'Uninstall msgx-update-test.exe') -or
        ($ArgumentList -join ' ') -ne '/S /currentuser') { throw '调用了错误的卸载器或参数' }
    $global:MsgxUninstallTestLaunches++
    [pscustomobject]@{ ExitCode = 0 }
}
$target = Join-Path $PSScriptRoot '../installer/uninstall.ps1'
function Expect-Rejection($action) {
    $rejected = $false
    try { & $action } catch { $rejected = $true }
    if (-not $rejected -or $global:MsgxUninstallTestLaunches -ne 0) { throw '错误目标未被拦截' }
}
try {
    & $target -TestBuild -WhatIf
    if ($global:MsgxUninstallTestLaunches) { throw '预览不应启动卸载器' }
    Expect-Rejection { & $target -Confirm:$false } # 测试产品不能被正式卸载入口选中。
    $valid = $global:MsgxUninstallTestEntry.UninstallString
    $global:MsgxUninstallTestEntry.UninstallString = '"C:\elsewhere\Uninstall wrong.exe" /currentuser'
    Expect-Rejection { & $target -TestBuild -Confirm:$false }
    $global:MsgxUninstallTestEntry.UninstallString = $valid + ' /unexpected'
    Expect-Rejection { & $target -TestBuild -Confirm:$false }
    $global:MsgxUninstallTestEntry.UninstallString = $valid
    $global:MsgxUninstallTestRunning = $true
    Expect-Rejection { & $target -TestBuild -Confirm:$false }
    $global:MsgxUninstallTestRunning = $false
    & $target -TestBuild -Confirm:$false
    if ($global:MsgxUninstallTestLaunches -ne 1) { throw '合法目标未调用一次' }
    '卸载入口：预览、产品隔离、路径和参数校验、同名进程保护及合法调用通过。'
} finally {
    # scratch 是本测试刚创建的唯一目录；清理前核实仍在系统临时目录下。
    $resolved = [IO.Path]::GetFullPath($scratch)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase)) {
        throw '临时目录边界异常'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
