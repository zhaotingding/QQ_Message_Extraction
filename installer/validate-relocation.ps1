param(
    [string]$OldDirectory,
    [string]$NewDirectory,
    [string]$ExecutableName,
    [string]$UninstallCommand,
    [switch]$FromEnvironment
)

$ErrorActionPreference = 'Stop'
if ($FromEnvironment) {
    $OldDirectory = $env:MSGX_RELOCATION_OLD_DIRECTORY
    $NewDirectory = $env:MSGX_RELOCATION_NEW_DIRECTORY
    $ExecutableName = $env:MSGX_RELOCATION_EXECUTABLE_NAME
    $UninstallCommand = $env:MSGX_RELOCATION_UNINSTALL_COMMAND
}

function Get-CheckedLocation([string]$Value, [string]$Label) {
    # 拒绝设备路径、盘相对路径和 Win32 会忽略的尾随字符。
    if ($Value -notmatch '^[a-zA-Z]:[\\/]' -or $Value.Substring(2) -match '[:*?"<>|\x00-\x1F]' -or
        ($Value -split '[\\/]' | Where-Object { $_ -match '[. ]$' -and $_ -notin @('.', '..') })) {
        throw "$Label 需要有效的本地绝对路径。"
    }
    $full = [IO.Path]::GetFullPath($Value.Replace('/', '\'))
    $ancestor = [IO.DirectoryInfo]::new($full)
    $existing = $null
    while ($ancestor) {
        try { $item = Get-Item -LiteralPath $ancestor.FullName -Force }
        catch [System.Management.Automation.ItemNotFoundException] {
            $ancestor = $ancestor.Parent
            continue
        }
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "$Label 不能包含文件、目录联接或符号链接。"
        }
        if (-not $existing) { $existing = $ancestor.FullName }
        $ancestor = $ancestor.Parent
    }
    if (-not $existing) { throw "无法访问 $Label 所在磁盘。" }

    # 比较实际卷上的路径，避免盘符别名或 8.3 短名称绕过重叠检查。
    $verified = [MsgxRelocationLocation]::ForDirectory($existing).TrimEnd('\')
    $suffix = $full.Substring($existing.TrimEnd('\').Length).Trim('\')
    $key = if ($suffix) { "$verified\$suffix" } else { $verified }
    [pscustomobject]@{ Full = $full.TrimEnd('\'); Key = $key; Existing = $existing }
}

function Test-Overlap([string]$Left, [string]$Right) {
    $comparison = [StringComparison]::OrdinalIgnoreCase
    return $Left.Equals($Right, $comparison) -or $Left.StartsWith($Right + '\', $comparison) -or
        $Right.StartsWith($Left + '\', $comparison)
}

try {
    $profileName = switch ($ExecutableName) {
        'msgx-desktop.exe' { 'msgx' }
        'msgx-update-test.exe' { 'msgx-update-test' }
        default { throw '无法识别现有安装的产品身份。' }
    }
    if ($UninstallCommand -notmatch '^"([^"\r\n]+)"(?:\s+/currentuser)?\s*$') {
        throw '原卸载登记无效，无法安全迁移。'
    }
    $registeredUninstaller = $Matches[1]
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class MsgxRelocationLocation {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
        IntPtr security, uint mode, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path,
        uint length, uint flags);

    public static string ForDirectory(string path) {
        using (var handle = CreateFile(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            var result = new StringBuilder(32768);
            uint length = handle.IsInvalid ? 0 :
                GetFinalPathNameByHandle(handle, result, (uint)result.Capacity, 1);
            if (length == 0 || length >= result.Capacity) {
                throw new IOException("Cannot verify the physical directory path.");
            }
            return result.ToString();
        }
    }
}
'@

    $old = Get-CheckedLocation $OldDirectory '原程序目录'
    $new = Get-CheckedLocation $NewDirectory '新程序目录'
    if (Test-Overlap $old.Key $new.Key) { throw '新程序目录必须与原程序目录分开。' }
    if (-not (Test-Path -LiteralPath $old.Full -PathType Container)) { throw '原程序目录不存在。' }
    if ($old.Full -eq [IO.Path]::GetPathRoot($OldDirectory).TrimEnd('\') -or
        $new.Full -eq [IO.Path]::GetPathRoot($NewDirectory).TrimEnd('\')) {
        throw '不能使用磁盘根目录。'
    }

    $expectedUninstaller = Join-Path $old.Full "Uninstall $ExecutableName"
    if (-not $registeredUninstaller.Equals($expectedUninstaller, [StringComparison]::OrdinalIgnoreCase)) {
        throw '原卸载登记与原程序目录不一致。'
    }
    foreach ($file in @($expectedUninstaller, (Join-Path $old.Full $ExecutableName))) {
        $item = Get-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        if (-not $item -or $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw '原程序或卸载器不完整，无法安全迁移。'
        }
    }
    foreach ($name in @('.ui', 'data', 'config.yaml')) {
        if (Test-Path -LiteralPath (Join-Path $old.Full $name)) {
            throw '原程序目录内还有应用资料，无法安全迁移。'
        }
    }
    # 旧版卸载器递归清理程序目录。不要让程序里的目录联接指向原资料或其他位置。
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($old.Full)
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath ($pending.Pop()) -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw '原程序目录内有目录联接或符号链接，无法安全迁移。'
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }

    # 程序卸载器会移除整个原程序目录。默认根和已记住的资料根都要保护。
    if (-not $env:LOCALAPPDATA) { throw '无法确认当前用户的资料位置。' }
    $protected = foreach ($name in @('msgx', 'msgx-update-test')) {
        @{ Path = (Join-Path $env:LOCALAPPDATA $name); AllowProjectInstall = $false }
    }
    $selectionFile = Join-Path (Join-Path $env:LOCALAPPDATA $profileName) 'profile-path.yaml'
    if (Test-Path -LiteralPath $selectionFile) {
        try { $selected = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($selectionFile)) }
        catch { throw '无法读取已保存的资料位置，已停止迁移。' }
        if ($selected -isnot [string]) { throw '已保存的资料位置无效，已停止迁移。' }
        $protected += @{ Path = $selected; AllowProjectInstall = $true }
    }
    foreach ($folder in $protected) {
        $profile = Get-CheckedLocation $folder.Path '个人资料目录'
        $allowed = $null
        # 原资料根就是源码项目时，可把程序放在其专用子目录。卸载只处理 installed/。
        if ($folder.AllowProjectInstall -and
            (Test-Path -LiteralPath (Join-Path $profile.Full 'src/msgx/__init__.py') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $profile.Full 'installer') -PathType Container)) {
            $allowed = $profile.Key + '\installed'
        }
        foreach ($program in $old, $new) {
            if (-not $program.Key.Equals($allowed, [StringComparison]::OrdinalIgnoreCase) -and
                (Test-Overlap $program.Key $profile.Key)) {
                throw '原目录和新目录都必须与个人资料目录分开。'
            }
        }
    }

    if (Test-Path -LiteralPath $new.Full) {
        if (Get-ChildItem -LiteralPath $new.Full -Force | Select-Object -First 1) {
            throw '请选择一个新的或空的程序文件夹。'
        }
    }

    # 在最近的现有目标目录试写独占探针，关闭句柄即删除，不创建所选目录。
    $probe = Join-Path $new.Existing ('.msgx-relocation-' + [guid]::NewGuid())
    try {
        $stream = [IO.FileStream]::new($probe, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
            [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
        try { $stream.WriteByte(1) } finally { $stream.Dispose() }
    } catch {
        throw '新程序目录或其父目录不可写，请选择其他位置。'
    }
} catch {
    $message = $_.Exception.Message -split '\r?\n' | Select-Object -First 1
    if ($FromEnvironment) {
        [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'relocation-error.txt'), "$message`r`n",
            [Text.UnicodeEncoding]::new($false, $false))
    } else {
        [Console]::Error.WriteLine($message)
    }
    exit 1
}
exit 0
