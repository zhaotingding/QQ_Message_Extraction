$ErrorActionPreference = 'Stop'
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$scratch = Join-Path $tempRoot ('msgx-relocation-test-' + [guid]::NewGuid())
$testDirectory = New-Item -ItemType Directory -Path $scratch
$localAppData = $env:LOCALAPPDATA
$helper = Join-Path $scratch 'validate-relocation.ps1'
$exeName = 'msgx-update-test.exe'
$link = Join-Path $scratch '程序联接'
$project = Join-Path $scratch '消息萃取项目'
$installed = Join-Path $project 'installed'
$oldProgramLink = Join-Path $installed '资料联接'
$envNames = @('MSGX_RELOCATION_OLD_DIRECTORY', 'MSGX_RELOCATION_NEW_DIRECTORY',
    'MSGX_RELOCATION_EXECUTABLE_NAME', 'MSGX_RELOCATION_UNINSTALL_COMMAND')
$previousEnv = @{}
foreach ($name in $envNames) { $previousEnv[$name] = [Environment]::GetEnvironmentVariable($name) }

function Assert-Validation([string]$Old, [string]$New, [string]$Command, [string]$Expected) {
    # 使用真实 Windows PowerShell 解析参数和退出码，避免依赖测试调用者的版本。
    $env:MSGX_RELOCATION_OLD_DIRECTORY = $Old
    $env:MSGX_RELOCATION_NEW_DIRECTORY = $New
    $env:MSGX_RELOCATION_EXECUTABLE_NAME = $exeName
    $env:MSGX_RELOCATION_UNINSTALL_COMMAND = $Command
    $errorFile = Join-Path $scratch 'relocation-error.txt'
    if (Test-Path -LiteralPath $errorFile) { Remove-Item -LiteralPath $errorFile }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $helper -FromEnvironment
    $exitCode = $LASTEXITCODE
    if (-not $Expected) {
        if ($exitCode -ne 0 -or (Test-Path -LiteralPath $errorFile)) {
            $detail = if (Test-Path -LiteralPath $errorFile) { [IO.File]::ReadAllText($errorFile, [Text.Encoding]::Unicode) }
            throw "合法迁移未通过预检：$detail"
        }
    } else {
        if ($exitCode -ne 1 -or -not (Test-Path -LiteralPath $errorFile)) { throw "应拒绝：$Expected" }
        $detail = [IO.File]::ReadAllText($errorFile, [Text.Encoding]::Unicode)
        if (-not $detail.Contains($Expected)) { throw "应拒绝：$Expected，实际：$detail" }
    }
}

try {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '../installer/validate-relocation.ps1') -Destination $helper
    $env:LOCALAPPDATA = Join-Path $scratch '本机资料'
    $old = Join-Path $scratch '原程序'
    $new = Join-Path $scratch '新程序'
    $uninstaller = Join-Path $old "Uninstall $exeName"
    New-Item -ItemType Directory -Path $old, $env:LOCALAPPDATA | Out-Null
    New-Item -ItemType File -Path $uninstaller, (Join-Path $old $exeName) | Out-Null
    $uninstallCommand = '"' + $uninstaller + '" /currentuser'

    Assert-Validation $old $new $uninstallCommand ''
    if (Test-Path -LiteralPath $new) { throw '预检不应创建新程序目录' }
    if (Get-ChildItem -LiteralPath $scratch -Force -Filter '.msgx-relocation-*') { throw '预检探针未删除' }
    Assert-Validation $old (Join-Path $old.ToUpperInvariant() '另一个位置') $uninstallCommand '与原程序目录分开'
    Assert-Validation $old $new ('"' + (Join-Path $scratch "Uninstall $exeName") + '" /currentuser') '与原程序目录不一致'

    New-Item -ItemType Directory -Path $new | Out-Null
    New-Item -ItemType File -Path (Join-Path $new '现有文件.txt') | Out-Null
    Assert-Validation $old $new $uninstallCommand '新的或空的程序文件夹'
    $nonempty = Join-Path $new '现有文件.txt'
    Assert-Validation $old (Join-Path $nonempty '无法创建') $uninstallCommand '不能包含文件'

    New-Item -ItemType Directory -Path (Join-Path $old '.ui') | Out-Null
    Assert-Validation $old (Join-Path $scratch '备选程序') $uninstallCommand '原程序目录内还有应用资料'
    Remove-Item -LiteralPath (Join-Path $old '.ui')

    $testProfileFolder = Join-Path $env:LOCALAPPDATA 'msgx-update-test'
    New-Item -ItemType Directory -Path $testProfileFolder | Out-Null
    $profile = Join-Path $scratch '保留资料'
    $preference = Join-Path $testProfileFolder 'profile-path.yaml'
    [IO.File]::WriteAllText($preference, (ConvertTo-Json $profile), [Text.UTF8Encoding]::new($false))
    Assert-Validation $old (Join-Path $profile '子目录') $uninstallCommand '个人资料目录分开'
    Assert-Validation $old (Join-Path $profile 'installed') $uninstallCommand '个人资料目录分开'
    Assert-Validation $old (Join-Path $env:LOCALAPPDATA 'msgx/profile') $uninstallCommand '个人资料目录分开'

    # 只有保存的源码项目根的直属 installed/ 能与资料同处一个项目。
    New-Item -ItemType Directory -Path (Join-Path $project 'src/msgx'), (Join-Path $project 'installer') | Out-Null
    New-Item -ItemType File -Path (Join-Path $project 'src/msgx/__init__.py') | Out-Null
    [IO.File]::WriteAllText($preference, (ConvertTo-Json $project), [Text.UTF8Encoding]::new($false))
    Assert-Validation $old $installed $uninstallCommand ''
    Assert-Validation $old $project $uninstallCommand '个人资料目录分开'
    Assert-Validation $old (Join-Path $project '.ui/installed') $uninstallCommand '个人资料目录分开'
    Assert-Validation $old (Join-Path $project 'data/installed') $uninstallCommand '个人资料目录分开'

    New-Item -ItemType Junction -Path $installed -Target $new | Out-Null
    Assert-Validation $old $installed $uninstallCommand '目录联接或符号链接'
    [IO.Directory]::Delete($installed, $false)

    New-Item -ItemType Directory -Path $installed | Out-Null
    $projectUninstaller = Join-Path $installed "Uninstall $exeName"
    New-Item -ItemType File -Path $projectUninstaller, (Join-Path $installed $exeName) | Out-Null
    $projectUninstallCommand = '"' + $projectUninstaller + '" /currentuser'
    Assert-Validation $installed (Join-Path $scratch '迁出程序') $projectUninstallCommand ''
    New-Item -ItemType Junction -Path $oldProgramLink -Target $project | Out-Null
    Assert-Validation $installed (Join-Path $scratch '迁出程序') $projectUninstallCommand '原程序目录内有目录联接'
    [IO.Directory]::Delete($oldProgramLink, $false)
    New-Item -ItemType Directory -Path (Join-Path $installed 'data') | Out-Null
    Assert-Validation $installed (Join-Path $scratch '迁出程序') $projectUninstallCommand '原程序目录内还有应用资料'

    New-Item -ItemType Junction -Path $link -Target $old | Out-Null
    Assert-Validation $old (Join-Path $link '新位置') $uninstallCommand '目录联接或符号链接'
    if (-not (Test-Path -LiteralPath $uninstaller)) { throw '预检修改了既有安装' }
    '迁移预检通过：新目录、重叠拒绝、卸载登记、非空目录、资料保护、目录联接、迁入或迁出项目专用子目录。'
} finally {
    $env:LOCALAPPDATA = $localAppData
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $previousEnv[$name]) }
    if ($testDirectory.FullName -ne $scratch -or
        -not $scratch.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw '测试目录边界异常' }
    if (Test-Path -LiteralPath $link) { [IO.Directory]::Delete($link, $false) }
    if (Test-Path -LiteralPath $oldProgramLink) { [IO.Directory]::Delete($oldProgramLink, $false) }
    $installedItem = Get-Item -LiteralPath $installed -Force -ErrorAction SilentlyContinue
    if ($installedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { [IO.Directory]::Delete($installed, $false) }
    Remove-Item -LiteralPath $scratch -Recurse -Force
}
