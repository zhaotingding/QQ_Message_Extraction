param([Parameter(Mandatory = $true)][string]$RequestFile)
# This helper is elevated by the GUI. Never terminate QQ by image name.
$ErrorActionPreference = 'Stop'

function Select-NapCatProcessIds($Processes, [string]$InstallDir, $InjectedIds = @()) {
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    $launcher = Join-Path $InstallDir 'NapCatWinBootMain.exe'
    foreach ($item in $Processes) {
        if ($item.ExecutablePath -and [string]::Equals($item.ExecutablePath, $launcher, [StringComparison]::OrdinalIgnoreCase)) {
            [void]$ids.Add([int]$item.ProcessId)
        }
    }
    foreach ($id in $InjectedIds) { [void]$ids.Add([int]$id) }
    do {
        $added = $false
        foreach ($item in $Processes) {
            if ($item.Name -in @('QQ.exe', 'NapCatWinBootMain.exe') -and
                $ids.Contains([int]$item.ParentProcessId) -and $ids.Add([int]$item.ProcessId)) { $added = $true }
        }
    } while ($added)
    return @($ids)
}

function Set-QuickAccount([string]$Account) {
    $config = Get-Content -LiteralPath $webuiFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $config | Add-Member -NotePropertyName autoLoginAccount -NotePropertyValue $Account -Force
    $tempConfig = "$webuiFile.msgx-restart.tmp"
    [IO.File]::WriteAllText($tempConfig, ($config | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempConfig -Destination $webuiFile -Force
}

function Call-LocalApi([string]$Path, $Body = @{}) {
    $uri = "http://127.0.0.1:$($webui.port)/api$Path"
    $request = [Net.HttpWebRequest]::Create($uri)
    $request.Proxy = $null
    $request.Method = 'POST'
    $request.ContentType = 'application/json'
    $request.Timeout = 3000
    $request.ReadWriteTimeout = 3000
    if ($script:credential) { $request.Headers['Authorization'] = "Bearer $script:credential" }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress))
    $request.ContentLength = $bytes.Length
    $stream = $request.GetRequestStream()
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    $response = $request.GetResponse()
    try {
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        try { $value = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
    if ($value.code -ne 0) { throw 'NapCat management request failed.' }
    return $value.data
}

$job = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$outcome = @{ scan = $false; note = '强制重启失败；请查看协议端日志' }
$restore = $false
$stage = '读取配置'
$mutex = $null
$locked = $false
try {
    $install = [IO.Path]::GetFullPath($job.dir).TrimEnd('\')
    if (!(Test-Path -LiteralPath (Join-Path $install 'napcat.mjs')) -or $job.account -notmatch '^\d+$') {
        throw 'Invalid NapCat target.'
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $mutexId = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($install.ToLowerInvariant())) | ForEach-Object { $_.ToString('x2') }) } finally { $sha.Dispose() }
    $mutex = [Threading.Mutex]::new($false, "Local\msgx-napcat-$mutexId")
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (!$locked) {
        $outcome.note = '此 NapCat 的强制重启仍在执行，本次未重复启动'
        throw 'Restart already running.'
    }
    $webuiFile = Join-Path $install 'config\webui.json'
    $webui = Get-Content -LiteralPath $webuiFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (!$webui.port) { $webui | Add-Member -NotePropertyName port -NotePropertyValue 6099 -Force }
    $saved = [string]$webui.autoLoginAccount
    if ($saved -and $saved -ne $job.account) {
        $outcome.note = '保存的 NapCat 自动登录账号与目标 QQ 不一致，未重启'
        throw 'Target account mismatch.'
    }
    # Match the exact injector path and injected QQ modules, then only their descendants.
    $stage = '定位目标进程'
    $processes = @(Get-CimInstance Win32_Process)
    $injected = @()
    $hook = Join-Path $install 'NapCatWinBootHook.dll'
    foreach ($qqProcess in @(Get-Process -Name QQ -ErrorAction SilentlyContinue)) {
        foreach ($module in @($qqProcess.Modules)) {
            if ([string]::Equals($module.FileName, $hook, [StringComparison]::OrdinalIgnoreCase)) {
                $injected += $qqProcess.Id
                break
            }
        }
    }
    $targets = @(Select-NapCatProcessIds $processes $install $injected)
    $stage = '结束目标进程'
    foreach ($targetId in $targets) {
        Stop-Process -Id $targetId -Force -ErrorAction SilentlyContinue
    }
    $stopDeadline = [DateTime]::UtcNow.AddSeconds(12)
    while ($targets.Count -and @(Get-Process -Id $targets -ErrorAction SilentlyContinue).Count -and [DateTime]::UtcNow -lt $stopDeadline) {
        Start-Sleep -Milliseconds 200
    }
    if ($targets.Count -and @(Get-Process -Id $targets -ErrorAction SilentlyContinue).Count) { throw 'Target process did not exit.' }
    Set-QuickAccount ''
    $restore = $true
    $stage = '启动扫码登录'
    foreach ($name in @('NAPCAT_QUICK_ACCOUNT', 'NAPCAT_QUICK_PASSWORD', 'NAPCAT_QUICK_PASSWORD_MD5')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:NAPCAT_PATCH_PACKAGE = Join-Path $install 'qqnt.json'
    $env:NAPCAT_LOAD_PATH = Join-Path $install 'loadNapCat.js'
    $env:NAPCAT_INJECT_PATH = $hook
    $env:NAPCAT_LAUNCHER_PATH = Join-Path $install 'NapCatWinBootMain.exe'
    $env:NAPCAT_MAIN_PATH = Join-Path $install 'napcat.mjs'
    if (!(Test-Path -LiteralPath $env:NAPCAT_LOAD_PATH)) {
        $mainUri = [Uri]::new($env:NAPCAT_MAIN_PATH).AbsoluteUri
        [IO.File]::WriteAllText($env:NAPCAT_LOAD_PATH, "(async () => {await import('$mainUri')})()", [Text.UTF8Encoding]::new($false))
    }
    $arguments = '"' + $job.qq + '" "' + $hook + '"'
    Start-Process -FilePath (Join-Path $install 'NapCatWinBootMain.exe') -ArgumentList $arguments -WorkingDirectory $install -WindowStyle Hidden | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(65)
    $qr = Join-Path $install 'cache\qrcode.png'
    $script:credential = ''
    $refreshed = $false
    $stage = '等待新二维码'
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            if (!$script:credential) {
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($webui.token + '.napcat')) | ForEach-Object { $_.ToString('x2') }) } finally { $sha.Dispose() }
                $auth = Call-LocalApi '/auth/login' @{ hash = $hash }
                $script:credential = $auth.Credential
                if (!$script:credential) { throw 'Management verification required.' }
            }
            $state = Call-LocalApi '/QQLogin/CheckLoginStatus'
            if (!$state.isLogin -and !$refreshed) {
                $qrRequested = [DateTime]::UtcNow
                $result = Call-LocalApi '/QQLogin/RefreshQRcode'
                if (!$result.restarting) { $refreshed = $true }
            }
            if ($refreshed -and (Test-Path -LiteralPath $qr) -and (Get-Item -LiteralPath $qr).LastWriteTimeUtc -ge $qrRequested) {
                $outcome = @{ scan = $true; qr_since = ([DateTimeOffset]::new($qrRequested).ToUnixTimeMilliseconds() / 1000.0) }
                break
            }
        } catch { $script:credential = '' }
        Start-Sleep -Milliseconds 500
    }
    if (!$outcome.scan) { $outcome.note = 'NapCat 已启动，但未取得本次新二维码；请查看协议端日志' }
} catch {
    # Do not emit exception bodies: management replies may contain credentials or QR URLs.
    if ($outcome.note -eq '强制重启失败；请查看协议端日志') {
        $outcome.note = "强制重启失败（阶段：$stage；类型：$($_.Exception.GetType().Name)）"
    }
} finally {
    if ($restore) {
        try {
            Set-QuickAccount $saved
            if ($script:credential) { Call-LocalApi '/QQLogin/SetQuickLoginQQ' @{ uin = $saved } | Out-Null }
        } catch { $outcome.note = '重启流程已结束，但自动登录设置未能恢复；请检查 NapCat 配置' }
    }
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
    [IO.File]::WriteAllText($job.result, ($outcome | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}
