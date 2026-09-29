param([switch]$RegisterShortcutOnly, [switch]$UseDesktopShortcut)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$desktopRoot = Join-Path $repositoryRoot 'desktop'
$logRoot = Join-Path $repositoryRoot '.inkflow-dev'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

# Register only this repository's development entry. Do not replace an installed
# InkFlow shortcut or another Electron application's shortcut.
$inkflowPrograms = [Environment]::GetFolderPath('Programs')
$inkflowIcon = Join-Path $desktopRoot 'resources\icon.ico'
$inkflowElectron = Join-Path $desktopRoot 'node_modules\electron\dist\electron.exe'
$inkflowLaunchScript = Join-Path $PSScriptRoot 'start-desktop-hidden.ps1'
$inkflowSilentLauncher = Join-Path $PSScriptRoot 'start-desktop-hidden.vbs'
$inkflowWScript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$inkflowPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$inkflowShortcutPath = Join-Path $inkflowPrograms '墨流（本地开发）.lnk'
if ((Test-Path -LiteralPath $inkflowPrograms) -and (Test-Path -LiteralPath $inkflowIcon)) {
    $inkflowShortcutShell = New-Object -ComObject WScript.Shell
    $inkflowShortcut = $inkflowShortcutShell.CreateShortcut($inkflowShortcutPath)
    $inkflowShortcut.TargetPath = $inkflowWScript
    $inkflowShortcut.Arguments = "`"$inkflowSilentLauncher`""
    $inkflowShortcut.WorkingDirectory = $repositoryRoot
    $inkflowShortcut.IconLocation = "$inkflowIcon,0"
    $inkflowShortcut.Description = '墨流 · 墨宝小说工作台（本机开发版）'
    $inkflowShortcut.WindowStyle = 1
    $inkflowShortcut.Save()

    $inkflowLegacyShortcutPath = Join-Path $inkflowPrograms 'Electron.lnk'
    if (Test-Path -LiteralPath $inkflowLegacyShortcutPath) {
        $inkflowLegacyShortcut = $inkflowShortcutShell.CreateShortcut($inkflowLegacyShortcutPath)
        if ([string]::Equals($inkflowLegacyShortcut.TargetPath, $inkflowElectron, [StringComparison]::OrdinalIgnoreCase) -and [string]::IsNullOrWhiteSpace($inkflowLegacyShortcut.Arguments)) {
            $inkflowShortcutBackup = Join-Path $logRoot 'shortcut-backup'
            New-Item -ItemType Directory -Force -Path $inkflowShortcutBackup | Out-Null
            $inkflowBackupPath = Join-Path $inkflowShortcutBackup ('Electron-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff') + '.lnk')
            Move-Item -LiteralPath $inkflowLegacyShortcutPath -Destination $inkflowBackupPath
            Write-Output "已替换墨流的旧 Electron 开始菜单入口；原快捷方式可从 $inkflowBackupPath 恢复。"
        }
    }
}

if ($UseDesktopShortcut) {
    $inkflowDesktopShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) '墨流 InkFlow.lnk'
    $inkflowShortcutShell = New-Object -ComObject WScript.Shell
    if (Test-Path -LiteralPath $inkflowDesktopShortcutPath) {
        $existingShortcut = $inkflowShortcutShell.CreateShortcut($inkflowDesktopShortcutPath)
        if (-not [string]::Equals($existingShortcut.TargetPath, $inkflowWScript, [StringComparison]::OrdinalIgnoreCase)) {
            $inkflowShortcutBackup = Join-Path $logRoot 'shortcut-backup'
            New-Item -ItemType Directory -Force -Path $inkflowShortcutBackup | Out-Null
            $inkflowBackupPath = Join-Path $inkflowShortcutBackup ('墨流 InkFlow-before-local-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff') + '.lnk')
            Copy-Item -LiteralPath $inkflowDesktopShortcutPath -Destination $inkflowBackupPath
            Write-Output "桌面原快捷方式已备份：$inkflowBackupPath"
        }
    }
    $desktopShortcut = $inkflowShortcutShell.CreateShortcut($inkflowDesktopShortcutPath)
    $desktopShortcut.TargetPath = $inkflowWScript
    $desktopShortcut.Arguments = "`"$inkflowSilentLauncher`""
    $desktopShortcut.WorkingDirectory = $repositoryRoot
    $desktopShortcut.IconLocation = "$inkflowIcon,0"
    $desktopShortcut.Description = '墨流 · 墨宝小说工作台（本机开发版，跟随当前源码）'
    $desktopShortcut.WindowStyle = 1
    $desktopShortcut.Save()
    Write-Output "桌面墨流入口现指向本机开发版：$inkflowDesktopShortcutPath"
}

if ($RegisterShortcutOnly) {
    Write-Output "墨宝启动入口已登记：$inkflowShortcutPath"
    return
}

# Reopening the taskbar entry while InkFlow is running only activates the
# existing app through its single-instance handler, without another Vite server.
$startupMutex = [Threading.Mutex]::new($false, 'Local\InkFlowDevStartup')
$startupLockHeld = $false
try {
    try { $startupLockHeld = $startupMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $startupLockHeld = $true }
    if (-not $startupLockHeld) { return }

$inkflowRunning = Get-CimInstance Win32_Process -Filter "Name='electron.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $inkflowElectron -and $_.CommandLine -notmatch '(?:^|\s)--type=' } |
    Select-Object -First 1
if ($inkflowRunning) {
    Start-Process -FilePath $inkflowElectron -ArgumentList @("`"$desktopRoot`"") -WorkingDirectory $desktopRoot -WindowStyle Hidden
    Write-Output '墨流已在运行，已请求显示现有窗口。'
    return
}

$windowsSystem32 = Join-Path $env:SystemRoot 'System32'
$env:Path = "$windowsSystem32;$env:Path"
$env:ComSpec = Join-Path $windowsSystem32 'cmd.exe'
$runLogRoot = Join-Path $logRoot 'run'
New-Item -ItemType Directory -Force -Path $runLogRoot | Out-Null
$launchId = [Guid]::NewGuid().ToString('N')
$stdout = Join-Path $runLogRoot "desktop-$launchId.stdout.log"
$stderr = Join-Path $runLogRoot "desktop-$launchId.stderr.log"

function Show-InkFlowStartupFailure([string]$Message) {
    try {
        if (-not ('InkFlowStartupNotice' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class InkFlowStartupNotice {
    [DllImport("user32.dll", EntryPoint = "MessageBoxW", CharSet = CharSet.Unicode)]
    public static extern int Show(IntPtr owner, string text, string caption, uint type);
}
'@ -ErrorAction Stop | Out-Null
        }
        [InkFlowStartupNotice]::Show([IntPtr]::Zero, $Message, '墨流启动提示', 0x10) | Out-Null
    } catch {
        try { Add-Content -LiteralPath $stderr -Value "Unable to show startup notice: $($_.Exception.Message)" -Encoding UTF8 } catch { }
    }
}

function Get-RecentStartupLogText([string]$Path) {
    try {
        $lines = @(Get-Content -LiteralPath $Path -Tail 8 -ErrorAction Stop | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $escapePattern = [regex]::Escape([string][char]27) + '\[[0-?]*[ -/]*[@-~]'
        $text = ($lines -join "`n") -replace $escapePattern, ''
        $text = $text -replace '(?i)(bearer\s+|api[_ -]?key\s*[:=]\s*)[^\s,;]+', '$1[已隐藏]'
        if ($text.Length -gt 700) { $text = $text.Substring($text.Length - 700) }
        return $text
    } catch {
        return ''
    }
}

function Get-DescendantProcessIds([int]$RootProcessId, [object[]]$Processes) {
    $ids = @{}
    $ids[$RootProcessId] = $true
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($candidate in $Processes) {
            $processId = [int]$candidate.ProcessId
            $parentId = [int]$candidate.ParentProcessId
            if (-not $ids.ContainsKey($processId) -and $ids.ContainsKey($parentId)) {
                $ids[$processId] = $true
                $changed = $true
            }
        }
    }
    return $ids
}

try {
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $nodeRoot = Split-Path -Parent $node
    $npmCli = Join-Path $nodeRoot 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $npmCli)) {
        throw "npm CLI not found next to node.exe: $npmCli"
    }
    $process = Start-Process `
        -FilePath $node `
        -ArgumentList @("`"$npmCli`"", 'run', 'dev') `
        -WorkingDirectory $desktopRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru `
        -ErrorAction Stop
} catch {
    $reason = $_.Exception.Message
    try { Add-Content -LiteralPath $stderr -Value $reason -Encoding UTF8 } catch { }
    Show-InkFlowStartupFailure "墨流开发版没有启动。`n原因：$reason`n`n本次启动日志：`n$stdout`n$stderr"
    return
}

# Confirm a visible main window from this exact npm launch, not an old log line
# or another Electron process. Vite, TypeScript and Electron remain hidden.
$startupTimeoutSeconds = 90
$deadline = (Get-Date).AddSeconds($startupTimeoutSeconds)
$ready = $false
$failure = ''
while ((Get-Date) -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) {
        $process.WaitForExit()
        $failure = "启动进程提前退出，代码 $($process.ExitCode)。"
        break
    }

    $snapshot = @(Get-CimInstance Win32_Process -Filter "Name='electron.exe' OR Name='node.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue)
    if ($snapshot.Count -gt 0) {
        $descendants = Get-DescendantProcessIds -RootProcessId $process.Id -Processes $snapshot
        $mainWindows = @($snapshot | Where-Object {
            $descendants.ContainsKey([int]$_.ProcessId) -and
            [string]::Equals([string]$_.ExecutablePath, $inkflowElectron, [StringComparison]::OrdinalIgnoreCase) -and
            [string]$_.CommandLine -notmatch '(?:^|\s)--type='
        })
        foreach ($candidate in $mainWindows) {
            try {
                $windowProcess = Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction Stop
                if ($windowProcess.MainWindowHandle -ne [IntPtr]::Zero) {
                    $ready = $true
                    break
                }
            } catch { }
        }
    }
    if ($ready) { break }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    $process.Refresh()
    if (-not $failure) {
        if ($process.HasExited) {
            $failure = "启动进程提前退出，代码 $($process.ExitCode)。"
        } else {
            $failure = "等待 $startupTimeoutSeconds 秒后仍未确认墨流主窗口就绪；进程可能仍在初始化，本脚本不会重启或强制结束它。"
        }
    }
    $logTail = Get-RecentStartupLogText $stdout
    if (-not $logTail) { $logTail = Get-RecentStartupLogText $stderr }
    $message = "墨流开发版启动未确认。`n$failure`n"
    if ($logTail) { $message += "`n最近日志：`n$logTail`n" }
    $message += "`n本次启动日志：`n$stdout`n$stderr"
    Show-InkFlowStartupFailure $message
    return
}

Write-Output "墨流开发版窗口已就绪（启动 PID $($process.Id)）。本次日志：$stdout；$stderr"
} finally {
    if ($startupLockHeld) { $startupMutex.ReleaseMutex() }
    $startupMutex.Dispose()
}
